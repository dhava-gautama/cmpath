# Changelog

## Unreleased

`cmp_turns.request_id` is now declared `NOT NULL` in the journal schema. A
`TEXT PRIMARY KEY` on a rowid table is nullable in SQLite, which is what let a
NULL request ID be stored on the column that every child journal table
references and every retention statement matches on. `Engine.Open` rebuilds a
database written before the constraint with the documented
create/copy/drop/rename procedure: the replacement table is the frozen target
definition rather than a reference to the current journal DDL, every row must be
copied before the commit, the partial index and both retired-ID guards are
recreated in the same transaction, foreign key enforcement is switched off
before `BEGIN` because the pragma is inert inside a transaction, and
`PRAGMA foreign_key_check` must be clean before the commit. A database whose
column already carries the constraint is probed with `PRAGMA table_info` and left
alone, so the journal DDL that runs on every open neither rebuilds repeatedly nor
accumulates an index or a trigger. The harness schema version stays 4: the
constraint only removes states the schema already admitted, so a rebuilt database
is still read correctly by an older binary, and a bump would turn that compatible
tightening into a hard refusal; `info` reports `cmp_turns_request_id_not_null` to
tell the two states apart instead. A database that already contains a NULL
request ID is refused on open with `integrity` naming the affected row count --
the engine never deletes journal evidence on its own, so those rows are retired or
repaired explicitly before the database opens again.

Native retention now converges instead of wedging on a request ID that already
has a tombstone: `ApplyRetention` inserts the tombstone with `INSERT OR IGNORE`,
so a row retired out of band is an idempotent conflict rather than a
`UNIQUE constraint failed` that made every apply fail while `Turn()` still
returned the live row. An existing tombstone is never rewritten -- its
fingerprint, status and `retired_at` are preserved. Each candidate is re-read
after its deletes in the same transaction, and apply returns the new coded error
`integrity` -- rolling the whole transaction back -- if any of the seven journal
tables still holds a row or the tombstone is not exactly one. The plan hash now
also covers the tombstones that already exist for the candidate IDs, so one
inserted between dry run and apply is a `stale_plan` rather than an invisible
change.

A second `BEFORE UPDATE OF request_id` trigger on `cmp_turns` closes the reuse
hole the INSERT-only guard left open: renaming a live turn onto a retired ID is
now aborted with the same `retired request ID cannot be reused` message, so a
retired ID can neither be inserted nor renamed back into execution. Both triggers
are created with `IF NOT EXISTS` from the journal DDL that every `Open` runs, so
the guard applies to fresh and existing databases alike. The engine still never
prunes a tombstone; that remains engine behavior rather than an enforced
invariant, and the docs now say so.

`check()` now reports database damage as a verdict instead of raising: the
`PRAGMA` reads, the FTS5 integrity-check command and the closing commit each
fold their `sqlite3.DatabaseError` into the returned `sqlite` or `fts` fields,
with `fts` reporting `not checked` when the pragmas fail first. SQLite
connections apply a 10000 ms `PRAGMA busy_timeout` floor over the `timeout`
constructor argument (WAL readers never block, but two writers serialize, and
5 s is shorter than the maintenance/hook write horizon); a caller may ask for
longer, never less.

Adds the autosave subsystem (`cmpath.autosave`, console script
`cmpath-autosave`): automatic, redacted, deduplicated capture of Kimi Code
session evidence. Hook mode captures the turn prompt and ingests `wire.jsonl`
transcripts idempotently behind parser-stamped per-path cursors, with
`save_turn()` committing its message row and dedup state in one atomic batch.
`--heal` re-ingests every recorded wire from line 0 to close holes the cursors
skipped, `--doctor` is a strictly read-only health check, and `--doctor --deep`
digest-audits every recorded cursor against the stored evidence. A thin
`scripts/autosave_session.py` shim remains the installable hook command. The
suite grows by 154 autosave tests.

Native row decoding is now type-faithful. `BLOB` decodes to `[]byte` instead of
through the text interface, which had made a blob `request_id` indistinguishable
from a text one; SQLite never compares a blob equal to text, so a retention
apply matched no rows in its tombstone insert or its seven deletes yet reported
`applied: true`, and two different blobs produced one plan fingerprint. Values
the driver cannot represent faithfully fail closed: `Row.Text`, `Row.Int`,
`Row.Float` and `Row.Blob` return a coded `*sqlite.TypeError` for a NULL,
absent or wrongly-typed column rather than coercing it, and `Engine.Code`
classifies that as `integrity`. The stdio bridge contains a panic in a single
operation as a coded `internal_error`, its transaction already rolled back, so a
malformed row can no longer terminate the host's persistent channel as it did
before this change (`panic: interface conversion: interface {} is nil, not
string`, process exit code 2). The Go connection now installs a documented
10000 ms busy-timeout floor, matching the Python connection, raisable through
`engine.WithBusyTimeoutMS` or `--busy-timeout-ms` and never lowerable; a
`SQLITE_BUSY` result, including `SQLITE_BUSY_SNAPSHOT`, is reported as `busy`
instead of `storage_error`, so a caller can retry contention rather than read it
as damage. The Go suite gains storage-class, timeout-floor and held-write-lock
tests.

## 0.4.0a5 — 2026-09-14

Consolidates the 0.4.0a4 safety controls with the portable MCP integration,
authenticated managed-turn control plane, dependency-free Python/TypeScript/Go
SDKs, explicit adapter guides, offline conformance runner, Kimi/Hermes
configuration bundle, and local doctor diagnostics. Adds a read-only release
integrity verifier that compares current-version wheel, source-distribution,
portable-bundle, and optional Codex plugin payloads with canonical source and
validates wheel RECORD hashes. The portable bundle is now included in source
distributions.

On 2026-09-17 this alpha added Windows AMD64 validation, automatic MSYS2 UCRT64
GCC discovery, deterministic release bundles and checksums, Windows CI, and a
bounded hybrid memory router with `none`, `pinned`, `task`, `lineage`, and
`deep` routes. `RoutedHarness` connects selected context to the existing
five-boundary recovery journal. Subsequent hardening makes streamable HTTP
loopback-only until an authenticated remote provider exists, makes backup
publication atomically no-replace, canonicalizes route aliases, and adds a
matching Linux CI matrix. The integrated Windows run passes 168 Python tests
with three intentional skips and all 17 isolated release checks; the Linux/WSL
run passes the same 168 tests with platform-specific skips.

The source is public at <https://github.com/dhava-gautama/cmpath>. The live
SumoPod run is documented separately in LIVE_RESULTS.md and remains a
bounded synthetic workflow, not a general quality or profitability claim.
Native binaries still require a target-specific rebuild; PyPI and hosted
service publication are not claimed.

## Live validation supplement — 2026-09-11

Records failed access probes to the supplied provider: direct DNS failure and authenticated proxy HTTP 403 / Cloudflare 1010. No model completion or usage was received. Adds a bounded provider probe, complete paired reader and document-agent/recovery runner, exact citation-ID diagnostics, and five local HTTP/native tests. The Python suite now passes 82 tests. Runtime code, native binaries, original local study and 0.4.0a2 distribution artifacts are unchanged. See LIVE_RESULTS.md.

## 0.4.0a2 — 2026-09-11

Adds an installed research-agent CLI with a real configured HTTP tool loop, root-confined document tools, complete request counting, exact provider-response persistence and controlled recovery. Adds schema-2 model response journals and request tombstones, coherent JSONL export, reviewed-plan retirement and an installed maintenance CLI. A database trigger blocks retired-ID reinsertion even by an already-open older engine.

Adds 40 synthetic matched baseline cases (80 rows) and 8 separate recovery diagnostics. Both CMP and task-filtered SQLite/BM25 recover the required source in 32/40 cases; no retrieval advantage is claimed. Includes an optional matched live-reader runner, not invoked. Default token accounting remains estimated; a local command counter adapter is available.

The release records 77 Python tests, 22 Go race-tested cases, old-database migration, clean installed CLIs and source rebuild. Linux x86-64 is the validated platform.

## 0.4.0a1 — 2026-09-11

Adds a Go engine for direct embedding in an agent harness and a persistent stdio adapter exposed as Python `NativeHarness`. Includes a separately versioned SQLite turn journal, stable logical request IDs, exact saved contexts, generation fencing on recovery, atomic reply/snapshot/fact commits, durable tool intent and results, and complete provider-request counting and audit hooks.

The native engine shares the existing version-1 task/evidence schema with Python. It preserves source roles and text, citation identifiers, large JSON integers and Unicode normalization. Model and tool callbacks run outside database transactions. Real checksum examples demonstrate both deployment paths; a caller-configured text-generation example records the full outgoing request.

Adds 16 Go tests exercised with the race detector and 14 Python integration tests, bringing the Python suite to 56. The native study records 900 timed searches and 200 lifecycle pairs. All 600 native ordered-hit comparisons match Python. No search speed improvement or live-model quality result is claimed.

Vendors SQLite 3.53.4 and `golang.org/x/text` 0.42.0, with provenance and license notices. Ships Linux x86-64 native binaries, source, Python wheel, source distribution and reproduction scripts. This is an alpha: no Rust implementation, native API parity for every Python storage operation, journal pruning, universal binaries or package-index publication is claimed.

## 0.3.0rc1

New product API built around a single durable SQLite database. Adds explicit tasks, title/alias resolution, separate resume operations, task dependency scope, source-linked fact revisions, optimistic planner updates, indexed BM25 evidence search, complete-message context counting, standalone backups, JSON export and a JSON-first CLI.

Includes a version-2 checkpoint converter, 42 automated tests, 5,991 public-data retrieval evaluations, 320 operational probes, 300 scaling queries, a cited research paper, raw measurements and release distributions. No model answer-quality result is claimed. This is a breaking revision from the earlier CMPSession research harness.
## 0.4.0a4 — 2026-09-12

Adds explicit `Reply.provenance` evidence references for scoped derived snapshot
writes. Sources are checked for existence, scope admissibility and retraction in
the same transaction as commit; missing annotations fail closed with
`provenance_required`. Adds `action_eligible`, a short-lived revalidation
certificate automatically used by native and Python session tool helpers. The
certificate narrows the pre-effect race and records its expiry, but does not
claim atomicity for an external service that does not participate in the
transaction. Exports and retention now include the new journal tables. The
remaining TOCTOU rate is an explicit benchmark target.
