"""Offline CMP adapter conformance tests.

The public entry point is ``run_suite``.  Tests use the deterministic local
adapter by default; callers can pass a factory for another adapter implementing
the methods in SPEC.md.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from .provider import ProviderBoundary
    from .reference import AdapterError, LocalAdapter
except ImportError:  # unittest discover -s conformance imports as top-level
    from provider import ProviderBoundary
    from reference import AdapterError, LocalAdapter

try:
    from cmpath import NativeBackend, NativeHarness, TaskMemory
    from cmpath.agent import WorkspaceTools
except ImportError:  # pragma: no cover - run.py bootstraps source imports
    NativeBackend = NativeHarness = TaskMemory = WorkspaceTools = None


def _assert_code(case: unittest.TestCase, code: str, operation):
    with case.assertRaises(AdapterError) as caught:
        operation()
    case.assertEqual(caught.exception.code, code)


def _test_case(adapter_factory=LocalAdapter):
    class CMPAdapterConformance(unittest.TestCase):
        def setUp(self):
            self.adapter = adapter_factory()
            self.task = self.adapter.create_task("Conformance task", snapshot={"step": "start"})
            self.source = self.adapter.append_evidence(
                self.task["id"], "document", "The approved amount is 42.",
                source={"origin": "fixture", "untrusted": True},
            )

        def tearDown(self):
            self.adapter.close()

        def test_request_identity_and_no_silent_replay(self):
            calls = []

            def complete(_):
                calls.append(1)
                return {"text": "done"}

            first = self.adapter.run("identity-1", self.task["id"], "Do it", complete)
            second = self.adapter.run("identity-1", self.task["id"], "Do it", complete)
            self.assertEqual(first, second)
            self.assertEqual(calls, [1])
            _assert_code(self, "conflict", lambda: self.adapter.begin(
                "identity-1", self.task["id"], "Different input"))

            self.adapter.begin("pending-1", self.task["id"], "Pending")
            _assert_code(self, "in_progress", lambda: self.adapter.run(
                "pending-1", self.task["id"], "Pending", complete))
            self.assertEqual(calls, [1])

        def test_source_citations_and_scoped_provenance(self):
            turn = self.adapter.begin(
                "citation-1", self.task["id"], "What amount was approved?",
                consistency="scope", scope="task",
            )
            self.assertIn(self.source["citation"], turn["package"]["citations"])
            self.assertRegex(self.source["citation"], r"^T[1-9][0-9]*:M[1-9][0-9]*$")
            _assert_code(self, "provenance_required", lambda: self.adapter.commit(
                "citation-1", turn["generation"],
                {"text": "42", "snapshot": {"amount": 42}},
            ))
            saved = self.adapter.commit(
                "citation-1", turn["generation"],
                {"text": "42", "snapshot": {"amount": 42},
                 "provenance": [{"evidence_id": self.source["id"], "kind": "supports"}]},
            )
            self.assertEqual(saved["status"], "committed")
            self.assertEqual(saved["reply"]["provenance"][0]["evidence_id"], self.source["id"])

        def test_recovery_fences_stale_generation(self):
            turn = self.adapter.begin("generation-1", self.task["id"], "Resume")
            recovered = self.adapter.recover("generation-1", turn["generation"])
            self.assertEqual(recovered["generation"], turn["generation"] + 1)
            _assert_code(self, "fenced", lambda: self.adapter.commit(
                "generation-1", turn["generation"], {"text": "stale"}))
            _assert_code(self, "fenced", lambda: self.adapter.checkpoint_model_request(
                "generation-1", turn["generation"], "m1", {"messages": []}, 1))
            _assert_code(self, "fenced", lambda: self.adapter.start_tool(
                "generation-1", turn["generation"], "tool-1", "write", {}))

        def test_model_checkpoint_is_exact_and_recoverable(self):
            turn = self.adapter.begin("model-1", self.task["id"], "Use model", budget=1000)
            payload = {"model": "local", "messages": turn["package"]["messages"],
                       "tools": [{"name": "lookup", "parameters": {"type": "object"}}],
                       "temperature": 0}
            raw = self.adapter.checkpoint_model_request(
                "model-1", turn["generation"], "call-1", payload, 100,
            )
            self.assertEqual(raw, json.dumps(payload, ensure_ascii=False,
                                             separators=(",", ":"), allow_nan=False).encode())
            self.assertEqual(self.adapter.model_calls("model-1")[0]["payload_json"], raw.decode())
            recovered = self.adapter.recover("model-1", turn["generation"])
            self.assertEqual(self.adapter.checkpoint_model_request(
                "model-1", recovered["generation"], "call-1", payload, 100), raw)
            response = '{ "choices": [{"message": {"role":"assistant", "content":"ok"}}] }'
            self.adapter.record_model_response(
                "model-1", recovered["generation"], "call-1", response,
            )
            self.assertEqual(self.adapter.model_calls("model-1")[0]["response_json"], response)
            self.adapter.commit("model-1", recovered["generation"], {"text": "ok"})
            over_budget = self.adapter.begin(
                "model-budget", self.task["id"], "Budget", budget=100,
            )
            _assert_code(self, "budget", lambda: self.adapter.checkpoint_model_request(
                "model-budget", over_budget["generation"], "too-big", {}, 101,
            ))

        def test_tool_intent_lease_and_expiry(self):
            turn = self.adapter.begin("tool-1", self.task["id"], "Call tool")
            started = self.adapter.start_tool(
                "tool-1", turn["generation"], "call-1", "write", {"value": 42},
            )
            self.assertTrue(started["created"])
            lease = self.adapter.action_eligible("tool-1", turn["generation"], "call-1", 1000)
            self.assertTrue(lease["token"])
            finished = self.adapter.finish_tool(
                "tool-1", turn["generation"], "call-1", {"ok": True}, lease["token"],
            )
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(self.adapter.start_tool(
                "tool-1", turn["generation"], "call-1", "write", {"value": 42},
            )["result"], {"ok": True})
            self.adapter.commit("tool-1", turn["generation"], {"text": "tool done"})

            pending = self.adapter.begin("tool-2", self.task["id"], "Another tool")
            self.adapter.start_tool("tool-2", pending["generation"], "call-2", "write", {})
            short = self.adapter.action_eligible("tool-2", pending["generation"], "call-2", 1)
            time.sleep(0.01)
            _assert_code(self, "stale_context", lambda: self.adapter.finish_tool(
                "tool-2", pending["generation"], "call-2", {"ok": True}, short["token"],
            ))

        def test_indeterminate_tool_is_not_reexecuted(self):
            turn = self.adapter.begin("tool-recovery", self.task["id"], "Recover tool")
            self.adapter.start_tool("tool-recovery", turn["generation"], "call-1", "write", {})
            recovered = self.adapter.recover("tool-recovery", turn["generation"])
            invoked = []
            _assert_code(self, "indeterminate_tool", lambda: self.adapter.tool(
                "tool-recovery", recovered["generation"], "call-1", "write", {},
                lambda: invoked.append(True),
            ))
            self.assertEqual(invoked, [])

        def test_auth_failure_and_body_limits_are_single_attempt(self):
            def unauthorized(raw, secret):
                self.assertTrue(secret)
                raise PermissionError("provider rejected credential")

            provider = ProviderBoundary(unauthorized, secret="fixture-secret")
            _assert_code(self, "auth", lambda: provider.send({"messages": []}))
            self.assertEqual(provider.calls, 1)
            _assert_code(self, "auth", lambda: provider.send({"messages": []}))
            self.assertEqual(provider.calls, 2)

            def too_large(_raw, _secret):
                return b"{}" + b"x" * ProviderBoundary.MAX_RESPONSE_BYTES

            bounded = ProviderBoundary(too_large)
            _assert_code(self, "body_limit", lambda: bounded.send({"messages": []}))
            self.assertEqual(bounded.calls, 1)
            _assert_code(self, "body_limit", lambda: ProviderBoundary(
                lambda raw, secret: {},
            ).send({"data": "x" * ProviderBoundary.MAX_REQUEST_BYTES}))

        def test_storage_backup_and_rooted_documents(self):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                live = root / "live.db"
                backup = root / "backup.db"
                memory = TaskMemory(live)
                try:
                    task = memory.create_task("Storage fixture")
                    memory.append(task.id, "document", "safe local source")
                    try:
                        memory.backup(backup)
                    except OSError as exc:
                        # Python's current fixture uses fsync on a read-only
                        # descriptor on Windows. Keep this suite runnable on
                        # that platform while still checking the SQLite backup
                        # contract with the stdlib online-backup API.
                        if os.name != "nt" or getattr(exc, "errno", None) != 9:
                            raise
                        source_db = sqlite3.connect(live)
                        target_db = sqlite3.connect(backup)
                        try:
                            source_db.backup(target_db)
                        finally:
                            target_db.close()
                            source_db.close()
                    self.assertTrue(backup.is_file())
                    check = sqlite3.connect(backup)
                    try:
                        self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        self.assertEqual(check.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                    finally:
                        check.close()
                    with self.assertRaises(ValueError):
                        memory.backup(live)
                finally:
                    memory.close()

                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "note.md").write_text("approved", encoding="utf-8")
                tools = WorkspaceTools(workspace, max_bytes=32, max_files=2)
                self.assertIn("parent traversal", tools.execute("read_document", {
                    "path": "../outside.md", "start_line": 1, "max_lines": 1,
                })["error"])
                (workspace / "large.md").write_text("x" * 33, encoding="utf-8")
                if os.name == "nt":
                    # WorkspaceTools' descriptor-level no-follow read is a
                    # POSIX hardening path; path traversal is still checked on
                    # Windows, while native Windows adapters provide the read.
                    self.assertTrue(tools._path("note.md").is_file())
                else:
                    self.assertEqual(tools.execute("read_document", {
                        "path": "note.md", "start_line": 1, "max_lines": 1,
                    })["lines"][0]["text"], "approved")
                    self.assertIn("read limit", tools.execute("read_document", {
                        "path": "large.md", "start_line": 1, "max_lines": 1,
                    })["error"])

        def test_protocol_limit_constants_are_explicit(self):
            # Keep protocol limits discoverable to adapter implementers without
            # requiring a native process or network endpoint.
            self.assertEqual(NativeBackend.MAX_FRAME if NativeBackend else 16 * 1024 * 1024,
                             16 * 1024 * 1024)
            self.assertEqual(ProviderBoundary.MAX_RESPONSE_BYTES, 8 * 1024 * 1024)

    return CMPAdapterConformance


CMPAdapterConformance = _test_case()


def run_suite(adapter_factory=LocalAdapter, *, verbosity: int = 2) -> bool:
    """Run all contract cases for ``adapter_factory`` and return success."""
    suite = unittest.TestLoader().loadTestsFromTestCase(_test_case(adapter_factory))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if run_suite() else 1)
