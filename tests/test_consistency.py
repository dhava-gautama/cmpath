"""Python freshness configuration and legacy request-identity compatibility."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmpath.agent import AgentConfig, ResearchAgent, TOOLS
from cmpath.agent_cli import main
from cmpath.harness import Harness


class RecordingBackend:
    def __init__(self):
        self.calls = []

    def call(self, operation, **arguments):
        self.calls.append((operation, arguments))
        return {"status": "committed", "reply": {"text": "saved"}}


class ConsistencyTests(unittest.TestCase):
    def test_begin_default_and_explicit_snapshot_preserve_wire_arguments(self):
        backend = RecordingBackend()
        harness = Harness(backend)
        harness.begin("r", 1, "question")
        harness.begin("r", 1, "question", consistency="snapshot")
        self.assertEqual(backend.calls[0], backend.calls[1])
        self.assertNotIn("consistency", backend.calls[0][1])
        harness.begin("r", 1, "question", consistency="scope")
        self.assertEqual(backend.calls[-1][1]["consistency"], "scope")

    def test_invalid_consistency_rejected_before_backend_dispatch(self):
        backend = RecordingBackend()
        for value in (None, "", "latest", "SCOPE", False, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "consistency"):
                    Harness(backend).begin("r", 1, "q", consistency=value)
                with self.assertRaisesRegex(ValueError, "consistency"):
                    AgentConfig("http://127.0.0.1:1", "fixture", "r", ".", consistency=value)
        self.assertEqual(backend.calls, [])

    def test_agent_default_identity_is_legacy_hash_and_scope_is_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = RecordingBackend()
            base = dict(endpoint="http://127.0.0.1:1", model="fixture", request_id="r", workspace=folder)
            legacy = {"endpoint": base["endpoint"], "model": "fixture",
                      "workspace": str(Path(folder).resolve()), "max_turns": 8,
                      "max_tokens": 1000, "budget": 16000,
                      "counting": "estimated-json", "tools": TOOLS}
            expected = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
            for options in ({}, {"consistency": "snapshot"}, {"consistency": "scope"}):
                agent = ResearchAgent(Harness(backend), AgentConfig(**base, **options),
                                      transport=lambda _: self.fail("committed request must replay"))
                self.assertEqual(agent.run(1, "question"), {"text": "saved"})
            self.assertEqual(backend.calls[0], backend.calls[1])
            self.assertEqual(backend.calls[0][1]["model_key"], expected)
            self.assertNotIn("consistency", backend.calls[0][1])
            legacy["consistency"] = "scope"
            scoped = backend.calls[2][1]
            self.assertEqual(scoped["consistency"], "scope")
            self.assertEqual(scoped["model_key"], hashlib.sha256(
                json.dumps(legacy, sort_keys=True).encode()).hexdigest())
            self.assertNotEqual(scoped["model_key"], expected)

    def test_cli_scope_reaches_agent_config(self):
        argv = ["cmpath-agent", "--binary", "unused", "--db", "unused",
                "--task", "1", "--request-id", "r", "--endpoint", "http://127.0.0.1:1",
                "--model", "fixture", "--workspace", ".", "--question", "q"]
        for extra, expected in (([], "snapshot"), (["--consistency", "scope"], "scope")):
            with self.subTest(expected=expected), patch("sys.argv", argv + extra), \
                    patch("cmpath.agent_cli.NativeHarness") as native, \
                    patch("cmpath.agent_cli.ResearchAgent") as agent, \
                    patch("sys.stdout", new_callable=io.StringIO):
                native.return_value.__enter__.return_value.task.return_value = {"id": 1}
                agent.return_value.dispatch_count = 0
                agent.return_value.run.return_value = {"text": "saved"}
                main()
                self.assertEqual(agent.call_args.args[1].consistency, expected)


if __name__ == "__main__":
    unittest.main()
