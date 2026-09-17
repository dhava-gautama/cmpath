"""Small deterministic adapter used by the offline conformance suite.

It intentionally has no network or subprocess dependency.  The suite therefore
checks the adapter contract on every platform, while the production native
adapter can be exercised separately by supplying an adapter factory.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable

import sys

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cmpath import TaskMemory  # noqa: E402


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


def _wire(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_wire(value).encode("utf-8")).hexdigest()


class AdapterError(RuntimeError):
    """Stable, machine-readable rejection from an adapter."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class LocalAdapter:
    """Reference contract implementation backed by temporary/in-memory CMP.

    This is a test double, not a replacement for ``NativeHarness``.  It keeps
    the journal in ordinary Python objects so the conformance suite remains
    runnable where a native executable is unavailable (notably Windows).
    """

    def __init__(self):
        self.memory = TaskMemory(":memory:")
        self.turns: dict[str, dict[str, Any]] = {}
        self.calls: dict[tuple[str, str], dict[str, Any]] = {}
        self.tools: dict[tuple[str, str], dict[str, Any]] = {}
        self.leases: dict[str, dict[str, Any]] = {}
        self._epoch: dict[int, int] = {}
        self._lease_nonce = 0

    def close(self):
        self.memory.close()

    def _touch(self, task_id: int):
        self._epoch[task_id] = self._epoch.get(task_id, 0) + 1

    @staticmethod
    def _task_record(task) -> dict[str, Any]:
        return {"id": task.id, "title": task.title, "status": task.status,
                "parents": list(task.parents), "aliases": list(task.aliases),
                "snapshot": task.snapshot, "revision": task.revision}

    def create_task(self, title: str, *, snapshot: dict[str, Any] | None = None):
        task = self.memory.create_task(title, snapshot=snapshot)
        self._epoch[task.id] = 0
        return self._task_record(task)

    def append_evidence(self, task_id: int, role: str, content: str,
                        *, source: dict[str, Any] | None = None):
        evidence = self.memory.append(task_id, role, content, source=source)
        self._touch(task_id)
        return {"id": evidence.id, **evidence.as_record()}

    def _package(self, task_id: int, query: str, *, system: str = "",
                 budget: int = 2000, reserve: int = 0, scope: str = "lineage",
                 retrieval_limit: int = 24, recent: int = 4,
                 consistency: str = "snapshot") -> dict[str, Any]:
        package = self.memory.context(task_id, query, budget=budget, reserve=reserve,
                                      system=system, scope=scope,
                                      retrieval_limit=retrieval_limit, recent=recent)
        task = self.memory.task(task_id)
        return {"task_id": task_id, "messages": package.as_messages(),
                "messages_json": package.messages_json,
                "snapshot": task.snapshot, "citations": list(package.citations),
                "used_units": package.used_units,
                "input_allowance": package.input_allowance,
                "counting": package.counting,
                "omitted_candidates": package.omitted_candidates,
                "consistency": consistency}

    def _public(self, turn: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(turn)

    def begin(self, request_id: str, task_id: int, query: str, *, system: str = "",
              model_key: str = "", budget: int = 2000, reserve: int = 0,
              scope: str = "lineage", retrieval_limit: int = 24, recent: int = 4,
              consistency: str = "snapshot", **extra):
        if not isinstance(request_id, str) or not request_id.strip():
            raise AdapterError("invalid", "request ID is required")
        if consistency not in ("snapshot", "scope"):
            raise AdapterError("invalid", "unsupported consistency mode")
        identity = {"task_id": task_id, "query": query, "system": system,
                    "model_key": model_key, "budget": budget, "reserve": reserve,
                    "scope": scope, "retrieval_limit": retrieval_limit,
                    "recent": recent, "consistency": consistency, **extra}
        fingerprint = _hash(identity)
        old = self.turns.get(request_id)
        if old is not None:
            if old["fingerprint"] != fingerprint:
                raise AdapterError("conflict", "request ID was used with different input")
            old = self._public(old)
            old["replayed"] = True
            old["created"] = False
            return old
        if any(t["task_id"] == task_id and t["status"] == "pending"
               for t in self.turns.values()):
            raise AdapterError("busy", "task already has an unfinished turn")
        try:
            package = self._package(task_id, query, system=system, budget=budget,
                                    reserve=reserve, scope=scope,
                                    retrieval_limit=retrieval_limit, recent=recent,
                                    consistency=consistency)
            task = self.memory.task(task_id)
            # Match the native boundary: retain the user input in source memory
            # after the initial context is packed.
            self.append_evidence(task_id, "user", query,
                                 source={"request_id": request_id})
        except (KeyError, ValueError) as exc:
            raise AdapterError("invalid", str(exc)) from exc
        turn = {"request_id": request_id, "task_id": task_id, "status": "pending",
                "generation": 1, "task_revision": task.revision,
                "scope_epoch": self._epoch.get(task_id, 0), "package": package,
                "reply": None, "fingerprint": fingerprint, "created": True,
                "replayed": False}
        self.turns[request_id] = turn
        return self._public(turn)

    def inspect(self, request_id: str):
        if request_id not in self.turns:
            raise AdapterError("not_found", "turn does not exist")
        return self._public(self.turns[request_id])

    def _pending(self, request_id: str, generation: int):
        turn = self.turns.get(request_id)
        if turn is None:
            raise AdapterError("not_found", "turn does not exist")
        if turn["status"] != "pending":
            raise AdapterError("state", "turn is not pending")
        if turn["generation"] != generation:
            raise AdapterError("fenced", "worker generation is stale")
        return turn

    def recover(self, request_id: str, expected_generation: int):
        turn = self._pending(request_id, expected_generation)
        if self.memory.task(turn["task_id"]).revision != turn["task_revision"]:
            raise AdapterError("conflict", "task snapshot changed; prepare a new turn")
        turn["generation"] += 1
        return self._public(turn)

    def checkpoint_model_request(self, request_id: str, generation: int,
                                 call_id: str, payload: Any, units: int) -> bytes:
        turn = self._pending(request_id, generation)
        if not isinstance(call_id, str) or not call_id.strip():
            raise AdapterError("invalid", "model call ID is required")
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise AdapterError("invalid", "model units must be nonnegative")
        raw = _wire(payload)
        if units > turn["package"]["input_allowance"]:
            raise AdapterError("budget", "complete model payload exceeds allowance")
        key = (request_id, call_id)
        old = self.calls.get(key)
        record = {"request_id": request_id, "call_id": call_id,
                  "payload_json": raw, "units": units, "response_json": None}
        if old is not None and (old["payload_json"], old["units"]) != (raw, units):
            raise AdapterError("conflict", "model call ID has different payload or count")
        self.calls.setdefault(key, record)
        return raw.encode("utf-8")

    def record_model_response(self, request_id: str, generation: int,
                              call_id: str, response: Any):
        self._pending(request_id, generation)
        key = (request_id, call_id)
        if key not in self.calls:
            raise AdapterError("not_found", "model request is not checkpointed")
        if isinstance(response, str):
            try:
                value = json.loads(response, parse_constant=_reject_constant)
            except (TypeError, ValueError) as exc:
                raise AdapterError("invalid", "invalid provider response JSON") from exc
            raw = response
        else:
            value = response
            try:
                raw = _wire(value)
            except (TypeError, ValueError) as exc:
                raise AdapterError("invalid", "invalid provider response JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError("invalid", "provider response must be a JSON object")
        old = self.calls[key]["response_json"]
        if old is not None and old != raw:
            raise AdapterError("conflict", "model response was already recorded differently")
        self.calls[key]["response_json"] = raw
        return {"recorded": True}

    def model_calls(self, request_id: str):
        return [deepcopy(v) for (rid, _), v in self.calls.items() if rid == request_id]

    def start_tool(self, request_id: str, generation: int, call_id: str,
                   name: str, arguments: Any):
        self._pending(request_id, generation)
        if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
            raise AdapterError("invalid", "tool call ID and name are required")
        try:
            raw = _wire(arguments)
        except (TypeError, ValueError) as exc:
            raise AdapterError("invalid", "tool arguments are not JSON") from exc
        fp = _hash([name, arguments])
        key = (request_id, call_id)
        old = self.tools.get(key)
        if old is not None:
            if old["fingerprint"] != fp:
                raise AdapterError("conflict", "tool call ID has different input")
            replay = deepcopy(old)
            replay["created"] = False
            return replay
        row = {"request_id": request_id, "call_id": call_id, "name": name,
               "arguments": deepcopy(arguments), "arguments_json": raw,
               "fingerprint": fp, "status": "started", "result": None,
               "created": True}
        self.tools[key] = row
        return deepcopy(row)

    def action_eligible(self, request_id: str, generation: int, call_id: str,
                        ttl_ms: int = 5000):
        turn = self._pending(request_id, generation)
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or not 1 <= ttl_ms <= 600000:
            raise AdapterError("invalid", "lease TTL must be between 1 and 600000 milliseconds")
        tool = self.tools.get((request_id, call_id))
        if tool is None:
            raise AdapterError("not_found", "tool call does not exist")
        if tool["status"] != "started":
            raise AdapterError("state", "only a started tool can receive a lease")
        if self._epoch.get(turn["task_id"], 0) != turn["scope_epoch"]:
            raise AdapterError("stale_context", "captured scope changed")
        self._lease_nonce += 1
        expires = int(time.time() * 1000) + ttl_ms
        token = _hash([request_id, generation, call_id, tool["fingerprint"], expires, self._lease_nonce])
        self.leases[token] = {"request_id": request_id, "generation": generation,
                              "call_id": call_id, "fingerprint": tool["fingerprint"],
                              "expires_at": expires}
        return {"token": token, "request_id": request_id, "call_id": call_id,
                "generation": generation, "args_hash": tool["fingerprint"],
                "expires_at": expires}

    def finish_tool(self, request_id: str, generation: int, call_id: str,
                    result: Any, lease_token: str = ""):
        turn = self._pending(request_id, generation)
        tool = self.tools.get((request_id, call_id))
        if tool is None:
            raise AdapterError("not_found", "tool call does not exist")
        raw = _wire(result)
        if tool["status"] == "completed":
            if tool["result_json"] != raw:
                raise AdapterError("conflict", "tool result was already recorded differently")
            return deepcopy(tool)
        if lease_token:
            lease = self.leases.get(lease_token)
            if lease is None:
                raise AdapterError("stale_context", "action eligibility lease is missing")
            if lease["generation"] != generation or lease["call_id"] != call_id:
                raise AdapterError("fenced", "action eligibility lease belongs to another worker")
            if lease["expires_at"] <= int(time.time() * 1000):
                raise AdapterError("stale_context", "action eligibility lease has expired")
            if lease["fingerprint"] != tool["fingerprint"]:
                raise AdapterError("conflict", "action eligibility lease arguments do not match intent")
        evidence = self.append_evidence(turn["task_id"], "tool", raw,
                                        source={"request_id": request_id, "call_id": call_id,
                                                "tool": tool["name"]})
        tool.update({"status": "completed", "result": deepcopy(result),
                     "result_json": raw, "evidence_id": evidence["id"], "created": False})
        # The tool result is now part of the captured scope for the same turn.
        turn["scope_epoch"] = self._epoch.get(turn["task_id"], 0)
        return deepcopy(tool)

    def commit(self, request_id: str, generation: int, reply: dict[str, Any]):
        turn = self._pending(request_id, generation)
        if not isinstance(reply, dict) or not isinstance(reply.get("text"), str) or not reply["text"].strip():
            raise AdapterError("invalid", "final reply must be nonempty text")
        task = self.memory.task(turn["task_id"])
        if task.revision != turn["task_revision"]:
            raise AdapterError("conflict", "task snapshot changed during execution")
        for tool in self.tools.values():
            if tool["request_id"] == request_id and tool["status"] == "started":
                raise AdapterError("indeterminate_tool", "tool intent has no confirmed result")
        snapshot = reply.get("snapshot")
        changed = snapshot is not None and snapshot != turn["package"]["snapshot"]
        provenance = reply.get("provenance") or []
        if turn["package"]["consistency"] == "scope" and changed and not any(
                isinstance(p, dict) and p.get("kind") == "supports" for p in provenance):
            raise AdapterError("provenance_required", "scoped snapshot writes need supports provenance")
        for p in provenance:
            if not isinstance(p, dict) or p.get("kind") not in ("supports", "contradicts"):
                raise AdapterError("invalid", "invalid provenance reference")
            try:
                evidence = self.memory.message(int(p["evidence_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise AdapterError("stale_context", "provenance evidence is unavailable") from exc
            if evidence.task_id != turn["task_id"]:
                raise AdapterError("stale_context", "provenance evidence is outside the task")
        self.append_evidence(turn["task_id"], "assistant", reply["text"],
                             source={"request_id": request_id})
        if snapshot is not None:
            if not isinstance(snapshot, dict):
                raise AdapterError("invalid", "snapshot must be an object")
            try:
                self.memory.set_snapshot(turn["task_id"], snapshot,
                                         expected_revision=task.revision)
            except (KeyError, ValueError) as exc:
                raise AdapterError("invalid", str(exc)) from exc
        turn.update({"status": "committed", "reply": deepcopy(reply), "created": False})
        return self._public(turn)

    def abort(self, request_id: str, generation: int):
        turn = self._pending(request_id, generation)
        turn["status"] = "aborted"
        return {"aborted": True}

    def tool(self, request_id: str, generation: int, call_id: str, name: str,
             arguments: Any, execute: Callable[[], Any]):
        row = self.start_tool(request_id, generation, call_id, name, arguments)
        if row["status"] == "completed":
            return deepcopy(row["result"])
        if not row.get("created"):
            raise AdapterError("indeterminate_tool", "reconcile the started tool before retrying")
        lease = self.action_eligible(request_id, generation, call_id)
        return self.finish_tool(request_id, generation, call_id, execute(), lease["token"])["result"]

    def run(self, request_id: str, task_id: int, query: str,
            callback: Callable[[dict[str, Any]], Any], **options):
        if not callable(callback):
            raise TypeError("callback must be callable")
        turn = self.begin(request_id, task_id, query, **options)
        if turn["status"] == "committed":
            return deepcopy(turn["reply"])
        if not turn.get("created"):
            raise AdapterError("in_progress", "inspect and explicitly recover pending work")
        reply = callback(turn)
        if isinstance(reply, str):
            reply = {"text": reply}
        return self.commit(request_id, turn["generation"], reply)["reply"]
