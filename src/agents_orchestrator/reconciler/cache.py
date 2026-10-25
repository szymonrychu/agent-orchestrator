"""Workspace cache restore and save Jobs (D1, D2, D6).

A cache-enabled session gets Job `agent-<slug>-cache-restore` before init and Job
`agent-<slug>-cache-save` during the discard. Both run `orch cache restore|save` in the configured
cache image, never the session's `spec.image`, and mount only the session's workspace PVC (the save
read-only). The S3 credentials stay in the reconciler: the Job gets exactly one presigned, expiring
URL in `CACHE_URL` and no Secret, `spec.env`, `envFrom` or service account token. The pod is
hardened (uid 1000, non-root, read-only root filesystem with an emptyDir `/tmp` as `HOME`, no
privilege escalation, all capabilities dropped, seccomp RuntimeDefault). The save archives into an
emptyDir `/scratch` sized to `cache_max_bytes`. Like the init Job, neither carries a TTL.

The restore is recorded in `sessions.status.cache.restore` (contract orch-session-status-cache)
with the crash-safe shape of the init run: the state is written before the create and `job` only
after it, the outcome is recorded and then the Job deleted. The outcome is the last line of the
pod log, the JSON the CLI prints; a line that cannot be read is a failure, never a success.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from psycopg_pool import AsyncConnectionPool

from agents_orchestrator.cache.presign import object_name, presign
from agents_orchestrator.db.sessions import Session
from agents_orchestrator.k8s.kube import KubeApi, KubeError, Obj, is_owned
from agents_orchestrator.k8s.labels import object_names, prefix, session_labels
from agents_orchestrator.k8s.render import WORKSPACE_VOLUME, workdir_mount, workdir_path
from agents_orchestrator.reconciler.checks import (
    DEADLINE_GRACE_SECONDS,
    TAIL_LINES,
    container_exit_code,
    job_condition,
    log_tail,
)
from agents_orchestrator.reconciler.init import REASON_MAX, StatusWriter
from agents_orchestrator.settings import Settings

log = logging.getLogger(__name__)

CACHE_LABEL = "agents.szymonrichert.pl/cache"
CacheAction = Literal["restore", "save"]
BACKOFF_LIMIT = 2
TMP_DIR = "/tmp"
SCRATCH_DIR = "/scratch"


def restore_job_name(slug: str) -> str:
    return f"{prefix(slug)}-cache-restore"


def save_job_name(slug: str) -> str:
    return f"{prefix(slug)}-cache-save"


def cache_labels(slug: str, action: CacheAction) -> dict[str, str]:
    return {**session_labels(slug), CACHE_LABEL: action}


def render_cache_job(session: Session, settings: Settings, action: CacheAction, url: str) -> Obj:
    """The restore or save Job of a cache-enabled session, handed the one presigned `url`."""
    if action not in get_args(CacheAction):
        raise ValueError(f"unknown cache action {action!r}")
    spec, slug = session.spec, session.slug
    if spec.cache is None:
        raise ValueError(f"session {slug} has no workspace cache")
    save = action == "save"
    labels = cache_labels(slug, action)
    mounts: list[dict[str, Any]] = [
        {**workdir_mount(spec), "readOnly": True} if save else workdir_mount(spec),
        {"name": "tmp", "mountPath": TMP_DIR},
    ]
    volumes: list[dict[str, Any]] = [
        {
            "name": WORKSPACE_VOLUME,
            "persistentVolumeClaim": {"claimName": object_names(slug)["pvc"]},
        },
        {"name": "tmp", "emptyDir": {}},
    ]
    if save:
        mounts.append({"name": "scratch", "mountPath": SCRATCH_DIR})
        volumes.append(
            {"name": "scratch", "emptyDir": {"sizeLimit": str(settings.cache_max_bytes)}}
        )
    container: dict[str, Any] = {
        "name": "cache",
        "image": settings.cache_image,
        "command": ["orch", "cache", action],
        "env": [
            {"name": "CACHE_URL", "value": url},
            {"name": "CACHE_DIR", "value": workdir_path(spec)},
            {"name": "CACHE_MAX_BYTES", "value": str(settings.cache_max_bytes)},
            {"name": "CACHE_EXCLUDE", "value": json.dumps(spec.cache.exclude)},
            {"name": "CACHE_SCRATCH", "value": SCRATCH_DIR},
            {"name": "CACHE_VERIFY_TLS", "value": str(settings.cache_s3_verify_tls).lower()},
            {"name": "HOME", "value": TMP_DIR},
        ],
        "securityContext": {
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": mounts,
    }
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsUser": 1000,
            "runAsNonRoot": True,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": volumes,
    }
    if settings.image_pull_secret:
        pod_spec = {"imagePullSecrets": [{"name": settings.image_pull_secret}], **pod_spec}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": save_job_name(slug) if save else restore_job_name(slug),
            "namespace": settings.namespace,
            "labels": dict(labels),
        },
        "spec": {
            "backoffLimit": BACKOFF_LIMIT,
            "activeDeadlineSeconds": settings.cache_timeout_seconds,
            "template": {"metadata": {"labels": dict(labels)}, "spec": pod_spec},
        },
    }


@dataclass(frozen=True)
class CacheGate:
    """What the cache restore means for this tick's init run, sessions and phase."""

    hold: bool = False  # neither init nor any session is started; phase pending
    degraded: bool = False  # init and the sessions start; phase degraded
    message: str = ""

    @classmethod
    def of(cls, cache: dict[str, Any] | None, enabled: bool) -> CacheGate:
        if not enabled:
            return cls()
        restore = (cache or {}).get("restore")
        state = (restore or {}).get("state")
        if restore is None or state == "running":
            return cls(hold=True, message="cache restore running")
        if state == "failed":
            reason = restore.get("reason")
            return cls(
                degraded=True,
                message=f"cache restore failed: {reason}" if reason else "cache restore failed",
            )
        return cls()


def parse_outcome(tail: str | None) -> dict[str, Any] | None:
    """The CLI's outcome object: the last non-empty log line as JSON, else None."""
    lines = [ln for ln in (tail or "").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("outcome"), str) else None


class CacheRunner:
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

    async def restore_step(self, session: Session) -> CacheGate:
        """Advance the one restore run by at most one transition; the gate for init and sessions."""
        spec_cache = session.spec.cache
        if spec_cache is None:
            return CacheGate()
        cache = session.status.get("cache")
        restore = (cache or {}).get("restore")
        if restore is None:
            key = spec_cache.key or ""
            cache = {"key": key, "object": object_name(self.settings, key) if key else None}
            restore = {
                "state": "running",
                "started_at": (await self._now()).isoformat(),
                "finished_at": None,
                "bytes": None,
                "reason": None,
                "job": None,
            }
            cache = await self._write(session, cache, restore)
            restore = await self._start(session, cache, restore)
        elif restore.get("state") == "running":
            restore = await self._observe(session, cache or {}, restore)
        elif restore.get("job"):
            await self._delete_job(session)  # crashed between the outcome and the delete
        return CacheGate.of({"restore": restore}, True)

    # ------------------------------------------------------------------ transitions

    async def _start(
        self, session: Session, cache: dict[str, Any], restore: dict[str, Any]
    ) -> dict[str, Any]:
        ns, name = self.settings.namespace, restore_job_name(session.slug)
        if not self.settings.cache_available:
            return await self._finish(session, cache, restore, "failed", reason="not_configured")
        if not cache.get("key"):
            return await self._finish(session, cache, restore, "failed", reason="no_cache_key")
        existing = await self.kube.get_job(ns, name)
        if existing is not None:
            if not is_owned(existing, session.slug):
                return await self._finish(
                    session, cache, restore, "failed", reason="foreign_object"
                )
        else:
            try:
                url = presign(self.settings, "GET", cache["key"])
                await self.kube.create_job(render_cache_job(session, self.settings, "restore", url))
            except Exception as exc:
                log.exception("creating the cache restore Job of session %s failed", session.slug)
                reason = f"job_create_failed: {exc}"[:REASON_MAX]
                return await self._finish(session, cache, restore, "failed", reason=reason)
        restore = {**restore, "job": name}
        await self._write(session, cache, restore)
        return restore

    async def _observe(
        self, session: Session, cache: dict[str, Any], restore: dict[str, Any]
    ) -> dict[str, Any]:
        ns, name = self.settings.namespace, restore_job_name(session.slug)
        job = await self.kube.get_job(ns, name)
        if job is None:
            if restore.get("job") is None:  # crashed between the running write and the create
                return await self._start(session, cache, restore)
            return await self._finish(session, cache, restore, "failed", reason="job_missing")
        if not is_owned(job, session.slug):
            return await self._finish(session, cache, restore, "failed", reason="foreign_object")
        if restore.get("job") is None:  # crashed between the create and the job write
            restore = {**restore, "job": name}
            await self._write(session, cache, restore)
        status = job.get("status") or {}
        failed = job_condition(job, "Failed")
        if failed is not None and failed.get("reason") == "DeadlineExceeded":
            return await self._outcome(session, cache, restore, reason="timeout")
        if job_condition(job, "Complete") is not None or status.get("succeeded"):
            return await self._outcome(session, cache, restore)
        if failed is not None or status.get("failed"):
            return await self._outcome(session, cache, restore, failed=True)
        started = datetime.fromisoformat(restore["started_at"])
        limit = self.settings.cache_timeout_seconds + DEADLINE_GRACE_SECONDS
        if (await self._now() - started).total_seconds() > limit:
            return await self._outcome(session, cache, restore, reason="timeout")
        return restore

    async def _outcome(
        self,
        session: Session,
        cache: dict[str, Any],
        restore: dict[str, Any],
        *,
        reason: str | None = None,
        failed: bool = False,
    ) -> dict[str, Any]:
        """Record the Job's outcome from its pod's log and exit code, then delete it."""
        ns = self.settings.namespace
        pods = [
            p
            for p in await self.kube.list_pods(ns, cache_labels(session.slug, "restore"))
            if is_owned(p, session.slug)
        ]
        pod = pods[-1] if pods else None
        tail: str | None = None
        if pod is not None:
            try:
                tail = log_tail(
                    await self.kube.read_pod_log(ns, pod["metadata"]["name"], TAIL_LINES)
                )
            except KubeError as exc:
                log.warning("reading the cache log of session %s failed: %s", session.slug, exc)
        if reason is not None:
            restore = await self._finish(session, cache, restore, "failed", reason=reason)
        else:
            restore = await self._finish_from_log(session, cache, restore, pod, tail, failed)
        await self._delete_job(session)
        return restore

    async def _finish_from_log(
        self,
        session: Session,
        cache: dict[str, Any],
        restore: dict[str, Any],
        pod: Obj | None,
        tail: str | None,
        failed: bool,
    ) -> dict[str, Any]:
        outcome = parse_outcome(tail)
        kind = outcome["outcome"] if outcome else None
        if not failed and kind == "restored":
            nbytes = outcome.get("bytes") if outcome else None
            if isinstance(nbytes, int) and not isinstance(nbytes, bool):
                return await self._finish(session, cache, restore, "restored", nbytes=nbytes)
            return await self._finish(session, cache, restore, "failed", reason="bad_output")
        if not failed and kind == "miss":
            return await self._finish(session, cache, restore, "miss")
        if kind == "failed" and outcome and outcome.get("reason"):
            reason = str(outcome["reason"])[:REASON_MAX]
        elif failed:
            code = container_exit_code(pod) if pod is not None else None
            reason = f"exit {code}" if code is not None else "failed"
        else:  # a finished Job whose last line says nothing we can trust
            reason = "bad_output"
        return await self._finish(session, cache, restore, "failed", reason=reason)

    async def _finish(
        self,
        session: Session,
        cache: dict[str, Any],
        restore: dict[str, Any],
        state: str,
        *,
        nbytes: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        restore = {
            **restore,
            "state": state,
            "finished_at": (await self._now()).isoformat(),
            "bytes": nbytes,
            "reason": reason,
        }
        await self._write(session, cache, restore)
        log.info(
            "cache restore of session %s %s%s", session.slug, state, f": {reason}" if reason else ""
        )
        return restore

    async def _delete_job(self, session: Session) -> None:
        """Delete the session's own restore Job (background propagation); never a foreign one."""
        ns, name = self.settings.namespace, restore_job_name(session.slug)
        try:
            job = await self.kube.get_job(ns, name)
            if job is not None and is_owned(job, session.slug):
                await self.kube.delete("Job", ns, name)
        except KubeError as exc:  # the outcome is recorded; a later tick deletes it
            log.warning("deleting the restore Job of session %s failed: %s", session.slug, exc)

    # ------------------------------------------------------------------ helpers

    async def _write(
        self, session: Session, cache: dict[str, Any], restore: dict[str, Any]
    ) -> dict[str, Any]:
        """Write status.cache with this restore, keeping any save state the row carries."""
        stored = session.status.get("cache") or {}
        merged = {
            **stored,
            **{k: v for k, v in cache.items() if k != "restore"},
            "restore": restore,
        }
        await self.write_status(session, {"cache": merged}, session.observed_generation)
        return merged

    async def _now(self) -> datetime:
        async with self.pool.connection() as conn:
            return (await (await conn.execute("SELECT now()")).fetchone())[0]
