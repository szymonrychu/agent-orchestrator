"""Deterministic OpenAPI export: `python -m agents_orchestrator.api.openapi > openapi.json`."""

from __future__ import annotations

import json

from agents_orchestrator.api.app import create_app
from agents_orchestrator.settings import Settings


def export() -> str:
    """The app's OpenAPI document as JSON with sorted keys and a trailing newline."""
    app = create_app(Settings(database_url="postgresql://openapi/export"))
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(), end="")
