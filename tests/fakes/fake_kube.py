"""In-memory KubeApi that behaves like the reconciler's production Role (D9, D26).

- Every call outside `namespace` raises Forbidden, and so do get and list on Secrets (the Role
  grants secrets only create, patch and delete). Forbidden attempts are also recorded in
  `forbidden`, so a test can assert none were made even when the caller swallowed the error.
- Applies, deletes and every call are recorded (`applies`, `deletes`, `calls`).
- Applying a StatefulSet simulates its controller: pods `<sts>-<i>` for i < replicas carrying the
  pod template labels; scaling down removes the higher ordinals, deleting the StatefulSet
  removes its pods, and deleting a pod of a live StatefulSet recreates it with a new uid.
  Readiness and phase are controlled with `set_pod`; `controller_paused` freezes the pods and
  the StatefulSet status.
- Revisions: `metadata.generation` counts spec changes; the controller sets
  `status.observedGeneration` and `status.updateRevision` (`<sts>-<hash of the template>`), and
  labels every pod it creates `controller-revision-hash: <updateRevision>`. Under
  `updateStrategy: OnDelete` a template change leaves existing pods on their old revision until
  they are deleted, and a deleted pod returns at the update revision. Under RollingUpdate (the
  default) no pod is recreated, as before; existing pods are relabelled at the update revision.
- `graceful_pod_deletes` keeps a deleted pod terminating (`metadata.deletionTimestamp` set, its
  labels and readiness unchanged) until `finish_termination`, like a real grace period.
- `seed` places objects (the orchestrator's own, unlabelled ones, or foreign ones) without
  recording them; `seed_orchestrator_objects` seeds what the chart puts in the same namespace.
- `fail(kind, name)` injects an error for one object.
- Creating a Job simulates its controller: one Running pod `<job>-<suffix>` with the pod template
  labels; `finish_job` ends it (succeeded, failed with an exit code, or deadline exceeded) and
  gives the pod its log; deleting a Job removes its pods.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agents_orchestrator.k8s.kube import FIELD_MANAGER, Forbidden, KubeError
from agents_orchestrator.reconciler.rollout import REVISION_LABEL

READ_VERBS = frozenset({"get", "list", "watch"})
ORCHESTRATOR_LABELS = {
    "app.kubernetes.io/name": "agents-orchestrator",
    "app.kubernetes.io/instance": "agents-orchestrator",
    "app.kubernetes.io/managed-by": "Helm",
}

_seq = itertools.count(1)


@dataclass(frozen=True)
class Call:
    verb: str
    kind: str
    namespace: str
    name: str | None = None
    selector: dict[str, str] | None = None


@dataclass(frozen=True)
class Apply:
    obj: dict[str, Any]
    field_manager: str
    force: bool

    @property
    def kind(self) -> str:
        return self.obj["kind"]

    @property
    def name(self) -> str:
        return self.obj["metadata"]["name"]


@dataclass
class _Failure:
    exc: Exception
    verbs: frozenset[str] | None
    times: int | None


@dataclass
class FakeKube:
    namespace: str = "agents"
    pods_ready: bool = False
    controller_paused: bool = False  # the StatefulSet controller lags: no pod or status changes
    graceful_pod_deletes: bool = False  # a deleted pod stays terminating until finish_termination
    objects: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    applies: list[Apply] = field(default_factory=list)
    deletes: list[tuple[str, str, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    forbidden: list[Call] = field(default_factory=list)
    _failures: dict[tuple[str, str], _Failure] = field(default_factory=dict)

    # ------------------------------------------------------------------ test controls

    def seed(self, *objs: Mapping[str, Any]) -> None:
        for obj in objs:
            o = copy.deepcopy(dict(obj))
            meta = o.setdefault("metadata", {})
            meta.setdefault("namespace", self.namespace)
            meta.setdefault("labels", {})
            meta.setdefault("uid", str(uuid.uuid4()))
            self.objects[(o["kind"], meta["namespace"], meta["name"])] = o

    def seed_orchestrator_objects(self) -> list[tuple[str, str]]:
        """The chart's own objects in the shared namespace; returns their (kind, name)."""
        objs: list[dict[str, Any]] = []
        for component in ("api", "dispatcher", "ingester", "reconciler"):
            labels = {**ORCHESTRATOR_LABELS, "app.kubernetes.io/component": component}
            objs.append(_obj("apps/v1", "Deployment", f"agents-orchestrator-{component}", labels))
            objs.append(_pod(f"agents-orchestrator-{component}-7d9f8c-x2k4q", labels, ready=True))
        objs.append(
            _obj("v1", "ConfigMap", "agents-orchestrator-settings", dict(ORCHESTRATOR_LABELS))
        )
        for secret in ("agents-orchestrator-claude", "agents-orchestrator-master-key"):
            objs.append(_obj("v1", "Secret", secret, dict(ORCHESTRATOR_LABELS)))
        objs.append(_obj("v1", "Service", "agents-orchestrator", dict(ORCHESTRATOR_LABELS)))
        cnpg = {"cnpg.io/cluster": "agents-orchestrator-db"}
        for i in (1, 2):
            objs.append(_obj("v1", "PersistentVolumeClaim", f"agents-orchestrator-db-{i}", cnpg))
            objs.append(_pod(f"agents-orchestrator-db-{i}", cnpg, ready=True))
        self.seed(*objs)
        return [(o["kind"], o["metadata"]["name"]) for o in objs]

    def fail(
        self,
        kind: str,
        name: str,
        exc: Exception | None = None,
        *,
        verbs: Iterable[str] | None = None,
        times: int | None = None,
    ) -> None:
        """Make calls on one object raise `exc` (all verbs unless `verbs`; `times` then heal)."""
        self._failures[(kind, name)] = _Failure(
            exc or KubeError(f"injected failure for {kind}/{name}"),
            frozenset(verbs) if verbs is not None else None,
            times,
        )

    def heal(self, kind: str, name: str) -> None:
        self._failures.pop((kind, name), None)

    def set_pod(
        self,
        name: str,
        *,
        ready: bool | None = None,
        phase: str | None = None,
        pod_ip: str | None = None,
    ) -> None:
        pod = self.objects[("Pod", self.namespace, name)]
        if pod_ip is not None:
            pod["status"]["podIP"] = pod_ip
        if phase is not None:
            pod["status"]["phase"] = phase
        if ready is not None:
            _set_ready(pod, ready)

    def finish_job(
        self,
        name: str,
        *,
        exit_code: int = 0,
        log: str = "",
        deadline_exceeded: bool = False,
    ) -> None:
        """End a Job like its controller: Complete (exit 0), Failed, or DeadlineExceeded."""
        job = self.objects[("Job", self.namespace, name)]
        pod = next(p for p in self._job_pods(name))
        pod["log"] = log
        if deadline_exceeded:
            pod["status"]["phase"] = "Failed"
            job["status"] = {
                "failed": 1,
                "conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}],
            }
            return
        ok = exit_code == 0
        pod["status"]["phase"] = "Succeeded" if ok else "Failed"
        pod["status"]["containerStatuses"] = [
            {"name": "check", "state": {"terminated": {"exitCode": exit_code}}}
        ]
        if ok:
            job["status"] = {
                "succeeded": 1,
                "conditions": [{"type": "Complete", "status": "True"}],
            }
        else:
            job["status"] = {
                "failed": 1,
                "conditions": [
                    {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}
                ],
            }

    def finish_termination(self, name: str) -> None:
        """A terminating pod is gone; its StatefulSet controller recreates it."""
        pod = self.objects.pop(("Pod", self.namespace, name))
        assert pod["metadata"].get("deletionTimestamp"), f"pod {name} is not terminating"
        self._recreate(pod)

    def has(self, kind: str, name: str, namespace: str | None = None) -> bool:
        return (kind, namespace or self.namespace, name) in self.objects

    def get(self, kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
        return self.objects[(kind, namespace or self.namespace, name)]

    def names(self, kind: str) -> list[str]:
        return sorted(n for (k, ns, n) in self.objects if k == kind and ns == self.namespace)

    def applied(self, kind: str | None = None) -> list[Apply]:
        return [a for a in self.applies if kind is None or a.kind == kind]

    def deleted(self, kind: str | None = None) -> list[str]:
        return [n for (k, _ns, n) in self.deletes if kind is None or k == kind]

    def reset_records(self) -> None:
        self.applies.clear()
        self.deletes.clear()
        self.calls.clear()
        self.forbidden.clear()

    # ------------------------------------------------------------------ the Role and failures

    def _check(
        self, verb: str, kind: str, namespace: str, name: str | None = None, selector=None
    ) -> None:
        call = Call(verb, kind, namespace, name, dict(selector) if selector is not None else None)
        self.calls.append(call)
        if namespace != self.namespace:
            self.forbidden.append(call)
            raise Forbidden(f"{verb} {kind} in namespace {namespace!r} is outside the Role")
        if kind == "Secret" and verb in READ_VERBS:
            self.forbidden.append(call)
            raise Forbidden(f"{verb} on secrets is not granted to the reconciler")
        if name is not None:
            failure = self._failures.get((kind, name))
            if failure is not None and (failure.verbs is None or verb in failure.verbs):
                if failure.times is not None:
                    failure.times -= 1
                    if failure.times <= 0:
                        self.heal(kind, name)
                raise failure.exc

    # ------------------------------------------------------------------ KubeApi

    async def apply(
        self, obj: dict[str, Any], *, field_manager: str = FIELD_MANAGER, force: bool = True
    ) -> dict[str, Any]:
        meta = obj["metadata"]
        self._check("patch", obj["kind"], meta.get("namespace", ""), meta["name"])
        self.applies.append(Apply(copy.deepcopy(obj), field_manager, force))
        key = (obj["kind"], meta["namespace"], meta["name"])
        stored = copy.deepcopy(obj)
        previous = self.objects.get(key)
        stored["metadata"]["uid"] = (
            previous["metadata"]["uid"] if previous is not None else str(uuid.uuid4())
        )
        stored.pop("stringData", None)  # a Secret's data is write-only for this fake
        self.objects[key] = stored
        if obj["kind"] == "StatefulSet":
            self._sts_applied(stored, previous)
            self._sync_pods(stored)
        return copy.deepcopy(stored)

    async def get_object(self, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        self._check("get", kind, namespace, name)
        obj = self.objects.get((kind, namespace, name))
        return copy.deepcopy(obj) if obj is not None else None

    async def list_objects(
        self, kind: str, namespace: str, selector: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        self._check("list", kind, namespace, selector=selector)
        return self._select(kind, namespace, selector)

    async def delete(self, kind: str, namespace: str, name: str) -> bool:
        self._check("delete", kind, namespace, name)
        obj = self.objects.pop((kind, namespace, name), None)
        if obj is None:
            return False
        self.deletes.append((kind, namespace, name))
        if kind == "StatefulSet":
            for pod in self._pods_of(name):
                self.objects.pop(("Pod", namespace, pod["metadata"]["name"]), None)
        if kind == "Job":
            for pod in self._job_pods(name):
                self.objects.pop(("Pod", namespace, pod["metadata"]["name"]), None)
        return True

    async def list_pods(self, namespace: str, selector: Mapping[str, str]) -> list[dict[str, Any]]:
        self._check("list", "Pod", namespace, selector=selector)
        return self._select("Pod", namespace, selector)

    async def delete_pod(self, namespace: str, name: str) -> bool:
        self._check("delete", "Pod", namespace, name)
        key = ("Pod", namespace, name)
        pod = self.objects.get(key)
        if pod is None:
            return False
        self.deletes.append(key)
        if self.graceful_pod_deletes:
            pod["metadata"].setdefault("deletionTimestamp", "2026-10-28T00:00:00Z")
            return True
        del self.objects[key]
        self._recreate(pod)
        return True

    async def create_job(self, obj: dict[str, Any]) -> dict[str, Any]:
        meta = obj["metadata"]
        self._check("create", "Job", meta.get("namespace", ""), meta["name"])
        key = ("Job", meta["namespace"], meta["name"])
        if key in self.objects:
            raise KubeError(f"jobs {meta['name']!r} already exists")
        stored = copy.deepcopy(obj)
        stored["metadata"]["uid"] = str(uuid.uuid4())
        stored.setdefault("status", {})
        self.objects[key] = stored
        template = stored["spec"]["template"]
        pod = _pod(
            f"{meta['name']}-x7k2p", dict(template["metadata"].get("labels", {})), ready=False
        )
        pod["metadata"]["namespace"] = meta["namespace"]
        pod["metadata"]["annotations"] = {"fake/job": meta["name"]}
        pod["status"]["phase"] = "Running"
        self.objects[("Pod", meta["namespace"], pod["metadata"]["name"])] = pod
        return copy.deepcopy(stored)

    async def get_job(self, namespace: str, name: str) -> dict[str, Any] | None:
        return await self.get_object("Job", namespace, name)

    async def read_pod_log(self, namespace: str, name: str, tail_lines: int) -> str:
        self._check("get", "Pod", namespace, name)
        pod = self.objects.get(("Pod", namespace, name))
        if pod is None:
            raise KubeError(f"pods {name!r} not found")
        lines = pod.get("log", "").splitlines()
        return "\n".join(lines[-tail_lines:]) if tail_lines else ""

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ internals

    def _select(
        self, kind: str, namespace: str, selector: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        out = []
        for (k, ns, _name), obj in sorted(self.objects.items()):
            labels = obj["metadata"].get("labels", {})
            if (
                k == kind
                and ns == namespace
                and all(labels.get(a) == b for a, b in selector.items())
            ):
                out.append(copy.deepcopy(obj))
        return out

    def _pods_of(self, sts_name: str) -> list[dict[str, Any]]:
        return [
            o
            for (k, ns, _n), o in self.objects.items()
            if k == "Pod"
            and ns == self.namespace
            and o["metadata"].get("annotations", {}).get("fake/statefulset") == sts_name
        ]

    def _job_pods(self, job_name: str) -> list[dict[str, Any]]:
        return [
            o
            for (k, ns, _n), o in self.objects.items()
            if k == "Pod"
            and ns == self.namespace
            and o["metadata"].get("annotations", {}).get("fake/job") == job_name
        ]

    def _recreate(self, pod: dict[str, Any]) -> None:
        owner = pod["metadata"].get("annotations", {}).get("fake/statefulset")
        sts = self.objects.get(("StatefulSet", self.namespace, owner)) if owner else None
        if sts is not None:
            self._sync_pods(sts)  # the controller recreates the pod, unready, with a new uid

    def _sts_applied(self, sts: dict[str, Any], previous: dict[str, Any] | None) -> None:
        """Generation, status and, under RollingUpdate, the existing pods' revision label."""
        meta = sts["metadata"]
        if previous is None:
            meta["generation"], status = 1, {}
        else:
            changed = previous.get("spec") != sts.get("spec")
            meta["generation"] = previous["metadata"].get("generation", 1) + changed
            status = copy.deepcopy(previous.get("status") or {})
        sts["status"] = status
        if self.controller_paused:
            return
        template = json.dumps(sts["spec"]["template"], sort_keys=True).encode()
        revision = f"{meta['name']}-{hashlib.sha256(template).hexdigest()[:10]}"
        status["observedGeneration"] = meta["generation"]
        status["updateRevision"] = revision
        if (sts["spec"].get("updateStrategy") or {}).get("type") != "OnDelete":
            for pod in self._pods_of(meta["name"]):
                pod["metadata"]["labels"][REVISION_LABEL] = revision

    def _sync_pods(self, sts: dict[str, Any]) -> None:
        if self.controller_paused:
            return
        name = sts["metadata"]["name"]
        replicas = sts["spec"].get("replicas", 1)
        labels = dict(sts["spec"]["template"]["metadata"].get("labels", {}))
        revision = (sts.get("status") or {}).get("updateRevision")
        if revision is not None:
            labels[REVISION_LABEL] = revision
        wanted = {f"{name}-{i}" for i in range(replicas)}
        for pod in self._pods_of(name):
            if pod["metadata"]["name"] not in wanted:
                del self.objects[("Pod", self.namespace, pod["metadata"]["name"])]
        for pod_name in sorted(wanted):
            key = ("Pod", self.namespace, pod_name)
            if key not in self.objects:
                pod = _pod(pod_name, dict(labels), ready=self.pods_ready)
                pod["metadata"]["annotations"] = {"fake/statefulset": name}
                pod["metadata"]["namespace"] = self.namespace
                self.objects[key] = pod


def _obj(api_version: str, kind: str, name: str, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": name, "labels": labels},
    }


def _set_ready(pod: dict[str, Any], ready: bool) -> None:
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True" if ready else "False"}]


def _pod(name: str, labels: dict[str, str], *, ready: bool) -> dict[str, Any]:
    pod = _obj("v1", "Pod", name, labels)
    pod["metadata"]["uid"] = f"{next(_seq)}-{uuid.uuid4()}"
    pod["status"] = {
        "phase": "Running" if ready else "Pending",
        "podIP": f"10.0.{next(_seq) // 250}.{next(_seq) % 250 + 1}",
    }
    _set_ready(pod, ready)
    return pod
