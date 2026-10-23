"""Level-triggered reconcile: the clusters table drives Kubernetes and the agents table.

Every tick reads every cluster row and converges it (D6), so a crash mid-way leaves nothing
that the next tick does not repair. All calls target `settings.namespace`, the one namespace
shared with the orchestrator's own objects (D26). The guards that keep the reconciler off those:
- every list carries the cluster selector, and only objects carrying both the managed-by and
  the cluster label are ever deleted
- before applying a StatefulSet, Service, ConfigMap or PVC it reads the object; one that exists
  without both labels is foreign, and then nothing of that cluster is applied
- Secrets are never read or listed: the cluster Secret is applied with the rest and deleted by
  its deterministic name

A deleting cluster (D15) waits for `pre_discard`, then loses every labelled object including
its PVC, and once none is left its row is purged, which cascades its whole history.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from psycopg_pool import AsyncConnectionPool

from agent_orchestrator.db import agents as agents_repo
from agent_orchestrator.db import clusters, queue
from agent_orchestrator.db.clusters import Cluster, ClusterNotFound
from agent_orchestrator.db.pool import listen, notify
from agent_orchestrator.k8s.kube import FIELD_MANAGER, KubeApi, Obj, is_owned, pod_ready
from agent_orchestrator.k8s.labels import cluster_labels, object_names, prefix
from agent_orchestrator.k8s.render import pod_url, render_cluster
from agent_orchestrator.reconciler.sessions import SessionBootstrapper
from agent_orchestrator.settings import Settings
from agent_orchestrator.tokens import tokens_for

log = logging.getLogger(__name__)

CHANNELS = ("cluster_changed",)
GUARDED_KINDS = frozenset({"StatefulSet", "Service", "ConfigMap", "PersistentVolumeClaim"})
# Deletion order: the StatefulSet first so its pods stop using the config and the volume.
PRUNE_ORDER = ("StatefulSet", "Service", "ConfigMap", "PersistentVolumeClaim")

WrapperFactory = Callable[[str, str], Any]
"""(base_url, auth_token) -> wrapper client; used by session bootstrap."""


class Reconciler:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        kube: KubeApi,
        settings: Settings,
        wrapper_factory: WrapperFactory | None = None,
    ) -> None:
        self.pool = pool
        self.kube = kube
        self.settings = settings
        self.wrapper_factory = wrapper_factory
        self.bootstrapper = (
            SessionBootstrapper(pool, wrapper_factory) if wrapper_factory is not None else None
        )

    # ------------------------------------------------------------------ lifecycle

    async def run(self, stop: asyncio.Event) -> None:
        """Reconcile every `reconcile_poll_seconds`, and at once on NOTIFY cluster_changed."""
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
                    await self.reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("reconcile pass failed")
                wake_wait = asyncio.create_task(wake.wait())
                try:
                    await asyncio.wait(
                        {wake_wait, stop_wait},
                        timeout=self.settings.reconcile_poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    wake_wait.cancel()
        finally:
            listener_task.cancel()
            stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task

    # ------------------------------------------------------------------ one pass

    async def reconcile_once(self) -> None:
        await self._run_restarts()
        await self._run_rebootstraps()
        async with self.pool.connection() as conn:
            rows = await clusters.list_clusters(conn)
        for cluster in rows:
            try:
                if cluster.desired == "deleting":
                    await self._discard(cluster)
                else:
                    await self._converge(cluster)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("reconcile of cluster %s failed", cluster.slug)
                await self._record_error(cluster, exc)

    async def pre_discard(self, cluster: Cluster) -> bool:
        """The gate before a discard deletes anything (D15): True lets the deletion proceed.

        The base reconciler has no pre-discard hook, so it always returns True. A subclass
        overrides this to deliver the cluster's history export and returns False to block
        the deletion until delivery succeeds.
        """
        return True

    # ------------------------------------------------------------------ an active cluster

    async def _converge(self, cluster: Cluster) -> None:
        ns, slug = self.settings.namespace, cluster.slug
        objs = render_cluster(cluster, self.settings, tokens_for(self.settings, cluster.id))

        foreign = await self._foreign(slug, objs)
        if foreign:
            await self._write_status(
                cluster,
                {"phase": "error", "message": "; ".join(foreign)},
                cluster.observed_generation,
            )
            return

        for obj in objs:
            await self.kube.apply(obj, field_manager=FIELD_MANAGER, force=True)
        desired = {(o["kind"], o["metadata"]["name"]) for o in objs}
        await self._prune(slug, desired)

        replicas = cluster.spec.replicas
        ready = await self._observe(cluster)
        if ready >= replicas:
            phase, message = "ready", ""
        else:
            phase = "pending" if ready == 0 else "degraded"
            message = f"{ready} of {replicas} pods ready"
        problems = await self._bootstrap_sessions(cluster)
        if problems:
            message = "; ".join(filter(None, [message, *problems]))
        log.debug("cluster %s in %s: %s", slug, ns, phase)
        await self._write_status(
            cluster,
            {"phase": phase, "ready": ready, "replicas": replicas, "message": message},
            cluster.generation,
        )

    async def _bootstrap_sessions(self, cluster: Cluster) -> list[str]:
        """Give every ready agent without a live session one (D6); messages of the failures."""
        if self.bootstrapper is None:
            return []
        async with self.pool.connection() as conn:
            agents = [
                a
                for a in await agents_repo.list_agents(conn, cluster.id)
                if a.ready and a.gone_at is None
            ]
        token = tokens_for(self.settings, cluster.id).auth
        return await self.bootstrapper.bootstrap_many(agents, cluster.spec, token)

    async def _foreign(self, slug: str, objs: list[Obj]) -> list[str]:
        """`foreign_object <kind>/<name>` for each rendered name held by an unlabelled object."""
        found = []
        for obj in objs:
            kind, name = obj["kind"], obj["metadata"]["name"]
            if kind not in GUARDED_KINDS:
                continue  # Secrets cannot be read; the reserved prefix protects them
            existing = await self.kube.get_object(kind, self.settings.namespace, name)
            if existing is not None and not is_owned(existing, slug):
                found.append(f"foreign_object {kind}/{name}")
        return found

    async def _prune(self, slug: str, desired: set[tuple[str, str]]) -> None:
        ns, selector = self.settings.namespace, cluster_labels(slug)
        for kind in PRUNE_ORDER:
            for obj in await self.kube.list_objects(kind, ns, selector):
                name = obj["metadata"]["name"]
                if (kind, name) not in desired and is_owned(obj, slug):
                    await self.kube.delete(kind, ns, name)

    async def _observe(self, cluster: Cluster) -> int:
        """Pods into agents rows; returns the number of ready pods within replicas."""
        slug, replicas = cluster.slug, cluster.spec.replicas
        pods = await self.kube.list_pods(self.settings.namespace, cluster_labels(slug))
        base = prefix(slug) + "-"
        seen: dict[int, Obj] = {}
        for pod in pods:
            name = pod["metadata"]["name"]
            suffix = name[len(base) :] if name.startswith(base) else ""
            if is_owned(pod, slug) and suffix.isdigit():  # check Job pods are not agents
                seen[int(suffix)] = pod
        ready = 0
        async with self.pool.connection() as conn:
            for ordinal, pod in sorted(seen.items()):
                name = pod["metadata"]["name"]
                is_ready = pod_ready(pod) and ordinal < replicas
                ready += is_ready
                await agents_repo.upsert_agent(
                    conn,
                    cluster.id,
                    ordinal,
                    name,
                    pod_url(slug, name, self.settings),
                    (pod.get("status") or {}).get("phase"),
                    is_ready,
                )
            for agent in await agents_repo.list_agents(conn, cluster.id):
                if agent.ordinal in seen or agent.gone_at is not None:
                    continue
                if agent.ordinal >= replicas:
                    await agents_repo.mark_gone(conn, agent.id)
                elif agent.ready or agent.pod_phase is not None:
                    # the pod is being recreated: keep the agent, but never pick it meanwhile
                    await agents_repo.upsert_agent(
                        conn, cluster.id, agent.ordinal, agent.pod_name, agent.url, None, False
                    )
        return ready

    # ------------------------------------------------------------------ a deleting cluster

    async def _discard(self, cluster: Cluster) -> None:
        if not await self.pre_discard(cluster):
            await self._write_status(
                cluster,
                {"phase": "deleting", "message": "waiting for the pre-discard hook"},
                cluster.observed_generation,
            )
            return
        ns, slug = self.settings.namespace, cluster.slug
        await self._prune(slug, set())
        await self.kube.delete("Secret", ns, object_names(slug)["secret"])  # absent is fine

        remaining = await self._remaining(slug)
        if remaining:
            await self._write_status(
                cluster,
                {"phase": "deleting", "message": "waiting for " + ", ".join(remaining)},
                cluster.generation,
            )
            return
        async with self.pool.connection() as conn, conn.transaction():
            await clusters.purge(conn, cluster.id)
            await notify(conn, "status", {"cluster_id": str(cluster.id)})
        log.info("cluster %s discarded", slug)

    async def _remaining(self, slug: str) -> list[str]:
        ns, selector = self.settings.namespace, cluster_labels(slug)
        left = []
        for kind in PRUNE_ORDER:
            for obj in await self.kube.list_objects(kind, ns, selector):
                if is_owned(obj, slug):
                    left.append(f"{kind}/{obj['metadata']['name']}")
        for pod in await self.kube.list_pods(ns, selector):
            if is_owned(pod, slug):
                left.append(f"Pod/{pod['metadata']['name']}")
        return left

    # ------------------------------------------------------------------ restart commands

    async def _run_restarts(self) -> None:
        while True:
            async with self.pool.connection() as conn:
                cmd = await queue.claim_command(conn, ["restart"])
            if cmd is None:
                return
            try:
                status, result = await self._restart(cmd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("restart command %s failed", cmd.id)
                status, result = "failed", {"error": str(exc) or type(exc).__name__}
            async with self.pool.connection() as conn:
                await queue.complete_command(conn, cmd.id, status, result)

    async def _restart(self, cmd: queue.Command) -> tuple[str, dict[str, Any]]:
        async with self.pool.connection() as conn:
            agent = await agents_repo.get_agent(conn, cmd.agent_id)
            cluster = await clusters.get(conn, cmd.cluster_id)
        if agent is None or cluster is None:
            return "failed", {"error": "agent_not_found"}
        if cluster.desired == "deleting":
            return "failed", {"error": "cluster_deleting"}
        if agent.gone_at is not None:
            return "failed", {"error": "agent_gone"}
        ns, slug = self.settings.namespace, cluster.slug
        pods = await self.kube.list_pods(ns, cluster_labels(slug))
        if not any(p["metadata"]["name"] == agent.pod_name and is_owned(p, slug) for p in pods):
            return "failed", {"error": "pod_not_found"}
        await self.kube.delete_pod(ns, agent.pod_name)
        return "done", {"pod": agent.pod_name}

    # ------------------------------------------------------------------ rebootstrap commands

    async def _run_rebootstraps(self) -> None:
        """Clear the session of each agent a rebootstrap command names and bootstrap it again."""
        while True:
            async with self.pool.connection() as conn:
                cmd = await queue.claim_command(conn, ["rebootstrap"])
            if cmd is None:
                return
            try:
                status, result = await self._rebootstrap(cmd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("rebootstrap command %s failed", cmd.id)
                status, result = "failed", {"error": str(exc) or type(exc).__name__}
            async with self.pool.connection() as conn:
                await queue.complete_command(conn, cmd.id, status, result)

    async def _rebootstrap(self, cmd: queue.Command) -> tuple[str, dict[str, Any]]:
        async with self.pool.connection() as conn:
            agent = await agents_repo.get_agent(conn, cmd.agent_id)
            cluster = await clusters.get(conn, cmd.cluster_id)
        if agent is None or cluster is None:
            return "failed", {"error": "agent_not_found"}
        if agent.gone_at is not None:
            return "failed", {"error": "agent_gone"}
        if cluster.desired == "deleting":
            return "failed", {"error": "cluster_deleting"}
        async with self.pool.connection() as conn:
            await agents_repo.clear_session(conn, agent.id)
        agent = replace(agent, session_id=None, session_created_at=None)
        if self.bootstrapper is None or not agent.ready:
            return "done", {"cleared": True, "bootstrapped": False}
        token = tokens_for(self.settings, cluster.id).auth
        problem = await self.bootstrapper.bootstrap(agent, cluster.spec, token)
        if problem:  # the session stays cleared; the next reconcile tick bootstraps it again (D6)
            return "done", {"cleared": True, "bootstrapped": False, "message": problem}
        return "done", {"cleared": True, "bootstrapped": True}

    # ------------------------------------------------------------------ status

    async def _write_status(
        self, cluster: Cluster, fields: dict[str, Any], observed_generation: int
    ) -> None:
        """Merge `fields` into the stored status; a write only when something changed.

        The row is re-read, so keys another step of this tick wrote (the discard hook's) are
        kept. Skipping unchanged writes keeps the reconciler's own NOTIFY from waking it.
        """
        async with self.pool.connection() as conn, conn.transaction():
            current = await clusters.get(conn, cluster.id)
            if current is None:
                return
            status = {**current.status, **fields}
            if status == current.status and observed_generation == current.observed_generation:
                return
            await clusters.set_status(conn, cluster.id, status, observed_generation)
            await notify(conn, "status", {"cluster_id": str(cluster.id)})

    async def _record_error(self, cluster: Cluster, exc: Exception) -> None:
        try:
            await self._write_status(
                cluster,
                {"phase": "error", "message": str(exc) or type(exc).__name__},
                cluster.observed_generation,
            )
        except ClusterNotFound:
            pass
        except Exception:
            log.exception("could not record the error of cluster %s", cluster.slug)
