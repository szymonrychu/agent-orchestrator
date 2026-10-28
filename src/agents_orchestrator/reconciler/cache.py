"""Workspace cache and Claude transcript restore and save Jobs (D1, D2, D6; transcripts D7).

A cache-enabled session gets Job `agent-<slug>-cache-restore` before init and Job
`agent-<slug>-cache-save` during the discard. Both run `orch cache restore|save` in the configured
cache image, never the session's `spec.image`, and mount only the session's workspace PVC (the save
read-only). The same Jobs, runner and outcome handling serve a second target, the session's Claude
transcripts: Jobs `agent-<slug>-sessions-restore|save` mount the PVC subPath `sessions` at
`/sessions` (no excludes) and move object `<cache_s3_prefix>sessions/<slug>.tar.zst`, recorded in
`status.cache.sessions_restore|sessions_save`. A target changes only the mount, `CACHE_DIR`,
`CACHE_EXCLUDE`, the object, the Job name, the cache label and the status key.

The S3 credentials stay in the reconciler: the Job gets exactly one presigned, expiring URL in
`CACHE_URL` and no Secret, `spec.env`, `envFrom` or service account token: it runs as
`ORCH_AGENT_SERVICE_ACCOUNT` with no token mounted, as every Job does (D4). The pod is
hardened (uid 1000, non-root, read-only root filesystem with an emptyDir `/tmp` as `HOME`, no
privilege escalation, all capabilities dropped, seccomp RuntimeDefault). The save archives into an
emptyDir `/scratch` sized to `cache_max_bytes`. Like the init Job, neither carries a TTL.

The workdir restore and save are recorded in `sessions.status.cache.restore|save` (contract
orch-session-status-cache) with the crash-safe shape of the init run: the state is written before
the create and `job` only after it, the outcome is recorded and then the Job deleted. The outcome
is the last line of the pod log, the JSON the CLI prints; a line that cannot be read is a failure,
never a success. The saves run from the discard once the agent pods are gone, the transcripts
after the workdir; whatever their outcome, they never hold the discard.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from psycopg_pool import AsyncConnectionPool

from agents_orchestrator.cache.presign import CacheTarget, object_name, presign
from agents_orchestrator.db.sessions import Session
from agents_orchestrator.k8s.kube import KubeApi, KubeError, Obj, is_owned
from agents_orchestrator.k8s.labels import object_names, prefix, session_labels
from agents_orchestrator.k8s.render import (
    SESSIONS_ROOT,
    WORKSPACE_VOLUME,
    agent_identity,
    workdir_mount,
    workdir_path,
)
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
SESSIONS_SUBPATH = "sessions"  # the PVC subtree holding every agent pod's /sessions
# A save runs only after a restore that left the workdir trustworthy: it unpacked the archive,
# found none (first use), or emptied the workdir because the archive was unreadable (`reset`, so
# the save overwrites the bad object). Every other outcome, present or future, skips the save.
SAVE_OK_RESTORE_STATES = frozenset({"restored", "miss", "reset"})
# A sessions restore skipped because the session's pods existed when the feature arrived leaves
# the transcripts the pods wrote, so they are saved as after a successful restore.
PODS_EXIST = "pods_exist"


@dataclass(frozen=True)
class CacheOp:
    """One action on one target: what names its Job, cache label and status key."""

    action: CacheAction
    target: CacheTarget = "workdir"

    def __post_init__(self) -> None:
        if self.action not in get_args(CacheAction):
            raise ValueError(f"unknown cache action {self.action!r}")
        if self.target not in get_args(CacheTarget):
            raise ValueError(f"unknown cache target {self.target!r}")

    @property
    def sessions(self) -> bool:
        return self.target == "sessions"

    @property
    def status_key(self) -> str:
        return f"sessions_{self.action}" if self.sessions else self.action

    @property
    def label(self) -> str:
        return f"sessions-{self.action}" if self.sessions else self.action

    @property
    def title(self) -> str:
        return f"sessions {self.action}" if self.sessions else f"cache {self.action}"

    def job_name(self, slug: str) -> str:
        return f"{prefix(slug)}-{'sessions' if self.sessions else 'cache'}-{self.action}"


def restore_job_name(slug: str) -> str:
    return CacheOp("restore").job_name(slug)


def save_job_name(slug: str) -> str:
    return CacheOp("save").job_name(slug)


def job_name(slug: str, action: CacheAction, target: CacheTarget = "workdir") -> str:
    return CacheOp(action, target).job_name(slug)


def cache_labels(slug: str, action: CacheAction, target: CacheTarget = "workdir") -> dict[str, str]:
    return {**session_labels(slug), CACHE_LABEL: CacheOp(action, target).label}


def render_cache_job(
    session: Session,
    settings: Settings,
    action: CacheAction,
    url: str,
    target: CacheTarget = "workdir",
) -> Obj:
    """The restore or save Job of `target` of a cache-enabled session, given one presigned `url`."""
    op = CacheOp(action, target)
    spec, slug = session.spec, session.slug
    if spec.cache is None:
        raise ValueError(f"session {slug} has no workspace cache")
    save = action == "save"
    labels = cache_labels(slug, action, target)
    if op.sessions:
        mount: dict[str, Any] = {
            "name": WORKSPACE_VOLUME,
            "mountPath": SESSIONS_ROOT,
            "subPath": SESSIONS_SUBPATH,
        }
        cache_dir, exclude = SESSIONS_ROOT, []
    else:
        mount, cache_dir, exclude = workdir_mount(spec), workdir_path(spec), spec.cache.exclude
    mounts: list[dict[str, Any]] = [
        {**mount, "readOnly": True} if save else mount,
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
            {"name": "CACHE_DIR", "value": cache_dir},
            {"name": "CACHE_MAX_BYTES", "value": str(settings.cache_max_bytes)},
            {"name": "CACHE_EXCLUDE", "value": json.dumps(exclude)},
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
        **agent_identity(settings),
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
            "name": op.job_name(slug),
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


def _pods_wrote_sessions(restore: dict[str, Any] | None, target: CacheTarget) -> bool:
    """No sessions restore ran: the session predates it, or its pods existed (`pods_exist`)."""
    if target != "sessions":
        return False
    return restore is None or (
        restore.get("state") == "skipped" and restore.get("reason") == PODS_EXIST
    )


def _restore_skip(restore: dict[str, Any] | None) -> str | None:
    """Why a save may not follow this restore run, or None when it may."""
    state = (restore or {}).get("state")
    if restore is None or state == "running":
        return "restore_running"
    if state in SAVE_OK_RESTORE_STATES:
        return None
    if state == "failed":
        head = str(restore.get("reason") or "").split(":")[0].split(" ")[0]
        return head or "restore_failed"
    return f"restore_{state}"


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
        op = CacheOp("restore")
        cache = session.status.get("cache")
        restore = (cache or {}).get(op.status_key)
        if restore is None:
            restore = await self._begin(session, op)
        elif restore.get("state") == "running":
            restore = await self._observe(session, cache or {}, op, restore)
        elif restore.get("job"):
            await self._delete_job(session, op)  # crashed between the outcome and the delete
        return CacheGate.of({"restore": restore}, True)

    async def save_step(self, session: Session, target: CacheTarget = "workdir") -> bool:
        """Advance the one save run of `target` by at most one transition; True lets the discard on.

        Skipped (no Job) unless the target's restore succeeded (`restored`, `miss` on first use, or
        `reset` after an unreadable archive), so a failed restore never replaces a good archive
        with partial content. The sessions save also runs without a sessions restore record (a
        session older than the transcript backup) and after one skipped with `pods_exist`. A
        failed or timed-out save is logged and recorded, never held.
        """
        if session.spec.cache is None:
            return True
        op = CacheOp("save", target)
        cache = session.status.get("cache") or {}
        save = cache.get(op.status_key)
        if save is None:
            skip = self._skip_save(cache, target)
            if skip is not None:
                await self._write(session, cache, op, {"state": "skipped", "reason": skip})
                log.info("%s of session %s skipped: %s", op.title, session.slug, skip)
                return True
            save = await self._begin(session, op)
        elif save.get("state") == "running":
            save = await self._observe(session, cache, op, save)
        elif save.get("job"):
            await self._delete_job(session, op)  # crashed between the outcome and the delete
        return save.get("state") != "running"

    def save_owed(self, session: Session, target: CacheTarget = "workdir") -> bool:
        """`spec.cache` is set and the save of `target` has no outcome yet."""
        save = (session.status.get("cache") or {}).get(CacheOp("save", target).status_key)
        return session.spec.cache is not None and (save is None or save.get("state") == "running")

    def _skip_save(self, cache: dict[str, Any], target: CacheTarget) -> str | None:
        restore = cache.get(CacheOp("restore", target).status_key)
        skip = None if _pods_wrote_sessions(restore, target) else _restore_skip(restore)
        if skip is None and not self.settings.cache_available:
            return "not_configured"
        return skip

    # ------------------------------------------------------------------ transitions

    async def _begin(self, session: Session, op: CacheOp) -> dict[str, Any]:
        cache = dict(session.status.get("cache") or {})
        if not op.sessions:
            key = session.spec.cache.key or "" if session.spec.cache else ""
            cache["key"] = cache.get("key") or key
            cache["object"] = cache.get("object") or (
                object_name(self.settings, key) if key else None
            )
        run = {
            "state": "running",
            "started_at": (await self._now()).isoformat(),
            "finished_at": None,
            "bytes": None,
            "reason": None,
            "job": None,
        }
        cache = await self._write(session, cache, op, run)
        return await self._start(session, cache, op, run)

    async def _start(
        self, session: Session, cache: dict[str, Any], op: CacheOp, run: dict[str, Any]
    ) -> dict[str, Any]:
        ns, name = self.settings.namespace, op.job_name(session.slug)
        if not self.settings.cache_available:
            return await self._finish(session, cache, op, run, "failed", reason="not_configured")
        if not op.sessions and not cache.get("key"):
            return await self._finish(session, cache, op, run, "failed", reason="no_cache_key")
        existing = await self.kube.get_job(ns, name)
        if existing is not None:
            if not is_owned(existing, session.slug):
                return await self._finish(
                    session, cache, op, run, "failed", reason="foreign_object"
                )
        else:
            try:
                method = "PUT" if op.action == "save" else "GET"
                obj = session.slug if op.sessions else cache["key"]
                url = presign(self.settings, method, obj, op.target)
                await self.kube.create_job(
                    render_cache_job(session, self.settings, op.action, url, op.target)
                )
            except Exception as exc:
                log.exception("creating the %s Job of session %s failed", op.title, session.slug)
                reason = f"job_create_failed: {exc}"[:REASON_MAX]
                return await self._finish(session, cache, op, run, "failed", reason=reason)
        run = {**run, "job": name}
        await self._write(session, cache, op, run)
        return run

    async def _observe(
        self, session: Session, cache: dict[str, Any], op: CacheOp, run: dict[str, Any]
    ) -> dict[str, Any]:
        ns, name = self.settings.namespace, op.job_name(session.slug)
        job = await self.kube.get_job(ns, name)
        if job is None:
            if run.get("job") is None:  # crashed between the running write and the create
                return await self._start(session, cache, op, run)
            return await self._finish(session, cache, op, run, "failed", reason="job_missing")
        if not is_owned(job, session.slug):
            return await self._finish(session, cache, op, run, "failed", reason="foreign_object")
        if run.get("job") is None:  # crashed between the create and the job write
            run = {**run, "job": name}
            await self._write(session, cache, op, run)
        status = job.get("status") or {}
        failed = job_condition(job, "Failed")
        if failed is not None and failed.get("reason") == "DeadlineExceeded":
            return await self._outcome(session, cache, op, run, reason="timeout")
        if job_condition(job, "Complete") is not None or status.get("succeeded"):
            return await self._outcome(session, cache, op, run)
        if failed is not None or status.get("failed"):
            return await self._outcome(session, cache, op, run, failed=True)
        started = datetime.fromisoformat(run["started_at"])
        limit = self.settings.cache_timeout_seconds + DEADLINE_GRACE_SECONDS
        if (await self._now() - started).total_seconds() > limit:
            return await self._outcome(session, cache, op, run, reason="timeout")
        return run

    async def _outcome(
        self,
        session: Session,
        cache: dict[str, Any],
        op: CacheOp,
        run: dict[str, Any],
        *,
        reason: str | None = None,
        failed: bool = False,
    ) -> dict[str, Any]:
        """Record the Job's outcome from its pod's log and exit code, then delete it."""
        ns = self.settings.namespace
        pods = [
            p
            for p in await self.kube.list_pods(ns, cache_labels(session.slug, op.action, op.target))
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
                log.warning(
                    "reading the %s log of session %s failed: %s", op.title, session.slug, exc
                )
        if reason is not None:
            run = await self._finish(session, cache, op, run, "failed", reason=reason)
        else:
            run = await self._finish_from_log(session, cache, op, run, pod, tail, failed)
        await self._delete_job(session, op)
        return run

    async def _finish_from_log(
        self,
        session: Session,
        cache: dict[str, Any],
        op: CacheOp,
        run: dict[str, Any],
        pod: Obj | None,
        tail: str | None,
        failed: bool,
    ) -> dict[str, Any]:
        outcome = parse_outcome(tail)
        kind = outcome["outcome"] if outcome else None
        action = op.action
        done = "saved" if action == "save" else "restored"
        if not failed and kind == done:
            nbytes = outcome.get("bytes") if outcome else None
            if isinstance(nbytes, int) and not isinstance(nbytes, bool):
                return await self._finish(session, cache, op, run, done, nbytes=nbytes)
            return await self._finish(session, cache, op, run, "failed", reason="bad_output")
        if not failed and action == "restore" and kind == "miss":
            return await self._finish(session, cache, op, run, "miss")
        if not failed and action == "restore" and kind == "reset" and outcome:
            reason = str(outcome.get("reason") or "extract_failed")[:REASON_MAX]
            return await self._finish(session, cache, op, run, "reset", reason=reason)
        if not failed and action == "save" and kind == "skipped" and outcome:
            reason = str(outcome.get("reason") or "skipped")[:REASON_MAX]
            return await self._finish(session, cache, op, run, "skipped", reason=reason)
        if kind == "failed" and outcome and outcome.get("reason"):
            reason = str(outcome["reason"])[:REASON_MAX]
        elif failed:
            code = container_exit_code(pod) if pod is not None else None
            reason = f"exit {code}" if code is not None else "failed"
        else:  # a finished Job whose last line says nothing we can trust
            reason = "bad_output"
        return await self._finish(session, cache, op, run, "failed", reason=reason)

    async def _finish(
        self,
        session: Session,
        cache: dict[str, Any],
        op: CacheOp,
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
        await self._write(session, cache, op, run)
        detail = f": {reason}" if reason else ""
        if op.action == "save" and state == "failed":
            subject = "" if op.sessions else f" (key {cache.get('key')})"
            log.warning("%s of session %s%s failed%s", op.title, session.slug, subject, detail)
        else:
            log.info("%s of session %s %s%s", op.title, session.slug, state, detail)
        return run

    async def _delete_job(self, session: Session, op: CacheOp) -> None:
        """Delete the session's own cache Job (background propagation); never a foreign one."""
        ns, name = self.settings.namespace, op.job_name(session.slug)
        try:
            job = await self.kube.get_job(ns, name)
            if job is not None and is_owned(job, session.slug):
                await self.kube.delete("Job", ns, name)
        except KubeError as exc:  # the outcome is recorded; a later tick deletes it
            log.warning("deleting the %s Job of session %s failed: %s", op.title, session.slug, exc)

    # ------------------------------------------------------------------ helpers

    async def _write(
        self, session: Session, cache: dict[str, Any], op: CacheOp, run: dict[str, Any]
    ) -> dict[str, Any]:
        """Write status.cache with this run, keeping the other runs' state the row carries."""
        stored = session.status.get("cache") or {}
        key = op.status_key
        merged = {**stored, **{k: v for k, v in cache.items() if k != key}, key: run}
        await self.write_status(session, {"cache": merged}, session.observed_generation)
        return merged

    async def _now(self) -> datetime:
        async with self.pool.connection() as conn:
            return (await (await conn.execute("SELECT now()")).fetchone())[0]
