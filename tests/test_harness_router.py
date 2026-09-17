from __future__ import annotations

import unittest

from cmpath import Harness, HarnessError, MemoryRoute, RoutedHarness


class _Backend:
    """Small deterministic backend fixture for the routed adapter."""

    def __init__(self):
        self.calls = []
        self.turn = {
            "request_id": "routed",
            "task_id": 7,
            "status": "pending",
            "generation": 1,
            "created": True,
            "package": {"messages": [{"role": "user", "content": "native"}],
                        "snapshot": {"step": "start"}},
        }
        self.tools = {}

    def call(self, operation, **arguments):
        self.calls.append((operation, arguments))
        if operation == "begin":
            return self.turn
        if operation == "model_request":
            return {"recorded": True}
        if operation == "model_response":
            return {"recorded": True}
        if operation == "tool_start":
            call_id = arguments["call_id"]
            if call_id in self.tools:
                return self.tools[call_id]
            value = {"call_id": call_id, "name": arguments["name"],
                     "arguments": arguments["arguments"], "status": "started",
                     "created": True}
            self.tools[call_id] = value
            return value
        if operation == "action_eligible":
            return {"token": "lease-1", "call_id": arguments["call_id"]}
        if operation == "tool_finish":
            call = self.tools[arguments["call_id"]]
            call.update(status="completed", result=arguments["result"])
            return call
        if operation == "commit":
            self.turn = {**self.turn, "status": "committed",
                         "reply": arguments["reply"], "created": False}
            return self.turn
        if operation == "abort":
            self.turn = {**self.turn, "status": "aborted"}
            return {"aborted": True}
        raise AssertionError(f"unexpected backend operation: {operation}")

    def close(self):
        self.calls.append(("close", {}))


class _Context:
    route = "lineage"
    task_id = 7
    citations = ("T7:M2",)
    reason = "fixture route"
    package = None

    def as_messages(self):
        return [{"role": "system", "content": "quoted"},
                {"role": "user", "content": "routed"}]

    def as_dict(self):
        return {"route": self.route, "task_id": self.task_id,
                "messages": self.as_messages(), "citations": list(self.citations)}


class _Router:
    def __init__(self):
        self.calls = []

    def retrieve(self, query, task_id=None, requested_route=None, **options):
        self.calls.append((query, task_id, requested_route, options))
        return _Context()


class RoutedHarnessTests(unittest.TestCase):
    def setUp(self):
        self.backend = _Backend()
        self.router = _Router()
        self.harness = Harness(self.backend)

    def test_missing_router_fails_before_begin(self):
        with self.assertRaises(TypeError):
            self.harness.prepare_turn("routed", 7, "question")
        self.assertEqual(self.backend.calls, [])

    def test_prepare_turn_is_opt_in_and_forwards_bounded_route_options(self):
        routed = self.harness.prepare_turn(
            "routed", 7, "question", router=self.router,
            budget=321, reserve=10, scope="lineage", retrieval_limit=5, recent=2,
        )
        self.assertEqual(routed.route, "lineage")
        self.assertEqual(routed.messages, _Context().as_messages())
        self.assertEqual(routed.citations, ("T7:M2",))
        self.assertEqual(self.router.calls[0], (
            "question", 7, None,
            {"budget": 321, "reserve": 10, "scope": "lineage",
             "retrieval_limit": 5, "recent": 2},
        ))

    def test_explicit_route_and_facade_are_supported(self):
        routed = RoutedHarness(self.harness, self.router).prepare_turn(
            "routed", 7, "question", requested_route=MemoryRoute.PINNED,
        )
        self.assertEqual(self.router.calls[0][2], MemoryRoute.PINNED)
        self.assertEqual(routed.route, "lineage")

    def test_all_five_boundaries_reach_native_session_without_execution(self):
        routed = self.harness.prepare_turn("routed", 7, "question", router=self.router)
        raw = routed.before_model("model-1", {"model": "fixture"})
        self.assertIsInstance(raw, bytes)
        routed.after_model("model-1", response={"choices": []})
        before = routed.before_tool("tool-1", "write", {"id": 3})
        self.assertEqual(before["eligibility"]["token"], "lease-1")
        routed.after_tool("tool-1", result={"ok": True},
                          lease_token=before["eligibility"]["token"])
        committed = routed.commit({"text": "done"})
        self.assertEqual(committed["status"], "committed")
        self.assertEqual([name for name, _ in self.backend.calls], [
            "begin", "model_request", "model_response", "tool_start",
            "action_eligible", "tool_finish", "commit",
        ])

    def test_run_router_opt_in_and_replay_do_not_retrieve_again(self):
        seen = []

        def complete(session):
            seen.append(session.messages)
            return "done"

        self.assertEqual(
            self.harness.run("routed", 7, "question", complete, router=self.router),
            {"text": "done"},
        )
        self.assertEqual(len(self.router.calls), 1)
        # The backend now reports a committed replay; no new route read occurs.
        self.harness.run("routed", 7, "question", complete, router=self.router)
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(seen, [_Context().as_messages()])

    def test_after_hooks_require_one_confirmed_outcome(self):
        routed = self.harness.prepare_turn("routed", 7, "question", router=self.router)
        with self.assertRaises(ValueError):
            routed.after_model("m")
        with self.assertRaises(ValueError):
            routed.after_tool("t")


if __name__ == "__main__":
    unittest.main()
