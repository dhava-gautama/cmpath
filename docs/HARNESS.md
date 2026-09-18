# Harness integration

The harness owns task selection, model dispatch and external actions. The memory engine owns the durable records and transition checks. Both native deployment modes use the same Go engine: import `cmpath.local/native/engine` into a Go harness, or connect an existing Python harness through `NativeHarness` and one persistent child process.

## A turn's lifecycle

1. Select or resolve an explicit task. `Begin` atomically restores its snapshot into a counted context, appends the user's original input once and records a pending turn. The current query, system text and selected task snapshot are mandatory. Evidence is admitted in complete atoms; facts stay paired with their source messages.
2. Dispatch the model from the host. Before every actual model call, record its complete serialized request and size. Tool schemas and the entire follow-up conversation belong in this payload too. `RunSession.checkpoint_model_request` returns the exact bytes to send.
3. Execute host-approved tools through the tool journal. Store intent before execution and result after confirmation. The application still decides which tools are allowed and supplies their implementations.
4. Commit the assistant response, new planner snapshot and any explicit fact revisions in one transaction. A stale task revision or worker generation rejects the commit. In a scoped turn, a replacement snapshot must also declare `provenance` evidence IDs; CMP validates those source records atomically.

Slow model and tool callbacks run outside database transactions and outside the backend transport lock. Other tasks can proceed while a model is generating. Each task permits one pending turn; different tasks can have independent pending turns. SQLite still serializes writes to a database.

## Python integration

`Harness(backend)` is the lifecycle layer. A backend implements `call(operation, **arguments)` and `close()`. `NativeHarness(binary, database, create=False, timeout=15)` supplies the Go implementation over stdio. A read connection does not create an absent database; use `create=True` when creation is intended. The existing `TaskMemory` API remains available for direct Python storage use.

`harness.run(request_id, task_id, query, complete, **options)` calls `complete(session)` only for a newly prepared turn. The callback returns a string or a dictionary with `text`, optional `snapshot`, optional `facts` and optional `provenance` (`[{"evidence_id": 481, "kind": "supports"}]`). Scoped snapshot writes require at least one `supports` edge; missing, deleted, out-of-scope or retracted sources fail closed. It can use `session.messages`, `session.snapshot`, `session.tool(...)`, `session.action_eligible(...)` and `session.checkpoint_model_request(...)`. These are integration hooks for a real host loop; the engine does not fabricate model responses or perform implicit network calls.

### Opt-in hybrid routing

The native turn path remains the default. A host that has a
`cmpath.router.MemoryRouter` (also exported as `HybridMemoryRouter`) can opt
in to a bounded pinned/on-demand context with
`harness.prepare_routed_turn(request_id, task_id, query, router=router)` or the
short `prepare_turn(...)` spelling. `NativeHarness(..., router=router)` and
`RoutedHarness(harness, router)` are equivalent convenience forms. The adapter
calls the router's `retrieve(query, task_id=..., requested_route=...)` method;
route policy, pin bounds, task resolution, source citations and budgeted
packing remain owned by `MemoryRouter`. Pass `requested_route` (or `route`/
`route_kind`) only when deliberately selecting one of its route kinds. Router
package options such as `budget`, `reserve`, `system`, `retrieval_limit` and
`recent` are forwarded when supplied; `router_options` is available for
additional router-specific read options.

The returned `RoutedTurn` exposes the router's defensive `messages`,
`citations`, `route`, `reason` and `as_dict()` values. Its
`checkpoint_model_request()`/`before_model()`, `record_model_response()`/
`after_model()`, `before_tool()`, `after_tool()`, and `commit()` methods are
thin lifecycle adapters over the same `NativeHarness` journal. A routed model
payload receives the exact routed messages only when the caller has not
provided a `messages` field; provider-specific tool schemas and options are
left untouched. `before_tool()` records intent and returns an eligibility
lease but never runs a callback; the host invokes the tool and then calls
`after_tool()` with its confirmed result (or known error).

Routing happens after the native `begin` transaction. A routing failure leaves
the pending turn inspectable and recoverable. Committed request replays do not
retrieve a fresh context, and pending replays raise `in_progress`; this keeps
the durable request identity tied to its original route. No adapter method
automatically retries a model request, tool intent, or uncertain transport
operation. Inspect/reconcile an indeterminate model or tool outcome before
continuing, and supply explicit provenance when committing a derived scoped
snapshot.

### External Python host recipe

`RoutedHarness` is for an executor that owns the provider and tool callbacks.
It is an adapter over a `Harness` backend, not a model client. The host keeps
the same logical request ID and executes the five boundaries explicitly:

```python
from cmpath import MemoryRouter, NativeHarness, RoutedHarness, TaskMemory

memory = TaskMemory("cmpath.db")
router = MemoryRouter(memory)
native = NativeHarness("native/bin/cmpath-native", "cmpath.db")
host = RoutedHarness(native, router)

turn = host.prepare_turn(
    "release-42", 3, "What budget was approved?",
    requested_route="lineage", budget=1600, reserve=300,
)
request_bytes = turn.before_model(
    "model-1", {"model": "provider-model"},
)
provider_response = provider.complete(request_bytes)       # host-owned call
turn.after_model("model-1", response=provider_response)

intent = turn.before_tool("tool-1", "publish", {"task": 3})
result = publish_to_external_system(intent)                 # host-owned call
turn.after_tool("tool-1", result=result,
                lease_token=intent["eligibility"]["token"])
turn.commit({"text": "Published", "snapshot": {"phase": "done"}})
```

`before_model` only journals the complete provider payload and returns bytes;
`after_model` records a confirmed response or known error. `before_tool`
records exact intent and returns a fresh local eligibility lease but never calls
the tool. The host invokes the external effect and then supplies its confirmed
result to `after_tool`; `commit` is the fifth boundary. A missing, expired or
mismatched lease blocks reconciliation. These local checks cannot make an
arbitrary remote API atomic, so keep provider/tool idempotency keys and retain
the request ID for external reconciliation.

On a committed replay, `host.prepare_turn(...)` returns the saved native turn
without retrieving a new route context. Do not dispatch a provider or invoke a
tool in that branch:

```python
replay = host.prepare_turn(
    "release-42", 3, "What budget was approved?",
    requested_route="lineage", budget=1600, reserve=300,
)
if replay.turn["status"] == "committed":
    reply = replay.turn["reply"]
```

An identical pending replay raises `HarnessError` with code `in_progress` and
requires inspection plus deliberate `recover(request_id, generation)` before
continuing. A recovered started tool is an uncertain external outcome and must
be reconciled; `RoutedHarness` never silently replays it.

An MCP-only host can use `cmp_route` to prepare a bounded prompt package, but
that tool does not install the five lifecycle boundaries. Use the returned
`messages` and `citations` as input to the host, then use `RoutedHarness`, the
authenticated control plane, or an SDK client to checkpoint the provider and
tool loop. `cmp_codex_event` records Codex hook evidence; it is not a substitute
for `before-model`, `after-model`, `before-tool`, `after-tool`, and `commit`.

Options include `system`, `model_key`, `budget`, `reserve`, `scope`, `retrieval_limit`, `recent` and `counting`. The Python default recent-message count is four; the Go `Request` zero value for `Recent` means none. An omitted Go budget defaults to 2,000, retrieval limit to 24 and scope to the selected task plus three ancestor hops.

Use a globally unique logical `request_id` within the database, supplied by the application's request dispatcher. Reuse it only for the same task, query, system text, model configuration key and packing options. Wire transport IDs are separate and generated automatically. Changing the meaning of a logical request while reusing its ID raises a conflict.

The executable `examples/native_workflow.py` runs a real file-checksum tool. `examples/native_chat.py` binds the harness to a caller-configured Chat Completions endpoint. It requires the endpoint, model, question and logical request ID. No credentials are embedded in the examples. The text-generation example uses an estimated input count; replace it with the host's actual tokenizer accounting for a production token limit.

## Embedded Go integration

Import the `engine` package directly. `engine.Open(path)` returns the local engine. `Run(ctx, request, complete)` invokes a host callback receiving an `engine.Session`, then commits its returned `engine.Reply`. The callback can call `Session.Tool` to bracket a real external tool. `native/examples/embedded` is a complete file-checksum workflow with no Python or process bridge.

For an existing executor, use `Begin`, `RecordModelRequest`, `StartTool`, `ActionEligible`, `FinishTool` and `Commit` directly at the executor's existing boundaries. Call `ActionEligible` after the tool intent is recorded and immediately before invoking the external callback. Pass its returned `token` to `FinishTool`; a missing, expired or mismatched token leaves the intent unresolved for explicit reconciliation. The certificate narrows the race window and makes the check auditable; it cannot make an arbitrary non-participating remote service atomic.

`Begin` and `Preview` accept an optional `engine.Counter` containing a name and complete-message count function. The counter receives a defensive copy and must be deterministic and nonnegative. Callback counting does not cross the stdio boundary: the Python bridge supports the built-in estimated or exact serialized UTF-8 byte count for initial packing, then checks a caller-supplied counter before actual model dispatch.

## Model responses

`harness.model_calls(request_id)` and `session.model_calls()` return requests in their original recorded order, including exact `payload_json`, counting metadata and nullable `response_json`. Call `session.record_model_response(call_id, response)` with a confirmed JSON object or exact JSON-object string before executing its tools. Identical recording is idempotent; changed bytes conflict. An absent response remains an unknown provider outcome. The research agent preserves the original UTF-8 HTTP response text.

## Recovery and retry

An identical completed request returns its saved reply and does not run the callback again. An identical pending request returns its saved first-call context but does not authorize automatic execution. `run` reports `in_progress`; the application inspects it with `harness.inspect(request_id)`.

Use `harness.recover(request_id, expected_generation)` to deliberately take over unfinished work. It increments the durable generation. Commits and tool updates from an older generation are rejected. Generations prevent stale writes; they are not access credentials. All participants in one database remain in the same application isolation domain.

The recovered `RunSession` exposes its saved context, snapshot and tool records. A completed tool call returns its recorded result. A tool left in `started` state has an uncertain external outcome: the process may have died before or after the real action. `session.tool` reports `indeterminate_tool` instead of executing it again. Query the external system or use the tool's own idempotency mechanism, then call `reconcile_tool(call_id, confirmed_result)` with the confirmed outcome. Final commit is blocked while any started tool remains unresolved.

Recovery cannot guarantee exactly-once external effects. A local SQLite transaction cannot atomically commit with an arbitrary remote system. The implemented guarantee is durable local intent, visible uncertainty, a pre-effect eligibility certificate and no automatic replay of an uncertain tool. A provider request can also complete or incur a charge before a process failure; its saved request is an audit record, not proof that generation did or did not happen.

`session.abort()` records an aborted turn and releases the task for a new logical request. It preserves the original user message and tool audit. It does not undo an external effect. A changed task snapshot makes recovery and commit conflict; abort that turn and prepare new work against the updated state.

## Counting and provider protocols

The initial package's `messages_json` is the exact serialization counted by the native byte counter. With `counting="estimated"`, its units are a character-based estimate with message framing. With `counting="utf8-json-bytes"`, units are UTF-8 bytes, not model tokens. `used_units <= budget - reserve` holds under the selected counter.

For actual model calls, `checkpoint_model_request(call_id, payload, counter=..., counting=...)` counts the entire request serialization, including `messages`, tool definitions and provider options. The custom counter receives that serialized JSON string. Supply a counter for the model's real request accounting, or clearly retain the estimated label. The engine enforces the declared count against the allowance; correctness of a caller-defined count function belongs to the host.

The hook preserves arbitrary provider payload structure. It does not convert a tool response into a different provider's format. The host should retain tool-call IDs, assistant tool-call messages and tool-result messages in the exact sequence required by its provider, then checkpoint each complete follow-up request before dispatch. Tool records are also retained as source-role evidence for subsequent turns.

## Interoperability and retention

The native engine reads the version-1 task/evidence schema used by CMP 0.3 and adds separately versioned `cmp_` journal tables. Python-written messages, facts and snapshots are visible to Go; Go commits are visible to Python. Native task and evidence identifiers retain the existing `Tn:Mm` citation format. Original roles, UTF-8 content and large JSON integers are preserved.

The native package uses Unicode NFKC normalization and case folding for query and alias handling. Unicode table versions can differ from an older Python runtime, so full equivalence across every Unicode release is not claimed. Neither implementation performs learned semantic routing.

Use the harness hooks for task execution; direct `TaskMemory` writes bypass those hooks. A direct snapshot change is detected at native commit. The older generic JSON `export()` does not include the native journal. Use a complete SQLite backup for recovery of native turns. The existing `TaskMemory.backup()` API copies all tables, including the journal.

Journal foreign keys prevent deletion of a task or evidence still referenced by recorded turns or tool results. This intentionally preserves the recovery record. Use `retention_plan(cutoff)` and `apply_retention(cutoff, plan_hash)` for reviewed retirement of eligible terminal journals. Evidence and minimal retired-ID tombstones remain. `export_journal(destination)` writes a complete audit JSONL archive without intentionally replacing an existing path; its hard-link publication and filesystem-portable reservation fallback (including the fallback's interruption marker) are documented in [RETENTION.md](RETENTION.md). It is not an importable recovery format. Choose one database per user or access-control domain and put any service authentication and encryption in the host application.

## Stdio protocol

The bridge is one JSON request and one JSON response per line. Requests carry `v: 1`, a transport `id`, an `op` and `args`. Responses repeat the version and ID and contain either `result` or an error with `code` and `message`. Operations are task/evidence functions and harness lifecycle hooks; no raw-SQL operation is exposed.

Python sends one in-flight request per backend connection and applies a deadline to the full write/response operation. A timeout or broken stream terminates the child and reports an uncertain outcome. It never silently resends a mutating operation. Reopen and inspect the logical request ID. Frames are limited to 16 MiB; use smaller evidence batches for larger ingestion jobs. A Go harness using the package directly has no stdio frame limit.

This protocol and the Python `Backend` interface are the extension points for a future Rust implementation. This release includes a working Go implementation; a Rust backend has not been implemented or benchmarked.

Journal schema 4 adds confirmed model responses, retired-ID tombstones, provenance edges and action leases. Opening a supported schema-1 journal upgrades it additively. Stop older workers during upgrades and retain a SQLite backup. Older binaries refuse new schema-4 connections; database triggers additionally abort both the insertion of a turn with a retired request ID and the renaming of a live turn onto one, so already-open older connections cannot reintroduce a retired ID through either path. Those guards cover `cmp_turns`; the child journal tables rely on foreign-key enforcement, which is per-connection. See [RETENTION.md](RETENTION.md) for the retention convergence and plan-hash contracts.
