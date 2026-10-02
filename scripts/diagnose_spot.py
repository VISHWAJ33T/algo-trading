"""Read-only probe: why can't the bot get the NIFTY 50 spot price?

Run this DURING MARKET HOURS (9:15 AM - 3:30 PM IST) on the machine that runs
the bot:

    .venv\\Scripts\\python.exe -m scripts.diagnose_spot

It places no orders and writes nothing to the database -- it only logs in, reads
market data, and prints what it finds.

Background: on 14-Aug-2026 the bot correctly identified three entry signals and
skipped all three, because it could not obtain the NIFTY 50 spot level that the
ATM strike must be derived from. Both routes failed:

  * the websocket index subscription delivered no ticks at all, ever
    ("no tick yet" x374, never once "silent for N s"), and
  * the REST quote returned HTTP 400 "Invalid neosymbol values".

Both routes use the same token and exchange segment, so a single wrong value
would explain both -- but the bot never logged which segment it actually used.
This script prints that, then tries the alternatives, so the answer comes from
one market-hours run instead of another day of guesswork.

NOTE: this logs in, which creates a new Kotak session. Stop the bot before
running it, in case Kotak invalidates the bot's existing session.
"""
from __future__ import annotations

import json
import time

from src.auth import KotakSession
from src.config import load_config
from src.instruments import (
    NIFTY_INDEX_FALLBACK_TOKEN,
    NIFTY_INDEX_SEGMENT,
    NIFTY_INDEX_SYMBOL,
    _is_nifty_50_row,
    resolve_nifty_futures,
)

# Segment names to try for the index, most to least likely. `nse_cm` is what the
# bot uses today; the rest are the plausible alternatives if indices are served
# under a different segment than cash equities.
CANDIDATE_SEGMENTS = ["nse_cm", "nse_com", "NSE", "nse_ind", "nse_cm_idx"]
CANDIDATE_TOKENS = ["26000", "26009", "Nifty 50", "NIFTY 50", "NIFTY"]
QUOTE_TYPES = ["ltp", "ohlc", "all"]

LINE = "=" * 72


def heading(text: str) -> None:
    print(f"\n{LINE}\n{text}\n{LINE}")


def show(label: str, value) -> None:
    text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
    print(f"  {label:<34} {text[:400]}")


def main() -> None:
    config = load_config()
    session = KotakSession(config)
    session.login()
    print("Logged in.")

    # ---------------------------------------------------------------- step 1
    heading("STEP 1  What the scrip master actually says about the index")
    records = session.call_with_retry(
        "search_scrip", exchange_segment=NIFTY_INDEX_SEGMENT, symbol=NIFTY_INDEX_SYMBOL
    )
    if not isinstance(records, list):
        show("search_scrip FAILED", records)
        records = []
    print(f"  {len(records)} rows returned for symbol={NIFTY_INDEX_SYMBOL!r} in {NIFTY_INDEX_SEGMENT}")

    matches = [r for r in records if _is_nifty_50_row(r)]
    print(f"  {len(matches)} matched as the NIFTY 50 index row\n")
    for record in matches[:3]:
        print("  FULL MATCHED ROW (this is the ground truth we are missing):")
        for key, value in record.items():
            print(f"      {key:<24} {value!r}")
        print()

    if matches:
        index_token = str(matches[0].get("pSymbol", NIFTY_INDEX_FALLBACK_TOKEN))
        index_segment = str(matches[0].get("pExchSeg") or NIFTY_INDEX_SEGMENT)
    else:
        index_token, index_segment = NIFTY_INDEX_FALLBACK_TOKEN, NIFTY_INDEX_SEGMENT
    show(">>> token the bot would use", index_token)
    show(">>> segment the bot would use", index_segment)
    print("      (the REST call sends these as  '<segment>|<token>')")

    # ---------------------------------------------------------------- step 2
    heading("STEP 2  Does a REST quote work for the index, in any format?")
    print("  Looking for any combination that returns a price rather than a fault.\n")
    working = []
    for segment in dict.fromkeys([index_segment] + CANDIDATE_SEGMENTS):
        for token in dict.fromkeys([index_token] + CANDIDATE_TOKENS):
            for quote_type in QUOTE_TYPES:
                label = f"{segment}|{token}  ({quote_type})"
                try:
                    response = session.call_with_retry(
                        "quotes",
                        instrument_tokens=[
                            {"instrument_token": token, "exchange_segment": segment}
                        ],
                        quote_type=quote_type,
                    )
                except Exception as exc:
                    show(label, f"EXCEPTION {exc}")
                    continue
                ok = isinstance(response, (list, dict)) and "fault" not in str(response)[:60].lower()
                show(label, ("OK  " if ok else "fail ") + str(response))
                if ok:
                    working.append(label)
    print()
    show("WORKING REST COMBINATIONS", working or "NONE -- REST cannot quote the index")

    # ---------------------------------------------------------------- step 3
    heading("STEP 3  Does the websocket deliver index ticks?")
    print("  Subscribing to the futures (known to work) and the index together,")
    print("  then printing every raw message for 60 seconds.\n")

    futures = resolve_nifty_futures(session)
    show("futures", f"{futures.trading_symbol} token={futures.token} seg={futures.exchange_segment}")
    show("index", f"token={index_token} seg={index_segment}")
    print()

    seen = {"futures": 0, "index": 0, "other": 0}
    samples: list[str] = []

    def on_message(message):
        if not isinstance(message, dict) or message.get("type") != "stock_feed":
            return
        for tick in message.get("data") or []:
            if not isinstance(tick, dict):
                continue
            token = str(tick.get("tk", ""))
            if token == str(futures.token):
                seen["futures"] += 1
            elif token == str(index_token):
                seen["index"] += 1
                if len(samples) < 5:
                    samples.append(f"INDEX TICK: {tick}")
            else:
                seen["other"] += 1
                if len(samples) < 5:
                    samples.append(f"UNRECOGNISED TOKEN {token!r}: {tick}")

    client = session.client
    client.on_message = on_message
    client.on_error = lambda e: print(f"  [ws error] {e}")
    client.on_close = lambda *a: None

    client.subscribe(
        instrument_tokens=[
            {"instrument_token": futures.token, "exchange_segment": futures.exchange_segment}
        ],
        isIndex=False,
    )
    # Same wait the bot uses, so both legs land on one socket.
    for _ in range(80):
        if getattr(getattr(client, "NeoWebSocket", None), "is_hsw_open", 0) == 1:
            break
        time.sleep(0.25)
    client.subscribe(
        instrument_tokens=[{"instrument_token": index_token, "exchange_segment": index_segment}],
        isIndex=True,
    )

    for remaining in range(60, 0, -10):
        print(f"  listening... {remaining}s left   futures={seen['futures']} "
              f"index={seen['index']} other={seen['other']}")
        time.sleep(10)

    heading("RESULT")
    show("futures ticks", seen["futures"])
    show("index ticks", seen["index"])
    show("ticks with an unrecognised token", seen["other"])
    for sample in samples:
        print(f"    {sample}")

    print()
    if seen["index"]:
        print("  => The index feed WORKS with the token/segment above.")
    elif seen["other"]:
        print("  => Index data is arriving under a DIFFERENT token than expected.")
        print("     Use the token shown in the samples above.")
    elif working:
        print("  => Websocket index feed is dead, but REST works. The bot should")
        print("     source spot from the REST combination listed in step 2.")
    else:
        print("  => Neither route delivers the index. Send this whole output over;")
        print("     it rules out every option tried and tells us where to look next.")

    print("\nDone. No orders were placed and the database was not touched.")


if __name__ == "__main__":
    main()
