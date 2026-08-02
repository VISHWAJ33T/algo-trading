# Nifty CE/PE Auto-Trading Bot (Kotak Neo API)

Automates a discretionary Nifty F&O intraday strategy (VWAP + EMA9/EMA15 + Elder's
Force Index + HH/HL swing structure on 5-min & 30-min) against a Kotak Securities
account via the [Kotak-neo-api-v2](https://github.com/Kotak-Neo/Kotak-neo-api-v2)
SDK.

**New to this project or to trading in general? Read [DOCUMENTATION.md](DOCUMENTATION.md)
first** — it explains what this bot does and why in plain language, including the
historical-data question, a full glossary, and exactly what Kotak's API can and
can't provide (verified by reading the SDK's own source code, not just its docs).

## Why this isn't a GitHub Actions job

Kotak's API has no historical intraday candle endpoint -- `quotes()` only returns a
day-level OHLC snapshot, not a series of bars. The bot builds its own 5-min/30-min
candles from live ticks over a websocket, which means it has to run as one
continuous process through the trading session rather than as short, independently
scheduled runs. It's designed to run locally (or on any always-on VPS later) via
`python -m src.main`.

## Setup

1. Create a virtualenv and install dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

   **If this fails with a `numpy` / `meson` / "Unknown compiler(s)" build error**,
   your Python version is too new to have a ready-made (prebuilt) numpy package
   available, and your machine doesn't have a C++ compiler installed to build it
   from source. Fix: use Python 3.12 instead, which has ready-made packages for
   everything this project needs, so no compiler is required at all.

   ```bash
   rmdir /s /q .venv
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

   (Install Python 3.12 first from https://www.python.org/downloads/release/python-3120/
   if `py -3.12` isn't found. `py -3.12` picks that version specifically via
   Windows' Python launcher, regardless of what other Python version is on your
   PATH -- plain `python -m venv .venv` would just recreate the same broken
   environment again.)

2. Copy `.env.example` to `.env` and fill in your Kotak credentials:
   - `KOTAK_CONSUMER_KEY`: from Neo app/website > Invest > Trade API
   - `KOTAK_MOBILE_NUMBER`, `KOTAK_UCC`, `KOTAK_MPIN`: your login identity
   - `KOTAK_TOTP_SECRET`: the TOTP **secret** (same one used to set up your
     authenticator app), not a 6-digit code -- the bot generates fresh codes
     itself via `pyotp`.

3. Keep `LIVE_TRADING=false` for the first runs. In this mode the bot logs every
   signal and simulates fills/P&L instead of calling Kotak's order APIs.

4. Run during market hours:

   ```bash
   python -m src.main
   ```

5. Watch `logs/bot.log` (and console output) for candle closes, indicator values,
   and entry/exit decisions. Compare against your own chart for a few sessions.

6. Set real `LOT_SIZE` and `MAX_DAILY_LOSS` in `.env`, then set `LIVE_TRADING=true`
   to place real orders.

## Easiest way to start/stop (no command prompt needed)

Once setup (below) is done once, day-to-day use doesn't require opening a terminal
at all:

- Double-click **`Start Trading Bot.vbs`** (in the project folder) — this silently
  starts both the bot and the dashboard in the background and opens your browser to
  the dashboard automatically once it's ready. No window appears.
- Double-click **`Stop Trading Bot.vbs`** when you want to stop for the day — it
  shows a small confirmation popup once both have stopped.

If double-clicking "Start" a second time while it's already running, it just
re-opens the dashboard in your browser instead of starting a second copy (avoiding
ever running two live trading sessions on the same account at once).

If the dashboard opens but keeps showing "Not Running" for more than a minute or
two, something failed at startup (almost always incomplete `.env` credentials) --
check `logs\bot.log`, which now always records a startup failure clearly, even
though no window was ever shown.

## Dashboard (live status + historical analytics)

Every candle, strategy decision, and trade the bot processes is saved to a local
SQLite database (`data/bot.db`). A separate, read-only web dashboard lets you (or
your client) view this in a browser -- today's live activity, and P&L analytics
across any date range:

```bash
python -m src.dashboard
```

Then open **http://127.0.0.1:8765**. The dashboard is a completely separate process
from the bot -- it's safe to leave running (or not) regardless of whether the bot
itself is active; it only ever reads the database.

- **Today tab**: today's P&L, any currently open position, a price-vs-VWAP chart
  for the day, today's trade list, and a live log of every decision the bot made
  (auto-refreshes every 8 seconds).
- **History tab**: pick any date range to see a daily P&L bar chart, a cumulative
  equity curve, win rate, and the full trade list for that range -- this is how
  your client can review "how did last month go" at a glance.

## Tests

```bash
pytest tests/
```

Indicator math and strategy signal logic are pure functions tested against
hand-built candle sequences -- no live connection required.

## Strategy rules encoded

- **CE entry**: the closed 5-min candle *crosses above* VWAP (it came from at/below
  VWAP and closed above it) and is green, smoothed Force Index > 0, close above
  EMA9 & EMA15, and 5-min structure is HH-HL. No entry while price is already
  sitting above VWAP.
- **PE entry**: mirrored -- a red candle crosses below VWAP, smoothed Force Index
  < 0, close below both EMAs, 5-min structure LH-LL.
- **CE exit**: close < VWAP, or smoothed Force Index < 0.
- **PE exit**: close > VWAP, or smoothed Force Index > 0.
- **Profit target**: 30 points of the underlying, or ~15 points of option premium,
  whichever comes first (`TARGET_UNDERLYING_POINTS` / `TARGET_PREMIUM_POINTS`).
  Checked continuously while a position is open, not only at candle closes.
- The 30-min chart is recorded and charted for context but no longer gates entry.
- Decisions are evaluated on **closed** candles only, and no new entry is taken
  after the 15:20 square-off.
- Only one position open at a time; ATM strike is resolved by rounding the
  **NIFTY 50 spot index** (not the futures price) to the nearest 50.
