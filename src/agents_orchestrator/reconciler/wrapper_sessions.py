"""Session bootstrap and adoption (D6): every ready agent pod gets exactly one wrapper session.

The wrapper allows few sessions per pod and session creation is synchronous, so a blind create
can answer 429. The order is therefore adopt first:
1. a creating, ready or busy session is adopted (the newest first)
2. a stopped or failed session that may have a transcript is resumed, then adopted; a refusal
   falls through
3. otherwise a new session is created in the one rendered workdir (`render.workdir_path`)

A stored session is kept only while adoptable, resumed when stopped or failed with a transcript,
and otherwise cleared (also when the wrapper no longer lists it) so the agent bootstraps again.
Every successful resume, of the stored session or in step 2, records `agents.wrapper_resumed_at`
(deploy-safe-agent-sessions D3) so the ingester re-attaches to a session whose stored
`session_closed` the resume lifted; an adopt or create records none.
Errors never raise out of `bootstrap`: they come back as a message for the session status and
the next tick bootstraps it again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from psycopg_pool import AsyncConnectionPool

from agents_orchestrator.db import agents as agents_repo
from agents_orchestrator.db.agents import Agent
from agents_orchestrator.spec import SessionSpec
from agents_orchestrator.wrapper.models import Snapshot, WrapperError, WrapperUnreachable

log = logging.getLogger(__name__)

ADOPTABLE = frozenset({"creating", "ready", "busy"})
RESUMABLE = frozenset({"stopped", "failed"})
MAX_CONCURRENT_BOOTSTRAPS = 8

WrapperFactory = Callable[[str, str], Any]


def _newest_first(sessions: list[Snapshot]) -> list[Snapshot]:
    """Newest created first; sessions without a timestamp keep the wrapper's order, reversed."""
    indexed = list(enumerate(sessions))
    indexed.sort(
        key=lambda p: (p[1].created_at.timestamp() if p[1].created_at else float("-inf"), p[0])
    )
    return [s for _i, s in reversed(indexed)]


def _describe(exc: Exception) -> str:
    if isinstance(exc, WrapperError):
        return f"{exc.status} {exc.code}"
    return str(exc) or type(exc).__name__


class WrapperSessionBootstrapper:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        wrapper_factory: WrapperFactory,
        limit: int = MAX_CONCURRENT_BOOTSTRAPS,
    ) -> None:
        self.pool = pool
        self.wrapper_factory = wrapper_factory
        self._slots = asyncio.Semaphore(limit)

    async def bootstrap_many(self, agents: list[Agent], spec: SessionSpec, token: str) -> list[str]:
        """Bootstrap the agents concurrently; returns one message per agent that failed."""
        results = await asyncio.gather(*(self.bootstrap(a, spec, token) for a in agents))
        return [m for m in results if m]

    async def bootstrap(self, agent: Agent, spec: SessionSpec, token: str) -> str | None:
        """Make `agent` hold a session. Returns None on success, else a status message."""
        if agent.url is None:
            return f"{agent.pod_name}: no url"
        async with self._slots:
            client = self.wrapper_factory(agent.url, token)
            try:
                return await self._bootstrap(client, agent, spec)
            except asyncio.CancelledError:
                raise
            except (WrapperError, WrapperUnreachable) as exc:
                log.warning("session bootstrap of %s deferred: %s", agent.pod_name, exc)
                return f"session bootstrap of {agent.pod_name}: {_describe(exc)}"
            finally:
                aclose = getattr(client, "aclose", None)
                if aclose is not None:
                    await aclose()

    async def _bootstrap(self, client: Any, agent: Agent, spec: SessionSpec) -> str | None:
        listed = await client.list_sessions()
        if agent.wrapper_session_id is not None:
            stored = next(
                (s for s in listed if s.wrapper_session_id == agent.wrapper_session_id), None
            )
            if stored is not None and stored.state in ADOPTABLE:
                return None
            if (
                stored is not None
                and stored.state in RESUMABLE
                and stored.has_transcript is not False
            ):
                try:
                    resumed = await client.resume_session(stored.wrapper_session_id)
                except WrapperError as exc:
                    if exc.status >= 500 or exc.status == 429:
                        raise
                    log.info("resume of stored %s refused: %s", stored.wrapper_session_id, exc)
                else:
                    await self._record(agent, resumed, resumed=True)
                    return None
            async with self.pool.connection() as conn:
                await agents_repo.clear_wrapper_session(conn, agent.id)
            log.info(
                "session %s of %s is %s, bootstrapping again",
                agent.wrapper_session_id,
                agent.pod_name,
                stored.state if stored else "gone",
            )
        snap, resumed = await self._pick(client, listed, spec)
        await self._record(agent, snap, resumed=resumed)
        return None

    async def _record(self, agent: Agent, snap: Snapshot, *, resumed: bool) -> None:
        async with self.pool.connection() as conn:
            await agents_repo.set_wrapper_session(
                conn, agent.id, snap.wrapper_session_id, snap.created_at, resumed=resumed
            )

    async def _pick(
        self, client: Any, listed: list[Snapshot], spec: SessionSpec
    ) -> tuple[Snapshot, bool]:
        """The session to record and whether it was resumed."""
        ordered = _newest_first(listed)
        for snap in ordered:
            if snap.state in ADOPTABLE:
                return snap, False
        for snap in ordered:
            if snap.state in RESUMABLE and snap.has_transcript is not False:
                try:
                    return await client.resume_session(snap.wrapper_session_id), True
                except WrapperError as exc:
                    if exc.status >= 500 or exc.status == 429:
                        raise
                    log.info("resume of %s refused: %s", snap.wrapper_session_id, exc)
        created = await client.create_session(
            spec.workdir, spec.workdir, model=spec.model, effort=spec.effort
        )
        return created, False
