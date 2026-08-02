"""Local read-only web dashboard for the client: today's live status plus
historical analytics, both read straight from storage.py's SQLite database.

This is a separate process from the trading bot (src/main.py) -- it only
ever reads the database, so it's safe to run (or leave running) at any time,
independent of whether the bot itself is currently active.

Run with: python -m src.dashboard
Then open http://127.0.0.1:8765 in a browser.
"""
from __future__ import annotations

import datetime as dt

from flask import Flask, jsonify, render_template, request
from zoneinfo import ZoneInfo

from src import indicators, storage
from src.candles import Candle

IST = ZoneInfo("Asia/Kolkata")

app = Flask(__name__)


def _today_str() -> str:
    return dt.datetime.now(IST).date().isoformat()


def _bot_is_live() -> bool:
    """Best-effort guess at whether the bot is currently running: is there a
    decision logged in the last 2 minutes? (Decisions are only ever written
    on a live 5-min candle close by src/main.py.)"""
    recent = storage.get_recent_decisions(_today_str(), limit=1)
    if not recent:
        return False
    last_ts = dt.datetime.fromisoformat(recent[0]["timestamp"])
    return abs(dt.datetime.now(dt.timezone.utc) - last_ts) < dt.timedelta(minutes=2)


def _row_to_candle(row: dict) -> Candle:
    return Candle(
        start=dt.datetime.fromisoformat(row["start_time"]),
        open=row["open"], high=row["high"], low=row["low"], close=row["close"],
        volume=row["volume"] or 0.0,
    )


def _running_typical_price(candles: list[Candle]) -> list[float]:
    """Cumulative mean of typical price -- the volume-less degenerate case of
    VWAP, used only as a display fallback (see _with_indicators)."""
    result: list[float] = []
    total = 0.0
    for i, c in enumerate(candles):
        total += (c.high + c.low + c.close) / 3
        result.append(total / (i + 1))
    return result


def _with_indicators(rows: list[dict]) -> tuple[list[dict], bool]:
    """Recompute every indicator series from the stored OHLCV rather than
    trusting the values persisted alongside each candle.

    The persisted values come from the strategy's decision snapshot, which is
    empty until *all* indicators have warmed up (Force Index needs 14 candles,
    EMA15 needs 15) -- so the stored vwap/ema columns are NULL for the first
    ~75 minutes of every session even though VWAP is well-defined from the
    very first candle. Charting the recomputed series instead means the lines
    start where the maths allows, not where the strategy started trading.

    Returns the enriched rows and whether VWAP fell back to typical price.
    """
    complete = [r for r in rows if None not in (r["open"], r["high"], r["low"], r["close"])]
    candles = [_row_to_candle(r) for r in complete]

    vwaps = indicators.vwap(candles)
    # A feed that never delivered volume leaves cumulative volume at 0, which
    # makes VWAP undefined for every candle. Rather than drawing nothing, fall
    # back to typical price and let the UI label the line as such.
    vwap_is_fallback = bool(candles) and all(v is None for v in vwaps)
    if vwap_is_fallback:
        vwaps = _running_typical_price(candles)

    ema9 = indicators.ema_over_candles(candles, 9)
    ema15 = indicators.ema_over_candles(candles, 15)
    force_index = indicators.force_index_smoothed(candles)

    for i, row in enumerate(complete):
        row["vwap"] = vwaps[i]
        row["ema9"] = ema9[i]
        row["ema15"] = ema15[i]
        row["force_index"] = force_index[i]

    return complete, vwap_is_fallback


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/today")
def api_today():
    session_date = request.args.get("date", _today_str())

    open_trade = storage.get_open_trade()
    trades = storage.get_trades_for_date(session_date)
    decisions = storage.get_recent_decisions(session_date, limit=100)
    candles_5m, vwap_is_fallback = _with_indicators(
        storage.get_candles_for_date(session_date, timeframe="5m")
    )

    closed_trades = [t for t in trades if t["exit_time"] is not None]
    todays_pnl = sum(t["pnl"] for t in closed_trades)

    return jsonify({
        "session_date": session_date,
        "is_live_now": _bot_is_live(),
        "open_position": open_trade,
        "todays_pnl": todays_pnl,
        "trades": list(reversed(trades)),
        "recent_decisions": decisions,
        "candles_5m": candles_5m,
        "vwap_is_fallback": vwap_is_fallback,
    })


@app.route("/api/history")
def api_history():
    end_date = request.args.get("end", _today_str())
    start_date = request.args.get(
        "start", (dt.date.fromisoformat(end_date) - dt.timedelta(days=30)).isoformat()
    )

    daily_summary = storage.get_daily_summary(start_date, end_date)
    trades = storage.get_trades_in_range(start_date, end_date)
    closed_trades = [t for t in trades if t["exit_time"] is not None]

    total_pnl = sum(t["pnl"] for t in closed_trades)
    wins = sum(1 for t in closed_trades if t["pnl"] > 0)
    losses = sum(1 for t in closed_trades if t["pnl"] <= 0)
    win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0

    return jsonify({
        "start_date": start_date,
        "end_date": end_date,
        "daily_summary": daily_summary,
        "trades": list(reversed(trades)),
        "summary": {
            "total_trades": len(closed_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
        },
    })


if __name__ == "__main__":
    storage.init_db()
    app.run(host="127.0.0.1", port=8765, debug=False)
