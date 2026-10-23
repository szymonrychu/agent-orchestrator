"""Session bootstrap and adoption by the reconciler (D6), against the in-process fake wrapper."""

from __future__ import annotations

import asyncio

import pytest_asyncio
from factories import insert_command
from fakes.fake_kube import FakeKube
from fakes.fake_wrapper import FakeWrapper
from psycopg.rows import dict_row

from agent_orchestrator.db import agents as agents_repo
from agent_orchestrator.db import clusters, queue
from agent_orchestrator.k8s.render import workdir_path
from agent_orchestrator.reconciler import Reconciler
from agent_orchestrator.reconciler import sessions as sessions_mod
from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec
from agent_orchestrator.tokens import tokens_for
from agent_orchestrator.wrapper.client import WrapperClient
from agent_orchestrator.wrapper.models import WrapperError

KEY = "k" * 32
IMAGE = "harbor.example/containers/claude-code-wrapper:ca2ddf3"


class Env:
    def __init__(self, pool, dsn: str, fake: FakeWrapper) -> None:
        self.pool, self.fake = pool, fake
        self.kube = FakeKube(namespace="agents")
        self.kube.pods_ready = True
        self.settings = Settings(database_url=dsn, master_key=KEY, agent_image=IMAGE)
        self.factory_calls: list[tuple[str, str]] = []
        self.client_override = None
        self.rec = Reconciler(pool, self.kube, self.settings, wrapper_factory=self._factory)

    def _factory(self, url: str, token: str):
        self.factory_calls.append((url, token))
        if self.client_override is not None:
            return self.client_override
        return WrapperClient(self.fake.base_url, token)

    async def cluster(self, slug: str = "alpha", **spec) -> clusters.Cluster:
        async with self.pool.connection() as conn:
            c = await clusters.create(conn, slug, ClusterSpec(**spec))
        self.fake.token = tokens_for(self.settings, c.id).auth
        return c

    async def agents(self, cluster_id) -> list[agents_repo.Agent]:
        async with self.pool.connection() as conn:
            return await agents_repo.list_agents(conn, cluster_id)

    async def status(self, cluster_id) -> dict:
        async with self.pool.connection() as conn:
            c = await clusters.get(conn, cluster_id)
        return c.status

    async def seed_session(self, cluster_id, state: str, transcript: bool = False) -> str:
        """A session already on the wrapper, created with the cluster token."""
        client = WrapperClient(self.fake.base_url, tokens_for(self.settings, cluster_id).auth)
        snap = await client.create_session("seed", "seed")
        if state != "ready":
            self.fake.set_session_state(
                state, has_transcript=transcript, session_id=snap.session_id
            )
        if transcript:
            self.fake.sessions[snap.session_id].turns_completed = 1
        return snap.session_id


@pytest_asyncio.fixture
async def fake():
    async with FakeWrapper() as fw:
        yield fw


@pytest_asyncio.fixture
async def env(pool, dsn, fake) -> Env:
    return Env(pool, dsn, fake)


async def test_creates_session_for_ready_pod(env, fake):
    c = await env.cluster("alpha", workdir="repo", model="opus", effort="high")
    await env.rec.reconcile_once()

    (agent,) = await env.agents(c.id)
    assert len(fake.sessions) == 1
    ses = next(iter(fake.sessions.values()))
    assert ses.name == "repo"
    assert ses.model == "opus"
    assert agent.session_id == ses.session_id
    assert agent.session_created_at is not None

    await env.rec.reconcile_once()  # steady state: no second session
    assert len(fake.sessions) == 1


async def test_adopts_existing_session(env, fake):
    c = await env.cluster("alpha")
    sid = await env.seed_session(c.id, "busy")
    await env.rec.reconcile_once()

    (agent,) = await env.agents(c.id)
    assert agent.session_id == sid
    assert list(fake.sessions) == [sid]


async def test_unready_pod_skipped(env, fake):
    env.kube.pods_ready = False
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()

    (agent,) = await env.agents(c.id)
    assert agent.session_id is None
    assert fake.sessions == {}
    assert env.factory_calls == []


class Stub503:
    async def list_sessions(self):
        raise WrapperError(503, "booting", "not ready")


async def test_wrapper_unavailable_retries_next_tick(env, fake):
    c = await env.cluster("alpha")
    env.client_override = Stub503()
    await env.rec.reconcile_once()

    (agent,) = await env.agents(c.id)
    assert agent.session_id is None
    assert "503" in (await env.status(c.id))["message"]
    assert "agent-alpha-0" in (await env.status(c.id))["message"]

    env.client_override = None
    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    assert agent.session_id is not None
    assert (await env.status(c.id))["message"] == ""


async def test_network_error_leaves_agent_without_session(env, fake):
    c = await env.cluster("alpha")
    await fake.set_unreachable(True)
    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    assert agent.session_id is None
    assert (await env.status(c.id))["message"]
    await fake.set_unreachable(False)


async def test_lost_session_rebootstrapped(env, fake):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    old = agent.session_id
    del fake.sessions[old]

    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    assert agent.session_id not in (None, old)
    assert list(fake.sessions) == [agent.session_id]


async def test_uses_cluster_auth_token(env, fake):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    expected = tokens_for(env.settings, c.id).auth
    assert env.factory_calls
    assert {token for _url, token in env.factory_calls} == {expected}
    assert all("agent-alpha-0.agent-alpha.agents.svc" in url for url, _t in env.factory_calls)
    assert len(fake.sessions) == 1  # the fake only answers to that token


async def test_adopts_only_live_sessions(env, fake):
    c = await env.cluster("alpha")
    stopped = await env.seed_session(c.id, "stopped")
    failed = await env.seed_session(c.id, "failed")
    await env.rec.reconcile_once()

    (agent,) = await env.agents(c.id)
    assert agent.session_id not in (stopped, failed, None)
    assert fake.sessions[agent.session_id].state == "ready"
    assert fake.sessions[stopped].state == "stopped"
    assert fake.sessions[failed].state == "failed"

    for state in ("creating", "ready", "busy"):
        assert state in sessions_mod.ADOPTABLE
    assert "stopped" not in sessions_mod.ADOPTABLE and "failed" not in sessions_mod.ADOPTABLE


async def test_resumes_stopped_session_with_transcript(env, fake):
    c = await env.cluster("alpha")
    sid = await env.seed_session(c.id, "stopped", transcript=True)
    await env.rec.reconcile_once()

    (agent,) = await env.agents(c.id)
    assert agent.session_id == sid
    assert fake.sessions[sid].state == "ready"
    assert list(fake.sessions) == [sid]


async def test_failed_session_without_transcript_gets_new_session(env, fake):
    c = await env.cluster("alpha")
    sid = await env.seed_session(c.id, "failed", transcript=False)
    await env.rec.reconcile_once()

    (agent,) = await env.agents(c.id)
    assert agent.session_id not in (None, sid)
    assert fake.sessions[sid].state == "failed"
    assert fake.sessions[agent.session_id].state == "ready"


async def test_rebootstrap_command_clears_and_bootstraps(env, fake):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    old = agent.session_id
    del fake.sessions[old]  # the wrapper answered 404 on submit
    cmd = await insert_command(env.pool, agent.id, kind="rebootstrap")

    # no converge in between: the command alone must bootstrap in the same tick
    await env.rec._run_rebootstraps()

    (agent,) = await env.agents(c.id)
    assert agent.session_id not in (None, old)
    async with env.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT status FROM commands WHERE id = %s", (cmd["id"],))
        assert (await cur.fetchone())["status"] == "done"
    async with env.pool.connection() as conn:
        assert await queue.claim_command(conn, ["rebootstrap"]) is None


async def test_rebootstrap_runs_inside_reconcile_tick(env, fake):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    old = agent.session_id
    del fake.sessions[old]
    await insert_command(env.pool, agent.id, kind="rebootstrap")
    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    assert agent.session_id not in (None, old)
    assert list(fake.sessions) == [agent.session_id]


async def test_session_workdir_matches_rendered_workdir_path(env, fake):
    c = await env.cluster("alpha", workdir="my-repo")
    await env.rec.reconcile_once()
    (agent,) = await env.agents(c.id)
    assert fake.sessions[agent.session_id].workdir == workdir_path(c.spec)
    assert workdir_path(c.spec) == "/workdir/my-repo"


async def test_bootstraps_run_concurrently_bounded_at_eight(env, fake):
    assert sessions_mod.MAX_CONCURRENT_BOOTSTRAPS == 8
    running = peak = 0

    class Slow:
        async def list_sessions(self):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            raise WrapperError(503, "booting", "later")

    env.client_override = Slow()
    c = await env.cluster("alpha", replicas=12)
    await env.rec.reconcile_once()
    assert 2 <= peak <= 8
    assert len(await env.agents(c.id)) == 12
