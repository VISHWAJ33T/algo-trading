"""Kotak Neo login/session management.

Kotak doesn't document session/token lifetime, so the approach here is
defensive: log in once at process start, and wrap every API call through
`call_with_retry` so that if a call fails in a way that looks like an
expired/invalid session, we re-login once and retry before giving up.
"""
from __future__ import annotations

import logging
import time

import pyotp
from neo_api_client import NeoAPI

from src.config import Config

logger = logging.getLogger(__name__)

# Substrings seen in Kotak error payloads (`emsg`/exception text) that indicate
# the session/token is no longer valid and a fresh login is needed.
_AUTH_ERROR_MARKERS = ("unauthorized", "invalid session", "invalid token", "401")


class KotakSession:
    def __init__(self, config: Config):
        self._config = config
        self.client: NeoAPI | None = None

    def login(self) -> NeoAPI:
        """Fresh TOTP login. Safe to call again to re-authenticate.

        Both totp_login() and totp_validate() fail "quietly" -- on bad
        credentials they return an error dict instead of raising, so each
        response is checked explicitly here. Without this check, a bad
        login silently proceeds and only surfaces several steps later as a
        confusing "Complete the 2fa process" error from some unrelated call.
        """
        config = self._config
        client = NeoAPI(environment=config.environment, consumer_key=config.consumer_key)

        totp_code = pyotp.TOTP(config.totp_secret).now()
        login_response = client.totp_login(
            mobile_number=config.mobile_number,
            ucc=config.ucc,
            totp=totp_code,
        )
        _raise_if_error("totp_login", login_response)

        validate_response = client.totp_validate(mpin=config.mpin)
        _raise_if_error("totp_validate", validate_response)

        logger.info("Logged in to Kotak Neo as UCC=%s", config.ucc)
        self.client = client
        return client

    def call_with_retry(self, fn_name: str, /, *args, retries: int = 1, **kwargs):
        """Call `self.client.<fn_name>(*args, **kwargs)`, re-logging in and
        retrying once if the response/exception looks auth-related."""
        if self.client is None:
            self.login()

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                method = getattr(self.client, fn_name)
                result = method(*args, **kwargs)
                if _looks_like_auth_error(result):
                    raise RuntimeError(f"Auth-like error response from {fn_name}: {result}")
                return result
            except Exception as exc:  # noqa: BLE001 -- SDK exception types aren't documented
                last_error = exc
                if attempt >= retries or not _is_auth_error(exc):
                    raise
                logger.warning(
                    "Call to %s failed (%s); re-authenticating and retrying (attempt %d/%d)",
                    fn_name, exc, attempt + 1, retries,
                )
                time.sleep(1)
                self.login()

        raise last_error  # pragma: no cover -- unreachable, satisfies type checkers


def _is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _looks_like_auth_error(result) -> bool:
    if isinstance(result, dict):
        stat = str(result.get("stat", "")).lower()
        emsg = str(result.get("emsg", "")).lower()
        if stat == "not_ok" and any(marker in emsg for marker in _AUTH_ERROR_MARKERS):
            return True
    return False


def _raise_if_error(step: str, response) -> None:
    """totp_login()/totp_validate() return an error dict (never raise) on
    bad credentials -- e.g. {'error': [{'message': 'Any of Mobile Number,
    UCC or totp is missing'}]} or {'Error Message': '...'}. Treat presence
    of any of these keys as failure."""
    if isinstance(response, dict) and any(key in response for key in ("error", "Error", "Error Message")):
        raise RuntimeError(f"Kotak login step '{step}' failed: {response}")
