"""Command counters are explicit fixtures, not provider tokenizer validation."""
import json
import sys
import unittest

from cmpath.request_counter import LocalTokenCounter, estimated_request_units, utf8_request_bytes


class RequestCounterTests(unittest.TestCase):
    def test_full_utf8_payload_reaches_command_including_tools(self):
        raw = json.dumps({"messages": [{"role": "user", "content": "café"}], "tools": [{"name": "read", "description": "αβ"}]}, ensure_ascii=False)
        code = "import sys,json; b=sys.stdin.buffer.read(); p=json.loads(b); assert p['tools'][0]['name']=='read'; print(json.dumps({'units':len(b)}))"
        counter = LocalTokenCounter([sys.executable, "-c", code], name="fixture-utf8-bytes")
        self.assertEqual(counter(raw), utf8_request_bytes(raw))
        self.assertEqual(estimated_request_units(raw), (len(raw) + 3) // 4 + 8)

    def test_invalid_outputs_fail_without_fallback(self):
        for output in ('{"units":true}', '{"units":-1}', '{"units":3.2}', '{"units":3,"other":0}', 'not-json'):
            counter = LocalTokenCounter([sys.executable, "-c", "print(" + repr(output) + ")"], name="invalid-fixture")
            with self.assertRaises(ValueError):
                counter('{}')
        oversized = LocalTokenCounter([sys.executable, "-c", "print('x'*4097)"], name="verbose-fixture")
        with self.assertRaisesRegex(ValueError, "4096"):
            oversized('{}')

    def test_failure_timeout_and_shell_string_rejected(self):
        failed = LocalTokenCounter([sys.executable, "-c", "raise SystemExit(3)"], name="failure-fixture")
        with self.assertRaisesRegex(ValueError, "exit code 3"):
            failed('{}')
        timed = LocalTokenCounter([sys.executable, "-c", "import time; time.sleep(5)"], name="timeout-fixture", timeout=.05)
        with self.assertRaisesRegex(ValueError, "timed out"):
            timed('{}')
        with self.assertRaises(ValueError):
            LocalTokenCounter("python tokenizer.py", name="bad-command")


if __name__ == "__main__":
    unittest.main()
