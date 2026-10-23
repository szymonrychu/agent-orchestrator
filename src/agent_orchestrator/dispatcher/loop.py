"""Dispatcher loop: claim, send, map the outcome, run commands, hand stale rows to the verifier.

The dispatcher never writes agent_status (free_since moves only through the ingester) and never
re-sends an ambiguous or stale turn: those go to `verifying` and the verifier decides (D24).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx
from psycopg_pool import AsyncConnectionPool

from agent_orchestrator.db import clusters, queue
from agent_orchestrator.db.pool import listen
from agent_orchestrator.settings import Settings
from agent_orchestrator.tokens import tokens_for
from agent_orchestrator.wrapper.client import WrapperClient
from agent_orchestrator.wrapper.models import (
    Ambiguous,
    Busy,
    ConfigError,
    Gone,
    Rejected,
    TurnAccepted,
    Unavailable,
)

log = logging.getLogger(__name__)

MAX_IN_FLIGHT = 8
CHANNELS = ("agent_free", "message_queued")
COMMAND_KINDS = ("interrupt", "compact")
DEFAULT_MAX_ATTEMPTS = 5


class Dispatcher:
    def __init__(self, pool: AsyncConnectionPool, settings: Settings) -> None:
        self.pool = pool
        self.settings = settings
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self, cluster_id: Any, url: str) -> WrapperClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        token = tokens_for(self.settings, cluster_id).auth
        return WrapperClient(
            url,
            token,
            http=self._http,
            send_timeout=self.settings.wrapper_send_timeout_seconds,
        )

    async def run(self, stop: asyncio.Event) -> None:
        wake = asyncio.Event()

        async def listener() -> None:
            async for _channel, _payload in listen(self.pool.conninfo, CHANNELS):
                wake.set()

        listener_task = asyncio.create_task(listener())
        stop_wait = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                wake.clear()
                try:
                    await self.drain()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("dispatcher drain failed")
                wake_wait = asyncio.create_task(wake.wait())
                try:
                    await asyncio.wait(
                        {wake_wait, stop_wait},
                        timeout=self.settings.dispatch_poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    wake_wait.cancel()
        finally:
            listener_task.cancel()
            stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task
            await self.aclose()

    # ------------------------------------------------------------------ one wake

    async def drain(self) -> None:
        await self._drain_messages()
        await self._drain_commands()
        async with self.pool.connection() as conn:
            await queue.verify_stale_dispatching(conn, self.settings.dispatching_stale_seconds)
        await self.verify_pending()

    async def verify_pending(self) -> None:
        """Hook for the ambiguous-send verifier; a no-op until it replaces this."""

    async def _drain_messages(self) -> None:
        slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                await slots.acquire()
                try:
                    async with self.pool.connection() as conn:
                        claim = await queue.claim_and_pick(conn)
                except BaseException:
                    slots.release()
                    raise
                if claim is None:
                    slots.release()
                    break
                task = asyncio.create_task(self._guarded_send(claim, slots))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _guarded_send(self, claim: queue.Claim, slots: asyncio.Semaphore) -> None:
        try:
            await self._send(claim)
        except Exception:
            # The row stays dispatching; the stale rule hands it to the verifier (D24).
            log.exception("send of message %s failed unexpectedly", claim.message_id)
        finally:
            slots.release()

    # ------------------------------------------------------------------ one send

    async def _heartbeat(self, message_id: int) -> None:
        while True:
            await asyncio.sleep(self.settings.dispatching_heartbeat_seconds)
            async with self.pool.connection() as conn:
                await queue.heartbeat_dispatching(conn, message_id)

    async def _send(self, claim: queue.Claim) -> None:
        s = self.settings
        if not claim.agent_url:
            outcome: Any = Unavailable("agent has no url")
        else:
            client = self._client(claim.cluster_id, claim.agent_url)
            heartbeat = asyncio.create_task(self._heartbeat(claim.message_id))
            try:
                outcome = await client.submit_turn(
                    claim.session_id,
                    claim.text,
                    {"orch_message_id": claim.message_id, "orch_attempt": claim.attempts + 1},
                )
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

        async with self.pool.connection() as conn:
            mid = claim.message_id
            if isinstance(outcome, TurnAccepted):
                await queue.mark_sent(conn, mid, outcome.turn_id)
                return
            if isinstance(outcome, Ambiguous):
                # The turn may be running: keep the reservation, never re-send here.
                await queue.mark_verifying(conn, mid, "ambiguous")
                return
            await queue.release_agent(
                conn, claim.agent_id, claim.previous_dispatched_at, claimed_at=claim.dispatched_at
            )
            if isinstance(outcome, Busy):
                await queue.requeue(conn, mid)
            elif isinstance(outcome, ConfigError):
                await queue.fail_message(conn, mid, "config_error")
            elif isinstance(outcome, Rejected):
                await queue.fail_message(conn, mid, "rejected")
            elif isinstance(outcome, Unavailable | Gone):
                max_attempts = await self._max_attempts(conn, claim)
                error = "session_gone" if isinstance(outcome, Gone) else "unavailable"
                await queue.record_failure(
                    conn,
                    mid,
                    error,
                    max_attempts,
                    s.retry_backoff_base_seconds,
                    s.retry_backoff_max_seconds,
                )
                if isinstance(outcome, Gone):
                    await queue.enqueue_command(conn, claim.agent_id, "rebootstrap")
            else:  # pragma: no cover - SubmitOutcome is exhaustive
                log.error("unmapped outcome %r for message %s", outcome, mid)

    async def _max_attempts(self, conn: Any, claim: queue.Claim) -> int:
        cluster = await clusters.get(conn, claim.cluster_id)
        return cluster.spec.max_attempts if cluster is not None else DEFAULT_MAX_ATTEMPTS

    # ------------------------------------------------------------------ commands

    async def _drain_commands(self) -> None:
        while True:
            async with self.pool.connection() as conn:
                cmd = await queue.claim_command(conn, COMMAND_KINDS)
            if cmd is None:
                return
            status, result = await self._run_command(cmd)
            async with self.pool.connection() as conn:
                await queue.complete_command(conn, cmd.id, status, result)

    async def _run_command(self, cmd: queue.Command) -> tuple[str, dict[str, Any]]:
        if not cmd.agent_url or not cmd.session_id:
            return "failed", {"error": "agent has no session"}
        client = self._client(cmd.cluster_id, cmd.agent_url)
        try:
            if cmd.kind == "interrupt":
                response = await client.interrupt(cmd.session_id)
            else:
                response = await client.command(cmd.session_id, cmd.kind)
        except Exception as exc:
            return "failed", {"error": f"{type(exc).__name__}: {exc}"}
        return "done", {"response": response}
