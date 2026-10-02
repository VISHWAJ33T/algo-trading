"""Local SQLite persistence for candles, strategy decisions, and trades.

This is what the dashboard (src/dashboard.py) reads from to show live status
and historical analytics. WAL mode is enabled so the trading bot (writer) and
the dashboard (reader, a separate process) can safely use the same file at
the same time.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from src.candles import SESSION_CLOSE, SESSION_OPEN, Candle
from src.strategy import Decision

IST = ZoneInfo("Asia/Kolkata")
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    start_time TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    vwap REAL, ema9 REAL, ema15 REAL, force_index REAL,
    structure TEXT
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(timeframe, session_date, start_time);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    option_type TEXT,
    reason TEXT,
    close REAL, vwap REAL, ema9 REAL, ema15 REAL, force_index REAL,
    structure_5m TEXT, structure_30m TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_lookup ON decisions(session_date, timestamp);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    option_type TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_time TEXT,
    exit_price REAL,
    pnl REAL,
    exit_reason TEXT,
    live_trading INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_lookup ON trades(session_date, entry_time);
"""

# Columns added after the first release. SQLite can only add one column per
# ALTER, and IF NOT EXISTS isn't supported for columns, so they are applied
# individually against the live table definition (see _migrate).
#
# instrument_token/exchange_segment exist so an open position can be rebuilt
# into a tradeable Instrument after a restart without re-resolving it from the
# scrip master; underlying_entry_price so the "30 points of the underlying"
# half of the profit target survives the same restart.
_TRADE_COLUMN_MIGRATIONS = {
    "instrument_token": "TEXT",
    "exchange_segment": "TEXT",
    "underlying_entry_price": "REAL",
}

# Written to exit_reason when a trade is closed out administratively because
# the bot was not running to manage it (see abandon_stale_open_trades). Such
# rows deliberately keep pnl NULL -- the exit price is genuinely unknown, and
# inventing one would corrupt the win/loss statistics.
ABANDONED_EXIT_REASON = "abandoned - bot was not running to close this trade"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Adds any columns missing from an existing trades table, so an upgrade
    drops onto an existing bot.db without needing it recreated."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for column, column_type in _TRADE_COLUMN_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {column_type}")


def _session_date(timestamp: dt.datetime) -> str:
    return timestamp.astimezone(IST).date().isoformat()


def save_candle(timeframe: str, candle: Candle, decision: Decision | None = None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO candles
               (session_date, timeframe, start_time, open, high, low, close, volume,
                vwap, ema9, ema15, force_index, structure)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _session_date(candle.start), timeframe, candle.start.isoformat(),
                candle.open, candle.high, candle.low, candle.close, candle.volume,
                decision.vwap if decision else None,
                decision.ema9 if decision else None,
                decision.ema15 if decision else None,
                decision.force_index if decision else None,
                (decision.structure_5m if timeframe == "5m" else decision.structure_30m) if decision else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def save_decision(timestamp: dt.datetime, decision: Decision) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO decisions
               (session_date, timestamp, action, option_type, reason, close, vwap,
                ema9, ema15, force_index, structure_5m, structure_30m)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _session_date(timestamp), timestamp.isoformat(), decision.action,
                decision.option_type, decision.reason, decision.close, decision.vwap,
                decision.ema9, decision.ema15, decision.force_index,
                decision.structure_5m, decision.structure_30m,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_trade_entry(
    option_type: str, trading_symbol: str, quantity: int,
    entry_time: dt.datetime, entry_price: float, live_trading: bool,
    instrument_token: str | None = None, exchange_segment: str | None = None,
    underlying_entry_price: float | None = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO trades
               (session_date, option_type, trading_symbol, quantity, entry_time,
                entry_price, live_trading, instrument_token, exchange_segment,
                underlying_entry_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _session_date(entry_time), option_type, trading_symbol, quantity,
                entry_time.isoformat(), entry_price, int(live_trading),
                instrument_token, exchange_segment, underlying_entry_price,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def record_trade_exit(trade_id: int, exit_time: dt.datetime, exit_price: float, pnl: float, reason: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE trades SET exit_time = ?, exit_price = ?, pnl = ?, exit_reason = ?
               WHERE id = ?""",
            (exit_time.isoformat(), exit_price, pnl, reason, trade_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_open_trade(session_date: str | None = None) -> dict | None:
    """The most recent trade with no exit recorded. Pass `session_date` to
    restrict it to one session -- without that, an orphaned row from a previous
    day is reported as today's open position (which is how a 30-Jul trade was
    still showing as open on 03-Aug)."""
    conn = get_connection()
    try:
        if session_date is None:
            row = conn.execute(
                "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM trades WHERE exit_time IS NULL AND session_date = ?
                   ORDER BY entry_time DESC LIMIT 1""",
                (session_date,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def abandon_stale_open_trades(before_session_date: str) -> int:
    """Closes out open trades left behind by earlier sessions, returning how
    many were affected.

    `exit_time` is set so they stop being reported as open, but `exit_price`
    and `pnl` stay NULL: the position was never actually squared off by the
    bot, so any number put there would be fabricated. Callers that aggregate
    P&L must therefore skip rows with a NULL pnl.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """UPDATE trades SET exit_time = entry_time, exit_reason = ?
               WHERE exit_time IS NULL AND session_date < ?""",
            (ABANDONED_EXIT_REASON, before_session_date),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_realized_pnl(session_date: str) -> float:
    """Total realised P&L booked for a session, so the daily-loss limit is
    still enforced correctly after the bot is restarted mid-session."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) FROM trades WHERE session_date = ? AND pnl IS NOT NULL",
            (session_date,),
        ).fetchone()
        return float(row[0])
    finally:
        conn.close()


def get_trades_for_date(session_date: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE session_date = ? ORDER BY entry_time", (session_date,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_recent_decisions(session_date: str, limit: int = 50) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE session_date = ? ORDER BY timestamp DESC LIMIT ?",
            (session_date, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_candles_for_date(session_date: str, timeframe: str = "5m") -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM candles WHERE session_date = ? AND timeframe = ? ORDER BY start_time",
            (session_date, timeframe),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_recent_candles(timeframe: str, before_session_date: str, limit: int = 200) -> list[Candle]:
    """Most recent already-closed candles strictly before `before_session_date`,
    for seeding a fresh TickAggregator so EMA/Force Index don't have to
    re-warm-up from zero every morning. Restricted to regular session hours
    so a stale post-close reading (e.g. from a bot instance that didn't shut
    its feed down cleanly at close) can't be seeded in as if it were live
    price action."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM candles WHERE timeframe = ? AND session_date < ?
               ORDER BY session_date DESC, start_time DESC LIMIT ?""",
            (timeframe, before_session_date, limit),
        ).fetchall()
    finally:
        conn.close()

    candles = [
        Candle(
            start=dt.datetime.fromisoformat(row["start_time"]),
            open=row["open"], high=row["high"], low=row["low"], close=row["close"],
            volume=row["volume"] or 0.0,
        )
        for row in reversed(rows)
    ]
    return [c for c in candles if SESSION_OPEN <= c.start.time() < SESSION_CLOSE]


def get_trades_in_range(start_date: str, end_date: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM trades WHERE session_date BETWEEN ? AND ?
               ORDER BY entry_time""",
            (start_date, end_date),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_daily_summary(start_date: str, end_date: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT session_date,
                      COUNT(*) AS trade_count,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                      COALESCE(SUM(pnl), 0) AS total_pnl
               FROM trades
               WHERE session_date BETWEEN ? AND ? AND exit_time IS NOT NULL
                     AND pnl IS NOT NULL
               GROUP BY session_date
               ORDER BY session_date""",
            (start_date, end_date),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
