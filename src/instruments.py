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


@dataclass(frozen=True)
class Instrument:
    token: str
    exchange_segment: str
    trading_symbol: str
    lot_size: int
    expiry: dt.date


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


def _search_scrip(session: KotakSession, **kwargs) -> list[dict]:
    response = session.call_with_retry("search_scrip", exchange_segment="nse_fo", **kwargs)
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


def resolve_atm_option(session: KotakSession, underlying_ltp: float, option_type: str) -> Instrument:
    """Resolves the nearest-expiry ATM CE/PE for Nifty index options.

    `option_type` must be "CE" or "PE". ATM strike = underlying_ltp rounded to
    the nearest 50.
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")

    atm_strike = round(underlying_ltp / 50) * 50
    records = _search_scrip(session, symbol="nifty", option_type=option_type, strike_price=str(int(atm_strike)))

    today = dt.date.today()
    candidates = [
        _to_instrument(record) for record in records if _is_exact_nifty(record)
    ]
    candidates = [i for i in candidates if i.expiry >= today]

    if not candidates:
        raise RuntimeError(
            f"No unexpired {option_type} contract found for Nifty strike {atm_strike}. "
            f"Sample record for debugging: {records[0] if records else None}"
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
        for key in ("ltp", "last_traded_price", "lastPrice"):
            if key in item:
                return float(item[key])

    raise RuntimeError(f"Could not extract LTP from quotes() response: {response}")
