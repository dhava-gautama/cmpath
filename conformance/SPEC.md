# CMP adapter conformance v1

This directory defines the local, transport-neutral contract for a CMP adapter.
The contract is deliberately about observable behavior, not a particular Python,
Go, MCP, HTTP, or stdio API.  `test_conformance.py` runs the contract against
the included deterministic adapter and can also be pointed at an adapter with
the same small Python protocol.

## Adapter boundary

An adapter under test exposes the methods below.  Methods return JSON-like
dictionaries and raise `AdapterError` with a stable `code` on a rejected
operation.  The reference implementation is in `reference.py`; it is a local
test fixture, not a second production database implementation.

```text
create_task(title, snapshot={}) -> {id, revision, snapshot}
append_evidence(task_id, role, content, source={}) -> {id, citation, ...}
begin(request_id, task_id, query, consistency="snapshot", ...) -> turn
inspect(request_id) -> turn
recover(request_id, expected_generation) -> turn
checkpoint_model_request(request_id, generation, call_id, payload, units) -> bytes
record_model_response(request_id, generation, call_id, response) -> {recorded: true}
model_calls(request_id) -> [{call_id, payload_json, response_json, ...}]
start_tool(request_id, generation, call_id, name, arguments) -> tool
action_eligible(request_id, generation, call_id, ttl_ms=5000) -> lease
finish_tool(request_id, generation, call_id, result, lease_token="") -> tool
commit(request_id, generation, reply) -> turn
abort(request_id, generation) -> {aborted: true}
run(request_id, task_id, query, callback) -> reply
close() -> None
```

An adapter may provide a richer native API.  A thin binding should normalize
its errors and records to this boundary before running the suite.

## Required guarantees

1. **Request identity.** A request ID is a logical idempotency key. Reusing it
   with the same input returns the original pending/committed record. Reusing
   it with different input is `conflict`. The serialized initial request and
   context are retained exactly enough to reconstruct the turn.
2. **Source citations.** Evidence is immutable and every returned citation is a
   stable `T<task>:M<message>` identifier resolving to the original source
   record. A scoped derived snapshot must carry at least one `supports`
   provenance edge to existing, same-task, non-retracted evidence. Missing,
   cross-task, or unknown provenance fails closed.
3. **Stale generations.** Recovery increments the worker generation. All writes
   carrying an older generation (`checkpoint`, model response, tool, commit,
   abort) fail with `fenced`; they never overwrite current state.
4. **Model checkpoint recovery.** A complete provider request, including tool
   schemas and options, is durably checkpointed before dispatch. The exact
   UTF-8 bytes and caller-declared size units are inspectable. A confirmed
   response is recorded before downstream effects. A recovered worker reuses a
   saved request/response and does not dispatch it a second time.
5. **Tool eligibility leases.** Tool intent is journaled before the callback.
   `action_eligible` revalidates generation and captured scope, returns a
   short-lived, call-specific lease, and rejects expired, mismatched, or stale
   leases. The lease narrows a race; it is not an exactly-once guarantee for a
   remote side effect.
6. **No silent retry.** A pending duplicate is observable as `in_progress`; an
   indeterminate started tool is `indeterminate_tool`; an unknown model outcome
   is `indeterminate_model`. The adapter never invokes a callback or remote
   provider automatically after a transport/process failure. Retry requires an
   explicit caller decision and reconciliation.
7. **Authentication failure.** HTTP 401/403 (or an equivalent auth rejection)
   is surfaced as `auth`, without retry. Secrets are supplied by the caller,
   are not written into request journals or error text, and missing credentials
   fail before a non-loopback request is sent.
8. **Body limits.** Oversized protocol frames and provider response bodies are
   rejected before allocation/dispatch. The reference limits are 16 MiB for a
   native JSONL frame, 8 MiB for a provider response, 1 MiB per local document,
   200 lines per read, and 500 listed files. Adapters may use smaller limits,
   but must expose a bounded, deterministic rejection.
9. **Storage safety.** Read-only opening of a missing database does not create
   it. Backups are standalone and integrity-checkable, are not written over the
   live database, and are published atomically. Local file tools stay beneath
   their configured root, reject parent traversal and symlinks, and have no
   shell/write capability.

## Running

The suite uses only the Python standard library and a temporary local SQLite
database. From this directory's parent:

```bash
python conformance/run.py
python -m unittest discover -s conformance -p 'test_*.py' -v
```

To run an external adapter, import `run_suite` and pass a factory returning an
object implementing the boundary above. The suite never contacts a provider.
The optional native smoke test is enabled only when `CMP_NATIVE_BINARY` names
an executable that responds to `--version`; otherwise the local contract suite
still runs in full.

