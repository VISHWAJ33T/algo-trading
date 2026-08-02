"""Pure indicator functions over a list of candles.Candle. No I/O, no API
dependency -- unit tested directly against hand-built OHLCV sequences.
"""
from __future__ import annotations

from src.candles import Candle

# Elder's classic Force Index smoothing period.
FORCE_INDEX_EMA_PERIOD = 13

# How many bars either side of a candle must be lower/higher for it to count as
# a swing point. 1 = the ordinary 3-bar pivot a trader reads off a chart. It was
# 2 originally, which lagged the visible structure by an extra bar and left long
# stretches of the session with no confirmed structure at all (see
# swing_structure).
SWING_FRACTAL_WIDTH = 1


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


def find_swing_points(candles: list[Candle], n: int = SWING_FRACTAL_WIDTH) -> list[tuple[int, str, float]]:
    """Fractal swing points: a bar's high/low is confirmed as a swing point
    only if it's the strict extreme among the `n` bars before and after it.
    Returns (index, "high"|"low", price) in chronological order. The most
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


def swing_structure(candles: list[Candle], n: int = SWING_FRACTAL_WIDTH) -> str | None:
    """Returns "HH_HL" (up), "LH_LL" (down), or None (genuinely sideways /
    not enough confirmed swings yet).

    This used to demand a higher high *and* a higher low simultaneously, which
    is stricter than how the structure is actually read off a chart: a trend is
    up while price is *not making lower lows*, even if the last swing high
    hasn't been taken out yet (a pullback inside an uptrend does exactly that).
    On 30-Jul-2026 that strictness left the 30-min structure at None for the
    whole session and blocked every valid entry.

    So the rule is: up = making higher highs or higher lows, and not making
    lower lows. Down is the mirror. When the two disagree (higher low but lower
    high, i.e. a contracting range) the lows decide the direction, because
    that's the "not making LL" test the strategy is written around.
    """
    points = find_swing_points(candles, n)
    highs = [p[2] for p in points if p[1] == "high"]
    lows = [p[2] for p in points if p[1] == "low"]

    higher_high = len(highs) >= 2 and highs[-1] > highs[-2]
    lower_high = len(highs) >= 2 and highs[-1] < highs[-2]
    higher_low = len(lows) >= 2 and lows[-1] > lows[-2]
    lower_low = len(lows) >= 2 and lows[-1] < lows[-2]

    up = (higher_high or higher_low) and not lower_low
    down = (lower_high or lower_low) and not higher_high

    if up and not down:
        return "HH_HL"
    if down and not up:
        return "LH_LL"
    if up and down:
        return "HH_HL" if higher_low else "LH_LL" if lower_low else None
    return None
