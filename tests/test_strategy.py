import datetime as dt

import pytest

from src.candles import Candle
from src.strategy import StrategyEngine

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def mc(i: int, o: float, h: float, l: float, c: float, v: float) -> Candle:
    start = dt.datetime(2026, 1, 5, 9, 15, tzinfo=IST) + dt.timedelta(minutes=i * 5)
    return Candle(start=start, open=o, high=h, low=l, close=c, volume=v)


def _hh_hl_series() -> list[Candle]:
    # Same zigzag used in test_indicators.py: confirmed HH_HL structure, and
    # the final close sits above VWAP/EMA9/EMA15 with a positive smoothed
    # Force Index (verified against the actual indicator functions).
    highs = [10, 20, 30, 50, 21, 22, 23, 24, 25, 60, 27, 28, 29, 31, 32]
    lows = [5, 4, 3, 2, 1, 0, -50, 1, 2, 3, 4, 7, -40, 8, 9]
    return [mc(i, l, h, l, (h + l) / 2, 10) for i, (h, l) in enumerate(zip(highs, lows))]


def _flat_series(length: int = 15) -> list[Candle]:
    return [mc(i, 100, 101, 99, 100, 10) for i in range(length)]


def test_hold_while_indicators_are_warming_up():
    engine = StrategyEngine()
    decision = engine.evaluate(_flat_series(5), _flat_series(5), open_option_type=None)
    assert decision.action == "hold"
    assert "warming up" in decision.reason


def test_enters_ce_when_all_conditions_align_on_both_timeframes():
    engine = StrategyEngine()
    series = _hh_hl_series()
    decision = engine.evaluate(series, series, open_option_type=None)
    assert decision.action == "enter"
    assert decision.option_type == "CE"


def test_holds_when_structure_only_confirmed_on_one_timeframe():
    engine = StrategyEngine()
    decision = engine.evaluate(_hh_hl_series(), _flat_series(15), open_option_type=None)
    assert decision.action == "hold"


def test_exits_ce_on_vwap_and_force_index_breakdown():
    engine = StrategyEngine()
    candles = _flat_series(14) + [mc(14, 100, 100, 50, 50, 10)]
    decision = engine.evaluate(candles, candles, open_option_type="CE")
    assert decision.action == "exit"
    assert decision.option_type == "CE"


def test_exits_pe_on_vwap_and_force_index_breakout():
    engine = StrategyEngine()
    candles = _flat_series(14) + [mc(14, 100, 150, 100, 150, 10)]
    decision = engine.evaluate(candles, candles, open_option_type="PE")
    assert decision.action == "exit"
    assert decision.option_type == "PE"


def test_holds_open_position_when_no_exit_condition_met():
    engine = StrategyEngine()
    series = _hh_hl_series()
    decision = engine.evaluate(series, series, open_option_type="CE")
    assert decision.action == "hold"


def _mc_on(day: int, i: int, o: float, h: float, l: float, c: float, v: float) -> Candle:
    start = dt.datetime(2026, 1, day, 9, 15, tzinfo=IST) + dt.timedelta(minutes=i * 5)
    return Candle(start=start, open=o, high=h, low=l, close=c, volume=v)


def test_prior_day_history_warms_up_indicators_but_vwap_stays_session_scoped():
    engine = StrategyEngine()
    # Prior session: 20 candles at price 200 -- if this leaked into today's
    # VWAP it would drag it far from today's actual price.
    prior_day = [_mc_on(5, i, 200, 201, 199, 200, 10) for i in range(20)]
    # Today so far: only 2 candles -- on its own, not enough for EMA9/EMA15.
    today = [_mc_on(6, i, 100, 101, 99, 100, 10) for i in range(2)]

    today_only = engine.evaluate(today, today, open_option_type=None)
    assert today_only.reason == "indicators still warming up"

    seeded = engine.evaluate(prior_day + today, prior_day + today, open_option_type=None)
    assert seeded.reason != "indicators still warming up"
    assert seeded.vwap == pytest.approx(100.0)
