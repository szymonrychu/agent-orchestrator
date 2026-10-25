"""Bench runner: drives the arms through the public API only (D18)."""

from __future__ import annotations

import json
import logging
import random
import secrets
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import httpx

from agents_orchestrator.bench.spec import Arm, BenchSpec, Task
from agents_orchestrator.spec import SLUG_MAX, slugify

log = logging.getLogger("agents_orchestrator.bench")

_TERMINAL_MESSAGE = ("failed", "cancelled")
_CHECK_DONE = ("passed", "failed", "cancelled")
_SESSION_STATES = ("idle", "busy")


class BenchError(RuntimeError):
    """The bench could not complete a step (API error, cluster never ready)."""


def arm_slug(arm: str, run: str) -> str:
    """`bench-<arm>-<run>` as a DNS-1123 label of at most SLUG_MAX characters."""
    tail = f"-{run}"
    room = SLUG_MAX - len("bench-") - len(tail)
    return f"bench-{slugify(arm, room) or 'arm'}{tail}"


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Bench:
    def __init__(
        self,
        spec: BenchSpec,
        client: httpx.Client,
        out: TextIO,
        *,
        keep: bool = False,
        run_id: str | None = None,
        poll_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.spec = spec
        self.http = client
        self.out = out
        self.keep = keep
        self.run_id = run_id or secrets.token_hex(3)
        self.poll = poll_seconds
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.records: list[dict[str, Any]] = []
        self._chaos_done = False
        self._arm_started = 0.0
        self._cluster_id = ""

    # ------------------------------------------------------------------ api helpers
    def _call(self, method: str, url: str, **kw: Any) -> Any:
        try:
            resp = self.http.request(method, url, **kw)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BenchError(
                f"{method} {url} -> {exc.response.status_code} {exc.response.text[:200]}"
            ) from None
        except httpx.HTTPError as exc:
            raise BenchError(f"{method} {url} failed: {exc}") from None
        return resp.json() if resp.content else None

    # ------------------------------------------------------------------ run
    def run(self) -> list[dict[str, Any]]:
        created: list[str] = []
        try:
            for arm in self.spec.arms:
                self._run_arm(arm, created)
        finally:
            if not self.keep:
                self._discard(created)
        return self.records

    def _discard(self, cluster_ids: list[str]) -> None:
        for cid in cluster_ids:
            try:
                self._call("DELETE", f"/api/v1/clusters/{cid}")
            except BenchError as exc:
                log.warning("discarding bench cluster %s failed: %s", cid, exc)

    def _run_arm(self, arm: Arm, created: list[str]) -> None:
        body = {
            "slug": arm_slug(arm.name, self.run_id),
            "spec": {
                **self.spec.cluster.model_dump(
                    exclude_none=True, exclude={"max_replicas", "static"}
                ),
                "replicas": arm.replicas,
                "peers": arm.peers,
                **arm.model_dump(include={"max_replicas", "static"}, exclude_none=True),
            },
        }
        cluster = self._call("POST", "/api/v1/clusters", json=body)
        cid = self._cluster_id = cluster["id"]
        created.append(cid)
        self._wait_ready(cid, arm.replicas)
        self._arm_started = time.monotonic()
        self._chaos_done = self.spec.chaos is None
        for task in self.spec.tasks:
            for repeat in range(1, self.spec.repeats + 1):
                self._record(self._run_one(arm, task, repeat, cid))

    def _wait_ready(self, cid: str, replicas: int) -> None:
        deadline = time.monotonic() + self.spec.timeout_seconds
        while True:
            agents = [
                a for a in self._call("GET", f"/api/v1/clusters/{cid}/agents") if not a["gone"]
            ]
            if (
                len(agents) == replicas
                and sum(1 for a in agents if a["ready"]) == replicas
                and all(a["state"] in _SESSION_STATES for a in agents)
            ):
                return
            if time.monotonic() > deadline:
                raise BenchError(f"cluster {cid} not ready with {replicas} sessions in time")
            self.sleep(self.poll)

    def _run_one(self, arm: Arm, task: Task, repeat: int, cid: str) -> dict[str, Any]:
        t0 = time.monotonic()
        accepted = self._call(
            "POST", f"/api/v1/clusters/{cid}/messages", json={"text": task.prompt}
        )
        mid = accepted["message_id"]
        msg = self._await_message(mid)
        local_wall = time.monotonic() - t0
        turn = msg.get("turn") or {}
        queued, done = _ts(msg.get("created_at")), _ts(turn.get("completed_at"))
        wall = (done - queued).total_seconds() if queued and done else local_wall
        status = turn.get("status") or msg["status"]
        completed = status == "completed" and msg["status"] == "sent"
        usage = turn.get("usage") or {}
        verify = self._verify(cid, task.verify)
        return {
            "arm": arm.name,
            "task": task.name,
            "repeat": repeat,
            "cluster_id": cid,
            "message_id": mid,
            "wall_seconds": round(wall, 3),
            "turn_status": "completed" if completed else status,
            "failed_turns": max(msg.get("attempts", 1) - 1, 0) + (0 if completed else 1),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "verify_passed": verify["status"] == "passed",
            "exit_code": verify.get("exit_code"),
            "leader_epoch": self._leader_epoch(cid),
        }

    def _leader_epoch(self, cid: str) -> int | None:
        try:
            return self._call("GET", f"/api/v1/clusters/{cid}").get("leader_epoch")
        except BenchError as exc:
            log.warning("reading leader epoch of %s failed: %s", cid, exc)
            return None

    def _record(self, rec: dict[str, Any]) -> None:
        self.records.append(rec)
        self.out.write(json.dumps(rec) + "\n")
        self.out.flush()

    # ------------------------------------------------------------------ waiting
    @staticmethod
    def _finished(msg: dict[str, Any]) -> bool:
        turn = msg.get("turn") or {}
        return msg["status"] in _TERMINAL_MESSAGE or bool(
            turn.get("status") and turn.get("completed_at")
        )

    def _await_message(self, mid: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.spec.timeout_seconds
        while True:
            self._maybe_chaos()
            msg = self._call("GET", f"/api/v1/messages/{mid}")
            if self._finished(msg):
                return msg
            if time.monotonic() > deadline:
                msg["status"] = "failed"
                msg["turn"] = {**(msg.get("turn") or {}), "status": "timeout"}
                return msg
            try:
                if not self._follow_stream(mid):
                    self.sleep(self.poll)
            except httpx.HTTPError:
                self.sleep(self.poll)  # stream unavailable: fall back to polling

    def _follow_stream(self, mid: int) -> bool:
        """Block until the status stream mentions `mid` or poll_seconds pass.

        Returns False when the stream ended at once without an event (caller polls).
        """
        end = time.monotonic() + self.poll
        timeout = httpx.Timeout(10.0, read=self.poll)
        event = ""
        seen = False
        try:
            with self.http.stream("GET", "/api/v1/stream", timeout=timeout) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    seen = True
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif (
                        line.startswith("data:")
                        and event == "message_status"
                        and self._mentions(line[5:].strip(), mid)
                    ):
                        return True
                    if time.monotonic() > end:
                        return True
        except httpx.ReadTimeout:
            return True
        return seen

    @staticmethod
    def _mentions(data: str, mid: int) -> bool:
        try:
            msgs = json.loads(data).get("messages", [])
        except (ValueError, AttributeError):
            return False
        return any(isinstance(m, dict) and m.get("id") == mid for m in msgs)

    # ------------------------------------------------------------------ chaos and verify
    def _maybe_chaos(self) -> None:
        chaos = self.spec.chaos
        if self._chaos_done or chaos is None:
            return
        if time.monotonic() - self._arm_started < chaos.after_seconds:
            return
        agents = self._call("GET", f"/api/v1/clusters/{self._cluster_id}/agents")
        busy = [a for a in agents if not a["gone"] and a["state"] == "busy"]
        if busy:
            victim = self.rng.choice(busy)
            self._call("POST", f"/api/v1/agents/{victim['id']}/restart")
            self._chaos_done = True
            log.info("chaos: restarted agent %s", victim["id"])

    def _verify(self, cid: str, command: str) -> dict[str, Any]:
        check_id = self._call("POST", f"/api/v1/clusters/{cid}/checks", json={"command": command})[
            "check_id"
        ]
        deadline = time.monotonic() + self.spec.timeout_seconds
        while True:
            check = self._call("GET", f"/api/v1/checks/{check_id}")
            if check["status"] in _CHECK_DONE:
                return check
            if time.monotonic() > deadline:
                return {**check, "status": "failed"}
            self.sleep(self.poll)


def run_bench(
    spec: BenchSpec, api: str, out_path: str | Path, *, keep: bool = False, **kw: Any
) -> list[dict[str, Any]]:
    """Run `spec` against the API at `api`, appending JSONL records to `out_path`."""
    with (
        httpx.Client(base_url=api.rstrip("/"), timeout=30.0) as client,
        open(out_path, "w") as out,
    ):
        return Bench(spec, client, out, keep=keep, **kw).run()
