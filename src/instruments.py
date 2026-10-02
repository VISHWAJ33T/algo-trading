"""Instrument resolution (Nifty Futures + ATM CE/PE).

Originally this hand-parsed the scrip master CSV with guessed column names,
since Kotak's public docs don't list the full schema. Reading the *installed*
neo_api_client package source (api/scrip_search.py) instead of guessing
showed the SDK already exposes a `search_scrip()` method that does this CSV
download + filtering server-side/in-SDK, using the real (undocumented)
column names: `pSymbolName`, `pOptionType`, `dStrikePrice;` (yes, with a
trailing semicolon in the column name), `pExpiryDate`. We use that instead of
re-implementing CSV parsing ourselves.

Two things confirmed by reading that source, both handled below:
- `search_scrip(symbol="nifty", ...)` matches `pSymbolName` as a
  case-insensitive SUBSTRING, so it also returns BANKNIFTY / FINNIFTY /
  MIDCPNIFTY contracts (all contain "nifty"). Every result is filtered to an
  exact `pSymbolName == "NIFTY"` match here.
- `pOptionType` is only populated for option contracts; futures rows are
  assumed to have it empty/None. This assumption isn't documented anywhere,
  so `resolve_nifty_futures` raises a clear error (with a sample record) if
  it can't find any unexpired futures contract, so a wrong assumption here
  surfaces immediately instead of silently trading the wrong instrument.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass

from src.auth import KotakSession

logger = logging.getLogger(__name__)


# NIFTY 50 spot index, cash segment. Strike selection must be driven by the
# index, not by the futures contract: futures trade at a premium/discount to
# spot (~50 points on 30-Jul-2026), which is enough to pick a strike one step
# away from the true ATM.
# search_scrip() filters with `df["pSymbolName"].str.contains(symbol)` (see the
# SDK's api/scrip_search.py), and in the cash scrip master the index row carries
# pSymbolName="NIFTY" -- the string "Nifty 50" only ever appears in pTrdSymbol.
# Searching for "nifty 50" therefore matched nothing and raised "No data found"
# on every single startup of 03-Aug-2026, silently demoting strike selection to
# the fallback token below. The search term must be the broad "nifty"; the exact
# NIFTY 50 row is then picked out of the results by _is_nifty_50_row().
NIFTY_INDEX_SYMBOL = "nifty"
NIFTY_INDEX_SEGMENT = "nse_cm"
# Kotak's well-known token for the NIFTY 50 index, used only if the scrip
# master lookup comes back empty (see resolve_nifty_index).
NIFTY_INDEX_FALLBACK_TOKEN = "26000"

STRIKE_STEP = 50

# Annualised rate used to discount the strike in the put-call parity spot
# calculation below. The result is barely sensitive to it -- at a week to expiry
# a 2% error in the rate moves the derived spot by about 9 points, well inside
# the 50-point strike step -- so this is a constant rather than another .env
# knob nobody would tune.
RISK_FREE_RATE = 0.065


@dataclass(frozen=True)
class Instrument:
    token: str
    exchange_segment: str
    trading_symbol: str
    lot_size: int
    expiry: dt.date | None = None


def _parse_expiry(value: str) -> dt.date:
    # search_scrip() returns expiry pre-formatted as "DDMonYYYY", e.g. "28AUG2025".
    return dt.datetime.strptime(value, "%d%b%Y").date()


def _is_exact_nifty(record: dict) -> bool:
    return str(record.get("pSymbolName", "")).strip().upper() == "NIFTY"


def _to_instrument(record: dict) -> Instrument:
    return Instrument(
        token=str(record["pSymbol"]),
        exchange_segment=str(record["pExchSeg"]),
        trading_symbol=str(record["pTrdSymbol"]),
        lot_size=int(record["lLotSize"]),
        expiry=_parse_expiry(record["pExpiryDate"]),
    )


def _search_scrip(session: KotakSession, exchange_segment: str = "nse_fo", **kwargs) -> list[dict]:
    response = session.call_with_retry("search_scrip", exchange_segment=exchange_segment, **kwargs)
    if not isinstance(response, list):
        raise RuntimeError(f"Unexpected search_scrip() response: {response}")
    return response


def resolve_nifty_futures(session: KotakSession) -> Instrument:
    """Resolves the current (nearest-expiry, unexpired) Nifty Futures contract."""
    records = _search_scrip(session, symbol="nifty")

    today = dt.date.today()
    candidates = []
    for record in records:
        if not _is_exact_nifty(record):
            continue
        option_type = record.get("pOptionType")
        if option_type not in (None, "", "XX"):
            continue  # this row is an option contract, not a future
        instrument = _to_instrument(record)
        if instrument.expiry >= today:
            candidates.append(instrument)

    if not candidates:
        raise RuntimeError(
            "No unexpired Nifty Futures contract found via search_scrip(). "
            "The pOptionType-based futures/options split may not hold for this "
            f"data -- sample record for debugging: {records[0] if records else None}"
        )
    return min(candidates, key=lambda i: i.expiry)


def _row_names(record: dict) -> list[str]:
    """The readable names on a scrip-master row, normalised. Both fields are
    read because the scrip master is not consistent about which one carries the
    readable name, and spaces/hyphens are stripped so "NIFTY 50", "Nifty 50"
    and "NIFTY-50" all compare equal."""
    names = []
    for field in ("pTrdSymbol", "pSymbolName"):
        name = str(record.get(field) or "").strip().upper()
        if name:
            names.append(name.replace(" ", "").replace("-", ""))
    return names


def _is_nifty_50_row(record: dict) -> bool:
    """True for the NIFTY 50 index row itself, and not for the many other cash
    instruments whose name contains "nifty" (NIFTYBEES, NIFTYQLITY, ...).

    Bare "NIFTY" counts. The 04-Aug-2026 log settled what this row actually
    looks like -- the diagnostic added for exactly this purpose printed
    ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'HDFCNIFTY-BL', ...], so in
    the nse_cm scrip master the index is simply "NIFTY" and the string "NIFTY 50"
    never appears at all. Matching only "NIFTY50" therefore still fell through to
    the hardcoded token on every startup. In the cash segment "NIFTY" is
    unambiguous (the futures and options contracts live in nse_fo), so it is a
    safe exact match -- and it stays an *exact* one, which is what keeps
    NIFTYBEES and friends out.
    """
    return any(name in ("NIFTY50", "NIFTY") for name in _row_names(record))


def resolve_nifty_index(session: KotakSession) -> Instrument:
    """Resolves the NIFTY 50 spot index in the cash segment.

    Cash-segment rows have no expiry or lot size, so only token/segment/symbol
    are meaningful here. If the scrip master lookup returns nothing usable we
    fall back to Kotak's well-known index token rather than failing the whole
    startup, since the bot can still trade -- it just needs *a* spot feed.
    """
    try:
        records = _search_scrip(session, exchange_segment=NIFTY_INDEX_SEGMENT, symbol=NIFTY_INDEX_SYMBOL)
    except Exception:
        logger.exception("search_scrip() for the NIFTY 50 index failed; falling back to token %s",
                         NIFTY_INDEX_FALLBACK_TOKEN)
        records = []

    # Two passes so an explicit "NIFTY 50" row always wins over a bare "NIFTY"
    # one, in case a future scrip master carries both.
    for wanted in ("NIFTY50", "NIFTY"):
        for record in records:
            if wanted not in _row_names(record):
                continue
            instrument = Instrument(
                token=str(record["pSymbol"]),
                exchange_segment=str(record.get("pExchSeg") or NIFTY_INDEX_SEGMENT),
                trading_symbol="NIFTY 50",
                lot_size=0,
            )
            # The *resolved* segment is logged, not the one searched. Both the
            # websocket subscription and the REST quote are built from
            # "<segment>|<token>", so a wrong pExchSeg breaks both at once --
            # which is exactly the 14-Aug-2026 symptom (no index tick ever, and
            # HTTP 400 "Invalid neosymbol values"). The old message printed the
            # segment we searched rather than the one we ended up using, so the
            # log could not distinguish those cases.
            logger.info(
                "Resolved NIFTY 50 index: token=%s exchange_segment=%s (matched %r; "
                "quotes/websocket will address it as %s|%s)",
                instrument.token, instrument.exchange_segment, wanted,
                instrument.exchange_segment, instrument.token,
            )
            return instrument

    # Logged at WARNING with the candidate names, so if the fallback is ever
    # taken again the next log says exactly what the scrip master did return
    # instead of just that the match failed.
    logger.warning(
        "No exact NIFTY 50 row in the %s scrip master (%d rows searched); using fallback token %s. "
        "Sample of what was returned: %s",
        NIFTY_INDEX_SEGMENT, len(records), NIFTY_INDEX_FALLBACK_TOKEN,
        [str(r.get("pTrdSymbol") or r.get("pSymbolName")) for r in records[:10]],
    )
    return Instrument(
        token=NIFTY_INDEX_FALLBACK_TOKEN,
        exchange_segment=NIFTY_INDEX_SEGMENT,
        trading_symbol="NIFTY 50",
        lot_size=0,
    )


def atm_strike(spot: float) -> int:
    """ATM strike = NIFTY 50 *spot* rounded to the nearest 50."""
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def resolve_atm_option(session: KotakSession, underlying_ltp: float, option_type: str) -> Instrument:
    """Resolves the nearest-expiry ATM CE/PE for Nifty index options.

    `option_type` must be "CE" or "PE". `underlying_ltp` must be the NIFTY 50
    *index* level, not the futures price -- futures carry a premium over spot
    (24315 vs 24264.05 on 30-Jul-2026) which is enough to select the wrong
    strike, so main.py sources this from the index feed.
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")

    atm_strike_price = atm_strike(underlying_ltp)
    records = _search_scrip(session, symbol="nifty", option_type=option_type, strike_price=str(atm_strike_price))

    today = dt.date.today()
    candidates = [
        _to_instrument(record) for record in records if _is_exact_nifty(record)
    ]
    candidates = [i for i in candidates if i.expiry >= today]

    if not candidates:
        raise RuntimeError(
            f"No unexpired {option_type} contract found for Nifty strike {atm_strike_price}. "
            f"Sample record for debugging: {records[0] if records else None}"
        )
    logger.info(
        "ATM %s for NIFTY 50 spot %.2f -> strike %d", option_type, underlying_ltp, atm_strike_price
    )
    return min(candidates, key=lambda i: i.expiry)


def spot_from_put_call_parity(session: KotakSession, reference_price: float) -> float | None:
    """Derives the NIFTY 50 spot level from option premiums, or None if it
    cannot be computed.

    Why this exists. The strategy requires the ATM strike to come from the spot
    index rather than from Nifty futures -- futures trade ~50-90 points above
    spot, which is one to two strike steps, and picking the strike off futures
    is the original complaint that started this whole thread (24600 CE bought
    while Nifty was at 24530). But on this account neither route to the index
    works: the websocket index subscription has never delivered a single tick,
    and the REST quote rejects the index outright with HTTP 400 "Invalid
    neosymbol values". Both symptoms are consistent with the account simply not
    being entitled to index market data, which is not something code can fix.

    Option quotes, by contrast, work reliably on this account -- every entry the
    bot has ever taken priced its option through `fetch_ltp`. Put-call parity
    turns those into spot:

        C - P = S - K * e^(-rT)   =>   S = C - P + K * e^(-rT)

    The relation holds at *any* strike, so `reference_price` (the futures price)
    is only used to choose which strike to quote, never as the answer -- the
    futures premium cancels out entirely. Both legs must be the same strike and
    the same expiry, which is checked below.

    Costs two REST quotes, and is only called when the direct index sources have
    failed.
    """
    try:
        call = resolve_atm_option(session, reference_price, "CE")
        put = resolve_atm_option(session, reference_price, "PE")
    except Exception:
        logger.exception("Could not resolve the option pair needed to derive spot")
        return None

    if call.expiry != put.expiry:
        logger.warning(
            "Skipping put-call parity: CE expiry %s != PE expiry %s, so the pair is not comparable",
            call.expiry, put.expiry,
        )
        return None

    strike = atm_strike(reference_price)
    try:
        call_price = fetch_ltp(session, call)
        put_price = fetch_ltp(session, put)
    except Exception:
        logger.exception("Could not quote the option pair needed to derive spot")
        return None

    years_to_expiry = 0.0
    if call.expiry is not None:
        years_to_expiry = max(0, (call.expiry - dt.date.today()).days) / 365.0
    discounted_strike = strike * math.exp(-RISK_FREE_RATE * years_to_expiry)

    spot = call_price - put_price + discounted_strike
    logger.info(
        "Derived NIFTY 50 spot %.2f from put-call parity at strike %d "
        "(CE %.2f, PE %.2f, expiry %s, futures reference %.2f)",
        spot, strike, call_price, put_price, call.expiry, reference_price,
    )
    return spot


def fetch_ltp(session: KotakSession, instrument: Instrument) -> float:
    """Fetches the current LTP for `instrument` via quotes(). The raw
    response is logged at DEBUG on every call for early sanity-checking
    against what Kotak actually returns."""
    response = session.call_with_retry(
        "quotes",
        instrument_tokens=[
            {"instrument_token": instrument.token, "exchange_segment": instrument.exchange_segment}
        ],
        quote_type="ltp",
    )
    logger.debug("quotes() raw response for %s: %s", instrument.trading_symbol, response)

    data = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(data, list) and data:
        item = data[0]
        # "iv"/"index_value" cover the cash-segment index quote, which reports
        # an index level rather than a last traded price.
        for key in ("ltp", "last_traded_price", "lastPrice", "iv", "index_value"):
            if key in item:
                return float(item[key])

    # The neosymbol is included because that string -- not the Python arguments
    # -- is what Kotak validates, and "Invalid neosymbol values" says nothing
    # about which part it objected to.
    raise RuntimeError(
        f"Could not extract LTP from quotes() response for neosymbol "
        f"{instrument.exchange_segment}|{instrument.token} "
        f"({instrument.trading_symbol}): {response}"
    )
