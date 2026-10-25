"""Per-cluster token derivation: every process derives the same tokens from the master key."""

import hashlib
import hmac
from dataclasses import dataclass, field
from uuid import UUID

from agents_orchestrator.settings import Settings


@dataclass(frozen=True)
class ClusterTokens:
    auth: str = field(repr=False)
    peer: str = field(repr=False)
    agent: str = field(repr=False)

    def __repr__(self) -> str:
        return "ClusterTokens(auth='***', peer='***', agent='***')"

    __str__ = __repr__


def _mac(master_key: bytes, purpose: str, cluster_id: UUID) -> str:
    return hmac.new(master_key, f"{purpose}:{cluster_id}".encode(), hashlib.sha256).hexdigest()


def derive_tokens(master_key: bytes, cluster_id: UUID) -> ClusterTokens:
    return ClusterTokens(
        auth=_mac(master_key, "auth", cluster_id),
        peer=_mac(master_key, "peer", cluster_id),
        agent=_mac(master_key, "agent", cluster_id),
    )


def tokens_for(settings: Settings, cluster_id: UUID) -> ClusterTokens:
    return derive_tokens(settings.require_master_key().encode(), cluster_id)
