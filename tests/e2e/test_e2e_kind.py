"""kind end-to-end: the chart installed into namespace `agents` (D9, D19, D22, D26, D28).

Run by `scripts/e2e.sh` (`mise run test:e2e`), which creates the kind cluster, installs the
chart with e2e/values-kind.yaml, runs `helm test`, port-forwards the api Service to
ORCH_E2E_API_URL and then runs this module. The tests share one agent session and run in
file order: the session is scheduled from a preset created through POST /presets, the
round-robin fixture sends the messages the event-stream test reads, the preset tests run after
them, and the discard fixture runs last, so the shared-namespace test compares against a
baseline taken before the session existed. Every fixture is lazy, so a single test can also run
alone:
`uv run pytest tests/e2e/test_e2e_kind.py::<name>` brings the same environment up itself
(tests/e2e/conftest.py runs scripts/e2e.sh in hold mode and deletes the kind cluster afterwards).

The test (never the orchestrator) uses kubectl exec to look inside agent pods.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import yaml
from websockets.exceptions import InvalidStatus
from websockets.sync.client import ClientConnection, connect

from agents_orchestrator.settings import Settings

pytestmark = pytest.mark.e2e

NAMESPACE = os.environ.get("ORCH_E2E_NAMESPACE", "agents")
RELEASE = os.environ.get("ORCH_E2E_RELEASE", "agents-orchestrator")
# not a secret: its sha256 is serviceApi.tokenHashes.e2e in e2e/values-kind.yaml
E2E_SERVICE_TOKEN = "e2e-service-token-not-a-secret"
API_URL = os.environ.get("ORCH_E2E_API_URL", "http://127.0.0.1:18080")
KUBE_CONTEXT = os.environ.get("ORCH_E2E_KUBE_CONTEXT", "kind-agents-orchestrator-e2e")
SLUG = "e2e-rr"
PRESET_NAME = "E2E two replicas"
GENERATED_SLUG = re.compile(r"^default-[a-z]{4}$")
SESSION_LABEL = "agents.szymonrichert.pl/session"
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
LEADER_TIMEOUT = 90  # election and handover are applied within the dispatcher poll
HIVE_PUSH_TIMEOUT = 150  # hive push every 10 s, feed rows settle for 5 s
ALONE_SLUG = "e2e-alone"
SHELL_TIMEOUT = 60
EVIL_ORIGIN = "http://evil.example"


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
        f"{SESSION_LABEL}={slug}",
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


def agents_of(api: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    resp = api.get(f"/sessions/{session_id}/agents")
    resp.raise_for_status()
    return resp.json()


def all_events(api: httpx.Client, session_id: str) -> list[list[dict[str, Any]]]:
    """Every page of the merged timeline, newest page first, each page as returned."""
    pages: list[list[dict[str, Any]]] = []
    params: dict[str, Any] = {"limit": 500}
    while True:
        resp = api.get(f"/sessions/{session_id}/events", params=params)
        resp.raise_for_status()
        body = resp.json()
        pages.append(body["events"])
        if not body["has_more_before"] or not body["events"]:
            return pages
        params = {"limit": 500, "before_id": body["events"][0]["id"]}


def leader_view(api: httpx.Client, session_id: str) -> tuple[dict[str, Any] | None, int | None]:
    """(the leader's agent view, the epoch) from the public session view; (None, None) before
    the first election."""
    view = api.get(f"/sessions/{session_id}").json()
    leader_id = view.get("leader_agent_id")
    if not leader_id:
        return None, None
    leader = next((a for a in agents_of(api, session_id) if a["id"] == leader_id), None)
    return leader, view["leader_epoch"]


def wait_leader(
    api: httpx.Client, session_id: str, epoch: int, agent_id: str | None = None
) -> dict[str, Any]:
    """Wait (at most LEADER_TIMEOUT) until the session view shows `epoch` (and `agent_id`)."""

    def probe() -> dict[str, Any] | None:
        leader, current = leader_view(api, session_id)
        ok = leader and current == epoch and (agent_id is None or leader["id"] == agent_id)
        return leader if ok else None

    return wait_for(f"leader at epoch {epoch}", probe, LEADER_TIMEOUT)


# Run inside an agent pod: one agent-API call with the pod's own URL, token and session id.
# argv: method, path, JSON body or "", "token"/"notoken". Prints `RESULT [status, body]`.
AGENT_CALL = (
    "import json, os, sys, urllib.request, urllib.error\n"
    "method, path, body, auth = sys.argv[1:5]\n"
    "headers = {'X-Orch-Session': os.environ['ORCH_SESSION_ID']}\n"
    "if auth == 'token':\n"
    "    headers['Authorization'] = 'Bearer ' + os.environ['ORCH_AGENT_TOKEN']\n"
    "data = None\n"
    "if body:\n"
    "    data = body.encode()\n"
    "    headers['Content-Type'] = 'application/json'\n"
    "url = os.environ['ORCH_AGENT_URL'] + path\n"
    "req = urllib.request.Request(url, data, headers, method=method)\n"
    "try:\n"
    "    r = urllib.request.urlopen(req, timeout=15)\n"
    "    out = [r.status, r.read().decode()]\n"
    "except urllib.error.HTTPError as e:\n"
    "    out = [e.code, e.read().decode()]\n"
    "print('RESULT ' + json.dumps(out))\n"
)


def pod_exec(pod: str, *argv: str) -> str:
    return kubectl("exec", pod, "-c", "ccw", "--", *argv, timeout=60).stdout


def agent_call(
    pod: str, method: str, path: str, body: dict[str, Any] | None = None, token: bool = True
) -> tuple[int, Any]:
    """Call the agent API from inside `pod` (kubectl exec + python3 urllib); (status, JSON)."""
    out = pod_exec(
        pod,
        "python3",
        "-c",
        AGENT_CALL,
        method,
        path,
        json.dumps(body) if body is not None else "",
        "token" if token else "notoken",
    )
    line = next(ln for ln in out.splitlines() if ln.startswith("RESULT "))
    status, text = json.loads(line.removeprefix("RESULT "))
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def pod_name_of(agent: dict[str, Any]) -> str:
    return agent["pod_name"]


def on_start_any_mcp(configmap: dict[str, Any]) -> dict[str, Any]:
    """The any-mcp JSON a session's on-start.sh writes (the heredoc body)."""
    script = configmap["data"]["on-start.sh"]
    match = re.search(r"<<'(\w+)'\n(.*?)\n\1\n", script, re.DOTALL)
    assert match, script
    return json.loads(match.group(2))


def ws_url(api: httpx.Client, agent_id: str, terminal_id: str) -> str:
    """The attach WebSocket URL on the same host and port as the REST API port-forward."""
    base = str(api.base_url).rstrip("/").replace("http", "ws", 1)
    return f"{base}/agents/{agent_id}/terminals/{terminal_id}/attach"


def attach(api: httpx.Client, agent_id: str, terminal_id: str, **kwargs: Any) -> ClientConnection:
    """A headless attach: no Origin unless given, and never through an environment proxy."""
    return connect(ws_url(api, agent_id, terminal_id), proxy=None, **kwargs)


def read_until(ws: ClientConnection, pattern: str, timeout: float = SHELL_TIMEOUT) -> str:
    """Collect PTY output (binary frames; text frames are control JSON) until `pattern` matches.

    The pattern is matched per line against the output with every CR, CRLF or LF turned into one
    newline: a pty echoes the typed command ending in CRLF and then prints the command's output
    after a bare CR, so `^ok$` finds the output line and never the echoed `echo ok`.
    """
    deadline = time.monotonic() + timeout
    seen = ""
    while not re.search(pattern, re.sub(r"\r\n?", "\n", seen), re.MULTILINE):
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"no match for {pattern!r} within {timeout:.0f}s in {seen!r}")
        try:
            frame = ws.recv(timeout=left)
        except TimeoutError:
            continue
        if isinstance(frame, bytes):
            seen += frame.decode(errors="replace")
    return seen


def terminals_of(api: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    resp = api.get(f"/sessions/{session_id}/terminals")
    assert resp.status_code == 200, resp.text
    return resp.json()["terminals"]


@contextlib.contextmanager
def shell(api: httpx.Client, agent_id: str) -> Iterator[dict[str, Any]]:
    """A terminal created through POST /agents/{id}/terminals; deleted on exit if still there."""
    made = api.post(f"/agents/{agent_id}/terminals", json={"rows": 24, "cols": 80})
    assert made.status_code == 201, made.text
    term = made.json()
    try:
        yield term
    finally:
        api.delete(f"/agents/{agent_id}/terminals/{term['terminal_id']}")


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
    # read at fixture time: tests/e2e/conftest.py may have brought the environment up
    base = os.environ.get("ORCH_E2E_API_URL", API_URL)
    with httpx.Client(base_url=f"{base}/api/v1", timeout=30) as client:
        yield client


@pytest.fixture(scope="module")
def baseline() -> dict[tuple[str, str], dict[str, Any]]:
    """Fingerprints of the orchestrator's own objects before any agent session exists."""
    objs = orchestrator_objects()
    assert ("deployment", f"{RELEASE}-reconciler") in objs, objs
    return {o: fingerprint(*o) for o in objs}


@pytest.fixture(scope="module")
def preset(api: httpx.Client) -> Iterator[dict[str, Any]]:
    """The 2-replica, peers-on preset the shared session is scheduled from (POST /presets)."""
    resp = api.post(
        "/presets",
        json={
            "name": PRESET_NAME,
            "spec": {"replicas": 2, "peers": True, "env": FAKE_TURN_ENV},
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    try:
        yield created
    finally:  # the edit-and-delete test already removed it; a lone run may not have
        api.delete(f"/presets/{created['id']}")


@pytest.fixture(scope="module")
def session(api: httpx.Client, baseline: dict, preset: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """A 2-replica, peers-on session scheduled from a preset, with 2 idle agents."""
    resp = api.post("/sessions", json={"preset_id": preset["id"], "slug": SLUG})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    cid = created["id"]
    assert created["slug"] == SLUG, created
    assert created["preset_id"] == preset["id"], created
    assert created["preset_name"] == PRESET_NAME, created

    def two_idle_agents() -> list[dict[str, Any]] | None:
        view = api.get(f"/sessions/{cid}").json()
        if view["status"].get("error"):
            raise AssertionError(f"session reconcile error: {view['status']}")
        agents = [a for a in agents_of(api, cid) if not a["gone"]]
        ready = [a for a in agents if a["ready"] and a["state"] == "idle" and a["free_since"]]
        return ready if len(ready) == 2 else None

    try:
        agents = wait_for("2 ready idle agents with sessions", two_idle_agents, READY_TIMEOUT)
    except AssertionError:
        print(kubectl("get", "pods,pvc", "-l", f"{SESSION_LABEL}={SLUG}", check=False).stdout)
        print(kubectl("describe", "pods", "-l", f"{SESSION_LABEL}={SLUG}", check=False).stdout)
        print(kubectl("logs", "-l", f"{SESSION_LABEL}={SLUG}", "--tail=80", check=False).stdout)
        raise
    try:
        yield {**created, "agents": agents}
    finally:  # the discard fixture normally removed it already (404 then); never leave it behind
        api.delete(f"/sessions/{cid}")


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
def round_robin(api: httpx.Client, session: dict[str, Any]) -> dict[str, Any]:
    """Three slow (4 s) messages; each awaited until its turn completed.

    The session is a hive, so untargeted messages go to the leader: the first is untargeted
    (the leader), the second targets the other agent explicitly (an explicit target wins) and
    the third is untargeted again and waits for the leader. The second is sent DISPATCH_STAGGER
    seconds after the first was dispatched and the third right after the second was dispatched,
    so both agents are busy when the third arrives and the leader frees well before the other.
    """
    cid = session["id"]
    leader = wait_for("a leader", lambda: leader_view(api, cid)[0], LEADER_TIMEOUT)
    other = next(a for a in agents_of(api, cid) if a["id"] != leader["id"])

    def send(n: int, target: str | None = None) -> int:
        body: dict[str, Any] = {"text": SLOW_PROMPT.format(n=n)}
        if target:
            body["target_agent_id"] = target
        resp = api.post(f"/sessions/{cid}/messages", json=body)
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
    ids.append(send(2, other["id"]))
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
        "leader": leader,
        "other": other,
        "busy_before_third": busy,
        "messages": messages,
        "freed_event_id": freed,
    }


@pytest.fixture(scope="module")
def discarded(
    api: httpx.Client, session: dict[str, Any], round_robin: dict[str, Any]
) -> dict[str, Any]:
    """DELETE the session and wait until no labelled object remains (at most 180 s)."""
    cid = session["id"]
    resp = api.delete(f"/sessions/{cid}")
    assert resp.status_code == 202, resp.text
    started = time.monotonic()
    remaining: list[str] = []

    def gone() -> bool:
        nonlocal remaining
        remaining = labelled_objects(SLUG)
        return not remaining and api.get(f"/sessions/{cid}").status_code == 404

    wait_for(f"session {SLUG} objects to be deleted ({remaining})", gone, DISCARD_TIMEOUT)
    return {"session": session, "elapsed": time.monotonic() - started}


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
    leader, other = round_robin["leader"], round_robin["other"]
    m1, m2, m3 = round_robin["messages"]
    # untargeted goes to the leader of a hive, an explicit target wins
    assert m1["agent_id"] == leader["id"], (m1, leader)
    assert m2["agent_id"] == other["id"], (m2, other)
    # both were busy when the third arrived; it waited for the leader, which freed first
    assert set(round_robin["busy_before_third"].values()) == {"busy"}, round_robin
    freed = round_robin["freed_event_id"]
    first_freed = min(freed, key=freed.__getitem__)
    assert first_freed == m1["agent_id"], (freed, m1, m2)
    assert m3["agent_id"] == leader["id"] == first_freed, (freed, m1, m2, m3)
    for m in (m1, m2, m3):
        assert m["attempts"] == 0 and m["error"] is None, m  # sent first time, no retry
        assert m["turn_id"], m


def test_merged_event_stream(
    api: httpx.Client, session: dict[str, Any], round_robin: dict[str, Any]
) -> None:
    pages = all_events(api, session["id"])
    for page in pages:
        ids = [e["id"] for e in page]
        assert ids == sorted(ids) and len(set(ids)) == len(ids), ids
    events = sorted((e for page in pages for e in page), key=lambda e: e["id"])
    agent_ids = {a["id"] for a in session["agents"]}
    assert {e["agent_id"] for e in events} == agent_ids
    assert all(e["session_id"] == session["id"] for e in events)
    # interleaved: the timeline switches between the agents more than once
    sequence = [e["agent_id"] for e in events]
    switches = sum(1 for a, b in zip(sequence, sequence[1:], strict=False) if a != b)
    assert switches >= 2, sequence
    # each agent's own events keep their per-wrapper-session order inside the merged timeline
    for agent in agent_ids:
        own = [(e["wrapper_session_id"], e["seq"]) for e in events if e["agent_id"] == agent]
        for wrapper_session in {s for s, _ in own}:
            seqs = [q for s, q in own if s == wrapper_session]
            assert seqs == sorted(seqs), (agent, wrapper_session, seqs)
    turn_ids = {e["turn_id"] for e in events if e["turn_id"]}
    assert {m["turn_id"] for m in round_robin["messages"]} <= turn_ids


def test_any_mcp_installed_by_on_start(session: dict[str, Any]) -> None:
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

    pods = [a["pod_name"] for a in session["agents"]]
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


def test_in_session_pod_refused_by_allowlist(api: httpx.Client) -> None:
    image = kube_json("get", "deployment", f"{RELEASE}-api")["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]
    pod = f"allowlist-probe-{uuid.uuid4().hex[:6]}"
    service = f"http://{RELEASE}.{NAMESPACE}.svc"
    script = (
        "import json, urllib.request, urllib.error\n"
        "out = {}\n"
        "for path in ('/api/v1/sessions', '/healthz'):\n"
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
    code, body = result["/api/v1/sessions"]
    assert code == 403, result
    assert json.loads(body)["error"]["code"] == "forbidden", body
    assert result["/healthz"][0] == 200, result  # health routes are exempt
    # the port-forwarded caller arrives from loopback, inside ORCH_ALLOWED_CIDRS
    resp = api.get("/sessions")
    assert resp.status_code == 200, resp.text


def test_in_session_service_token(api: httpx.Client) -> None:
    image = kube_json("get", "deployment", f"{RELEASE}-api")["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]
    pod = f"service-probe-{uuid.uuid4().hex[:6]}"
    services = f"http://{RELEASE}-services.{NAMESPACE}.svc:8002"
    main = f"http://{RELEASE}.{NAMESPACE}.svc"
    script = (
        "import json, urllib.request, urllib.error\n"
        "def call(method, url, token):\n"
        "    req = urllib.request.Request(url, method=method)\n"
        "    if token:\n"
        "        req.add_header('Authorization', 'Bearer ' + token)\n"
        "    try:\n"
        "        return urllib.request.urlopen(req, timeout=10).status\n"
        "    except urllib.error.HTTPError as e:\n"
        "        return e.code\n"
        f"tok = '{E2E_SERVICE_TOKEN}'\n"
        "out = {\n"
        f"    'with': call('GET', '{services}/api/v1/presets', tok),\n"
        f"    'without': call('GET', '{services}/api/v1/presets', None),\n"
        f"    'delete': call('DELETE', '{services}/api/v1/sessions/does-not-matter', tok),\n"
        f"    'main': call('GET', '{main}/api/v1/presets', tok),\n"
        "}\n"
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
    assert result == {"with": 200, "without": 401, "delete": 403, "main": 403}, result


def test_files_list_and_read_through_api(api: httpx.Client, session: dict[str, Any]) -> None:
    cid = session["id"]
    agent_id = session["agents"][0]["id"]
    note = f"e2e-note-{uuid.uuid4().hex[:8]}.txt"
    text = f"hello-{uuid.uuid4().hex}"
    # the agent writes a file into the shared workdir through its own shell
    with shell(api, agent_id) as term, attach(api, agent_id, term["terminal_id"]) as ws:
        ws.send(f"printf '%s\\n' {text} > {note}\n".encode())
        ws.send(b"echo written-$((6*7))\n")
        read_until(ws, r"^written-42$")

    listing = api.get(f"/sessions/{cid}/files")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["session_id"] == cid and body["path"] == "", body
    assert body["root"].startswith("/workdir"), body["root"]
    entry = next(e for e in body["entries"] if e["name"] == note)
    assert entry["type"] == "file" and entry["size"] == len(text) + 1, entry

    content = api.get(f"/sessions/{cid}/files/content", params={"path": note})
    assert content.status_code == 200, content.text
    got = content.json()
    assert got["path"] == note and got["kind"] == "text", got
    assert got["content"] == text + "\n", got

    # a path that leaves the workdir is refused, for the listing and for the content
    for route in ("files", "files/content"):
        for bad in ("..", "../etc/passwd", "/etc/passwd"):
            resp = api.get(f"/sessions/{cid}/{route}", params={"path": bad})
            assert resp.status_code == 400, (route, bad, resp.text)
            assert resp.json()["error"]["code"] == "invalid_path", (route, bad, resp.text)


def test_terminal_lifecycle_over_websocket(api: httpx.Client, session: dict[str, Any]) -> None:
    cid = session["id"]
    agent_id = session["agents"][0]["id"]
    root = api.get(f"/sessions/{cid}/files").json()["root"]
    made = api.post(f"/agents/{agent_id}/terminals", json={"rows": 24, "cols": 80})
    assert made.status_code == 201, made.text
    term = made.json()
    tid = term["terminal_id"]
    assert re.fullmatch(r"term_[0-9a-f]{12}", tid), term
    assert term["agent_id"] == agent_id and term["session_id"] == cid, term
    assert term["alive"] is True and (term["rows"], term["cols"]) == (24, 80), term
    try:
        with attach(api, agent_id, tid) as ws:
            ws.send(b"pwd\n")
            read_until(ws, re.escape(root))
            ws.send(b"echo ok\n")
            read_until(ws, r"^ok$")
            ws.send(json.dumps({"type": "ping"}))
            listed = wait_for(
                "the terminal listed as attached",
                lambda: next(
                    (
                        t
                        for t in terminals_of(api, cid)
                        if t["terminal_id"] == tid and t["attached"]
                    ),
                    None,
                ),
                30,
            )
            assert listed["agent_id"] == agent_id and listed["alive"] is True, listed

        # reattach: the wrapper replays the shell's recent output before anything live
        with attach(api, agent_id, tid) as ws:
            replay = read_until(ws, r"^ok$")
            assert root in replay, replay
            ws.send(b"echo again\n")
            read_until(ws, r"^again$")

        deleted = api.delete(f"/agents/{agent_id}/terminals/{tid}")
        assert deleted.status_code == 204, deleted.text
        wait_for(
            "the terminal to be gone from the list",
            lambda: all(t["terminal_id"] != tid for t in terminals_of(api, cid)),
            30,
        )
        again = api.delete(f"/agents/{agent_id}/terminals/{tid}")
        assert again.status_code == 404, again.text
        assert again.json()["error"]["code"] == "terminal_not_found", again.text
    finally:
        api.delete(f"/agents/{agent_id}/terminals/{tid}")


def test_terminal_websocket_refuses_foreign_origin(
    api: httpx.Client, session: dict[str, Any]
) -> None:
    agent_id = session["agents"][0]["id"]
    with shell(api, agent_id) as term:
        with pytest.raises(InvalidStatus) as refused:
            attach(api, agent_id, term["terminal_id"], origin=EVIL_ORIGIN)
        assert refused.value.response.status_code == 403
        # the REST routes apply the same rule
        post = api.post(f"/agents/{agent_id}/terminals", headers={"Origin": EVIL_ORIGIN})
        assert post.status_code == 403, post.text
        assert post.json()["error"]["code"] == "forbidden_origin", post.text
        delete = api.delete(
            f"/agents/{agent_id}/terminals/{term['terminal_id']}", headers={"Origin": EVIL_ORIGIN}
        )
        assert delete.status_code == 403, delete.text
        # the refused attempts opened and closed nothing: the shell is still there and usable
        with attach(api, agent_id, term["terminal_id"]) as ws:
            ws.send(b"echo still-$((6*7))\n")
            read_until(ws, r"^still-42$")


# ------------------------------------------------------------------ hive tests (API only)

WRITE_PROMPT = (
    "write note file"  # the scenario turn that Writes NOTE_FILE (e2e/agent-test.scenario.json)
)
NOTE_FILE = "e2e-note.txt"


def test_agent_api_identity(api: httpx.Client, session: dict[str, Any]) -> None:
    pod0 = f"agent-{SLUG}-0"
    status, me = agent_call(pod0, "GET", "/agent/v1/me")
    assert status == 200, me
    assert me["pod_name"] == pod0 and me["session_id"] == session["id"], me
    assert {m["pod_name"] for m in me["members"]} == {pod0, f"agent-{SLUG}-1"}, me
    assert me["static"] is False and me["max_replicas"] == 2, me

    status, body = agent_call(pod0, "GET", "/agent/v1/me", token=False)
    assert status == 401, body

    # the agents port serves nothing but /agent/v1, /healthz and /readyz
    status, body = agent_call(pod0, "GET", "/api/v1/sessions")
    assert status == 404, body

    # a pod that is not an agent of the session is refused even with a valid token
    token = pod_exec(pod0, "printenv", "ORCH_AGENT_TOKEN").strip()
    url = pod_exec(pod0, "printenv", "ORCH_AGENT_URL").strip()
    image = kube_json("get", "deployment", f"{RELEASE}-api")["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]
    script = (
        "import json, urllib.request, urllib.error\n"
        f"req = urllib.request.Request('{url}/agent/v1/me', headers={{\n"
        f"    'Authorization': 'Bearer {token}', 'X-Orch-Session': '{session['id']}'}})\n"
        "try:\n"
        "    r = urllib.request.urlopen(req, timeout=10)\n"
        "    out = [r.status, r.read().decode()]\n"
        "except urllib.error.HTTPError as e:\n"
        "    out = [e.code, e.read().decode()]\n"
        "print('RESULT ' + json.dumps(out))\n"
    )
    pod = f"identity-probe-{uuid.uuid4().hex[:6]}"
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
    code, text = json.loads(line.removeprefix("RESULT "))
    assert code == 403, (code, text)
    assert json.loads(text)["error"]["code"] == "forbidden", text


def test_leadership_handover_routes(api: httpx.Client, session: dict[str, Any]) -> None:
    cid = session["id"]
    first = wait_leader(api, cid, 1)
    other = next(a for a in agents_of(api, cid) if a["id"] != first["id"])
    status, me = agent_call(first["pod_name"], "GET", "/agent/v1/me")
    assert status == 200 and me["leader"] == first["pod_name"] and me["epoch"] == 1, me

    status, body = agent_call(
        first["pod_name"],
        "POST",
        "/agent/v1/leadership/handover",
        {"to": other["pod_name"], "reason": "e2e handover", "epoch": 1},
    )
    assert status == 202 and body["request_id"], body
    wait_leader(api, cid, 2, other["id"])

    resp = api.post(f"/sessions/{cid}/messages", json={"text": "hello after the handover"})
    assert resp.status_code == 202, resp.text
    mid = resp.json()["message_id"]

    def done() -> dict[str, Any] | None:
        view = message(api, mid)
        turn = view["turn"]
        return view if view["status"] == "sent" and turn and turn["completed_at"] else None

    view = wait_for("untargeted message completed", done, TURN_TIMEOUT, interval=1)
    assert view["agent_id"] == other["id"], (view, other)


def test_user_make_leader(api: httpx.Client, session: dict[str, Any]) -> None:
    cid = session["id"]
    leader, epoch = leader_view(api, cid)
    assert leader is not None and epoch is not None
    target = next(a for a in agents_of(api, cid) if a["id"] != leader["id"])
    resp = api.post(f"/sessions/{cid}/leader", json={"agent_id": target["id"]})
    assert resp.status_code == 202, resp.text
    assert resp.json()["request_id"], resp.text
    wait_leader(api, cid, epoch + 1, target["id"])
    assert next(a for a in agents_of(api, cid) if a["id"] == target["id"])["leader"] is True


def test_advisory_locks(api: httpx.Client, session: dict[str, Any]) -> None:
    cid = session["id"]
    leader, _ = leader_view(api, cid)
    assert leader is not None
    agents = sorted(agents_of(api, cid), key=lambda a: a["ordinal"])
    pod0, pod1 = (pod_name_of(a) for a in agents)
    member = next(pod for pod in (pod0, pod1) if pod != leader["pod_name"])
    path = f"e2e-lock-{uuid.uuid4().hex[:6]}.txt"

    status, body = agent_call(pod0, "POST", "/agent/v1/locks/claim", {"paths": [path]})
    assert status == 200 and body == {"claimed": [path], "held": []}, body

    status, body = agent_call(pod1, "GET", "/agent/v1/locks")
    assert status == 200, body
    assert [(x["path"], x["agent"]) for x in body["locks"] if x["path"] == path] == [(path, pod0)]

    status, body = agent_call(pod1, "POST", "/agent/v1/locks/claim", {"paths": [path]})
    assert status == 200 and body["claimed"] == [], body
    assert body["held"] == [{"path": path, "agent": pod0}], body

    hook = {"data": {"tool_name": "Write", "tool_input": {"file_path": f"/workdir/workdir/{path}"}}}
    status, body = agent_call(pod1, "POST", "/agent/v1/locks/check", hook)
    assert status == 200 and body["decision"] == "allow", body
    assert pod0 in body["additional_context"] and path in body["additional_context"], body

    status, body = agent_call(
        member, "POST", "/agent/v1/locks/reassign", {"path": path, "to": None}
    )
    assert status == 403, body
    assert body["error"]["code"] == "not_leader", body

    status, body = agent_call(
        leader["pod_name"], "POST", "/agent/v1/locks/reassign", {"path": path, "to": None}
    )
    assert status == 200 and body == {"path": path, "agent": None}, body
    status, body = agent_call(pod1, "GET", "/agent/v1/locks")
    assert all(x["path"] != path for x in body["locks"]), body


def test_hive_notes_delivered(api: httpx.Client, session: dict[str, Any]) -> None:
    cid = session["id"]
    writer, reader = sorted(agents_of(api, cid), key=lambda a: a["ordinal"])
    resp = api.post(
        f"/sessions/{cid}/messages",
        json={"text": WRITE_PROMPT, "target_agent_id": writer["id"]},
    )
    assert resp.status_code == 202, resp.text

    def notes() -> list[dict[str, Any]]:
        events = (e for page in all_events(api, cid) for e in page)
        return [e for e in events if e["type"] == "note_received"]

    def feed_on_reader() -> bool:
        return any(
            e["agent_id"] == reader["id"]
            and e["data"].get("key") is None
            and NOTE_FILE in e["data"].get("text", "")
            for e in notes()
        )

    wait_for(
        f"a feed note naming {NOTE_FILE} on the other agent", feed_on_reader, HIVE_PUSH_TIMEOUT, 3
    )
    received = notes()
    for agent in (writer, reader):
        keyed = [
            e
            for e in received
            if e["agent_id"] == agent["id"] and e["data"].get("key") == "hive_state"
        ]
        assert keyed, (agent["pod_name"], received)
        assert "hive-state" in keyed[-1]["data"]["text"], keyed[-1]


def test_alone_session_has_no_hive_tools(api: httpx.Client, session: dict[str, Any]) -> None:
    resp = api.post(
        "/sessions",
        json={
            "slug": ALONE_SLUG,
            "spec": {"replicas": 1, "static": True, "env": FAKE_TURN_ENV},
        },
    )
    assert resp.status_code == 201, resp.text
    alone = resp.json()
    try:
        assert alone["hive"] is False and alone["leader_agent_id"] is None, alone
        name = f"agent-{ALONE_SLUG}-config"

        def rendered() -> dict[str, Any] | None:
            proc = kubectl("get", "configmap", name, "-o", "json", check=False)
            return json.loads(proc.stdout) if proc.returncode == 0 else None

        configmap = wait_for("the alone session's ConfigMap", rendered, 120)
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        assert config["notes"] == {"enabled": False}, config
        assert "peers" not in config or not config["peers"].get("enabled"), config
        assert "onAgentToolCall" not in config["hooks"], config["hooks"]
        plugins = on_start_any_mcp(configmap).get("plugins", [])
        assert "orchestrator" not in {p.get("name") for p in plugins}, plugins

        # the hive session of the same release renders the plugin entry, so the parse is sound
        hive_map = kube_json("get", "configmap", f"agent-{SLUG}-config")
        hive_plugins = on_start_any_mcp(hive_map).get("plugins", [])
        assert "orchestrator" in {p.get("name") for p in hive_plugins}, hive_plugins
        assert yaml.safe_load(hive_map["data"]["config.yaml"])["notes"] == {"enabled": True}
    finally:
        api.delete(f"/sessions/{alone['id']}")

        def gone() -> bool:
            return (
                not labelled_objects(ALONE_SLUG)
                and api.get(f"/sessions/{alone['id']}").status_code == 404
            )

        wait_for(f"session {ALONE_SLUG} objects to be deleted", gone, DISCARD_TIMEOUT)


def pods_of(slug: str) -> dict[str, str]:
    """Pod name -> pod uid of a session, read with kubectl."""
    out = kubectl(
        "get",
        "pods",
        "-l",
        f"{SESSION_LABEL}={slug}",
        "-o",
        'jsonpath={range .items[*]}{.metadata.name}{" "}{.metadata.uid}{"\\n"}{end}',
    ).stdout
    return dict(line.split() for line in out.splitlines() if line.strip())


def presets_by_name(api: httpx.Client) -> dict[str, dict[str, Any]]:
    resp = api.get("/presets")
    assert resp.status_code == 200, resp.text
    return {p["name"]: p for p in resp.json()}


def default_preset(api: httpx.Client) -> dict[str, Any]:
    found = [p for p in api.get("/presets").json() if p["is_default"]]
    assert len(found) == 1, found
    return found[0]


def test_default_preset_listed(api: httpx.Client) -> None:
    listed = presets_by_name(api)
    assert "Default" in listed, listed
    default = listed["Default"]
    assert default["is_default"] is True, default
    assert default["spec"]["replicas"] == 1, default
    assert default_preset(api)["id"] == default["id"]


def test_session_scheduled_from_preset_keeps_provenance(
    api: httpx.Client, session: dict[str, Any], preset: dict[str, Any]
) -> None:
    view = api.get(f"/sessions/{session['id']}").json()
    assert view["slug"] == SLUG
    assert view["preset_id"] == preset["id"] and view["preset_name"] == PRESET_NAME, view
    assert view["spec"]["replicas"] == 2 and view["spec"]["peers"] is True, view["spec"]
    assert view["spec"]["env"] == FAKE_TURN_ENV, view["spec"]


def test_preset_edit_and_delete_leave_session_unchanged(
    api: httpx.Client,
    session: dict[str, Any],
    preset: dict[str, Any],
    round_robin: dict[str, Any],
) -> None:
    cid = session["id"]
    before = api.get(f"/sessions/{cid}").json()
    pods_before = pods_of(SLUG)
    assert len(pods_before) == 2, pods_before
    copy_id: str | None = None

    def assert_unchanged() -> None:
        # several reconcile polls (ORCH_RECONCILE_POLL_SECONDS=3) pass first
        time.sleep(10)
        view = api.get(f"/sessions/{cid}").json()
        assert view["spec"] == before["spec"], view["spec"]
        assert view["generation"] == before["generation"], view
        assert view["observed_generation"] == before["observed_generation"], view
        assert pods_of(SLUG) == pods_before

    try:
        edited = api.put(
            f"/presets/{preset['id']}", json={"name": PRESET_NAME, "spec": {"replicas": 1}}
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["spec"]["replicas"] == 1, edited.text
        copied = api.post(f"/presets/{preset['id']}/copy")
        assert copied.status_code == 201, copied.text
        copy = copied.json()
        copy_id = copy["id"]
        assert copy["id"] != preset["id"] and copy["name"] != PRESET_NAME, copy
        assert copy["is_default"] is False and copy["spec"]["replicas"] == 1, copy
        assert_unchanged()
        assert api.get(f"/sessions/{cid}").json()["preset_id"] == preset["id"]

        deleted = api.delete(f"/presets/{preset['id']}")
        assert deleted.status_code == 204, deleted.text
        assert api.get(f"/presets/{preset['id']}").status_code == 404
        assert_unchanged()
        after = api.get(f"/sessions/{cid}").json()
        assert after["preset_id"] is None, after
        assert after["preset_name"] == PRESET_NAME, after
    finally:
        if copy_id is not None:
            gone = api.delete(f"/presets/{copy_id}")
            assert gone.status_code in (204, 404), gone.text
    assert copy_id not in {p["id"] for p in api.get("/presets").json()}


def test_schedule_default_preset_generated_slug(api: httpx.Client) -> None:
    default = default_preset(api)
    resp = api.post("/sessions", json={"preset_id": default["id"]})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    cid = created["id"]
    try:
        assert GENERATED_SLUG.fullmatch(created["slug"]), created["slug"]
        assert created["spec"]["replicas"] == 1, created["spec"]
        assert created["preset_id"] == default["id"], created
        assert created["preset_name"] == "Default", created
    finally:  # the session costs a reconcile for nothing: discard it at once
        api.delete(f"/sessions/{cid}")
    wait_for(
        f"session {created['slug']} row to be gone",
        lambda: api.get(f"/sessions/{cid}").status_code == 404,
        DISCARD_TIMEOUT,
    )


def test_patch_session_retired(api: httpx.Client, session: dict[str, Any]) -> None:
    resp = api.patch(f"/sessions/{session['id']}", json={"spec": {"replicas": 3}})
    assert resp.status_code == 405, resp.text


def test_delete_default_preset_refused(api: httpx.Client) -> None:
    default = default_preset(api)
    resp = api.delete(f"/presets/{default['id']}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "preset_default", resp.text
    assert api.get(f"/presets/{default['id']}").status_code == 200


# ------------------------------------------------------------------ scaling (D1-D4)

HASH_ANNOTATION = "agents.szymonrichert.pl/config-hash"
SCALE_SLUG_PREFIX = "e2e-scale"


@contextlib.contextmanager
def scale_session(api: httpx.Client, suffix: str, spec: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Create a session of its own through POST /sessions; always delete it and wait for it."""
    slug = f"{SCALE_SLUG_PREFIX}-{suffix}"
    resp = api.post("/sessions", json={"slug": slug, "spec": {**spec, "env": FAKE_TURN_ENV}})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    cid = created["id"]
    try:
        yield created
    finally:
        api.delete(f"/sessions/{cid}")

        def gone() -> bool:
            return not labelled_objects(slug) and api.get(f"/sessions/{cid}").status_code == 404

        wait_for(f"session {slug} objects to be deleted", gone, DISCARD_TIMEOUT)


def pod_identity(slug: str) -> dict[str, tuple[str, str | None]]:
    """Pod name -> (uid, config-hash annotation) of a session, read with kubectl."""
    items = kube_json("get", "pods", "-l", f"{SESSION_LABEL}={slug}")["items"]
    return {
        p["metadata"]["name"]: (
            p["metadata"]["uid"],
            (p["metadata"].get("annotations") or {}).get(HASH_ANNOTATION),
        )
        for p in items
    }


def live_agents(api: httpx.Client, cid: str) -> list[dict[str, Any]]:
    return [a for a in agents_of(api, cid) if not a["gone"]]


def agent_by_pod(api: httpx.Client, cid: str, pod: str) -> dict[str, Any]:
    return next(a for a in agents_of(api, cid) if a["pod_name"] == pod)


def agent_me(pod: str) -> dict[str, Any]:
    status, me = agent_call(pod, "GET", "/agent/v1/me")
    assert status == 200, me
    return me


def wait_members(pod: str, expected: set[str]) -> None:
    def probe() -> bool:
        return {m["pod_name"] for m in agent_me(pod)["members"]} == expected

    wait_for(f"{pod} sees members {sorted(expected)}", probe, 120)


def wait_pod_gone(slug: str, pod: str, timeout: float = DISCARD_TIMEOUT) -> None:
    wait_for(f"pod {pod} to be gone", lambda: pod not in pod_identity(slug), timeout)


def scale_to(api: httpx.Client, cid: str, replicas: int) -> httpx.Response:
    return api.post(f"/sessions/{cid}/scale", json={"replicas": replicas})


def error_code(resp: httpx.Response) -> str:
    return resp.json()["error"]["code"]


def test_scale_up_and_down_keeps_surviving_pods(api: httpx.Client, baseline: dict) -> None:
    with scale_session(api, "updown", {"replicas": 1, "max_replicas": 3}) as created:
        cid, slug = created["id"], created["slug"]
        pods = [f"agent-{slug}-{i}" for i in range(3)]
        wait_sessions(api, cid, 1)
        before = pod_identity(slug)
        assert set(before) == {pods[0]}, before
        assert before[pods[0]][1], before
        leader = wait_for("a leader", lambda: leader_view(api, cid)[0], LEADER_TIMEOUT)
        assert leader["pod_name"] == pods[0], leader

        resp = scale_to(api, cid, 3)
        assert resp.status_code == 202, resp.text
        assert resp.json()["replicas"] == 3 and resp.json()["previous_replicas"] == 1, resp.text
        wait_sessions(api, cid, 3)
        grown = pod_identity(slug)
        assert set(grown) == set(pods), grown
        assert grown[pods[0]] == before[pods[0]], (before, grown)  # same uid, same config hash
        wait_members(pods[0], set(pods))
        assert agent_me(pods[0])["leader"] == pods[0]  # a joining agent never leads
        joined_leader, _ = leader_view(api, cid)
        assert joined_leader is not None and joined_leader["id"] == leader["id"], joined_leader

        resp = scale_to(api, cid, 2)
        assert resp.status_code == 202, resp.text
        wait_pod_gone(slug, pods[2])
        shrunk = pod_identity(slug)
        assert set(shrunk) == set(pods[:2]), shrunk
        assert shrunk[pods[0]] == before[pods[0]] and shrunk[pods[1]] == grown[pods[1]], shrunk
        wait_for("agent-2 marked gone", lambda: agent_by_pod(api, cid, pods[2])["gone"], 120)
        wait_members(pods[0], set(pods[:2]))
        final_leader, _ = leader_view(api, cid)
        assert final_leader is not None and final_leader["id"] == leader["id"], final_leader


def test_scale_refusals(api: httpx.Client, baseline: dict) -> None:
    with scale_session(api, "refuse", {"replicas": 3, "max_replicas": 3}) as created:
        cid = created["id"]
        wait_sessions(api, cid, 3)
        for bad in (0, 4):
            resp = scale_to(api, cid, bad)
            assert resp.status_code == 422, (bad, resp.text)
            assert error_code(resp) == "out_of_range", resp.text

        wait_for("the first election", lambda: leader_view(api, cid)[1] is not None, LEADER_TIMEOUT)
        current, epoch = leader_view(api, cid)
        assert current is not None and epoch is not None
        # The highest ordinal is the pod a 3->2 scale removes; it must be the leader.
        top = max(live_agents(api, cid), key=lambda a: int(a["pod_name"].rsplit("-", 1)[1]))
        if top["id"] != current["id"]:
            resp = api.post(f"/sessions/{cid}/leader", json={"agent_id": top["id"]})
            assert resp.status_code == 202, resp.text
            wait_leader(api, cid, epoch + 1, top["id"])
        resp = scale_to(api, cid, 2)
        assert resp.status_code == 409, resp.text
        assert error_code(resp) == "leader_would_leave", resp.text
        assert len(live_agents(api, cid)) == 3

    with scale_session(api, "static", {"replicas": 2, "max_replicas": 2, "static": True}) as s:
        resp = scale_to(api, s["id"], 1)
        assert resp.status_code == 409, resp.text
        assert error_code(resp) == "static_session", resp.text
        view = api.get(f"/sessions/{s['id']}").json()
        assert view["static"] is True and view["spec"]["replicas"] == 2, view


def test_agent_scale_leader_only(api: httpx.Client, session: dict[str, Any]) -> None:
    cid = session["id"]
    leader, _ = leader_view(api, cid)
    assert leader is not None
    follower = next(a for a in live_agents(api, cid) if a["id"] != leader["id"])
    replicas = api.get(f"/sessions/{cid}").json()["spec"]["replicas"]
    body = {"replicas": replicas}

    status, out = agent_call(leader["pod_name"], "POST", "/agent/v1/scale", body)
    assert status == 202, out
    assert out["replicas"] == replicas, out

    status, out = agent_call(follower["pod_name"], "POST", "/agent/v1/scale", body)
    assert status == 403, out
    assert out["error"]["code"] == "not_leader", out


def test_static_single_pod_is_alone(api: httpx.Client, baseline: dict) -> None:
    with scale_session(api, "single", {"replicas": 1, "max_replicas": 1, "static": True}) as c:
        assert c["hive"] is False and c["static"] is True and c["leader_agent_id"] is None, c
        name = f"agent-{c['slug']}-config"

        def rendered() -> dict[str, Any] | None:
            proc = kubectl("get", "configmap", name, "-o", "json", check=False)
            return json.loads(proc.stdout) if proc.returncode == 0 else None

        configmap = wait_for("the single static pod's ConfigMap", rendered, 120)
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        assert config["notes"] == {"enabled": False}, config
        assert not (config.get("peers") or {}).get("enabled"), config
        plugins = on_start_any_mcp(configmap).get("plugins", [])
        for plugin in plugins:
            if plugin.get("name") == "orchestrator":
                allow = set(plugin.get("allow", []))
                assert not allow & {"scale", "me", "leadership_handover", "lock_claim"}, plugin
        assert "orchestrator" not in {p.get("name") for p in plugins}, plugins


def test_scale_down_waits_for_busy_agent(api: httpx.Client, baseline: dict) -> None:
    """The fake claude's "slow" turn lasts 4 s, so the window is short: the pod is checked
    right after the scale is posted, while the turn is still running."""
    with scale_session(api, "drain", {"replicas": 2, "max_replicas": 2}) as created:
        cid, slug = created["id"], created["slug"]
        top_pod = f"agent-{slug}-1"
        wait_sessions(api, cid, 2)
        wait_leader(api, cid, 1)
        top = agent_by_pod(api, cid, top_pod)
        uid = pod_identity(slug)[top_pod][0]

        body = {"text": SLOW_PROMPT.format(n=1), "target_agent_id": top["id"]}
        resp = api.post(f"/sessions/{cid}/messages", json=body)
        assert resp.status_code == 202, resp.text
        mid = resp.json()["message_id"]

        def busy() -> bool:
            sent = message(api, mid)["status"] == "sent"
            return sent and agent_by_pod(api, cid, top_pod)["state"] == "busy"

        wait_for("the top agent to be busy", busy, TURN_TIMEOUT, interval=0.2)
        resp = scale_to(api, cid, 1)
        assert resp.status_code == 202, resp.text

        def done() -> bool:
            turn = message(api, mid)["turn"]
            return bool(turn and turn["completed_at"])

        # sample the pod for the whole turn: it must stay until the turn is sent
        samples = 0
        deadline = time.monotonic() + TURN_TIMEOUT
        while not done():
            assert pod_identity(slug).get(top_pod, (None,))[0] == uid, "pod left mid-turn"
            samples += 1
            assert time.monotonic() < deadline, "the turn did not complete"
            time.sleep(0.5)
        assert samples >= 1
        wait_pod_gone(slug, top_pod)
        wait_for("the top agent marked gone", lambda: agent_by_pod(api, cid, top_pod)["gone"], 120)


def test_discard_removes_objects_and_history(
    api: httpx.Client, discarded: dict[str, Any], round_robin: dict[str, Any]
) -> None:
    cid = discarded["session"]["id"]
    assert discarded["elapsed"] <= DISCARD_TIMEOUT
    assert labelled_objects(SLUG) == []
    for kind in ("statefulset", "service", "configmap", "persistentvolumeclaim"):
        names = kubectl("get", kind, "-o", "name").stdout
        assert f"agent-{SLUG}" not in names, (kind, names)
    # the Secret is checked by name: it is deleted by its deterministic name
    assert kubectl("get", "secret", f"agent-{SLUG}-ccw", check=False).returncode != 0
    session_resp = api.get(f"/sessions/{cid}")
    assert session_resp.status_code == 404, session_resp.text
    assert session_resp.json()["error"]["code"] == "not_found"
    events_resp = api.get(f"/sessions/{cid}/events")
    assert events_resp.status_code == 404, events_resp.text
    for m in round_robin["messages"]:
        assert api.get(f"/messages/{m['id']}").status_code == 404, m
    assert all(c["id"] != cid for c in api.get("/sessions").json())


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


# ------------------------------------------------------------------ init scripts

INIT_TIMEOUT = 300
RECONCILE_INTERVALS = 10  # seconds: several reconcile polls (ORCH_RECONCILE_POLL_SECONDS=3)
WORKDIR = "/workdir/workdir"


@contextlib.contextmanager
def init_preset(api: httpx.Client, scripts: list[str], replicas: int) -> Iterator[dict[str, Any]]:
    resp = api.post(
        "/presets",
        json={
            "name": f"e2e-init-{uuid.uuid4().hex[:6]}",
            "spec": {"replicas": replicas, "env": FAKE_TURN_ENV, "init_scripts": scripts},
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    try:
        yield created
    finally:
        api.delete(f"/presets/{created['id']}")


@contextlib.contextmanager
def init_session(api: httpx.Client, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    resp = api.post("/sessions", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    cid = created["id"]
    try:
        yield created
    finally:
        api.delete(f"/sessions/{cid}")
        wait_for(
            f"session {created['slug']} row to be gone",
            lambda: api.get(f"/sessions/{cid}").status_code == 404,
            DISCARD_TIMEOUT,
        )


def wait_init_state(api: httpx.Client, cid: str, states: tuple[str, ...]) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        view = api.get(f"/sessions/{cid}").json()
        if view["status"].get("error"):
            raise AssertionError(f"session reconcile error: {view['status']}")
        init = view["status"].get("init")
        return view if init and init.get("state") in states else None

    return wait_for(f"status.init.state in {states}", probe, INIT_TIMEOUT)


def wait_sessions(api: httpx.Client, cid: str, count: int) -> list[dict[str, Any]]:
    def probe() -> list[dict[str, Any]] | None:
        agents = [a for a in agents_of(api, cid) if not a["gone"]]
        ready = [a for a in agents if a["ready"] and a["free_since"]]
        return ready if len(ready) == count else None

    return wait_for(f"{count} agents with sessions", probe, READY_TIMEOUT)


def init_jobs(slug: str) -> list[str]:
    out = kubectl("get", "jobs", "-o", "name", check=False).stdout
    return [ln for ln in out.splitlines() if ln.endswith(f"agent-{slug}-init")]


def test_init_scripts_run_once(api: httpx.Client, baseline: dict) -> None:
    with (
        init_preset(api, ["echo e2e-init > e2e-init.txt"], replicas=2) as preset,
        init_session(api, {"preset_id": preset["id"]}) as created,
    ):
        cid, slug = created["id"], created["slug"]
        view = wait_init_state(api, cid, ("succeeded", "failed"))
        assert view["status"]["init"]["state"] == "succeeded", view["status"]
        agents = wait_sessions(api, cid, 2)
        for agent in agents:
            pod = pod_name_of(agent)
            assert pod_exec(pod, "cat", f"{WORKDIR}/e2e-init.txt").strip() == "e2e-init"
            pod_exec(pod, "test", "-s", f"{WORKDIR}/.agent-init/init.sh")
            pod_exec(pod, "test", "-e", f"{WORKDIR}/.agent-init/init.log")
        # run once: the Job is gone after the outcome and the run is never started again
        started = view["status"]["init"]["started_at"]
        time.sleep(2 * RECONCILE_INTERVALS)
        later = api.get(f"/sessions/{cid}").json()
        assert later["status"]["init"]["started_at"] == started, later["status"]
        assert later["status"]["init"]["state"] == "succeeded", later["status"]
        assert init_jobs(slug) == []


def test_init_scripts_edit_writes_session_and_preset_without_roll(
    api: httpx.Client, baseline: dict
) -> None:
    with (
        init_preset(api, ["echo first > first.txt"], replicas=1) as preset,
        init_session(api, {"preset_id": preset["id"]}) as created,
    ):
        cid, slug = created["id"], created["slug"]
        view = wait_init_state(api, cid, ("succeeded",))
        wait_sessions(api, cid, 1)
        started = view["status"]["init"]["started_at"]
        pods_before = pods_of(slug)
        assert len(pods_before) == 1, pods_before
        pod = f"agent-{slug}-0"

        def assert_no_roll() -> None:
            time.sleep(2 * RECONCILE_INTERVALS)
            now = api.get(f"/sessions/{cid}").json()
            assert now["status"]["init"]["started_at"] == started, now["status"]
            assert pods_of(slug) == pods_before
            assert init_jobs(slug) == []

        # public route
        public = ["echo second > second.txt"]
        put = api.put(f"/sessions/{cid}/init-scripts", json={"scripts": public})
        assert put.status_code == 200, put.text
        got = api.get(f"/sessions/{cid}/init-scripts").json()
        assert got["scripts"] == public and got["edited_since_run"] is True, got
        assert api.get(f"/presets/{preset['id']}").json()["spec"]["init_scripts"] == public
        assert_no_roll()

        # agent route from inside the pod
        agent_scripts = ["echo third > third.txt"]
        status, body = agent_call(pod, "PUT", "/agent/v1/init-scripts", {"scripts": agent_scripts})
        assert status == 200, body
        assert body["scripts"] == agent_scripts, body
        status, body = agent_call(pod, "GET", "/agent/v1/init-scripts")
        assert status == 200 and body["scripts"] == agent_scripts, body
        assert body["edited_since_run"] is True, body
        assert api.get(f"/sessions/{cid}/init-scripts").json()["scripts"] == agent_scripts
        assert api.get(f"/presets/{preset['id']}").json()["spec"]["init_scripts"] == agent_scripts
        assert_no_roll()


def test_init_scripts_failure_starts_sessions_degraded(api: httpx.Client, baseline: dict) -> None:
    spec = {"replicas": 1, "env": FAKE_TURN_ENV, "init_scripts": ["exit 3"]}
    with init_session(
        api, {"spec": spec, "slug": f"e2e-initfail-{uuid.uuid4().hex[:6]}"}
    ) as created:
        cid = created["id"]
        view = wait_init_state(api, cid, ("failed",))
        init = view["status"]["init"]
        assert init["state"] == "failed" and init["exit_code"] == 3, init

        def degraded() -> dict[str, Any] | None:
            now = api.get(f"/sessions/{cid}").json()
            st = now["status"]
            return now if st.get("phase") == "degraded" and "init failed" in st["message"] else None

        wait_for("phase degraded with init failed", degraded, INIT_TIMEOUT)
        assert len(wait_sessions(api, cid, 1)) == 1


def test_session_without_init_scripts_skipped(api: httpx.Client, baseline: dict) -> None:
    # own session: the shared `session` fixture is discarded by the discard test before this runs
    with (
        init_preset(api, [], replicas=1) as preset,
        init_session(api, {"preset_id": preset["id"]}) as created,
    ):
        view = wait_init_state(api, created["id"], ("skipped",))
        assert view["status"]["init"]["state"] == "skipped", view["status"]


# ------------------------------------------------------------------ configuration (A6)

HOOK_FILE = "hook-ran.txt"
HOOK_TIMEOUT = 120
COMPACT_TIMEOUT = 180
LEASE_TIMEOUT = 180  # lease 60 s plus the dispatcher poll and the turn-boundary wait
AUTOCLOSE_TIMEOUT = 240  # 30 s window plus the sweeper interval and the discard


@contextlib.contextmanager
def config_session(
    api: httpx.Client, suffix: str, spec: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """A session of its own through POST /sessions; always deleted and awaited in teardown."""
    slug = f"e2e-cfg-{suffix}"
    resp = api.post("/sessions", json={"slug": slug, "spec": {**spec, "env": FAKE_TURN_ENV}})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    cid = created["id"]
    try:
        yield created
    finally:
        api.delete(f"/sessions/{cid}")
        wait_for(
            f"session {slug} row to be gone",
            lambda: api.get(f"/sessions/{cid}").status_code == 404,
            DISCARD_TIMEOUT,
        )


def ready_agents(api: httpx.Client, cid: str, count: int) -> list[dict[str, Any]]:
    def probe() -> list[dict[str, Any]] | None:
        view = api.get(f"/sessions/{cid}").json()
        if view["status"].get("error"):
            raise AssertionError(f"session reconcile error: {view['status']}")
        agents = [a for a in agents_of(api, cid) if not a["gone"]]
        ready = [a for a in agents if a["ready"] and a["free_since"]]
        return ready if len(ready) == count else None

    return wait_for(f"{count} ready agents", probe, READY_TIMEOUT)


def test_wrapper_hook_and_idle_compact(api: httpx.Client) -> None:
    spec = {
        "replicas": 1,
        "wrapper": {
            "hooks": {
                "onTurnComplete": [
                    {"type": "command", "run": f'echo "$POD_NAME" > {HOOK_FILE}'},
                ]
            },
            "auto_compact": {"idle_seconds": 5},
        },
    }
    with config_session(api, "hook", spec) as created:
        cid = created["id"]
        agent = ready_agents(api, cid, 1)[0]
        resp = api.post(f"/sessions/{cid}/messages", json={"text": "say hello"})
        assert resp.status_code == 202, resp.text
        mid = resp.json()["message_id"]

        def turn_done() -> bool:
            view = message(api, mid)
            return bool(view["status"] == "sent" and view["turn"] and view["turn"]["completed_at"])

        wait_for(f"message {mid} turn to complete", turn_done, TURN_TIMEOUT, interval=1)

        def hook_output() -> str | None:
            r = api.get(f"/sessions/{cid}/files/content", params={"path": HOOK_FILE})
            return r.json()["content"] if r.status_code == 200 else None

        content = wait_for(
            f"{HOOK_FILE} written by the onTurnComplete hook", hook_output, HOOK_TIMEOUT
        )
        assert content.strip() == agent["pod_name"], (content, agent["pod_name"])

        def compacted() -> list[dict[str, Any]] | None:
            r = api.get(f"/sessions/{cid}/events", params={"types": "compacted"})
            r.raise_for_status()
            return r.json()["events"] or None

        events = wait_for("a compacted event after 5 s idle", compacted, COMPACT_TIMEOUT)
        assert all(e["type"] == "compacted" for e in events), events


def test_leader_lease_hands_over(api: httpx.Client) -> None:
    spec = {"replicas": 2, "leader_lease_seconds": 60}
    with config_session(api, "lease", spec) as created:
        cid = created["id"]
        ready_agents(api, cid, 2)

        def first_leader() -> dict[str, Any] | None:
            view = api.get(f"/sessions/{cid}").json()
            return view if view["leader_agent_id"] and view["leader_epoch"] is not None else None

        first = wait_for("a first leader", first_leader, LEADER_TIMEOUT)

        def handed_over() -> dict[str, Any] | None:
            view = api.get(f"/sessions/{cid}").json()
            ok = (
                view["leader_agent_id"] not in (None, first["leader_agent_id"])
                and view["leader_epoch"] > first["leader_epoch"]
                and view["leader_reason"] == "lease_expired"
            )
            return view if ok else None

        wait_for(
            f"leader to change from {first['leader_agent_id']} (epoch {first['leader_epoch']}) "
            "with reason lease_expired",
            handed_over,
            LEASE_TIMEOUT,
        )


def test_autoclose_discards_session(api: httpx.Client) -> None:
    from datetime import datetime, timedelta

    with config_session(api, "autoclose", {"replicas": 1, "autoclose_after_seconds": 30}) as c:
        cid = c["id"]
        view = api.get(f"/sessions/{cid}").json()
        created_at = datetime.fromisoformat(view["created_at"])
        assert datetime.fromisoformat(view["closes_at"]) == created_at + timedelta(seconds=30), view

        def closing() -> bool:
            r = api.get(f"/sessions/{cid}")
            return r.status_code == 404 or r.json()["desired"] == "deleting"

        wait_for("the autoclose sweeper to mark the session deleting", closing, AUTOCLOSE_TIMEOUT)

        def gone() -> bool:
            return api.get(f"/sessions/{cid}").status_code == 404

        wait_for("the closed session to answer 404", gone, DISCARD_TIMEOUT)
