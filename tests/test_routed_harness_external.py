from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from cmpath import (
    Harness,
    HarnessError,
    MemoryRoute,
    MemoryRouter,
    NativeHarness,
    RoutedHarness,
    TaskMemory,
)
try:
    from _native_support import native_binary, native_binary_status
except ImportError:  # ``python -m unittest tests.test_*`` package mode.
    from tests._native_support import native_binary, native_binary_status


ROOT = Path(__file__).resolve().parents[1]
NATIVE_BINARY = native_binary(ROOT)
NATIVE_BINARY_OK, NATIVE_BINARY_REASON = native_binary_status(NATIVE_BINARY)


class _ExternalBackend:
    """A deterministic backend standing in for a host-owned journal service."""

    def __init__(self):
        self.calls = []
        self.turn = None
        self.tools = {}
        self.closed = False

    def call(self, operation, **arguments):
        self.calls.append((operation, copy.deepcopy(arguments)))
        if operation == "begin":
            if self.turn is None:
                self.turn = {
                    "request_id": arguments["request_id"],
                    "task_id": arguments["task_id"],
                    "status": "pending",
                    "generation": 1,
                    "created": True,
                    "package": {
                        "messages": [
                            {"role": "system", "content": "native"},
                            {"role": "user", "content": arguments["query"]},
                        ],
                        "snapshot": {"phase": "start"},
                    },
                }
                result = copy.deepcopy(self.turn)
                # A later begin with the same logical ID is a pending replay,
                # not permission to invoke a second route/model/tool loop.
                self.turn["created"] = False
                return result
            return copy.deepcopy(self.turn)
        if operation == "model_request":
            return {"recorded": True, "call_id": arguments["call_id"]}
        if operation == "model_response":
            return {"recorded": True, "call_id": arguments["call_id"]}
        if operation == "tool_start":
            call_id = arguments["call_id"]
            existing = self.tools.get(call_id)
            if existing is not None:
                return copy.deepcopy(existing)
            value = {
                "call_id": call_id,
                "name": arguments["name"],
                "arguments": copy.deepcopy(arguments["arguments"]),
                "status": "started",
                "created": True,
            }
            self.tools[call_id] = value
            return copy.deepcopy(value)
        if operation == "action_eligible":
            return {"token": "lease-external", "call_id": arguments["call_id"]}
        if operation == "tool_finish":
            call = self.tools[arguments["call_id"]]
            call.update(status="completed", result=copy.deepcopy(arguments["result"]))
            return copy.deepcopy(call)
        if operation == "commit":
            self.turn.update(
                status="committed",
                reply=copy.deepcopy(arguments["reply"]),
                created=False,
            )
            return copy.deepcopy(self.turn)
        if operation == "abort":
            self.turn.update(status="aborted")
            return {"aborted": True}
        raise AssertionError(f"unexpected backend operation: {operation}")

    def close(self):
        self.closed = True


class _ExternalContext:
    route = "lineage"
    task_id = 11
    citations = ("T11:M4",)
    reason = "external router fixture"

    def as_messages(self):
        return [
            {"role": "system", "content": "quoted memory"},
            {"role": "user", "content": "evidence T11:M4"},
            {"role": "user", "content": "question"},
        ]

    def as_dict(self):
        return {
            "route": self.route,
            "task_id": self.task_id,
            "messages": self.as_messages(),
            "citations": list(self.citations),
        }


class _ExternalRouter:
    def __init__(self):
        self.calls = []

    def retrieve(self, query, task_id=None, requested_route=None, **options):
        self.calls.append((query, task_id, requested_route, copy.deepcopy(options)))
        return _ExternalContext()


class ExternalHostRoutedHarnessTests(unittest.TestCase):
    """Verify a host that owns provider/tool callbacks can use RoutedHarness."""

    def setUp(self):
        self.backend = _ExternalBackend()
        self.router = _ExternalRouter()
        self.facade = RoutedHarness(Harness(self.backend), self.router)
        self.addCleanup(self.facade.close)

    def _options(self):
        return {
            "budget": 1200,
            "reserve": 100,
            "scope": "lineage",
            "retrieval_limit": 6,
            "recent": 2,
        }

    def test_external_host_executes_all_five_boundaries_once(self):
        provider_calls = []
        tool_effects = []
        options = self._options()
        turn = self.facade.prepare_turn(
            "external-1",
            11,
            "question",
            requested_route=MemoryRoute.LINEAGE,
            **options,
        )
        self.assertEqual(turn.route, "lineage")
        self.assertEqual(turn.citations, ("T11:M4",))
        self.assertEqual([name for name, _ in self.backend.calls], ["begin"])

        # before-model journals the complete provider payload but does not
        # invoke the provider.  The external host dispatches only afterwards.
        payload = {
            "model": "external-model",
            "messages": turn.messages,
            "tools": [{"name": "lookup", "parameters": {"type": "object"}}],
        }
        raw = turn.before_model(
            "model-call-1",
            payload,
            counter=lambda serialized: len(serialized.encode("utf-8")),
            counting="utf8-json-bytes",
        )
        self.assertEqual(json.loads(raw), payload)
        self.assertEqual(provider_calls, [])
        provider_calls.append(payload)
        turn.after_model(
            "model-call-1",
            response={"choices": [{"message": {"content": "lookup"}}]},
        )

        # before-tool records intent and obtains a lease, but cannot run an
        # arbitrary callback owned by the external host.
        before = turn.before_tool(
            "tool-call-1", "lookup", {"key": "budget"}, ttl_ms=1000
        )
        self.assertEqual(tool_effects, [])
        self.assertEqual(before["eligibility"]["token"], "lease-external")
        tool_effects.append({"key": "budget", "value": 200})
        turn.after_tool(
            "tool-call-1",
            result=tool_effects[0],
            lease_token=before["eligibility"]["token"],
        )
        committed = turn.commit({"text": "done"})
        self.assertEqual(committed["status"], "committed")
        self.assertEqual([name for name, _ in self.backend.calls], [
            "begin",
            "model_request",
            "model_response",
            "tool_start",
            "action_eligible",
            "tool_finish",
            "commit",
        ])
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(len(tool_effects), 1)

    def test_committed_replay_does_not_route_or_repeat_external_effects(self):
        provider_calls = []
        tool_effects = []
        options = self._options()

        turn = self.facade.prepare_turn(
            "replay-1", 11, "question", requested_route="lineage", **options
        )
        provider_calls.append(turn.before_model("model-call-1", {"model": "external"}))
        turn.after_model("model-call-1", response={"ok": True})
        before = turn.before_tool("tool-call-1", "write", {"id": 7})
        tool_effects.append("executed-once")
        turn.after_tool(
            "tool-call-1",
            result=tool_effects[0],
            lease_token=before["eligibility"]["token"],
        )
        turn.commit({"text": "saved"})

        replay = self.facade.prepare_turn(
            "replay-1", 11, "question", requested_route="lineage", **options
        )
        self.assertEqual(replay.turn["status"], "committed")
        self.assertIsNone(replay.context)
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(tool_effects, ["executed-once"])

    def test_pending_replay_requires_explicit_recovery_before_routing(self):
        options = self._options()
        first = self.facade.prepare_turn(
            "pending-1", 11, "question", requested_route="lineage", **options
        )
        self.assertEqual(first.turn["status"], "pending")
        with self.assertRaises(HarnessError) as caught:
            self.facade.prepare_turn(
                "pending-1", 11, "question", requested_route="lineage", **options
            )
        self.assertEqual(caught.exception.code, "in_progress")
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual([name for name, _ in self.backend.calls], ["begin", "begin"])


@unittest.skipUnless(NATIVE_BINARY_OK, NATIVE_BINARY_REASON)
class NativeExternalHostRoutedHarnessTests(unittest.TestCase):
    """Run the same external-host recipe against the durable native bridge."""

    def test_native_journal_and_router_replay_are_safe_for_external_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "native.db"
            memory = TaskMemory(database)
            task = memory.create_task("Native release")
            source = memory.append(
                task.id,
                "document",
                "Native release budget is 200 USD.",
                source={"path": "release.md"},
            )

            class CountingRouter:
                def __init__(self, wrapped):
                    self.wrapped = wrapped
                    self.calls = []

                def retrieve(self, query, **options):
                    self.calls.append((query, copy.deepcopy(options)))
                    return self.wrapped.retrieve(query, **options)

            router = CountingRouter(MemoryRouter(memory))
            native = NativeHarness(NATIVE_BINARY, database, create=True)
            host = RoutedHarness(native, router)
            effects = []
            options = {
                "budget": 3000,
                "reserve": 200,
                "scope": "task",
                "retrieval_limit": 8,
                "recent": 2,
            }
            try:
                turn = host.prepare_turn(
                    "native-external-1",
                    task.id,
                    "What is the release budget?",
                    requested_route=MemoryRoute.TASK,
                    **options,
                )
                self.assertEqual(turn.route, "task")
                self.assertIn(source.citation, turn.citations)

                payload = {"model": "external", "messages": turn.messages}
                raw = turn.before_model(
                    "model-1",
                    payload,
                    counter=lambda serialized: len(serialized.encode("utf-8")),
                    counting="utf8-json-bytes",
                )
                self.assertEqual(json.loads(raw), payload)
                # The provider call belongs to the host, after the checkpoint.
                turn.after_model("model-1", response={"choices": []})

                intent = turn.before_tool(
                    "tool-1", "publish", {"task_id": task.id}, ttl_ms=1000
                )
                self.assertEqual(effects, [])
                effects.append({"published": True})
                turn.after_tool(
                    "tool-1",
                    result=effects[0],
                    lease_token=intent["eligibility"]["token"],
                )
                saved = turn.commit({"text": "published"})
                self.assertEqual(saved["status"], "committed")

                replay = host.prepare_turn(
                    "native-external-1",
                    task.id,
                    "What is the release budget?",
                    requested_route=MemoryRoute.TASK,
                    **options,
                )
                self.assertEqual(replay.turn["status"], "committed")
                self.assertIsNone(replay.context)
                self.assertEqual(len(router.calls), 1)
                self.assertEqual(effects, [{"published": True}])
                self.assertEqual(
                    native.inspect("native-external-1")["status"], "committed"
                )
                self.assertEqual(
                    native.model_calls("native-external-1")[0]["response_json"],
                    '{"choices":[]}',
                )
            finally:
                host.close()
                memory.close()


if __name__ == "__main__":
    unittest.main()
