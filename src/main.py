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
from src.instruments import fetch_ltp, resolve_atm_option, resolve_nifty_futures, resolve_nifty_index
from src.logging_config import setup_logging
from src.risk import RiskManager
from src.strategy import StrategyEngine
from src import storage
from src.ws_feed import LiveFeed

logger = logging.getLogger(__name__)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SQUARE_OFF_TIME = dt.time(15, 20)
MARKET_CLOSE_TIME = dt.time(15, 30)
POLL_SECONDS = 5
# How often the open position's premium is re-quoted while checking the profit
# target. The underlying leg of the target is checked on every poll (the index
# ticks in over the websocket and costs nothing); the premium leg needs a REST
# quote, so it is throttled.
TARGET_QUOTE_SECONDS = 15


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

    today = dt.datetime.now(IST).date().isoformat()
    feed.agg_5m.seed(storage.get_recent_candles("5m", today))
    feed.agg_30m.seed(storage.get_recent_candles("30m", today))

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


def _try_enter(session: KotakSession, feed: LiveFeed, executor: OrderExecutor, decision) -> None:
    """Places the entry for `decision`, using the NIFTY 50 spot index (not the
    futures price) to pick the ATM strike."""
    now_ist = dt.datetime.now(IST).time()
    if now_ist >= SQUARE_OFF_TIME:
        # Otherwise the last candles of the day open positions that the
        # square-off has already run past: on 30-Jul-2026 entries were taken at
        # 15:25 and 15:30, the second of which was never closed at all.
        logger.info("Skipping %s entry: past the %s square-off time", decision.option_type, SQUARE_OFF_TIME)
        return

    spot = feed.latest_index_ltp
    if spot is None:
        # Falling back to the futures price shifts the strike by the futures
        # premium (~50 points on 30-Jul-2026, i.e. one strike), so it is a
        # last resort and is logged as such.
        spot = feed.latest_ltp
        if spot is None:
            logger.warning("Skipping %s entry: no spot or futures price available yet", decision.option_type)
            return
        logger.warning(
            "No spot index price yet; selecting the ATM strike from the futures price %.2f instead", spot
        )

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

    reason = _target_hit(position, config, feed.latest_index_ltp, premium)
    if reason is None:
        return

    logger.info("%s for %s", reason, position.instrument.trading_symbol)
    try:
        exit_ltp = premium if premium is not None else fetch_ltp(session, position.instrument)
        executor.exit(exit_ltp, reason)
    except Exception:
        logger.exception("Failed to exit position on profit target")


def _run_until_market_close(
    executor: OrderExecutor, session: KotakSession, feed: LiveFeed, config: Config
) -> None:
    last_premium_quote = 0.0
    while True:
        now_ist = dt.datetime.now(IST).time()

        if now_ist >= MARKET_CLOSE_TIME:
            logger.info("Market close reached, stopping.")
            break

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
