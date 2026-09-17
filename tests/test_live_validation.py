"""Local HTTP/native fixtures for live-runner integrity, never model-quality data."""
import contextlib
import base64
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
from _native_support import native_binary, native_binary_status

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_validation", ROOT / "scripts/run_live_validation.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
BINARY = native_binary(ROOT)
NATIVE_BINARY_OK, NATIVE_BINARY_REASON = native_binary_status(BINARY)


@contextlib.contextmanager
def server(callback):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            code, data, headers = callback(self.path, raw, dict(self.headers))
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    host = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=host.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{host.server_port}"
    finally:
        host.shutdown()
        host.server_close()
        thread.join()


def answer(text, calls=None):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class LiveValidationTests(unittest.TestCase):
    def test_malformed_successful_http_body_is_preserved_before_json_parsing(self):
        body = b' ["non-object provider response"] \n'
        def handle(path, raw, headers):
            return 200, body if path == "/json" else b'\xffinvalid-utf8', {}
        with tempfile.TemporaryDirectory() as directory, server(handle) as endpoint:
            for route in ("json", "bytes"):
                root = Path(directory) / route
                root.mkdir()
                transport = live.RecordedHTTP(live.AgentConfig(endpoint + "/" + route, "fixture", route, root), root)
                with self.assertRaises(ValueError):
                    transport(b'{}')
                self.assertEqual(transport.http_bodies_received, 1)
                self.assertEqual(transport.responses, 0)
                receipt = live.rows(root / "http_receipts.jsonl")[0]
                if route == "json":
                    self.assertEqual(receipt["response_json"].encode(), body)
                else:
                    self.assertEqual(base64.b64decode(receipt["body_base64"]), b'\xffinvalid-utf8')
                self.assertEqual(live.rows(root / "errors.jsonl")[0]["http_status"], 200)

    def test_wire_preservation_no_redirect_or_retry_and_credential_omission(self):
        requests = []
        wire = ' \n{"choices": [], "usage": {"prompt_tokens": 7}, "large":900719925474099312345}\n'
        def handle(path, raw, headers):
            requests.append((path, raw, headers))
            if path == "/redirect":
                return 307, b"", {"Location": "/must-not-reach"}
            return 200, wire.encode(), {}
        with tempfile.TemporaryDirectory() as directory, server(handle) as endpoint, patch.dict(os.environ, {"CMP_API_KEY": "fixture-private-key"}):
            root = Path(directory)
            config = live.AgentConfig(endpoint + "/complete", "fixture", "trace", root)
            transport = live.RecordedHTTP(config, root, limit=1)
            raw = b'{ "model" : "fixture", "messages": [] }'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(transport(raw).raw_json, wire)
            self.assertEqual(live.rows(root / "responses.jsonl")[0]["response_json"], wire)
            self.assertEqual(live.rows(root / "requests.jsonl")[0]["payload_json"].encode(), raw)
            with self.assertRaisesRegex(ValueError, "limit"):
                transport(raw)
            redirect_dir = root / "redirect"
            redirect_dir.mkdir()
            redirected = live.RecordedHTTP(live.AgentConfig(endpoint + "/redirect", "fixture", "redirect", root), redirect_dir)
            with self.assertRaises(urllib.error.HTTPError):
                redirected(raw)
            self.assertEqual([p for p, _, _ in requests], ["/complete", "/redirect"])
            self.assertEqual(requests[0][2]["Authorization"], "Bearer fixture-private-key")
            for path in root.rglob("*.json*"):
                self.assertNotIn("fixture-private-key", path.read_text())

    def test_full_paired_reader_and_exact_citations_without_label_leakage(self):
        class Fixture:
            config = type("Config", (), {"model": "fixture"})()
            count = 0
            payloads = []
            def __call__(self, raw):
                self.count += 1
                self.payloads.append(json.loads(raw))
                return answer("T1:M20 fixture, not the T1:M2 citation.")
        fixture = Fixture()
        original_rows = live.rows
        def checked_rows(path):
            if Path(path).name == "labels.jsonl":
                self.assertEqual(fixture.count, 80)
            return original_rows(path)
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "rows", checked_rows):
            summary = live.fixed_reader(fixture, ROOT / "results/pilot", Path(directory))
            self.assertEqual(summary["responses"], 80)
        self.assertEqual(len(fixture.payloads), 80)
        self.assertTrue(all(p["max_tokens"] == 200 and p["tools"] == [] and p["temperature"] == 0 for p in fixture.payloads))
        self.assertEqual(set(live.CITATION.findall("[T1:M20], T1:M2x, AT1:M2, [T2:M35].")), {"T1:M20", "T2:M35"})

    @unittest.skipUnless(NATIVE_BINARY_OK, NATIVE_BINARY_REASON)
    def test_actual_document_reads_controlled_pause_and_reopen_replay(self):
        received = []
        def handle(path, raw, headers):
            request = json.loads(raw)
            received.append(request)
            if request["messages"][-1]["role"] == "tool":
                tool_result = json.loads(request["messages"][-1]["content"])
                self.assertIn("lines", tool_result)
                value = answer("Fixture answer [docs/RETENTION.md:L30-L32].")
            else:
                value = answer(None, [{"id": "same-provider-id", "type": "function", "function": {
                    "name": "read_document", "arguments": json.dumps({"path": "docs/RETENTION.md", "start_line": 30, "max_lines": 3})}}])
            return 200, json.dumps(value).encode(), {}
        with tempfile.TemporaryDirectory() as directory, server(handle) as endpoint:
            root = Path(directory)
            config = live.AgentConfig(endpoint + "/complete", "fixture", "agent", root)
            transport = live.RecordedHTTP(config, root)
            with contextlib.redirect_stdout(io.StringIO()):
                result = live.agent_checks(transport, BINARY, root)
            self.assertEqual(transport.attempts, 4)
            self.assertEqual(len(live.rows(root / "tool_executions.jsonl")), 2)
            self.assertTrue(result["document_workspace_unchanged"])
            for task in result["tasks"]:
                self.assertTrue(all(task["committed_replay"].values()))
                self.assertEqual(task["successful_document_tool_calls"], 1)
            recovery = result["tasks"][1]["recovery"]
            for key in ("controlled_pause_exercised", "generation_increased", "saved_responses_unchanged", "completed_tools_not_reexecuted"):
                self.assertTrue(recovery[key], key)

    def test_probe_error_stops_cli_and_existing_output_blocks_repeated_spending(self):
        requests = []
        def handle(path, raw, headers):
            requests.append(raw)
            return 403, b"error code: 1010\n", {}
        with tempfile.TemporaryDirectory() as directory, server(handle) as endpoint, patch.dict(os.environ, {"CMP_API_KEY": "fixture-private-key"}):
            output = Path(directory) / "live"
            args = ["run_live_validation.py", "--endpoint", endpoint + "/complete", "--stage", "all", "--output", str(output)]
            with patch.object(live.sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(live.main(), 1)
                with self.assertRaises(FileExistsError):
                    live.main()
            self.assertEqual(len(requests), 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["provider_responses_received"], 0)
            self.assertEqual(summary["dispatch_attempts"], 1)
            self.assertNotIn("agent", summary)
            self.assertNotIn("fixed_reader", summary)


if __name__ == "__main__":
    unittest.main()
