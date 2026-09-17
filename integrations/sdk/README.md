# CMP managed-turn client examples

This directory contains dependency-free HTTP client examples for Python,
TypeScript, and Go. They all speak the same deliberately small JSON contract.
The clients use only their language standard library (TypeScript uses the
runtime's built-in `fetch`).

## Wire contract (v1)

Send a `POST` to the configured endpoint (the examples default to
`/v1/managed-turn`) with a JSON envelope:

```json
{
  "v": 1,
  "op": "before_model",
  "request_id": "checkout-42",
  "generation": 1,
  "call_id": "model-1",
  "args": {"payload_json": "{\"model\":\"example\",\"messages\":[]}", "units": 18, "counting": "utf8-json-bytes"}
}
```

The server returns HTTP 2xx and `{"v":1,"ok":true,"result":...}` on
success. Errors use a non-2xx status where possible and
`{"v":1,"ok":false,"error":{"code":"...","message":"..."}}`.
Implementations must treat a timeout or broken connection during a mutating
operation as an unknown outcome: inspect or reconcile it; do not silently
resend it.

The supported operations and their `args` are:

| Operation | Arguments | Purpose |
| --- | --- | --- |
| `begin` | `task_id`, `query`, optional `system`, `model_key`, `budget`, `reserve`, `scope`, `retrieval_limit`, `recent`, `counting` | Create or replay a logical turn and return its context. |
| `inspect` | none | Read the saved turn and journal state. |
| `recover` | `generation` | Explicitly fence a pending worker and return the new generation. |
| `before_model` | `payload_json`, `units`, `counting` | Record the complete serialized provider request immediately before dispatch. |
| `after_model` | exactly one of `response_json` or `error` | Record the confirmed response, or a known provider failure. |
| `before_tool` | `name`, `arguments`, optional `ttl_ms` | Record intent and request action eligibility immediately before the host invokes the tool. |
| `after_tool` | `result` or `error`, optional `lease_token` | Record the confirmed tool outcome after the callback returns. |
| `commit` | `reply` | Commit the assistant text and any snapshot, facts, or provenance. |
| `abort` | none | Explicitly abandon a pending turn while retaining its audit. |

`request_id`, `generation`, and `call_id` are top-level fields where
applicable. `before_model` accepts a caller-produced `payload_json` so the
bytes counted by CMP can be the exact bytes sent to the provider. A non-string
payload in the language examples is compactly JSON encoded for convenience;
use a string for strict wire identity.

The HTTP API is a transport boundary, not a transparent effect fence. A host
must explicitly install and execute all five lifecycle hooks:
`before-model`, `after-model`, `before-tool`, `after-tool`, and `commit`.
`before-tool` eligibility narrows a local race window; it cannot make an
arbitrary non-participating remote service atomic. Keep provider and tool
idempotency keys, reconcile indeterminate outcomes, and abort rather than
guess.

## Examples

- [`python/cmp_client.py`](python/cmp_client.py) uses `urllib.request` and is
  tested by [`python/test_cmp_client.py`](python/test_cmp_client.py).
- [`typescript/cmp-client.ts`](typescript/cmp-client.ts) uses global `fetch`.
- [`go/cmpclient.go`](go/cmpclient.go) uses `net/http`; its contract tests run
  with `go test` and have no third-party modules.

The snippets intentionally do not import a framework or provider SDK. Adapter
guides in [`../../adapters`](../adapters) show where to call the same methods
from OpenAI Agents SDK, LangGraph, Kimi, Cursor, Hermes, or a custom harness.
