"""Workspace cache restore and save Jobs (D1, D2, D6).

A cache-enabled session gets Job `agent-<slug>-cache-restore` before init and Job
`agent-<slug>-cache-save` during the discard. Both run `orch cache restore|save` in the configured
cache image, never the session's `spec.image`, and mount only the session's workspace PVC (the save
read-only). The S3 credentials stay in the reconciler: the Job gets exactly one presigned, expiring
URL in `CACHE_URL` and no Secret, `spec.env`, `envFrom` or service account token. The pod is
hardened (uid 1000, non-root, read-only root filesystem with an emptyDir `/tmp` as `HOME`, no
privilege escalation, all capabilities dropped, seccomp RuntimeDefault). The save archives into an
emptyDir `/scratch` sized to `cache_max_bytes`. Like the init Job, neither carries a TTL.

The restore and the save are recorded in `sessions.status.cache.restore|save` (contract
orch-session-status-cache) with the crash-safe shape of the init run: the state is written before
the create and `job` only after it, the outcome is recorded and then the Job deleted. The outcome
is the last line of the pod log, the JSON the CLI prints; a line that cannot be read is a failure,
never a success. The save runs from the discard once the agent pods are gone; whatever its
outcome, it never holds the discard.
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
# A save runs only after a restore that left the workdir trustworthy: it unpacked the archive,
# found none (first use), or emptied the workdir because the archive was unreadable (`reset`, so
# the save overwrites the bad object). Every other outcome, present or future, skips the save.
SAVE_OK_RESTORE_STATES = frozenset({"restored", "miss", "reset"})


def restore_job_name(slug: str) -> str:
    return f"{prefix(slug)}-cache-restore"


def save_job_name(slug: str) -> str:
    return f"{prefix(slug)}-cache-save"


def job_name(slug: str, action: CacheAction) -> str:
    return save_job_name(slug) if action == "save" else restore_job_name(slug)


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
            "name": job_name(slug, action),
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
        if session.spec.cache is None:
            return CacheGate()
        cache = session.status.get("cache")
        restore = (cache or {}).get("restore")
        if restore is None:
            restore = await self._begin(session, "restore")
        elif restore.get("state") == "running":
            restore = await self._observe(session, cache or {}, "restore", restore)
        elif restore.get("job"):
            await self._delete_job(session, "restore")  # crashed between the outcome and the delete
        return CacheGate.of({"restore": restore}, True)

    async def save_step(self, session: Session) -> bool:
        """Advance the one save run by at most one transition; True lets the discard go on.

        Skipped (no Job) unless the restore succeeded (`restored`, `miss` on first use, or `reset`
        after an unreadable archive), so a failed restore never replaces a good archive with a
        partial workdir. A failed or timed-out save is logged and recorded, never held.
        """
        if session.spec.cache is None:
            return True
        cache = session.status.get("cache") or {}
        save = cache.get("save")
        if save is None:
            skip = self._skip_save(cache)
            if skip is not None:
                await self._write(session, cache, "save", {"state": "skipped", "reason": skip})
                log.info("cache save of session %s skipped: %s", session.slug, skip)
                return True
            save = await self._begin(session, "save")
        elif save.get("state") == "running":
            save = await self._observe(session, cache, "save", save)
        elif save.get("job"):
            await self._delete_job(session, "save")  # crashed between the outcome and the delete
        return save.get("state") != "running"

    def _skip_save(self, cache: dict[str, Any]) -> str | None:
        restore = cache.get("restore")
        state = (restore or {}).get("state")
        if restore is None or state == "running":
            return "restore_running"
        if state not in SAVE_OK_RESTORE_STATES:
            if state == "failed":
                head = str(restore.get("reason") or "").split(":")[0].split(" ")[0]
                return head or "restore_failed"
            return f"restore_{state}"
        if not self.settings.cache_available:
            return "not_configured"
        return None

    # ------------------------------------------------------------------ transitions

    async def _begin(self, session: Session, action: CacheAction) -> dict[str, Any]:
        stored = session.status.get("cache") or {}
        key = session.spec.cache.key or "" if session.spec.cache else ""
        cache = {
            **stored,
            "key": stored.get("key") or key,
            "object": stored.get("object") or (object_name(self.settings, key) if key else None),
        }
        run = {
            "state": "running",
            "started_at": (await self._now()).isoformat(),
            "finished_at": None,
            "bytes": None,
            "reason": None,
            "job": None,
        }
        cache = await self._write(session, cache, action, run)
        return await self._start(session, cache, action, run)

    async def _start(
        self, session: Session, cache: dict[str, Any], action: CacheAction, run: dict[str, Any]
    ) -> dict[str, Any]:
        ns, name = self.settings.namespace, job_name(session.slug, action)
        if not self.settings.cache_available:
            return await self._finish(
                session, cache, action, run, "failed", reason="not_configured"
            )
        if not cache.get("key"):
            return await self._finish(session, cache, action, run, "failed", reason="no_cache_key")
        existing = await self.kube.get_job(ns, name)
        if existing is not None:
            if not is_owned(existing, session.slug):
                return await self._finish(
                    session, cache, action, run, "failed", reason="foreign_object"
                )
        else:
            try:
                method = "PUT" if action == "save" else "GET"
                url = presign(self.settings, method, cache["key"])
                await self.kube.create_job(render_cache_job(session, self.settings, action, url))
            except Exception as exc:
                log.exception(
                    "creating the cache %s Job of session %s failed", action, session.slug
                )
                reason = f"job_create_failed: {exc}"[:REASON_MAX]
                return await self._finish(session, cache, action, run, "failed", reason=reason)
        run = {**run, "job": name}
        await self._write(session, cache, action, run)
        return run

    async def _observe(
        self, session: Session, cache: dict[str, Any], action: CacheAction, run: dict[str, Any]
    ) -> dict[str, Any]:
        ns, name = self.settings.namespace, job_name(session.slug, action)
        job = await self.kube.get_job(ns, name)
        if job is None:
            if run.get("job") is None:  # crashed between the running write and the create
                return await self._start(session, cache, action, run)
            return await self._finish(session, cache, action, run, "failed", reason="job_missing")
        if not is_owned(job, session.slug):
            return await self._finish(
                session, cache, action, run, "failed", reason="foreign_object"
            )
        if run.get("job") is None:  # crashed between the create and the job write
            run = {**run, "job": name}
            await self._write(session, cache, action, run)
        status = job.get("status") or {}
        failed = job_condition(job, "Failed")
        if failed is not None and failed.get("reason") == "DeadlineExceeded":
            return await self._outcome(session, cache, action, run, reason="timeout")
        if job_condition(job, "Complete") is not None or status.get("succeeded"):
            return await self._outcome(session, cache, action, run)
        if failed is not None or status.get("failed"):
            return await self._outcome(session, cache, action, run, failed=True)
        started = datetime.fromisoformat(run["started_at"])
        limit = self.settings.cache_timeout_seconds + DEADLINE_GRACE_SECONDS
        if (await self._now() - started).total_seconds() > limit:
            return await self._outcome(session, cache, action, run, reason="timeout")
        return run

    async def _outcome(
        self,
        session: Session,
        cache: dict[str, Any],
        action: CacheAction,
        run: dict[str, Any],
        *,
        reason: str | None = None,
        failed: bool = False,
    ) -> dict[str, Any]:
        """Record the Job's outcome from its pod's log and exit code, then delete it."""
        ns = self.settings.namespace
        pods = [
            p
            for p in await self.kube.list_pods(ns, cache_labels(session.slug, action))
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
            run = await self._finish(session, cache, action, run, "failed", reason=reason)
        else:
            run = await self._finish_from_log(session, cache, action, run, pod, tail, failed)
        await self._delete_job(session, action)
        return run

    async def _finish_from_log(
        self,
        session: Session,
        cache: dict[str, Any],
        action: CacheAction,
        run: dict[str, Any],
        pod: Obj | None,
        tail: str | None,
        failed: bool,
    ) -> dict[str, Any]:
        outcome = parse_outcome(tail)
        kind = outcome["outcome"] if outcome else None
        done = "saved" if action == "save" else "restored"
        if not failed and kind == done:
            nbytes = outcome.get("bytes") if outcome else None
            if isinstance(nbytes, int) and not isinstance(nbytes, bool):
                return await self._finish(session, cache, action, run, done, nbytes=nbytes)
            return await self._finish(session, cache, action, run, "failed", reason="bad_output")
        if not failed and action == "restore" and kind == "miss":
            return await self._finish(session, cache, action, run, "miss")
        if not failed and action == "restore" and kind == "reset" and outcome:
            reason = str(outcome.get("reason") or "extract_failed")[:REASON_MAX]
            return await self._finish(session, cache, action, run, "reset", reason=reason)
        if not failed and action == "save" and kind == "skipped" and outcome:
            reason = str(outcome.get("reason") or "skipped")[:REASON_MAX]
            return await self._finish(session, cache, action, run, "skipped", reason=reason)
        if kind == "failed" and outcome and outcome.get("reason"):
            reason = str(outcome["reason"])[:REASON_MAX]
        elif failed:
            code = container_exit_code(pod) if pod is not None else None
            reason = f"exit {code}" if code is not None else "failed"
        else:  # a finished Job whose last line says nothing we can trust
            reason = "bad_output"
        return await self._finish(session, cache, action, run, "failed", reason=reason)

    async def _finish(
        self,
        session: Session,
        cache: dict[str, Any],
        action: CacheAction,
        run: dict[str, Any],
        state: str,
        *,
        nbytes: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        run = {
            **run,
            "state": state,
            "finished_at": (await self._now()).isoformat(),
            "bytes": nbytes,
            "reason": reason,
        }
        await self._write(session, cache, action, run)
        detail = f": {reason}" if reason else ""
        if action == "save" and state == "failed":
            log.warning(
                "cache save of session %s (key %s) failed%s", session.slug, cache.get("key"), detail
            )
        else:
            log.info("cache %s of session %s %s%s", action, session.slug, state, detail)
        return run

    async def _delete_job(self, session: Session, action: CacheAction) -> None:
        """Delete the session's own cache Job (background propagation); never a foreign one."""
        ns, name = self.settings.namespace, job_name(session.slug, action)
        try:
            job = await self.kube.get_job(ns, name)
            if job is not None and is_owned(job, session.slug):
                await self.kube.delete("Job", ns, name)
        except KubeError as exc:  # the outcome is recorded; a later tick deletes it
            log.warning("deleting the %s Job of session %s failed: %s", action, session.slug, exc)

    # ------------------------------------------------------------------ helpers

    async def _write(
        self, session: Session, cache: dict[str, Any], action: CacheAction, run: dict[str, Any]
    ) -> dict[str, Any]:
        """Write status.cache with this run, keeping the other action's state the row carries."""
        stored = session.status.get("cache") or {}
        merged = {**stored, **{k: v for k, v in cache.items() if k != action}, action: run}
        await self.write_status(session, {"cache": merged}, session.observed_generation)
        return merged

    async def _now(self) -> datetime:
        async with self.pool.connection() as conn:
            return (await (await conn.execute("SELECT now()")).fetchone())[0]
