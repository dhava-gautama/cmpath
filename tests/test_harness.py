import json
import os
import signal
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest

from cmpath import NativeHarness, HarnessError, TaskMemory
from _native_support import native_binary, native_binary_status

ROOT = Path(__file__).resolve().parents[1]
BINARY = native_binary(ROOT)
NATIVE_BINARY_OK, NATIVE_BINARY_REASON = native_binary_status(BINARY)


@unittest.skipUnless(NATIVE_BINARY_OK, NATIVE_BINARY_REASON)
class HarnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "memory.db"
        self.harness = NativeHarness(BINARY, self.database, create=True)
        self.addCleanup(self.harness.close)
        self.task = self.harness.create_task("Cobalt invoice", aliases=["monthly bill"],
                                             snapshot={"next": "verify"})

    def assertCode(self, code, operation):
        with self.assertRaises(HarnessError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_python_native_database_interoperability(self):
        with TaskMemory(self.database) as memory:
            source = memory.append(self.task["id"], "user", "Cobalt budget is 3400 USD.",
                                   source={"identifier": 900719925474099312345})
            memory.set_fact(self.task["id"], "budget", 3400, evidence_id=source.id)
        hits = self.harness.search("cobalt budget")
        self.assertEqual(hits[0]["source"]["identifier"], 900719925474099312345)
        self.assertEqual(hits[0]["citation"], source.citation)
        session = self.harness.begin("interoperable", self.task["id"], "What budget was approved?")
        self.assertIn(source.citation, session.turn["package"]["citations"])
        session.commit({"text": "Recorded", "snapshot": {"next": "draft"}})
        with TaskMemory(self.database) as memory:
            self.assertEqual(memory.task(self.task["id"]).snapshot, {"next": "draft"})
            self.assertEqual(memory.transcript(self.task["id"])[-1].content, "Recorded")
            self.assertTrue(memory.check()["ok"])

    def test_completed_retry_invokes_callback_once(self):
        calls = []
        def complete(session):
            calls.append(session.snapshot)
            return {"text": "Invoice prepared", "snapshot": {"next": "review"}}
        one = self.harness.run("once", self.task["id"], "Prepare invoice", complete)
        two = self.harness.run("once", self.task["id"], "Prepare invoice", complete)
        self.assertEqual(one, two)
        self.assertEqual(calls, [{"next": "verify"}])

    def test_process_kill_fencing_and_tool_reconciliation(self):
        session = self.harness.begin("crash", self.task["id"], "Run the workflow")
        session._call("tool_start", call_id="external-1", name="external_action", arguments={"id": 17})
        original_payload = session.turn["package"]["messages_json"]
        self.harness.backend._process.kill()
        self.harness.backend._process.wait()
        self.harness.close()
        self.harness = NativeHarness(BINARY, self.database)
        self.addCleanup(self.harness.close)
        self.assertEqual(self.harness.inspect("crash")["package"]["messages_json"], original_payload)
        self.assertCode("in_progress", lambda: self.harness.run("crash", self.task["id"], "Run the workflow", lambda _: self.fail("must not auto-retry")))
        recovered = self.harness.recover("crash", 1)
        self.assertEqual(recovered.turn["generation"], 2)
        self.assertCode("indeterminate_tool", lambda: recovered.tool("external-1", "external_action", {"id": 17}, lambda: self.fail("must reconcile")))
        recovered.reconcile_tool("external-1", {"confirmed": True})
        self.assertEqual(recovered.tool("external-1", "external_action", {"id": 17}, lambda: self.fail("must use saved result")), {"confirmed": True})
        self.assertCode("fenced", lambda: self.harness.backend.call("commit", request_id="crash", generation=1, reply={"text": "stale"}))
        recovered.commit({"text": "Reconciled"})

    def test_complete_provider_payload_with_tools_is_counted(self):
        session = self.harness.begin("model", self.task["id"], "Explain the budget", budget=1500)
        payload = {"model": "caller-configured", "messages": session.messages,
                   "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]}
        raw = session.checkpoint_model_request("call-1", payload, counter=lambda text: len(text.encode()), counting="utf8-json-bytes")
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                stored, units = connection.execute("SELECT payload_json,units FROM cmp_model_calls").fetchone()
        self.assertEqual(stored.encode(), raw)
        self.assertEqual(units, len(raw))
        payload["tools"][0]["function"]["description"] = "x" * 9000
        self.assertCode("budget", lambda: session.checkpoint_model_request("call-2", payload))

    def test_callback_failure_leaves_recoverable_turn(self):
        def complete(_):
            raise RuntimeError("provider stopped")
        with self.assertRaisesRegex(RuntimeError, "provider stopped"):
            self.harness.run("failure", self.task["id"], "Continue", complete)
        self.assertEqual(self.harness.inspect("failure")["status"], "pending")

    def test_stale_python_snapshot_is_not_overwritten(self):
        session = self.harness.begin("stale", self.task["id"], "Continue")
        with TaskMemory(self.database) as memory:
            memory.set_snapshot(self.task["id"], {"next": "external edit"}, expected_revision=1)
        self.assertCode("conflict", lambda: session.commit({"text": "Stale completion", "snapshot": {"next": "wrong"}}))
        self.assertEqual(self.harness.task(self.task["id"])["snapshot"], {"next": "external edit"})

    def test_scoped_snapshot_requires_and_records_provenance(self):
        source = self.harness.append_batch([{"task_id": self.task["id"], "role": "document",
                                             "content": "Approved amount is 3."}])[0]
        session = self.harness.begin("derived", self.task["id"], "Persist the approved amount",
                                     consistency="scope", scope="task")
        self.assertCode("provenance_required", lambda: session.commit(
            {"text": "Derived", "snapshot": {"amount": 3}}))
        saved = session.commit({"text": "Derived", "snapshot": {"amount": 3},
                                "provenance": [{"evidence_id": source["id"], "kind": "supports"}]})
        self.assertEqual(saved["status"], "committed")
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                self.assertEqual(connection.execute(
                    "SELECT evidence_id,kind FROM cmp_turn_provenance").fetchone(),
                    (source["id"], "supports"))

    def test_close_drains_native_process_handles(self):
        backend = self.harness.backend
        process = backend._process
        self.harness.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertFalse(backend._reader.is_alive())
        self.assertFalse(backend._errors.is_alive())

    def test_action_eligibility_revalidates_scope(self):
        session = self.harness.begin("eligible", self.task["id"], "Perform the approved action",
                                     consistency="scope", scope="task")
        started = session._call("tool_start", call_id="send", name="external_write",
                                arguments={"amount": 3})
        self.assertTrue(started["created"])
        lease = session.action_eligible("send", ttl_ms=1000)
        self.assertTrue(lease["token"])
        self.harness.append_batch([{"task_id": self.task["id"], "role": "document",
                                    "content": "Changed after eligibility."}])
        self.assertCode("stale_context", lambda: session.action_eligible("send", ttl_ms=1000))

    def test_large_integer_and_unicode_survive_bridge(self):
        snapshot = {"id": 900719925474099312345, "label": "Merak 🌊 日本語"}
        task = self.harness.create_task("Unicode state", snapshot=snapshot)
        session = self.harness.begin("unicode", task["id"], "Preserve the state")
        self.assertEqual(session.snapshot, snapshot)
        changed = session.messages
        changed[-1]["content"] = "mutated"
        self.assertNotEqual(session.messages[-1]["content"], "mutated")

    def test_unicode_alias_normalization_and_mixed_targets(self):
        self.assertEqual(self.harness.resolve("ＭＯＮＴＨＬＹ ＢＩＬＬ")["task_id"], self.task["id"])
        other = self.harness.create_task("Other project")
        self.assertEqual(self.harness.resolve(f"T{self.task['id']} and Other project")["status"], "ambiguous")
        self.assertEqual(self.harness.resolve(f"Ｔ{other['id']}")["task_id"], other["id"])

    def test_archived_task_is_open_in_the_saved_context(self):
        with TaskMemory(self.database) as memory:
            memory.archive(self.task["id"])
        session = self.harness.begin("archived", self.task["id"], "Continue this task")
        self.assertEqual(self.harness.task(self.task["id"])["status"], "open")
        self.assertIn('"status":"open"', session.messages[-2]["content"])
        session.commit({"text": "Resumed"})

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires POSIX process suspension")
    def test_transport_timeout_leaves_journal_recoverable(self):
        self.harness.begin("timeout", self.task["id"], "Preserve this input")
        self.harness.backend.timeout = 0.1
        os.kill(self.harness.backend._process.pid, signal.SIGSTOP)
        self.assertCode("transport", lambda: self.harness.backend.call("info"))
        with NativeHarness(BINARY, self.database) as reopened:
            self.assertEqual(reopened.inspect("timeout")["status"], "pending")

    def test_other_task_runs_while_model_callback_waits(self):
        other = self.harness.create_task("Independent work")
        entered = threading.Event()
        proceed = threading.Event()
        errors = []
        def complete(_):
            entered.set()
            self.assertTrue(proceed.wait(5))
            return "done"
        def run():
            try:
                self.harness.run("slow", self.task["id"], "Wait", complete)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(5))
        try:
            session = self.harness.begin("parallel", other["id"], "Work independently")
            self.assertTrue(session.turn["created"])
            session.commit({"text": "independent task completed"})
        finally:
            proceed.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_protocol_error_does_not_desynchronize_transport(self):
        self.assertCode("operation", lambda: self.harness.backend.call("missing_operation"))
        self.assertEqual(self.harness.task(self.task["id"])["title"], "Cobalt invoice")
        self.assertCode("conflict", lambda: self._reuse_different_request())

    def _reuse_different_request(self):
        self.harness.begin("same", self.task["id"], "first query")
        self.harness.begin("same", self.task["id"], "second query")

    def test_missing_database_is_not_created_for_read_connection(self):
        missing = Path(self.temp.name) / "missing.db"
        with self.assertRaises(HarnessError):
            NativeHarness(BINARY, missing)
        self.assertFalse(missing.exists())

    def test_atomic_batch_rolls_back_all_messages(self):
        self.assertCode("invalid", lambda: self.harness.append_batch([
            {"task_id": self.task["id"], "role": "user", "content": "should disappear"},
            {"task_id": self.task["id"], "role": "system", "content": "invalid role"},
        ]))
        self.assertEqual(self.harness.search("disappear"), [])


if __name__ == "__main__":
    unittest.main()
