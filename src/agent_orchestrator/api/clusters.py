"""Cluster, agent and meta routes (D11, D13, D15)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from agent_orchestrator.api import schemas
from agent_orchestrator.api.errors import ApiError, not_found
from agent_orchestrator.db import agents as agents_repo
from agent_orchestrator.db import clusters as repo
from agent_orchestrator.db import leadership, presets, views
from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import (
    ClusterSpec,
    SpecInvalid,
    fill_resource_defaults,
    generated_slug,
    validate_slug,
    validate_spec,
)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


Pool = Annotated[AsyncConnectionPool, Depends(get_pool)]
Cfg = Annotated[Settings, Depends(get_settings)]

router = APIRouter(prefix="/api/v1")

_CREATE_LOCK = 0x4F524348  # serialises cluster creation so max_clusters cannot be overshot


def _view(cluster: repo.Cluster, agg: dict | None, settings: Settings) -> schemas.ClusterView:
    agg = agg or {}
    return schemas.ClusterView(
        id=cluster.id,
        slug=cluster.slug,
        spec=cluster.spec,
        desired=cluster.desired,
        status=cluster.status,
        generation=cluster.generation,
        observed_generation=cluster.observed_generation,
        created_at=cluster.created_at,
        preset_id=cluster.preset_id,
        preset_name=cluster.preset_name,
        summary=views.summarize(cluster, agg, settings.default_auto_compact_pct),
        hive=cluster.spec.hive,
        max_replicas=cluster.spec.max_replicas or cluster.spec.replicas,
        static=cluster.spec.static,
        leader_agent_id=agg.get("leader_agent_id"),
        leader_epoch=agg.get("leader_epoch"),
    )


def agent_view(r: dict) -> schemas.AgentView:
    known = r["context_pct"] is not None or r["context_used_tokens"] is not None
    return schemas.AgentView(
        id=r["id"],
        ordinal=r["ordinal"],
        pod_name=r["pod_name"],
        ready=r["ready"],
        state=r["state"] or "unknown",
        free_since=r["free_since"],
        current_turn_id=r["current_turn_id"],
        context=schemas.AgentContext(
            used_tokens=r["context_used_tokens"],
            window_tokens=r["context_window_tokens"],
            used_percentage=r["context_pct"],
        )
        if known
        else None,
        context_pct=r["context_pct"],
        leader=bool(r.get("leader")),
        peers=len(r["peers"] or []),
        gone=r["gone_at"] is not None,
    )


async def _one_view(conn, cluster: repo.Cluster, settings: Settings) -> schemas.ClusterView:
    aggs = await views.cluster_aggregates(conn, cluster.id)
    return _view(cluster, aggs.get(cluster.id), settings)


def check_spec(
    spec: ClusterSpec, settings: Settings, stored: ClusterSpec | None = None
) -> ClusterSpec:
    """Validate the spec and return it with empty resource keys filled from the defaults.

    A model, effort or image equal to the one in `stored` is accepted even when it has left the
    settings lists; scheduling and preset creation pass no `stored` and validate fully.
    """
    try:
        validate_spec(spec, settings, stored)
    except SpecInvalid as exc:
        raise ApiError(422, "invalid_request", "; ".join(exc.errors)) from None
    return fill_resource_defaults(spec, settings)


@router.get("/meta", response_model=schemas.Meta)
async def meta(settings: Cfg) -> schemas.Meta:
    hook = settings.discard_hook()
    return schemas.Meta(
        defaults=schemas.MetaDefaults(
            model=settings.default_model,
            effort=settings.default_effort,
            workdir=ClusterSpec.model_fields["workdir"].default,
            resources=schemas.MetaResources(**settings.default_resources()),
            any_mcp_config=settings.default_any_mcp_config(),
            discard_hook=hook.kind if hook else "none",
            replicas=settings.default_replicas,
            auto_compact_pct=settings.default_auto_compact_pct,
            pvc_size=settings.pvc_size,
            storage_class=settings.pvc_storage_class,
            max_attempts=ClusterSpec.model_fields["max_attempts"].default,
        ),
        agent_image=settings.agent_image,
        agent_images=sorted(settings.image_allowlist()),
        models=list(settings.models),
        efforts=list(settings.efforts),
        caps=schemas.MetaCaps(
            max_replicas=settings.max_replicas,
            max_pvc_size=settings.max_pvc_size,
            max_clusters=settings.max_clusters,
        ),
        discard_hook=hook.kind if hook else "none",
    )


@router.get("/clusters", response_model=list[schemas.ClusterView])
async def list_clusters(pool: Pool, settings: Cfg) -> list[schemas.ClusterView]:
    async with pool.connection() as conn:
        found = await repo.list_clusters(conn)
        aggs = await views.cluster_aggregates(conn)
    return [_view(c, aggs.get(c.id), settings) for c in found]


@router.post("/clusters", status_code=201, response_model=schemas.ClusterView)
async def create_cluster(
    body: schemas.CreateCluster,
    pool: Pool,
    settings: Cfg,
) -> schemas.ClusterView:
    if (body.spec is None) == (body.preset_id is None):
        raise ApiError(422, "invalid_request", "exactly one of spec or preset_id")
    if body.spec is not None:
        if body.slug is None:
            raise ApiError(422, "invalid_request", "slug is required with spec")
        spec = check_spec(body.spec, settings)
    if body.slug is not None:
        try:
            validate_slug(body.slug)
        except ValueError as exc:
            raise ApiError(422, "invalid_request", str(exc)) from None
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CREATE_LOCK,))
            if await repo.count_active(conn) >= settings.max_clusters:
                raise ApiError(
                    409, "cluster_limit", f"at most {settings.max_clusters} clusters are allowed"
                )
            if body.preset_id is None:
                cluster = await _insert(conn, body.slug, spec)
            else:
                cluster = await _create_from_preset(conn, body.preset_id, body.slug, settings)
        return await _one_view(conn, cluster, settings)


_SLUG_ATTEMPTS = 5


async def _insert(conn, slug: str, spec: ClusterSpec, **provenance) -> repo.Cluster:
    try:
        return await repo.create(conn, slug, spec, **provenance)
    except repo.ClusterExists:
        raise ApiError(409, "conflict", f"cluster {slug!r} already exists") from None


async def _create_from_preset(
    conn, preset_id: uuid.UUID, slug: str | None, settings: Settings
) -> repo.Cluster:
    """Snapshot the preset's spec, filled and validated against the current settings."""
    preset = await presets.get_for_share(conn, preset_id)
    if preset is None:
        raise not_found("preset")
    raw = dict(preset.spec)
    raw.setdefault("replicas", settings.default_replicas)
    try:
        spec = check_spec(ClusterSpec.model_validate(raw), settings)
    except ValidationError as exc:
        raise ApiError(422, "invalid_request", f"preset '{preset.name}': {exc}") from None
    except ApiError as exc:
        raise ApiError(422, "invalid_request", f"preset '{preset.name}': {exc.message}") from None
    provenance = {"preset_id": preset.id, "preset_name": preset.name}
    if slug is not None:
        return await _insert(conn, slug, spec, **provenance)
    for _ in range(_SLUG_ATTEMPTS):
        try:
            return await repo.create(conn, generated_slug(preset.name), spec, **provenance)
        except repo.ClusterExists:
            continue
    raise ApiError(409, "conflict", "could not generate a free cluster slug; retry")


@router.get("/clusters/{cluster_id}", response_model=schemas.ClusterView)
async def get_cluster(
    cluster_id: uuid.UUID,
    pool: Pool,
    settings: Cfg,
) -> schemas.ClusterView:
    async with pool.connection() as conn:
        cluster = await repo.get(conn, cluster_id)
        if cluster is None:
            raise not_found("cluster")
        return await _one_view(conn, cluster, settings)


@router.delete("/clusters/{cluster_id}", status_code=202, response_model=schemas.ClusterView)
async def discard_cluster(
    cluster_id: uuid.UUID,
    request: Request,
    pool: Pool,
    settings: Cfg,
) -> schemas.ClusterView:
    if request.query_params:
        raise ApiError(
            422, "invalid_request", "discard takes no parameters; it always deletes everything"
        )
    async with pool.connection() as conn:
        try:
            cluster = await repo.request_delete(conn, cluster_id)
        except repo.ClusterNotFound:
            raise not_found("cluster") from None
        return await _one_view(conn, cluster, settings)


@router.get("/clusters/{cluster_id}/agents", response_model=list[schemas.AgentView])
async def list_agents(cluster_id: uuid.UUID, pool: Pool) -> list[schemas.AgentView]:
    async with pool.connection() as conn:
        if await repo.get(conn, cluster_id) is None:
            raise not_found("cluster")
        rows = await views.agent_rows(conn, cluster_id)
    return [agent_view(r) for r in rows]


@router.post(
    "/clusters/{cluster_id}/leader", status_code=202, response_model=schemas.LeadershipRequested
)
async def make_leader(
    cluster_id: uuid.UUID, body: schemas.MakeLeaderIn, pool: Pool
) -> schemas.LeadershipRequested:
    """Queue a user leadership override; the dispatcher decides at apply time."""
    async with pool.connection() as conn:
        cluster = await repo.get(conn, cluster_id)
        if cluster is None:
            raise not_found("cluster")
        target = await agents_repo.get_agent(conn, body.agent_id)
        if target is None or target.cluster_id != cluster_id:
            raise not_found("agent")
        if cluster.spec.alone:
            raise ApiError(409, "alone", "a single-agent cluster has no leader to change")
        request_id = await leadership.insert_request(
            conn, cluster_id, "user", None, target.id, "user", None
        )
    return schemas.LeadershipRequested(request_id=request_id)
