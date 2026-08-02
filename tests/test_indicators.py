import datetime as dt

from src.candles import Candle
from src.indicators import ema, find_swing_points, force_index_smoothed, swing_structure, vwap

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def make_candle(minute_offset: int, o: float, h: float, l: float, c: float, v: float) -> Candle:
    start = dt.datetime(2026, 1, 5, 9, 15, tzinfo=IST) + dt.timedelta(minutes=minute_offset)
    return Candle(start=start, open=o, high=h, low=l, close=c, volume=v)


def test_vwap_is_cumulative_volume_weighted_typical_price():
    candles = [
        make_candle(0, 100, 102, 99, 101, 10),
        make_candle(5, 101, 103, 100, 102, 20),
    ]
    result = vwap(candles)

    typical_0 = (102 + 99 + 101) / 3
    expected_0 = typical_0  # single bar: vwap == typical price
    assert result[0] == expected_0

    typical_1 = (103 + 100 + 102) / 3
    expected_1 = (typical_0 * 10 + typical_1 * 20) / 30
    assert abs(result[1] - expected_1) < 1e-9


def test_vwap_none_when_zero_volume():
    candles = [make_candle(0, 100, 100, 100, 100, 0)]
    assert vwap(candles) == [None]


def test_ema_seeds_with_sma_then_recurs():
    values = [1, 2, 3, 4, 5, 6, 7]
    result = ema(values, period=3)

    assert result[0] is None
    assert result[1] is None
    assert result[2] == sum(values[:3]) / 3  # seeded SMA

    multiplier = 2 / 4
    expected_3 = (values[3] - result[2]) * multiplier + result[2]
    assert abs(result[3] - expected_3) < 1e-9


def test_ema_returns_all_none_when_not_enough_data():
    assert ema([1, 2], period=5) == [None, None]


def test_force_index_smoothed_needs_enough_bars():
    candles = [make_candle(i * 5, 100, 100, 100, 100 + i, 10) for i in range(5)]
    result = force_index_smoothed(candles, period=13)
    # Not enough bars yet to seed a 13-period EMA of the raw force index.
    assert all(value is None for value in result)


def test_force_index_smoothed_sign_reflects_price_direction():
    # Rising closes with steady volume -> positive raw force index throughout,
    # so the smoothed value should also be positive once it warms up.
    candles = [make_candle(i * 5, 100, 105, 95, 100 + i, 50) for i in range(20)]
    result = force_index_smoothed(candles, period=13)

    warmed_up = [v for v in result if v is not None]
    assert warmed_up, "expected the smoothed force index to warm up within 20 bars"
    assert all(v > 0 for v in warmed_up)


def test_find_swing_points_detects_simple_peak_and_trough():
    # index:      0    1    2    3    4
    # highs:     10   12   20   12   10   -> index 2 is a confirmed swing high
    # lows:       5    3    1    3    5   -> index 2 is a confirmed swing low
    candles = [
        make_candle(0, 8, 10, 5, 8, 10),
        make_candle(5, 9, 12, 3, 9, 10),
        make_candle(10, 15, 20, 1, 15, 10),
        make_candle(15, 9, 12, 3, 9, 10),
        make_candle(20, 8, 10, 5, 8, 10),
    ]
    points = find_swing_points(candles, n=2)

    assert (2, "high", 20) in points
    assert (2, "low", 1) in points


def _build_candles(highs: list[float], lows: list[float]) -> list[Candle]:
    return [
        make_candle(i * 5, l, h, l, (h + l) / 2, 10)
        for i, (h, l) in enumerate(zip(highs, lows))
    ]


def test_swing_structure_detects_higher_high_higher_low():
    # Confirmed swing highs at idx3 (50) and idx9 (60): 60 > 50 -> higher high.
    # Confirmed swing lows at idx6 (-50) and idx12 (-40): -40 > -50 -> higher low.
    # All other values are distinct fillers chosen so no other index in the
    # confirmable range (2..12 for a 15-bar series with n=2) ties or beats them.
    highs = [10, 20, 30, 50, 21, 22, 23, 24, 25, 60, 27, 28, 29, 31, 32]
    lows = [5, 4, 3, 2, 1, 0, -50, 1, 2, 3, 4, 7, -40, 8, 9]
    candles = _build_candles(highs, lows)

    points = find_swing_points(candles, n=2)
    assert (3, "high", 50) in points
    assert (9, "high", 60) in points
    assert (6, "low", -50) in points
    assert (12, "low", -40) in points

    assert swing_structure(candles, n=2) == "HH_HL"


def test_swing_structure_detects_lower_high_lower_low():
    # Confirmed swing highs at idx3 (50) and idx9 (40): 40 < 50 -> lower high.
    # Confirmed swing lows at idx6 (-40) and idx12 (-50): -50 < -40 -> lower low.
    highs = [10, 20, 30, 50, 21, 22, 23, 24, 25, 40, 27, 28, 29, 31, 32]
    lows = [5, 4, 3, 2, 1, 0, -40, 1, 2, 3, 4, 7, -50, 8, 9]
    candles = _build_candles(highs, lows)

    assert swing_structure(candles, n=2) == "LH_LL"


def test_swing_structure_none_without_enough_confirmed_points():
    candles = [make_candle(i * 5, 100, 101, 99, 100, 10) for i in range(3)]
    assert swing_structure(candles, n=2) is None
