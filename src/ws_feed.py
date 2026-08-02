"""Live tick feed: wraps client.subscribe()/on_message and feeds ticks into
5-min and 30-min TickAggregators.

Message shape confirmed by reading the installed neo_api_client package
source (NeoWebSocket.py) rather than guessing:

- `on_message` receives `{"type": "stock_feed", "data": [tick, tick, ...]}`
  for live subscribed ticks (also "quotes" for one-off REST-triggered
  snapshots, and "order_feed" for order updates -- both ignored here).
- Each tick dict in `data` uses Kotak's short wire codes, NOT the
  human-readable names `quotes()` returns: `tk`=instrument_token,
  `e`=exchange_segment, `ltp`=last traded price, `v`=cumulative volume
  traded so far today, `ltq`=last traded quantity, `ltt`=last traded time.
  A given message may only carry a subset of these fields.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Callable

from src.auth import KotakSession
from src.candles import Candle, TickAggregator
from src.instruments import Instrument

logger = logging.getLogger(__name__)

_LTP_KEYS = ("ltp",)
_TIMESTAMP_KEYS = ("ltt",)
_CUMULATIVE_VOLUME_KEYS = ("v",)
_TRADE_QUANTITY_KEYS = ("ltq",)


@dataclass
class Tick:
    timestamp: dt.datetime
    ltp: float
    cumulative_volume: float | None
    trade_quantity: float | None


def _first(tick: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in tick and tick[key] not in (None, ""):
            return tick[key]
    return None


def _parse_timestamp(raw) -> dt.datetime:
    if raw is None:
        return dt.datetime.now(dt.timezone.utc)
    try:
        value = float(raw)
        # Heuristic: treat values > 10^12 as milliseconds since epoch.
        if value > 1e12:
            value /= 1000
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    except (TypeError, ValueError):
        return dt.datetime.now(dt.timezone.utc)


def _parse_tick(tick: dict) -> Tick | None:
    ltp_raw = _first(tick, _LTP_KEYS)
    if ltp_raw is None:
        return None
    return Tick(
        timestamp=_parse_timestamp(_first(tick, _TIMESTAMP_KEYS)),
        ltp=float(ltp_raw),
        cumulative_volume=_to_float_or_none(_first(tick, _CUMULATIVE_VOLUME_KEYS)),
        trade_quantity=_to_float_or_none(_first(tick, _TRADE_QUANTITY_KEYS)),
    )


def _to_float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class LiveFeed:
    def __init__(self, session: KotakSession, instrument: Instrument):
        self.session = session
        self.instrument = instrument
        self.agg_5m = TickAggregator(5)
        self.agg_30m = TickAggregator(30)
        self.latest_ltp: float | None = None

        self.on_candle_close_5m: Callable[[Candle], None] | None = None
        self.on_candle_close_30m: Callable[[Candle], None] | None = None

        self._last_cumulative_volume: float | None = None

    def start(self) -> None:
        client = self.session.client or self.session.login()
        client.on_message = self._on_message
        client.on_error = self._on_error
        client.on_close = self._on_close

        client.subscribe(
            instrument_tokens=[
                {"instrument_token": self.instrument.token, "exchange_segment": self.instrument.exchange_segment}
            ],
            isIndex=False,
        )
        logger.info("Subscribed to live feed for %s", self.instrument.trading_symbol)

    def _on_message(self, message) -> None:
        logger.debug("Raw websocket message: %s", message)
        if not isinstance(message, dict) or message.get("type") != "stock_feed":
            return

        ticks = message.get("data")
        if not isinstance(ticks, list):
            return

        for raw_tick in ticks:
            if not isinstance(raw_tick, dict):
                continue
            self._handle_tick(raw_tick)

    def _handle_tick(self, raw_tick: dict) -> None:
        try:
            tick = _parse_tick(raw_tick)
        except Exception:
            logger.exception("Failed to parse tick: %s", raw_tick)
            return
        if tick is None:
            return

        self.latest_ltp = tick.ltp

        volume_delta = 0.0
        if tick.cumulative_volume is not None:
            if self._last_cumulative_volume is not None:
                volume_delta = max(0.0, tick.cumulative_volume - self._last_cumulative_volume)
            self._last_cumulative_volume = tick.cumulative_volume
        elif tick.trade_quantity is not None:
            volume_delta = tick.trade_quantity

        closed_5m = self.agg_5m.on_tick(tick.timestamp, tick.ltp, volume_delta)
        if closed_5m is not None and self.on_candle_close_5m is not None:
            self.on_candle_close_5m(closed_5m)

        closed_30m = self.agg_30m.on_tick(tick.timestamp, tick.ltp, volume_delta)
        if closed_30m is not None and self.on_candle_close_30m is not None:
            self.on_candle_close_30m(closed_30m)

    def _on_error(self, error) -> None:
        logger.error("Websocket error: %s", error)

    def _on_close(self, *args) -> None:
        logger.warning("Websocket closed; reconnecting in 2s")
        time.sleep(2)
        self.start()
