"""Init scripts run once per cluster, as one Job, recorded in clusters.status.init (D2, D3).

The first converge of a cluster decides, once: no `spec.init_scripts` writes `skipped`, a
non-empty list writes `running` and creates Job `agent-<slug>-init`. Once `status.init` exists no
other Job is ever started for the cluster, whatever later edits do to the scripts: they apply to
the next cluster. Crash safety follows the checks: the state is written before the create and
`job` only after it succeeds, so a later tick tells a crash before the create (create it) from a
Job that vanished (`job_missing`). The outcome is recorded, then the Job is deleted; it carries
no TTL, so an offline reconciler still finds the outcome. A converge never prunes it (PRUNE_ORDER
has no Job) and a discard deletes it with the check Jobs.

The Job runs the scripts in the shared workdir and leaves them there for the agents
(contract orch-init-workdir-files): `<workdir>/.agent-init/init.sh` (the scripts as run, under a
header), `init.log` (their stdout and stderr) and a `.gitignore` of `*`. Only the workdir is
shared: what a script installs elsewhere in the Job pod does not reach the agent pods.

`status.init` (contract orch-cluster-status-init) is `{state, scripts_hash, started_at,
finished_at, exit_code, log_tail, reason, job}`, `state` one of skipped, running, succeeded,
failed; `skipped` carries only `state` and `scripts_hash`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from agents_orchestrator.db.clusters import Cluster
from agents_orchestrator.k8s.kube import KubeApi, KubeError, Obj, is_owned
from agents_orchestrator.k8s.labels import cluster_labels, object_names, prefix
from agents_orchestrator.k8s.render import WORKSPACE_VOLUME, workdir_mount, workdir_path
from agents_orchestrator.reconciler.checks import (
    BACKOFF_LIMIT,
    DEADLINE_GRACE_SECONDS,
    DEADLINE_SECONDS,
    TAIL_LINES,
    container_exit_code,
    job_condition,
    log_tail,
)
from agents_orchestrator.settings import Settings
from agents_orchestrator.spec import INIT_DIR, init_scripts_hash

log = logging.getLogger(__name__)

INIT_LABEL = "agents.szymonrichert.pl/init"
WORKDIR_ENV = "AGENT_INIT_WORKDIR"
INIT_SH = "init.sh"
INIT_LOG = "init.log"
REASON_MAX = 2000
# Linux caps one argv string at 128 KiB; INIT_SH is passed in chunks of at most this many
# characters (at most 64 KiB of UTF-8 each). Below it the command has exactly one INIT_SH arg.
ARG_CHUNK_CHARS = 16384

# $1... is INIT_SH (in chunks); the Job's exit code is the script's.
# The workdir is created here, as the pod user: a container `workingDir` that is missing is created
# by the runtime as root, and the uid 1000 script could not write in it.
WRAPPER = f"""set -e
mkdir -p "${WORKDIR_ENV}"
cd "${WORKDIR_ENV}"
d={INIT_DIR}
mkdir -p "$d"
printf '*\\n' > "$d/.gitignore"
printf '%s' "$@" > "$d/{INIT_SH}"
code=0
sh -eu "$d/{INIT_SH}" > "$d/{INIT_LOG}" 2>&1 || code=$?
cat "$d/{INIT_LOG}"
exit "$code"
"""

StatusWriter = Callable[[Cluster, dict[str, Any], int], Awaitable[None]]
"""(cluster, fields, observed_generation): the reconciler's merging status write."""


def init_job_name(slug: str) -> str:
    return f"{prefix(slug)}-init"


def init_labels(slug: str) -> dict[str, str]:
    return {**cluster_labels(slug), INIT_LABEL: "true"}


def render_init_sh(slug: str, scripts: list[str]) -> str:
    """The scripts as one `sh -eu` file, each under a `# --- script <n> ---` line."""
    lines = [
        f"# init scripts of cluster {slug}",
        "# run once at cluster start by agents-orchestrator; edits through the init scripts API"
        " or the orchestrator_init_scripts_edit tool apply to the next cluster",
    ]
    for n, script in enumerate(scripts, 1):
        lines += [f"# --- script {n} ---", script.rstrip("\n")]
    return "\n".join(lines) + "\n"


def render_init_job(cluster: Cluster, settings: Settings) -> Obj:
    spec, slug = cluster.spec, cluster.slug
    labels = init_labels(slug)
    init_sh = render_init_sh(slug, spec.init_scripts)
    chunks = [init_sh[i : i + ARG_CHUNK_CHARS] for i in range(0, len(init_sh), ARG_CHUNK_CHARS)]
    container: dict[str, Any] = {
        "name": "init",
        "image": spec.image or settings.agent_image,
        "command": ["sh", "-c", WRAPPER, "agent-init", *chunks],
        "env": [
            *({"name": k, "value": v} for k, v in sorted(spec.env.items())),
            {"name": WORKDIR_ENV, "value": workdir_path(spec)},
        ],
        "volumeMounts": [workdir_mount(spec)],
    }
    if spec.resources is not None:  # as the agent container
        resources = {k: v for k, v in spec.resources.model_dump().items() if v}
        if resources:
            container["resources"] = resources
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "securityContext": {"runAsUser": 1000, "fsGroup": 1000},
        "containers": [container],
        "volumes": [
            {
                "name": WORKSPACE_VOLUME,
                "persistentVolumeClaim": {"claimName": object_names(slug)["pvc"]},
            }
        ],
    }
    if settings.image_pull_secret:
        pod_spec = {"imagePullSecrets": [{"name": settings.image_pull_secret}], **pod_spec}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": init_job_name(slug),
            "namespace": settings.namespace,
            "labels": dict(labels),
        },
        "spec": {
            "backoffLimit": BACKOFF_LIMIT,
            "activeDeadlineSeconds": DEADLINE_SECONDS,
            "template": {"metadata": {"labels": dict(labels)}, "spec": pod_spec},
        },
    }


@dataclass(frozen=True)
class InitGate:
    """What the init run means for this tick's sessions and phase."""

    hold: bool = False  # no session is bootstrapped; phase pending
    degraded: bool = False  # sessions start; phase degraded
    message: str = ""

    @classmethod
    def of(cls, init: dict[str, Any] | None, scripts: list[str]) -> InitGate:
        state = (init or {}).get("state")
        if state == "running" or (init is None and scripts):
            return cls(hold=True, message="init running")
        if state == "failed":
            assert init is not None
            if init.get("exit_code") is not None:
                return cls(degraded=True, message=f"init failed (exit {init['exit_code']})")
            reason = init.get("reason")
            return cls(degraded=True, message=f"init failed: {reason}" if reason else "init failed")
        return cls()


class InitRunner:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        kube: KubeApi,
        settings: Settings,
        *,
        write_status: StatusWriter,
    ) -> None:
        self.pool, self.kube, self.settings = pool, kube, settings
        self.write_status = write_status

    async def step(self, cluster: Cluster) -> InitGate:
        """Advance the cluster's one init run by at most one transition; the session gate."""
        init = cluster.status.get("init")
        if init is None:
            if not cluster.spec.init_scripts:
                init = {"state": "skipped", "scripts_hash": init_scripts_hash([])}
                await self._write(cluster, init)
            else:
                init = {
                    "state": "running",
                    "scripts_hash": init_scripts_hash(cluster.spec.init_scripts),
                    "started_at": (await self._now()).isoformat(),
                    "finished_at": None,
                    "exit_code": None,
                    "log_tail": None,
                    "reason": None,
                    "job": None,
                }
                await self._write(cluster, init)
                init = await self._start(cluster, init)
        elif init.get("state") == "running":
            init = await self._observe(cluster, init)
        elif init.get("state") in ("succeeded", "failed") and init.get("job"):
            await self._delete_job(cluster)  # crashed between the outcome and the delete
        return InitGate.of(init, cluster.spec.init_scripts)

    # ------------------------------------------------------------------ transitions

    async def _start(self, cluster: Cluster, init: dict[str, Any]) -> dict[str, Any]:
        ns, name = self.settings.namespace, init_job_name(cluster.slug)
        existing = await self.kube.get_job(ns, name)
        if existing is not None:
            if not is_owned(existing, cluster.slug):
                return await self._finish(cluster, init, "failed", reason="foreign_object")
        else:
            try:
                await self.kube.create_job(render_init_job(cluster, self.settings))
            except Exception as exc:
                log.exception("creating the init Job of cluster %s failed", cluster.slug)
                reason = f"job_create_failed: {exc}"[:REASON_MAX]
                return await self._finish(cluster, init, "failed", reason=reason)
            # what was rendered is what runs: a create retried after an edit runs the edit
            init = {**init, "scripts_hash": init_scripts_hash(cluster.spec.init_scripts)}
        init = {**init, "job": name}
        await self._write(cluster, init)
        return init

    async def _observe(self, cluster: Cluster, init: dict[str, Any]) -> dict[str, Any]:
        ns, name = self.settings.namespace, init_job_name(cluster.slug)
        job = await self.kube.get_job(ns, name)
        if job is None:
            if init.get("job") is None:  # crashed between the running write and the create
                return await self._start(cluster, init)
            return await self._finish(cluster, init, "failed", reason="job_missing")
        if not is_owned(job, cluster.slug):
            return await self._finish(cluster, init, "failed", reason="foreign_object")
        if init.get("job") is None:  # crashed between the create and the job write
            init = {**init, "job": name}
            await self._write(cluster, init)
        status = job.get("status") or {}
        failed = job_condition(job, "Failed")
        if failed is not None and failed.get("reason") == "DeadlineExceeded":
            return await self._outcome(cluster, init, "failed", reason="timeout")
        if job_condition(job, "Complete") is not None or status.get("succeeded"):
            return await self._outcome(cluster, init, "succeeded")
        if failed is not None or status.get("failed"):
            return await self._outcome(cluster, init, "failed")
        started = datetime.fromisoformat(init["started_at"])
        if (
            await self._now() - started
        ).total_seconds() > DEADLINE_SECONDS + DEADLINE_GRACE_SECONDS:
            return await self._outcome(cluster, init, "failed", reason="timeout")
        return init

    async def _outcome(
        self, cluster: Cluster, init: dict[str, Any], state: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Record the Job's outcome with its pod's exit code and log tail, then delete it."""
        ns = self.settings.namespace
        pods = [
            p
            for p in await self.kube.list_pods(ns, init_labels(cluster.slug))
            if is_owned(p, cluster.slug)
        ]
        pod = pods[-1] if pods else None
        exit_code: int | None = 0 if state == "succeeded" else None
        tail: str | None = None
        if pod is not None:
            if state == "failed" and reason is None:
                exit_code = container_exit_code(pod)
            try:
                tail = log_tail(
                    await self.kube.read_pod_log(ns, pod["metadata"]["name"], TAIL_LINES)
                )
            except KubeError as exc:
                log.warning("reading the init log of cluster %s failed: %s", cluster.slug, exc)
        init = await self._finish(
            cluster, init, state, exit_code=exit_code, log_tail=tail, reason=reason
        )
        await self._delete_job(cluster)
        return init

    async def _finish(
        self,
        cluster: Cluster,
        init: dict[str, Any],
        state: str,
        *,
        exit_code: int | None = None,
        log_tail: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        init = {
            **init,
            "state": state,
            "finished_at": (await self._now()).isoformat(),
            "exit_code": exit_code,
            "log_tail": log_tail,
            "reason": reason,
        }
        await self._write(cluster, init)
        log.info("init of cluster %s %s%s", cluster.slug, state, f": {reason}" if reason else "")
        return init

    async def _delete_job(self, cluster: Cluster) -> None:
        """Delete the cluster's own init Job (background propagation); never a foreign one."""
        ns, name = self.settings.namespace, init_job_name(cluster.slug)
        try:
            job = await self.kube.get_job(ns, name)
            if job is not None and is_owned(job, cluster.slug):
                await self.kube.delete("Job", ns, name)
        except KubeError as exc:  # the outcome is recorded; a later tick deletes it
            log.warning("deleting the init Job of cluster %s failed: %s", cluster.slug, exc)

    # ------------------------------------------------------------------ helpers

    async def _write(self, cluster: Cluster, init: dict[str, Any]) -> None:
        await self.write_status(cluster, {"init": init}, cluster.observed_generation)

    async def _now(self) -> datetime:
        async with self.pool.connection() as conn:
            return (await (await conn.execute("SELECT now()")).fetchone())[0]
