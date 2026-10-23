"""Request and response models for the clusters, agents and meta routes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agent_orchestrator.spec import ClusterSpec


class CreateCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    spec: ClusterSpec = ClusterSpec()


class PatchCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ClusterSpec


class ClusterSummary(BaseModel):
    state: Literal["working", "waiting", "booting", "error", "deleting"]
    ready: int
    replicas: int
    context_pct_max: float | None
    auto_compact_pct: int
    queued: int
    failed: int


class ClusterView(BaseModel):
    id: uuid.UUID
    slug: str
    spec: ClusterSpec
    desired: Literal["active", "deleting"]
    status: dict[str, Any]
    generation: int
    observed_generation: int
    created_at: datetime
    summary: ClusterSummary


class AgentContext(BaseModel):
    used_tokens: int | None
    window_tokens: int | None
    used_percentage: float | None


class AgentView(BaseModel):
    id: uuid.UUID
    ordinal: int
    pod_name: str
    ready: bool
    state: str
    free_since: datetime | None
    current_turn_id: str | None
    context: AgentContext | None
    peers: int
    gone: bool


class MetaDefaults(BaseModel):
    model: str
    effort: str
    replicas: int
    auto_compact_pct: int
    pvc_size: str
    storage_class: str
    max_attempts: int


class MetaCaps(BaseModel):
    max_replicas: int
    max_pvc_size: str
    max_clusters: int


class Meta(BaseModel):
    defaults: MetaDefaults
    agent_image: str
    agent_images: list[str]
    models: list[str]
    caps: MetaCaps
    discard_hook: Literal["none", "command", "url"]
