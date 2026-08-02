"""Aggregates live ticks into rolling 5-min / 30-min OHLCV candles.

Buckets are anchored to the 9:15 IST session open (not to UTC clock marks) so
that 30-min candles line up with 9:15/9:45/10:15/... as a trader would expect,
rather than 10:00/10:30/... which is what naive epoch-based flooring would
produce.
"""
from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)


@dataclass
class Candle:
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def _session_anchor(timestamp: dt.datetime) -> dt.datetime:
    local = timestamp.astimezone(IST)
    return local.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)


def in_session(timestamp: dt.datetime) -> bool:
    """True only during regular market hours in IST. Ticks outside these hours
    (post-close snapshots, reconnect replays) must not build candles: on
    30-Jul-2026 they produced a tail of flat zero-volume 5-min candles running
    to 19:40 IST, which the strategy then evaluated as if it were live price
    action."""
    return SESSION_OPEN <= timestamp.astimezone(IST).time() < SESSION_CLOSE


class TickAggregator:
    """Buckets ticks for one instrument into candles of a fixed interval."""

    def __init__(self, interval_minutes: int, max_bars: int = 200):
        self.interval = dt.timedelta(minutes=interval_minutes)
        self.bars: deque[Candle] = deque(maxlen=max_bars)
        self._current: Candle | None = None
        self._current_bucket_start: dt.datetime | None = None

    def seed(self, candles: list[Candle]) -> None:
        """Pre-load already-closed candles (e.g. from a prior session) as
        history, so EMA/Force Index are warmed up from the first live tick
        instead of needing dozens of bars to build up. Call before any ticks
        are fed in -- it does not touch `_current`."""
        for candle in candles:
            self.bars.append(candle)

    def _bucket_start(self, timestamp: dt.datetime) -> dt.datetime:
        local = timestamp.astimezone(IST)
        anchor = _session_anchor(local)
        elapsed = local - anchor
        bucket_index = int(elapsed.total_seconds() // self.interval.total_seconds())
        return anchor + bucket_index * self.interval

    def on_tick(self, timestamp: dt.datetime, price: float, volume_delta: float = 0.0) -> Candle | None:
        """Feed one tick. Returns the candle that just closed, if this tick
        started a new bucket; otherwise None."""
        bucket_start = self._bucket_start(timestamp)

        if self._current is None or bucket_start != self._current_bucket_start:
            closed = self._current
            if closed is not None:
                self.bars.append(closed)
            self._current = Candle(bucket_start, price, price, price, price, volume_delta)
            self._current_bucket_start = bucket_start
            return closed

        current = self._current
        current.high = max(current.high, price)
        current.low = min(current.low, price)
        current.close = price
        current.volume += volume_delta
        return None

    def closed_candles(self) -> list[Candle]:
        """Only fully closed candles, oldest first. This is what the strategy
        must be evaluated on: `all_candles()` ends with a bar holding a single
        tick, so evaluating on it judged each signal on the first print of the
        *next* candle instead of the close of the one that just finished."""
        return list(self.bars)

    def all_candles(self) -> list[Candle]:
        """Closed candles plus the still-forming current one, oldest first.
        For display only -- see closed_candles() for anything that decides."""
        candles = list(self.bars)
        if self._current is not None:
            candles.append(self._current)
        return candles
