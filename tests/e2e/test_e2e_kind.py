"""kind end-to-end: the chart installed into namespace `agents` (D9, D19, D22, D26, D28).

Run by `scripts/e2e.sh` (`mise run test:e2e`), which creates the kind cluster, installs the
chart with e2e/values-kind.yaml, runs `helm test`, port-forwards the api Service to
ORCH_E2E_API_URL and then runs this module. The tests share one agent cluster and run in
file order: the round-robin fixture sends the messages the event-stream test reads, and the
discard fixture runs last, so the shared-namespace test compares against a baseline taken
before the cluster existed. Every fixture is lazy, so a single test can also run alone.

The test (never the orchestrator) uses kubectl exec to look inside agent pods.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import yaml

from agent_orchestrator.settings import Settings

pytestmark = pytest.mark.e2e

NAMESPACE = os.environ.get("ORCH_E2E_NAMESPACE", "agents")
RELEASE = os.environ.get("ORCH_E2E_RELEASE", "agent-orchestrator")
API_URL = os.environ.get("ORCH_E2E_API_URL", "http://127.0.0.1:18080")
KUBE_CONTEXT = os.environ.get("ORCH_E2E_KUBE_CONTEXT", "kind-agent-orchestrator-e2e")
SLUG = "e2e-rr"
CLUSTER_LABEL = "agents.szymonrichert.pl/cluster"
COMPONENTS = ("api", "dispatcher", "ingester", "reconciler")
RECONCILER_SA = f"system:serviceaccount:{NAMESPACE}:{RELEASE}-reconciler"

# The test agent image (e2e/agent-test.Dockerfile) ships the wrapper's scripted fake claude at
# /opt/agent-test/claude and a scenario whose "slow" turns take a fixed 4 s. spec.env puts the
# fake first on PATH (so the rendered config's `claude` binary resolves to it) and selects
# that scenario; the renderer passes both through to the claude child (claude.passthrough_env).
FAKE_TURN_ENV = {
    "PATH": "/opt/agent-test:/opt/mise/shims:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin"
    ":/sbin:/bin",
    "FAKE_CLAUDE_SCENARIO": "/opt/agent-test/scenario.json",
}
SLOW_PROMPT = "slow task {n}: report when done"
DISPATCH_STAGGER = 1.5  # seconds between dispatching message 1 and sending message 2

# Run inside an agent pod: every session's claude config (.claude.json, settings.json).
SESSION_CONFIGS = (
    "import glob, json, os\n"
    "out = []\n"
    "for d in sorted(glob.glob('/sessions/*/config')):\n"
    "    docs = [json.load(open(os.path.join(d, f))) for f in ('.claude.json', 'settings.json')]\n"
    "    out.append(docs)\n"
    "print(json.dumps(out))\n"
)

READY_TIMEOUT = 900  # onStart pip-installs any-mcp from GitHub in every pod
TURN_TIMEOUT = 300
DISCARD_TIMEOUT = 180


# ------------------------------------------------------------------ helpers


def run(*argv: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(argv)} exited {proc.returncode}\nstdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def kubectl(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    return run("kubectl", "-n", NAMESPACE, *args, check=check, timeout=timeout)


def kube_json(*args: str) -> dict[str, Any]:
    return json.loads(kubectl(*args, "-o", "json").stdout)


def wait_for(what: str, probe: Callable[[], Any], timeout: float, interval: float = 2.0) -> Any:
    """Poll `probe` until it returns a truthy value; fail naming `what` and the last value."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        last = probe()
        if last:
            return last
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}: {last!r}")
        time.sleep(interval)


def labelled_objects(slug: str) -> list[str]:
    out = kubectl(
        "get",
        "statefulsets,services,configmaps,secrets,persistentvolumeclaims,pods",
        "-l",
        f"{CLUSTER_LABEL}={slug}",
        "-o",
        "name",
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def fingerprint(kind: str, name: str) -> dict[str, Any]:
    """What must not change on an orchestrator object: identity, spec generation and data."""
    obj = kube_json("get", kind, name)
    meta = obj["metadata"]
    out: dict[str, Any] = {"uid": meta["uid"], "generation": meta.get("generation")}
    if kind in ("configmap", "secret"):
        out["resourceVersion"] = meta["resourceVersion"]
        out["data"] = obj.get("data")
    if kind == "service":
        out["spec"] = obj["spec"]
    return out


def orchestrator_objects() -> list[tuple[str, str]]:
    """The chart's own Deployments, Service, ConfigMap and Secrets, plus the e2e Postgres."""
    names: list[tuple[str, str]] = []
    for kind in ("deployment", "service", "configmap", "secret", "serviceaccount", "role"):
        out = kubectl(
            "get", kind, "-l", f"app.kubernetes.io/instance={RELEASE}", "-o", "name"
        ).stdout
        names += [(kind, line.split("/", 1)[1]) for line in out.splitlines() if line.strip()]
    names += [("deployment", "postgres"), ("service", "postgres")]
    return sorted(set(names))


def any_mcp_ref() -> str:
    data = kube_json("get", "configmap", f"{RELEASE}-settings").get("data") or {}
    return data.get("ORCH_ANY_MCP_REF") or Settings.model_fields["any_mcp_ref"].default


def agents_of(api: httpx.Client, cluster_id: str) -> list[dict[str, Any]]:
    resp = api.get(f"/clusters/{cluster_id}/agents")
    resp.raise_for_status()
    return resp.json()


def all_events(api: httpx.Client, cluster_id: str) -> list[list[dict[str, Any]]]:
    """Every page of the merged timeline, newest page first, each page as returned."""
    pages: list[list[dict[str, Any]]] = []
    params: dict[str, Any] = {"limit": 500}
    while True:
        resp = api.get(f"/clusters/{cluster_id}/events", params=params)
        resp.raise_for_status()
        body = resp.json()
        pages.append(body["events"])
        if not body["has_more_before"] or not body["events"]:
            return pages
        params = {"limit": 500, "before_id": body["events"][0]["id"]}


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module", autouse=True)
def kind_context() -> None:
    """Refuse to touch anything but the e2e kind cluster (kubectl and helm follow KUBECONFIG)."""
    current = run("kubectl", "config", "current-context", check=False).stdout.strip()
    if current != KUBE_CONTEXT:
        pytest.fail(
            f"kube context is {current!r}, not {KUBE_CONTEXT!r}: run through scripts/e2e.sh "
            "(mise run test:e2e), which creates the kind cluster and points KUBECONFIG at it"
        )


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"{API_URL}/api/v1", timeout=30) as client:
        yield client


@pytest.fixture(scope="module")
def baseline() -> dict[tuple[str, str], dict[str, Any]]:
    """Fingerprints of the orchestrator's own objects before any agent cluster exists."""
    objs = orchestrator_objects()
    assert ("deployment", f"{RELEASE}-reconciler") in objs, objs
    return {o: fingerprint(*o) for o in objs}


@pytest.fixture(scope="module")
def cluster(api: httpx.Client, baseline: dict) -> dict[str, Any]:
    """A 2-replica, peers-on cluster created through the API, with 2 idle agents."""
    resp = api.post(
        "/clusters",
        json={"slug": SLUG, "spec": {"replicas": 2, "peers": True, "env": FAKE_TURN_ENV}},
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    cid = created["id"]

    def two_idle_agents() -> list[dict[str, Any]] | None:
        view = api.get(f"/clusters/{cid}").json()
        if view["status"].get("error"):
            raise AssertionError(f"cluster reconcile error: {view['status']}")
        agents = [a for a in agents_of(api, cid) if not a["gone"]]
        ready = [a for a in agents if a["ready"] and a["state"] == "idle" and a["free_since"]]
        return ready if len(ready) == 2 else None

    try:
        agents = wait_for("2 ready idle agents with sessions", two_idle_agents, READY_TIMEOUT)
    except AssertionError:
        print(kubectl("get", "pods,pvc", "-l", f"{CLUSTER_LABEL}={SLUG}", check=False).stdout)
        print(kubectl("describe", "pods", "-l", f"{CLUSTER_LABEL}={SLUG}", check=False).stdout)
        print(kubectl("logs", "-l", f"{CLUSTER_LABEL}={SLUG}", "--tail=80", check=False).stdout)
        raise
    return {**created, "agents": agents}


def message(api: httpx.Client, mid: int) -> dict[str, Any]:
    view = api.get(f"/messages/{mid}").json()
    if view["status"] == "failed":
        raise AssertionError(f"message {mid} failed: {view['error']}")
    return view


def first_idle_after(events: list[dict[str, Any]], agent_id: str, after_id: int) -> int:
    """Id of the agent's first session_state `ready` (idle) event newer than `after_id`."""
    return next(
        e["id"]
        for e in events
        if e["agent_id"] == agent_id
        and e["id"] > after_id
        and e["type"] == "session_state"
        and (e["data"] or {}).get("state") == "ready"
    )


@pytest.fixture(scope="module")
def round_robin(api: httpx.Client, cluster: dict[str, Any]) -> dict[str, Any]:
    """Three slow (4 s) messages; each awaited until its turn completed.

    The second is sent DISPATCH_STAGGER seconds after the first was dispatched and the third
    right after the second was dispatched, so both agents are busy when the third arrives and
    the first agent frees well before the second.
    """
    cid = cluster["id"]
    by_free = sorted(agents_of(api, cid), key=lambda a: a["free_since"])

    def send(n: int) -> int:
        resp = api.post(f"/clusters/{cid}/messages", json={"text": SLOW_PROMPT.format(n=n)})
        assert resp.status_code == 202, resp.text
        return resp.json()["message_id"]

    def dispatched(mid: int) -> Callable[[], dict[str, Any] | None]:
        def probe() -> dict[str, Any] | None:
            view = message(api, mid)
            return view if view["status"] == "sent" and view["agent_id"] else None

        return probe

    ids = [send(1)]
    wait_for("message 1 dispatched", dispatched(ids[0]), TURN_TIMEOUT, interval=0.2)
    time.sleep(DISPATCH_STAGGER)
    ids.append(send(2))
    wait_for("message 2 dispatched", dispatched(ids[1]), TURN_TIMEOUT, interval=0.2)

    def both_busy() -> dict[str, str] | None:
        states = {a["id"]: a["state"] for a in agents_of(api, cid)}
        return states if set(states.values()) == {"busy"} else None

    busy = wait_for("both agents busy", both_busy, 30, interval=0.2)
    ids.append(send(3))

    def done() -> list[dict[str, Any]] | None:
        views = [message(api, mid) for mid in ids]
        finished = all(
            v["status"] == "sent" and v["turn"] and v["turn"]["completed_at"] for v in views
        )
        return views if finished else None

    messages = wait_for("3 completed turns", done, TURN_TIMEOUT, interval=1)
    events = sorted((e for page in all_events(api, cid) for e in page), key=lambda e: e["id"])
    started = {
        e["turn_id"]: e["id"] for e in events if e["type"] == "turn_started" and e["turn_id"]
    }
    both_started = max(started[m["turn_id"]] for m in messages[:2])
    freed = {
        m["agent_id"]: first_idle_after(events, m["agent_id"], both_started) for m in messages[:2]
    }
    return {
        "by_free_since": by_free,
        "busy_before_third": busy,
        "messages": messages,
        "freed_event_id": freed,
    }


@pytest.fixture(scope="module")
def discarded(
    api: httpx.Client, cluster: dict[str, Any], round_robin: dict[str, Any]
) -> dict[str, Any]:
    """DELETE the cluster and wait until no labelled object remains (at most 180 s)."""
    cid = cluster["id"]
    resp = api.delete(f"/clusters/{cid}")
    assert resp.status_code == 202, resp.text
    started = time.monotonic()
    remaining: list[str] = []

    def gone() -> bool:
        nonlocal remaining
        remaining = labelled_objects(SLUG)
        return not remaining and api.get(f"/clusters/{cid}").status_code == 404

    wait_for(f"cluster {SLUG} objects to be deleted ({remaining})", gone, DISCARD_TIMEOUT)
    return {"cluster": cluster, "elapsed": time.monotonic() - started}


# ------------------------------------------------------------------ tests


def test_chart_installed_and_helm_test_passes(api: httpx.Client) -> None:
    status = json.loads(run("helm", "status", RELEASE, "-n", NAMESPACE, "-o", "json").stdout)
    assert status["info"]["status"] == "deployed", status["info"]
    for component in COMPONENTS:
        dep = kube_json("get", "deployment", f"{RELEASE}-{component}")
        assert dep["status"].get("availableReplicas", 0) >= 1, (component, dep["status"])
        init = dep["spec"]["template"]["spec"]["initContainers"]
        assert [c["args"] for c in init] == [["migrate"]], init
    proc = run("helm", "test", RELEASE, "-n", NAMESPACE, "--timeout", "3m")
    assert "Phase:" in proc.stdout and "Succeeded" in proc.stdout, proc.stdout
    assert api.get("/meta").status_code == 200


def test_round_robin_by_free_since(round_robin: dict[str, Any]) -> None:
    by_free = round_robin["by_free_since"]
    m1, m2, m3 = round_robin["messages"]
    assert len({a["id"] for a in by_free}) == 2
    # the first two go to different agents, the longest-free one first
    assert m1["agent_id"] == by_free[0]["id"], (m1, by_free)
    assert m2["agent_id"] == by_free[1]["id"], (m2, by_free)
    assert m1["agent_id"] != m2["agent_id"]
    # both were busy when the third arrived; it goes to the agent that freed first, which
    # is the one whose idle transition (free_since) the ingester recorded first
    assert set(round_robin["busy_before_third"].values()) == {"busy"}, round_robin
    freed = round_robin["freed_event_id"]
    first_freed = min(freed, key=freed.__getitem__)
    assert first_freed == m1["agent_id"], (freed, m1, m2)
    assert m3["agent_id"] == first_freed, (freed, m1, m2, m3)
    for m in (m1, m2, m3):
        assert m["attempts"] == 0 and m["error"] is None, m  # sent first time, no retry
        assert m["turn_id"], m


def test_merged_event_stream(
    api: httpx.Client, cluster: dict[str, Any], round_robin: dict[str, Any]
) -> None:
    pages = all_events(api, cluster["id"])
    for page in pages:
        ids = [e["id"] for e in page]
        assert ids == sorted(ids) and len(set(ids)) == len(ids), ids
    events = sorted((e for page in pages for e in page), key=lambda e: e["id"])
    agent_ids = {a["id"] for a in cluster["agents"]}
    assert {e["agent_id"] for e in events} == agent_ids
    assert all(e["cluster_id"] == cluster["id"] for e in events)
    # interleaved: the timeline switches between the agents more than once
    sequence = [e["agent_id"] for e in events]
    switches = sum(1 for a, b in zip(sequence, sequence[1:], strict=False) if a != b)
    assert switches >= 2, sequence
    # each agent's own events keep their per-session order inside the merged timeline
    for agent in agent_ids:
        own = [(e["session_id"], e["seq"]) for e in events if e["agent_id"] == agent]
        for session in {s for s, _ in own}:
            seqs = [q for s, q in own if s == session]
            assert seqs == sorted(seqs), (agent, session, seqs)
    turn_ids = {e["turn_id"] for e in events if e["turn_id"]}
    assert {m["turn_id"] for m in round_robin["messages"]} <= turn_ids


def test_any_mcp_installed_by_on_start(cluster: dict[str, Any]) -> None:
    ref = any_mcp_ref()
    cm = kube_json("get", "configmap", f"agent-{SLUG}-config")["data"]
    assert f"REF={ref}\n" in cm["on-start.sh"], cm["on-start.sh"]
    rendered = yaml.safe_load(cm["config.yaml"])
    claude = rendered["claude"]
    assert claude["mcp_servers"]["any-mcp"] == {
        "type": "stdio",
        "command": "/opt/any-mcp/bin/any-mcp",
        "args": ["--config", "/opt/any-mcp/etc/any-mcp.yaml"],
    }
    assert "AskUserQuestion" in claude["permissions"]["deny"]
    assert rendered["sessions"]["ask_user_question"] == "deny"
    assert rendered["hooks"]["onStart"][0]["run"] == "sh /etc/ccw/on-start.sh"

    pods = [a["pod_name"] for a in cluster["agents"]]
    assert len(pods) == 2
    for pod in pods:
        assert kubectl("exec", pod, "-c", "ccw", "--", "cat", "/opt/any-mcp/.ref").stdout == (
            ref + "\n"
        ), pod
        kubectl("exec", pod, "-c", "ccw", "--", "/opt/any-mcp/bin/any-mcp", "--help")
        kubectl("exec", pod, "-c", "ccw", "--", "test", "-s", "/opt/any-mcp/etc/any-mcp.yaml")
        # the session the wrapper started registers the stdio server and denies the tool
        found = kubectl("exec", pod, "-c", "ccw", "--", "python3", "-c", SESSION_CONFIGS).stdout
        sessions = json.loads(found)
        assert sessions, (pod, "no session config under /sessions")
        for claude_json, settings in sessions:
            server = claude_json["mcpServers"]["any-mcp"]
            assert server["command"] == "/opt/any-mcp/bin/any-mcp", (pod, server)
            assert server["args"] == ["--config", "/opt/any-mcp/etc/any-mcp.yaml"], (pod, server)
            assert "AskUserQuestion" in settings["permissions"]["deny"], (pod, settings)


def test_reconciler_role_cannot_read_secrets() -> None:
    def can(verb: str, resource: str, *extra: str) -> bool:
        proc = kubectl("auth", "can-i", verb, resource, *extra, "--as", RECONCILER_SA, check=False)
        answer = proc.stdout.strip()
        assert answer in ("yes", "no"), (verb, resource, proc.stdout, proc.stderr)
        return answer == "yes"

    assert can("create", "secrets")
    assert can("patch", "secrets")
    assert can("delete", "secrets")
    assert not can("get", "secrets")
    assert not can("list", "secrets")
    assert not can("watch", "secrets")
    assert not can("create", "pods", "--subresource=exec")
    assert not can("get", "pods", "--subresource=exec")
    # what the reconciler does need, it has
    assert can("create", "statefulsets.apps")
    assert can("delete", "persistentvolumeclaims")
    assert can("get", "pods", "--subresource=log")
    role = kube_json("get", "role", f"{RELEASE}-reconciler")
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    assert "pods/exec" not in resources, role["rules"]


def test_in_cluster_pod_refused_by_allowlist(api: httpx.Client) -> None:
    image = kube_json("get", "deployment", f"{RELEASE}-api")["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]
    pod = f"allowlist-probe-{uuid.uuid4().hex[:6]}"
    service = f"http://{RELEASE}.{NAMESPACE}.svc"
    script = (
        "import json, urllib.request, urllib.error\n"
        "out = {}\n"
        "for path in ('/api/v1/clusters', '/healthz'):\n"
        "    try:\n"
        f"        r = urllib.request.urlopen('{service}' + path, timeout=10)\n"
        "        out[path] = [r.status, r.read().decode()]\n"
        "    except urllib.error.HTTPError as e:\n"
        "        out[path] = [e.code, e.read().decode()]\n"
        "print('RESULT ' + json.dumps(out))\n"
    )
    overrides = {
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-c", script],
                }
            ],
        }
    }
    kubectl(
        "run", pod, f"--image={image}", "--restart=Never", f"--overrides={json.dumps(overrides)}"
    )
    try:
        kubectl("wait", f"pod/{pod}", "--for=jsonpath={.status.phase}=Succeeded", "--timeout=180s")
        logs = kubectl("logs", pod).stdout
    finally:
        kubectl("delete", "pod", pod, "--wait=false", check=False)
    line = next(ln for ln in logs.splitlines() if ln.startswith("RESULT "))
    result = json.loads(line.removeprefix("RESULT "))
    code, body = result["/api/v1/clusters"]
    assert code == 403, result
    assert json.loads(body)["error"]["code"] == "forbidden", body
    assert result["/healthz"][0] == 200, result  # health routes are exempt
    # the port-forwarded caller arrives from loopback, inside ORCH_ALLOWED_CIDRS
    resp = api.get("/clusters")
    assert resp.status_code == 200, resp.text


def test_discard_removes_objects_and_history(
    api: httpx.Client, discarded: dict[str, Any], round_robin: dict[str, Any]
) -> None:
    cid = discarded["cluster"]["id"]
    assert discarded["elapsed"] <= DISCARD_TIMEOUT
    assert labelled_objects(SLUG) == []
    for kind in ("statefulset", "service", "configmap", "persistentvolumeclaim"):
        names = kubectl("get", kind, "-o", "name").stdout
        assert f"agent-{SLUG}" not in names, (kind, names)
    # the Secret is checked by name: it is deleted by its deterministic name
    assert kubectl("get", "secret", f"agent-{SLUG}-ccw", check=False).returncode != 0
    cluster_resp = api.get(f"/clusters/{cid}")
    assert cluster_resp.status_code == 404, cluster_resp.text
    assert cluster_resp.json()["error"]["code"] == "not_found"
    events_resp = api.get(f"/clusters/{cid}/events")
    assert events_resp.status_code == 404, events_resp.text
    for m in round_robin["messages"]:
        assert api.get(f"/messages/{m['id']}").status_code == 404, m
    assert all(c["id"] != cid for c in api.get("/clusters").json())


def test_shared_namespace_leaves_orchestrator_objects(
    baseline: dict[tuple[str, str], dict[str, Any]], discarded: dict[str, Any]
) -> None:
    assert orchestrator_objects() == sorted(baseline)
    for (kind, name), before in baseline.items():
        assert fingerprint(kind, name) == before, (kind, name)
    for component in COMPONENTS:
        dep = kube_json("get", "deployment", f"{RELEASE}-{component}")
        assert dep["status"].get("availableReplicas", 0) >= 1, (component, dep["status"])
    pg = kube_json("get", "deployment", "postgres")
    assert pg["status"].get("availableReplicas", 0) >= 1, pg["status"]
