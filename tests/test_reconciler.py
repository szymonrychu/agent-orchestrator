"""Reconcile loop against a real Postgres and FakeKube (the production Role, simulated)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx2
import pytest
import pytest_asyncio
from factories import (
    insert_agent,
    insert_check,
    insert_command,
    insert_event,
    insert_message,
    insert_turn,
)
from fakes.fake_kube import FakeKube
from lightkube import AsyncClient, KubeConfig
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_orchestrator.db import agents as agents_repo
from agent_orchestrator.db import clusters, queue
from agent_orchestrator.db.pool import listen
from agent_orchestrator.k8s.kube import (
    FIELD_MANAGER,
    Forbidden,
    KubeError,
    LightkubeApi,
)
from agent_orchestrator.k8s.labels import CLUSTER_LABEL, MANAGED_BY_LABEL, cluster_labels
from agent_orchestrator.reconciler import Reconciler
from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec

KEY = "k" * 32
IMAGE = "harbor.example/containers/claude-code-wrapper:ca2ddf3"
GUARDED = ("PersistentVolumeClaim", "ConfigMap", "Service", "StatefulSet")
FIVE = ("PersistentVolumeClaim", "Secret", "ConfigMap", "Service", "StatefulSet")


def make_settings(dsn: str, **kw) -> Settings:
    return Settings(database_url=dsn, master_key=KEY, agent_image=IMAGE, **kw)


class Env:
    def __init__(self, pool, dsn: str) -> None:
        self.pool, self.dsn = pool, dsn
        self.kube = FakeKube(namespace="agents")
        self.settings = make_settings(dsn)
        self.rec = Reconciler(pool, self.kube, self.settings, wrapper_factory=None)

    async def cluster(self, slug: str = "alpha", **spec) -> clusters.Cluster:
        async with self.pool.connection() as conn:
            return await clusters.create(conn, slug, ClusterSpec(**spec))

    async def get(self, cluster_id) -> clusters.Cluster | None:
        async with self.pool.connection() as conn:
            return await clusters.get(conn, cluster_id)

    async def update(self, cluster_id, **spec) -> None:
        """Change a stored spec the way an out-of-band write would: new spec, generation + 1."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE clusters SET spec = %s, generation = generation + 1 WHERE id = %s",
                (Jsonb(ClusterSpec(**spec).model_dump(mode="json", exclude_none=True)), cluster_id),
            )

    async def delete(self, cluster_id) -> clusters.Cluster:
        async with self.pool.connection() as conn:
            return await clusters.request_delete(conn, cluster_id)

    async def agents(self, cluster_id) -> list[agents_repo.Agent]:
        async with self.pool.connection() as conn:
            return await agents_repo.list_agents(conn, cluster_id)

    async def rows(self, query: str, params: tuple = ()) -> list[dict]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return await cur.fetchall()


@pytest_asyncio.fixture
async def env(pool, dsn) -> Env:
    return Env(pool, dsn)


def labelled(kind: str, name: str, slug: str, *, api: str = "v1") -> dict:
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {"name": name, "labels": cluster_labels(slug)},
        "data": {"stale": "yes"},
    }


# ---------------------------------------------------------------- create, apply, guard


async def test_no_agent_url_warns_once_per_cluster(env, caplog):
    await env.cluster("alpha")
    with caplog.at_level("WARNING", logger="agent_orchestrator.reconciler.loop"):
        await env.rec.reconcile_once()
        await env.rec.reconcile_once()
        warned = [r for r in caplog.records if "ORCH_AGENT_URL" in r.getMessage()]
        assert len(warned) == 1 and "alpha" in warned[0].getMessage()
        await env.cluster("beta")
        await env.rec.reconcile_once()
    warned = [r for r in caplog.records if "ORCH_AGENT_URL" in r.getMessage()]
    assert len(warned) == 2 and "beta" in warned[1].getMessage()


async def test_create_applies_five_objects(env):
    c = await env.cluster("alpha", replicas=2)
    await env.rec.reconcile_once()

    applied = env.kube.applied()
    assert sorted(a.kind for a in applied) == sorted(FIVE)
    assert {a.name for a in applied} == {
        "agent-alpha-workspace",
        "agent-alpha-ccw",
        "agent-alpha-config",
        "agent-alpha",
    }
    for a in applied:
        assert (a.field_manager, a.force) == (FIELD_MANAGER, True) == ("agent-orchestrator", True)
        assert a.obj["metadata"]["namespace"] == "agents"
        assert a.obj["metadata"]["labels"] == cluster_labels("alpha")
    pvc = env.kube.applied("PersistentVolumeClaim")[0].obj
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    assert pvc["spec"]["storageClassName"] == "rook-ceph-rwx"
    for kind in FIVE:
        assert env.kube.names(kind), kind

    got = await env.get(c.id)
    assert got.observed_generation == got.generation == 1
    assert got.status["phase"] == "pending"
    assert (got.status["ready"], got.status["replicas"]) == (0, 2)

    # level-triggered and idempotent: a second pass applies the same set and deletes nothing
    env.kube.reset_records()
    await env.rec.reconcile_once()
    assert sorted(a.kind for a in env.kube.applied()) == sorted(FIVE)
    assert env.kube.deletes == []


async def test_unlabelled_foreign_object_not_applied_over(env):
    foreign = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "agent-alpha-config", "labels": {"team": "someone-else"}},
        "data": {"theirs": "keep"},
    }
    env.kube.seed(foreign)
    # an object with only one of the two labels is just as foreign
    env.kube.seed(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "agent-alpha", "labels": {CLUSTER_LABEL: "alpha"}},
        }
    )
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()

    assert env.kube.applied("ConfigMap") == [] and env.kube.applied("Service") == []
    assert env.kube.applied() == []  # nothing of the cluster is applied next to a foreign one
    assert env.kube.get("ConfigMap", "agent-alpha-config")["data"] == {"theirs": "keep"}
    assert env.kube.deletes == []
    got = await env.get(c.id)
    assert got.status["phase"] == "error"
    assert "foreign_object ConfigMap/agent-alpha-config" in got.status["message"]
    assert "foreign_object Service/agent-alpha" in got.status["message"]
    assert got.observed_generation == 0


async def test_error_in_one_cluster_does_not_block_others(env):
    a = await env.cluster("alpha")
    b = await env.cluster("bravo", replicas=2)
    env.kube.fail("StatefulSet", "agent-alpha", KubeError("admission webhook denied"))
    await env.rec.reconcile_once()

    got_a, got_b = await env.get(a.id), await env.get(b.id)
    assert got_a.status["phase"] == "error"
    assert "admission webhook denied" in got_a.status["message"]
    assert got_a.observed_generation == 0
    assert sorted(x.kind for x in env.kube.applied() if x.name.startswith("agent-bravo")) == sorted(
        FIVE
    )
    assert got_b.status["phase"] == "pending" and got_b.observed_generation == 1
    assert len(await env.agents(b.id)) == 2

    env.kube.heal("StatefulSet", "agent-alpha")
    await env.rec.reconcile_once()
    assert (await env.get(a.id)).status["phase"] == "pending"


# ---------------------------------------------------------------- pods and agents


async def test_observe_pods_upserts_agents(env):
    c = await env.cluster("alpha", replicas=2)
    await env.rec.reconcile_once()
    env.kube.set_pod("agent-alpha-0", ready=True, phase="Running")
    env.kube.set_pod("agent-alpha-1", ready=False, phase="Pending")
    # a check Job pod carries the cluster labels too, but is not an agent
    env.kube.seed(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "agent-alpha-check-7-abcde", "labels": cluster_labels("alpha")},
            "status": {"phase": "Running"},
        }
    )
    await env.rec.reconcile_once()

    agents = await env.agents(c.id)
    assert [(a.ordinal, a.pod_name, a.ready, a.pod_phase) for a in agents] == [
        (0, "agent-alpha-0", True, "Running"),
        (1, "agent-alpha-1", False, "Pending"),
    ]
    assert agents[0].url == "http://agent-alpha-0.agent-alpha.agents.svc.cluster.local:8080"
    assert agents[1].url == "http://agent-alpha-1.agent-alpha.agents.svc.cluster.local:8080"
    assert all(a.gone_at is None and a.session_id is None for a in agents)
    states = await env.rows(
        "SELECT state FROM agent_status WHERE agent_id = ANY(%s)", ([a.id for a in agents],)
    )
    assert [r["state"] for r in states] == ["unknown", "unknown"]

    got = await env.get(c.id)
    assert (got.status["phase"], got.status["ready"], got.status["replicas"]) == ("degraded", 1, 2)
    env.kube.set_pod("agent-alpha-1", ready=True, phase="Running")
    await env.rec.reconcile_once()
    assert (await env.get(c.id)).status["phase"] == "ready"


async def test_observe_stores_pod_ip(env):
    c = await env.cluster("alpha", replicas=2)
    await env.rec.reconcile_once()
    env.kube.set_pod("agent-alpha-0", pod_ip="10.1.2.3")
    del env.kube.objects[("Pod", "agents", "agent-alpha-1")]["status"]["podIP"]  # no IP yet
    env.kube.controller_paused = True
    await env.rec.reconcile_once()
    agents = {a.ordinal: a for a in await env.agents(c.id)}
    assert agents[0].pod_ip == "10.1.2.3"
    assert agents[1].pod_ip is None


async def test_observe_updates_pod_ip(env):
    c = await env.cluster("alpha", replicas=1)
    await env.rec.reconcile_once()
    env.kube.set_pod("agent-alpha-0", pod_ip="10.1.2.3")
    await env.rec.reconcile_once()
    assert (await env.agents(c.id))[0].pod_ip == "10.1.2.3"
    env.kube.set_pod("agent-alpha-0", pod_ip="10.9.9.9")  # restarted pod, new IP
    await env.rec.reconcile_once()
    assert (await env.agents(c.id))[0].pod_ip == "10.9.9.9"


async def test_gone_agent_clears_pod_ip(env):
    c = await env.cluster("alpha", replicas=2)
    await env.rec.reconcile_once()
    env.kube.set_pod("agent-alpha-1", pod_ip="10.1.2.4")
    await env.rec.reconcile_once()
    assert (await env.agents(c.id))[1].pod_ip == "10.1.2.4"
    await env.update(c.id, replicas=1)
    await env.rec.reconcile_once()
    agents = {a.ordinal: a for a in await env.agents(c.id)}
    assert agents[1].gone_at is not None and agents[1].pod_ip is None
    assert agents[0].pod_ip is not None


async def test_scale_down_marks_gone(env):
    c = await env.cluster("alpha", replicas=3)
    await env.rec.reconcile_once()
    before = {a.ordinal: a.id for a in await env.agents(c.id)}
    assert sorted(before) == [0, 1, 2]

    await env.update(c.id, replicas=1)
    await env.rec.reconcile_once()
    assert env.kube.get("StatefulSet", "agent-alpha")["spec"]["replicas"] == 1
    agents = {a.ordinal: a for a in await env.agents(c.id)}
    assert agents[0].gone_at is None
    assert agents[1].gone_at is not None and agents[2].gone_at is not None
    assert not agents[1].ready and not agents[2].ready
    got = await env.get(c.id)
    assert got.observed_generation == 2 and got.status["replicas"] == 1

    # scaling back up reuses the agent rows of those ordinals and clears gone
    await env.update(c.id, replicas=2)
    await env.rec.reconcile_once()
    agents = {a.ordinal: a for a in await env.agents(c.id)}
    assert agents[1].id == before[1] and agents[1].gone_at is None
    assert agents[2].gone_at is not None


async def test_vanished_pod_below_replicas_is_unready_not_gone(env):
    c = await env.cluster("alpha", replicas=2)
    env.kube.pods_ready = True
    await env.rec.reconcile_once()
    assert all(a.ready for a in await env.agents(c.id))
    env.kube.controller_paused = True
    del env.kube.objects[("Pod", "agents", "agent-alpha-1")]  # evicted, not yet recreated
    await env.rec.reconcile_once()
    agents = {a.ordinal: a for a in await env.agents(c.id)}
    assert agents[1].gone_at is None and agents[1].ready is False
    assert agents[0].ready is True


async def test_restart_command_deletes_pod(env):
    c = await env.cluster("alpha", replicas=2)
    await env.rec.reconcile_once()
    agents = await env.agents(c.id)
    old_uid = env.kube.get("Pod", "agent-alpha-1")["metadata"]["uid"]
    cmd = await insert_command(env.pool, agents[1].id, kind="restart")

    await env.rec.reconcile_once()
    assert env.kube.deleted("Pod") == ["agent-alpha-1"]
    assert env.kube.get("Pod", "agent-alpha-1")["metadata"]["uid"] != old_uid  # recreated
    row = (await env.rows("SELECT status, result FROM commands WHERE id = %s", (cmd["id"],)))[0]
    assert row["status"] == "done" and row["result"]["pod"] == "agent-alpha-1"

    # an agent row pointing at a pod without the cluster labels never gets it deleted
    env.kube.seed_orchestrator_objects()
    stray = await insert_agent(
        env.pool, c.id, ordinal=9, pod_name="agent-orchestrator-api-7d9f8c-x2k4q"
    )
    cmd2 = await insert_command(env.pool, stray["id"], kind="restart")
    env.kube.reset_records()
    await env.rec.reconcile_once()
    assert env.kube.deleted("Pod") == []
    assert env.kube.has("Pod", "agent-orchestrator-api-7d9f8c-x2k4q")
    row = (await env.rows("SELECT status, result FROM commands WHERE id = %s", (cmd2["id"],)))[0]
    assert row["status"] == "failed" and row["result"]["error"] == "pod_not_found"


# ---------------------------------------------------------------- prune and discard


async def test_prune_removes_undesired_labelled_only(env):
    c = await env.cluster("alpha")
    env.kube.seed(
        labelled("ConfigMap", "agent-alpha-old", "alpha"),
        labelled("Service", "agent-alpha-legacy", "alpha"),
        # only the cluster label: not ours to delete
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "agent-alpha-half", "labels": {CLUSTER_LABEL: "alpha"}},
        },
        # only the managed-by label
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "agent-alpha-other",
                "labels": {MANAGED_BY_LABEL: "agent-orchestrator"},
            },
        },
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "agent-alpha-plain"}},
        # another cluster's objects are not this cluster's to prune
        labelled("ConfigMap", "agent-bravo-config", "bravo"),
    )
    await env.rec.reconcile_once()

    assert sorted(env.kube.deleted()) == ["agent-alpha-legacy", "agent-alpha-old"]
    for name in ("agent-alpha-half", "agent-alpha-other", "agent-alpha-plain"):
        assert env.kube.has("ConfigMap", name)
    assert env.kube.has("ConfigMap", "agent-bravo-config")
    assert env.kube.has("ConfigMap", "agent-alpha-config")
    assert (await env.get(c.id)).status["phase"] == "pending"


async def test_delete_removes_everything_including_pvc_then_purges_row(env):
    c = await env.cluster("alpha", replicas=2)
    await env.rec.reconcile_once()
    agent = (await env.agents(c.id))[0]
    await insert_message(env.pool, c.id)
    await insert_event(env.pool, c.id, agent.id)
    await insert_turn(env.pool, agent.id)
    await insert_command(env.pool, agent.id, kind="interrupt")
    await insert_check(env.pool, c.id)

    await env.delete(c.id)
    env.kube.reset_records()
    await env.rec.reconcile_once()

    assert sorted(k for (k, _ns, _n) in env.kube.deletes) == sorted(FIVE)
    assert "agent-alpha-workspace" in env.kube.deleted("PersistentVolumeClaim")
    for kind in (*FIVE, "Pod"):
        assert env.kube.names(kind) == [], kind
    assert await env.get(c.id) is None
    for table in ("agents", "messages", "events", "checks"):
        rows = await env.rows(f"SELECT count(*) AS n FROM {table} WHERE cluster_id = %s", (c.id,))
        assert rows[0]["n"] == 0, table
    for table in ("turns", "commands", "agent_status"):
        rows = await env.rows(f"SELECT count(*) AS n FROM {table} WHERE agent_id = %s", (agent.id,))
        assert rows[0]["n"] == 0, table


async def test_purge_notifies_cluster_changed_and_status(env):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    await env.delete(c.id)
    got: list = []
    ready = asyncio.Event()

    async def reader():
        async for ch, payload in listen(env.dsn, ["cluster_changed", "status"], on_ready=ready.set):
            got.append((ch, payload))
            if {x[0] for x in got} == {"cluster_changed", "status"}:
                return

    task = asyncio.create_task(reader())
    await asyncio.wait_for(ready.wait(), 5)
    await env.rec.reconcile_once()
    await asyncio.wait_for(task, 5)
    assert await env.get(c.id) is None
    assert ("cluster_changed", {"cluster_id": str(c.id)}) in got
    assert ("status", {"cluster_id": str(c.id)}) in got


async def test_delete_waits_while_objects_remain(env):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    await env.delete(c.id)
    env.kube.fail("PersistentVolumeClaim", "agent-alpha-workspace", verbs=["delete"])
    await env.rec.reconcile_once()

    got = await env.get(c.id)
    assert got is not None and got.status["phase"] == "error"
    assert env.kube.has("PersistentVolumeClaim", "agent-alpha-workspace")
    env.kube.heal("PersistentVolumeClaim", "agent-alpha-workspace")
    await env.rec.reconcile_once()
    assert await env.get(c.id) is None
    assert env.kube.names("PersistentVolumeClaim") == []


async def test_delete_removes_secret_by_name(env):
    a = await env.cluster("alpha")
    b = await env.cluster("bravo")
    await env.rec.reconcile_once()
    assert env.kube.has("Secret", "agent-alpha-ccw")
    # bravo's Secret is already gone (deleted by hand): the discard still completes
    del env.kube.objects[("Secret", "agents", "agent-bravo-ccw")]
    await env.delete(a.id)
    await env.delete(b.id)
    env.kube.reset_records()
    await env.rec.reconcile_once()

    assert "agent-alpha-ccw" in env.kube.deleted("Secret")
    assert not env.kube.has("Secret", "agent-alpha-ccw")
    secret_calls = [x for x in env.kube.calls if x.kind == "Secret"]
    assert {(x.verb, x.name) for x in secret_calls} == {
        ("delete", "agent-alpha-ccw"),
        ("delete", "agent-bravo-ccw"),
    }
    assert env.kube.forbidden == []
    assert await env.get(a.id) is None and await env.get(b.id) is None


async def test_pre_discard_false_blocks_deletion(env):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    seen: list = []
    allow = False

    async def pre_discard(cluster):
        seen.append(cluster.id)
        return allow

    env.rec.pre_discard = pre_discard
    await env.delete(c.id)
    env.kube.reset_records()
    await env.rec.reconcile_once()
    await env.rec.reconcile_once()

    assert seen == [c.id, c.id]
    assert env.kube.deletes == [] and env.kube.applies == []
    got = await env.get(c.id)
    assert got is not None and got.status["phase"] == "deleting"
    for kind in FIVE:
        assert env.kube.names(kind), kind

    allow = True
    await env.rec.reconcile_once()
    assert await env.get(c.id) is None
    assert sorted(k for (k, _ns, _n) in env.kube.deletes) == sorted(FIVE)


async def test_default_pre_discard_allows(env):
    c = await env.cluster("alpha")
    assert await env.rec.pre_discard(c) is True


# ---------------------------------------------------------------- the Role and the namespace


async def test_secrets_only_created_patched_deleted(env):
    # FakeKube enforces the Role: reading Secrets is refused
    for call in (
        env.kube.get_object("Secret", "agents", "agent-orchestrator-claude"),
        env.kube.list_objects("Secret", "agents", cluster_labels("alpha")),
    ):
        with pytest.raises(Forbidden):
            await call
    assert len(env.kube.forbidden) == 2
    env.kube.reset_records()

    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    await env.update(c.id, replicas=2)
    await env.rec.reconcile_once()

    assert env.kube.forbidden == []
    assert {(x.verb, x.name) for x in env.kube.calls if x.kind == "Secret"} == {
        ("patch", "agent-alpha-ccw")
    }
    secret = env.kube.applied("Secret")[-1].obj
    assert set(secret["stringData"]) == {
        "CCW_AUTH_TOKEN",
        "CCW_PEER_TOKEN",
        "ORCH_AGENT_TOKEN",
    }
    got = await env.get(c.id)
    assert got.status["phase"] == "pending" and got.observed_generation == 2

    await env.delete(c.id)
    await env.rec.reconcile_once()
    assert env.kube.forbidden == []
    assert {x.verb for x in env.kube.calls if x.kind == "Secret"} == {"patch", "delete"}
    assert await env.get(c.id) is None


async def test_shared_namespace_never_touches_orchestrator_objects(env):
    seeded = env.kube.seed_orchestrator_objects()
    snapshot = {key: json.dumps(obj, sort_keys=True) for key, obj in env.kube.objects.items()}

    c = await env.cluster("alpha", replicas=2)
    await env.rec.reconcile_once()  # create
    env.kube.seed(labelled("ConfigMap", "agent-alpha-stale", "alpha"))
    await env.update(c.id, replicas=1)
    await env.rec.reconcile_once()  # prune and scale down
    assert env.kube.deleted() == ["agent-alpha-stale"]
    await env.delete(c.id)
    await env.rec.reconcile_once()  # discard
    assert await env.get(c.id) is None

    for kind, name in seeded:
        key = (kind, "agents", name)
        assert json.dumps(env.kube.objects[key], sort_keys=True) == snapshot[key], name
    assert set(env.kube.objects) == set(snapshot)  # only the orchestrator's objects remain
    assert env.kube.forbidden == []
    assert env.kube.calls
    for call in env.kube.calls:
        assert call.namespace == "agents", call
        if call.verb == "list":
            assert call.selector == cluster_labels("alpha"), call
        else:
            assert call.name.startswith("agent-alpha"), call


async def test_calls_outside_namespace_are_forbidden(env):
    with pytest.raises(Forbidden):
        await env.kube.list_pods("default", cluster_labels("alpha"))
    other = Env(env.pool, env.dsn)
    other.settings = make_settings(env.dsn, namespace="elsewhere")
    other.rec = Reconciler(env.pool, other.kube, other.settings, wrapper_factory=None)
    c = await other.cluster("alpha")
    await other.rec.reconcile_once()
    got = await other.get(c.id)
    assert got.status["phase"] == "error" and "outside the Role" in got.status["message"]


# ---------------------------------------------------------------- run loop


async def test_notify_triggers_immediate_reconcile(env):
    env.rec.settings = make_settings(env.dsn, reconcile_poll_seconds=60)
    stop = asyncio.Event()
    task = asyncio.create_task(env.rec.run(stop))
    try:
        await asyncio.sleep(0.5)  # first pass done, listening
        # inserted by SQL: no NOTIFY, and the poll is a minute away
        await env.rows(
            "INSERT INTO clusters (id, slug, spec) VALUES (%s, 'quiet', '{}') RETURNING id",
            (uuid.uuid4(),),
        )
        await asyncio.sleep(0.5)
        assert not env.kube.has("StatefulSet", "agent-quiet")
        await env.rows("SELECT pg_notify('cluster_changed', '{}') AS n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not env.kube.has("StatefulSet", "agent-quiet"):
            await asyncio.sleep(0.05)
        assert env.kube.has("StatefulSet", "agent-quiet")
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_poll_reconciles_without_notify(env):
    env.rec.settings = make_settings(env.dsn, reconcile_poll_seconds=0.2)
    stop = asyncio.Event()
    task = asyncio.create_task(env.rec.run(stop))
    try:
        await asyncio.sleep(0.3)
        await env.rows(
            "INSERT INTO clusters (id, slug, spec) VALUES (%s, 'polled', '{}') RETURNING id",
            (uuid.uuid4(),),
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not env.kube.has("StatefulSet", "agent-polled"):
            await asyncio.sleep(0.05)
        assert env.kube.has("StatefulSet", "agent-polled")
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_unchanged_status_is_not_rewritten(env):
    """A steady state writes nothing, so the reconciler's own NOTIFY cannot wake it forever."""
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    got: list = []
    ready = asyncio.Event()

    async def reader():
        async for ch, payload in listen(env.dsn, ["cluster_changed", "status"], on_ready=ready.set):
            got.append((ch, payload))

    task = asyncio.create_task(reader())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        await env.rec.reconcile_once()
        await asyncio.sleep(0.5)
        assert got == []
        env.kube.set_pod("agent-alpha-0", ready=True, phase="Running")
        await env.rec.reconcile_once()
        await asyncio.sleep(0.5)
        assert ("cluster_changed", {"cluster_id": str(c.id)}) in got
        assert ("status", {"cluster_id": str(c.id)}) in got
    finally:
        task.cancel()


# ---------------------------------------------------------------- agents repository


async def test_agents_repo_writes(env):
    c = await env.cluster("alpha")
    async with env.pool.connection() as conn:
        a = await agents_repo.upsert_agent(
            conn, c.id, 0, "agent-alpha-0", "http://x", "Pending", False
        )
        again = await agents_repo.upsert_agent(
            conn, c.id, 0, "agent-alpha-0", "http://x", "Running", True
        )
        assert again.id == a.id and again.ready and again.pod_phase == "Running"
        assert await agents_repo.set_session(conn, a.id, "s-1", None) is True
        got = await agents_repo.get_agent(conn, a.id)
        assert got.session_id == "s-1" and got.session_created_at is not None
        assert await agents_repo.clear_session(conn, a.id) is True
        got = await agents_repo.get_agent(conn, a.id)
        assert got.session_id is None and got.session_created_at is None
        assert await agents_repo.mark_gone(conn, a.id) is True
        assert await agents_repo.mark_gone(conn, a.id) is False  # already gone
        listed = await agents_repo.list_agents(conn, c.id)
        assert [x.id for x in listed] == [a.id] and listed[0].gone_at is not None
        assert not listed[0].ready
        assert await agents_repo.get_agent(conn, uuid.uuid4()) is None
    rows = await env.rows("SELECT state FROM agent_status WHERE agent_id = %s", (a.id,))
    assert [r["state"] for r in rows] == ["unknown"]


async def test_restart_of_deleting_cluster_fails(env):
    c = await env.cluster("alpha")
    await env.rec.reconcile_once()
    agent = (await env.agents(c.id))[0]
    env.rec.pre_discard = _never
    await env.delete(c.id)
    cmd = await insert_command(env.pool, agent.id, kind="restart")
    await env.rec.reconcile_once()
    async with env.pool.connection() as conn:
        assert await queue.claim_command(conn, ["restart"]) is None
    row = (await env.rows("SELECT status, result FROM commands WHERE id = %s", (cmd["id"],)))[0]
    assert row["status"] == "failed" and row["result"]["error"] == "cluster_deleting"
    assert env.kube.deleted("Pod") == []


async def _never(cluster) -> bool:
    return False


# ---------------------------------------------------------------- LightkubeApi over HTTP


def _lightkube(handler) -> LightkubeApi:
    config = KubeConfig.from_dict(
        {
            "current-context": "c",
            "clusters": [{"name": "k", "cluster": {"server": "http://kube.test"}}],
            "contexts": [{"name": "c", "context": {"cluster": "k", "user": "u"}}],
            "users": [{"name": "u", "user": {}}],
        }
    )
    client = AsyncClient(config, transport=httpx2.MockTransport(handler))
    return LightkubeApi(client)


def _status(code: int, reason: str) -> httpx2.Response:
    return httpx2.Response(
        code,
        json={
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "code": code,
            "reason": reason,
            "message": f"{reason} for test",
        },
    )


async def test_lightkube_apply_is_server_side_with_field_manager_and_force():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = json.loads(request.content)
        body["metadata"]["uid"] = "u-1"
        return httpx2.Response(200, json=body)

    kube = _lightkube(handler)
    obj = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "agent-alpha-config", "namespace": "agents", "labels": {"a": "b"}},
        "data": {"k": "v"},
    }
    out = await kube.apply(obj)
    req = seen[0]
    assert req.method == "PATCH"
    assert req.url.path == "/api/v1/namespaces/agents/configmaps/agent-alpha-config"
    assert req.url.params["fieldManager"] == "agent-orchestrator"
    assert req.url.params["force"] == "true"
    assert req.headers["content-type"] == "application/apply-patch+yaml"
    assert out["metadata"]["uid"] == "u-1" and out["data"] == {"k": "v"}

    await kube.apply(
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "agent-alpha", "namespace": "agents"},
            "spec": {
                "selector": {"matchLabels": {"a": "b"}},
                "serviceName": "agent-alpha",
                "template": {
                    "metadata": {"labels": {"a": "b"}},
                    "spec": {"containers": [{"name": "c", "image": "i"}]},
                },
            },
        }
    )
    assert seen[1].url.path == "/apis/apps/v1/namespaces/agents/statefulsets/agent-alpha"
    await kube.aclose()


async def test_lightkube_get_list_delete_and_errors():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/missing"):
            return _status(404, "NotFound")
        if "/secrets" in path:
            return _status(403, "Forbidden")
        if request.method == "GET" and path == "/api/v1/namespaces/agents/pods":
            return httpx2.Response(
                200,
                json={
                    "kind": "PodList",
                    "apiVersion": "v1",
                    "metadata": {},
                    "items": [
                        {
                            "metadata": {"name": "agent-alpha-0", "namespace": "agents"},
                            "status": {"phase": "Running"},
                        }
                    ],
                },
            )
        if request.method == "DELETE":
            return httpx2.Response(
                200, json={"kind": "Status", "apiVersion": "v1", "status": "Success"}
            )
        if request.method == "GET" and path.endswith("/pods/agent-alpha-0/log"):
            return httpx2.Response(200, text="one\ntwo\n")
        return _status(500, "InternalError")

    kube = _lightkube(handler)
    assert await kube.get_object("ConfigMap", "agents", "missing") is None
    assert await kube.delete("PersistentVolumeClaim", "agents", "missing") is False
    assert await kube.delete("Service", "agents", "agent-alpha") is True
    assert seen[-1].url.path == "/api/v1/namespaces/agents/services/agent-alpha"
    pods = await kube.list_pods("agents", cluster_labels("alpha"))
    assert [p["metadata"]["name"] for p in pods] == ["agent-alpha-0"]
    selector = seen[-1].url.params["labelSelector"]
    assert set(selector.split(",")) == {
        "agents.szymonrichert.pl/cluster=alpha",
        "app.kubernetes.io/managed-by=agent-orchestrator",
    }
    with pytest.raises(Forbidden):
        await kube.delete("Secret", "agents", "agent-alpha-ccw")
    with pytest.raises(KubeError):
        await kube.get_object("Service", "agents", "boom")
    log = await kube.read_pod_log("agents", "agent-alpha-0", 50)
    assert log == "one\ntwo"
    assert seen[-1].url.params["tailLines"] == "50"
    with pytest.raises(KubeError):
        await kube.list_objects("Deployment", "agents", {})
    await kube.aclose()


def test_lightkube_default_client_uses_env_config(monkeypatch, tmp_path):
    cfg = tmp_path / "kubeconfig"
    cfg.write_text(
        "apiVersion: v1\nkind: Config\ncurrent-context: c\n"
        "clusters: [{name: k, cluster: {server: 'http://from-kubeconfig.test'}}]\n"
        "contexts: [{name: c, context: {cluster: k, user: u}}]\n"
        "users: [{name: u, user: {}}]\n"
    )
    monkeypatch.setenv("KUBECONFIG", str(cfg))
    kube = LightkubeApi()
    assert kube.client._client.config.cluster.server == "http://from-kubeconfig.test"
