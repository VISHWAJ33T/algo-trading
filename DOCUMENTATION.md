# Project Documentation — Nifty CE/PE Auto-Trading Bot

This document explains, in plain language, what this project does, why it's built the
way it is, and how to set it up and run it. It assumes no prior knowledge of trading
or of this codebase — it's written so you can explain any part of it to the person using the bot
with confidence.

---

## 1. What this project actually is

The bot implements a trading strategy written in plain English (reproduced in
section 3). It says things like "buy a Call option when price is above VWAP, above
two moving averages, the Force Index is positive, and the chart is making Higher
Highs and Higher Lows." That's a **recipe a human trader would normally watch a
chart and follow by hand, all day, every 5 minutes.**

This project is software that watches the market itself, computes the same things a
human would look at (VWAP, moving averages, etc.), and follows that exact recipe
automatically — deciding when to buy, when to sell, and actually placing those
orders on the trader's Kotak Securities account through Kotak's official
programming interface (API).

It is **not** artificial intelligence and it does not "learn" or improve the
strategy. It mechanically checks the same conditions the strategy describes, every 5
minutes, for as long as it's running.

---

## 2. Glossary (trading terms used throughout this doc and the code)

| Term | Meaning |
|---|---|
| **F&O** | "Futures & Options" — derivative contracts, as opposed to buying shares directly. |
| **Nifty** | India's benchmark stock market index (NSE), tracking the top 50 companies. You can't buy "the index" directly, but you can trade Futures and Options based on it. |
| **Futures** | A contract to buy/sell Nifty at a fixed price on a fixed future date. Has a real traded price and real traded volume. |
| **Option (CE/PE)** | A contract giving the right (not obligation) to buy (**CE** = Call) or sell (**PE** = Put) Nifty at a fixed "strike" price by a fixed date. Buying a CE profits if Nifty rises; buying a PE profits if Nifty falls. This strategy only ever **buys** CE or PE — it never sells/writes options. |
| **Strike price** | The fixed price written into an option contract (e.g. a "24000 CE" gives the right to buy at 24000). |
| **ATM (At-The-Money)** | The strike price closest to the current Nifty price. This bot always trades the ATM strike, recalculated live (rounded to the nearest 50 points). |
| **Expiry** | The date an F&O contract stops existing. The bot always uses the *nearest* unexpired contract. |
| **Lot size** | Options/futures can only be traded in fixed-size bundles (e.g. 75 units per lot for Nifty, though this changes over time — see caveats). You can't buy "10 units," only whole lots. |
| **LTP** | Last Traded Price — the most recent price a contract actually traded at. |
| **Candle / OHLCV** | A summary of trading activity over a fixed time window (e.g. 5 minutes): **O**pen, **H**igh, **L**ow, **C**lose price, and **V**olume traded. A price chart is just a sequence of candles. |
| **VWAP** | Volume-Weighted Average Price — the average price paid across the day so far, weighted by how much volume traded at each price. Roughly: "is the current price above or below what most people paid today?" |
| **EMA** | Exponential Moving Average — a smoothed average of recent closing prices that reacts faster to new data than a simple average. "EMA9" = smoothed over ~9 candles. |
| **Force Index** | An indicator combining price change and volume, meant to capture the "force" behind a price move (a big move on big volume = strong force). Elder's classic version smooths it with a 13-period EMA. |
| **HH/HL, LH/LL** | Chart pattern shorthand: **H**igher **H**igh / **H**igher **L**ow (price making progressively higher peaks and troughs = uptrend), vs **L**ower **H**igh / **L**ower **L**ow (downtrend). |
| **MIS** | "Margin Intraday Square-off" — an order product type meaning the position must be closed the same trading day (no overnight holding). This bot exclusively uses MIS. |
| **Paper trading** | Simulating trades (logging what *would* happen) without sending real orders or risking real money. |
| **Broker API** | A programming interface a brokerage (here, Kotak Securities) provides so software — instead of a human clicking buttons — can log in, read prices, and place orders on an account. |

---

## 3. The strategy (verbatim, for reference)

- **Market**: Nifty F&O. **Timeframe**: 5-minute candles, cross-checked against 30-minute candles.
- **Indicators**: VWAP, EMA9, EMA15, Elder's Force Index.
- **Buy a Call (CE)** at the ATM strike when: price/candle closes above VWAP, Force
  Index is above zero, price is above both EMA9 and EMA15, **and** both the 5-min
  and 30-min charts are making Higher Highs/Higher Lows.
- **Buy a Put (PE)** at the ATM strike when: the mirror image of the above (below
  VWAP, Force Index below zero, below both EMAs, Lower Highs/Lower Lows on both
  timeframes).
- **Exit a CE** when price closes below VWAP, **or** Force Index goes below zero.
- **Exit a PE** when price closes above VWAP, **or** Force Index goes above zero.

### 3a. Revised rules after the 30-Jul-2026 review

Reviewing that session's log, the trader found three signals the bot skipped and
two entries it should not have taken. The rules below supersede the ones above
wherever they differ; section 9a explains what in the code produced each case.

- **Entry is a VWAP *cross*, not a VWAP *side***. A CE is taken on the green candle
  that carries price from at/below VWAP to above it — never while price is already
  parked above VWAP. PE is the mirror (red candle crossing down). This is the
  single biggest change: it is what rules out the 13:45 and 15:10 entries.
- **Trend is read as "not making lower lows"**, on the ordinary 3-bar pivot, rather
  than demanding a simultaneous higher high *and* higher low confirmed two bars
  late. A pullback inside an uptrend still counts as an uptrend.
- **The 30-min chart no longer gates entry.** It is still computed, logged and
  charted, but requiring it to confirm blocked the valid 12:25 signal — the 30-min
  structure was still carrying that morning's lower low while the 5-min chart had
  already turned. Entry is now a 5-min decision.
- **ATM strike comes from the NIFTY 50 spot index**, not from Nifty Futures. At
  12:25 on 30-Jul futures were 24315 while spot was 24264.05 — a full strike apart,
  so the bot was buying 24300 CE where the strategy calls for 24250 CE.
- **Profit target**: 30 points of the underlying, which at an ATM delta of ~0.5 is
  about 15 points of premium; whichever is reached first closes the position. Both
  are configurable in `.env`.

Everything else in this document is about *how* that recipe gets executed by software
against Kotak's platform, and the real-world constraints that shaped the "how."

---

## 4. The historical data problem (the thing you asked about)

### What "historical data" means and why it normally matters

To compute something like a 15-candle moving average *right now*, you need the
previous 14 candles' worth of prices. Normally, trading software gets this by
asking the broker: "give me the last 100 five-minute candles for Nifty." That's
called **historical/historical OHLC data**, and it's standard on most trading
platforms (Zerodha, Upstox, TradingView, etc. all offer it).

It's also what lets you **backtest** — replay a strategy against, say, the last 6
months of past data, to see how it would have performed before ever risking real
money on it.

### What Kotak's API actually offers (I read the entire SDK to confirm this)

I didn't rely on the README alone — I downloaded the actual Kotak Neo Python
package (`neo_api_client`, the thing this bot imports) and read every source file
in it, plus grepped the whole package for anything resembling "candle," "chart,"
"history," "interval," "backtest," or "algo." Here's the complete picture of what
data Kotak's API can give you:

| Kotak API method | What it gives you |
|---|---|
| `quotes()` | A **single live snapshot** per instrument: last traded price, today's open/high/low so far, previous day's close, bid/ask depth, 52-week high/low. One row, right now. **Not a series of candles.** |
| `subscribe()` (websocket) | A **live stream** of individual trade ticks (price + volume) as they happen, from the moment you subscribe onward. Nothing from before you subscribed. |
| `order_report()` / `order_history()` / `trade_report()` | Records of *your own* past orders/trades — not market price history. |
| `scrip_master()` / `search_scrip()` | A lookup of what contracts exist (symbols, strikes, expiries, lot sizes) — not price history. |
| `positions()` / `holdings()` / `margin_required()` / `limits()` | Account state (what you currently hold, how much margin you have). |

That's the entire API surface. **There is no method anywhere in this SDK that
returns a series of past OHLC candles at any interval.** This isn't a documentation
gap I couldn't find — I confirmed it by reading the actual installed package
source code line by line (`neo_api.py`, every file under `api/`, `NeoWebSocket.py`),
not just the public README.

### So — can we do this without historical data? Yes, and that's exactly what this bot does.

The bot never asks Kotak for past candles. Instead, from the moment it starts each
morning, it:

1. Opens a live tick stream (`subscribe()`) for the current Nifty Futures contract.
2. Builds its **own** 5-minute and 30-minute candles in memory, tick by tick, as the
   session unfolds (this is the `src/candles.py` file).
3. Computes VWAP/EMA/Force Index/HH-HL structure on *that self-built series only*.

This fully avoids needing Kotak to provide historical data — but it has two real
consequences the trader should know about:

- **No backtesting is possible with this bot as built.** It has no memory of any day
  before the one it's currently running. You can't ask it "how would this have done
  last month?" To backtest, you'd need a *separate* historical data source (e.g. a
  paid data vendor, or NSE's own downloadable data) and separate backtesting code —
  that's not part of what exists today, but could be added later if useful.
- **There's a daily "warm-up" period with no trades.** Each morning the bot starts
  from zero candles. The strategy needs at least 15 five-minute candles to compute
  EMA15 and the Force Index (that's ~75 minutes of market time, so roughly until
  ~10:30 AM if the market opens 9:15 AM), and the Higher-High/Higher-Low structure
  check needs at least two *confirmed* swing points on the slower 30-minute chart,
  which realistically pushes the earliest possible trade later into the morning. The
  bot will sit there logging "indicators still warming up" / "no entry conditions
  met" during this window — that's expected, not a bug.

### Does Kotak run any trading algorithm for us?

No. I want to be explicit about this since it's easy to assume a broker's "Trade
API" product might include some kind of built-in signal/strategy engine. It
doesn't. Kotak's API is purely **plumbing**: it logs you in, tells you prices, and
places orders. It has no concept of "strategy," "signal," or "backtest" anywhere in
it (I grepped for all of these terms across the entire package — nothing). The word
"algo" appears only in Kotak's internal URL naming (e.g. `algo-user/v5/...`), which
is just what Kotak calls their whole trading-API product line, not an actual
algorithm running on their end. **100% of the "brain" — every indicator
calculation and every buy/sell decision — is code we wrote in this repository.**
Kotak never sees or evaluates the strategy; it only receives the final "buy X" or
"sell X" instruction once our code has already decided to send it.

---

## 5. Why this isn't a GitHub Actions job

This was the original question that started this project. Two reasons rule it out,
both following directly from the point above:

1. Since there's no historical candle data, the bot has to build candles itself
   from a **continuously open live connection**, all day. GitHub Actions runs jobs
   as short, independent, scheduled executions — it can't hold an open websocket
   connection and an in-memory candle buffer across a 6-hour trading session.
2. GitHub's `schedule` cron trigger is explicitly "best effort" — GitHub's own docs
   warn that scheduled runs can be delayed by minutes under load, with no guaranteed
   exact-minute execution. For a strategy that acts on 5-minute candle closes,
   that's an unacceptable amount of timing slop.

Instead, this runs as one continuous Python program (`python -m src.main`) for the
whole trading day, on whatever machine you choose to run it on (your own PC to
start, a cheap always-on cloud server later if desired).

---

## 6. How the whole system fits together (data flow)

```
 9:15 AM                                                          3:20/3:30 PM
   │                                                                    │
   ├─ Log in to Kotak (auto TOTP + MPIN)                               │
   ├─ Look up today's Nifty Futures contract & subscribe to live ticks │
   │                                                                    │
   │   ┌────────────────────────────────────────────────────────┐      │
   │   │  every tick: price + volume arrive over the websocket   │      │
   │   │       │                                                 │      │
   │   │       ▼                                                 │      │
   │   │  bucket into 5-min AND 30-min candles (candles.py)      │      │
   │   │       │                                                 │      │
   │   │       ▼  (only fires the moment a 5-min candle closes)  │      │
   │   │  compute VWAP / EMA9 / EMA15 / Force Index / HH-HL      │      │
   │   │  on both timeframes (indicators.py)                     │      │
   │   │       │                                                 │      │
   │   │       ▼                                                 │      │
   │   │  check entry/exit rules from the strategy spec          │      │
   │   │  (strategy.py) → "enter CE" / "enter PE" / "exit" / hold│      │
   │   │       │                                                 │      │
   │   │       ▼                                                 │      │
   │   │  risk checks: already in a position? daily loss cap hit?│      │
   │   │  (risk.py)                                              │      │
   │   │       │                                                 │      │
   │   │       ▼                                                 │      │
   │   │  look up the ATM CE/PE contract, get its live price,    │      │
   │   │  and either log it (paper mode) or place the real order │      │
   │   │  via Kotak's place_order() (execution.py)               │      │
   │   └────────────────────────────────────────────────────────┘      │
   │                                                                    │
   └─ At ~3:20 PM: close any still-open position, then stop             │
```

---

## 7. What each file does (plain-English tour of `src/`)

| File | Plain-English role |
|---|---|
| `config.py` | Reads all settings from a `.env` file — credentials, whether to place real orders or just log them, lot size, daily loss limit, profit target. |
| `auth.py` | Logs into Kotak (mobile number + UCC + an auto-generated 6-digit TOTP code + MPIN). Also wraps every API call so that if a call fails in a way that looks like the session expired, it logs in again and retries once automatically. |
| `instruments.py` | Figures out *which exact contract* to trade: the current Nifty Futures contract (whose ticks build the candles), the NIFTY 50 spot index (whose level picks the ATM strike — see section 3a), and the ATM Call/Put itself, using Kotak's built-in `search_scrip()` lookup (see section 9 for why this changed from an earlier version). Also fetches a live price for a specific contract. |
| `candles.py` | Takes a stream of individual trade ticks and groups them into 5-minute and 30-minute OHLCV candles — this is the code that replaces the "historical data" Kotak doesn't provide, by building the chart ourselves in real time. |
| `indicators.py` | Pure maths: given a list of candles, compute VWAP, EMA, Elder's Force Index, and detect Higher-High/Higher-Low (or Lower-High/Lower-Low) chart structure. No network calls — this is why it can be unit-tested without touching Kotak or the market at all. |
| `strategy.py` | Encodes the strategy's exact rules from sections 3 and 3a: given the closed candle and its indicator values, decide "enter CE," "enter PE," "exit," or "do nothing." When it holds, it records *which* condition blocked the entry, so a decision can be checked against a chart afterwards. |
| `risk.py` | Guardrails: won't open a second position while one is already open, and (once configured) will stop taking new trades if the day's losses cross a limit you set. |
| `execution.py` | Actually acts on a decision: buys/sells the resolved contract. Controlled entirely by one on/off switch (`LIVE_TRADING`) — when off, it only logs what it *would* have done and tracks a simulated profit/loss; when on, it calls Kotak's real order-placement API. Both modes run through identical logic otherwise. |
| `ws_feed.py` | The plumbing between Kotak's live data feed and `candles.py` — receives raw tick messages and hands clean (price, volume, time) values onward. |
| `main.py` | Wires everything above together and runs the whole day: log in → find contracts → **restore any position left open by an earlier run today** → start the feed → react to every 5-min candle close → watch the profit target between closes → square off any open position before market close → stop the feed. |
| `logging_config.py` | Sends a copy of everything the bot does to both the screen and a daily log file (`logs/bot.log`), so a session can be reviewed afterward. Caps the file at 20 MB and collapses repeated messages, so a malfunctioning loop can't produce a log too large to open (see section 9b). |
| `storage.py` | Saves every candle, decision, and trade into a local SQLite database file (`data/bot.db`) as the bot runs — this is what section 10's dashboard reads from. Also the record that lets an open position survive a restart. |
| `dashboard.py` + `templates/dashboard.html` | A small, separate web page (run with `python -m src.dashboard`) that reads `data/bot.db` and shows it in a clean, non-technical view — see section 10. |

---

## 8. Key decisions made, and why

| Decision | What we chose | Why |
|---|---|---|
| Where does it run? | A long-running Python process (your own PC first, movable to a cloud VM later) | GitHub Actions can't hold an open live connection reliably enough for 5-min timing (section 5). |
| What feeds the indicators — Nifty spot index, or Nifty Futures? | **Nifty Futures** (current month contract) | VWAP and Force Index both require real traded **volume**. The Nifty index itself is a calculated number with no genuine traded volume; Futures are an actual traded instrument with real volume. |
| Force Index: raw or smoothed? | **13-period EMA-smoothed** (Elder's original definition) | Matches how the indicator is classically defined and how most charting platforms show it by default; less noisy zero-line crosses than the raw 1-bar version. |
| How to detect Higher-High/Higher-Low structure? | **2-bar fractal**: a candle's high/low only counts as a confirmed swing point if it's the most extreme point among the 2 candles before *and* after it | A standard, low-noise definition of a "swing point." Trade-off: a swing point is only confirmed 2 candles after it forms — a small, deliberate lag in exchange for not reacting to every minor wiggle. |
| Position sizing / daily loss limit | Config values (`.env`), placeholders until you set real numbers | The strategy doesn't specify these at all — they're business decisions, not something to guess at in code. |
| Can it hold more than one position at once? | No — one position at a time, hard-coded | The strategy describes a single directional bet (CE or PE), never both together or multiple entries stacked. |
| Live vs. paper trading | One switch, `LIVE_TRADING=true/false` in `.env`, defaulting to **off** | The design asks for the real order-placing code to exist but be easy to turn off — so early runs are safe to observe without risking money, and going live later is a one-line config change, not a code change. |

---

## 9. Things that were wrong in an earlier pass, and how they got fixed

Kotak's own public documentation for this SDK is incomplete in places (it doesn't
list the full column names in its data files, or the exact shape of live tick
messages). The first version of this bot filled those gaps with reasonable-looking
guesses, clearly flagged as guesses. Since then, I read the *actual installed
package source code* (not just the docs) to replace those guesses with verified
facts:

- **Instrument lookup** originally hand-downloaded Kotak's raw contract-list CSV
  file and guessed at column names (like assuming a column called "instrument
  type" existed). Reading `neo_api_client`'s own source showed it already has a
  built-in `search_scrip()` function that does this lookup correctly, using the
  *real* column names (`pSymbolName`, `pOptionType`, `dStrikePrice;`,
  `pExpiryDate`). `instruments.py` was rewritten to use that instead of
  re-guessing — simpler and more reliable.
- One sharp edge discovered in that same code: `search_scrip(symbol="nifty")`
  matches names by substring, so it would also match "BANK**NIFTY**",
  "FIN**NIFTY**", "MIDCP**NIFTY**", etc. The code now explicitly filters results
  down to an exact match on "NIFTY" to avoid ever accidentally resolving to the
  wrong index's contract.
- **Live tick message format**: reading `NeoWebSocket.py` directly showed the exact
  shape (`{"type": "stock_feed", "data": [ {...tick...}, ... ]}`, with short field
  codes like `ltp`, `v`, `ltt`, `ltq` inside each tick). `ws_feed.py` was corrected
  to match this exactly, rather than guessing at plausible field names.
- **Order parameters** (`place_order`'s exact accepted values for order type,
  product, transaction type, validity) were cross-checked against the SDK's own
  internal validation rules (`req_data_validation.py`) and confirmed correct as
  originally written.

### 9a. What the 30-Jul-2026 session review turned up

Five complaints, four underlying defects. All five cases are now covered by tests
(`tests/test_strategy.py`), and replaying that day's recorded candles through the
fixed engine reproduces exactly the behaviour the strategy asks for.

1. **Decisions were being made on the wrong candle.** `handle_5m_close` evaluated
   `all_candles()`, which ends with the *next* candle — a bar holding a single
   tick, opened microseconds earlier. So every signal was judged on the first
   print of the next candle instead of the close of the candle that just
   finished. Visible in the recorded data: the 13:50 decision logged
   `close=24345.00`, which is the 13:50 candle's opening tick, while the 13:45
   candle it was meant to judge had closed at 24348.50. Fixed by adding
   `TickAggregator.closed_candles()` and evaluating on that.
2. **No VWAP-cross requirement.** The rule was implemented as `close > VWAP`, so
   any candle in a trend that had long since crossed VWAP kept qualifying. That
   is the 13:45 entry (price 27 points above VWAP, opened above it) and the 15:10
   entry (which was additionally a *red* candle). Fixed in
   `strategy._vwap_cross`, plus an explicit green/red requirement.
3. **Market structure was too strict to ever fire.** The old rule wanted a
   confirmed higher high *and* higher low from 2-bar fractals. On 30-Jul the
   30-min structure came back `None` for the entire session, and the 5-min
   structure was `None` at 14:35 and `LH_LL` at 11:10 — so three valid signals
   were held. `indicators.swing_structure` now reads the trend the way a chart
   does, and the 30-min timeframe no longer gates entry.
4. **Nothing stopped the bot at the close.** Entries were taken at 15:25 and
   15:30 — after the 15:20 square-off — and the 15:30 one was never closed at
   all. Meanwhile the websocket kept reconnecting and aggregating stale
   post-close prints into flat zero-volume candles until 19:40. Fixed with
   `candles.in_session` (ticks outside 09:15–15:30 build no candles), an entry
   cut-off at the square-off time, a square-off that retries instead of latching
   after one attempt, and `LiveFeed.stop()`.

Replayed on the recorded 30-Jul candles, the fixed engine takes the 11:10, 12:25
and 14:35 entries, skips 13:45 and 15:10, and closes two of the four trades on the
+30-point target.

### 9b. What the 03-Aug-2026 session review turned up

Three problems were reported: the bot picked the wrong strike, it only ever
recorded buys and never closed anything, and there was no dashboard for the day.
The log for that session was 87 MB — 917,890 lines against 97 KB and roughly a
thousand lines on a normal day — and that outsized log turned out to be the
thread that explained everything else.

1. **A websocket connection leak took the process down three times.**
   `LiveFeed._on_close` called `start()` to reconnect. That was never necessary:
   the SDK's socket runs under `run_forever(ping_interval=0, reconnect=5)`, so
   websocket-client already retries every five seconds, and `NeoWebSocket`
   already replays every subscription when the reconnect is acknowledged. Worse,
   the SDK sets `is_hsw_open = 0` *before* invoking the close callback, so
   `client.subscribe()` took its "no live socket" branch and spawned a brand new
   socket thread rather than reusing the existing one — and the old connection
   stayed alive to fire its own close later.

   Fix 4 in the 30-Jul pass had added the spot-index subscription, which made
   `start()` issue *two* subscribe calls instead of one. That took the branching
   factor from one to two and turned a linear leak into an exponential one:

   | | 30-Jul (one subscribe) | 03-Aug (two subscribes) |
   |---|---|---|
   | Reconnects logged | 236 | 307,506 |
   | Distinct sockets created | — | 101,104 |
   | Log size | 97 KB | 87 MB |

   The process ran out of OS threads and died — `can't start new thread` appears
   at 10:11:26, 11:18:43 and 13:44:29, which are exactly the last log lines
   before each of the three restarts. Fixed by deleting the reconnect from
   `_on_close` entirely and letting the SDK do the job it was already doing;
   `start()` is now idempotent, and the main loop watches
   `LiveFeed.seconds_since_last_tick()` so a feed that reconnects but never
   delivers is reported rather than hidden.

2. **The NIFTY 50 lookup could never match, so strikes came off the futures
   price.** `resolve_nifty_index` searched `symbol="nifty 50"`, but
   `search_scrip()` filters with `df["pSymbolName"].str.contains(symbol)` and in
   the cash scrip master `pSymbolName` is `NIFTY` — "Nifty 50" only ever appears
   in `pTrdSymbol`. The search raised "No data found" on every startup, silently
   demoting strike selection to the hardcoded fallback token, and the index feed
   then never printed. All three entries logged *"No spot index price yet;
   selecting the ATM strike from the futures price"* and bought 24600 CE off
   futures at 24617.60 while Nifty itself was at 24530 — two strikes away from
   ATM, which breaks the premium-based target the strategy is built on.

   Fixed by searching the broad `"nifty"` and picking the exact row out of the
   results (`_is_nifty_50_row`). Two backstops were added because a wrong strike
   is worse than no trade: if the websocket index is silent the bot now falls
   back to a REST quote of the *index*, and if even that fails it **skips the
   entry** rather than ever deriving a strike from the futures price.

3. **Position state lived only in memory, so every restart abandoned the open
   trade.** `OrderExecutor.position` was never persisted. After each crash the
   bot came back believing it was flat: the open trade's row kept `exit_time`
   NULL forever, the profit target and the end-of-day square-off were both
   skipped (each is gated on `position is not None`), and the strategy took a
   *fresh* entry instead of managing the one already on. That is precisely
   "trade not closing, means only buy entries record" — three buys, zero exits.

   Fixed by persisting the instrument token, exchange segment and underlying
   entry price with each trade, and restoring the open position (and the day's
   realised P&L, so the loss cap still applies) on startup via
   `OrderExecutor.restore_open_position`. Trades left open by *earlier* sessions
   are closed out administratively by `storage.abandon_stale_open_trades`, which
   deliberately leaves `exit_price` and `pnl` NULL — the bot never squared them
   off, so any number there would be invented. Every P&L and win-rate total now
   skips rows with a NULL `pnl`.

4. **The missing dashboard was a symptom, not a separate fault.** Only five
   5-minute candles were ever stored for the day (against 126 on 30-Jul) because
   the feed was dead for most of the session, and all three trades were open with
   no P&L, so the dashboard was faithfully rendering an almost-empty database.
   One related bug was fixed alongside it: `get_open_trade()` was not scoped to a
   session, so a 30-Jul trade that had been orphaned was still being shown as
   today's open position.

5. **The log itself is now capped.** `logs/bot.log` rotated only at midnight with
   no size ceiling, so a runaway loop had a whole day to fill one file.
   `TimedSizedRotatingFileHandler` adds a 20 MB ceiling on top of the daily
   rotation (numbering the extra files rather than overwriting the day's
   archive), and `DuplicateSuppressor` collapses identical messages to 20 per
   minute, keyed on the *unformatted* template so `"reconnecting in %ds"`
   collapses across its arguments. The count of what was dropped is appended to
   the next message that gets through, so suppression is always visible in the
   log itself. Applied to the file handler only — the console still shows
   everything for anyone running the bot interactively.

`tests/test_recovery.py` covers all of this, including an end-to-end replay of
the 03-Aug sequence (enter → lose the process → restart → target closes the
position) and a check that no strike is ever resolved without a spot price.

### 9c. What the 04-Aug-2026 session review turned up

The first session run on the 9b fixes. Three of them held up in production:

| | 03-Aug | 04-Aug |
|---|---|---|
| Log size | 87 MB (917,890 lines) | **47 KB (538 lines)** |
| Sockets created | 101,104 | **1** |
| Process crashes | 3 (`can't start new thread`) | **0** — survived to a clean 15:30 close |

The database also confirmed the migration ran on a real `bot.db` and
that the four orphaned trades were closed out correctly, with NULL P&L, leaving
30-Jul's genuine figures untouched.

But the bot still took no trades: **zero candles, zero decisions**. The feed
delivered one tick at 09:07 and nothing for the remaining 6.4 hours, logging 375
stall warnings while the SDK reconnected every ~130 seconds without ever
restoring data. Two defects behind that, both newly visible now that the storm
was no longer burying them.

1. **Two competing sockets are opened at every startup.** `HSWebSocketLib`
   transmits through a **module-level global `ws`**, which every `StartServer`
   rebinds on construction. The SDK decides in `get_live_feed()` whether to
   reuse the live socket or open a new one by testing `is_hsw_open == 1` — a
   flag only set when the server's connect acknowledgement arrives,
   asynchronously. Issuing the futures and index subscriptions back to back
   meant the flag was still `0` for the second, so a *second* socket was opened.
   The two then shared one global: each one's `on_open` sent its connect frame
   over whichever socket had bound the global last, and the loser got
   `Connection is already closed`. Its handshake never completed, `is_hsw_open`
   never reached 1, and the SDK's resubscribe-on-reconnect path — the one 9b
   relied on — was therefore never reached.

   The tell is a pair of `Websocket connected` lines at every startup, present
   in both the 03-Aug and 04-Aug logs. Fixed by waiting for the handshake
   (`_wait_until_socket_live`) before the second subscribe, so both legs land on
   one socket.

   *This means the 9b reasoning was right about what the SDK's code does and
   wrong about whether it ever runs.* The resubscribe logic exists; it was
   unreachable.

2. **Warning about a dead feed was not enough.** The bot correctly reported the
   stall 375 times and did nothing about it. `LiveFeed.restart()` now rebuilds
   the connection — closing the old socket and discarding the SDK's
   `NeoWebSocket` so a new one replaces it rather than accumulating — after five
   minutes of silence, rate-limited to one attempt per five minutes and driven
   only from the main loop, never from a callback thread. That shape is
   deliberate: it is what keeps recovery from turning back into 03-Aug.

3. **The NIFTY 50 row is called `NIFTY`, not `NIFTY 50`.** The diagnostic added
   in 9b paid for itself: the log printed what the scrip master actually
   returned — `['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'HDFCNIFTY-BL',
   ...]`. The string "NIFTY 50" never appears in `nse_cm` at all, so matching
   only `NIFTY50` still fell through to the hardcoded fallback token on every
   startup. Bare `NIFTY` is now accepted as an *exact* match, which is
   unambiguous in the cash segment (the derivatives live in `nse_fo`) and still
   excludes NIFTYBEES, FINNIFTY, NIFTYQLITY and the rest.

### 9d. What the 14-Aug-2026 session review turned up

Everything from 9c held up. For the first time the bot ran a complete, healthy
session:

| | 03-Aug | 04-Aug | 14-Aug |
|---|---|---|---|
| Log size | 87 MB | 47 KB | 84 KB |
| Sockets per startup | ~2 then thousands | 2 | **1** |
| Candles built | 5 | 0 | **74** |
| Process deaths | 3 | 0 | 0 |

The index lookup resolved properly (`token 26000 (matched 'NIFTY')`), and the
stall-recovery path added in 9c fired at 09:15:02, rebuilt the connection, and
brought the feed back — candles start flowing from 09:20. Both are visible in
the log doing exactly what they were written to do.

The strategy then produced **three valid entry signals** — a PE at 11:00 and CEs
at 12:50 and 13:40 — and took none of them. The trader's read ("it failed to do
entries but its logic was correct") is exactly right.

**Every entry was blocked by the spot-price guard.** Both routes to the NIFTY 50
index level fail on this account:

- the websocket index subscription has **never delivered a single tick** — the
  log says `no tick yet` 374 times and `silent for N s` zero times, so it is not
  a dropout, it never started;
- the REST quote is rejected outright: HTTP 400 `Invalid neosymbol values` for
  `nse_cm|26000`.

Both symptoms together are consistent with the account not being entitled to
index market data, which no amount of code can fix. Option quotes, meanwhile,
work perfectly — every option price the bot has ever fetched came through the
same `fetch_ltp`.

**Why this only appeared now.** It was observed that entries worked on
30-Jul and something changed afterwards. That is precisely correct: on 30-Jul
the code passed `feed.latest_ltp` — the *futures* price — straight into
`resolve_atm_option`, so no spot was ever required. Requiring the spot index
(9b, fix 2/3) is what began blocking entries. It was a deliberate change, made
to fix the reported complaint that the bot bought 24600 CE while Nifty was
at 24530 — so the answer is not to undo it, but to obtain spot from a source
that works.

**The fix: put-call parity.** For any strike K and expiry T,

    C - P = S - K·e^(-rT)    =>    S = C - P + K·e^(-rT)

so two option quotes yield the spot level directly. `spot_from_put_call_parity`
uses the futures price only to decide *which* strike to quote — the relation
holds at any strike, so the futures premium cancels out of the answer entirely.
It costs two REST quotes and runs only when the direct index sources have
failed. Spot is now sourced in this order:

1. the websocket index tick,
2. a REST quote of the index,
3. put-call parity on the option pair,
4. otherwise no trade, exactly as before.

The order matters: if index data is ever enabled on the account, the bot
silently upgrades back to reading it directly, with no code change.

The result on the reported numbers: from futures 24618 with spot at 24530,
parity recovers 24530 and selects the **24550** strike — the ATM strike the strategy
asked for — where the futures price gives 24600.

Two diagnostic gaps that made this harder than it needed to be were also closed:
the resolved index instrument now logs the exchange segment it actually ended up
using (not the one that was searched), and `fetch_ltp` reports the exact
`segment|token` neosymbol string that Kotak rejected. `scripts/diagnose_spot.py`
is a read-only probe that can be run during market hours to test the index
routes directly, if the index feed is ever worth pursuing again.

### What's still an assumption (worth checking on the first real run)

- `resolve_nifty_futures()` assumes futures contracts have an empty/blank
  `pOptionType` field (since only options should have "CE"/"PE" there). This isn't
  documented anywhere; if it's ever wrong, the code raises a clear error showing a
  sample data record rather than silently trading the wrong contract.
- The exact fields present in every live tick message (some ticks may only carry a
  price update, others a fuller snapshot) — `ws_feed.py` logs the raw message for
  every tick at DEBUG level so this can be visually confirmed in the log the first
  time it runs against real market data.
- **Nifty's lot size** changes periodically (NSE revises it based on index level,
  roughly once or twice a year) — the code always reads the *current* lot size live
  from Kotak's own data rather than hard-coding a number, so this isn't a real risk,
  just worth knowing it isn't a fixed constant.
- **Session/token expiry** isn't documented by Kotak anywhere. The bot logs in once
  at the start of the day and automatically re-logs-in and retries if any API call
  ever fails in a way that looks like an expired session — but the *exact* expiry
  behavior (e.g. does it expire at midnight, after N hours, etc.) has never been
  confirmed against a real account.

---

## 10. The dashboard — seeing what the bot is doing, without reading logs

Everything the bot does is also saved into a small local database file
(`data/bot.db`) — every 5-minute candle, every computed indicator value, every
decision ("hold," "enter CE," "exit," and why), and every trade with its entry,
exit, and profit/loss. This is what powers a separate, simple web page (the
"dashboard") built specifically so the trader doesn't have to read a text log
file to understand what's happening.

The dashboard is its own small program (`src/dashboard.py`) — it runs
independently of the trading bot and only ever *reads* that database, so it's
safe to open any time, whether or not the bot is currently running.

**For day-to-day use, the trader never needs to open a command prompt at all.**
Two files sit in the project folder for exactly this:

- **`Start Trading Bot.vbs`** — double-click it. It silently starts both the bot
  and the dashboard in the background (no window appears at all) and opens the
  dashboard in the default browser automatically once it's ready.
- **`Stop Trading Bot.vbs`** — double-click it to stop both, with a small popup
  confirming it's done.

(Running the two pieces manually — `python -m src.dashboard` then opening
`http://127.0.0.1:8765` — still works exactly the same way if you ever need to
run it that way instead, e.g. while developing.) Either way, it shows the same
two views:

- **Today** — a live-updating page (refreshes every few seconds) showing: today's
  running profit/loss, whether a position is currently open and its details, a
  chart of today's price against every indicator the strategy uses — VWAP, EMA9,
  EMA15 and Force Index (so you can visually see why an entry did or didn't fire;
  each line can be individually shown/hidden and recoloured, and your choices are
  remembered in the browser), a table of every trade taken today, and a running
  log of every decision the bot made, even the "hold, no conditions met" ones —
  so nothing is a black box.
- **History** — pick any date range and see: total profit/loss, win rate, a
  day-by-day profit/loss bar chart, a cumulative "equity curve" (is the overall
  balance trending up or down over weeks/months), and every individual trade in
  that range. This is what answers "how did we do this month?" without needing to
  dig through log files.

Because this data is stored locally in a plain SQLite file, it accumulates
indefinitely across every day the bot runs — there's no server-side account or
subscription involved, and no data is ever sent anywhere except to Kotak (for
prices/orders). The file lives at `data/bot.db`; back it up like any other file if
you want to preserve trading history long-term.

**If something goes wrong at startup** (most commonly: incomplete `.env`
credentials), there's no visible window to show an error in, since
`Start Trading Bot.vbs` runs everything hidden. The bot is written so this can
never go completely unnoticed, though: it always writes a clear error to
`logs\bot.log` before exiting, even when nothing was ever visible on screen — so
"the dashboard just shows 'Not Running' and nothing happens" means: open
`logs\bot.log` and read the last few lines.

## 11. Setup guide

### Prerequisites
- Python 3.10+ installed.
- A Kotak Securities account with Neo Trade API access enabled (the trader needs
  to generate a "Consumer Key" from the Neo app/website, under **Invest → Trade
  API**), and their TOTP authenticator already set up for login.

### Steps

```bash
# 1. From the project folder, create an isolated Python environment
python -m venv .venv

# 2. Activate it
.venv\Scripts\activate            # Windows
# or: source .venv/bin/activate   # Mac/Linux

# 3. Install dependencies (this also installs Kotak's own SDK from GitHub)
pip install -r requirements.txt

# 4. Create your real config file from the template
copy .env.example .env            # Windows
# or: cp .env.example .env        # Mac/Linux
```

Now open `.env` and fill in:
- `KOTAK_CONSUMER_KEY` — from the Neo app/website, Invest → Trade API.
- `KOTAK_MOBILE_NUMBER`, `KOTAK_UCC`, `KOTAK_MPIN` — the account's login details.
- `KOTAK_TOTP_SECRET` — **the secret key**, not a 6-digit code. This is the same
  secret that was used to set up the authenticator app (usually shown as a QR code
  or a text string at setup time) — the bot generates fresh 6-digit codes from it
  automatically every time it logs in, so nobody has to type a code in by hand each
  morning.
- Leave `LIVE_TRADING=false` for now.

### Run the tests (checks the maths, no market connection needed)

```bash
pytest tests/
```

All 24 tests should pass. These only check the indicator/strategy *logic* (VWAP,
EMA, Force Index, structure detection, entry/exit rules) and the dashboard's
chart data against hand-built examples — they don't touch Kotak or the market
at all.

### Do a dry run during market hours (9:15 AM – 3:30 PM IST)

```bash
python -m src.main
```

With `LIVE_TRADING=false`, this will log in for real, subscribe to real live prices,
build real candles, and log every decision it makes — but it will **not** place any
real orders. Watch `logs/bot.log` (and the console) and compare what it's logging
against a live Nifty chart you pull up separately, for at least a few sessions,
before ever considering flipping it live.

### Go live (only after you're satisfied with dry-run behavior)

1. Set real numbers for `LOT_SIZE` and `MAX_DAILY_LOSS` in `.env` — the strategy
   the strategy doesn't specify these, so this is a business decision
   (yours) about how much capital/risk to put behind each signal.
   `TARGET_UNDERLYING_POINTS` (30) and `TARGET_PREMIUM_POINTS` (15) ship with the
   values from section 3a; either can be blanked out to disable that half of the
   target check.
2. Set `LIVE_TRADING=true`.
3. Run it again. From this point, every "enter"/"exit" decision places a real order
   on the account.

---

## 12. What this project deliberately does *not* do

Being explicit about scope, so nobody assumes more than what's built:

- **No backtesting** (explained in section 4) — this only ever looks forward from
  the moment it starts each day.
- **No trailing stop-loss, partial exits, or scaling into a position** — exits are
  exactly the two conditions the strategy specifies (VWAP cross / Force Index zero
  cross), nothing more elaborate.
- **No multi-underlying support** — it only trades Nifty, not Bank Nifty or single
  stocks, though the code is structured so that could be extended later if needed.
- **No web dashboard / UI** — everything is visible through the log file
  (`logs/bot.log`) and console output while it's running.
