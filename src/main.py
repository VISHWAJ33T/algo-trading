"""Entrypoint: login -> resolve instruments -> start live feed -> on each
5-min candle close, evaluate the strategy and execute. While a position is
open the profit target is checked continuously between candle closes. Runs
until market close, squaring off any open position shortly before the close.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from src.auth import KotakSession
from src.config import Config, load_config
from src.execution import OrderExecutor
from src.instruments import (
    fetch_ltp,
    resolve_atm_option,
    resolve_nifty_futures,
    resolve_nifty_index,
    spot_from_put_call_parity,
)
from src.logging_config import setup_logging
from src.risk import RiskManager
from src.strategy import StrategyEngine
from src import storage
from src.ws_feed import LiveFeed

logger = logging.getLogger(__name__)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dt.time(9, 15)
SQUARE_OFF_TIME = dt.time(15, 20)
MARKET_CLOSE_TIME = dt.time(15, 30)
POLL_SECONDS = 5
# How often the open position's premium is re-quoted while checking the profit
# target. The underlying leg of the target is checked on every poll (the index
# ticks in over the websocket and costs nothing); the premium leg needs a REST
# quote, so it is throttled.
TARGET_QUOTE_SECONDS = 15
# How quiet the tick feed may go during market hours before it is reported as
# stalled. The SDK reconnects and resubscribes on its own (see
# LiveFeed._on_close), so this is a health check rather than a trigger for any
# recovery action -- it exists because the old self-reconnect used to mask a
# dead feed behind an endless stream of reconnect messages.
FEED_STALL_WARNING_SECONDS = 120
# The stall check runs on its own slower cadence than the 5-second poll, so a
# genuinely dead feed reports once a minute instead of twelve times.
FEED_CHECK_SECONDS = 60
# How long the feed may stay silent before the connection is rebuilt from
# scratch. Longer than the warning threshold because the SDK gets first refusal:
# its own reconnect runs on a ~130s cycle, so this only steps in once that has
# demonstrably failed to bring data back.
FEED_RESTART_AFTER_SECONDS = 300
# Minimum gap between rebuilds. On 04-Aug-2026 the SDK reconnected roughly every
# 130 seconds for six hours without ever restoring the feed; this makes sure the
# recovery path can never turn into a reconnect loop of its own.
FEED_RESTART_INTERVAL_SECONDS = 300


def _now_ist() -> dt.datetime:
    """Single source of "now" for the session-time checks, so tests can pin the
    clock without patching the datetime module process-wide."""
    return dt.datetime.now(IST)


def main() -> None:
    # Logging is set up before anything else -- including before config is
    # loaded -- so that when this runs with no visible console (as the
    # "Start Trading Bot" launcher does), a startup failure (bad .env, login
    # failure, etc.) always ends up in logs/bot.log instead of vanishing
    # silently with a hidden window and no trace anywhere.
    setup_logging("INFO")
    try:
        _main()
    except Exception:
        logger.exception("Bot failed to start or crashed")
        raise


def _main() -> None:
    config = load_config()
    setup_logging(config.log_level)
    storage.init_db()
    logger.info(
        "Starting bot. LIVE_TRADING=%s LOT_SIZE=%d MAX_DAILY_LOSS=%s TARGET=%s underlying pts / %s premium pts",
        config.live_trading, config.lot_size, config.max_daily_loss,
        config.target_underlying_points, config.target_premium_points,
    )

    session = KotakSession(config)
    session.login()

    nifty_futures = resolve_nifty_futures(session)
    logger.info(
        "Resolved Nifty Futures instrument: %s (expiry %s)",
        nifty_futures.trading_symbol, nifty_futures.expiry,
    )
    nifty_index = resolve_nifty_index(session)
    logger.info(
        "Resolved spot index instrument for strike selection: %s (token %s)",
        nifty_index.trading_symbol, nifty_index.token,
    )

    risk = RiskManager(config.lot_size, config.max_daily_loss)
    executor = OrderExecutor(session, config, risk)
    strategy = StrategyEngine()
    feed = LiveFeed(session, nifty_futures, index_instrument=nifty_index)

    today = _now_ist().date().isoformat()
    feed.agg_5m.seed(storage.get_recent_candles("5m", today))
    feed.agg_30m.seed(storage.get_recent_candles("30m", today))

    # Any position and realised P&L from earlier in this session are restored
    # before the feed starts, so a restart resumes managing what is already
    # open (and keeps enforcing the daily loss cap) instead of starting flat.
    abandoned = storage.abandon_stale_open_trades(today)
    if abandoned:
        logger.warning(
            "Closed out %d open trade(s) left over from earlier sessions; they were never "
            "squared off by the bot, so no exit price or P&L is recorded for them.", abandoned,
        )
    executor.restore_open_position(today)
    risk.realized_pnl_today = storage.get_realized_pnl(today)
    if risk.realized_pnl_today:
        logger.info("Restored realised P&L for today: %.2f", risk.realized_pnl_today)

    def handle_30m_close(candle) -> None:
        storage.save_candle("30m", candle)

    def handle_5m_close(candle) -> None:
        logger.info(
            "5m candle closed %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            candle.start, candle.open, candle.high, candle.low, candle.close, candle.volume,
        )
        # closed_candles(), not all_candles(): the aggregator has already opened
        # the next bucket by the time this fires, and evaluating with that
        # one-tick bar on the end judged every signal on the first print of the
        # next candle instead of the close of the candle that just finished.
        decision = strategy.evaluate(
            feed.agg_5m.closed_candles(), feed.agg_30m.closed_candles(), executor.open_option_type
        )
        logger.info("Decision: %s (%s)", decision.action, decision.reason)

        storage.save_candle("5m", candle, decision)
        storage.save_decision(dt.datetime.now(dt.timezone.utc), decision)

        if decision.action == "enter":
            _try_enter(session, feed, executor, decision)
        elif decision.action == "exit" and executor.position is not None:
            try:
                option_ltp = fetch_ltp(session, executor.position.instrument)
                executor.exit(option_ltp, decision.reason)
            except Exception:
                logger.exception("Failed to exit position")

    feed.on_candle_close_5m = handle_5m_close
    feed.on_candle_close_30m = handle_30m_close
    feed.start()

    try:
        _run_until_market_close(executor, session, feed, config)
    finally:
        feed.stop()


def _current_spot(session: KotakSession, feed: LiveFeed, allow_rest_quote: bool) -> float | None:
    """The NIFTY 50 spot index level, or None if it genuinely cannot be had.

    Three sources are tried in descending order of directness. The order matters
    as much as the sources: it is written so that if the index data this account
    currently cannot see is ever enabled, the bot silently upgrades back to the
    direct reading with no code change.

      1. the websocket index tick -- free and live, but has never yet delivered;
      2. a REST quote of the index -- currently rejected with HTTP 400;
      3. put-call parity on the option pair -- two REST quotes, using the only
         market data this account demonstrably can read.

    What is deliberately *not* here is the futures price. Futures trade ~50-90
    points above spot, which is one to two strike steps, and using them is what
    produced the wrong-strike complaint this work started from. Note that
    parity's `reference_price` argument is the futures price, but only to decide
    which strike to quote -- the premium cancels out of the result.
    """
    spot = feed.latest_index_ltp
    if spot is not None:
        return spot
    if not allow_rest_quote:
        return None

    if feed.index_instrument is not None:
        try:
            spot = fetch_ltp(session, feed.index_instrument)
            logger.warning(
                "No spot index tick over the websocket; used a REST index quote of %.2f instead", spot
            )
            return spot
        except Exception:
            logger.warning(
                "The NIFTY 50 index is readable neither over the websocket nor over REST; "
                "deriving spot from option premiums instead", exc_info=True,
            )

    reference = feed.latest_ltp
    if reference is None:
        logger.error("No futures price either, so no strike can be chosen at all")
        return None
    return spot_from_put_call_parity(session, reference)


def _try_enter(session: KotakSession, feed: LiveFeed, executor: OrderExecutor, decision) -> None:
    """Places the entry for `decision`, using the NIFTY 50 spot index (not the
    futures price) to pick the ATM strike."""
    now_ist = _now_ist().time()
    if now_ist >= SQUARE_OFF_TIME:
        # Otherwise the last candles of the day open positions that the
        # square-off has already run past: on 30-Jul-2026 entries were taken at
        # 15:25 and 15:30, the second of which was never closed at all.
        logger.info("Skipping %s entry: past the %s square-off time", decision.option_type, SQUARE_OFF_TIME)
        return

    spot = _current_spot(session, feed, allow_rest_quote=True)
    if spot is None:
        # Skipping the trade is the correct outcome here. Taking it on the
        # futures price is what put the bot into 24600 CE on 03-Aug-2026
        # while Nifty itself was at 24530 -- two strikes away from ATM, which
        # breaks the premium-based target the whole strategy is built on.
        logger.error(
            "Skipping %s entry: no NIFTY 50 spot price available, and the ATM strike must "
            "never be derived from the futures price.", decision.option_type,
        )
        return

    try:
        option_instrument = resolve_atm_option(session, spot, decision.option_type)
        option_ltp = fetch_ltp(session, option_instrument)
        executor.enter(decision.option_type, option_instrument, option_ltp, underlying_ltp=spot)
    except Exception:
        logger.exception("Failed to enter %s position", decision.option_type)


def _target_hit(position, config: Config, spot: float | None, premium: float | None) -> str | None:
    """Reason string if the profit target is reached, else None. Long CE gains
    when the underlying rises, long PE when it falls; the option premium rises
    in both cases, so the premium leg is direction-independent."""
    if config.target_underlying_points and spot is not None and position.underlying_entry_price is not None:
        move = spot - position.underlying_entry_price
        if position.option_type == "PE":
            move = -move
        if move >= config.target_underlying_points:
            return f"target hit: underlying moved {move:.2f} points in favour"

    if config.target_premium_points and premium is not None:
        gain = premium - position.entry_price
        if gain >= config.target_premium_points:
            return f"target hit: premium gained {gain:.2f} points"
    return None


def _check_profit_target(
    executor: OrderExecutor, session: KotakSession, feed: LiveFeed, config: Config, quote_premium: bool
) -> None:
    position = executor.position
    if position is None:
        return

    premium = None
    if quote_premium and config.target_premium_points:
        try:
            premium = fetch_ltp(session, position.instrument)
        except Exception:
            logger.exception("Failed to quote %s while checking the profit target", position.instrument.trading_symbol)

    # The REST fallback for spot rides the same throttle as the premium quote,
    # so a missing index feed costs one extra request every TARGET_QUOTE_SECONDS
    # rather than one on every poll.
    spot = _current_spot(session, feed, allow_rest_quote=quote_premium)
    reason = _target_hit(position, config, spot, premium)
    if reason is None:
        return

    logger.info("%s for %s", reason, position.instrument.trading_symbol)
    try:
        exit_ltp = premium if premium is not None else fetch_ltp(session, position.instrument)
        executor.exit(exit_ltp, reason)
    except Exception:
        logger.exception("Failed to exit position on profit target")


def _supervise_feed(feed: LiveFeed, now_ist: dt.time, last_restart: float) -> float:
    """Reports a feed that has gone quiet during market hours, and rebuilds the
    connection if it stays quiet.

    Returns the (possibly updated) time of the last rebuild.

    Warning alone was not enough. On 04-Aug-2026 the connection leak was gone
    and the process survived the whole session for the first time, but the feed
    delivered a single tick at 09:07 and nothing for the next 6.4 hours while
    the SDK reconnected fruitlessly every ~130 seconds -- so the bot logged 375
    stall warnings, built no candles, and took no trades. The rebuild below is
    the escalation for exactly that state.
    """
    if not (MARKET_OPEN_TIME <= now_ist < MARKET_CLOSE_TIME):
        return last_restart

    age = feed.seconds_since_last_tick()
    stalled_for = age if age is not None else feed.seconds_since_start()

    if age is None:
        logger.warning("No tick has arrived from the live feed yet")
    elif age >= FEED_STALL_WARNING_SECONDS:
        logger.warning("Live feed has been silent for %.0fs", age)
    else:
        index_age = feed.seconds_since_last_index_tick()
        if feed.index_instrument is not None and (index_age is None or index_age >= FEED_STALL_WARNING_SECONDS):
            logger.warning(
                "NIFTY 50 spot index feed is not delivering (%s); strike selection will fall back "
                "to a REST index quote",
                "no tick yet" if index_age is None else f"silent for {index_age:.0f}s",
            )
        return last_restart

    if stalled_for is None or stalled_for < FEED_RESTART_AFTER_SECONDS:
        return last_restart
    now = time.monotonic()
    if now - last_restart < FEED_RESTART_INTERVAL_SECONDS:
        return last_restart

    try:
        feed.restart()
    except Exception:
        logger.exception("Failed to rebuild the live feed connection")
    return now


def _run_until_market_close(
    executor: OrderExecutor, session: KotakSession, feed: LiveFeed, config: Config
) -> None:
    last_premium_quote = 0.0
    last_feed_check = 0.0
    last_feed_restart = 0.0
    while True:
        now_ist = _now_ist().time()

        if now_ist >= MARKET_CLOSE_TIME:
            logger.info("Market close reached, stopping.")
            break

        if time.monotonic() - last_feed_check >= FEED_CHECK_SECONDS:
            last_feed_check = time.monotonic()
            last_feed_restart = _supervise_feed(feed, now_ist, last_feed_restart)

        if now_ist >= SQUARE_OFF_TIME:
            # Retried every poll rather than latched behind a "done" flag, so a
            # failed square-off (network blip, bad quote) gets another chance
            # before the close instead of leaving the position open overnight.
            if executor.position is not None:
                logger.info("Square-off time reached with an open position; exiting it.")
                try:
                    option_ltp = fetch_ltp(session, executor.position.instrument)
                    executor.exit(option_ltp, "end-of-day square-off")
                except Exception:
                    logger.exception("Failed to square off position at end of day")
        elif executor.position is not None:
            now = time.monotonic()
            quote_premium = now - last_premium_quote >= TARGET_QUOTE_SECONDS
            if quote_premium:
                last_premium_quote = now
            _check_profit_target(executor, session, feed, config, quote_premium)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
