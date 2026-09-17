import json
from pathlib import Path
import tempfile
import unittest


class MCPServerTests(unittest.TestCase):
    def test_in_process_tools_preserve_sources_and_scope(self):
        try:
            from mcp import Client
        except ImportError:
            self.skipTest("optional mcp dependency is not installed")

        from cmpath.mcp_server import create_server

        async def exercise():
            with tempfile.TemporaryDirectory() as root:
                server = create_server(Path(root) / "memory.db")
                try:
                    async with Client(server) as client:
                        names = {tool.name for tool in (await client.list_tools()).tools}
                        self.assertIn("cmp_context", names)
                        self.assertIn("cmp_codex_event", names)
                        created = await client.call_tool("cmp_create_task", {"title": "Release"})
                        body = created.structured_content
                        task_id = body.get("result", body)["id"]
                        evidence = await client.call_tool(
                            "cmp_append_evidence",
                            {"task_id": task_id, "role": "document", "content": "Ship on Friday.", "source": {"path": "plan.md"}},
                        )
                        body = evidence.structured_content
                        record = body.get("result", body)
                        self.assertEqual(record["citation"], f"T{task_id}:M{record['id']}")
                        packed = await client.call_tool(
                            "cmp_context", {"task_id": task_id, "query": "When do we ship?"}
                        )
                        self.assertIn(record["citation"], json.dumps(packed.structured_content))
                        first = await client.call_tool("cmp_codex_event", {
                            "event": "UserPromptSubmit", "session_id": "session-1",
                            "turn_id": "turn-1", "cwd": "/repo", "model": "codex",
                            "prompt": "Remember the release plan."
                        })
                        first_body = first.structured_content
                        first_record = first_body.get("result", first_body)
                        second = await client.call_tool("cmp_codex_event", {
                            "event": "UserPromptSubmit", "session_id": "session-1",
                            "turn_id": "turn-2", "cwd": "/repo", "model": "codex",
                            "prompt": "What was the release plan?"
                        })
                        second_body = second.structured_content
                        second_record = second_body.get("result", second_body)
                        self.assertEqual(first_record["task_id"], second_record["task_id"])
                        self.assertIn("additionalContext", second_record["hookSpecificOutput"])
                finally:
                    server._cmp_memory.close()

        import asyncio
        asyncio.run(exercise())

    def test_cmp_route_is_bounded_ambiguous_safe_and_ephemeral(self):
        try:
            from mcp import Client
        except ImportError:
            self.skipTest("optional mcp dependency is not installed")

        from cmpath import TaskMemory
        from cmpath.mcp_server import create_server

        async def exercise():
            with tempfile.TemporaryDirectory() as root:
                db = Path(root) / "memory.db"
                with TaskMemory(db) as memory:
                    parent = memory.create_task("Parent plan", aliases=["parent"])
                    child = memory.create_task(
                        "Child plan", aliases=["child"], parents=[parent.id]
                    )
                    sibling = memory.create_task(
                        "Sibling plan", aliases=["shared"]
                    )
                    other_shared = memory.create_task(
                        "Another shared", aliases=["shared"]
                    )
                    parent_message = memory.append(
                        parent.id, "user", "Parent budget is 1200 USD."
                    )
                    child_message = memory.append(
                        child.id, "user", "Child budget is 3400 USD."
                    )
                    sibling_message = memory.append(
                        sibling.id, "user", "Sibling-only secret."
                    )
                    # Make the active task different from a later unresolved
                    # hint; cmp_route must not use it as a guess.
                    memory.resume(child.id)
                    before = memory.export()

                server = create_server(db)
                try:
                    async with Client(server) as client:
                        names = {
                            tool.name for tool in (await client.list_tools()).tools
                        }
                        self.assertIn("cmp_route", names)

                        def body(result):
                            value = result.structured_content
                            return value.get("result", value)

                        none = body(
                            await client.call_tool(
                                "cmp_route", {"query": "ignored", "route": "none"}
                            )
                        )
                        self.assertEqual(none["decision"]["route"], "none")
                        self.assertEqual(none["citations"], [])
                        self.assertEqual(
                            none["messages"], json.loads(none["messages_json"])
                        )

                        task = body(
                            await client.call_tool(
                                "cmp_route",
                                {
                                    "query": "When is the child budget?",
                                    "route": "task",
                                    "task_id": child.id,
                                    "budget": 500,
                                    "reserve": 25,
                                    "retrieval_limit": 4,
                                    "recent": 1,
                                },
                            )
                        )
                        self.assertEqual(task["decision"]["route"], "task")
                        self.assertEqual(task["decision"]["task_id"], child.id)
                        self.assertIn(child_message.citation, task["citations"])
                        self.assertNotIn(parent_message.citation, task["citations"])
                        self.assertLessEqual(
                            task["used_units"], task["input_allowance"]
                        )
                        self.assertEqual(
                            task["messages"], json.loads(task["messages_json"])
                        )

                        lineage = body(
                            await client.call_tool(
                                "cmp_route",
                                {
                                    "query": "budget",
                                    "route": "lineage",
                                    "task_id": child.id,
                                },
                            )
                        )
                        self.assertIn(parent_message.citation, lineage["citations"])

                        deep = body(
                            await client.call_tool(
                                "cmp_route",
                                {
                                    "query": "secret",
                                    "route": "deep",
                                    "task_id": child.id,
                                },
                            )
                        )
                        self.assertIn(sibling_message.citation, deep["citations"])

                        pinned = body(
                            await client.call_tool(
                                "cmp_route",
                                {
                                    "query": "caller request",
                                    "route": "pinned",
                                    "pinned_config": {
                                        "max_pinned_tasks": 1,
                                        "max_pinned_evidence": 1,
                                    },
                                    "pinned_input": {
                                        "task_id": parent.id,
                                        "evidence_ids": [parent_message.id],
                                    },
                                },
                            )
                        )
                        self.assertEqual(pinned["decision"]["route"], "pinned")
                        self.assertIn(parent_message.citation, pinned["citations"])
                        self.assertLessEqual(
                            pinned["used_units"], pinned["input_allowance"]
                        )

                        # A subsequent request gets a fresh router: the prior
                        # call's ephemeral pin must not leak into this result.
                        unpinned = body(
                            await client.call_tool(
                                "cmp_route",
                                {"query": "caller request", "route": "pinned"},
                            )
                        )
                        self.assertNotIn(parent_message.citation, unpinned["citations"])

                        unresolved = body(
                            await client.call_tool(
                                "cmp_route",
                                {
                                    "query": "budget",
                                    "route": "task",
                                    "task_hint": "shared",
                                },
                            )
                        )
                        self.assertEqual(unresolved["decision"]["route"], "none")
                        self.assertEqual(
                            unresolved["decision"]["resolution"]["status"],
                            "ambiguous",
                        )
                        self.assertEqual(unresolved["citations"], [])
                        self.assertEqual(
                            unresolved["decision"]["resolution"]["candidates"],
                            [sibling.id, other_shared.id],
                        )
                        with TaskMemory(db) as check:
                            self.assertEqual(check.state()["active_task"], child.id)
                finally:
                    server._cmp_memory.close()
                with TaskMemory(db) as check:
                    self.assertEqual(check.export(), before)

        import asyncio
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
