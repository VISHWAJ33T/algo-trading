"""Live tick feed: wraps client.subscribe()/on_message and feeds ticks into
5-min and 30-min TickAggregators.

Message shape confirmed by reading the installed neo_api_client package
source (NeoWebSocket.py) rather than guessing:

- `on_message` receives `{"type": "stock_feed", "data": [tick, tick, ...]}`
  for live subscribed ticks (also "quotes" for one-off REST-triggered
  snapshots, and "order_feed" for order updates -- both ignored here).
- Each tick dict in `data` uses Kotak's short wire codes, NOT the
  human-readable names `quotes()` returns: `tk`=instrument_token,
  `e`=exchange_segment, `ltp`=last traded price, `v`=cumulative volume
  traded so far today, `ltq`=last traded quantity, `ltt`=last traded time.
  A given message may only carry a subset of these fields.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Callable

from src.auth import KotakSession
from src.candles import Candle, TickAggregator, in_session
from src.instruments import Instrument

logger = logging.getLogger(__name__)

# Index subscriptions report the index level as "iv" rather than "ltp" (see
# INDEX_MAPPING in the SDK's HSWebSocketLib), so both keys are accepted.
# How long to wait for the SDK's socket to finish its connect handshake before
# putting a second subscription on it. Generous because the cost of giving up
# early is a competing socket (see LiveFeed._subscribe_all), while the cost of
# waiting is a few seconds of startup.
SOCKET_READY_TIMEOUT_SECONDS = 20.0
SOCKET_READY_POLL_SECONDS = 0.25

_LTP_KEYS = ("ltp", "iv")
_TOKEN_KEYS = ("tk",)
_TIMESTAMP_KEYS = ("ltt",)
_CUMULATIVE_VOLUME_KEYS = ("v",)
_TRADE_QUANTITY_KEYS = ("ltq",)


@dataclass
class Tick:
    timestamp: dt.datetime
    ltp: float
    cumulative_volume: float | None
    trade_quantity: float | None


def _first(tick: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in tick and tick[key] not in (None, ""):
            return tick[key]
    return None


def _parse_timestamp(raw) -> dt.datetime:
    if raw is None:
        return dt.datetime.now(dt.timezone.utc)
    try:
        value = float(raw)
        # Heuristic: treat values > 10^12 as milliseconds since epoch.
        if value > 1e12:
            value /= 1000
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    except (TypeError, ValueError):
        return dt.datetime.now(dt.timezone.utc)


def _parse_tick(tick: dict) -> Tick | None:
    ltp_raw = _first(tick, _LTP_KEYS)
    if ltp_raw is None:
        return None
    return Tick(
        timestamp=_parse_timestamp(_first(tick, _TIMESTAMP_KEYS)),
        ltp=float(ltp_raw),
        cumulative_volume=_to_float_or_none(_first(tick, _CUMULATIVE_VOLUME_KEYS)),
        trade_quantity=_to_float_or_none(_first(tick, _TRADE_QUANTITY_KEYS)),
    )


def _to_float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class LiveFeed:
    """Candles are built from `instrument` (Nifty futures, which is what has a
    tradeable volume series and therefore usable VWAP/Force Index). The spot
    index is subscribed alongside it purely to keep `latest_index_ltp` current
    for ATM strike selection -- it never feeds the candle aggregators."""

    def __init__(self, session: KotakSession, instrument: Instrument, index_instrument: Instrument | None = None):
        self.session = session
        self.instrument = instrument
        self.index_instrument = index_instrument
        self.agg_5m = TickAggregator(5)
        self.agg_30m = TickAggregator(30)
        self.latest_ltp: float | None = None
        self.latest_index_ltp: float | None = None

        self.on_candle_close_5m: Callable[[Candle], None] | None = None
        self.on_candle_close_30m: Callable[[Candle], None] | None = None

        self._last_cumulative_volume: float | None = None
        self._stopped = False
        self._started = False
        self._subscribed_at: float | None = None
        # Written from SDK callback threads, read from the main loop. A plain
        # float assignment is atomic, so no lock is needed.
        self._last_tick_at: float | None = None
        self._last_index_tick_at: float | None = None

    def start(self) -> None:
        """Subscribes once. Deliberately NOT called again on disconnect -- see
        `_on_close` for why."""
        if self._started:
            logger.debug("LiveFeed.start() called again; ignoring (already subscribed)")
            return
        self._started = True
        self._subscribe_all()

    def _subscribe_all(self) -> None:
        self._subscribed_at = time.monotonic()
        client = self.session.client or self.session.login()
        client.on_message = self._on_message
        client.on_error = self._on_error
        client.on_close = self._on_close

        # Index and futures need different subscription types (INDEX_SUBS vs
        # SCRIP_SUBS in the SDK), so they cannot be combined into one call.
        #
        # They MUST, however, go onto the same socket, and that is why the
        # second subscribe waits. The SDK decides in get_live_feed() whether to
        # reuse the live socket or open a new one by testing `is_hsw_open == 1`
        # -- a flag that is only set when the server's connect acknowledgement
        # arrives, asynchronously. Issuing both calls back to back means the
        # flag is still 0 for the second one, so the SDK opens a *second*
        # socket. That matters because HSWebSocketLib.hs_send() transmits over a
        # module-level global `ws` which each new StartServer rebinds: the two
        # sockets then share one global, each one's on_open sends its connect
        # frame over whichever socket bound the global last, and the loser gets
        # "Connection is already closed". Its handshake never completes,
        # `is_hsw_open` never reaches 1, and the SDK's own resubscribe-on-
        # reconnect path is therefore never reached.
        #
        # That is what killed 04-Aug-2026: the feed delivered one tick at 09:07
        # and then nothing for the remaining 6.4 hours, reconnecting every ~130s
        # without ever restoring data. Both 03-Aug and 04-Aug logs show the
        # tell-tale pair of "Websocket connected" lines at every startup.
        self._subscribe(client, self.instrument, is_index=False, label="live feed")

        if self.index_instrument is not None:
            if not self._wait_until_socket_live(client):
                logger.warning(
                    "Live socket did not report ready within %.0fs; subscribing the spot index "
                    "anyway. If a second 'Websocket connected' appears, the index and futures "
                    "legs have landed on competing sockets.", SOCKET_READY_TIMEOUT_SECONDS,
                )
            self._subscribe(client, self.index_instrument, is_index=True, label="spot index feed")

    @staticmethod
    def _socket_is_live(client) -> bool:
        """True once the SDK's websocket has completed its connect handshake.

        Reaches into SDK internals because nothing is exposed publicly for it;
        guarded so a change in the SDK degrades to "not ready" rather than
        raising out of the feed.
        """
        try:
            return getattr(client.NeoWebSocket, "is_hsw_open", 0) == 1
        except Exception:
            return False

    def _wait_until_socket_live(self, client) -> bool:
        deadline = time.monotonic() + SOCKET_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._socket_is_live(client):
                return True
            time.sleep(SOCKET_READY_POLL_SECONDS)
        return self._socket_is_live(client)

    def _subscribe(self, client, instrument: Instrument, is_index: bool, label: str) -> None:
        try:
            client.subscribe(
                instrument_tokens=[
                    {
                        "instrument_token": instrument.token,
                        "exchange_segment": instrument.exchange_segment,
                    }
                ],
                isIndex=is_index,
            )
        except Exception:
            logger.exception("Failed to subscribe to the %s for %s", label, instrument.trading_symbol)
            return
        logger.info("Subscribed to %s for %s", label, instrument.trading_symbol)

    def restart(self) -> bool:
        """Tears the websocket down and subscribes again from scratch.

        This is the recovery path for a feed that has gone silent, and it is
        deliberately shaped to be the opposite of the 03-Aug-2026 failure:

        - It is only ever called from the main loop, never from a websocket
          callback thread, so reconnects cannot fan out in parallel.
        - It closes the existing socket and discards the SDK's NeoWebSocket
          before re-subscribing, so a new socket replaces the old one instead of
          accumulating alongside it.
        - The caller rate-limits how often this may run.

        Returns whether the resubscribe was attempted.
        """
        client = self.session.client
        if client is None or self._stopped:
            return False

        logger.warning("Rebuilding the live feed connection after a stall")
        try:
            socket = getattr(client.NeoWebSocket, "hsWebsocket", None)
            if socket is not None:
                socket.close()
        except Exception:
            # Already-dead sockets raise here routinely; the point of the call
            # is only to make sure we are not leaving a live one behind.
            logger.debug("Closing the old websocket raised (it was likely already dead)", exc_info=True)

        try:
            # Dropping NeoWebSocket is what makes the next subscribe() build a
            # fresh connection with an empty subscription list, rather than
            # replaying onto the socket that just failed.
            client.NeoWebSocket = None
        except Exception:
            logger.exception("Could not reset the SDK websocket; skipping this feed rebuild")
            return False

        self._subscribe_all()
        return True

    def seconds_since_start(self) -> float | None:
        """Time since the subscriptions were last (re)issued. Lets the main loop
        measure a feed that has never delivered a single tick, where
        `seconds_since_last_tick()` has nothing to report."""
        if self._subscribed_at is None:
            return None
        return time.monotonic() - self._subscribed_at

    def seconds_since_last_tick(self) -> float | None:
        """Age of the most recent tick of any kind, or None if none has ever
        arrived. Used by the main loop to notice a silently dead feed."""
        if self._last_tick_at is None:
            return None
        return time.monotonic() - self._last_tick_at

    def seconds_since_last_index_tick(self) -> float | None:
        if self._last_index_tick_at is None:
            return None
        return time.monotonic() - self._last_index_tick_at

    def stop(self) -> None:
        """Stops consuming ticks and suppresses the reconnect loop. Without
        this the feed kept reconnecting and aggregating for hours after the
        close (30-Jul-2026 ran to 19:40 IST)."""
        self._stopped = True
        client = self.session.client
        if client is None:
            return
        tokens = [{"instrument_token": self.instrument.token, "exchange_segment": self.instrument.exchange_segment}]
        try:
            client.un_subscribe(instrument_tokens=tokens, isIndex=False)
            if self.index_instrument is not None:
                client.un_subscribe(
                    instrument_tokens=[
                        {
                            "instrument_token": self.index_instrument.token,
                            "exchange_segment": self.index_instrument.exchange_segment,
                        }
                    ],
                    isIndex=True,
                )
        except Exception:
            logger.exception("Failed to unsubscribe cleanly from the live feed")
        logger.info("Live feed stopped")

    def _on_message(self, message) -> None:
        logger.debug("Raw websocket message: %s", message)
        if self._stopped:
            return
        if not isinstance(message, dict) or message.get("type") != "stock_feed":
            return

        ticks = message.get("data")
        if not isinstance(ticks, list):
            return

        for raw_tick in ticks:
            if not isinstance(raw_tick, dict):
                continue
            self._handle_tick(raw_tick)

    def _handle_tick(self, raw_tick: dict) -> None:
        try:
            tick = _parse_tick(raw_tick)
        except Exception:
            logger.exception("Failed to parse tick: %s", raw_tick)
            return
        if tick is None:
            # A tick carrying no price at all. Logged (and collapsed by the
            # duplicate suppressor) rather than dropped in silence, because an
            # index tick whose level arrives under an unexpected key would look
            # exactly like "the index feed is dead" from the outside -- which is
            # one of the candidates for the 14-Aug-2026 failure.
            logger.debug("Tick carried no recognised price field: %s", raw_tick)
            return

        self._last_tick_at = time.monotonic()

        if self._is_index_tick(raw_tick):
            self.latest_index_ltp = tick.ltp
            self._last_index_tick_at = time.monotonic()
            return

        self._warn_if_unrecognised(raw_tick)
        self.latest_ltp = tick.ltp

        # Post-close ticks (including the snapshot each reconnect replays) must
        # not build candles -- see candles.in_session.
        if not in_session(tick.timestamp):
            return

        volume_delta = 0.0
        if tick.cumulative_volume is not None:
            if self._last_cumulative_volume is not None:
                volume_delta = max(0.0, tick.cumulative_volume - self._last_cumulative_volume)
            self._last_cumulative_volume = tick.cumulative_volume
        elif tick.trade_quantity is not None:
            volume_delta = tick.trade_quantity

        closed_5m = self.agg_5m.on_tick(tick.timestamp, tick.ltp, volume_delta)
        if closed_5m is not None and self.on_candle_close_5m is not None:
            self.on_candle_close_5m(closed_5m)

        closed_30m = self.agg_30m.on_tick(tick.timestamp, tick.ltp, volume_delta)
        if closed_30m is not None and self.on_candle_close_30m is not None:
            self.on_candle_close_30m(closed_30m)

    def _warn_if_unrecognised(self, raw_tick: dict) -> None:
        """Flags a tick whose token is neither the futures nor the index one.

        Such a tick is still treated as the futures instrument (ticks may carry
        no token at all, and futures is the only other subscription), but if the
        index level were ever to arrive under an unexpected token this is what
        makes it visible instead of it quietly feeding the candle series.
        """
        token = _first(raw_tick, _TOKEN_KEYS)
        if token is None or str(token) == str(self.instrument.token):
            return
        logger.warning(
            "Tick for unrecognised token %s (expected futures %s or index %s); "
            "treating it as the futures instrument",
            token, self.instrument.token,
            self.index_instrument.token if self.index_instrument else None,
        )

    def _is_index_tick(self, raw_tick: dict) -> bool:
        if self.index_instrument is None:
            return False
        token = _first(raw_tick, _TOKEN_KEYS)
        return token is not None and str(token) == str(self.index_instrument.token)

    def _on_error(self, error) -> None:
        logger.error("Websocket error: %s", error)

    def _on_close(self, *args) -> None:
        """Reports the disconnect and nothing else. Reconnecting here is what
        produced the 03-Aug-2026 failure, and it was never necessary:

        - The SDK's socket already runs under
          `run_forever(ping_interval=0, reconnect=5)`, so websocket-client
          retries the connection itself every 5 seconds, indefinitely.
        - On each successful reconnect the SDK re-sends every subscription by
          itself (`NeoWebSocket.on_hsm_message` replays `sub_list` when the
          connection acknowledgement arrives), so subscriptions survive without
          our help.

        Calling `start()` from here therefore added nothing -- but because the
        SDK sets `is_hsw_open = 0` before invoking this callback,
        `client.subscribe()` took its "no live socket" branch and spawned a
        *whole new* HSWebSocket thread instead of reusing the existing one. The
        old connection stayed alive, its own `on_close` fired later, and each
        close spawned two more connections (one per subscribe call). That
        branching factor of two turned a linear leak (236 reconnects on
        30-Jul-2026, when start() made a single subscribe call) into an
        exponential one: 101,104 sockets, 307,506 reconnects and an 87 MB log
        on 03-Aug-2026, ending in `can't start new thread` and a dead process
        three times in one session.

        The main loop watches `seconds_since_last_tick()` to catch the case
        this no longer covers -- a socket that reconnects but never delivers.
        """
        if self._stopped:
            logger.info("Websocket closed after stop(); not reconnecting")
            return
        logger.warning("Websocket closed; the SDK will reconnect and resubscribe on its own")
