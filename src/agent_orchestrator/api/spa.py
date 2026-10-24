"""Static serving of the built web UI with client-route fallback (D12)."""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

INDEX_CACHE = "no-cache"
ASSET_CACHE = "public, max-age=31536000, immutable"


class SpaStaticFiles(StaticFiles):
    """index.html is never cached, hashed assets are immutable, unknown
    non-API paths fall back to index.html, unknown /api/ paths stay 404, and any
    method other than GET/HEAD is 405 (the mount shadows partial route matches)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        if scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or path.startswith("assets/"):
                raise
            path = "index.html"
            response = await super().get_response(path, scope)
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = ASSET_CACHE
        elif response.status_code < 400:
            response.headers["Cache-Control"] = INDEX_CACHE
        return response
