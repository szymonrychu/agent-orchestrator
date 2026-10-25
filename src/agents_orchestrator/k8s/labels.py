"""Labels, selectors and the reserved object-name prefix of one session (D26)."""

from __future__ import annotations

SESSION_LABEL = "agents.szymonrichert.pl/session"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY = "agents-orchestrator"


def prefix(slug: str) -> str:
    return f"agent-{slug}"


def session_labels(slug: str) -> dict[str, str]:
    return {SESSION_LABEL: slug, MANAGED_BY_LABEL: MANAGED_BY}


def selector(slug: str) -> dict[str, dict[str, str]]:
    return {"matchLabels": session_labels(slug)}


def object_names(slug: str) -> dict[str, str]:
    base = prefix(slug)
    names = {
        "pvc": f"{base}-workspace",
        "secret": f"{base}-ccw",
        "configmap": f"{base}-config",
        "service": base,
        "statefulset": base,
    }
    for name in names.values():
        assert name.startswith(base), f"{name} lacks the reserved prefix {base}"
    return names
