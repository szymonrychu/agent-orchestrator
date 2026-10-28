"""Claude transcripts (/sessions) are saved to S3 at discard, after the workdir (D7, G6)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_cache_jobs_render import (
    ACCESS_KEY,
    CACHE_IMAGE,
    SECRET_KEY,
    container_of,
    make_session,
    make_settings,
    pod_of,
    walk,
)

from agents_orchestrator.cache import presign as presign_module
from agents_orchestrator.cache.presign import object_name, presign, sessions_object_name
from agents_orchestrator.k8s import labels, render
from agents_orchestrator.k8s.labels import object_names
from agents_orchestrator.reconciler import cache as cache_mod
from agents_orchestrator.reconciler.cache import CACHE_LABEL, job_name, render_cache_job
from agents_orchestrator.spec import CACHE_KEY_RE

SESSIONS_SAVE = job_name("alpha", "save", "sessions")


# ---------------------------------------------------------------------- the Job


def _env(job: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container_of(job)["env"]}


def test_sessions_save_job_render():
    s, settings = make_session(), make_settings()
    url = presign(settings, "PUT", "alpha", "sessions")
    job = render_cache_job(s, settings, "save", url, target="sessions")
    workdir = render_cache_job(s, settings, "save", url)

    assert job["metadata"]["name"] == SESSIONS_SAVE == "agent-alpha-sessions-save"
    want = {**labels.session_labels("alpha"), CACHE_LABEL: "sessions-save"}
    assert job["metadata"]["labels"] == want
    assert job["spec"]["template"]["metadata"]["labels"] == want
    c = container_of(job)
    assert c["image"] == CACHE_IMAGE and c["command"] == ["orch", "cache", "save"]

    # Only the PVC subPath `sessions`, read-only, plus the two emptyDirs.
    pvc = object_names("alpha")["pvc"]
    assert c["volumeMounts"] == [
        {
            "name": render.WORKSPACE_VOLUME,
            "mountPath": render.SESSIONS_ROOT,
            "subPath": "sessions",
            "readOnly": True,
        },
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "scratch", "mountPath": "/scratch"},
    ]
    assert [v for k, v in walk(job) if k == "persistentVolumeClaim"] == [{"claimName": pvc}]
    assert pod_of(job)["volumes"] == pod_of(workdir)["volumes"]

    env = _env(job)
    assert env["CACHE_DIR"] == render.SESSIONS_ROOT == "/sessions"
    assert env["CACHE_EXCLUDE"] == "[]"
    assert {k: v for k, v in env.items() if k not in ("CACHE_DIR", "CACHE_EXCLUDE")} == {
        k: v for k, v in _env(workdir).items() if k not in ("CACHE_DIR", "CACHE_EXCLUDE")
    }

    # Exactly one URL: a presigned PUT of <prefix>sessions/<slug>.tar.zst.
    assert [v for v in env.values() if "://" in v] == [url]
    parts = urlsplit(env["CACHE_URL"])
    assert parts.path == "/workspace-cache/sessions/alpha.tar.zst"
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query["X-Amz-SignedHeaders"] == "content-type;host"  # PUT signs Content-Type
    assert "X-Amz-Signature" in query

    # No credential, no spec.env, no token: the workdir Job's hardening, field for field. The
    # access key id is in the signed URL's X-Amz-Credential by design; nowhere else.
    dumped = json.dumps(job).replace(url, "")
    for leaked in (ACCESS_KEY, SECRET_KEY, "SPEC_VAR", "AWS_"):
        assert leaked not in dumped
    assert SECRET_KEY not in url
    keys = [k for k, _ in walk(job)]
    assert "secretKeyRef" not in keys and "envFrom" not in keys and "secret" not in keys
    pod, wpod = pod_of(job), pod_of(workdir)
    assert {k: v for k, v in pod.items() if k not in ("containers", "volumes")} == {
        k: v for k, v in wpod.items() if k not in ("containers", "volumes")
    }
    assert pod["automountServiceAccountToken"] is False
    assert c["securityContext"] == container_of(workdir)["securityContext"]
    assert job["spec"] | {"template": None} == workdir["spec"] | {"template": None}

    unknown: Any = "home"
    with pytest.raises(ValueError):
        render_cache_job(s, settings, "save", url, target=unknown)


def test_sessions_save_respects_max_bytes():
    s, settings = make_session(), make_settings(cache_max_bytes=12345)
    job = render_cache_job(s, settings, "save", "https://u", target="sessions")
    workdir = render_cache_job(s, settings, "save", "https://u")
    env = _env(job)
    assert env["CACHE_MAX_BYTES"] == "12345" == _env(workdir)["CACHE_MAX_BYTES"]
    assert env["CACHE_SCRATCH"] == "/scratch"
    scratch = [v for v in pod_of(job)["volumes"] if v["name"] == "scratch"]
    assert scratch == [{"name": "scratch", "emptyDir": {"sizeLimit": "12345"}}]
    assert {"name": "scratch", "mountPath": "/scratch"} in container_of(job)["volumeMounts"]


def test_sessions_object_name_distinct_from_cache_keys():
    s = make_settings()
    assert sessions_object_name(s, "alpha") == "workspace-cache/sessions/alpha.tar.zst"
    keys = ["alpha", "sessions", "sessions.alpha", "sessions-alpha", "a", "x" * 63, "a.b_c-d"]
    slugs = ["alpha", "sessions", "a", "s" * 40, "a-b"]
    for key in keys:
        assert CACHE_KEY_RE.fullmatch(key)
        assert "/" not in object_name(s, key)[len(s.cache_s3_prefix) :]
        for slug in slugs:
            assert object_name(s, key) != sessions_object_name(s, slug)

    # One presign function and one S3 client sign both objects.
    assert cache_mod.presign is presign_module.presign
    presign_module._client.cache_clear()
    workdir_url = presign(s, "PUT", "alpha")
    sessions_url = presign(s, "PUT", "alpha", "sessions")
    assert presign_module._client.cache_info().misses == 1
    assert urlsplit(workdir_url).path == "/workspace-cache/alpha.tar.zst"
    assert urlsplit(sessions_url).path == "/workspace-cache/sessions/alpha.tar.zst"
    unknown: Any = "home"
    with pytest.raises(ValueError):
        presign(s, "PUT", "alpha", unknown)
