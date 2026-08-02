import datetime as dt

from src.candles import Candle, TickAggregator, in_session

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


def test_closed_candles_excludes_the_forming_candle():
    agg = TickAggregator(5)
    agg.on_tick(dt.datetime(2026, 1, 5, 9, 16, tzinfo=IST), price=100, volume_delta=5)
    agg.on_tick(dt.datetime(2026, 1, 5, 9, 21, tzinfo=IST), price=200, volume_delta=5)

    # The 9:20 bucket is open with a single 200 tick in it; only the 9:15
    # candle has actually closed, and that is what the strategy must see.
    assert [c.close for c in agg.closed_candles()] == [100]
    assert [c.close for c in agg.all_candles()] == [100, 200]


def test_in_session_covers_only_regular_market_hours():
    assert not in_session(dt.datetime(2026, 7, 30, 9, 14, tzinfo=IST))
    assert in_session(dt.datetime(2026, 7, 30, 9, 15, tzinfo=IST))
    assert in_session(dt.datetime(2026, 7, 30, 15, 29, tzinfo=IST))
    assert not in_session(dt.datetime(2026, 7, 30, 15, 30, tzinfo=IST))
    assert not in_session(dt.datetime(2026, 7, 30, 19, 40, tzinfo=IST))
