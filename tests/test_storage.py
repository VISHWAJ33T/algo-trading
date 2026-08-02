import datetime as dt

import pytest

from src import storage
from src.candles import Candle
from src.strategy import Decision

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test_bot.db")
    storage.init_db()


def _candle(start: dt.datetime) -> Candle:
    return Candle(start=start, open=100, high=105, low=95, close=102, volume=1000)


def test_save_and_fetch_candle_with_decision():
    start = dt.datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    decision = Decision(
        action="hold", reason="warming up", close=102, vwap=101, ema9=100, ema15=99,
        force_index=5.0, structure_5m="HH_HL", structure_30m=None,
    )
    storage.save_candle("5m", _candle(start), decision)

    rows = storage.get_candles_for_date("2026-01-05", timeframe="5m")
    assert len(rows) == 1
    assert rows[0]["close"] == 102
    assert rows[0]["vwap"] == 101
    assert rows[0]["structure"] == "HH_HL"


def test_save_decision_and_fetch_recent():
    now = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    decision = Decision(action="enter", option_type="CE", reason="all conditions met", close=103)
    storage.save_decision(now, decision)

    rows = storage.get_recent_decisions("2026-01-05")
    assert len(rows) == 1
    assert rows[0]["action"] == "enter"
    assert rows[0]["option_type"] == "CE"


def test_trade_entry_then_exit_roundtrip():
    entry_time = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    exit_time = dt.datetime(2026, 1, 5, 10, 30, tzinfo=IST)

    trade_id = storage.record_trade_entry(
        option_type="CE", trading_symbol="NIFTY26JAN24000CE", quantity=75,
        entry_time=entry_time, entry_price=150.0, live_trading=False,
    )

    open_trade = storage.get_open_trade()
    assert open_trade is not None
    assert open_trade["id"] == trade_id
    assert open_trade["exit_time"] is None

    storage.record_trade_exit(trade_id, exit_time, exit_price=180.0, pnl=2250.0, reason="target hit")

    assert storage.get_open_trade() is None

    trades = storage.get_trades_for_date("2026-01-05")
    assert len(trades) == 1
    assert trades[0]["pnl"] == 2250.0
    assert trades[0]["exit_reason"] == "target hit"


def test_get_recent_candles_excludes_today_and_after_hours():
    prior_day, today = "2026-01-05", "2026-01-06"

    def save(session_day: str, hour: int, minute: int, close: float) -> None:
        y, m, d = (int(p) for p in session_day.split("-"))
        start = dt.datetime(y, m, d, hour, minute, tzinfo=IST)
        storage.save_candle("5m", Candle(start=start, open=close, high=close, low=close, close=close, volume=100))

    save(prior_day, 9, 15, 100)   # in-session
    save(prior_day, 15, 25, 110)  # in-session, near close
    save(prior_day, 17, 0, 999)   # after-hours (e.g. a feed that didn't shut down cleanly) -- must be excluded
    save(today, 9, 15, 200)       # today -- must be excluded, seeding only wants prior sessions

    candles = storage.get_recent_candles("5m", before_session_date=today)

    assert [c.close for c in candles] == [100, 110]


def test_daily_summary_aggregates_wins_and_losses():
    day1 = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    day2 = dt.datetime(2026, 1, 6, 10, 0, tzinfo=IST)

    for entry_time, pnl in [(day1, 500.0), (day1, -200.0), (day2, 300.0)]:
        trade_id = storage.record_trade_entry(
            "CE", "NIFTY26JAN24000CE", 75, entry_time, 150.0, live_trading=False
        )
        storage.record_trade_exit(trade_id, entry_time + dt.timedelta(minutes=15), 150.0 + pnl / 75, pnl, "exit")

    summary = storage.get_daily_summary("2026-01-05", "2026-01-06")
    assert len(summary) == 2
    assert summary[0]["session_date"] == "2026-01-05"
    assert summary[0]["trade_count"] == 2
    assert summary[0]["wins"] == 1
    assert summary[0]["losses"] == 1
    assert summary[0]["total_pnl"] == pytest.approx(300.0)
    assert summary[1]["total_pnl"] == pytest.approx(300.0)
