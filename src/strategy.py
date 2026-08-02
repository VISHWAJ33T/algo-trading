"""Encodes the CE/PE entry and exit rules on top of the indicator functions.

Entry/exit decisions are evaluated once per 5-min candle close. The strategy
engine itself is stateless with respect to broker positions -- the caller
(main.py) tells it whether a position is currently open and of what type via
`open_option_type`, and execution.py is the single source of truth for actual
broker/paper position state.

Decision carries the full indicator snapshot (not just the action) so callers
can log/persist *why* a decision was made, not just what it was -- this is
what feeds the dashboard's live view and historical candle charts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.candles import Candle
from src.indicators import ema_over_candles, force_index_smoothed, swing_structure, vwap

Action = Literal["hold", "enter", "exit"]
OptionType = Literal["CE", "PE"]


def _session_candles(candles: list[Candle]) -> list[Candle]:
    """VWAP resets every session (see indicators.vwap), so it must only see
    today's candles -- even though candles_5m/candles_30m may otherwise carry
    prior-session history so EMA/Force Index are warmed up from bar one."""
    if not candles:
        return []
    latest_date = candles[-1].start.date()
    return [c for c in candles if c.start.date() == latest_date]


@dataclass(frozen=True)
class Decision:
    action: Action
    option_type: OptionType | None = None
    reason: str = ""
    close: float | None = None
    vwap: float | None = None
    ema9: float | None = None
    ema15: float | None = None
    force_index: float | None = None
    structure_5m: str | None = None
    structure_30m: str | None = None


class StrategyEngine:
    def evaluate(
        self,
        candles_5m: list[Candle],
        candles_30m: list[Candle],
        open_option_type: OptionType | None,
    ) -> Decision:
        if len(candles_5m) < 2:
            return Decision("hold", reason="not enough 5-min candles yet")

        vwap_5m = vwap(_session_candles(candles_5m))
        ema9_5m = ema_over_candles(candles_5m, 9)
        ema15_5m = ema_over_candles(candles_5m, 15)
        fi_5m = force_index_smoothed(candles_5m)

        v = vwap_5m[-1] if vwap_5m else None
        e9, e15, fi = ema9_5m[-1], ema15_5m[-1], fi_5m[-1]

        if v is None or e9 is None or e15 is None or fi is None:
            return Decision("hold", reason="indicators still warming up")

        close = candles_5m[-1].close
        structure_5m = swing_structure(candles_5m)
        structure_30m = swing_structure(candles_30m)

        snapshot = dict(
            close=close, vwap=v, ema9=e9, ema15=e15, force_index=fi,
            structure_5m=structure_5m, structure_30m=structure_30m,
        )

        if open_option_type is not None:
            return self._evaluate_exit(open_option_type, close, v, fi, snapshot)
        return self._evaluate_entry(structure_5m, structure_30m, close, v, e9, e15, fi, snapshot)

    @staticmethod
    def _evaluate_exit(open_option_type: OptionType, close: float, v: float, fi: float, snapshot: dict) -> Decision:
        if open_option_type == "CE":
            if close < v or fi < 0:
                return Decision("exit", "CE", "close below VWAP or Force Index below zero", **snapshot)
        else:
            if close > v or fi > 0:
                return Decision("exit", "PE", "close above VWAP or Force Index above zero", **snapshot)
        return Decision("hold", reason="position open, no exit condition met", **snapshot)

    @staticmethod
    def _evaluate_entry(
        structure_5m: str | None,
        structure_30m: str | None,
        close: float,
        v: float,
        e9: float,
        e15: float,
        fi: float,
        snapshot: dict,
    ) -> Decision:
        ce_ok = (
            close > v and fi > 0 and close > e9 and close > e15
            and structure_5m == "HH_HL" and structure_30m == "HH_HL"
        )
        if ce_ok:
            return Decision("enter", "CE", "VWAP/FI/EMA/HH-HL conditions met on 5m & 30m", **snapshot)

        pe_ok = (
            close < v and fi < 0 and close < e9 and close < e15
            and structure_5m == "LH_LL" and structure_30m == "LH_LL"
        )
        if pe_ok:
            return Decision("enter", "PE", "VWAP/FI/EMA/LH-LL conditions met on 5m & 30m", **snapshot)

        return Decision("hold", reason="no entry conditions met", **snapshot)
