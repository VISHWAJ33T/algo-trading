"""Regression tests for the 03-Aug-2026 failure.

Three independent defects combined to produce "three buy entries, no exits,
wrong strike, and an 87 MB log":

- LiveFeed reconnected itself on every disconnect, which spawned a new socket
  per subscribe call and leaked threads until the process died.
- The NIFTY 50 scrip lookup could never match, so strike selection silently
  used the futures price.
- Position state lived only in memory, so each restart abandoned the open
  trade and re-entered instead of managing it.
"""
import datetime as dt
import math
import logging

import pytest

from src import storage
from src.config import Config
from src.execution import OrderExecutor
from src.instruments import Instrument, _is_nifty_50_row
from src.logging_config import DuplicateSuppressor
from src.risk import RiskManager
from src.ws_feed import LiveFeed

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test_bot.db")
    storage.init_db()


def _config(live_trading: bool = False) -> Config:
    return Config(
        consumer_key="k", environment="prod", mobile_number="m", ucc="u", mpin="p", totp_secret="s",
        live_trading=live_trading, lot_size=1, max_daily_loss=None,
        target_underlying_points=30.0, target_premium_points=15.0, log_level="INFO",
    )


def _executor(live_trading: bool = False) -> OrderExecutor:
    return OrderExecutor(session=None, config=_config(live_trading), risk=RiskManager(1, None))


def _record_open_trade(session_day: str = "2026-08-03", live_trading: bool = False, **overrides) -> int:
    y, m, d = (int(p) for p in session_day.split("-"))
    kwargs = dict(
        option_type="CE", trading_symbol="NIFTY2680424550CE", quantity=65,
        entry_time=dt.datetime(y, m, d, 9, 20, tzinfo=IST), entry_price=43.8,
        live_trading=live_trading, instrument_token="45678", exchange_segment="nse_fo",
        underlying_entry_price=24530.0,
    )
    kwargs.update(overrides)
    return storage.record_trade_entry(**kwargs)


# --- the websocket reconnect storm -----------------------------------------

class _FakeNeoWebSocket:
    """The SDK object whose `is_hsw_open` flag decides, inside get_live_feed(),
    whether a subscribe reuses the live socket or opens a second one."""

    def __init__(self, is_hsw_open=1):
        self.is_hsw_open = is_hsw_open
        self.hsWebsocket = self
        self.closed = 0

    def close(self):
        self.closed += 1


class _FakeClient:
    """Stands in for the Kotak SDK client, counting how many subscriptions it
    is asked for and how many sockets that would have created."""

    def __init__(self, is_hsw_open=1):
        self.subscriptions: list[tuple[str, bool]] = []
        self.on_message = self.on_error = self.on_close = None
        self.NeoWebSocket = _FakeNeoWebSocket(is_hsw_open)
        # Mirrors the SDK: a subscribe issued while the socket is not open
        # spawns a whole new connection.
        self.sockets_created = 0

    def subscribe(self, instrument_tokens, isIndex):
        if self.NeoWebSocket is None:
            self.NeoWebSocket = _FakeNeoWebSocket()
        if self.NeoWebSocket.is_hsw_open != 1:
            self.sockets_created += 1
        self.subscriptions.append((instrument_tokens[0]["instrument_token"], isIndex))


class _FakeSession:
    def __init__(self, is_hsw_open=1):
        self.client = _FakeClient(is_hsw_open)


def _feed(is_hsw_open=1) -> tuple[LiveFeed, _FakeClient]:
    session = _FakeSession(is_hsw_open)
    feed = LiveFeed(
        session,
        Instrument("111", "nse_fo", "NIFTY26AUGFUT", 65, dt.date(2026, 8, 25)),
        index_instrument=Instrument("26000", "nse_cm", "NIFTY 50", 0),
    )
    return feed, session.client


def test_disconnect_does_not_resubscribe():
    """The SDK reconnects and replays subscriptions itself. Doing it here too
    is what span 101,104 sockets on 03-Aug-2026."""
    feed, client = _feed()
    feed.start()
    assert len(client.subscriptions) == 2

    for _ in range(50):
        feed._on_close()

    assert len(client.subscriptions) == 2


def test_start_is_idempotent():
    feed, client = _feed()
    feed.start()
    feed.start()
    feed.start()
    assert len(client.subscriptions) == 2


def test_both_legs_are_subscribed_including_the_index():
    """The index leg used to be skipped whenever the futures leg raised, which
    left strike selection with no spot price at all."""
    feed, client = _feed()
    feed.start()
    assert client.subscriptions == [("111", False), ("26000", True)]


def test_index_subscription_survives_a_failing_futures_subscription(caplog):
    feed, client = _feed()
    calls = []

    def flaky_subscribe(instrument_tokens, isIndex):
        calls.append((instrument_tokens[0]["instrument_token"], isIndex))
        if not isIndex:
            raise RuntimeError("futures subscribe failed")

    client.subscribe = flaky_subscribe
    with caplog.at_level(logging.ERROR):
        feed.start()

    assert ("26000", True) in calls


def test_both_legs_land_on_one_socket():
    """04-Aug-2026: both legs were subscribed back to back while the connect
    handshake was still in flight, so the SDK opened a second socket. The two
    then fought over HSWebSocketLib's module-level `ws` global and neither
    completed its handshake, leaving the feed dead for 6.4 hours."""
    feed, client = _feed(is_hsw_open=1)
    feed.start()
    assert len(client.subscriptions) == 2
    assert client.sockets_created == 0


def test_index_leg_waits_for_the_socket_handshake(monkeypatch):
    """The second subscribe must wait for is_hsw_open to flip.

    The handshake here completes on a timer, the way the real connect
    acknowledgement arrives over the network. Code that fires both subscribes
    back to back sees is_hsw_open still 0 for the second and opens a competing
    socket; only code that waits reuses the first one.
    """
    import threading

    feed, client = _feed(is_hsw_open=0)
    monkeypatch.setattr("src.ws_feed.SOCKET_READY_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr("src.ws_feed.SOCKET_READY_POLL_SECONDS", 0.02)

    real_subscribe = client.subscribe

    def subscribe_then_open_later(instrument_tokens, isIndex):
        first = not client.subscriptions
        real_subscribe(instrument_tokens, isIndex)
        if first:
            threading.Timer(0.15, lambda: setattr(client.NeoWebSocket, "is_hsw_open", 1)).start()

    client.subscribe = subscribe_then_open_later
    feed.start()

    assert [s[1] for s in client.subscriptions] == [False, True]
    # One socket for the futures leg; the index leg reused it instead of
    # spawning the second connection that broke 04-Aug-2026.
    assert client.sockets_created == 1


def test_feed_restart_replaces_the_socket_rather_than_adding_one():
    feed, client = _feed()
    feed.start()
    original = client.NeoWebSocket

    assert feed.restart() is True

    assert original.closed == 1          # the old socket was actually closed
    assert client.NeoWebSocket is not original  # and replaced, not accumulated
    assert len(client.subscriptions) == 4        # both legs re-subscribed


def test_feed_restart_is_a_no_op_after_stop():
    feed, client = _feed()
    feed.start()
    feed.stop()
    assert feed.restart() is False
    assert len(client.subscriptions) == 2


def test_seconds_since_start_covers_a_feed_that_never_ticked():
    feed, _ = _feed()
    assert feed.seconds_since_start() is None
    feed.start()
    assert feed.seconds_since_start() is not None
    assert feed.seconds_since_last_tick() is None


def test_feed_staleness_is_reported():
    feed, _ = _feed()
    assert feed.seconds_since_last_tick() is None

    feed._handle_tick({"tk": "111", "ltp": "24618.0"})
    assert feed.seconds_since_last_tick() < 1.0
    # A futures tick must not be mistaken for a spot index tick.
    assert feed.seconds_since_last_index_tick() is None

    feed._handle_tick({"tk": "26000", "iv": "24530.0"})
    assert feed.seconds_since_last_index_tick() < 1.0
    assert feed.latest_index_ltp == 24530.0


def test_stalled_feed_is_rebuilt_after_the_escalation_threshold(monkeypatch):
    """A warning was not enough on 04-Aug-2026: the bot logged 375 stall
    warnings over 6.4 hours and never recovered. It must now escalate."""
    from src import main

    feed, _ = _feed()
    feed.start()
    restarts = []
    monkeypatch.setattr(feed, "restart", lambda: restarts.append(1) or True)
    # Silent for longer than the escalation threshold.
    monkeypatch.setattr(feed, "seconds_since_last_tick", lambda: main.FEED_RESTART_AFTER_SECONDS + 1)

    last = main._supervise_feed(feed, dt.time(10, 0), last_restart=0.0)

    assert restarts == [1]
    assert last > 0.0


def test_feed_rebuilds_are_rate_limited(monkeypatch):
    """The recovery path must not become a reconnect loop of its own."""
    from src import main
    import time as time_module

    feed, _ = _feed()
    feed.start()
    restarts = []
    monkeypatch.setattr(feed, "restart", lambda: restarts.append(1) or True)
    monkeypatch.setattr(feed, "seconds_since_last_tick", lambda: main.FEED_RESTART_AFTER_SECONDS + 1)

    last = main._supervise_feed(feed, dt.time(10, 0), last_restart=0.0)
    for _ in range(50):
        last = main._supervise_feed(feed, dt.time(10, 0), last_restart=last)

    assert restarts == [1]
    # ...until the interval has elapsed.
    stale = time_module.monotonic() - main.FEED_RESTART_INTERVAL_SECONDS - 1
    main._supervise_feed(feed, dt.time(10, 0), last_restart=stale)
    assert restarts == [1, 1]


def test_a_healthy_feed_is_never_rebuilt(monkeypatch):
    from src import main

    feed, _ = _feed()
    feed.start()
    restarts = []
    monkeypatch.setattr(feed, "restart", lambda: restarts.append(1) or True)
    monkeypatch.setattr(feed, "seconds_since_last_tick", lambda: 1.0)
    monkeypatch.setattr(feed, "seconds_since_last_index_tick", lambda: 1.0)

    main._supervise_feed(feed, dt.time(10, 0), last_restart=0.0)
    assert restarts == []


def test_feed_is_not_rebuilt_outside_market_hours(monkeypatch):
    from src import main

    feed, _ = _feed()
    feed.start()
    restarts = []
    monkeypatch.setattr(feed, "restart", lambda: restarts.append(1) or True)
    monkeypatch.setattr(feed, "seconds_since_last_tick", lambda: 99_999)

    for when in (dt.time(5, 25), dt.time(8, 0), dt.time(16, 0), dt.time(21, 19)):
        main._supervise_feed(feed, when, last_restart=0.0)
    assert restarts == []


def test_feed_that_never_ticked_at_all_is_rebuilt(monkeypatch):
    """seconds_since_last_tick() is None in this state, so the escalation has to
    fall back to how long ago the subscriptions were issued."""
    from src import main

    feed, _ = _feed()
    feed.start()
    restarts = []
    monkeypatch.setattr(feed, "restart", lambda: restarts.append(1) or True)
    monkeypatch.setattr(feed, "seconds_since_last_tick", lambda: None)
    monkeypatch.setattr(feed, "seconds_since_start", lambda: main.FEED_RESTART_AFTER_SECONDS + 1)

    main._supervise_feed(feed, dt.time(10, 0), last_restart=0.0)
    assert restarts == [1]


# --- NIFTY 50 index resolution ---------------------------------------------

@pytest.mark.parametrize("record", [
    {"pTrdSymbol": "NIFTY 50", "pSymbolName": "NIFTY"},
    {"pTrdSymbol": "Nifty 50", "pSymbolName": "NIFTY"},
    {"pTrdSymbol": "NIFTY-50", "pSymbolName": "NIFTY"},
    {"pTrdSymbol": None, "pSymbolName": "NIFTY 50"},
    # What the nse_cm scrip master actually contains, per the 04-Aug-2026 log.
    {"pTrdSymbol": None, "pSymbolName": "NIFTY"},
    {"pTrdSymbol": "NIFTY", "pSymbolName": "NIFTY"},
])
def test_nifty_50_row_is_recognised(record):
    assert _is_nifty_50_row(record)


@pytest.mark.parametrize("record", [
    {"pTrdSymbol": "NIFTYBEES-EQ", "pSymbolName": "NIFTYBEES"},
    {"pTrdSymbol": "JUNIORBEES-EQ", "pSymbolName": "JUNIORBEES"},
    {"pTrdSymbol": "BANKNIFTY", "pSymbolName": "BANKNIFTY"},
    {"pTrdSymbol": "NIFTY 500", "pSymbolName": "NIFTY500"},
    {"pTrdSymbol": "FINNIFTY", "pSymbolName": "FINNIFTY"},
    {"pTrdSymbol": "MIDCPNIFTY", "pSymbolName": "MIDCPNIFTY"},
    {"pTrdSymbol": "HDFCNIFTY-BL", "pSymbolName": "HDFCNIFTY"},
    {"pTrdSymbol": "NIFTYQLITY-EQ", "pSymbolName": "NIFTYQLITY"},
    {"pTrdSymbol": "NIFTYBETA-BL", "pSymbolName": "NIFTYBETA"},
])
def test_other_nifty_named_instruments_are_rejected(record):
    assert not _is_nifty_50_row(record)


def test_the_real_04_aug_scrip_master_sample_resolves():
    """The exact rows the 04-Aug-2026 log reported, which previously fell
    through to the hardcoded fallback token."""
    from src.instruments import resolve_nifty_index

    names = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "HDFCNIFTY-BL",
             "NIFTYQLITY-EQ", "NIFTYBETA-EQ", "NIFTYQLITY-BL", "NIFTYBETA-BL", "NIFTYBEES-EQ"]
    records = [
        {"pSymbolName": n.split("-")[0], "pTrdSymbol": n, "pSymbol": 26000 + i, "pExchSeg": "nse_cm"}
        for i, n in enumerate(names)
    ]

    class Session:
        def call_with_retry(self, _method, **kwargs):
            return records

    instrument = resolve_nifty_index(Session())
    assert instrument.token == "26000"       # the NIFTY row, not NIFTYBEES
    assert instrument.exchange_segment == "nse_cm"


def test_explicit_nifty_50_row_wins_over_a_bare_nifty_row():
    from src.instruments import resolve_nifty_index

    records = [
        {"pSymbolName": "NIFTY", "pTrdSymbol": "NIFTY", "pSymbol": 111, "pExchSeg": "nse_cm"},
        {"pSymbolName": "NIFTY", "pTrdSymbol": "NIFTY 50", "pSymbol": 26000, "pExchSeg": "nse_cm"},
    ]

    class Session:
        def call_with_retry(self, _method, **kwargs):
            return records

    assert resolve_nifty_index(Session()).token == "26000"


# --- position state surviving a restart ------------------------------------

def test_open_position_is_restored_after_a_restart():
    trade_id = _record_open_trade()

    executor = _executor()
    assert executor.position is None
    executor.restore_open_position("2026-08-03")

    position = executor.position
    assert position is not None
    assert position.trade_id == trade_id
    assert position.option_type == "CE"
    assert position.quantity == 65
    assert position.entry_price == 43.8
    assert position.underlying_entry_price == 24530.0
    assert position.instrument.token == "45678"
    assert position.instrument.exchange_segment == "nse_fo"
    # This is what makes the strategy manage the trade instead of re-entering.
    assert executor.open_option_type == "CE"


def test_restored_position_can_be_exited():
    _record_open_trade()
    executor = _executor()
    executor.restore_open_position("2026-08-03")

    executor.exit(60.0, "end-of-day square-off")

    assert executor.position is None
    trades = storage.get_trades_for_date("2026-08-03")
    assert trades[0]["exit_time"] is not None
    assert trades[0]["pnl"] == pytest.approx((60.0 - 43.8) * 65)


def test_trade_from_an_earlier_session_is_not_restored():
    _record_open_trade(session_day="2026-07-30")
    executor = _executor()
    executor.restore_open_position("2026-08-03")
    assert executor.position is None


def test_paper_trade_is_not_resumed_in_live_mode(caplog):
    _record_open_trade(live_trading=False)
    executor = _executor(live_trading=True)
    with caplog.at_level(logging.WARNING):
        executor.restore_open_position("2026-08-03")

    assert executor.position is None
    assert "LIVE_TRADING" in caplog.text


def test_row_without_instrument_details_is_not_resumed(caplog):
    _record_open_trade(instrument_token=None, exchange_segment=None)
    executor = _executor()
    with caplog.at_level(logging.WARNING):
        executor.restore_open_position("2026-08-03")

    assert executor.position is None


def test_stale_open_trades_are_closed_out_without_inventing_a_pnl():
    _record_open_trade(session_day="2026-07-30")
    _record_open_trade(session_day="2026-08-03")

    assert storage.abandon_stale_open_trades("2026-08-03") == 1

    stale = storage.get_trades_for_date("2026-07-30")[0]
    assert stale["exit_time"] is not None       # no longer reported as open
    assert stale["exit_price"] is None          # but no price is fabricated
    assert stale["pnl"] is None
    assert stale["exit_reason"] == storage.ABANDONED_EXIT_REASON

    # Today's open trade is untouched.
    assert storage.get_open_trade("2026-08-03") is not None


def test_abandoned_trades_are_excluded_from_pnl_and_win_rate():
    settled = _record_open_trade(session_day="2026-08-03")
    storage.record_trade_exit(
        settled, dt.datetime(2026, 8, 3, 10, 0, tzinfo=IST), exit_price=60.0, pnl=1053.0, reason="target hit"
    )
    _record_open_trade(session_day="2026-08-03", entry_time=dt.datetime(2026, 8, 3, 11, 0, tzinfo=IST))
    storage.abandon_stale_open_trades("2026-08-04")

    summary = storage.get_daily_summary("2026-08-03", "2026-08-03")
    assert len(summary) == 1
    assert summary[0]["trade_count"] == 1
    assert summary[0]["total_pnl"] == pytest.approx(1053.0)

    assert storage.get_realized_pnl("2026-08-03") == pytest.approx(1053.0)


def test_get_open_trade_is_scoped_to_the_session():
    _record_open_trade(session_day="2026-07-30")
    assert storage.get_open_trade("2026-08-03") is None
    assert storage.get_open_trade("2026-07-30") is not None


def test_realised_pnl_is_restored_for_the_daily_loss_cap():
    trade_id = _record_open_trade()
    storage.record_trade_exit(
        trade_id, dt.datetime(2026, 8, 3, 10, 0, tzinfo=IST), exit_price=20.0, pnl=-1547.0, reason="stop"
    )
    assert storage.get_realized_pnl("2026-08-03") == pytest.approx(-1547.0)
    assert storage.get_realized_pnl("2026-08-04") == 0.0


def test_migration_adds_columns_to_a_pre_existing_database():
    """An existing bot.db already has trades in it; upgrading must not
    require recreating it."""
    conn = storage.get_connection()
    try:
        for column in storage._TRADE_COLUMN_MIGRATIONS:
            conn.execute(f"ALTER TABLE trades DROP COLUMN {column}")
        conn.commit()
    finally:
        conn.close()

    storage.init_db()  # migration runs again

    conn = storage.get_connection()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    finally:
        conn.close()
    assert set(storage._TRADE_COLUMN_MIGRATIONS).issubset(columns)


# --- the user-visible behaviour, end to end ------------------------------

def _raise(*args, **kwargs):
    raise RuntimeError("quote unavailable")


def _unexpected_call(*args, **kwargs):
    raise AssertionError("a strike must not be resolved without a spot price")


@pytest.fixture
def mid_session(monkeypatch):
    """Pins the bot's clock to 09:20 IST (well before the square-off cut-off)
    and returns the session date that entries will be recorded under.

    execution.enter() timestamps trades with the real UTC clock, so the session
    date is derived from that rather than from a hardcoded day.
    """
    from src import main

    today = dt.datetime.now(IST).date()
    monkeypatch.setattr(main, "_now_ist", lambda: dt.datetime.combine(today, dt.time(9, 20), tzinfo=IST))
    return today.isoformat()


def test_restart_mid_trade_exits_instead_of_re_entering(monkeypatch, mid_session):
    """The 03-Aug-2026 sequence, replayed: enter, lose the process, restart.

    Previously the restart lost the position, so the trade stayed open forever
    and the next signal opened a *second* one -- "only buy entries record".
    Now the restart resumes the position and the profit target closes it.
    """
    from src import main

    option = Instrument("45678", "nse_fo", "NIFTY2680424550CE", 65, dt.date(2026, 8, 4))
    feed, _ = _feed()
    feed.latest_index_ltp = 24530.0  # true spot, not the 24618 futures price

    monkeypatch.setattr(main, "resolve_atm_option", lambda session, spot, option_type: option)
    monkeypatch.setattr(main, "fetch_ltp", lambda session, instrument: 43.8)

    # --- session 1: the entry --------------------------------------------
    executor = _executor()
    decision = type("D", (), {"option_type": "CE", "action": "enter"})()
    main._try_enter(session=None, feed=feed, executor=executor, decision=decision)

    assert executor.position is not None
    # The strike came from spot 24530, not from the futures price.
    assert executor.position.instrument.trading_symbol == "NIFTY2680424550CE"
    assert executor.position.underlying_entry_price == 24530.0

    # --- the process dies -------------------------------------------------
    del executor
    assert storage.get_open_trade(mid_session) is not None

    # --- session 2: the restart -------------------------------------------
    executor = _executor()
    executor.restore_open_position(mid_session)
    assert executor.open_option_type == "CE"  # so the strategy manages, not re-enters

    # The premium target (15 points) now has a position to act on.
    monkeypatch.setattr(main, "fetch_ltp", lambda session, instrument: 59.0)
    main._check_profit_target(executor, session=None, feed=feed, config=_config(), quote_premium=True)

    assert executor.position is None
    trade = storage.get_trades_for_date(mid_session)[0]
    assert trade["exit_time"] is not None
    assert "premium gained" in trade["exit_reason"]
    assert trade["pnl"] == pytest.approx((59.0 - 43.8) * 65)


def test_entry_is_skipped_rather_than_using_the_futures_price(monkeypatch, mid_session, caplog):
    """Complaint #1: spot was 24530 and the bot bought the 24600 strike off the
    futures price. With every spot source exhausted it must take no trade."""
    from src import main

    feed, _ = _feed()
    feed.latest_index_ltp = None
    feed.latest_ltp = 24618.0  # futures -- must never be used for the strike

    monkeypatch.setattr(main, "fetch_ltp", _raise)                       # index REST quote fails
    monkeypatch.setattr(main, "spot_from_put_call_parity", lambda s, r: None)  # parity fails too
    monkeypatch.setattr(main, "resolve_atm_option", _unexpected_call)

    executor = _executor()
    decision = type("D", (), {"option_type": "CE", "action": "enter"})()
    with caplog.at_level(logging.ERROR):
        main._try_enter(session=None, feed=feed, executor=executor, decision=decision)

    assert executor.position is None
    assert storage.get_open_trade(mid_session) is None
    assert "never be derived from the futures price" in caplog.text


# --- deriving spot from option premiums -------------------------------------

def _fake_option_chain(monkeypatch, true_spot: float, days_to_expiry: int = 7):
    """A self-consistent option chain priced off `true_spot`.

    Premiums are set so put-call parity holds exactly:  C - P = S - K*e^(-rT).
    Recovering `true_spot` from them is then a real test of the maths, and
    independent of which strike happens to be quoted.
    """
    from src import instruments

    expiry = dt.date.today() + dt.timedelta(days=days_to_expiry)

    def resolve(session, underlying_ltp, option_type):
        strike = instruments.atm_strike(underlying_ltp)
        return Instrument(str(strike), "nse_fo", f"NIFTY{strike}{option_type}", 65, expiry)

    def quote(session, instrument):
        strike = int(instrument.token)
        discounted = strike * math.exp(-instruments.RISK_FREE_RATE * days_to_expiry / 365.0)
        call = max(true_spot - strike, 0.0) + 100.0     # any plausible call price
        put = call - (true_spot - discounted)           # forced by parity
        return call if instrument.trading_symbol.endswith("CE") else put

    monkeypatch.setattr(instruments, "resolve_atm_option", resolve)
    monkeypatch.setattr(instruments, "fetch_ltp", quote)


def test_put_call_parity_recovers_the_true_spot(monkeypatch):
    from src.instruments import spot_from_put_call_parity

    _fake_option_chain(monkeypatch, true_spot=24530.0)
    # Called with the *futures* price, which sits ~88 points above spot.
    assert spot_from_put_call_parity(None, 24618.0) == pytest.approx(24530.0, abs=0.01)


def test_put_call_parity_is_independent_of_the_futures_premium(monkeypatch):
    """The whole point: the futures price only picks which strike to quote, and
    its premium over spot cancels out of the answer."""
    from src.instruments import spot_from_put_call_parity

    _fake_option_chain(monkeypatch, true_spot=24530.0)
    results = [spot_from_put_call_parity(None, reference) for reference in (24560.0, 24618.0, 24700.0)]

    assert all(r == pytest.approx(24530.0, abs=0.01) for r in results)


def test_parity_derived_spot_picks_the_strike_the_client_asked_for(monkeypatch):
    """03-Aug: Nifty was 24530 and futures 24618. The bot bought 24600 CE; the
    correct ATM strike off spot is 24550."""
    from src.instruments import atm_strike, spot_from_put_call_parity

    _fake_option_chain(monkeypatch, true_spot=24530.0)
    spot = spot_from_put_call_parity(None, 24618.0)

    assert atm_strike(spot) == 24550
    assert atm_strike(24618.0) == 24600      # what the futures price gave


def test_parity_returns_none_when_the_legs_have_different_expiries(monkeypatch, caplog):
    from src import instruments

    def resolve(session, underlying_ltp, option_type):
        expiry = dt.date(2026, 8, 4) if option_type == "CE" else dt.date(2026, 8, 11)
        return Instrument("24500", "nse_fo", f"NIFTY24500{option_type}", 65, expiry)

    monkeypatch.setattr(instruments, "resolve_atm_option", resolve)
    monkeypatch.setattr(instruments, "fetch_ltp", _unexpected_call)

    with caplog.at_level(logging.WARNING):
        assert instruments.spot_from_put_call_parity(None, 24500.0) is None
    assert "not comparable" in caplog.text


def test_parity_returns_none_when_a_quote_fails(monkeypatch):
    from src import instruments

    monkeypatch.setattr(
        instruments, "resolve_atm_option",
        lambda s, p, t: Instrument("24500", "nse_fo", f"NIFTY24500{t}", 65, dt.date.today()),
    )
    monkeypatch.setattr(instruments, "fetch_ltp", _raise)

    assert instruments.spot_from_put_call_parity(None, 24500.0) is None


def test_spot_sources_are_tried_in_order(monkeypatch):
    """Websocket first, then the index REST quote, then parity -- so if index
    data is ever enabled on the account the bot upgrades itself."""
    from src import main

    feed, _ = _feed()
    feed.latest_ltp = 24618.0

    # 1. websocket tick wins outright
    feed.latest_index_ltp = 24530.0
    monkeypatch.setattr(main, "fetch_ltp", _unexpected_call)
    monkeypatch.setattr(main, "spot_from_put_call_parity", _unexpected_call)
    assert main._current_spot(None, feed, allow_rest_quote=True) == 24530.0

    # 2. no tick -> REST index quote
    feed.latest_index_ltp = None
    monkeypatch.setattr(main, "fetch_ltp", lambda s, i: 24531.0)
    assert main._current_spot(None, feed, allow_rest_quote=True) == 24531.0

    # 3. REST rejected -> parity
    monkeypatch.setattr(main, "fetch_ltp", _raise)
    monkeypatch.setattr(main, "spot_from_put_call_parity", lambda s, r: 24532.0)
    assert main._current_spot(None, feed, allow_rest_quote=True) == 24532.0


def test_parity_is_not_used_on_unthrottled_polls(monkeypatch):
    """Parity costs two REST quotes, so it must not run on every 5-second poll."""
    from src import main

    feed, _ = _feed()
    feed.latest_index_ltp = None
    feed.latest_ltp = 24618.0
    monkeypatch.setattr(main, "fetch_ltp", _unexpected_call)
    monkeypatch.setattr(main, "spot_from_put_call_parity", _unexpected_call)

    assert main._current_spot(None, feed, allow_rest_quote=False) is None


def test_rest_index_quote_is_used_when_the_websocket_index_is_silent(monkeypatch, mid_session):
    """A missing index tick falls back to a REST quote of the *index*, which is
    still a true spot price -- not to the futures price."""
    from src import main

    option = Instrument("45678", "nse_fo", "NIFTY2680424550CE", 65, dt.date(2026, 8, 4))
    feed, _ = _feed()
    feed.latest_index_ltp = None
    feed.latest_ltp = 24618.0

    quoted = []

    def fake_fetch_ltp(session, instrument):
        quoted.append(instrument.trading_symbol)
        return 24530.0 if instrument.trading_symbol == "NIFTY 50" else 43.8

    seen_spot = []
    monkeypatch.setattr(main, "fetch_ltp", fake_fetch_ltp)
    monkeypatch.setattr(main, "resolve_atm_option", lambda s, spot, t: seen_spot.append(spot) or option)

    executor = _executor()
    decision = type("D", (), {"option_type": "CE", "action": "enter"})()
    main._try_enter(session=None, feed=feed, executor=executor, decision=decision)

    assert "NIFTY 50" in quoted
    assert seen_spot == [24530.0]
    assert executor.position is not None


# --- log volume ------------------------------------------------------------

def _record(message: str, name: str = "src.ws_feed") -> logging.LogRecord:
    return logging.LogRecord(name, logging.WARNING, __file__, 1, message, None, None)


def test_duplicate_suppressor_caps_a_repeating_message():
    suppressor = DuplicateSuppressor(max_per_window=20, window_seconds=3600)
    allowed = sum(1 for _ in range(300_000) if suppressor.filter(_record("Websocket closed; reconnecting")))
    assert allowed == 20


def test_suppressor_keys_on_the_template_not_the_formatted_message():
    """"Websocket closed; reconnecting in %ds" must collapse across its
    arguments rather than looking like a new message each time."""
    suppressor = DuplicateSuppressor(max_per_window=5, window_seconds=3600)
    allowed = 0
    for i in range(100):
        record = logging.LogRecord("src.ws_feed", logging.WARNING, __file__, 1, "closed; retry in %ds", (i,), None)
        allowed += suppressor.filter(record)
    assert allowed == 5


def test_distinct_messages_are_not_suppressed_by_each_other():
    suppressor = DuplicateSuppressor(max_per_window=2, window_seconds=3600)
    assert suppressor.filter(_record("first"))
    assert suppressor.filter(_record("second"))
    assert suppressor.filter(_record("first"))
    assert suppressor.filter(_record("second"))
    assert not suppressor.filter(_record("first"))
    assert not suppressor.filter(_record("second"))


def test_suppression_count_is_reported_in_the_next_window():
    suppressor = DuplicateSuppressor(max_per_window=1, window_seconds=0.01)
    assert suppressor.filter(_record("flood"))
    for _ in range(9):
        suppressor.filter(_record("flood"))

    import time
    time.sleep(0.02)
    record = _record("flood")
    assert suppressor.filter(record)
    assert "+9 identical messages suppressed" in record.msg


def test_suppressor_does_not_grow_without_bound():
    suppressor = DuplicateSuppressor(max_per_window=1, window_seconds=0.001)
    for i in range(10_000):
        suppressor.filter(_record(f"message {i}"))
    assert len(suppressor._windows) <= 4096
