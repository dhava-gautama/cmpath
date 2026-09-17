"""Local protocol fixtures; these tests do not evaluate a live language model."""
import json
import http.server
import subprocess
import threading
import urllib.error
import os
from pathlib import Path
import tempfile
import unittest

from cmpath.agent import AgentConfig, ProviderResponse, ResearchAgent, WorkspaceTools
from cmpath.harness import HarnessError, NativeHarness
from _native_support import native_binary, native_binary_status

ROOT = Path(__file__).resolve().parents[1]
BINARY = native_binary(ROOT)
NATIVE_BINARY_OK, NATIVE_BINARY_REASON = native_binary_status(BINARY)


def answer(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": ident, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}} for ident, name, args in calls]
    return {"choices": [{"message": message}]}


class WorkspaceTests(unittest.TestCase):
    def test_document_read_is_available_for_regular_workspace_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "docs"
            root.mkdir()
            (root / "note.md").write_text("first\nsecond\n", encoding="utf-8")
            tools = WorkspaceTools(root)
            result = tools.execute("read_document", {
                "path": "note.md", "start_line": 2, "max_lines": 1,
            })
            self.assertEqual(result["lines"], [{"line": 2, "text": "second"}])

    def test_root_and_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "docs"
            root.mkdir()
            outside = Path(folder) / "secret.txt"
            outside.write_text("private")
            (root / "link.txt").symlink_to(outside)
            tools = WorkspaceTools(root)
            for path in ("../secret.txt", str(outside), "link.txt"):
                self.assertIn("error", tools.execute("read_document", {"path": path}))
            self.assertEqual(tools.execute("list_documents", {})["documents"], [])

    @unittest.skipUnless(os.name == "nt", "Windows reparse-point coverage")
    def test_windows_directory_junction_is_not_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "docs"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.md").write_text("private", encoding="utf-8")
            junction = root / "linked"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"directory junction unavailable: {result.stderr.strip()}")
            tools = WorkspaceTools(root)
            self.assertIn("error", tools.execute("read_document", {
                "path": "linked/secret.md", "start_line": 1, "max_lines": 1,
            }))
            self.assertEqual(tools.execute("list_documents", {})["documents"], [])

    def test_configuration_rejects_invalid_limit_types(self):
        with tempfile.TemporaryDirectory() as folder:
            base = dict(endpoint="http://127.0.0.1:1", model="fixture", request_id="r", workspace=folder)
            for field, value in (("max_turns", True), ("budget", 3.5), ("max_tokens", "100"),
                                 ("timeout", "bad"), ("timeout", float("nan")), ("timeout", True)):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    AgentConfig(**base, **{field: value})

    def test_local_http_protocol_fixture_and_redirect_refusal(self):
        captured = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/redirect":
                    self.send_response(307)
                    self.send_header("Location", "/unexpected")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(answer("HTTP fixture")).encode())
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                base = f"http://127.0.0.1:{server.server_port}"
                raw = b'{"model":"fixture","messages":[]}'
                agent = ResearchAgent(None, AgentConfig(base + "/complete", "fixture", "http", folder))
                self.assertEqual(agent.dispatch(raw)["choices"][0]["message"]["content"], "HTTP fixture")
                redirect = ResearchAgent(None, AgentConfig(base + "/redirect", "fixture", "http", folder))
                with self.assertRaises(urllib.error.HTTPError):
                    redirect.dispatch(raw)
                self.assertEqual(captured, [raw, raw])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@unittest.skipUnless(NATIVE_BINARY_OK, NATIVE_BINARY_REASON)
class AgentProtocolFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        (self.docs / "research.md").write_text("Launch date: May 14.\nOwner: Morgan.\n")
        self.harness = NativeHarness(BINARY, self.root / "memory.db", create=True)
        self.addCleanup(self.harness.close)
        self.task = self.harness.create_task("Research fixture")
        self.config = AgentConfig("http://127.0.0.1:1/v1/chat/completions", "fixture", "request", self.docs)

    def test_multi_tool_provider_sequence_and_whole_request_counter(self):
        requests, counted = [], []
        first = answer(calls=[("a", "list_documents", {}), ("b", "read_document", {"path": "research.md"})])
        second = answer(calls=[("c", "search_documents", {"query": "Morgan"})])
        results = iter([first, second, answer("Morgan owns launch [research.md:L2-L2].")])
        def transport(raw):
            requests.append(json.loads(raw))
            return next(results)
        def counter(raw):
            counted.append(raw)
            return len(raw.encode("utf-8"))
        config = AgentConfig(**{**self.config.__dict__, "counting_scheme": "fixture-utf8-bytes"})
        agent = ResearchAgent(self.harness, config, counter=counter, transport=transport)
        reply = agent.run(self.task["id"], "Who owns launch?")
        self.assertIn("Morgan", reply["text"])
        self.assertEqual(agent.dispatch_count, 3)
        self.assertEqual(requests[1]["messages"][-3], first["choices"][0]["message"])
        self.assertEqual([m["role"] for m in requests[1]["messages"][-3:]], ["assistant", "tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in requests[1]["messages"][-2:]], ["a", "b"])
        for row in self.harness.model_calls("request"):
            self.assertIn(row["payload_json"], counted)
            self.assertIn('"tools":', row["payload_json"])
            self.assertEqual(row["units"], len(row["payload_json"].encode("utf-8")))

    def test_saved_response_and_completed_tool_recovery_no_repeat(self):
        first = answer(calls=[("a", "read_document", {"path": "research.md"})])
        agent = ResearchAgent(self.harness, self.config, transport=lambda raw: first)
        original_begin = self.harness.begin
        def begin(*args, **kwargs):
            session = original_begin(*args, **kwargs)
            checkpoint = session.checkpoint_model_request
            def crash(call_id, *args, **kwargs):
                if call_id == "agent-model-2":
                    raise RuntimeError("fixture crash before next checkpoint")
                return checkpoint(call_id, *args, **kwargs)
            session.checkpoint_model_request = crash
            return session
        self.harness.begin = begin
        with self.assertRaisesRegex(RuntimeError, "fixture crash"):
            agent.run(self.task["id"], "Who owns launch?")
        self.harness.begin = original_begin
        resumed = ResearchAgent(self.harness, self.config, transport=lambda raw: answer("Morgan [research.md:L2-L2]."))
        resumed.workspace.execute = lambda *args: self.fail("completed tool must not execute again")
        reply = resumed.run(self.task["id"], "Who owns launch?", resume=True)
        self.assertIn("Morgan", reply["text"])
        self.assertEqual(resumed.dispatch_count, 1)

    def test_unknown_model_outcome_requires_explicit_retry(self):
        bodies = []
        def fail(raw):
            bodies.append(raw)
            raise OSError("fixture connection lost")
        agent = ResearchAgent(self.harness, self.config, transport=fail)
        with self.assertRaises(OSError):
            agent.run(self.task["id"], "Question")
        retry = ResearchAgent(self.harness, self.config, transport=lambda raw: (bodies.append(raw) or answer("Recovered")))
        with self.assertRaises(HarnessError) as caught:
            retry.run(self.task["id"], "Question", resume=True)
        self.assertEqual(caught.exception.code, "indeterminate_model")
        self.assertEqual(retry.dispatch_count, 0)
        retry.run(self.task["id"], "Question", resume=True, unknown_outcome_policy="retry")
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(retry.dispatch_count, 1)

    def test_full_payload_over_budget_prevents_dispatch(self):
        def counter(raw):
            self.assertIn('"tools":', raw)
            self.assertIn('"model":"fixture"', raw)
            return 1000000
        config = AgentConfig(**{**self.config.__dict__, "counting_scheme": "fixture-over-budget"})
        agent = ResearchAgent(self.harness, config, counter=counter,
                              transport=lambda raw: self.fail("over-budget request dispatched"))
        with self.assertRaises(HarnessError) as caught:
            agent.run(self.task["id"], "Question")
        self.assertEqual(caught.exception.code, "budget")
        self.assertEqual(agent.dispatch_count, 0)

    def test_malformed_provider_response_is_durable_and_rejected(self):
        agent = ResearchAgent(self.harness, self.config, transport=lambda raw: {"choices": []})
        with self.assertRaisesRegex(ValueError, "Malformed Chat Completions"):
            agent.run(self.task["id"], "Question")
        self.assertIsNotNone(self.harness.model_calls("request")[0]["response_json"])
        retry = ResearchAgent(self.harness, self.config, transport=lambda raw: self.fail("must reuse response"))
        with self.assertRaisesRegex(ValueError, "Malformed Chat Completions"):
            retry.run(self.task["id"], "Question", resume=True)

    def test_changed_configuration_conflicts(self):
        agent = ResearchAgent(self.harness, self.config, transport=lambda raw: answer("Done"))
        agent.run(self.task["id"], "Question")
        other = self.root / "other"
        other.mkdir()
        for change in ({"model": "changed"}, {"max_turns": 9}, {"workspace": other}):
            config = AgentConfig(**{**self.config.__dict__, **change})
            changed = ResearchAgent(self.harness, config, transport=lambda raw: self.fail("must reject configuration"))
            with self.assertRaises(HarnessError) as caught:
                changed.run(self.task["id"], "Question")
            self.assertEqual(caught.exception.code, "conflict")

    def test_nonfinite_provider_tool_arguments_are_rejected(self):
        response = answer(calls=[("bad", "read_document", {"path": "research.md"})])
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"path":"research.md","start_line":NaN}'
        agent = ResearchAgent(self.harness, self.config, transport=lambda raw: response)
        with self.assertRaisesRegex(ValueError, "Malformed provider function"):
            agent.run(self.task["id"], "Question")
        self.assertEqual(self.harness.backend.call("tools", request_id="request"), [])

    def test_http_response_wire_json_preserved_and_reused_on_resume(self):
        raw_response = (' \n{ "large_id" : 900719925474099312345,\n'
                        ' "choices" : [ { "message" : { "role" : "assistant",'
                        ' "content" : "Café fixture answer" } } ] }\n ')
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(raw_response.encode("utf-8"))
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = AgentConfig(**{**self.config.__dict__, "endpoint": f"http://127.0.0.1:{server.server_port}/complete"})
            original_begin = self.harness.begin
            def begin(*args, **kwargs):
                session = original_begin(*args, **kwargs)
                def crash(reply):
                    raise RuntimeError("fixture crash before commit")
                session.commit = crash
                return session
            self.harness.begin = begin
            agent = ResearchAgent(self.harness, config)
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                agent.run(self.task["id"], "Question")
            self.harness.begin = original_begin
            saved = self.harness.model_calls("request")[0]["response_json"]
            self.assertEqual(saved.encode("utf-8"), raw_response.encode("utf-8"))
            parsed = ProviderResponse(saved)
            self.assertEqual(parsed.raw_json, raw_response)
            self.assertEqual(parsed["large_id"], 900719925474099312345)
            resumed = ResearchAgent(self.harness, config)
            reply = resumed.run(self.task["id"], "Question", resume=True)
            self.assertEqual(reply["text"], "Café fixture answer")
            self.assertEqual(resumed.dispatch_count, 0)
            self.assertEqual(len(requests), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
