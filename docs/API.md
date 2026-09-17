# Python API

This document covers the Python `TaskMemory` storage API. For the 0.4 native execution hooks and recovery journal, see [HARNESS.md](HARNESS.md).

All identifiers are positive integers local to one database. Public citations combine task and message IDs, such as `T3:M12`. Task and message IDs are not reused after committed deletion. Treat IDs as opaque; they do not encode graph depth.

## TaskMemory

`TaskMemory(path=":memory:", timeout=5.0)` opens a database or creates a new one. File-backed stores use WAL and FULL synchronization. A file with an unknown schema is rejected without migration. SQLite must provide FTS5. Use the object as a context manager, or call `close()`.

| Method | Contract |
| --- | --- |
| `create_task(title, parents=(), aliases=(), snapshot=None)` | Creates a task. Parents must already exist, which keeps creation acyclic. Does not resume the task. |
| `task(task_id)` / `tasks(include_archived=True)` | Returns immutable task records. The `snapshot` property is a defensive JSON copy. |
| `resolve(query)` | Matches explicit `T` IDs or whole title/alias phrases. Returns `resolved`, `ambiguous` or `not_found`. Does not write or switch. |
| `resume(task_id, expected_version=None)` | Activates the task, reopens it if archived and increments state version. Returns a dictionary with `task_id`, `active_task`, `version` and the saved `snapshot`. |
| `state()` | Returns `active_task` and the state `version`. |
| `set_snapshot(task_id, snapshot, expected_revision=...)` | Updates a JSON planner object only if its revision still matches. |
| `archive(task_id)` | Changes visibility without deleting evidence. The active task cannot be archived. |
| `lineage(task_id, max_hops=3)` | Returns the task followed by unique ancestors within the bound. |
| `delete_task(task_id)` | Deletes a leaf task, its aliases, facts, evidence and FTS entries. A task with dependents is rejected. |

Aliases are explicit application metadata. Unicode word normalization handles case and punctuation, not translation or semantic equivalence. If the query names more than one known task, the resolver does not choose one. An unknown explicit ID remains unresolved even if the query also mentions a known title. There is no calibrated confidence probability.

## Evidence and facts

`append(task_id, role, content, source=None)` stores a complete, immutable evidence message. Accepted roles are `user`, `assistant`, `tool` and `document`. Content must be nonempty. `source` must be a JSON object and can contain a document identifier, original speaker, source date or section. The runtime records its own UTC insertion timestamp separately. Source metadata is preserved but is not included in the lexical index.

`message(message_id)` returns one record, while `transcript(task_id)` returns the complete task history. `Evidence.as_record()` includes content, original role, source metadata and citation. A citation is an internal pointer, not an assertion that an external source is accurate or publicly accessible.

`set_fact(task_id, key, value, evidence_id=..., retracted=False)` adds a new revision for a task-scoped key. Its source must belong to the same task. `fact(task_id, key)` returns the latest recorded revision; `fact(..., revision=1)` reads a historical one. `current_facts(task_id)` includes retractions so applications can distinguish withdrawn facts from absent facts. No natural-language update is parsed automatically. Revision order is insertion order and is not a general bitemporal truth model.

## Retrieval and packing

`search(query, task_ids=None, limit=8)` applies FTS5 BM25 to source message text. A provided task list is an explicit filter. The method returns no results for a query with no usable terms. Results rank by BM25, with later message IDs breaking ties. Higher returned `score` is better, but it is not a probability. Limits are 1-1,000. Query terms are capped at 64. English stop words are removed; multilingual semantic retrieval and language-specific segmentation are not implemented.

`context(task_id, query, budget=2000, reserve=0, system="", counter=None, scope="lineage", retrieval_limit=24, recent=4)` returns a `ContextPackage`. Scope accepts `task`, `lineage` or `all`. The selected task's title and snapshot, current fact/source pairs, ranked evidence and recent messages compete for the input allowance. Mandatory system instructions, the evidence envelope and the full latest query are always counted. Optional atoms that do not fit are skipped intact.

The contract is `counter(package.as_messages()) <= budget - reserve`. The default counter estimates characters divided by four plus message framing. It is explicitly labeled `estimated`. A custom counter must be deterministic, side-effect-free and return a nonnegative integer for the complete message list. Its unit need not be tokens: tests also use exact UTF-8 serialized bytes. Include model chat framing in an actual tokenizer integration. Withhold any provider overhead, tool schemas and desired output allowance yourself.

The package contains `used_units`, `input_allowance`, `counting`, `citations` and `omitted_candidates`. The latter counts candidate atoms not admitted, not every item absent from the database. `as_messages()` returns a defensive copy of the exact payload counted during assembly. Mutating that copy invalidates the original size measurement.

## Hybrid memory routing

The optional `cmpath.router` module exposes `MemoryRouter` (also available as
`HybridMemoryRouter`). Its `retrieve(query, task_id=None,
requested_route=None, ...)` method selects a bounded route and returns a
`RoutedContext`. Route selection is explicit and read-only: a direct task ID is
authoritative, while an ambiguous title or alias returns a `none` decision with
its candidates instead of guessing. The result's `as_dict()` form includes the
selected route, task ID, resolution, counted payload metadata and citations.

The five route modes are deliberately small and deterministic:

| Route | What is read | Selection and safety contract |
| --- | --- | --- |
| `none` | No stored evidence | Returns the guard, an empty envelope and the caller's query. |
| `pinned` | The router's explicit in-memory task-ID pins | Does not search; pin count and evidence count remain bounded. |
| `task` | Evidence and facts from the selected task | Requires an explicit or uniquely resolved task. |
| `lineage` | The selected task and its ancestors | Keeps retrieval inside the selected task lineage. |
| `deep` | All searchable evidence | Still requires an anchored selected task; it is not an unanchored guess. |

Every route counts the complete message payload against `budget - reserve`,
keeps admitted evidence paired with its source metadata, and returns its
`Tn:Mm` citations. Route retrieval does not append evidence, resume a task,
change active state, call a model or execute a tool. Treat the returned memory
as quoted data rather than instructions.

The CLI exposes the same operation:

```text
cmpath --db state.db route "What budget was approved?" --task-id 3 --budget 2000 --json
```

Use `--task-hint TITLE_OR_ALIAS` when the caller has a human-readable task
address. Hints are resolved without changing the active task; unresolved or
ambiguous hints are not promoted to a task ID. `--route` accepts `none`,
`pinned`, `task`, `lineage` or `deep`. `--pinned-config` accepts a JSON object
(inline or from a UTF-8 JSON file) and constructs the router's typed
`RouterConfig` when available. `--pinned-input` accepts arbitrary caller JSON;
for the canonical router, a bare task ID, `{"task_id": 3,
"evidence_ids": [12]}`, or `{"pins": [{"task_id": 3}]}` adds an ephemeral
in-memory pin before retrieval. Pins retain IDs only and are never persisted.
Other JSON shapes remain available to compatible router adapters as
caller-owned input. `--budget`, `--reserve`, `--scope`, `--retrieval-limit`,
`--recent` and `--system` map to the context budget controls where supported by
the installed router; omitted values leave the router/config defaults intact.
Routing does not append evidence, resume or archive tasks, switch active state,
call a model, or make network requests. The CLI remains JSON-first even when
`--json` is omitted.

### Codex and MCP prompt routes

The Codex plugin records lifecycle events through the `cmp_codex_event` MCP
tool. That hook is an explicit write of one event. For a read-only prompt
context, call `cmp_route` with one of the five route modes:

```json
{
  "task_id": 3,
  "query": "What budget was approved?",
  "route": "lineage",
  "budget": 1600,
  "reserve": 300,
  "retrieval_limit": 12,
  "recent": 3
}
```

`cmp_route` accepts `none`, `pinned`, `task`, `lineage`, and `deep`, and returns
the serialized `decision` alongside `messages`, `used_units`,
`input_allowance`, and `citations`. `pinned_input` and `pinned_config` apply
only to that call; recognized pins are not persisted or leaked to the next
MCP request. The lower-level `cmp_context` tool remains available with
`scope=task`, `lineage`, or `all` (the Python `task`, `lineage`, and `deep`
routes). Send a returned payload only after checking
`used_units <= input_allowance`, and preserve each citation next to claims in
the answer.

Useful Codex prompts are explicit about scope and provenance:

```text
Use task scope for T3. Answer only from returned evidence and cite every claim.
Use lineage scope for T3, including parent release decisions, and abstain if
the cited evidence is insufficient. Do not append memory for this question.
```

The hook's `UserPromptSubmit` accepts the same route/task/budget options and
uses the bounded Codex router; its response includes routing diagnostics and
may include prior cited task evidence as `hookSpecificOutput.additionalContext`.
Without an explicit route, ordinary prompts stay on the tiny current-session
`pinned` working set; recall/continue language or an explicit task address opts
into `task`, `lineage`, or `deep`. Ambiguous or unknown addresses return
`none` with the resolution rather than guessing. It is still quoted context,
not a permission to execute a model or external tool. Other hook events write
evidence but do not receive prompt context.

## Transactions and recovery

Each write commits before it returns. `with memory.batch():` groups multiple writes in one transaction and uses savepoints for nested methods. Exceptions roll back the applicable group. Calls through one object are serialized; separate connections rely on SQLite locking and a bounded busy timeout. Use state/snapshot version checks when multiple writers could update the same logical state.

`backup(destination)` writes an atomic standalone SQLite snapshot using SQLite's backup API. Run it outside a write transaction. Do not copy only the main live database file while WAL writes are outstanding. The temporary snapshot is synced through a writable file handle before publication so the durability barrier also works on Windows. `export()` returns JSON data for inspection or interchange, omitting the derived full-text index. Generic JSON import is not currently exposed. Reopen a backup with `TaskMemory(backup_path)` to restore it.

`check()` validates SQLite integrity, foreign keys and agreement between the FTS index and source content. `stats()` returns record counts, state and SQLite/schema versions. These diagnostics do not prove semantic correctness or fault tolerance under every storage failure.

## Errors

Unknown records raise `KeyError`; invalid arguments raise `ValueError`; impossible context minima raise `BudgetError`; stale expected versions raise `ConflictError`. Filesystem and SQLite errors remain visible. The CLI returns nonzero status and a JSON error for ordinary operational failures. It never creates a missing database implicitly for a read command.
