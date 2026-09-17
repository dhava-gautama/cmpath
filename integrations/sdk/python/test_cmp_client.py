import json
import unittest

from cmp_client import CMPClient, CMPError


class ClientContractTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def transport(url, body, headers, timeout):
            self.requests.append((url, json.loads(body), dict(headers), timeout))
            return 200, b'{"v":1,"ok":true,"result":{"accepted":true}}'

        self.client = CMPClient("http://127.0.0.1:8787", api_key="test-key", transport=transport)

    def test_lifecycle_uses_explicit_hooks_and_exact_payload_text(self):
        self.client.begin("r1", 7, "hello")
        self.client.before_model("r1", 1, "m1", '{ "messages": [] }', units=19, counting="bytes")
        self.client.after_model("r1", 1, "m1", response='{"id":"resp-1"}')
        self.client.before_tool("r1", 1, "t1", "send", {"value": 3})
        self.client.after_tool("r1", 1, "t1", result={"ok": True}, lease_token="lease-1")
        self.client.commit("r1", 1, {"text": "done"})

        self.assertEqual([row[1]["op"] for row in self.requests],
                         ["begin", "before_model", "after_model", "before_tool", "after_tool", "commit"])
        model = self.requests[1][1]
        self.assertEqual(model["args"]["payload_json"], '{ "messages": [] }')
        self.assertEqual(self.requests[4][1]["args"]["lease_token"], "lease-1")
        self.assertEqual(self.requests[0][2]["Authorization"], "Bearer test-key")

    def test_after_hooks_require_one_outcome(self):
        with self.assertRaises(ValueError):
            self.client.after_model("r1", 1, "m1")
        with self.assertRaises(ValueError):
            self.client.after_tool("r1", 1, "t1", result={}, error={})

    def test_protocol_errors_are_exposed(self):
        def failed(*_):
            return 409, b'{"v":1,"ok":false,"error":{"code":"fenced","message":"stale"}}'

        client = CMPClient("http://127.0.0.1:8787", transport=failed)
        with self.assertRaises(CMPError) as caught:
            client.inspect("r1")
        self.assertEqual(caught.exception.code, "fenced")
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
