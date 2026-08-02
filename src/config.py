"""Loads runtime configuration from environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _optional_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    return float(value)


@dataclass(frozen=True)
class Config:
    consumer_key: str
    environment: str
    mobile_number: str
    ucc: str
    mpin: str
    totp_secret: str

    live_trading: bool
    lot_size: int
    max_daily_loss: float | None

    log_level: str


_REQUIRED_ENV_VARS = (
    "KOTAK_CONSUMER_KEY",
    "KOTAK_MOBILE_NUMBER",
    "KOTAK_UCC",
    "KOTAK_MPIN",
    "KOTAK_TOTP_SECRET",
)


def load_config() -> Config:
    missing = [key for key in _REQUIRED_ENV_VARS if not os.environ.get(key, "").strip()]
    if missing:
        raise RuntimeError(
            f"Missing or blank required setting(s) in .env: {', '.join(missing)}. "
            "Copy .env.example to .env (if you haven't) and fill these in before running."
        )

    return Config(
        consumer_key=os.environ["KOTAK_CONSUMER_KEY"],
        environment=os.environ.get("KOTAK_ENVIRONMENT", "prod"),
        mobile_number=os.environ["KOTAK_MOBILE_NUMBER"],
        ucc=os.environ["KOTAK_UCC"],
        mpin=os.environ["KOTAK_MPIN"],
        totp_secret=os.environ["KOTAK_TOTP_SECRET"],
        live_trading=_bool(os.environ.get("LIVE_TRADING"), default=False),
        lot_size=int(os.environ.get("LOT_SIZE", "1")),
        max_daily_loss=_optional_float(os.environ.get("MAX_DAILY_LOSS")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
