import dataclasses
from uuid import UUID

import pytest

from agent_orchestrator.settings import Settings, SettingsError
from agent_orchestrator.tokens import ClusterTokens, derive_tokens, tokens_for

KEY = b"k" * 32
CID = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")


def test_deterministic():
    assert derive_tokens(KEY, CID) == derive_tokens(KEY, CID)


def test_auth_and_peer_differ():
    t = derive_tokens(KEY, CID)
    assert t.auth != t.peer


def test_depends_on_cluster_and_master():
    t = derive_tokens(KEY, CID)
    assert derive_tokens(KEY, OTHER) != t
    assert derive_tokens(b"z" * 32, CID) != t


def test_length_at_least_16():
    t = derive_tokens(KEY, CID)
    assert len(t.auth) == len(t.peer) == 64
    assert min(len(t.auth), len(t.peer)) >= 16


def test_repr_masks():
    t = derive_tokens(KEY, CID)
    for text in (repr(t), str(t), f"{t}"):
        assert t.auth not in text and t.peer not in text
        assert "***" in text


def test_tokens_for_uses_master_key(monkeypatch):
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ORCH_MASTER_KEY", "s" * 32)
    assert tokens_for(Settings(), CID) == derive_tokens(b"s" * 32, CID)
    monkeypatch.delenv("ORCH_MASTER_KEY")
    with pytest.raises(SettingsError):
        tokens_for(Settings(), CID)


def test_frozen():
    assert isinstance(derive_tokens(KEY, CID), ClusterTokens)
    with pytest.raises(dataclasses.FrozenInstanceError):
        derive_tokens(KEY, CID).auth = "x"  # type: ignore[misc]
