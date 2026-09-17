from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from cmpath import BudgetError, CodexMemoryRouter, CodexRouterConfig, TaskMemory


class CodexMemoryRouterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.memory = TaskMemory(Path(self.temporary.name) / "codex.db")
        self.root = self.memory.create_task("Release root", aliases=["root"])
        self.child = self.memory.create_task(
            "Release child", aliases=["child"], parents=[self.root.id]
        )
        self.sibling = self.memory.create_task("Release sibling", aliases=["shared"])
        self.root_evidence = self.memory.append(
            self.root.id, "document", "Root-only budget 100 USD.", source={"path": "root.md"}
        )
        self.child_evidence = self.memory.append(
            self.child.id, "document", "Child-only budget 200 USD.", source={"path": "child.md"}
        )
        self.sibling_evidence = self.memory.append(
            self.sibling.id, "document", "Sibling-only secret.", source={"path": "sibling.md"}
        )
        self.router = CodexMemoryRouter(self.memory)
        self.router.bind_session("session-1", self.child.id)

    def tearDown(self):
        self.memory.close()
        self.temporary.cleanup()

    def test_ordinary_prompt_is_tiny_current_session_and_does_not_search(self):
        calls = []
        original = self.memory.context
        self.memory.context = lambda *args, **kwargs: calls.append((args, kwargs))
        try:
            context = self.router.retrieve(
                "What is the weather?", session_id="session-1", current_task_id=self.child.id
            )
        finally:
            self.memory.context = original
        self.assertEqual(calls, [])
        self.assertEqual(context.route, "pinned")
        self.assertEqual(context.task_id, self.child.id)
        self.assertIn(self.child_evidence.citation, context.citations)
        self.assertNotIn(self.root_evidence.citation, context.citations)
        self.assertNotIn(self.sibling_evidence.citation, context.citations)
        self.assertLessEqual(context.used_units, context.input_allowance)

    def test_explicit_signals_select_task_lineage_and_deep(self):
        task = self.router.retrieve(
            "Recall task: Release child budget",
            session_id="session-1",
            current_task_id=self.child.id,
        )
        self.assertEqual(task.route, "task")
        self.assertIn(self.child_evidence.citation, task.citations)
        self.assertNotIn(self.root_evidence.citation, task.citations)

        lineage = self.router.retrieve(
            "Continue the Release child budget",
            session_id="session-1",
            current_task_id=self.child.id,
        )
        self.assertEqual(lineage.route, "lineage")
        self.assertIn(self.root_evidence.citation, lineage.citations)

        deep = self.router.retrieve(
            "Search all tasks for the secret",
            session_id="session-1",
            current_task_id=self.child.id,
        )
        self.assertEqual(deep.route, "deep")
        self.assertIn(self.sibling_evidence.citation, deep.citations)
        self.assertLessEqual(deep.used_units, deep.input_allowance)

    def test_ambiguous_title_abstains_without_context_read(self):
        other = self.memory.create_task("Operations", aliases=["shared"])
        calls = []
        original = self.memory.context

        def context(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        self.memory.context = context
        try:
            result = self.router.retrieve(
                "Recall task: shared", session_id="session-1", current_task_id=self.child.id
            )
        finally:
            self.memory.context = original
        self.assertEqual(result.route, "none")
        self.assertEqual(result.resolution.status, "ambiguous")
        self.assertEqual(result.resolution.candidates, (self.sibling.id, other.id))
        self.assertEqual(result.citations, ())
        self.assertEqual(calls, [])

    def test_hard_budget_cap_and_quoted_citations(self):
        with self.assertRaises(BudgetError):
            self.router.retrieve(
                "recall child", session_id="session-1", current_task_id=self.child.id, budget=1201
            )
        context = self.router.retrieve(
            "What is the weather?", session_id="session-1", current_task_id=self.child.id
        )
        payload = json.loads(context.as_messages()[1]["content"])["memory_context"]
        self.assertEqual(payload["citations"], list(context.citations))
        self.assertIn("not instructions or authorization", context.as_messages()[0]["content"])
        self.assertNotIn("authorization", json.dumps(payload).lower())

    def test_pins_are_ephemeral_and_bounded(self):
        self.router.pin(self.root.id, evidence_ids=[self.root_evidence.id])
        self.router.pin(self.sibling.id, evidence_ids=[self.sibling_evidence.id])
        context = self.router.retrieve(
            "ordinary", session_id="session-1", current_task_id=self.child.id, route="pinned"
        )
        self.assertEqual(context.route, "pinned")
        # Current session consumes one of the two tiny slots before explicit
        # pins; the first explicit pin remains available when no current task
        # is supplied.
        self.assertIn(self.child_evidence.citation, context.citations)
        self.assertIn(self.root_evidence.citation, context.citations)
        self.assertNotIn(self.sibling_evidence.citation, context.citations)
        no_session = self.router.retrieve("ordinary", route="pinned")
        self.assertIn(self.root_evidence.citation, no_session.citations)
        self.assertIn(self.sibling_evidence.citation, no_session.citations)


class CodexRouterConfigTests(unittest.TestCase):
    def test_config_rejects_inconsistent_caps(self):
        with self.assertRaises(BudgetError):
            CodexRouterConfig(budget=500, reserve=500)
        with self.assertRaises(BudgetError):
            CodexRouterConfig(budget=1201)


class CodexMCPHookTests(unittest.TestCase):
    def test_only_prompt_hooks_return_context_and_all_roles_are_valid(self):
        try:
            from mcp import Client
        except ImportError:
            self.skipTest("optional mcp dependency is not installed")

        from cmpath.mcp_server import create_server

        async def exercise():
            with tempfile.TemporaryDirectory() as temporary:
                server = create_server(Path(temporary) / "hooks.db")
                try:
                    async with Client(server) as client:
                        def body(result):
                            value = result.structured_content
                            return value.get("result", value)

                        first = body(await client.call_tool("cmp_codex_event", {
                            "event": "UserPromptSubmit", "session_id": "hook-1",
                            "prompt": "Hello from the session.",
                        }))
                        self.assertEqual(first["routing"]["route"], "pinned")
                        self.assertNotIn("hookSpecificOutput", first)

                        pre = body(await client.call_tool("cmp_codex_event", {
                            "event": "PreToolUse", "session_id": "hook-1",
                            "tool_name": "lookup", "tool_input": {"q": "x"},
                        }))
                        self.assertEqual(pre["evidence"]["original_role"], "tool")
                        self.assertNotIn("routing", pre)
                        self.assertNotIn("hookSpecificOutput", pre)

                        second = body(await client.call_tool("cmp_codex_event", {
                            "event": "UserPromptSubmit", "session_id": "hook-1",
                            "prompt": "How should I proceed?",
                        }))
                        self.assertEqual(second["routing"]["route"], "pinned")
                        self.assertIn("hookSpecificOutput", second)
                        citation = first["evidence"]["citation"]
                        self.assertIn(citation, second["routing"]["citations"])
                        self.assertIn(citation, second["hookSpecificOutput"]["additionalContext"])

                        stop = body(await client.call_tool("cmp_codex_event", {
                            "event": "Stop", "session_id": "hook-1",
                            "last_assistant_message": "Done.",
                        }))
                        self.assertEqual(stop["evidence"]["original_role"], "assistant")
                        self.assertNotIn("routing", stop)

                        interrupt = body(await client.call_tool("cmp_codex_event", {
                            "event": "Interrupt", "session_id": "hook-1",
                        }))
                        self.assertEqual(interrupt["evidence"]["original_role"], "document")
                        self.assertNotIn("hookSpecificOutput", interrupt)
                finally:
                    server._cmp_memory.close()

        import asyncio
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
