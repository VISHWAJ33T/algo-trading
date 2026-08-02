"""Pure indicator functions over a list of candles.Candle. No I/O, no API
dependency -- unit tested directly against hand-built OHLCV sequences.
"""
from __future__ import annotations

from src.candles import Candle

# Elder's classic Force Index smoothing period.
FORCE_INDEX_EMA_PERIOD = 13


def vwap(candles: list[Candle]) -> list[float | None]:
    """Session-cumulative VWAP (typical price weighted by volume), one value
    per candle. `candles` should already be scoped to a single session."""
    result: list[float | None] = []
    cum_pv = 0.0
    cum_vol = 0.0
    for c in candles:
        typical = (c.high + c.low + c.close) / 3
        cum_pv += typical * c.volume
        cum_vol += c.volume
        result.append(cum_pv / cum_vol if cum_vol > 0 else None)
    return result


def ema(values: list[float], period: int) -> list[float | None]:
    """Standard EMA: seeded with the SMA of the first `period` values, then
    the usual recurrence. Returns None for indices before the seed point."""
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result[period - 1] = seed

    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        result[i] = prev
    return result


def ema_over_candles(candles: list[Candle], period: int) -> list[float | None]:
    return ema([c.close for c in candles], period)


def force_index_smoothed(candles: list[Candle], period: int = FORCE_INDEX_EMA_PERIOD) -> list[float | None]:
    """Elder's Force Index: raw (close[i]-close[i-1]) * volume[i], smoothed
    with a `period`-length EMA. Index 0 has no prior close, so it and the
    first `period - 1` valid raw values are None."""
    if len(candles) < 2:
        return [None] * len(candles)

    raw = [0.0] + [
        (candles[i].close - candles[i - 1].close) * candles[i].volume
        for i in range(1, len(candles))
    ]
    smoothed_from_index1 = ema(raw[1:], period)

    result: list[float | None] = [None]
    result.extend(smoothed_from_index1)
    return result


def find_swing_points(candles: list[Candle], n: int = 2) -> list[tuple[int, str, float]]:
    """2-bar fractal swing points: a bar's high/low is confirmed as a swing
    point only if it's the strict extreme among the `n` bars before and after
    it. Returns (index, "high"|"low", price) in chronological order. The most
    recent `n` bars can never be confirmed yet -- that lag is intentional."""
    points: list[tuple[int, str, float]] = []
    for i in range(n, len(candles) - n):
        window = candles[i - n : i + n + 1]
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        if candles[i].high == max(highs) and highs.count(candles[i].high) == 1:
            points.append((i, "high", candles[i].high))
        if candles[i].low == min(lows) and lows.count(candles[i].low) == 1:
            points.append((i, "low", candles[i].low))
    return points


def swing_structure(candles: list[Candle], n: int = 2) -> str | None:
    """Returns "HH_HL", "LH_LL", or None (no confirmed directional
    structure yet) based on the last two confirmed swing highs and lows."""
    points = find_swing_points(candles, n)
    highs = [p for p in points if p[1] == "high"]
    lows = [p for p in points if p[1] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None

    last_high, prev_high = highs[-1][2], highs[-2][2]
    last_low, prev_low = lows[-1][2], lows[-2][2]

    if last_high > prev_high and last_low > prev_low:
        return "HH_HL"
    if last_high < prev_high and last_low < prev_low:
        return "LH_LL"
    return None
