"""Local tests for the bounded managed-turn control plane."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest

from cmpath.control_plane import ControlPlane, ControlPlaneError, ControlPlaneServer
from cmpath.harness import Harness


class RecordingBackend:
    def __init__(self):
        self.calls = []

    def call(self, operation, **arguments):
        self.calls.append((operation, arguments))
        if operation == "info":
            return {"version": "fixture", "harness_schema": 4}
        if operation in {"begin", "recover"}:
            return {"request_id": arguments.get("request_id", "r"), "status": "pending",
                    "generation": arguments.get("generation", 1), "created": True,
                    "package": {"messages": [], "snapshot": {}}}
        return {"recorded": True, "operation": operation}

    def close(self):
        pass


class ControlPlaneServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.plane = ControlPlane(Harness(self.backend))

    def test_allowlisted_lifecycle_never_accepts_execution_or_sql(self):
        self.plane.dispatch("create_task", {"title": "Release"})
        self.plane.dispatch("begin", {"request_id": "r", "task_id": 1, "query": "ship"})
        self.plane.dispatch("model_request", {"request_id": "r", "generation": 1,
                                               "call_id": "m1", "payload": {"messages": []}})
        self.plane.dispatch("model_response", {"request_id": "r", "generation": 1,
                                                "call_id": "m1", "response": {"id": "ok"}})
        self.plane.dispatch("tool_start", {"request_id": "r", "generation": 1,
                                            "call_id": "t1", "name": "lookup", "arguments": {"x": 1}})
        self.plane.dispatch("action_eligible", {"request_id": "r", "generation": 1, "call_id": "t1"})
        self.plane.dispatch("tool_finish", {"request_id": "r", "generation": 1,
                                             "call_id": "t1", "result": {"ok": True}})
        self.plane.dispatch("commit", {"request_id": "r", "generation": 1,
                                        "reply": {"text": "done"}})
        self.plane.dispatch("abort", {"request_id": "r", "generation": 1})
        with self.assertRaises(ControlPlaneError):
            self.plane.dispatch("sql", {"query": "DROP TABLE tasks"})
        self.assertNotIn("execute", [item[0] for item in self.backend.calls])

    def test_model_payload_and_response_bytes_are_checkpointed(self):
        self.plane.dispatch("model_request", {"request_id": "r", "generation": 1,
                                               "call_id": "m1", "payload": {"text": "café"}})
        self.plane.dispatch("model_response", {"request_id": "r", "generation": 1,
                                                "call_id": "m1", "response": {"text": "ok"}})
        request = self.backend.calls[-2]
        response = self.backend.calls[-1]
        self.assertEqual(request[0], "model_request")
        self.assertEqual(request[1]["payload_json"], '{"text":"café"}')
        self.assertEqual(response[1]["response_json"], '{"text":"ok"}')


class ControlPlaneHTTPTests(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.plane = ControlPlane(Harness(self.backend), auth_token="secret")
        self.server = ControlPlaneServer(self.plane, port=0).start()
        self.addCleanup(self.server.close)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Content-Length", str(len(encoded)))
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def test_health_is_loopback_and_mutations_require_bearer_token(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, body = self.request("POST", "/v1/tasks", {"title": "nope"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        status, body = self.request("POST", "/v1/tasks", {"title": "Release"},
                                    {"Authorization": "Bearer secret"})
        self.assertEqual(status, 201)
        self.assertEqual(body["recorded"], True)

    def test_lifecycle_routes_and_body_limit_are_bounded(self):
        auth = {"Authorization": "Bearer secret"}
        status, _ = self.request("POST", "/v1/turns/begin",
                                {"request_id": "r", "task_id": 1, "query": "q"}, auth)
        self.assertEqual(status, 200)
        status, _ = self.request("POST", "/v1/turns/r/model-requests",
                                {"generation": 1, "call_id": "m", "payload": {"messages": []}}, auth)
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/v1/turns/r", headers=auth)
        self.assertEqual(status, 200)
        tiny = ControlPlaneServer(ControlPlane(Harness(RecordingBackend())), port=0,
                                  max_body_bytes=8).start()
        self.addCleanup(tiny.close)
        status, body = self._request_to(tiny, "POST", "/v1/tasks", b'{"title":"too long"}', auth)
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "payload_too_large")

    def test_managed_turn_sdk_envelope_is_compatible(self):
        auth = {"Authorization": "Bearer secret"}
        envelope = {"v": 1, "op": "before_tool", "request_id": "r",
                    "generation": 1, "call_id": "t1",
                    "args": {"name": "lookup", "arguments": {"x": 1}, "ttl_ms": 5000}}
        status, body = self.request("POST", "/v1/managed-turn", envelope, auth)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["v"], 1)
        self.assertIn("eligibility", body["result"])
        status, body = self.request("POST", "/v1/managed-turn", envelope)
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])

    @staticmethod
    def _request_to(server, method, path, encoded, headers):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        request_headers = dict(headers)
        request_headers["Content-Length"] = str(len(encoded))
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body


if __name__ == "__main__":
    unittest.main()
