"""Rotating file + console logging setup, with two safety nets against a
runaway log.

On 03-Aug-2026 a websocket reconnect loop wrote 917,890 lines (87 MB) into a
single day's file -- 900x a normal session. Two things let that happen: the
handler rotated only at midnight with no size ceiling, and nothing collapsed
the handful of message templates that made up 99% of the volume. Both are
addressed here, so any future runaway loop is capped and legible rather than
producing a file too large to open.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Ceiling for a single log file before it is rotated early. A normal session is
# well under 1 MB, so this only ever trips on a malfunction.
MAX_LOG_BYTES = 20 * 1024 * 1024
BACKUP_COUNT = 30

# Identical messages allowed through per window before the rest are counted and
# dropped. Sized so a genuine burst (a reconnect flurry at the open) is still
# visible in full, while a sustained loop is reduced to one line a minute.
DUPLICATES_PER_WINDOW = 20
DUPLICATE_WINDOW_SECONDS = 60.0
# Cap on distinct message templates tracked at once, so the suppressor itself
# cannot become the leak.
_MAX_TRACKED_KEYS = 2048


@dataclass
class _Window:
    start: float
    emitted: int = 0
    suppressed: int = 0


class DuplicateSuppressor(logging.Filter):
    """Collapses floods of identical records into a periodic summary line.

    Records are keyed on (logger, level, *unformatted* message template), so
    "Websocket closed; reconnecting in %ds" collapses across all its arguments
    rather than being treated as a new message each time. The first
    `max_per_window` records in a window pass through untouched; the rest are
    counted, and the count is appended to the first record of the next window
    so the suppression is always visible in the log itself.
    """

    def __init__(
        self,
        max_per_window: int = DUPLICATES_PER_WINDOW,
        window_seconds: float = DUPLICATE_WINDOW_SECONDS,
    ):
        super().__init__()
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        # Websocket callbacks fire from many SDK threads at once.
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, int, str], _Window] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.levelno, str(record.msg))
        now = time.monotonic()

        with self._lock:
            window = self._windows.get(key)

            if window is not None and now - window.start < self.window_seconds:
                window.emitted += 1
                if window.emitted <= self.max_per_window:
                    return True
                window.suppressed += 1
                return False

            carried = window.suppressed if window is not None else 0
            self._windows[key] = _Window(start=now, emitted=1)
            if len(self._windows) > _MAX_TRACKED_KEYS:
                self._evict_expired(now)

        if carried and isinstance(record.msg, str):
            # Safe to append to the template: the added text has no % markers,
            # so record.args still formats correctly.
            record.msg = (
                f"{record.msg} [+{carried} identical messages suppressed in the "
                f"previous {self.window_seconds:.0f}s]"
            )
        return True

    def _evict_expired(self, now: float) -> None:
        """Caller must hold the lock."""
        cutoff = now - self.window_seconds
        for key in [k for k, w in self._windows.items() if w.start < cutoff]:
            del self._windows[key]


class TimedSizedRotatingFileHandler(TimedRotatingFileHandler):
    """Rotates at midnight *or* once the file passes `max_bytes`.

    The stdlib offers one or the other, not both. Daily files are what the
    user is asked to send back for debugging, so the timed rotation is kept
    as the primary scheme and the size ceiling is layered on top as a
    circuit breaker.
    """

    def __init__(self, *args, max_bytes: int = MAX_LOG_BYTES, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_bytes = max_bytes

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if super().shouldRollover(record):
            return 1
        if self.max_bytes <= 0:
            return 0
        if self.stream is None:
            self.stream = self._open()
        message = self.format(record) + self.terminator
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() + len(message.encode(self.encoding or "utf-8")) >= self.max_bytes:
            return 1
        return 0

    def rotation_filename(self, default_name: str) -> str:
        """A size rollover happens partway through a day, so the timed archive
        name is already taken -- the base implementation would delete it and
        lose the earlier half. Number the extra files instead."""
        name = super().rotation_filename(default_name)
        if not os.path.exists(name):
            return name
        for suffix in range(1, 1000):
            candidate = f"{name}.{suffix}"
            if not os.path.exists(candidate):
                return candidate
        return name


def setup_logging(level: str = "INFO") -> None:
    LOG_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = TimedSizedRotatingFileHandler(
        LOG_DIR / "bot.log", when="midnight", backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    # The filter goes on the handler, not on the root logger: a logger's own
    # filters are only consulted for records logged through that logger, and
    # records from child loggers (src.ws_feed, websocket, ...) reach the root's
    # *handlers* without ever being offered to the root logger's filters. The
    # file is also the only sink that can fill a disk, so the console keeps
    # showing everything for anyone running the bot interactively.
    file_handler.addFilter(DuplicateSuppressor())

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [file_handler, console_handler]
