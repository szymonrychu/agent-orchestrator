"""Cluster, agent and meta routes (D11, D13, D15)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from psycopg_pool import AsyncConnectionPool

from agent_orchestrator.api import schemas
from agent_orchestrator.api.errors import ApiError, not_found
from agent_orchestrator.db import clusters as repo
from agent_orchestrator.db import views
from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec, SpecInvalid, validate_slug, validate_spec


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


Pool = Annotated[AsyncConnectionPool, Depends(get_pool)]
Cfg = Annotated[Settings, Depends(get_settings)]

router = APIRouter(prefix="/api/v1")

_CREATE_LOCK = 0x4F524348  # serialises cluster creation so max_clusters cannot be overshot


def _view(cluster: repo.Cluster, agg: dict | None, settings: Settings) -> schemas.ClusterView:
    return schemas.ClusterView(
        id=cluster.id,
        slug=cluster.slug,
        spec=cluster.spec,
        desired=cluster.desired,
        status=cluster.status,
        generation=cluster.generation,
        observed_generation=cluster.observed_generation,
        created_at=cluster.created_at,
        summary=views.summarize(cluster, agg, settings.default_auto_compact_pct),
    )


async def _one_view(conn, cluster: repo.Cluster, settings: Settings) -> schemas.ClusterView:
    aggs = await views.cluster_aggregates(conn, cluster.id)
    return _view(cluster, aggs.get(cluster.id), settings)


def _check_spec(spec: ClusterSpec, settings: Settings) -> None:
    try:
        validate_spec(spec, settings)
    except SpecInvalid as exc:
        raise ApiError(422, "invalid_request", "; ".join(exc.errors)) from None


@router.get("/meta", response_model=schemas.Meta)
async def meta(settings: Cfg) -> schemas.Meta:
    hook = settings.discard_hook()
    return schemas.Meta(
        defaults=schemas.MetaDefaults(
            model=settings.default_model,
            effort=settings.default_effort,
            replicas=settings.default_replicas,
            auto_compact_pct=settings.default_auto_compact_pct,
            pvc_size=settings.pvc_size,
            storage_class=settings.pvc_storage_class,
            max_attempts=ClusterSpec.model_fields["max_attempts"].default,
        ),
        agent_image=settings.agent_image,
        agent_images=sorted(settings.image_allowlist()),
        models=list(settings.models),
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
    try:
        validate_slug(body.slug)
    except ValueError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None
    _check_spec(body.spec, settings)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CREATE_LOCK,))
            if await repo.count_active(conn) >= settings.max_clusters:
                raise ApiError(
                    409, "cluster_limit", f"at most {settings.max_clusters} clusters are allowed"
                )
            try:
                cluster = await repo.create(conn, body.slug, body.spec)
            except repo.ClusterExists:
                raise ApiError(409, "conflict", f"cluster {body.slug!r} already exists") from None
        return await _one_view(conn, cluster, settings)


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


@router.patch("/clusters/{cluster_id}", response_model=schemas.ClusterView)
async def patch_cluster(
    cluster_id: uuid.UUID,
    body: schemas.PatchCluster,
    pool: Pool,
    settings: Cfg,
) -> schemas.ClusterView:
    _check_spec(body.spec, settings)
    async with pool.connection() as conn:
        try:
            cluster = await repo.update_spec(conn, cluster_id, body.spec)
        except repo.ClusterNotFound:
            raise not_found("cluster") from None
        except repo.ClusterDeleting:
            raise ApiError(409, "cluster_deleting", "cluster is being discarded") from None
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
    out = []
    for r in rows:
        known = r["context_pct"] is not None or r["context_used_tokens"] is not None
        out.append(
            schemas.AgentView(
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
                peers=len(r["peers"] or []),
                gone=r["gone_at"] is not None,
            )
        )
    return out
