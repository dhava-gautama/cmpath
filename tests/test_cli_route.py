from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import cmpath
from cmpath import TaskMemory
from cmpath import cli


class CLIRouteTests(unittest.TestCase):
    def _invoke(self, db: Path, *args: str):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["--db", str(db), "route", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_route_forwards_pinned_inputs_and_does_not_write(self):
        calls = []
        task_id = 7

        class Result:
            def as_dict(self):
                return {"route": "task", "task_id": task_id, "messages": []}

        class FakeRouter:
            def __init__(self, memory, config=None):
                self.memory = memory
                self.config = config

            def resolve_task(self, hint):
                calls.append(("resolve_task", hint))
                return {"status": "resolved", "task_id": task_id}

            def retrieve(self, query, task_id=None, requested_route=None, **options):
                calls.append((query, task_id, requested_route, options, self.config))
                return Result()

        fake_module = types.ModuleType("cmpath.router")
        fake_module.MemoryRouter = FakeRouter
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "state.db"
            with TaskMemory(db) as memory:
                task = memory.create_task("Demo route")
                task_id = task.id
                before = memory.export()
            # A package attribute is patched as well as sys.modules because
            # ``from . import router`` may reuse an already imported module.
            with mock.patch.dict(sys.modules, {"cmpath.router": fake_module}), mock.patch.object(
                cmpath, "router", fake_module, create=True
            ):
                code, stdout, stderr = self._invoke(
                    db,
                    "What is pinned?",
                    "--task-hint",
                    "Demo route",
                    "--route",
                    "task",
                    "--budget",
                    "321",
                    "--scope",
                    "task",
                    "--pinned-config",
                    '{"mode":"safe"}',
                    "--pinned-input",
                    '{"source":"caller"}',
                    "--json",
                )
            self.assertEqual(code, 0, stderr)
            output = json.loads(stdout)
            self.assertEqual(output["task_id"], task_id)
            self.assertEqual(calls[0], ("resolve_task", "Demo route"))
            query, resolved_id, route, options, config = calls[1]
            self.assertEqual((query, resolved_id, route), ("What is pinned?", task_id, "task"))
            self.assertEqual(options["budget"], 321)
            self.assertEqual(options["scope"], "task")
            self.assertEqual(options["pinned_input"], {"source": "caller"})
            self.assertEqual(config, {"mode": "safe"})
            with TaskMemory(db) as memory:
                self.assertEqual(memory.export(), before)

    def test_route_accepts_query_option_and_rejects_duplicate_query(self):
        class FakeRouter:
            def __init__(self, memory):
                pass

            def retrieve(self, query, **options):
                return {"query": query}

        fake_module = types.ModuleType("cmpath.router")
        fake_module.MemoryRouter = FakeRouter
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "state.db"
            with TaskMemory(db):
                pass
            with mock.patch.dict(sys.modules, {"cmpath.router": fake_module}), mock.patch.object(
                cmpath, "router", fake_module, create=True
            ):
                code, stdout, stderr = self._invoke(db, "--query", "from-option")
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout), {"query": "from-option"})
                code, stdout, stderr = self._invoke(db, "positional", "--query", "option")
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stderr)["error"],
                                 "route query must be supplied positionally or with --query, not both")

    def test_route_rejects_non_object_pinned_config(self):
        class FakeRouter:
            def __init__(self, memory):
                raise AssertionError("router must not be constructed")

        fake_module = types.ModuleType("cmpath.router")
        fake_module.MemoryRouter = FakeRouter
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "state.db"
            with TaskMemory(db):
                pass
            with mock.patch.dict(sys.modules, {"cmpath.router": fake_module}), mock.patch.object(
                cmpath, "router", fake_module, create=True
            ):
                code, stdout, stderr = self._invoke(db, "query", "--pinned-config", "[]")
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("pinned config must be a JSON object", json.loads(stderr)["error"])

    def test_actual_router_uses_typed_config_and_ephemeral_pin(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "state.db"
            with TaskMemory(db) as memory:
                task = memory.create_task("Pinned route")
                evidence = memory.append(task.id, "user", "A source statement")
                before = memory.export()

            code, stdout, stderr = self._invoke(
                db,
                "anything",
                "--route",
                "pinned",
                "--budget",
                "500",
                "--pinned-config",
                '{"max_pinned_tasks":1,"max_pinned_evidence":1}',
                "--pinned-input",
                json.dumps({"task_id": task.id, "evidence_ids": [evidence.id]}),
                "--json",
            )
            self.assertEqual(code, 0, stderr)
            output = json.loads(stdout)
            self.assertEqual(output["route"], "pinned")
            self.assertEqual(output["task_id"], None)
            self.assertIn(evidence.citation, output["citations"])
            self.assertLessEqual(output["used_units"], output["input_allowance"])
            with TaskMemory(db) as memory:
                self.assertEqual(memory.export(), before)


if __name__ == "__main__":
    unittest.main()
