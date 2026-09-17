# Changelog

## 0.4.0a5 — 2026-09-14

Consolidates the 0.4.0a4 safety controls with the portable MCP integration,
authenticated managed-turn control plane, dependency-free Python/TypeScript/Go
SDKs, explicit adapter guides, offline conformance runner, Kimi/Hermes
configuration bundle, and local doctor diagnostics. Adds a read-only release
integrity verifier that compares current-version wheel, source-distribution,
portable-bundle, and optional Codex plugin payloads with canonical source and
validates wheel RECORD hashes. The portable bundle is now included in source
distributions.

The live SumoPod run is documented separately in LIVE_RESULTS.md and remains a
bounded synthetic workflow, not a general quality or profitability claim.
Native binaries still require a target-specific rebuild; package and source
artifacts are not published by this repository.

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
