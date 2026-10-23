"""Render a cluster spec into the Kubernetes objects of design D6 (pure, stable order)."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import yaml

from agent_orchestrator.db.clusters import Cluster
from agent_orchestrator.k8s.labels import cluster_labels, object_names, prefix, selector
from agent_orchestrator.k8s.on_start import render_on_start
from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec
from agent_orchestrator.tokens import ClusterTokens

HASH_ANNOTATION = "agents.szymonrichert.pl/config-hash"
WORKDIR_ROOT = "/workdir"
SESSIONS_ROOT = "/sessions"
WORKSPACE_VOLUME = "workspace"
PROBE_PERIOD = 5
STARTUP_SLACK = 12

ASK_IN_WRITING_PROMPT = (
    "The AskUserQuestion tool is disabled and nobody answers interactive questions. "
    "When you need a decision or information from the user, ask in writing in your reply: "
    "write a numbered list of questions, and after each question give your recommended answer, "
    'worded so that replying "yes" accepts it. Then stop and wait for the reply. '
    "Never wait on an interactive question tool."
)


def workdir_path(spec: ClusterSpec) -> str:
    return f"{WORKDIR_ROOT}/{spec.workdir}"


def workdir_mount(spec: ClusterSpec) -> dict[str, str]:
    """The shared workspace root mount; sessions and check Jobs work in workdir_path(spec)."""
    return {"name": WORKSPACE_VOLUME, "mountPath": WORKDIR_ROOT, "subPath": "workdir"}


def _pod_url(slug: str, pod: str, settings: Settings) -> str:
    return (
        f"http://{pod}.{prefix(slug)}.{settings.namespace}.svc.{settings.cluster_domain}"
        f":{settings.wrapper_port}"
    )


def _shutdown_timeout(grace: int) -> int:
    return max(grace - 10, grace // 2)


def _ccw_config(cluster: Cluster, settings: Settings) -> dict[str, Any]:
    spec, slug = cluster.spec, cluster.slug
    claude: dict[str, Any] = {
        "passthrough_env": sorted(spec.env),
        "env": {
            "claude_code_oauth_token": "${CLAUDE_CODE_OAUTH_TOKEN}",
            "disable_autoupdater": True,
            "claude_autocompact_pct_override": spec.auto_compact_pct
            or settings.default_auto_compact_pct,
        },
    }
    model = spec.model or settings.default_model
    effort = spec.effort or settings.default_effort
    if model:
        claude["model"] = model
    if effort:
        claude["effort"] = effort
    claude["permissions"] = {"deny": ["EnterPlanMode", "ExitPlanMode", "AskUserQuestion"]}
    claude["mcp_servers"] = {
        "any-mcp": {
            "type": "stdio",
            "command": "/opt/any-mcp/bin/any-mcp",
            "args": ["--config", "/opt/any-mcp/etc/any-mcp.yaml"],
        }
    }
    cfg: dict[str, Any] = {
        "server": {"auth_token": "${CCW_AUTH_TOKEN}"},
        "paths": {"workdir_root": WORKDIR_ROOT, "sessions_root": SESSIONS_ROOT},
        "claude": claude,
        "hooks": {
            "onStart": [
                {
                    "type": "command",
                    "run": "sh /etc/ccw/on-start.sh",
                    "blocking": True,
                    "on_error": "deny",
                    "timeout_seconds": settings.on_start_timeout_seconds,
                }
            ]
        },
        "sessions": {
            "max_sessions": 1,
            "resume_on_start": True,
            "idle_timeout_seconds": 0,
            "ask_user_question": "deny",
        },
        "prompts": {"system_append": ASK_IN_WRITING_PROMPT},
        "limits": {
            "shutdown_timeout_seconds": _shutdown_timeout(settings.agent_termination_grace_seconds)
        },
    }
    if spec.peers:
        cfg["peers"] = {
            "enabled": True,
            "name": "${POD_NAME}",
            "advertise_url": "${CCW_PEER_URL}",
            "token": "${CCW_PEER_TOKEN}",
            "static": [
                _pod_url(slug, f"{prefix(slug)}-{i}", settings) for i in range(spec.replicas)
            ],
        }
    return cfg


def _meta(slug: str, kind_name: str, settings: Settings) -> dict[str, Any]:
    return {
        "name": kind_name,
        "namespace": settings.namespace,
        "labels": cluster_labels(slug),
    }


def _secret_ref(name: str, key: str) -> dict[str, Any]:
    return {"valueFrom": {"secretKeyRef": {"name": name, "key": key}}}


def _container(cluster: Cluster, settings: Settings, names: dict[str, str]) -> dict[str, Any]:
    spec, slug = cluster.spec, cluster.slug
    port = settings.wrapper_port
    env: list[dict[str, Any]] = [
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "CCW_PEER_URL", "value": _pod_url(slug, "$(POD_NAME)", settings)},
        {"name": "CCW_AUTH_TOKEN", **_secret_ref(names["secret"], "CCW_AUTH_TOKEN")},
        {"name": "CCW_PEER_TOKEN", **_secret_ref(names["secret"], "CCW_PEER_TOKEN")},
        {
            "name": "CLAUDE_CODE_OAUTH_TOKEN",
            **_secret_ref(settings.claude_secret_name, "CLAUDE_CODE_OAUTH_TOKEN"),
        },
        *({"name": k, "value": v} for k, v in sorted(spec.env.items())),
    ]
    startup_failures = math.ceil(settings.on_start_timeout_seconds / PROBE_PERIOD) + STARTUP_SLACK
    container: dict[str, Any] = {
        "name": "ccw",
        "image": spec.image or settings.agent_image,
        "ports": [{"name": "http", "containerPort": port}],
        "env": env,
        "startupProbe": {
            "httpGet": {"path": "/healthz", "port": port},
            "periodSeconds": PROBE_PERIOD,
            "failureThreshold": startup_failures,
        },
        "livenessProbe": {"httpGet": {"path": "/healthz", "port": port}, "periodSeconds": 10},
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": port}, "periodSeconds": 10},
        "volumeMounts": [
            workdir_mount(spec),
            {
                "name": WORKSPACE_VOLUME,
                "mountPath": SESSIONS_ROOT,
                "subPathExpr": "sessions/$(POD_NAME)",
            },
            {"name": "mise", "mountPath": "/opt/mise"},
            {"name": "any-mcp", "mountPath": "/opt/any-mcp"},
            {"name": "config", "mountPath": "/etc/ccw"},
        ],
    }
    if spec.resources is not None:
        resources = {k: v for k, v in spec.resources.model_dump().items() if v}
        if resources:
            container["resources"] = resources
    return container


def render_cluster(
    cluster: Cluster, settings: Settings, tokens: ClusterTokens
) -> list[dict[str, Any]]:
    spec, slug = cluster.spec, cluster.slug
    names = object_names(slug)
    labels = cluster_labels(slug)

    config_data = {
        "config.yaml": yaml.safe_dump(_ccw_config(cluster, settings), sort_keys=False),
        "on-start.sh": render_on_start(spec, settings),
    }
    config_hash = hashlib.sha256(json.dumps(config_data, sort_keys=True).encode()).hexdigest()

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": _meta(slug, names["pvc"], settings),
        "spec": {
            "accessModes": ["ReadWriteMany"],
            "storageClassName": settings.pvc_storage_class,
            "resources": {"requests": {"storage": spec.pvc_size or settings.pvc_size}},
        },
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _meta(slug, names["secret"], settings),
        "type": "Opaque",
        "stringData": {"CCW_AUTH_TOKEN": tokens.auth, "CCW_PEER_TOKEN": tokens.peer},
    }
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(slug, names["configmap"], settings),
        "data": config_data,
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(slug, names["service"], settings),
        "spec": {
            "clusterIP": "None",
            "publishNotReadyAddresses": True,
            "selector": labels,
            "ports": [
                {
                    "name": "http",
                    "port": settings.wrapper_port,
                    "targetPort": settings.wrapper_port,
                }
            ],
        },
    }
    pod_spec: dict[str, Any] = {
        "securityContext": {"runAsUser": 1000, "fsGroup": 1000},
        "terminationGracePeriodSeconds": settings.agent_termination_grace_seconds,
        "containers": [_container(cluster, settings, names)],
        "volumes": [
            {"name": WORKSPACE_VOLUME, "persistentVolumeClaim": {"claimName": names["pvc"]}},
            {"name": "config", "configMap": {"name": names["configmap"]}},
            {"name": "mise", "emptyDir": {}},
            {"name": "any-mcp", "emptyDir": {}},
        ],
    }
    if settings.image_pull_secret:
        pod_spec = {"imagePullSecrets": [{"name": settings.image_pull_secret}], **pod_spec}
    statefulset = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": _meta(slug, names["statefulset"], settings),
        "spec": {
            "replicas": spec.replicas,
            "podManagementPolicy": "Parallel",
            "serviceName": names["service"],
            "selector": selector(slug),
            "template": {
                "metadata": {"labels": labels, "annotations": {HASH_ANNOTATION: config_hash}},
                "spec": pod_spec,
            },
        },
    }
    return [pvc, secret, configmap, service, statefulset]
