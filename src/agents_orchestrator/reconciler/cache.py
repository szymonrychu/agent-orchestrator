"""Workspace cache restore and save Jobs (D1, D2, D6).

A cache-enabled session gets Job `agent-<slug>-cache-restore` before init and Job
`agent-<slug>-cache-save` during the discard. Both run `orch cache restore|save` in the configured
cache image, never the session's `spec.image`, and mount only the session's workspace PVC (the save
read-only). The S3 credentials stay in the reconciler: the Job gets exactly one presigned, expiring
URL in `CACHE_URL` and no Secret, `spec.env`, `envFrom` or service account token. The pod is
hardened (uid 1000, non-root, read-only root filesystem with an emptyDir `/tmp` as `HOME`, no
privilege escalation, all capabilities dropped, seccomp RuntimeDefault). The save archives into an
emptyDir `/scratch` sized to `cache_max_bytes`. Like the init Job, neither carries a TTL.
"""

from __future__ import annotations

import json
from typing import Any, Literal, get_args

from agents_orchestrator.db.sessions import Session
from agents_orchestrator.k8s.kube import Obj
from agents_orchestrator.k8s.labels import object_names, prefix, session_labels
from agents_orchestrator.k8s.render import WORKSPACE_VOLUME, workdir_mount, workdir_path
from agents_orchestrator.settings import Settings

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
