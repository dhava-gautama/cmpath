"""Cross-language maintenance, additive migration and model-response persistence."""
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cmpath import HarnessError, NativeHarness, TaskMemory
from _native_support import native_binary, native_binary_status

ROOT = Path(__file__).resolve().parents[1]
BINARY = native_binary(ROOT)
NATIVE_BINARY_OK, NATIVE_BINARY_REASON = native_binary_status(BINARY)
OLD = ROOT / "native/bin/measured-0.4.0a1/cmpath-native"
OLD_BINARY_OK, OLD_BINARY_REASON = native_binary_status(OLD)


@unittest.skipUnless(NATIVE_BINARY_OK, NATIVE_BINARY_REASON)
class NativeMaintenanceTests(unittest.TestCase):
    def test_export_retire_backup_and_old_id_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "memory.db"
            with NativeHarness(BINARY, db, create=True) as h:
                task = h.create_task("Maintenance α")
                s = h.begin("durable-id", task["id"], "Remember this evidence")
                s.checkpoint_model_request("m1", {"messages": s.messages})
                raw = '{ "value":900719925474099312345, "text":"café\\u0000tail" }'
                s.record_model_response("m1", raw)
                s.commit({"text": "Evidence retained"})
                self.assertEqual(h.model_calls("durable-id")[0]["response_json"], raw)
                output = root / "journal.jsonl"
                report = h.export_journal(output)
                exported = output.read_bytes()
                # The native wire/export contract is UTF-8.  Keep this byte
                # assertion explicit so a Windows code-page decode cannot
                # make a corrupted export look valid through replacement.
                self.assertIn(b'caf\xc3\xa9', exported)
                self.assertNotIn(b'caf\xe9', exported)
                rows = [json.loads(x) for x in output.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(report["rows"]["cmp_model_responses"], 1)
                self.assertEqual(next(x["row"]["response_json"] for x in rows if x.get("table") == "cmp_model_responses"), raw)
                with self.assertRaises(HarnessError):
                    h.export_journal(output)
                plan = h.retention_plan("2100-01-01T00:00:00Z")
                self.assertEqual(plan["rows"]["cmp_turns"], 1)
                h.apply_retention(plan["cutoff"], plan["plan_hash"])
                for callback in (lambda: h.inspect("durable-id"), lambda: h.begin("durable-id", task["id"], "Remember this evidence")):
                    with self.assertRaises(HarnessError) as error:
                        callback()
                    self.assertEqual(error.exception.code, "retired")
            with TaskMemory(db) as memory:
                memory.backup(root / "backup.db")
            with closing(sqlite3.connect(root / "backup.db")) as backup:
                with backup:
                    self.assertEqual(backup.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
                    self.assertEqual(backup.execute("SELECT count(*) FROM cmp_retired_turns").fetchone()[0], 1)
                    self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    @unittest.skipUnless(OLD_BINARY_OK, OLD_BINARY_REASON)
    def test_real_a1_database_upgrades_with_pending_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "upgrade.db"
            with NativeHarness(OLD, db, create=True) as old:
                task = old.create_task("Schema migration")
                turn = old.begin("pending-a1", task["id"], "Resume original context")
                original = turn.messages
                turn.checkpoint_model_request("old-model", {"messages": original})
            with NativeHarness(BINARY, db) as new:
                self.assertEqual(new.backend.info["harness_schema"], 4)
                session = new.recover("pending-a1", 1)
                self.assertEqual(session.messages, original)
                self.assertIsNone(session.model_calls()[0]["response_json"])
                session.record_model_response("old-model", {"choices": [], "large": 900719925474099312345})
                session.commit({"text": "Recovered"})
            # An old executable refuses the newer journal rather than bypassing tombstones.
            with self.assertRaises(HarnessError):
                NativeHarness(OLD, db)

    def test_model_response_fencing_and_json_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with NativeHarness(BINARY, Path(temporary) / "memory.db", create=True) as h:
                task = h.create_task("Responses")
                stale = h.begin("r", task["id"], "Inspect")
                stale.checkpoint_model_request("m", {"messages": stale.messages})
                current = h.recover("r", 1)
                with self.assertRaises(HarnessError) as error:
                    stale.record_model_response("m", {"value": True})
                self.assertEqual(error.exception.code, "fenced")
                for value in ([], "null", '{"x":NaN}'):
                    with self.assertRaises(ValueError):
                        current.record_model_response("m", value)
                current.record_model_response("m", {"value": True})
                with self.assertRaises(HarnessError) as error:
                    current.record_model_response("m", {"value": False})
                self.assertEqual(error.exception.code, "conflict")

    @unittest.skipUnless(OLD_BINARY_OK, OLD_BINARY_REASON)
    def test_already_open_a1_process_cannot_bypass_retired_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "mixed.db"
            with NativeHarness(OLD, db, create=True) as old:
                task = old.create_task("Older worker")
                old.run("original", task["id"], "Original query", lambda _: "Recorded")
                with NativeHarness(BINARY, db) as current:
                    plan = current.retention_plan("2100-01-01T00:00:00Z")
                    current.apply_retention(plan["cutoff"], plan["plan_hash"])
                    called = []
                    with self.assertRaises(HarnessError):
                        old.run("original", task["id"], "Original query", lambda _: called.append(1))
                    self.assertEqual(called, [])
                    self.assertEqual(current.backend.call("info")["messages"], 2)


if __name__ == "__main__":
    unittest.main()
