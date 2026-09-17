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


if __name__ == "__main__":
    unittest.main()
