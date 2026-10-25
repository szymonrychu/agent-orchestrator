"""One LISTEN connection per process, fanned out to in-process subscribers (D14).

Subscribers get a bounded queue; one that falls behind is dropped (its stream closes and the
client reconnects), so a stuck client can never stall the others or grow memory. NOTIFYs are
only hints: every consumer also polls, so a lost NOTIFY delays at most one poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Any

from agents_orchestrator.db.pool import listen

log = logging.getLogger(__name__)

CHANNELS = ("events", "status", "session_changed", "leader_changed")
DEFAULT_QUEUE_SIZE = 256


class SubscriptionDropped(Exception):
    """The subscriber fell behind (or the listener stopped) and was removed."""

    def __init__(self) -> None:
        super().__init__("subscriber dropped")


class Subscription:
    def __init__(self, listener: Listener, channels: frozenset[str], size: int):
        self._listener = listener
        self.channels = channels
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(size)
        self._wake = asyncio.Event()
        self._dropped = False

    def _offer(self, channel: str, payload: dict[str, Any]) -> bool:
        try:
            self._queue.put_nowait((channel, payload))
        except asyncio.QueueFull:
            return False
        self._wake.set()
        return True

    def _drop(self) -> None:
        self._dropped = True
        self._wake.set()

    def drain(self) -> list[tuple[str, dict[str, Any]]]:
        """All pending notifications; raises SubscriptionDropped once removed."""
        if self._dropped:
            raise SubscriptionDropped
        out = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        self._wake.clear()
        return out

    async def wait(self, timeout: float) -> bool:
        """True when something is pending, False on timeout."""
        if self._dropped:
            raise SubscriptionDropped
        if self._queue.empty():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                return False
        if self._dropped:
            raise SubscriptionDropped
        return True

    def close(self) -> None:
        self._listener._remove(self)


class Listener:
    def __init__(self, dsn: str, queue_size: int = DEFAULT_QUEUE_SIZE):
        self._dsn = dsn
        self._queue_size = queue_size
        self._subs: set[Subscription] = set()
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    def subscriber_count(self) -> int:
        return len(self._subs)

    def subscribe(self, channels: Sequence[str]) -> Subscription:
        sub = Subscription(self, frozenset(channels), self._queue_size)
        self._subs.add(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    async def start(self, timeout: float = 10.0) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="notify-listener")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for sub in list(self._subs):
            sub._drop()
        self._subs.clear()

    async def _run(self) -> None:
        async for channel, payload in listen(self._dsn, CHANNELS, on_ready=self._ready.set):
            for sub in list(self._subs):
                if channel in sub.channels and not sub._offer(channel, payload):
                    log.warning("dropping slow subscriber on %s", channel)
                    self._subs.discard(sub)
                    sub._drop()
