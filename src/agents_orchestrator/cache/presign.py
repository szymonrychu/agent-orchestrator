"""Presigned cache object URLs, signed in the reconciler (design D2, D8).

The reconciler alone holds the S3 credentials; each cache Job gets one SigV4 URL for one method
(GET to restore, PUT to save) on exactly one object, expiring after the Job's own deadline
(`cache_timeout_seconds`) plus 600 seconds. Presigning is local: no network call is made.

Only the host is signed, so the transfer CLI's `Content-Type` and `Content-Length` on the PUT do
not need to match anything here. The secret is read from the settings (a `repr=False` field)
and handed straight to botocore; it is never logged and is not part of any URL.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from botocore.config import Config
from botocore.session import get_session

from agents_orchestrator.settings import Settings

EXPIRY_MARGIN_SECONDS = 600
_OPERATIONS = {"GET": "get_object", "PUT": "put_object"}


def object_name(settings: Settings, key: str) -> str:
    """The one object per cache key, overwritten on every save."""
    return f"{settings.cache_s3_prefix}{key}.tar.zst"


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


def presign(settings: Settings, method: Literal["GET", "PUT"], key: str) -> str:
    """One SigV4 URL for `method` on `object_name(settings, key)`, valid for the Job's lifetime."""
    operation = _OPERATIONS.get(method)
    if operation is None:
        raise ValueError(f"cache URLs are GET or PUT, not {method!r}")
    client = _client(
        settings.cache_s3_endpoint,
        settings.cache_s3_region,
        settings.cache_s3_path_style,
        settings.cache_s3_verify_tls,
        settings.cache_s3_access_key_id,
        settings.cache_s3_secret_access_key,
    )
    url: str = client.generate_presigned_url(
        operation,
        Params={"Bucket": settings.cache_s3_bucket, "Key": object_name(settings, key)},
        ExpiresIn=settings.cache_timeout_seconds + EXPIRY_MARGIN_SECONDS,
        HttpMethod=method,
    )
    return url
