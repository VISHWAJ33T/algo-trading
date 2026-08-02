"""Encodes the CE/PE entry and exit rules on top of the indicator functions.

Entry/exit decisions are evaluated once per 5-min candle close, on *closed*
candles only -- the caller must not pass the still-forming candle in (see
candles.TickAggregator.closed_candles). The strategy engine itself is stateless
with respect to broker positions -- the caller (main.py) tells it whether a
position is currently open and of what type via `open_option_type`, and
execution.py is the single source of truth for actual broker/paper position
state.

Entry is a VWAP *cross*, not a VWAP *side*: a CE is taken on the candle that
carries price from at/below VWAP to above it, and never while price is already
parked above VWAP. Same, mirrored, for PE. Everything else (Force Index sign,
both EMAs, market structure) is a filter on top of that trigger.

Decision carries the full indicator snapshot (not just the action) so callers
can log/persist *why* a decision was made, not just what it was -- this is
what feeds the dashboard's live view and historical candle charts. `reason`
names the specific condition that failed, so a "hold" on a candle that looked
like a signal can be explained without re-deriving the indicators by hand.
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
    vwap_cross: Literal["up", "down"] | None = None


def _vwap_cross(
    candle: Candle, vwap_now: float, prev_close: float | None, vwap_prev: float | None
) -> Literal["up", "down"] | None:
    """Direction in which this candle crossed VWAP, or None if it stayed on
    one side of it.

    Two ways to cross, either of which counts: the candle's own body spans
    VWAP (opened at/below, closed above), or the previous candle closed at/below
    VWAP and this one closed above it -- the second case catches a gap straight
    through VWAP, where the body never brackets it.
    """
    crossed_from_below = candle.open <= vwap_now or (
        prev_close is not None and vwap_prev is not None and prev_close <= vwap_prev
    )
    if candle.close > vwap_now and crossed_from_below:
        return "up"

    crossed_from_above = candle.open >= vwap_now or (
        prev_close is not None and vwap_prev is not None and prev_close >= vwap_prev
    )
    if candle.close < vwap_now and crossed_from_above:
        return "down"
    return None


class StrategyEngine:
    def evaluate(
        self,
        candles_5m: list[Candle],
        candles_30m: list[Candle],
        open_option_type: OptionType | None,
    ) -> Decision:
        if len(candles_5m) < 2:
            return Decision("hold", reason="not enough 5-min candles yet")

        session = _session_candles(candles_5m)
        vwap_5m = vwap(session)
        ema9_5m = ema_over_candles(candles_5m, 9)
        ema15_5m = ema_over_candles(candles_5m, 15)
        fi_5m = force_index_smoothed(candles_5m)

        v = vwap_5m[-1] if vwap_5m else None
        e9, e15, fi = ema9_5m[-1], ema15_5m[-1], fi_5m[-1]

        if v is None or e9 is None or e15 is None or fi is None:
            return Decision("hold", reason="indicators still warming up")

        candle = candles_5m[-1]
        close = candle.close
        structure_5m = swing_structure(candles_5m)
        structure_30m = swing_structure(candles_30m)

        prev_close = session[-2].close if len(session) >= 2 else None
        vwap_prev = vwap_5m[-2] if len(vwap_5m) >= 2 else None
        cross = _vwap_cross(candle, v, prev_close, vwap_prev)

        snapshot = dict(
            close=close, vwap=v, ema9=e9, ema15=e15, force_index=fi,
            structure_5m=structure_5m, structure_30m=structure_30m, vwap_cross=cross,
        )

        if open_option_type is not None:
            return self._evaluate_exit(open_option_type, close, v, fi, snapshot)
        return self._evaluate_entry(candle, v, e9, e15, fi, cross, structure_5m, structure_30m, snapshot)

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
        candle: Candle,
        v: float,
        e9: float,
        e15: float,
        fi: float,
        cross: str | None,
        structure_5m: str | None,
        structure_30m: str | None,
        snapshot: dict,
    ) -> Decision:
        close = candle.close

        # The 30-min timeframe is computed and logged, but does not gate entry.
        # It used to have to confirm the 5-min structure, which is what blocked
        # the valid 12:25 signal on 30-Jul-2026: the 30-min chart was still
        # carrying a lower low from that morning while the 5-min chart had
        # already turned. The revised rule is 5-min only -- VWAP cross, Force
        # Index sign, both EMAs, and 5-min structure.
        ce_checks = {
            "candle did not cross above VWAP": cross == "up",
            "candle is not green": candle.close > candle.open,
            "Force Index is not above zero": fi > 0,
            "close is not above both EMA9 and EMA15": close > e9 and close > e15,
            "5-min structure is not HH/HL": structure_5m == "HH_HL",
        }
        if all(ce_checks.values()):
            return Decision("enter", "CE", "CE: green candle crossed above VWAP, FI>0, above EMA9/15, HH-HL", **snapshot)

        pe_checks = {
            "candle did not cross below VWAP": cross == "down",
            "candle is not red": candle.close < candle.open,
            "Force Index is not below zero": fi < 0,
            "close is not below both EMA9 and EMA15": close < e9 and close < e15,
            "5-min structure is not LH/LL": structure_5m == "LH_LL",
        }
        if all(pe_checks.values()):
            return Decision("enter", "PE", "PE: red candle crossed below VWAP, FI<0, below EMA9/15, LH-LL", **snapshot)

        return Decision("hold", reason=_hold_reason(ce_checks, pe_checks, cross, structure_5m), **snapshot)


def _hold_reason(
    ce_checks: dict[str, bool], pe_checks: dict[str, bool], cross: str | None, structure_5m: str | None
) -> str:
    """Names the conditions that actually blocked the entry, for the side the
    candle was closest to taking, rather than a blanket "no entry conditions
    met" that gives no way to check the bot against a chart."""
    if cross == "up" or structure_5m == "HH_HL":
        side, checks = "CE", ce_checks
    elif cross == "down" or structure_5m == "LH_LL":
        side, checks = "PE", pe_checks
    else:
        return "no VWAP cross on this candle"

    failed = [name for name, ok in checks.items() if not ok]
    return f"no {side} entry: " + "; ".join(failed)
