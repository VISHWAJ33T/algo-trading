import datetime as dt

from src.dashboard import _with_indicators

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _rows(count: int, volume: float, stored_vwap=None) -> list[dict]:
    """Stored candle rows as get_candles_for_date returns them. `stored_vwap`
    defaults to None to mimic what the strategy actually persists while its
    indicators are still warming up."""
    start = dt.datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    return [
        {
            "start_time": (start + dt.timedelta(minutes=5 * i)).isoformat(),
            "open": 100.0 + i, "high": 105.0 + i, "low": 95.0 + i, "close": 102.0 + i,
            "volume": volume,
            "vwap": stored_vwap, "ema9": None, "ema15": None, "force_index": None,
        }
        for i in range(count)
    ]


def test_vwap_is_filled_from_the_first_candle_despite_null_in_storage():
    # The regression this guards: the chart used to read the stored vwap column
    # straight through, which is NULL until every indicator has warmed up.
    enriched, fallback = _with_indicators(_rows(20, volume=1000))

    assert not fallback
    assert all(row["vwap"] is not None for row in enriched)
    assert enriched[0]["vwap"] == (105.0 + 95.0 + 102.0) / 3


def test_all_indicator_series_are_populated_once_warmed_up():
    enriched, _ = _with_indicators(_rows(30, volume=1000))

    assert enriched[8]["ema9"] is not None      # EMA9 seeds on the 9th candle
    assert enriched[7]["ema9"] is None
    assert enriched[14]["ema15"] is not None    # EMA15 seeds on the 15th
    assert enriched[13]["force_index"] is not None


def test_zero_volume_session_falls_back_to_typical_price():
    # An index-like feed that never reports volume makes VWAP mathematically
    # undefined; the chart shows typical price instead and flags it.
    enriched, fallback = _with_indicators(_rows(20, volume=0.0))

    assert fallback
    assert all(row["vwap"] is not None for row in enriched)
    assert enriched[0]["vwap"] == (105.0 + 95.0 + 102.0) / 3


def test_empty_and_incomplete_rows_are_handled():
    assert _with_indicators([]) == ([], False)

    rows = _rows(3, volume=1000)
    rows[1]["close"] = None
    enriched, _ = _with_indicators(rows)
    assert len(enriched) == 2
