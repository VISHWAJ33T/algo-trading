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
from dataclasses import dataclass

from src.auth import KotakSession

logger = logging.getLogger(__name__)


# NIFTY 50 spot index, cash segment. Strike selection must be driven by the
# index, not by the futures contract: futures trade at a premium/discount to
# spot (~50 points on 30-Jul-2026), which is enough to pick a strike one step
# away from the true ATM.
NIFTY_INDEX_SYMBOL = "nifty 50"
NIFTY_INDEX_SEGMENT = "nse_cm"
# Kotak's well-known token for the NIFTY 50 index, used only if the scrip
# master lookup comes back empty (see resolve_nifty_index).
NIFTY_INDEX_FALLBACK_TOKEN = "26000"

STRIKE_STEP = 50


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

    for record in records:
        name = str(record.get("pTrdSymbol") or record.get("pSymbolName") or "").strip().upper()
        if name.replace(" ", "") == "NIFTY50":
            return Instrument(
                token=str(record["pSymbol"]),
                exchange_segment=str(record.get("pExchSeg", NIFTY_INDEX_SEGMENT)),
                trading_symbol=name,
                lot_size=0,
            )

    logger.warning(
        "No exact NIFTY 50 row in the %s scrip master (%d rows searched); using fallback token %s",
        NIFTY_INDEX_SEGMENT, len(records), NIFTY_INDEX_FALLBACK_TOKEN,
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

    raise RuntimeError(f"Could not extract LTP from quotes() response: {response}")
