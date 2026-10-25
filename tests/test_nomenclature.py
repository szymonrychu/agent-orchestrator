"""The orchestrator's unit is a session: no tracked line calls it a cluster (design D2, D12).

Lines naming a Kubernetes, CNPG or kind cluster stay; they are matched by the literal phrases of
ALLOWED_PHRASES, which are removed from a line before it is checked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GUARDED = ("src/agents_orchestrator", "tests", "openapi.json")

# Kubernetes, CNPG and kind clusters (D12); never the orchestrator's unit.
ALLOWED_PHRASES = (
    "cluster.local",
    "cluster_domain",
    "CLUSTER_DOMAIN",
    "ClusterIP",
    "clusterIP",
    "ClusterIssuer",
    "cluster-issuer",
    "ClusterRoleBinding",
    "ClusterRole",
    "in-cluster",
    "In-cluster",
    "cluster-internal",
    "Cluster-internal",
    "cluster node",
    "cluster-info",
    "db-cluster",
    "kind cluster",
    "config.cluster.server",
    # kubeconfig keys
    '"clusters": [{"name": "k", "cluster": {"server"',
    "clusters: [{name: k, cluster: {server",
    '"context": {"cluster": "k"',
    "context: {cluster: k",
    "cnpg.io/cluster",
    'CNPG_KIND = "Cluster"',
)

# Old names that retirement tests pin as gone (old routes, the old agent-API header).
RETIRED_PHRASES = (
    '"/api/v1/clusters"',
    "test_old_cluster_routes_gone",
    '"X-Orch-Cluster"',
    "test_old_cluster_header_refused",
)

# Schema history: 0001-0004 stay byte-identical, 0005 renames their old names, and the migration
# tests seed the pre-0005 schema.
HISTORY = frozenset(
    {
        "src/agents_orchestrator/db/migrations/0001_init.sql",
        "src/agents_orchestrator/db/migrations/0002_presets.sql",
        "src/agents_orchestrator/db/migrations/0003_hive.sql",
        "src/agents_orchestrator/db/migrations/0004_wrapper_session_id.sql",
        "src/agents_orchestrator/db/migrations/0005_sessions.sql",
        "tests/test_migrations.py",
        "tests/test_nomenclature.py",
    }
)

UNIT = re.compile("cluster", re.IGNORECASE)


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", *GUARDED], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.split()


def _offending(path: str) -> list[str]:
    try:
        text = (ROOT / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return []
    hits = []
    for n, line in enumerate(text.splitlines(), start=1):
        stripped = line
        for phrase in (*ALLOWED_PHRASES, *RETIRED_PHRASES):
            stripped = stripped.replace(phrase, "")
        if UNIT.search(stripped):
            hits.append(f"{path}:{n}: {line.strip()}")
    return hits


def test_guarded_paths_name_the_unit_session() -> None:
    files = [p for p in _tracked() if p not in HISTORY]
    assert len(files) > 100
    hits = [hit for path in files for hit in _offending(path)]
    assert not hits, "\n".join(hits[:50]) + f"\n({len(hits)} lines)"


def test_no_tracked_path_names_the_unit() -> None:
    paths = [p for p in _tracked() if UNIT.search(p) and p not in HISTORY]
    assert paths == []


def test_allowlist_names_only_kubernetes_clusters() -> None:
    for phrase in ALLOWED_PHRASES:
        assert UNIT.search(phrase), phrase
    assert not any(p.lower() in ("cluster", "clusters", "cluster_id") for p in ALLOWED_PHRASES)
