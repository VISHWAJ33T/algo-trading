"""Entrypoint: login -> resolve instruments -> start live feed -> on each
5-min candle close, evaluate the strategy and execute. Runs until market
close, squaring off any open position shortly before the close.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from src.auth import KotakSession
from src.config import load_config
from src.execution import OrderExecutor
from src.instruments import fetch_ltp, resolve_atm_option, resolve_nifty_futures
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
        "Starting bot. LIVE_TRADING=%s LOT_SIZE=%d MAX_DAILY_LOSS=%s",
        config.live_trading, config.lot_size, config.max_daily_loss,
    )

    session = KotakSession(config)
    session.login()

    nifty_futures = resolve_nifty_futures(session)
    logger.info(
        "Resolved Nifty Futures instrument: %s (expiry %s)",
        nifty_futures.trading_symbol, nifty_futures.expiry,
    )

    risk = RiskManager(config.lot_size, config.max_daily_loss)
    executor = OrderExecutor(session, config, risk)
    strategy = StrategyEngine()
    feed = LiveFeed(session, nifty_futures)

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
        decision = strategy.evaluate(
            feed.agg_5m.all_candles(), feed.agg_30m.all_candles(), executor.open_option_type
        )
        logger.info("Decision: %s (%s)", decision.action, decision.reason)

        storage.save_candle("5m", candle, decision)
        storage.save_decision(dt.datetime.now(dt.timezone.utc), decision)

        if decision.action == "enter" and feed.latest_ltp is not None:
            try:
                option_instrument = resolve_atm_option(session, feed.latest_ltp, decision.option_type)
                option_ltp = fetch_ltp(session, option_instrument)
                executor.enter(decision.option_type, option_instrument, option_ltp)
            except Exception:
                logger.exception("Failed to enter %s position", decision.option_type)
        elif decision.action == "exit" and executor.position is not None:
            try:
                option_ltp = fetch_ltp(session, executor.position.instrument)
                executor.exit(option_ltp, decision.reason)
            except Exception:
                logger.exception("Failed to exit position")

    feed.on_candle_close_5m = handle_5m_close
    feed.on_candle_close_30m = handle_30m_close
    feed.start()

    _run_until_market_close(executor, session)


def _run_until_market_close(executor: OrderExecutor, session: KotakSession) -> None:
    squared_off = False
    while True:
        now_ist = dt.datetime.now(IST).time()

        if now_ist >= MARKET_CLOSE_TIME:
            logger.info("Market close reached, stopping.")
            break

        if not squared_off and now_ist >= SQUARE_OFF_TIME and executor.position is not None:
            logger.info("Square-off time reached with an open position; exiting it.")
            try:
                option_ltp = fetch_ltp(session, executor.position.instrument)
                executor.exit(option_ltp, "end-of-day square-off")
            except Exception:
                logger.exception("Failed to square off position at end of day")
            squared_off = True

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
