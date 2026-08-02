import datetime as dt

from src.candles import Candle, TickAggregator

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _candle(i: int, close: float) -> Candle:
    start = dt.datetime(2026, 1, 5, 9, 15, tzinfo=IST) + dt.timedelta(minutes=i * 5)
    return Candle(start=start, open=close, high=close + 1, low=close - 1, close=close, volume=10)


def test_seed_preloads_bars_without_touching_current_candle():
    agg = TickAggregator(5)
    agg.seed([_candle(i, 100 + i) for i in range(20)])

    assert len(agg.all_candles()) == 20

    closed = agg.on_tick(dt.datetime(2026, 1, 6, 9, 16, tzinfo=IST), price=150, volume_delta=5)
    assert closed is None  # first live tick opens a new bucket, nothing closes yet
    assert len(agg.all_candles()) == 21  # seeded history + the newly forming candle


def test_seed_is_capped_by_max_bars():
    agg = TickAggregator(5, max_bars=3)
    agg.seed([_candle(i, 100) for i in range(10)])
    assert len(agg.bars) == 3
