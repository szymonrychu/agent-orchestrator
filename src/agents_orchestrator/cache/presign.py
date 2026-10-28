"""Presigned cache object URLs, signed in the reconciler (design D2, D8).

The reconciler alone holds the S3 credentials; each cache Job gets one SigV4 URL for one method
(GET to restore, PUT to save) on exactly one object, expiring after the Job's own deadline
(`cache_timeout_seconds`) plus 600 seconds. Presigning is local: no network call is made.

There is one object per target: `<prefix><key>.tar.zst` for a workspace cache key (the workdir)
and `<prefix>sessions/<slug>.tar.zst` for a session's Claude transcripts (D7). A cache key never
contains `/` (`spec.CACHE_KEY_RE`), so the two never name the same object.

A PUT URL also signs `Content-Type` (the one shared `transfer.CONTENT_TYPE` the CLI sends):
Ceph RGW treats a PUT that carries an unsigned Content-Type as anonymous and answers 403. GET signs
only the host. The secret is read from the settings (a `repr=False` field)
and handed straight to botocore; it is never logged and is not part of any URL.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from botocore.config import Config
from botocore.session import get_session

from agents_orchestrator.cache.transfer import CONTENT_TYPE
from agents_orchestrator.settings import Settings

EXPIRY_MARGIN_SECONDS = 600
_OPERATIONS = {"GET": "get_object", "PUT": "put_object"}
CacheTarget = Literal["workdir", "sessions"]


def object_name(settings: Settings, key: str) -> str:
    """The one object per cache key, overwritten on every save."""
    return f"{settings.cache_s3_prefix}{key}.tar.zst"


def sessions_object_name(settings: Settings, slug: str) -> str:
    """The one transcripts object per session slug, overwritten on every save."""
    return f"{settings.cache_s3_prefix}sessions/{slug}.tar.zst"


def target_object_name(settings: Settings, target: CacheTarget, name: str) -> str:
    """The object of `target`: `name` is the cache key (workdir) or the session slug (sessions)."""
    if target == "workdir":
        return object_name(settings, name)
    if target == "sessions":
        return sessions_object_name(settings, name)
    raise ValueError(f"unknown cache target {target!r}")


@lru_cache(maxsize=8)
def _client(
    endpoint: str,
    region: str,
    path_style: bool,
    verify_tls: bool,
    access_key_id: str,
    secret_access_key: str,
) -> Any:
    return get_session().create_client(
        "s3",
        endpoint_url=endpoint or None,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        verify=verify_tls,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if path_style else "virtual"},
        ),
    )


def presign(
    settings: Settings,
    method: Literal["GET", "PUT"],
    name: str,
    target: CacheTarget = "workdir",
) -> str:
    """One SigV4 URL for `method` on the object of `target` and `name`, valid for the Job's life.

    `name` is the workspace cache key for the workdir and the session slug for the sessions.
    """
    operation = _OPERATIONS.get(method)
    if operation is None:
        raise ValueError(f"cache URLs are GET or PUT, not {method!r}")
    key = target_object_name(settings, target, name)
    client = _client(
        settings.cache_s3_endpoint,
        settings.cache_s3_region,
        settings.cache_s3_path_style,
        settings.cache_s3_verify_tls,
        settings.cache_s3_access_key_id,
        settings.cache_s3_secret_access_key,
    )
    params = {"Bucket": settings.cache_s3_bucket, "Key": key}
    if method == "PUT":
        params["ContentType"] = CONTENT_TYPE
    url: str = client.generate_presigned_url(
        operation,
        Params=params,
        ExpiresIn=settings.cache_timeout_seconds + EXPIRY_MARGIN_SECONDS,
        HttpMethod=method,
    )
    return url
