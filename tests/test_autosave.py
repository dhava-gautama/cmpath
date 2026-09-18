"""Autosave: hook capture, redaction, dedup, transcript ingest, installer, the
--doctor health check, and the --heal wire sweep."""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cmpath.autosave import (_PARSER_VERSION, HASH_CAP, MAX_MESSAGE_CHARS,
                             STATE_KEY, TOOL_MAX_CHARS, Autosave,
                             audit_wire_cursor, content_hash,
                             iter_wire_events, iter_wire_messages,
                             locate_session_dir, project_name_from_path,
                             redact)

ROOT = Path(__file__).resolve().parent.parent
SRC = str(ROOT / "src")
HOOK = str(ROOT / "scripts" / "autosave_session.py")
INSTALLER = str(ROOT / "scripts" / "install_autosave.py")


def wire_line(event) -> str:
    return json.dumps(event)


def make_wire(path: Path, *, label: str = "", torn: bool = False) -> None:
    events = [
        {"type": "metadata", "protocol_version": "1.5"},
        {"type": "turn.prompt", "agentId": "main", "promptId": "p1",
         "input": [{"type": "text", "text": f"fix the login bug {label}".strip()}]},
        {"type": "context.append_loop_event",
         "event": {"type": "step.begin", "turnId": "0", "step": 1}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "turnId": "0", "step": 1,
                   "part": {"type": "think", "think": "internal reasoning"}}},
        {"type": "context.append_loop_event",
         "event": {"type": "tool.call", "name": "Bash", "args": {"command": "ls"}}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "turnId": "0", "step": 1,
                   "part": {"type": "text",
                            "text": f"fixed the login bug by retrying {label}".strip()}}},
        {"type": "turn.prompt", "agentId": "main", "promptId": "p2",
         "input": [{"type": "text", "text": f"now add a test {label}".strip()},
                   {"type": "image", "url": "x"}]},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(wire_line(event) + "\n")
        if torn:
            fh.write('{"type":"context.append_loop_event","event":'
                     '{"type":"content.part","part":{"type":"text","text":"late')


class RedactionTests(unittest.TestCase):
    def test_openai_style_keys(self):
        self.assertEqual(redact("key sk-abcdef1234567890ABCDEF end"),
                         "key [REDACTED:openai] end")

    def test_anthropic_style_keys_labelled_separately(self):
        out = redact("sk-ant-api03-abcdef123456789012345")
        self.assertEqual(out, "[REDACTED:anthropic]")

    def test_google_keys(self):
        out = redact("AIza" + "A" * 35)
        self.assertEqual(out, "[REDACTED:google]")

    def test_github_tokens(self):
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            out = redact(prefix + "A" * 36)
            self.assertEqual(out, "[REDACTED:github]", prefix)

    def test_github_fine_grained_pat(self):
        token = ("github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz"
                 "0123456789ABCDEF")
        out = redact(f"token {token} here")
        self.assertEqual(out, "token [REDACTED:github_pat] here")
        # the classic rule must not shadow or double-wrap it
        self.assertNotIn("github_pat_", out)

    def test_huggingface_tokens(self):
        out = redact("hf_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
        self.assertEqual(out, "[REDACTED:huggingface]")

    def test_slack_tokens(self):
        self.assertEqual(redact("xoxb-123456789012-abcdef"),
                         "[REDACTED:slack]")

    def test_aws_access_keys(self):
        self.assertEqual(redact("AKIAIOSFODNN7EXAMPLE"), "[REDACTED:aws]")
        self.assertEqual(redact("ASIAIOSFODNN7EXAMPLE"), "[REDACTED:aws]")

    def test_bearer_tokens(self):
        out = redact("Authorization: Bearer abc.def-ghi_jkl012345")
        self.assertIn("[REDACTED:bearer]", out)
        self.assertNotIn("abc.def", out)

    def test_assignment_generic_keyval(self):
        for text in ('API_KEY=abcd1234abcd1234', 'my_secret: "aabbccddeeff0011"',
                     'DB_PASSWORD = zz99x88y', 'auth_token=0123456789abcdef'):
            self.assertIn("[REDACTED:keyval]", redact(text), text)

    def test_private_key_block_removed_wholesale(self):
        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowABAA\nmore\n"
               "-----END RSA PRIVATE KEY-----")
        self.assertEqual(redact("pem: " + pem), "pem: [REDACTED:private_key]")

    def test_64_hex_digests(self):
        self.assertEqual(redact("sha " + "a" * 64 + " !"),
                         "sha [REDACTED:hex64] !")

    def test_multiple_secrets_in_one_string(self):
        out = redact("sk-abcdef1234567890ABCDEF and AKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("sk-", out)
        self.assertNotIn("AKIA", out)

    def test_clean_text_is_untouched(self):
        text = "The rocket launched at 05:13; password policies discussed. key idea."
        self.assertEqual(redact(text), text)


class AutosaveBase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.db = str(self.tmp / "auto.db")
        store = Autosave(self.db)
        self.addCleanup(store.close)
        self.store = store


class SaveTurnTests(AutosaveBase):
    def test_creates_project_task_with_alias(self):
        result = self.store.save_turn("s1", "/mnt/hdd/rocket", "hello", turn_id="0")
        self.assertEqual(result["status"], "saved")
        task = self.store.memory.task(result["task_id"])
        self.assertEqual(task.title, "Autosave: rocket")
        # cmpath normalises aliases through words(): "auto:rocket" -> "auto rocket"
        self.assertIn("auto rocket", task.aliases)
        self.assertEqual(self.store.memory.resolve("auto:rocket").status, "resolved")
        self.assertEqual(self.store.memory.resolve("auto:rocket").task_id,
                         result["task_id"])

    def test_reuses_task_across_sessions_and_truncates_turn_id(self):
        a = self.store.save_turn("s1", "/mnt/hdd/rocket", "one", turn_id="0")
        b = self.store.save_turn("s2", "/mnt/hdd/rocket", "two", turn_id=1)
        self.assertEqual(a["task_id"], b["task_id"])

    def test_same_turn_saved_twice_yields_one_evidence(self):
        self.store.save_turn("s1", "/p", "only once", turn_id="7")
        again = self.store.save_turn("s1", "/p", "only once", turn_id="7")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(len(self.store.memory.transcript(1)), 1)

    def test_different_turn_same_session_is_saved(self):
        self.store.save_turn("s1", "/p", "first", turn_id="0")
        second = self.store.save_turn("s1", "/p", "second", turn_id="1")
        self.assertEqual(second["status"], "saved")

    def test_source_records_session_turn_and_origin(self):
        self.store.save_turn("s1", "/p", "hello", turn_id="3",
                             extra={"origin_kind": "user"})
        evidence = self.store.memory.transcript(1)[0]
        self.assertEqual(evidence.role, "user")
        self.assertEqual(evidence.source["session"], "s1")
        self.assertEqual(evidence.source["kind"], "prompt")
        self.assertEqual(evidence.source["turn"], "3")
        self.assertEqual(evidence.source["origin_kind"], "user")

    def test_empty_and_whitespace_prompts_are_skipped(self):
        self.assertEqual(self.store.save_turn("s", "/p", "   ")["status"], "empty")
        self.assertEqual(self.store.save_turn("s", "/p", "")["status"], "empty")
        self.assertEqual(self.store.memory.transcript(1), [])

    def test_redaction_applies_to_prompts(self):
        self.store.save_turn("s", "/p", "token sk-abcdef1234567890ABCDEF leaked")
        evidence = self.store.memory.transcript(1)[0]
        self.assertIn("[REDACTED:openai]", evidence.content)
        self.assertNotIn("sk-", evidence.content)

    def test_truncation_caps_content_and_flags_source(self):
        self.store.save_turn("s", "/p", "y" * (MAX_MESSAGE_CHARS + 100), turn_id="0")
        evidence = self.store.memory.transcript(1)[0]
        self.assertEqual(len(evidence.content), MAX_MESSAGE_CHARS)
        self.assertTrue(evidence.source["truncated"])

    def test_identical_content_not_stored_twice_across_turns(self):
        self.store.save_turn("s", "/p", "ok", turn_id="0")
        dup = self.store.save_turn("s", "/p", "ok", turn_id="1")
        self.assertEqual(dup["status"], "duplicate")

    def test_seen_fact_is_capped(self):
        store = Autosave(self.db, seen_cap=3)
        self.addCleanup(store.close)
        for i in range(6):
            store.save_turn("s", "/p", f"turn {i}", turn_id=str(i))
        fact = store.memory.fact(1, "autosave_state")
        self.assertEqual(len(fact["value"]["seen"]), 3)
        self.assertEqual(fact["value"]["seen"], ["s:3", "s:4", "s:5"])


class SaveTurnAtomicityTests(AutosaveBase):
    """The message row and the ``autosave_state`` fact commit in one batch.

    ``save_turn`` used to append through one ``memory.batch()`` and store the
    state fact through another; a crash between the two left a task with
    messages but no state fact — the doctor's exit-1 rule 4 — and the recovery
    re-scan from line 0 without the stored-digest filter could duplicate rows.
    """

    def test_state_write_failure_rolls_back_the_message(self):
        with mock.patch.object(self.store, "_store_state",
                               side_effect=RuntimeError("state write failed")):
            with self.assertRaises(RuntimeError):
                self.store.save_turn("s1", "/p", "all or nothing", turn_id="0")
        self.assertEqual(self.store.memory.transcript(1), [])
        self.assertIsNone(self.store.memory.fact(1, STATE_KEY))

    def test_failed_save_leaves_no_seen_or_hashes_residue(self):
        with mock.patch.object(self.store, "_store_state",
                               side_effect=RuntimeError("state write failed")):
            with self.assertRaises(RuntimeError):
                self.store.save_turn("s1", "/p", "retry me", turn_id="0")
        # The seen key and digest rolled back with the row, so the retry is a
        # fresh save, not a duplicate, and lands exactly once.
        result = self.store.save_turn("s1", "/p", "retry me", turn_id="0")
        self.assertEqual(result["status"], "saved")
        rows = self.store.memory.transcript(result["task_id"])
        self.assertEqual([r.content for r in rows], ["retry me"])
        fact = self.store.memory.fact(result["task_id"], STATE_KEY)
        self.assertEqual(fact["value"]["seen"], ["s1:0"])

    def test_save_that_appends_nothing_writes_no_fact(self):
        self.assertEqual(self.store.save_turn("s", "/p", "")["status"], "empty")
        self.assertEqual(self.store.save_turn("s", "/p", "   ")["status"], "empty")
        self.assertEqual(self.store.memory.transcript(1), [])
        self.assertIsNone(self.store.memory.fact(1, STATE_KEY))

    def test_duplicate_save_writes_no_new_fact_revision(self):
        first = self.store.save_turn("s", "/p", "once", turn_id="0")
        revision = self.store.memory.fact(first["task_id"], STATE_KEY)["revision"]
        again = self.store.save_turn("s", "/p", "once", turn_id="0")
        self.assertEqual(again["status"], "duplicate")
        fact = self.store.memory.fact(first["task_id"], STATE_KEY)
        self.assertEqual(fact["revision"], revision)

    def test_normal_save_keeps_prompt_and_fact_in_step(self):
        result = self.store.save_turn("s1", "/p", "hello", turn_id="0")
        self.assertEqual(result["status"], "saved")
        rows = self.store.memory.transcript(result["task_id"])
        self.assertEqual([r.content for r in rows], ["hello"])
        fact = self.store.memory.fact(result["task_id"], STATE_KEY)
        self.assertEqual(fact["value"]["seen"], ["s1:0"])
        self.assertEqual(fact["value"]["hashes"], [content_hash("hello")])
        self.assertEqual(fact["evidence_id"], rows[0].id)


class DryRunTests(AutosaveBase):
    def test_dry_run_saves_nothing_and_missing_db_stays_missing(self):
        missing = self.tmp / "sub" / "dry.db"
        store = Autosave(missing, write=False)
        self.addCleanup(store.close)
        result = store.save_turn("s", "/p", "hello", turn_id="0")
        self.assertEqual(result["status"], "would_save")
        self.assertFalse(missing.exists())

    def _checksums(self) -> dict:
        """sha1 of the durable database file and its WAL (missing -> None).

        ``-shm`` is deliberately excluded: it is a throwaway shared-memory
        index of reader/writer slots that any second connection to a WAL
        database rewrites, even one that only opens and closes.
        """
        out = {}
        for suffix in ("", "-wal"):
            candidate = Path(self.db + suffix)
            out[suffix or "db"] = (hashlib.sha1(candidate.read_bytes()).hexdigest()
                                   if candidate.is_file() else None)
        return out

    def test_dry_run_rebuild_changes_no_bytes_and_deletes_nothing(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        seeded = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(seeded["added"], 4)  # 2 prompts + tool call + assistant
        task_id = seeded["task_id"]
        before_bytes = self._checksums()
        before_rows = [(r.id, r.role, r.content)
                       for r in self.store.memory.transcript(task_id)]

        # The writable store stays open across the whole test, so the -wal
        # file persists and is compared byte-for-byte too.
        dry = Autosave(self.db, write=False)
        self.addCleanup(dry.close)
        result = dry.ingest_transcript(wire, session_id="s", cwd="/proj",
                                       rebuild=True)

        self.assertNotIn("would_delete", result)
        self.assertNotIn("rebuilt", result)
        self.assertTrue(result["reset_cursor"])
        self.assertEqual(result["added"], 0)
        self.assertEqual(self._checksums(), before_bytes)
        self.assertEqual([(r.id, r.role, r.content)
                          for r in self.store.memory.transcript(task_id)],
                         before_rows)

    def test_dry_run_creates_no_project_task(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        self.assertEqual(self.store.memory.tasks(), [])
        dry = Autosave(self.db, write=False)
        self.addCleanup(dry.close)
        before = self._checksums()

        saved = dry.save_turn("s", "/proj", "hello", turn_id="0")
        self.assertEqual(saved["status"], "would_save")
        self.assertIsNone(saved["task_id"])
        result = dry.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(result["status"], "would_ingest")
        self.assertEqual(result["added"], 4)
        self.assertIsNone(result["task_id"])

        self.assertEqual(self.store.memory.tasks(), [])
        self.assertEqual(self._checksums(), before)


class IngestTests(AutosaveBase):
    def test_extracts_user_assistant_and_tool_text(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        result = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(result["added"], 4)  # 2 prompts + tool call + assistant
        rows = self.store.memory.transcript(result["task_id"])
        self.assertEqual([r.role for r in rows],
                         ["user", "tool", "assistant", "user"])
        self.assertEqual(rows[1].content, "tool call: Bash\ncommand: ls")
        self.assertEqual(rows[1].source["kind"], "wire")
        self.assertEqual(rows[1].source["line"], 5)
        self.assertIn("fixed the login bug", rows[2].content)
        self.assertEqual(rows[2].source["kind"], "wire")
        self.assertEqual(rows[2].source["line"], 6)

    def test_ingest_is_idempotent(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(first["added"], 4)
        self.assertIn(second["status"], ("clean", "unchanged"))
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(self.store.memory.transcript(first["task_id"])), 4)

    def test_torn_last_line_is_deferred_not_lost(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire, torn=True)
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(first["added"], 4)
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(' text"}}}\n')  # the line completes later
        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(second["added"], 1)
        self.assertIn("late text", " ".join(
            e.content for e in self.store.memory.transcript(1)))

    def test_wire_prompt_matching_saved_hook_prompt_is_deduped(self):
        self.store.save_turn("s", "/proj", "fix the login bug", turn_id="0")
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        result = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        # assistant text + tool call + second prompt
        self.assertEqual(result["added"], 3)

    def test_iter_wire_messages_stops_at_torn_line(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire, torn=True)
        lines = list(iter_wire_messages(wire))
        self.assertTrue(all(l < 8 for l, _r, _t in lines))

    def test_truncated_path_is_rescanned_not_skipped(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(first["added"], 4)
        task_id = first["task_id"]

        wire.write_text(wire_line(
            {"type": "turn.prompt", "agentId": "main", "promptId": "p9",
             "input": [{"type": "text", "text": "replaced with shorter content"}]}
        ) + "\n", encoding="utf-8")
        stamp = time.time() + 5
        os.utime(wire, (stamp, stamp))

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertTrue(second["reset_cursor"])
        self.assertEqual(second["added"], 1)
        contents = [e.content for e in self.store.memory.transcript(task_id)]
        self.assertIn("replaced with shorter content", contents)
        self.assertIn("fix the login bug", contents)  # evidence is append-only

    def test_rewritten_same_size_path_is_rescanned(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        self.assertEqual(
            self.store.ingest_transcript(wire, session_id="s", cwd="/proj")["added"], 4)

        text = wire.read_text(encoding="utf-8")
        size_before = wire.stat().st_size
        # same byte length, different content: only the head marker can tell
        wire.write_text(text.replace("login bug", "logon bug"), encoding="utf-8")
        self.assertEqual(wire.stat().st_size, size_before)
        stamp = time.time() + 5
        os.utime(wire, (stamp, stamp))

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertTrue(second["reset_cursor"])
        self.assertEqual(second["added"], 2)  # two rewritten messages; tool row unchanged

    def test_legacy_cursor_entry_is_upgraded_and_rescanned(self):
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        task_id = first["task_id"]
        payload = self.store.memory.fact(task_id, "autosave_state")["value"]
        key, entry = next(iter(payload["cursors"].items()))
        self.assertIn("size", entry)
        self.assertIn("head", entry)
        # A cursor written by an older cmpath: line and mtime only.
        payload["cursors"] = {key: {"line": entry["line"],
                                    "mtime": entry["mtime"]}}
        self.store.memory.set_fact(task_id, "autosave_state", payload,
                                   evidence_id=payload["last_evidence"])

        with wire.open("a", encoding="utf-8") as fh:
            fh.write(wire_line({"type": "turn.prompt", "agentId": "main",
                                "promptId": "p3",
                                "input": [{"type": "text", "text": "third ask"}]})
                     + "\n")
        stamp = time.time() + 5
        os.utime(wire, (stamp, stamp))

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertTrue(second["reset_cursor"])  # pre-stamp cursor -> migrate
        self.assertEqual(second["added"], 1)
        upgraded = self.store.memory.fact(task_id, "autosave_state")["value"]
        self.assertIn("size", upgraded["cursors"][key])
        self.assertIn("head", upgraded["cursors"][key])
        self.assertEqual(upgraded["cursors"][key]["parser"], _PARSER_VERSION)

    def test_pre_stamp_cursor_migrates_even_when_untouched(self):
        """A cursor with no parser stamp is migrated, mtime match or not.

        Before the stamp, a legacy ``{line, mtime}`` cursor whose mtime still
        matched was resumed as-is. The stamp makes that unsafe: the cursor
        predates the parser that is running, so whether its lines are stored
        is unknowable from the cursor alone, and the one-time migration
        re-scans from line 0 (dedup-safe) rather than resume. Nothing is
        missing here, so the pass adds nothing — but it re-stamps the cursor,
        so the migration happens once.
        """
        wire = self.tmp / "wire.jsonl"
        make_wire(wire)
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        task_id = first["task_id"]
        payload = self.store.memory.fact(task_id, "autosave_state")["value"]
        key, entry = next(iter(payload["cursors"].items()))
        payload["cursors"] = {key: {"line": entry["line"],
                                    "mtime": entry["mtime"]}}
        self.store.memory.set_fact(task_id, "autosave_state", payload,
                                   evidence_id=payload["last_evidence"])

        before = [r.id for r in self.store.memory.transcript(task_id)]
        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertTrue(second["reset_cursor"])
        self.assertEqual(second["added"], 0)  # nothing was skipped: dedup holds
        self.assertEqual([r.id for r in self.store.memory.transcript(task_id)],
                         before)
        migrated = self.store.memory.fact(task_id, "autosave_state")["value"]
        self.assertEqual(migrated["cursors"][key]["parser"], _PARSER_VERSION)

        third = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(third["status"], "unchanged")  # migration is one-time
        self.assertFalse(third["reset_cursor"])


def make_stream_wire(path: Path) -> None:
    """A wire with stepUuid-keyed assistant deltas, like the production schema."""
    events = [
        {"type": "metadata", "protocol_version": "1.5"},
        {"type": "turn.prompt", "agentId": "main", "promptId": "p1",
         "input": [{"type": "text", "text": "summarize the design"}]},
        {"type": "context.append_loop_event",
         "event": {"type": "step.begin", "turnId": "0", "step": 1,
                   "stepUuid": "uu-1"}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "turnId": "0", "step": 1, "stepUuid": "uu-1",
                   "part": {"type": "text", "text": "The design has"}}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "turnId": "0", "step": 1, "stepUuid": "uu-1",
                   "part": {"type": "think", "think": ""}}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "turnId": "0", "step": 1, "stepUuid": "uu-1",
                   "part": {"type": "text", "text": " three parts:"}}},
        {"type": "context.append_loop_event",
         "event": {"type": "tool.call", "name": "Bash", "args": {"command": "ls"}}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "turnId": "0", "step": 1, "stepUuid": "uu-1",
                   "part": {"type": "text", "text": " alpha, beta, gamma."}}},
        {"type": "context.append_loop_event",
         "event": {"type": "step.end", "turnId": "0", "step": 1, "stepUuid": "uu-1"}},
        {"type": "context.append_loop_event",
         "event": {"type": "step.begin", "turnId": "0", "step": 2,
                   "stepUuid": "uu-2"}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "turnId": "0", "step": 2, "stepUuid": "uu-2",
                   "part": {"type": "text", "text": "Done."}}},
        {"type": "context.append_loop_event",
         "event": {"type": "step.end", "turnId": "0", "step": 2, "stepUuid": "uu-2"}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part",
                   "part": {"type": "text", "text": "unkeyed fragment one"}}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part",
                   "part": {"type": "text", "text": "unkeyed fragment two"}}},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(wire_line(event) + "\n")


def make_bulk_wire(path: Path, count: int) -> None:
    """``count`` one-row messages: exactly one wire line per stored row.

    Unkeyed ``content.part`` events never coalesce, so the row count tracks the
    line count. Used to get above the 500-row pass cap and the 1000-digest
    window, which the small fixtures elsewhere cannot reach.
    """
    with path.open("w", encoding="utf-8") as fh:
        for i in range(count):
            fh.write(wire_line(
                {"type": "context.append_loop_event",
                 "event": {"type": "content.part",
                           "part": {"type": "text",
                                    "text": f"message number {i:04d} body"}}}) + "\n")


def make_padded_wire(path: Path, tail: str) -> None:
    """Three stored rows whose tail is the only thing that can change.

    The prompt line is long enough that byte 512 falls inside it, so rewriting
    ``tail`` leaves both the file size and the 512-byte head marker identical:
    only the tail distinguishes the two files.
    """
    events = [
        {"type": "metadata", "protocol_version": "1.5"},
        {"type": "turn.prompt", "agentId": "main", "promptId": "p1",
         "input": [{"type": "text", "text": "pad " + "x" * 600}]},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part",
                   "part": {"type": "text", "text": "bravo body"}}},
        {"type": "context.append_loop_event",
         "event": {"type": "content.part", "part": {"type": "text", "text": tail}}},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(wire_line(event) + "\n")


class CoalescingTests(AutosaveBase):
    def test_deltas_coalesce_per_step(self):
        wire = self.tmp / "wire.jsonl"
        make_stream_wire(wire)
        result = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        rows = self.store.memory.transcript(result["task_id"])
        assistants = [r for r in rows if r.role == "assistant"]
        # prompt + assistant 4-6 + tool 7 + assistant 8 + Done. + 2 unkeyed
        self.assertEqual(result["added"], 7)
        # the tool call sits between the deltas, so it splits the step group
        self.assertEqual(assistants[0].content, "The design has three parts:")
        self.assertEqual(assistants[0].source["line"], "4-6")
        self.assertEqual(assistants[1].content, "alpha, beta, gamma.")
        self.assertEqual(assistants[1].source["line"], 8)
        self.assertEqual(assistants[2].content, "Done.")
        tools = [r for r in rows if r.role == "tool"]
        self.assertEqual([r.content for r in tools], ["tool call: Bash\ncommand: ls"])
        self.assertEqual(tools[0].source["line"], 7)

    def test_unkeyed_events_are_never_merged(self):
        wire = self.tmp / "wire.jsonl"
        make_stream_wire(wire)
        result = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        contents = [r.content for r in self.store.memory.transcript(result["task_id"])]
        self.assertIn("unkeyed fragment one", contents)
        self.assertIn("unkeyed fragment two", contents)

    def test_rebuild_twice_converges_without_duplicates(self):
        wire = self.tmp / "wire.jsonl"
        make_stream_wire(wire)
        self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        before = [r.id for r in self.store.memory.transcript(1)]
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj",
                                             rebuild=True)
        # rebuild restarts the scan; it neither deletes nor re-appends
        self.assertIs(first["reset_cursor"], True)
        self.assertEqual(first["added"], 0)
        self.assertNotIn("rebuilt", first)
        self.assertNotIn("would_delete", first)
        rows1 = [r.content for r in self.store.memory.transcript(first["task_id"])]
        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj",
                                              rebuild=True)
        rows2 = [r.content for r in self.store.memory.transcript(first["task_id"])]
        self.assertEqual(rows1, rows2)
        self.assertEqual(len(rows2), len(set(rows2)))
        self.assertEqual([r.id for r in self.store.memory.transcript(1)], before)
        self.assertTrue(self.store.memory.check()["ok"])

    def test_rebuild_preserves_prompt_rows_and_other_paths(self):
        saved = self.store.save_turn("s", "/proj", "summarize the design",
                                     turn_id="0")
        self.assertEqual(saved["status"], "saved")
        wire = self.tmp / "wire.jsonl"
        make_stream_wire(wire)
        other = self.tmp / "other.jsonl"
        make_wire(other, label="(other)")
        self.store.ingest_transcript(other, session_id="s", cwd="/proj")
        result = self.store.ingest_transcript(wire, session_id="s", cwd="/proj",
                                              rebuild=True)
        rows = self.store.memory.transcript(result["task_id"])
        kinds = [(r.role, r.source["kind"]) for r in rows]
        self.assertIn(("user", "prompt"), kinds)
        self.assertTrue(any(r.source.get("path", "").endswith("other.jsonl")
                            for r in rows if r.source["kind"] == "wire"))
        # the wire prompt text equals the hook-saved prompt: no duplicate content
        contents = [r.content for r in rows]
        self.assertEqual(len(contents), len(set(contents)))


class BulkTranscriptTests(AutosaveBase):
    """Paths larger than the 500-row pass cap and the 1000-digest window.

    Both thresholds sit above every fixture in this file's other tests, which
    is why the defects they hide were not caught there.
    """

    def test_static_transcript_beyond_cap_is_completed_not_stuck(self):
        wire = self.tmp / "wire.jsonl"
        make_bulk_wire(wire, 900)
        task_id = None

        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        task_id = first["task_id"]
        self.assertEqual(first["added"], 500)
        self.assertEqual(first["scanned_to"], 500)
        self.assertEqual(len(self.store.memory.transcript(task_id)), 500)

        # The pass stopped at the cap, not at the end of the file, so the
        # cursor it just wrote must not be trusted as "file unchanged".
        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertFalse(second["reset_cursor"])
        self.assertEqual(second["added"], 400)
        self.assertEqual(second["scanned_to"], 900)
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertEqual(len(contents), 900)
        self.assertEqual(len(set(contents)), 900)

        third = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(third["status"], "unchanged")
        self.assertEqual(third["added"], 0)
        self.assertEqual(len(self.store.memory.transcript(task_id)), 900)

    def test_rebuild_converges_on_large_file(self):
        wire = self.tmp / "wire.jsonl"
        make_bulk_wire(wire, 1200)
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        task_id = first["task_id"]
        self.assertEqual(first["added"], 500)

        totals = []
        for _ in range(3):
            self.store.ingest_transcript(wire, session_id="s", cwd="/proj",
                                         rebuild=True)
            contents = [r.content for r in self.store.memory.transcript(task_id)]
            self.assertEqual(len(contents), len(set(contents)),  # no duplicates
                             f"duplicates after rebuild {len(totals) + 1}")
            totals.append(len(contents))
        # grows to the size of the file, then stops growing
        self.assertEqual(totals, [1000, 1200, 1200])

        before = [r.id for r in self.store.memory.transcript(task_id)]
        final = self.store.ingest_transcript(wire, session_id="s", cwd="/proj",
                                             rebuild=True)
        self.assertEqual(final["added"], 0)
        self.assertNotIn("rebuilt", final)
        self.assertNotIn("would_delete", final)
        self.assertEqual([r.id for r in self.store.memory.transcript(task_id)],
                         before)
        self.assertTrue(self.store.memory.check()["ok"])

    def test_rebuild_after_window_eviction_is_a_no_op(self):
        wire = self.tmp / "wire.jsonl"
        make_bulk_wire(wire, 1200)
        self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        for _ in range(3):  # converge
            self.store.ingest_transcript(wire, session_id="s", cwd="/proj",
                                         rebuild=True)
        before = [r.id for r in self.store.memory.transcript(1)]
        self.assertEqual(len(before), 1200)

        # The recorded digest window is full and covers fewer rows than the
        # file, so only the rows already stored for this path can keep the
        # rebuild from re-appending them.
        state = self.store.memory.fact(1, "autosave_state")["value"]
        self.assertEqual(len(state["hashes"]), HASH_CAP)
        self.assertLess(len(state["hashes"]), 1200)

        for _ in range(2):
            result = self.store.ingest_transcript(wire, session_id="s",
                                                  cwd="/proj", rebuild=True)
            self.assertEqual(result["status"], "clean")
            self.assertEqual(result["added"], 0)
            self.assertEqual([r.id for r in self.store.memory.transcript(1)],
                             before)


class ParserStampTests(AutosaveBase):
    """Cursor stamps: a parser-version mismatch is a rebuild, never a resume.

    The stamp is the guard against the trusted-cursor incident: a parser
    change leaves already-recorded cursors pointing past lines the previous
    version did not store, and every later pass resumed past the hole. A
    cursor stamped with another version — or with no version, which is what
    the pre-stamp cursors look like — must take the rebuild path: re-scan
    from line 0 against the digests already stored for the path, backfilling
    without duplicating.
    """

    def seed(self, *texts):
        wire = self.tmp / "wire.jsonl"
        prompt_wire(wire, *texts)
        return wire, self.store.ingest_transcript(wire, session_id="s",
                                                  cwd="/proj")

    def forge_cursor(self, cursor):
        """Replace the stored cursor with ``cursor``, keeping its path key."""
        store = Autosave(self.db)
        try:
            for task in store.memory.tasks():
                fact = store.memory.fact(task.id, STATE_KEY)
                if fact is None:
                    continue
                payload = fact["value"]
                key = next(iter(payload["cursors"]))
                payload["cursors"] = {key: cursor}
                store.memory.set_fact(task.id, STATE_KEY, payload,
                                      evidence_id=payload["last_evidence"])
                return key
        finally:
            store.close()
        self.fail("no autosave_state fact to rewrite")

    def stamped_cursor(self, task_id):
        state = self.store.memory.fact(task_id, STATE_KEY)["value"]
        return next(iter(state["cursors"].values()))

    def test_stale_version_cursor_rescans_and_backfills(self):
        """An old-version cursor pointing past a hole backfills it once."""
        wire, first = self.seed("first message", "second message")
        task_id = first["task_id"]
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(wire_line({"type": "turn.prompt", "promptId": "p9",
                                "input": [{"type": "text",
                                           "text": "the skipped message"}]})
                     + "\n")
        mtime, size, head = Autosave._file_marker(wire)
        # An old-parser cursor whose marker matches the file on disk: exactly
        # the trusted-looking cursor the stamp exists to distrust.
        self.forge_cursor({"line": 9999, "mtime": mtime, "size": size,
                           "head": head, "complete": True,
                           "parser": _PARSER_VERSION - 1})

        result = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")

        self.assertTrue(result["reset_cursor"])  # mismatch -> rebuild path
        self.assertEqual(result["added"], 1)
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertEqual(contents.count("the skipped message"), 1)
        self.assertEqual(len(contents), 3)
        self.assertEqual(self.stamped_cursor(task_id)["parser"],
                         _PARSER_VERSION)

        # Round-trip: the migrated cursor resumes normally afterwards.
        before = [(r.id, r.role, r.content)
                  for r in self.store.memory.transcript(task_id)]
        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["added"], 0)
        self.assertEqual([(r.id, r.role, r.content)
                          for r in self.store.memory.transcript(task_id)],
                         before)

    def test_pre_stamp_ahead_cursor_backfills_the_same_way(self):
        """A cursor with no parser key at all is migrated identically."""
        wire, first = self.seed("first message")
        task_id = first["task_id"]
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(wire_line({"type": "turn.prompt", "promptId": "p9",
                                "input": [{"type": "text",
                                           "text": "a later message"}]})
                     + "\n")
        mtime, size, head = Autosave._file_marker(wire)
        self.forge_cursor({"line": 9999, "mtime": mtime, "size": size,
                           "head": head, "complete": True})  # no "parser"

        result = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")

        self.assertTrue(result["reset_cursor"])
        self.assertEqual(result["added"], 1)
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertEqual(len(contents), 2)
        self.assertEqual(len(set(contents)), 2)
        self.assertEqual(self.stamped_cursor(task_id)["parser"],
                         _PARSER_VERSION)

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["added"], 0)

    def test_current_version_cursor_resumes_normally(self):
        """A cursor stamped with the running version is untouched by all this."""
        wire, first = self.seed("first message")
        task_id = first["task_id"]
        self.assertEqual(self.stamped_cursor(task_id)["parser"],
                         _PARSER_VERSION)

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertEqual(second["status"], "unchanged")
        self.assertFalse(second["reset_cursor"])

    def test_bulk_old_version_cursor_converges_over_passes(self):
        """A mismatch on a wire beyond the pass cap converges like a rebuild."""
        wire = self.tmp / "wire.jsonl"
        make_bulk_wire(wire, 1200)
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        task_id = first["task_id"]
        self.assertEqual(first["added"], 500)
        mtime, size, head = Autosave._file_marker(wire)
        self.forge_cursor({"line": 9999, "mtime": mtime, "size": size,
                           "head": head, "complete": True,
                           "parser": _PARSER_VERSION - 1})

        totals = []
        for index in range(3):
            result = self.store.ingest_transcript(wire, session_id="s",
                                                  cwd="/proj")
            if index == 0:
                self.assertTrue(result["reset_cursor"])
            else:
                self.assertFalse(result["reset_cursor"])
            contents = [r.content for r in self.store.memory.transcript(task_id)]
            self.assertEqual(len(contents), len(set(contents)),
                             "duplicates after version-migration re-scan")
            totals.append(len(contents))
        # grows to the size of the file, then stops growing
        self.assertEqual(totals, [1000, 1200, 1200])


class HealFixture(AutosaveBase):
    """Shared ``--heal`` fixtures: a seeded sessions root, a forged trusted
    cursor, and the in-process ``--heal`` entry point."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "sessions"
        self.wire = (self.root / "wd_proj_123456" / "session_aaa"
                     / "agents" / "main" / "wire.jsonl")

    def seed_wire(self, *texts):
        prompt_wire(self.wire, *texts)
        return self.store.ingest_session_dir(
            self.wire.parents[2], session_id="session_aaa", cwd="/proj")

    def forge_ahead_cursor(self):
        """Point the recorded cursor past every line, with a trusted marker.

        Mirrors the defect heal exists for: the parser skipped rows but still
        advanced its cursor, and the cursor's mtime/size/head all match the
        wire on disk, so no ordinary ingest will ever re-scan this wire. The
        cursor carries the current parser stamp, because the defect is a
        *trusted* cursor written by the parser that is running now.
        """
        mtime, size, head = Autosave._file_marker(self.wire)
        store = Autosave(self.db)
        try:
            for task in store.memory.tasks():
                fact = store.memory.fact(task.id, STATE_KEY)
                if fact is None:
                    continue
                payload = fact["value"]
                key = next(iter(payload["cursors"]))
                payload["cursors"][key] = {"line": 9999, "mtime": mtime,
                                           "size": size, "head": head,
                                           "complete": True,
                                           "parser": _PARSER_VERSION}
                store.memory.set_fact(task.id, STATE_KEY, payload,
                                      evidence_id=payload["last_evidence"])
                return key
        finally:
            store.close()
        self.fail("no autosave_state fact to rewrite")

    def run_heal(self, *extra):
        module = hook_module()
        argv = ["--heal", "--db", self.db, "--sessions-root", str(self.root),
                *extra]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = module.main(argv)
        return code, out.getvalue()


class HealTests(HealFixture):
    """``--heal``: re-scan every recorded wire from line 0 to close the holes
    a cursor skipped, which no ordinary ingest can reach (the wire looks
    untouched, so the cursor is trusted and resumed from)."""

    def test_cursor_ahead_of_stored_heals_the_hole(self):
        seeded = self.seed_wire("first message", "second message")
        task_id = seeded[0]["task_id"]
        with self.wire.open("a", encoding="utf-8") as fh:
            fh.write(wire_line({"type": "turn.prompt", "promptId": "p9",
                                "input": [{"type": "text",
                                           "text": "the skipped message"}]})
                     + "\n")
        self.forge_ahead_cursor()
        # the forged cursor is trusted, so an ordinary ingest skips the wire
        self.assertEqual(
            self.store.ingest_transcript(self.wire, session_id="session_aaa",
                                         cwd="/proj")["status"], "unchanged")

        code, _out = self.run_heal()

        self.assertEqual(code, 0)
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertIn("the skipped message", contents)
        self.assertEqual(contents.count("the skipped message"), 1)
        self.assertEqual(len(contents), len(set(contents)))
        self.assertEqual(len(contents), 3)

    def test_second_heal_adds_nothing(self):
        seeded = self.seed_wire("first message", "second message")
        task_id = seeded[0]["task_id"]
        prompt_wire(self.wire, "an appended message")
        self.forge_ahead_cursor()
        code, _out = self.run_heal()
        self.assertEqual(code, 0)
        before = [(r.id, r.role, r.content)
                  for r in self.store.memory.transcript(task_id)]

        code, out = self.run_heal("--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)["healed"]
        self.assertEqual([r["added"] for r in rows], [0])
        self.assertEqual([r["status"] for r in rows], ["clean"])
        self.assertEqual([(r.id, r.role, r.content)
                          for r in self.store.memory.transcript(task_id)],
                         before)

    def test_heal_converges_past_the_pass_cap(self):
        seeded = self.seed_wire("first message")
        task_id = seeded[0]["task_id"]
        make_bulk_wire(self.wire, 1200)
        self.forge_ahead_cursor()

        code, out = self.run_heal("--json")

        self.assertEqual(code, 0)
        row, = json.loads(out)["healed"]
        self.assertEqual(row["status"], "clean")
        self.assertGreaterEqual(row["passes"], 3)
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertEqual(len(contents), 1201)
        self.assertEqual(len(contents), len(set(contents)))

    def test_missing_wire_file_is_unresolved_not_fatal(self):
        self.seed_wire("first message")
        self.wire.unlink()

        code, out = self.run_heal("--json")

        self.assertEqual(code, 0)
        row, = json.loads(out)["healed"]
        self.assertEqual(row["status"], "unresolved")
        self.assertIsNone(row["path"])

    def test_dry_run_heal_changes_no_bytes(self):
        self.seed_wire("first message", "second message")
        prompt_wire(self.wire, "an un-ingested message")
        self.forge_ahead_cursor()

        def checksums():
            out = {}
            for suffix in ("", "-wal"):
                candidate = Path(self.db + suffix)
                out[suffix or "db"] = (
                    hashlib.sha1(candidate.read_bytes()).hexdigest()
                    if candidate.is_file() else None)
            return out

        before = checksums()
        code, out = self.run_heal("--dry-run", "--json")

        self.assertEqual(code, 0)
        row, = json.loads(out)["healed"]
        self.assertEqual(row["status"], "would_ingest")
        self.assertGreater(row["added"], 0)
        self.assertEqual(checksums(), before)

    def test_heal_below_threshold_does_not_optimize(self):
        self.seed_wire("first message", "second message")
        prompt_wire(self.wire, "an un-ingested message")
        self.forge_ahead_cursor()

        with mock.patch.object(Autosave, "optimize_index") as optimize:
            code, _out = self.run_heal()

        self.assertEqual(code, 0)
        optimize.assert_not_called()

    def test_heal_above_threshold_optimizes_once(self):
        module = hook_module()
        self.seed_wire("first message")
        make_bulk_wire(self.wire, 1200)
        self.forge_ahead_cursor()

        with mock.patch.object(Autosave, "optimize_index",
                               return_value=True) as optimize:
            code, out = self.run_heal("--json")

        self.assertEqual(code, 0)
        optimize.assert_called_once_with()
        report = json.loads(out)
        self.assertTrue(report["index_optimized"])
        row, = report["healed"]
        self.assertGreaterEqual(row["added"], module.HEAL_OPTIMIZE_THRESHOLD)

    def test_dry_run_heal_above_threshold_does_not_optimize(self):
        self.seed_wire("first message")
        make_bulk_wire(self.wire, 1200)
        self.forge_ahead_cursor()

        with mock.patch.object(Autosave, "optimize_index") as optimize:
            code, out = self.run_heal("--dry-run", "--json")

        self.assertEqual(code, 0)
        optimize.assert_not_called()
        report = json.loads(out)
        self.assertFalse(report["index_optimized"])
        row, = report["healed"]
        self.assertEqual(row["status"], "would_ingest")
        self.assertGreater(row["added"], 0)

    def test_optimize_index_runs_cleanly_and_is_idempotent(self):
        seeded = self.seed_wire("first message", "second message")
        task_id = seeded[0]["task_id"]

        self.assertTrue(self.store.optimize_index())
        self.assertTrue(self.store.optimize_index())

        # every stored message is still searchable through the merged index
        stored = {r.id for r in self.store.memory.transcript(task_id)}
        indexed = {r[0] for r in self.store.memory._db.execute(
            "SELECT rowid FROM evidence_index WHERE evidence_index MATCH ?",
            ("message",)).fetchall()}
        self.assertEqual(indexed, stored)


def _busy_timeout_ms() -> int:
    """Effective SQLite busy timeout on a fresh TaskMemory connection."""
    from cmpath.memory import TaskMemory
    memory = TaskMemory(":memory:")
    try:
        return int(memory._db.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        memory.close()


_BUSY_TIMEOUT_MS = _busy_timeout_ms()
_HOLD_SECONDS = 6.5
"""Lock hold used by these tests. The fork's TaskMemory raises the busy
timeout to a 10 s floor, comfortably above this hold; the public TaskMemory
gains the same pragma in a parallel port, so until that merge these tests
self-skip on whatever busy timeout is actually in force rather than
deterministically failing against the plain 5 s connection default."""


@unittest.skipIf(_BUSY_TIMEOUT_MS < int(_HOLD_SECONDS * 1000),
                 f"TaskMemory busy timeout ({_BUSY_TIMEOUT_MS} ms) does not "
                 f"cover a {_HOLD_SECONDS}s held write lock")
class ConcurrencyTests(HealFixture):
    """A hook ``save_turn`` colliding with the ``--heal`` sweep waits, never fails.

    Under WAL readers never block, but the heal's ingest batches and a
    concurrently firing hook's ``save_turn`` are two WRITERS on one database
    file, and without a busy timeout the second writer fails immediately with
    sqlite3.OperationalError "database is locked". The hook is fail-open, so
    that collision silently loses a turn's evidence; the heal would report an
    'error' row instead. The busy timeout on the TaskMemory connection makes
    the collided writer wait; these tests self-skip when the timeout actually
    in force does not cover the lock hold below. SQLite connections are not
    thread-safe by default, so every concurrent party here opens its own
    ``Autosave`` (and therefore its own ``TaskMemory`` connection), exactly as
    the heal process and the hook process do in production.
    """

    def test_save_turn_waits_out_a_held_write_lock(self):
        """A write that collides with a lock held past the old 5 s default lands."""
        self.seed_wire("first message")
        holder = sqlite3.connect(self.db, timeout=0, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        outcome = queue.Queue()

        def writer():
            store = Autosave(self.db)
            try:
                outcome.put(store.save_turn("s_lock", "/proj",
                                            "waited out the lock", turn_id="0"))
            except Exception as exc:  # a locked failure would surface here
                outcome.put(exc)
            finally:
                store.close()

        started = time.monotonic()
        thread = threading.Thread(target=writer)
        thread.start()
        # Held past the 5 s horizon sqlite3.connect used to install, so the
        # save only survives if the busy-timeout pragma is actually in force.
        time.sleep(6.5)
        holder.execute("ROLLBACK")
        holder.close()
        thread.join(30)
        result = outcome.get_nowait()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "saved")
        self.assertGreaterEqual(time.monotonic() - started, 6.5)
        contents = [r.content for r in self.store.memory.transcript(1)]
        self.assertEqual(contents.count("waited out the lock"), 1)

    def test_heal_and_hook_save_turn_under_contention(self):
        """A full heal sweep and concurrent hook saves lose nothing."""
        seeded = self.seed_wire("first message", "second message")
        task_id = seeded[0]["task_id"]
        # Above the 500-row pass cap: the heal needs several passes and takes
        # real time, so the writer below lands inside the heal's write window.
        make_bulk_wire(self.wire, 1200)
        self.forge_ahead_cursor()
        seed_count = len(self.store.memory.transcript(task_id))
        turns = 30

        outcome = queue.Queue()

        def writer():
            store = Autosave(self.db)
            try:
                for i in range(turns):
                    outcome.put(store.save_turn(
                        "session_hook", "/proj", f"hook turn {i:03d}",
                        turn_id=i))
                    time.sleep(0.02)
            except Exception as exc:  # a locked failure would surface here
                outcome.put(exc)
            finally:
                store.close()

        thread = threading.Thread(target=writer)
        thread.start()
        code, out = self.run_heal("--json")
        thread.join(60)
        self.assertEqual(code, 0)

        results = []
        while not outcome.empty():
            results.append(outcome.get_nowait())
        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(failures, [])
        saved = [r for r in results if r["status"] == "saved"]
        self.assertEqual(len(saved), turns)
        writer_ids = {r["citation"].split(":")[1][1:] for r in saved}
        self.assertEqual(len(writer_ids), turns)

        # The heal report: no 'error' rows, and the bulk wire fully healed.
        rows = json.loads(out)["healed"]
        self.assertTrue(all(r["status"] == "clean" for r in rows), rows)
        healed_added = sum(r["added"] for r in rows)
        self.assertEqual(healed_added, 1200)

        # Nothing lost, nothing duplicated: every writer save landed exactly
        # once, and the stored total is seed + healed + writer saves.
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertEqual(len(contents), seed_count + healed_added + turns)
        self.assertEqual(len(contents), len(set(contents)))
        for i in range(turns):
            self.assertEqual(contents.count(f"hook turn {i:03d}"), 1)
        stored_ids = {str(r.id) for r in self.store.memory.transcript(task_id)}
        self.assertTrue(writer_ids <= stored_ids)


class RewriteDetectionTests(AutosaveBase):
    """The append-versus-reset decision for a file that did not grow.

    A transcript is appended past its cursor only when it genuinely grew. The
    size is the cheap signal, but it cannot tell a same-size rewrite from a pass
    that merely stopped at the row cap, so the mtime has to be consulted too.
    These fixtures keep byte 512 fixed to make the head marker useless as a
    tiebreaker, which is what the old size comparison got wrong.
    """

    def test_same_size_rewrite_with_new_mtime_resets(self):
        wire = self.tmp / "wire.jsonl"
        make_padded_wire(wire, "alpha body")
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        task_id = first["task_id"]
        self.assertEqual(first["added"], 3)  # prompt + alpha + tail
        self.assertEqual(first["scanned_to"], 4)
        size_before = wire.stat().st_size

        # Same byte length, same 512-byte head: only the tail changes.
        make_padded_wire(wire, "gamma body")
        self.assertEqual(wire.stat().st_size, size_before)
        stamp = time.time() + 5
        os.utime(wire, (stamp, stamp))

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertTrue(second["reset_cursor"])
        self.assertEqual(second["added"], 1)
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertIn("gamma body", contents)
        self.assertEqual(contents.count("alpha body"), 1)
        self.assertEqual(len(contents), len(set(contents)))
        self.assertEqual(len(contents), 4)

    def test_rewrite_beyond_the_digest_window_does_not_duplicate(self):
        wire = self.tmp / "wire.jsonl"
        make_bulk_wire(wire, 1200)
        self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        for _ in range(3):  # converge to the full file
            self.store.ingest_transcript(wire, session_id="s", cwd="/proj",
                                         rebuild=True)
        self.assertEqual(len(self.store.memory.transcript(1)), 1200)

        # A rewrite past byte 512 and outside the retained digest window: the
        # hashes fact has forgotten it, so only the stored per-path rows can
        # stop the reset pass from re-appending the first 200 messages.
        text = wire.read_text(encoding="utf-8")
        size_before = wire.stat().st_size
        text = text.replace("message number 0049 body", "message number 0049 XXXX")
        wire.write_text(text, encoding="utf-8")
        self.assertEqual(wire.stat().st_size, size_before)
        stamp = time.time() + 5
        os.utime(wire, (stamp, stamp))

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertTrue(second["reset_cursor"])
        self.assertEqual(second["added"], 1)
        contents = [r.content for r in self.store.memory.transcript(1)]
        self.assertEqual(len(contents), 1201)
        self.assertEqual(len(contents), len(set(contents)))
        self.assertIn("message number 0049 XXXX", "\n".join(contents))

    def test_strictly_longer_file_with_unchanged_head_is_an_append(self):
        wire = self.tmp / "wire.jsonl"
        make_padded_wire(wire, "alpha body")
        first = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        task_id = first["task_id"]
        self.assertEqual(first["added"], 3)

        with wire.open("a", encoding="utf-8") as fh:
            fh.write(wire_line(
                {"type": "turn.prompt", "agentId": "main", "promptId": "p2",
                 "input": [{"type": "text", "text": "a later ask"}]}) + "\n")
        stamp = time.time() + 5
        os.utime(wire, (stamp, stamp))

        second = self.store.ingest_transcript(wire, session_id="s", cwd="/proj")
        self.assertFalse(second["reset_cursor"])
        self.assertEqual(second["added"], 1)
        contents = [r.content for r in self.store.memory.transcript(task_id)]
        self.assertIn("a later ask", contents)
        self.assertEqual(len(contents), len(set(contents)))
        self.assertEqual(len(contents), 4)


class ToolTrafficTests(AutosaveBase):
    """`tool.call` / `tool.result` capture: rendering, grouping and the cap."""

    def write_wire(self, *inner_events) -> Path:
        path = self.tmp / "wire.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(wire_line({"type": "metadata", "protocol_version": "1.5"}) + "\n")
            fh.write(wire_line({"type": "turn.prompt", "agentId": "main",
                                "promptId": "p1",
                                "input": [{"type": "text", "text": "run it"}]}) + "\n")
            for inner in inner_events:
                fh.write(wire_line({"type": "context.append_loop_event",
                                    "event": inner}) + "\n")
        return path

    def ingest(self, *inner_events):
        path = self.write_wire(*inner_events)
        result = self.store.ingest_transcript(path, session_id="s", cwd="/proj")
        return result, self.store.memory.transcript(result["task_id"])

    def tools(self, rows):
        return [r for r in rows if r.role == "tool"]

    def test_call_renders_detail_key_then_remaining_args(self):
        _result, rows = self.ingest(
            {"type": "tool.call", "name": "Bash", "toolCallId": "c1",
             "args": {"command": "ls -la", "timeout": 30,
                      "description": "list the tree"}})
        tool = self.tools(rows)[0]
        self.assertEqual(tool.content, "\n".join([
            "tool call: Bash",
            "toolCallId: c1",
            "command: ls -la",
            'args: {"timeout": 30, "description": "list the tree"}',
        ]))
        self.assertEqual(tool.source["line"], 3)
        self.assertEqual(tool.source["kind"], "wire")

    def test_call_detail_key_covers_read_path(self):
        _result, rows = self.ingest(
            {"type": "tool.call", "name": "Read", "toolCallId": "c2",
             "args": {"path": "/a/b.py"}})
        self.assertEqual(self.tools(rows)[0].content, "\n".join([
            "tool call: Read", "toolCallId: c2", "path: /a/b.py"]))

    def test_call_and_result_share_one_row_keyed_by_tool_call_id(self):
        result, rows = self.ingest(
            {"type": "tool.call", "name": "Read", "toolCallId": "c3",
             "args": {"path": "/a/b.py"}},
            {"type": "tool.result", "toolCallId": "c3",
             "result": {"output": "line one\nline two"}})
        self.assertEqual(result["added"], 2)  # prompt + one merged tool row
        tool = self.tools(rows)[0]
        self.assertEqual(tool.source["line"], "3-4")
        self.assertEqual(tool.content, "\n".join([
            "tool call: Read",
            "toolCallId: c3",
            "path: /a/b.py",
            "",
            "tool result",
            "toolCallId: c3",
            "output:",
            "line one",
            "line two",
        ]))

    def test_error_result_and_note_are_flagged(self):
        _result, rows = self.ingest(
            {"type": "tool.call", "name": "Bash", "toolCallId": "c4",
             "args": {"command": "false"}},
            {"type": "tool.result", "toolCallId": "c4", "note": "exit 1",
             "result": {"isError": True, "output": "boom"}})
        self.assertIn("tool result: error", self.tools(rows)[0].content)
        self.assertIn("note: exit 1", self.tools(rows)[0].content)
        self.assertIn("output:\nboom", self.tools(rows)[0].content)

    def test_non_string_output_falls_back_to_json(self):
        _result, rows = self.ingest(
            {"type": "tool.result", "toolCallId": "c5",
             "result": {"output": {"lines": 3}}})
        self.assertEqual(self.tools(rows)[0].content, "\n".join([
            "tool result", "toolCallId: c5", 'output: {"lines": 3}']))

    def test_tool_rows_use_the_tool_cap_and_flag_truncation(self):
        _result, rows = self.ingest(
            {"type": "tool.call", "name": "Bash", "toolCallId": "c6",
             "args": {"command": "cat big"}},
            {"type": "tool.result", "toolCallId": "c6",
             "result": {"output": "y" * (TOOL_MAX_CHARS + 500)}})
        tool = self.tools(rows)[0]
        self.assertEqual(len(tool.content), TOOL_MAX_CHARS)
        self.assertTrue(tool.source.get("truncated"))

    def test_assistant_rows_use_the_message_cap(self):
        result, rows = self.ingest(
            {"type": "content.part", "turnId": "0", "stepUuid": "uu-1",
             "part": {"type": "text", "text": "x" * (MAX_MESSAGE_CHARS + 50)}})
        assistant = [r for r in rows if r.role == "assistant"][0]
        self.assertEqual(len(assistant.content), MAX_MESSAGE_CHARS)
        self.assertTrue(assistant.source.get("truncated"))
        self.assertEqual(result["added"], 2)

    def test_display_field_is_not_rendered(self):
        _result, rows = self.ingest(
            {"type": "tool.call", "name": "Bash", "toolCallId": "c7",
             "display": "SHOULD NOT APPEAR", "args": {"command": "ls"}})
        self.assertEqual(self.tools(rows)[0].content,
                         "tool call: Bash\ntoolCallId: c7\ncommand: ls")

    def test_display_field_on_a_result_is_not_rendered(self):
        _result, rows = self.ingest(
            {"type": "tool.result", "toolCallId": "c8",
             "display": "SHOULD NOT APPEAR",
             "result": {"output": "ok"}})
        content = self.tools(rows)[0].content
        self.assertNotIn("SHOULD NOT APPEAR", content)
        self.assertIn("output:\nok", content)

    def test_unkeyed_calls_do_not_merge(self):
        _result, rows = self.ingest(
            {"type": "tool.call", "name": "Bash", "args": {"command": "one"}},
            {"type": "tool.call", "name": "Bash", "args": {"command": "two"}})
        tools = self.tools(rows)
        self.assertEqual([t.content for t in tools],
                         ["tool call: Bash\ncommand: one",
                          "tool call: Bash\ncommand: two"])
        self.assertEqual([t.source["line"] for t in tools], [3, 4])

    def test_iter_wire_events_exposes_the_tool_group_key(self):
        path = self.write_wire(
            {"type": "tool.call", "name": "Bash", "toolCallId": "c9",
             "args": {"command": "ls"}})
        keys = [(role, key) for _lineno, role, _text, key in iter_wire_events(path)]
        self.assertIn(("tool", ("tool", "c9")), keys)

    def test_flattened_tool_event_is_read_defensively(self):
        path = self.tmp / "wire.jsonl"
        path.write_text(wire_line(
            {"type": "tool.call", "name": "Bash", "toolCallId": "c10",
             "args": {"command": "ls"}}) + "\n", encoding="utf-8")
        lines = list(iter_wire_messages(path))
        self.assertEqual(lines, [(1, "tool", "tool call: Bash\ntoolCallId: c10\n"
                                             "command: ls\n\n")])


class SessionDirTests(AutosaveBase):
    def test_project_name_from_session_path(self):
        p = Path("/home/x/.kimi-code/sessions/wd_rocket_35f48ac79de6/"
                 "session_abcd/agents/main/wire.jsonl")
        self.assertEqual(project_name_from_path(p), "rocket")

    def test_locate_session_dir_matches_bare_uuid(self):
        root = self.tmp / "sessions"
        target = root / "wd_proj_123456" / "session_48540d9c-3d32"
        target.mkdir(parents=True)
        (root / "wd_other_123456" / "session_9999").mkdir(parents=True)
        self.assertEqual(locate_session_dir("48540d9c-3d32", root=root), target)
        self.assertIsNone(locate_session_dir("nope", root=root))

    def test_ingest_session_dir_covers_main_and_subagents(self):
        root = self.tmp / "sessions" / "wd_proj_123456" / "session_a"
        (root / "agents" / "main").mkdir(parents=True)
        (root / "agents" / "agent-1").mkdir(parents=True)
        make_wire(root / "agents" / "main" / "wire.jsonl")
        make_wire(root / "agents" / "agent-1" / "wire.jsonl", label="(sub)")
        results = self.store.ingest_session_dir(root, session_id="s", cwd="/proj")
        self.assertEqual(len(results), 2)
        # 2 paths x (2 prompts + assistant + tool) = 8, minus 1: both wires
        # render the identical tool text, and the shared hash list dedups it
        self.assertEqual(sum(r["added"] for r in results), 7)


class HookScriptTests(AutosaveBase):
    def payload(self, **overrides):
        payload = {"session_id": "48540d9c", "cwd": "/mnt/hdd/rocket",
                   "turn_id": "0", "origin_kind": "user", "origin_name": "",
                   "prompt": "please refactor the parser"}
        payload.update(overrides)
        return json.dumps(payload)

    def run_hook(self, stdin_text, db, *extra):
        return subprocess.run(
            [sys.executable, HOOK, "--db", db, *extra],
            input=stdin_text, capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": SRC}, timeout=60)

    def test_end_to_end_saves_prompt_once(self):
        db = str(self.tmp / "hook.db")
        first = self.run_hook(self.payload(), db)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, "")
        second = self.run_hook(self.payload(), db)
        self.assertEqual(second.returncode, 0)
        store = Autosave(db)
        self.addCleanup(store.close)
        rows = store.memory.transcript(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "please refactor the parser")
        self.assertEqual(rows[0].source["turn"], "0")

    def test_second_pass_with_nothing_new_ingests_nothing(self):
        """Events share one idempotent pass: a repeat over a still wire is a no-op."""
        db = str(self.tmp / "idem.db")
        session_dir = self.tmp / "sessions" / "wd_proj_123456" / "session_aaa"
        (session_dir / "agents" / "main").mkdir(parents=True, exist_ok=True)
        make_wire(session_dir / "agents" / "main" / "wire.jsonl")

        def snapshot():
            conn = sqlite3.connect(db)
            try:
                rows = conn.execute(
                    "SELECT task_id, role, content, source FROM messages"
                    " ORDER BY id").fetchall()
                state = conn.execute(
                    "SELECT value FROM facts WHERE key = ?", (STATE_KEY,)).fetchall()
            finally:
                conn.close()
            return rows, state

        args = (db, "--session-dir", str(session_dir))
        first = self.run_hook(self.payload(session_id="session_aaa"), *args)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = snapshot()
        self.assertTrue(before[0])       # the first pass stored something
        self.assertTrue(before[1])       # ... and wrote the cursor state

        second = self.run_hook(self.payload(session_id="session_aaa"), *args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")
        self.assertEqual(snapshot(), before)

    def test_garbage_stdin_exits_zero_and_writes_nothing(self):
        db = str(self.tmp / "hook2.db")
        for bad in ("", "not json", "[1, 2, 3]", '{"prompt":'):
            result = self.run_hook(bad, db)
            self.assertEqual(result.returncode, 0, bad)
        self.assertFalse(os.path.exists(db))

    def test_missing_module_exits_zero(self):
        result = subprocess.run(
            [sys.executable, HOOK, "--db", str(self.tmp / "x.db")],
            input=self.payload(), capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(self.tmp / "nowhere")}, timeout=60)
        self.assertEqual(result.returncode, 0)

    def test_dry_run_reports_without_writing(self):
        db = str(self.tmp / "dryhook.db")
        result = self.run_hook(self.payload(), db, "--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("would_save", result.stdout)
        self.assertFalse(os.path.exists(db))

    def test_selftest_passes(self):
        result = subprocess.run([sys.executable, HOOK, "--selftest"],
                                capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": SRC}, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SELFTEST PASS", result.stdout)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.config = self.tmp / "config.toml"
        self.config.write_text(
            '[[permission.rules]]\naction = "allow"\n\n[[hooks]]\n'
            'event = "PreToolUse"\ncommand = "python3 /x/audit-bash.py"\n',
            encoding="utf-8")
        self.script = str(self.tmp / "autosave_session.py")

    def try_toml(self, text):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib requires Python 3.11+")
        return tomllib.loads(text)

    def test_print_outputs_valid_toml_block_only(self):
        result = subprocess.run([sys.executable, INSTALLER, "--print",
                                 "--script", self.script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn('event = "SessionEnd"', result.stdout)
        self.assertIn("autosave_session.py", result.stdout)
        parsed = self.try_toml(result.stdout)
        events = [h["event"] for h in parsed["hooks"]]
        self.assertEqual(events, ["TurnStarted", "Stop", "SessionEnd"])
        self.assertTrue(all(h["timeout"] == 10 for h in parsed["hooks"]))
        self.assertTrue(all(h["command"] == f"python3 {self.script}"
                            for h in parsed["hooks"]))
        self.assertNotIn("permission", result.stdout)

    def test_install_appends_block_and_parses(self):
        result = subprocess.run([sys.executable, INSTALLER, "--config", str(self.config),
                                 "--script", self.script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = self.try_toml(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed["hooks"]), 4)
        managed = [h for h in parsed["hooks"]
                   if h["command"] == f"python3 {self.script}"]
        self.assertEqual([h["event"] for h in managed],
                         ["TurnStarted", "Stop", "SessionEnd"])
        self.assertEqual([h["timeout"] for h in managed], [10, 10, 10])
        self.assertEqual(parsed["permission"]["rules"][0]["action"], "allow")

    def test_install_refuses_to_double_install(self):
        common = [sys.executable, INSTALLER, "--config", str(self.config),
                  "--script", self.script]
        subprocess.run(common, capture_output=True, text=True, timeout=60)
        again = subprocess.run(common, capture_output=True, text=True, timeout=60)
        self.assertEqual(again.returncode, 1)
        parsed = self.try_toml(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed["hooks"]), 4)

    def test_install_refuses_legacy_turn_started_only_block(self):
        """A legacy single-event install is detected and refused (upgrade path)."""
        self.config.write_text(
            self.config.read_text(encoding="utf-8")
            + f"# >>> cmpath-autosave managed block >>>\n[[hooks]]\n"
              f'event = "TurnStarted"\ncommand = "python3 {self.script}"\n'
              f"timeout = 10\n# <<< cmpath-autosave managed block <<<\n",
            encoding="utf-8")
        result = subprocess.run([sys.executable, INSTALLER, "--config", str(self.config),
                                 "--script", self.script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        parsed = self.try_toml(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed["hooks"]), 2)

    def test_install_creates_backup(self):
        subprocess.run([sys.executable, INSTALLER, "--config", str(self.config),
                        "--script", self.script], capture_output=True, timeout=60)
        backups = list(self.tmp.glob("config.toml.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertIn("audit-bash", backups[0].read_text(encoding="utf-8"))

    def test_uninstall_removes_managed_block(self):
        common = [sys.executable, INSTALLER, "--config", str(self.config),
                  "--script", self.script]
        subprocess.run(common, capture_output=True, timeout=60)
        result = subprocess.run(common + ["--uninstall"], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = self.try_toml(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed["hooks"]), 1)
        self.assertEqual(parsed["hooks"][0]["event"], "PreToolUse")
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("autosave_session.py", text)
        self.assertNotIn("cmpath-autosave managed block", text)

    def add_marker_less_legacy_hook(self):
        """Append the hand-added, marker-less hook the live config carries."""
        self.config.write_text(
            self.config.read_text(encoding="utf-8")
            + "\n[[hooks]]\n"
              'event = "TurnStarted"\n'
              f'command = "python3 {self.script}"\n'
              "timeout = 10\n",
            encoding="utf-8")

    def test_install_replaces_marker_less_legacy_block(self):
        """A hand-added marker-less hook is replaced, not double-installed."""
        self.add_marker_less_legacy_hook()
        result = subprocess.run([sys.executable, INSTALLER, "--config", str(self.config),
                                 "--script", self.script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.config.read_text(encoding="utf-8")
        parsed = self.try_toml(text)
        self.assertEqual([h["event"] for h in parsed["hooks"]],
                         ["PreToolUse", "TurnStarted", "Stop", "SessionEnd"])
        managed = [h for h in parsed["hooks"]
                   if h["command"] == f"python3 {self.script}"]
        self.assertEqual([h["event"] for h in managed],
                         ["TurnStarted", "Stop", "SessionEnd"])
        self.assertEqual([h["timeout"] for h in managed], [10, 10, 10])
        self.assertEqual([h["event"] for h in managed].count("TurnStarted"), 1)
        self.assertEqual(text.count("autosave_session.py"), 3)

    def test_uninstall_removes_marker_less_legacy_block(self):
        """A hand-added marker-less hook is removable without markers."""
        self.add_marker_less_legacy_hook()
        result = subprocess.run([sys.executable, INSTALLER, "--config", str(self.config),
                                 "--uninstall"], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("autosave_session.py", text)
        parsed = self.try_toml(text)
        self.assertEqual([h["event"] for h in parsed["hooks"]], ["PreToolUse"])

    def test_uninstall_removes_marked_and_marker_less_blocks(self):
        """Uninstall strips the marked region and any marker-less section."""
        self.config.write_text(
            self.config.read_text(encoding="utf-8")
            + f"# >>> cmpath-autosave managed block >>>\n[[hooks]]\n"
              f'event = "TurnStarted"\ncommand = "python3 {self.script}"\n'
              f"timeout = 10\n# <<< cmpath-autosave managed block <<<\n",
            encoding="utf-8")
        self.add_marker_less_legacy_hook()
        result = subprocess.run([sys.executable, INSTALLER, "--config", str(self.config),
                                 "--uninstall"], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("autosave_session.py", text)
        self.assertNotIn("cmpath-autosave managed block", text)
        parsed = self.try_toml(text)
        self.assertEqual([h["event"] for h in parsed["hooks"]], ["PreToolUse"])


@functools.lru_cache(maxsize=1)
def hook_module():
    """Import the autosave CLI module in-process for unit-level checks."""
    import cmpath.autosave_cli as module
    return module


HOOK_CONFIG = """\
[[hooks]]
event = "TurnStarted"
command = "python3 {hook}"
timeout = 10
"""

MULTI_HOOK_CONFIG = """\
[[hooks]]
event = "TurnStarted"
command = "python3 {hook}"
timeout = 10

[[hooks]]
event = "Stop"
command = "python3 {hook}"
timeout = 10

[[hooks]]
event = "SessionEnd"
command = "python3 {hook}"
timeout = 10
"""


def prompt_wire(path: Path, *texts: str) -> None:
    """Append one ``turn.prompt`` event per text, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for index, text in enumerate(texts):
            fh.write(json.dumps({
                "type": "turn.prompt", "promptId": f"p{index}",
                "input": [{"type": "text", "text": text}]}) + "\n")


class DoctorHookDetectionTests(unittest.TestCase):
    """detect_hook is host-independent: every config is a temp file."""

    def test_detects_turn_started_hook(self):
        text = HOOK_CONFIG.format(hook=HOOK)
        found = hook_module().detect_hook(text, Path(HOOK).resolve())
        self.assertTrue(found["installed"])
        self.assertEqual(found["command"], f"python3 {HOOK}")
        self.assertEqual(found["timeout"], 10)
        self.assertTrue(found["timeout_sane"])
        self.assertEqual(found["parse"], "toml")
        self.assertEqual(found["events"], {"TurnStarted": True, "Stop": False,
                                           "SessionEnd": False})
        self.assertEqual(found["missing_events"], ["Stop", "SessionEnd"])

    def test_detects_all_managed_events_when_present(self):
        text = MULTI_HOOK_CONFIG.format(hook=HOOK)
        found = hook_module().detect_hook(text, Path(HOOK).resolve())
        self.assertTrue(found["installed"])
        self.assertEqual(found["events"], {"TurnStarted": True, "Stop": True,
                                           "SessionEnd": True})
        self.assertEqual(found["missing_events"], [])
        self.assertIn("Stop", found["note"])
        self.assertIn("SessionEnd", found["note"])

    def test_installed_when_stop_replaces_turn_started(self):
        """Any managed event is enough; TurnStarted is only the usual primary."""
        text = ('[[hooks]]\nevent = "Stop"\n'
                f'command = "python3 {HOOK}"\ntimeout = 10\n')
        found = hook_module().detect_hook(text, Path(HOOK).resolve())
        self.assertTrue(found["installed"])
        self.assertEqual(found["events"]["TurnStarted"], False)
        self.assertEqual(found["events"]["Stop"], True)
        self.assertEqual(found["missing_events"], ["TurnStarted", "SessionEnd"])

    def test_absent_when_only_other_events_hook_this_script(self):
        text = ('[[hooks]]\nevent = "PreToolUse"\n'
                f'command = "python3 {HOOK}"\ntimeout = 10\n')
        found = hook_module().detect_hook(text, Path(HOOK).resolve())
        self.assertFalse(found["installed"])
        self.assertIn("TurnStarted", found["note"])

    def test_absent_when_command_points_elsewhere(self):
        text = ('[[hooks]]\nevent = "TurnStarted"\n'
                'command = "python3 /opt/other/autosave.py"\ntimeout = 10\n')
        found = hook_module().detect_hook(text, Path(HOOK).resolve())
        self.assertFalse(found["installed"])

    def test_unsane_timeout_is_reported(self):
        text = ('[[hooks]]\nevent = "TurnStarted"\n'
                f'command = "python3 {HOOK}"\ntimeout = 2\n')
        found = hook_module().detect_hook(text, Path(HOOK).resolve())
        self.assertTrue(found["installed"])
        self.assertFalse(found["timeout_sane"])

    def test_regex_fallback_without_tomllib(self):
        text = HOOK_CONFIG.format(hook=HOOK)
        found = hook_module().detect_hook(text, Path(HOOK).resolve(),
                                          toml_available=False)
        self.assertTrue(found["installed"], found)
        self.assertEqual(found["parse"], "text")
        self.assertTrue(found["timeout_sane"])


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = str(self.tmp / "kimi.db")
        self.config = self.tmp / "config.toml"
        self.config.write_text(HOOK_CONFIG.format(hook=HOOK), encoding="utf-8")
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir()

    def seed(self, cwd="/tmp/proj", prompt="hello doctor", **kwargs):
        store = Autosave(self.db)
        try:
            return store.save_turn("sess-1", cwd, prompt, **kwargs)
        finally:
            store.close()

    def run_doctor(self, *extra, db=None, max_age=None):
        cmd = [sys.executable, HOOK, "--doctor", "--db", db or self.db,
               "--config", str(self.config),
               "--sessions-root", str(self.sessions), *extra]
        if max_age is not None:
            cmd += ["--max-age", str(max_age)]
        return subprocess.run(cmd, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": SRC}, timeout=60)

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def make_task(self, title, aliases=(), messages=()):
        """Create a task by hand, as a human could via the CLI.

        Aliases go through the same normalization ``create_task`` applies
        (``auto:slug`` is stored as ``auto slug``), so this reproduces both
        the hook's stored shape and any human-forged half of it. Each entry
        in ``messages`` is a ``(role, text)`` pair inserted directly, with no
        ``autosave_state`` fact — exactly the shape the doctor's missing-fact
        rule used to red-light forever.
        """
        store = Autosave(self.db)
        try:
            task = store.memory.create_task(title, aliases=aliases)
        finally:
            store.close()
        for role, text in messages:
            self.raw(
                "INSERT INTO messages(task_id, role, content, source,"
                " created_at) VALUES(?,?,?,?,?)",
                (task.id, role, text,
                 json.dumps({"kind": "prompt", "session": "manual"}),
                 "2026-09-01T00:00:00+00:00"))
        return task.id

    def session_wire(self, key="session_aaa/agents/main/wire.jsonl"):
        return self.sessions / "wd_proj_abcdef12" / key


class DoctorReportTests(DoctorBase):
    def test_healthy_db_exits_zero(self):
        self.seed()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout + result.stderr
        self.assertIn("installed", out)
        self.assertIn("sane=True", out)
        self.assertIn("messages=yes", out)
        self.assertIn("OK", out)

    def test_legacy_single_event_hook_stays_healthy_and_names_the_gap(self):
        """A TurnStarted-only install is healthy but reports the missing events."""
        self.seed()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("installed", result.stdout)
        self.assertIn("events: TurnStarted", result.stdout)
        self.assertIn("missing: Stop,SessionEnd", result.stdout)

    def test_full_event_hook_reports_no_missing_events(self):
        self.config.write_text(MULTI_HOOK_CONFIG.format(hook=HOOK), encoding="utf-8")
        self.seed()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("events: TurnStarted,Stop,SessionEnd", result.stdout)
        self.assertNotIn("missing:", result.stdout)

    def test_json_report_carries_per_event_presence(self):
        self.config.write_text(MULTI_HOOK_CONFIG.format(hook=HOOK), encoding="utf-8")
        self.seed()
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = json.loads(result.stdout)["hook"]
        self.assertEqual(hook["events"], {"TurnStarted": True, "Stop": True,
                                          "SessionEnd": True})
        self.assertEqual(hook["missing_events"], [])

    def test_absent_hook_is_reported(self):
        self.config.write_text(
            '[[hooks]]\nevent = "PreToolUse"\n'
            f'command = "python3 {HOOK}"\ntimeout = 10\n', encoding="utf-8")
        self.seed()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MISSING", result.stdout)
        self.assertIn("TurnStarted", result.stdout)

    def test_unreadable_config_is_reported(self):
        self.config.unlink()
        self.seed()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MISSING", result.stdout)
        self.assertIn("config not readable", result.stdout)

    def test_missing_state_fact_is_stale(self):
        self.seed()
        self.raw("DELETE FROM facts WHERE key='autosave_state'")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("autosave_state", result.stdout + result.stderr)

    def test_human_titled_task_without_alias_is_not_red_lit_forever(self):
        """'Autosave: notes' without the paired alias is not an autosave task.

        The permanent-red scenario: a human-created task whose title starts
        'Autosave:' is never hook-managed, so nothing writes its
        ``autosave_state`` fact; the doctor must not flag it and must exit 0
        while the real hook task stays healthy.
        """
        self.seed()
        self.make_task("Autosave: notes",
                       messages=[("user", "a human note"),
                                 ("assistant", "ack")])
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        autosave = [t for t in report["tasks"] if t["autosave"]]
        self.assertEqual(len(autosave), 1)
        self.assertNotIn("no current autosave_state fact",
                         result.stdout + result.stderr)

    def test_alias_only_task_is_not_enumerated(self):
        """An 'auto experiments' alias on a non-``Autosave:`` title stands alone."""
        self.seed()
        self.make_task("Experiments", aliases=["auto experiments"],
                       messages=[("user", "someone forged the alias")])
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        autosave = [t for t in json.loads(result.stdout)["tasks"]
                    if t["autosave"]]
        self.assertEqual(len(autosave), 1)
        self.assertNotIn("no current autosave_state fact",
                         result.stdout + result.stderr)

    def test_mismatched_title_and_alias_slugs_are_not_an_autosave_task(self):
        """The alias must be the one paired with the title's slug."""
        self.seed()
        self.make_task("Autosave: notes", aliases=["auto other"],
                       messages=[("user", "note")])
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        autosave = [t for t in json.loads(result.stdout)["tasks"]
                    if t["autosave"]]
        self.assertEqual(len(autosave), 1)
        self.assertNotIn("no current autosave_state fact",
                         result.stdout + result.stderr)

    def test_hook_signature_task_without_state_fact_is_still_stale(self):
        """The true positive survives: hook-shaped task, messages, no fact."""
        self.seed()
        task_id = self.make_task("Autosave: experiments",
                                 aliases=["auto:experiments"],
                                 messages=[("user", "hello")])
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"task {task_id} has 1 message(s) but no current"
                      f" autosave_state fact", result.stdout + result.stderr)

    def test_no_autosave_task_found_when_only_signature_less_tasks_exist(self):
        """Neither half alone substitutes for the hook's task."""
        self.make_task("cmpath local fork", aliases=["cmp project"],
                       messages=[("user", "unrelated")])
        self.make_task("Autosave: notes", messages=[("user", "note")])
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no autosave task found", result.stdout + result.stderr)

    def test_old_autosave_is_stale_with_nonzero_exit(self):
        self.seed()
        self.raw("UPDATE messages SET created_at=?", ("2026-01-01T00:00:00+00:00",))
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("STALE", result.stdout)

    def test_fresh_window_flag_overrides_default(self):
        self.seed()
        self.raw("UPDATE messages SET created_at=?", ("2026-01-01T00:00:00+00:00",))
        self.assertEqual(self.run_doctor().returncode, 1)
        self.assertEqual(self.run_doctor(max_age=10 ** 9).returncode, 0)

    def test_missing_db_exits_one(self):
        result = self.run_doctor(db=str(self.tmp / "nope.db"))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not found", result.stdout + result.stderr)

    def test_non_cmpath_db_cannot_diagnose(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE zzz(x)")
        conn.commit()
        conn.close()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("CANNOT DIAGNOSE", result.stdout)

    def test_invalid_max_age_cannot_diagnose(self):
        self.seed()
        result = self.run_doctor(max_age=0)
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_missing_hook_config_still_reports_db(self):
        self.config.unlink()
        self.seed()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("db ", result.stdout)

    def test_json_report_shape_and_per_task_counts(self):
        self.seed(cwd="/tmp/proj-a", prompt="first prompt", turn_id="t1")
        self.seed(cwd="/tmp/proj-b", prompt="second prompt", turn_id="t2")
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        for key in ("ok", "problems", "hook", "db", "tasks", "state",
                    "last_write", "max_age_seconds"):
            self.assertIn(key, report)
        self.assertEqual(report["max_age_seconds"], 604800)
        autosave = [t for t in report["tasks"] if t["autosave"]]
        self.assertEqual(len(autosave), 2)
        self.assertEqual(sorted(t["messages"] for t in autosave), [1, 1])

    def test_json_report_counts_messages_by_role_and_kind(self):
        self.seed()
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        db = json.loads(result.stdout)["db"]
        self.assertNotIn("has_evidence", db)
        self.assertEqual(db["messages_total"], 1)
        self.assertEqual(db["messages_by_role"], {"user": 1})
        self.assertEqual(db["messages_by_kind"], {"prompt": 1})

    def test_plain_report_counts_messages_and_claims_no_evidence_table(self):
        self.seed()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout + result.stderr
        self.assertIn("messages=yes", out)
        self.assertIn("rows=1", out)
        self.assertIn("by_role=user:1", out)
        self.assertIn("by_kind=prompt:1", out)
        self.assertNotIn("evidence=no", out)
        self.assertNotIn("evidence=yes", out)

    def test_stale_cursor_on_old_wire_is_stale(self):
        wire = self.session_wire()
        prompt_wire(wire, "first message")
        store = Autosave(self.db)
        try:
            store.ingest_session_dir(wire.parents[2], session_id="session_aaa",
                                     cwd="/tmp/proj")
        finally:
            store.close()
        prompt_wire(wire, "an un-ingested message")
        old = time.time() - 30 * 86400
        os.utime(wire, (old, old))
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("un-ingested", result.stdout)

    def test_pending_cursor_on_fresh_wire_is_healthy(self):
        wire = self.session_wire()
        prompt_wire(wire, "first message")
        store = Autosave(self.db)
        try:
            store.ingest_session_dir(wire.parents[2], session_id="session_aaa",
                                     cwd="/tmp/proj")
        finally:
            store.close()
        prompt_wire(wire, "an un-ingested message")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("pending", result.stdout)

    def cursor_row(self, report, key="session_aaa/agents/main/wire.jsonl"):
        rows = [c for row in report["state"] for c in row["cursors"]]
        row, = [c for c in rows if c["key"] == key]
        return row

    def ingested_wire(self, wire, *texts):
        prompt_wire(wire, *texts)
        store = Autosave(self.db)
        try:
            store.ingest_session_dir(wire.parents[2], session_id="session_aaa",
                                     cwd="/tmp/proj")
        finally:
            store.close()

    def forge_legacy_cursor(self, cursor):
        """Replace the stored cursor with ``cursor``, as an older cmpath wrote it.

        The row keeps its key; the cursor becomes whatever shape the caller
        passes, e.g. a bare line number or a ``{"line", "mtime"}`` dict.
        """
        store = Autosave(self.db)
        try:
            for task in store.memory.tasks():
                fact = store.memory.fact(task.id, STATE_KEY)
                if fact is None:
                    continue
                payload = fact["value"]
                key = next(iter(payload["cursors"]))
                payload["cursors"] = {key: cursor}
                store.memory.set_fact(task.id, STATE_KEY, payload,
                                      evidence_id=payload["last_evidence"])
                return key
        finally:
            store.close()
        self.fail("no autosave_state fact to rewrite")

    def test_rewritten_shorter_wire_is_provisional_not_current(self):
        """A wire rewritten shorter cannot be reported as fully stored."""
        wire = self.session_wire()
        self.ingested_wire(wire, *[f"message {i}" for i in range(6)])
        wire.write_text("", encoding="utf-8")
        prompt_wire(wire, "a replay nobody has stored")
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "provisional")
        self.assertIsNone(row["pending"])
        self.assertTrue(any("cannot be trusted" in p for p in report["problems"]),
                        report["problems"])

    def test_same_size_rewrite_with_new_mtime_is_provisional_not_current(self):
        """Same size and same head, but a new mtime: the wire was rewritten."""
        wire = self.session_wire()
        texts = [f"message {i:02d} " + "x" * 40 for i in range(6)]
        self.ingested_wire(wire, *texts)
        before = wire.read_bytes()
        changed = texts[:-1] + ["REPLAY! 99 " + "x" * 40]
        wire.write_text("\n".join(
            json.dumps({"type": "turn.prompt", "promptId": f"p{i}",
                        "input": [{"type": "text", "text": text}]})
            for i, text in enumerate(changed)) + "\n", encoding="utf-8")
        after = wire.read_bytes()
        os.utime(wire, (time.time() - 60, time.time() - 60))
        self.assertEqual(len(after), len(before))          # same size
        self.assertEqual(after[:512], before[:512])        # same head
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "provisional")
        self.assertIsNone(row["pending"])
        self.assertTrue(any("rewritten in place" in p for p in report["problems"]),
                        report["problems"])
        self.assertTrue(any("not stored" in p for p in report["problems"]),
                        report["problems"])

    def test_benign_mtime_touch_settles_through_the_digest_audit(self):
        """A wire whose bytes never changed is current again after the audit.

        This is the restart-shaped false alarm: the file was touched (same
        size, same head) but its mtime moved, so the cursor looks rewritten.
        The digest audit proves every recorded line is already stored and the
        cursor is reported current instead of provisional.
        """
        wire = self.session_wire()
        texts = [f"message {i:02d} " + "x" * 40 for i in range(6)]
        self.ingested_wire(wire, *texts)
        before = wire.read_bytes()
        os.utime(wire, (time.time() - 60, time.time() - 60))
        self.assertEqual(wire.read_bytes(), before)        # bytes untouched
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "current")
        self.assertIn("digest audit", row["reason"])
        self.assertIn("already stored", row["reason"])

    def test_byte_identical_rewrite_settles_through_the_digest_audit(self):
        """Rewriting the exact same bytes back is proven harmless by the audit."""
        wire = self.session_wire()
        texts = [f"message {i:02d} " + "x" * 40 for i in range(6)]
        self.ingested_wire(wire, *texts)
        before = wire.read_bytes()
        wire.write_bytes(before)
        os.utime(wire, (time.time() - 60, time.time() - 60))
        self.assertEqual(wire.read_bytes(), before)
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "current")
        self.assertIn("digest audit", row["reason"])

    def test_same_size_rewrite_with_unstored_rows_stays_provisional(self):
        """The audit catches a same-size rewrite whose rows were never saved.

        With the stored wire rows' content changed underneath them, no digest
        the audit prepares from the recorded lines is stored anymore: the
        rewrite replaced content nobody holds, so the cursor stays provisional
        and the doctor exits 1.
        """
        wire = self.session_wire()
        texts = [f"message {i:02d} " + "x" * 40 for i in range(6)]
        self.ingested_wire(wire, *texts)
        self.raw("UPDATE messages SET content = 'replaced: ' || content"
                 " WHERE json_extract(source,'$.kind')='wire'")
        before = wire.read_bytes()
        wire.write_bytes(before)
        os.utime(wire, (time.time() - 60, time.time() - 60))
        self.assertEqual(wire.read_bytes(), before)
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "provisional")
        self.assertIsNone(row["pending"])
        self.assertTrue(any("not stored" in p for p in report["problems"]),
                        report["problems"])
        self.assertIn("rewritten in place", row["reason"])
        self.assertIn("not stored", row["reason"])

    def test_audit_failure_falls_back_to_the_generic_provisional_reason(self):
        """A database error in the audit degrades to today's generic reason."""
        class Boom:
            def execute(self, *_args):
                raise sqlite3.OperationalError("db gone")

        wire = self.session_wire()
        texts = [f"message {i:02d} " + "x" * 40 for i in range(6)]
        self.ingested_wire(wire, *texts)
        before = wire.read_bytes()
        wire.write_bytes(before)
        _mtime, size, head = hook_module()._wire_marker(wire)
        problems: list[str] = []
        row = hook_module()._cursor_report(
            "session_aaa/agents/main/wire.jsonl",
            {"line": len(texts), "mtime": 1.0, "size": size,
             "head": head, "complete": True},
            [], self.sessions, datetime.now(timezone.utc), 604800, problems,
            conn=Boom(), task_id=1)
        self.assertEqual(row["status"], "provisional")
        self.assertIsNone(row["pending"])
        self.assertIn("rewritten in place", row["reason"])
        self.assertNotIn("digest audit", row["reason"])
        self.assertTrue(any("cannot be trusted" in p for p in problems))

    def test_audit_helper_returns_none_for_an_unreadable_wire(self):
        conn = sqlite3.connect(":memory:")
        try:
            audit = audit_wire_cursor(self.tmp / "nope" / "wire.jsonl", 5,
                                      conn, 1, "k")
        finally:
            conn.close()
        self.assertIsNone(audit)

    def test_audit_helper_propagates_database_errors(self):
        class Boom:
            def execute(self, *_args):
                raise sqlite3.OperationalError("db gone")

        wire = self.session_wire()
        self.ingested_wire(wire, "first message")
        with self.assertRaises(sqlite3.OperationalError):
            audit_wire_cursor(wire, 1, Boom(), 1,
                              "session_aaa/agents/main/wire.jsonl")

    def test_legacy_cursor_format_is_not_a_problem(self):
        """A cursor in the pre-size/head format is not evidence and not a fault."""
        wire = self.session_wire()
        self.ingested_wire(wire, "first message", "second message")
        recorded = os.path.getmtime(wire) - 60
        stamp = time.time()
        os.utime(wire, (stamp, stamp))
        self.forge_legacy_cursor({"line": 1, "mtime": recorded})
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "legacy")
        self.assertEqual(row["pending"], 0)
        plain = self.run_doctor()
        self.assertEqual(plain.returncode, 0, plain.stdout)
        self.assertIn("legacy", plain.stdout)
        self.assertIn("1 cursor(s) predate", plain.stdout)

    def test_bare_line_number_cursor_is_not_a_problem(self):
        """A cursor stored as a bare line number is the same legacy format."""
        wire = self.session_wire()
        self.ingested_wire(wire, "first message", "second message")
        self.forge_legacy_cursor(1)
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "legacy")
        self.assertEqual(row["pending"], 0)

    def test_legacy_cursor_with_backlog_is_still_stale(self):
        """The re-scan does not excuse a wire that really has un-stored lines."""
        wire = self.session_wire()
        self.ingested_wire(wire, "first message")
        recorded = os.path.getmtime(wire) - 60
        prompt_wire(wire, "an un-ingested message")
        old = time.time() - 30 * 86400
        os.utime(wire, (old, old))
        self.forge_legacy_cursor({"line": 1, "mtime": recorded})
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "stale")
        self.assertTrue(row["legacy"])
        self.assertEqual(row["pending"], 1)
        self.assertTrue(any("un-ingested" in p for p in report["problems"]),
                        report["problems"])

    def test_trusted_legacy_cursor_is_still_named_in_the_verdict(self):
        """A legacy cursor the ingester will resume from is legacy, not current.

        The old format is a fact about the cursor even when its mtime happens
        to still match the wire: the doctor reports it as informational
        `legacy` and the OK note names it, rather than presenting it as an
        ordinary modern cursor.
        """
        wire = self.session_wire()
        self.ingested_wire(wire, "first message", "second message")
        self.forge_legacy_cursor({"line": 2, "mtime": os.path.getmtime(wire)})
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        row = self.cursor_row(report)
        self.assertEqual(row["status"], "legacy")
        self.assertTrue(row["legacy"])
        self.assertEqual(row["pending"], 0)
        plain = self.run_doctor()
        self.assertEqual(plain.returncode, 0, plain.stdout)
        self.assertIn("1 cursor(s) predate", plain.stdout)

    def test_shrunk_wire_is_provisional_even_without_a_head(self):
        """A truncated wire is a fault, whatever format the cursor is in."""
        wire = self.session_wire()
        self.ingested_wire(wire, *[f"message {i:02d} " + "x" * 40
                                  for i in range(6)])
        lines = wire.read_text(encoding="utf-8").splitlines()
        wire.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        row = self.cursor_row(report)
        self.assertFalse(row["legacy"])
        self.assertEqual(row["status"], "provisional")
        self.assertIsNone(row["pending"])
        self.assertTrue(any("shrank" in p for p in report["problems"]),
                        report["problems"])

    def test_rewritten_wire_is_provisional_even_without_a_head(self):
        """A wire whose prefix is gone is a fault, not a legacy cursor."""
        wire = self.session_wire()
        texts = [f"message {i:02d} " + "x" * 40 for i in range(6)]
        self.ingested_wire(wire, *texts)
        wire.write_text("", encoding="utf-8")
        prompt_wire(wire, *(["REPLAYED 00 " + "x" * 40] + texts[1:]))
        result = self.run_doctor("--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        row = self.cursor_row(report)
        self.assertFalse(row["legacy"])
        self.assertEqual(row["status"], "provisional")
        self.assertIsNone(row["pending"])
        self.assertTrue(any("no longer starts with the bytes" in p
                            for p in report["problems"]), report["problems"])


class DeepDoctorTests(DoctorBase):
    """``--deep``: digest-audit every recorded cursor, on demand."""

    def run_deep(self, *extra):
        return self.run_doctor("--deep", *extra)

    def ingest_wire(self, wire, *texts):
        prompt_wire(wire, *texts)
        store = Autosave(self.db)
        try:
            store.ingest_session_dir(wire.parents[2], session_id="session_aaa",
                                     cwd="/tmp/proj")
        finally:
            store.close()

    def test_clean_store_reports_missing_zero_and_exits_zero(self):
        wire = self.session_wire()
        self.ingest_wire(wire, "first message", "second message")
        result = self.run_deep("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        deep = report["deep"]
        row, = deep["cursors"]
        self.assertEqual(row["key"], "session_aaa/agents/main/wire.jsonl")
        self.assertEqual(row["status"], "clean")
        self.assertEqual(row["missing"], 0)
        self.assertEqual(row["digests"], 2)
        self.assertEqual(deep["total_missing"], 0)
        self.assertEqual(deep["unauditable"], 0)
        plain = self.run_deep()
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("missing=0", plain.stdout)
        self.assertIn("total missing=0", plain.stdout)

    def test_ordinary_doctor_is_blind_to_a_seeded_hole_deep_is_not(self):
        """The absence-detector: a cursor ahead of what was ever stored.

        The seeding is the same the same-size-rewrite tests use: an UPDATE of
        the stored rows' content un-stores every recorded digest (DELETE is
        blocked by a guard trigger). The wire itself is untouched, so the
        ordinary doctor trusts the cursor and exits 0 -- only ``--deep``
        proves the recorded lines were never kept.
        """
        wire = self.session_wire()
        self.ingest_wire(wire, *[f"message {i:02d} " + "x" * 40
                                   for i in range(6)])
        self.raw("UPDATE messages SET content = 'replaced: ' || content"
                 " WHERE json_extract(source,'$.kind')='wire'")
        ordinary = self.run_doctor("--json")
        self.assertEqual(ordinary.returncode, 0, ordinary.stdout)
        self.assertEqual(json.loads(ordinary.stdout)["problems"], [])
        result = self.run_deep("--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        deep = report["deep"]
        row, = deep["cursors"]
        self.assertEqual(row["status"], "missing")
        self.assertEqual(row["missing"], 6)
        self.assertEqual(deep["total_missing"], 6)
        problem, = [p for p in report["problems"] if "un-stored" in p]
        self.assertIn("session_aaa/agents/main/wire.jsonl", problem)
        self.assertIn("--heal", problem)
        self.assertIn("UN-STORED", self.run_deep().stdout)

    def test_deep_reports_a_deleted_wire_as_unauditable_and_exits_zero(self):
        """An unreadable wire is informational under --deep, not a problem."""
        wire = self.session_wire()
        self.ingest_wire(wire, "first message")
        wire.unlink()
        result = self.run_deep("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["problems"], [])
        deep = report["deep"]
        row, = deep["cursors"]
        self.assertEqual(row["status"], "unauditable")
        self.assertEqual(deep["unauditable"], 1)
        self.assertEqual(deep["total_missing"], 0)
        plain = self.run_deep()
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("unauditable", plain.stdout)

    def test_json_report_carries_the_deep_section(self):
        wire = self.session_wire()
        self.ingest_wire(wire, "first message")
        result = self.run_deep("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        deep = json.loads(result.stdout)["deep"]
        self.assertEqual(set(deep), {"cursors", "total_missing",
                                     "unauditable"})
        self.assertEqual(set(deep["cursors"][0]),
                         {"task_id", "key", "line", "status", "digests",
                          "missing"})

    def test_deep_requires_doctor(self):
        """--deep on its own, with --heal, or with --ingest is an error."""
        for extra in ([], ["--heal"], ["--ingest", "wire.jsonl"]):
            result = subprocess.run(
                [sys.executable, HOOK, "--deep", *extra],
                capture_output=True, text=True,
                env={**os.environ, "PYTHONPATH": SRC}, timeout=60)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--deep applies only to --doctor", result.stderr)


class HealScriptTests(DoctorBase):
    """``--heal`` through the real CLI (subprocess, ``PYTHONPATH=src``)."""

    def run_heal(self, *extra):
        return subprocess.run(
            [sys.executable, HOOK, "--heal", "--db", self.db,
             "--sessions-root", str(self.sessions), *extra],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": SRC}, timeout=60)

    def seed_wire(self, *texts):
        wire = self.session_wire()
        prompt_wire(wire, *texts)
        store = Autosave(self.db)
        try:
            store.ingest_session_dir(wire.parents[2], session_id="session_aaa",
                                     cwd="/tmp/proj")
        finally:
            store.close()
        return wire

    def transcript_contents(self):
        store = Autosave(self.db)
        try:
            return [r.content for r in store.memory.transcript(1)]
        finally:
            store.close()

    def test_end_to_end_stores_the_message_after_the_cursor(self):
        wire = self.seed_wire("first message")
        prompt_wire(wire, "an appended message")

        result = self.run_heal()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("healed 1 message(s) across 1 wire(s)", result.stdout)
        self.assertIn("an appended message", self.transcript_contents())

    def test_json_report_shape_and_unresolved_rows(self):
        wire = self.seed_wire("first message")

        result = self.run_heal("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        row, = json.loads(result.stdout)["healed"]
        self.assertEqual(set(row), {"key", "path", "added", "passes", "status"})
        self.assertEqual(row["key"], "session_aaa/agents/main/wire.jsonl")
        self.assertEqual(row["status"], "clean")

        wire.unlink()
        result = self.run_heal("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = json.loads(result.stdout)["healed"]
        self.assertEqual(row["status"], "unresolved")
        self.assertIsNone(row["path"])

    def test_heal_sweeps_a_hook_signature_task_created_by_hand(self):
        """The hook's title+alias pair is enumerated even outside the hook."""
        wire = self.session_wire()
        prompt_wire(wire, "first message")
        store = Autosave(self.db)
        try:
            store.memory.create_task("Autosave: proj", aliases=["auto:proj"])
            store.ingest_session_dir(wire.parents[2], session_id="session_aaa",
                                     cwd="/tmp/proj")
        finally:
            store.close()
        prompt_wire(wire, "an appended message")

        result = self.run_heal()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("healed 1 message(s) across 1 wire(s)", result.stdout)
        self.assertIn("an appended message", self.transcript_contents())

    def test_heal_ignores_a_title_only_task_with_orphan_messages(self):
        """A title-only task falls out of the sweep.

        Its orphan messages have no hook-written state and no hook-resolved
        future (the alias is what the hook resolves by), so enumerating them
        would sweep nothing; reporting them is the doctor's business.
        """
        task_id = self.make_task("Autosave: notes",
                                 messages=[("user", "orphan note")])

        result = self.run_heal("--json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)["healed"]
        self.assertEqual(rows, [])
        self.assertNotIn(str(task_id), result.stdout + result.stderr)

    def test_heal_and_doctor_are_mutually_exclusive(self):
        result = self.run_heal("--doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr)


class HookFailOpenTests(AutosaveBase):
    """The hook path must return 0 on every error, never the doctor's code."""

    def test_db_path_that_is_a_directory_exits_zero(self):
        result = subprocess.run(
            [sys.executable, HOOK, "--db", str(self.tmp)],
            input=json.dumps({"session_id": "s", "cwd": "/tmp/p",
                              "prompt": "hi"}),
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": SRC}, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupt_db_exits_zero(self):
        db = self.tmp / "corrupt.db"
        db.write_bytes(b"this is not a sqlite database")
        result = subprocess.run(
            [sys.executable, HOOK, "--db", str(db)],
            input=json.dumps({"session_id": "s", "cwd": "/tmp/p",
                              "prompt": "hi"}),
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": SRC}, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_open_failure_is_swallowed_in_process(self):
        module = hook_module()
        args = mock.Mock(db=str(self.tmp / "x.db"), dry_run=False,
                         session_dir=None)
        payload = json.dumps({"session_id": "s", "cwd": "/tmp/p", "prompt": "hi"})
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(module, "_open", side_effect=RuntimeError("boom")):
            self.assertEqual(module.run_hook(args), 0)

    def test_default_path_never_runs_the_doctor(self):
        module = hook_module()
        args = mock.Mock(db=str(self.tmp / "y.db"), dry_run=False,
                         session_dir=None)
        payload = json.dumps({"session_id": "s", "cwd": "/tmp/p", "prompt": "hi"})
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch("sys.stdout", buffer):
            self.assertEqual(module.main(["--db", args.db]), 0)
        self.assertNotIn("verdict", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
