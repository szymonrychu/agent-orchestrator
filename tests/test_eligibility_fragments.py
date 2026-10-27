"""The eligibility and handover fragments are spelled once, in `db/eligibility.py`."""

from __future__ import annotations

from agents_orchestrator.db import eligibility, leadership, queue


def test_no_limit_is_the_only_spelling_of_the_literal() -> None:
    assert str(eligibility.NO_LIMIT) in eligibility.REPLICAS
    expected = f"coalesce((c.spec->>'replicas')::int, {eligibility.NO_LIMIT})"
    assert expected == eligibility.REPLICAS
    assert eligibility.STAYS.endswith(f"{eligibility.NO_LIMIT})")


def test_fragments_compose_from_each_other() -> None:
    assert eligibility.STAYS in eligibility.READY_STAYS
    assert eligibility.READY_STAYS in eligibility.ELIGIBLE
    assert eligibility.LEAVING in eligibility.HANDOVER_TRIGGER
    assert eligibility.COMPACTED in eligibility.HANDOVER_TRIGGER


def test_call_sites_embed_the_shared_fragments() -> None:
    assert eligibility.ELIGIBLE in leadership._CANDIDATES
    assert eligibility.HANDOVER_TRIGGER in leadership._CANDIDATES
    assert eligibility.ELIGIBLE in leadership._FREE_LONGEST
    assert eligibility.HANDOVER_TRIGGER in queue._HANDOVER_PENDING
    assert eligibility.ELIGIBLE in queue._HANDOVER_PENDING
