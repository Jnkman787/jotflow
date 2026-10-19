"""Whether JotFlow is still connected to Slack, for operators and alerting (#133).

The Socket Mode client reconnects on its own and says nothing JotFlow logs. So
:class:`ConnectionWatch` checks the connection every few seconds, logs each
change in three plain lines, and, only while connected, writes a heartbeat
file. ``jotflow health`` reads that file's age, so a stale heartbeat means
JotFlow is down or cut off from Slack. The lines and the file carry no tokens,
ids, or message content: only times and a count of checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

log = logging.getLogger("jotflow.health")

HEARTBEAT_ENV = "JOTFLOW_HEARTBEAT_FILE"
DEFAULT_HEARTBEAT = "jotflow-heartbeat"
CHECK_SECONDS = 5.0
BEAT_SECONDS = 30.0
MAX_AGE_SECONDS = 90.0

Clock = Callable[[], float]


def heartbeat_path(environ: Mapping[str, str] | None = None) -> Path:
    """``JOTFLOW_HEARTBEAT_FILE``, or ``jotflow-heartbeat`` in the temp directory (``/tmp``)."""
    env = os.environ if environ is None else environ
    configured = env.get(HEARTBEAT_ENV, "").strip()
    return Path(configured) if configured else Path(tempfile.gettempdir()) / DEFAULT_HEARTBEAT


class Watched(Protocol):
    async def is_connected(self) -> bool: ...


class Heartbeat:
    """The file ``jotflow health`` reads: the time of the last beat, nothing else."""

    def __init__(self, path: Path, clock: Clock = time.time) -> None:
        self.path = path
        self._clock = clock

    def beat(self) -> None:
        """Write the time, atomically, so a reader never sees half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".heartbeat-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(f"{self._clock():.0f}\n")
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise


def heartbeat_age(path: Path, now: float) -> float | None:
    """Seconds since the last beat, or None if there is no readable heartbeat."""
    try:
        text = path.read_text(encoding="ascii").strip()
        beat = float(text)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return max(0.0, now - beat)


class ConnectionWatch:
    """Watches the Slack connection: logs changes, and beats only while connected."""

    def __init__(
        self,
        connection: Watched,
        heartbeat: Heartbeat,
        *,
        clock: Clock = time.monotonic,
        check_seconds: float = CHECK_SECONDS,
        beat_seconds: float = BEAT_SECONDS,
    ) -> None:
        self._connection = connection
        self._heartbeat = heartbeat
        self._clock = clock
        self._check = check_seconds
        self._beat_every = beat_seconds
        self._connected = True  # serve starts watching once connect_async returns
        self._down_since = 0.0
        self._attempt = 0
        self._last_note = 0.0
        self._last_beat: float | None = None

    async def check(self) -> None:
        """One look at the connection. ``run`` calls this every few seconds."""
        try:
            connected = await self._connection.is_connected()
        except Exception:
            connected = False
        now = self._clock()
        if connected:
            if not self._connected:
                log.info("socket mode reconnected after %ds", round(now - self._down_since))
                self._connected = True
                self._last_beat = None  # beat at once
            if self._last_beat is None or now - self._last_beat >= self._beat_every:
                self._beat(now)
            return
        if self._connected:
            log.info("socket mode disconnected")
            self._connected = False
            self._down_since = now
            self._attempt = 0
            self._last_note = now - self._beat_every  # note the first attempt now
        if now - self._last_note >= self._beat_every:
            self._attempt += 1
            self._last_note = now
            log.info("socket mode reconnecting (attempt %d)", self._attempt)

    def _beat(self, now: float) -> None:
        try:
            self._heartbeat.beat()
            self._last_beat = now
        except OSError as exc:
            # Only the error type: the path is the operator's own, but keep lines plain.
            log.warning("could not write the heartbeat file (%s)", type(exc).__name__)

    async def run(self, stop: asyncio.Event) -> None:
        """Check until ``stop`` is set."""
        while not stop.is_set():
            await self.check()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._check)
