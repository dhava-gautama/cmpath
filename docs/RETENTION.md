# Journal export and retention

Journal retention removes old execution details. It preserves tasks, messages,
facts, dependencies, aliases, runtime state, and events, including tool evidence.

## Export

`Engine.Export(io.Writer)` produces version 1 `cmpath-journal-jsonl`, a UTF-8
JSON-lines inspection/interchange archive. `Engine.ExportFile(path)` writes to a
new caller-selected file, never intentionally overwriting an existing
destination. An owner-only temporary file is synced and atomically published
with a hard link in the destination directory when supported. On a POSIX
filesystem that rejects hard links (for example a WSL DrvFs-mounted Windows
drive), the exporter exclusively reserves the absent destination with a
`cmpath-export-reservation-v1` marker, syncs that marker, and renames the
complete temporary archive over it. Native Windows uses its no-replace rename
path directly. Concurrent `ExportFile` calls therefore still cannot replace an
existing destination. The fallback is weaker than hard-link publication for
non-cooperating writers: there is no portable compare-and-rename primitive,
so a writer that ignores the reservation could race the final rename.

If the process is interrupted after a POSIX reservation is synced but before
rename, the destination can contain the recognizable reservation marker rather
than an archive. It is safe to treat that marker as incomplete, confirm that
no exporter is still running, remove it, and retry. A normal write or rename
failure removes its own marker when it can verify that the path still contains
the exact marker. Readers never see a partially written archive; the marker
window and lack of directory-fsync support on some mounts are the documented
reduced crash-consistency guarantee. NTFS supports the hard-link path; FAT,
exFAT, some network shares, and other filesystems may use the fallback.

The header contains the version and SQLite schema definitions. Row records
contain a table name and every original column. The footer contains per-table
row counts. Tables include tasks, dependencies, aliases, messages, facts,
runtime state, events, SQLite ID sequences, harness metadata, turns, tool calls,
model requests, model responses, and retired-request tombstones. Derived FTS
storage is omitted; a future importer would rebuild its index from messages.

One transaction provides a coherent snapshot. Ordering is deterministic for an
unchanged database, with no current timestamp added. JSON-valued text columns
remain strings, preserving exact whitespace, large integer spellings, Unicode,
NULs, and original provider content. SQLite integers are emitted as exact JSON
integer tokens; consumers must avoid converting them to floating point. A
column holding a SQL BLOB is emitted as base64 rather than being coerced into
text, so its storage class is not silently changed into a form that would no
longer compare equal to the stored value. Export streams one row at a time,
bounding buffering to the largest row. Use file output for large archives
instead of including their contents in stdio replies.

**No archive importer exists. This is not a resumable backup format.** Use
SQLite's online backup API or a SQLite-aware backup tool for recovery. Copying
only a live database's main file can omit committed WAL data. Archives contain
original prompts, responses, tool arguments/results, and evidence.

## Two-step retention

1. Call `Engine.RetentionPlan(cutoff)` using an RFC3339 timestamp with timezone.
   Review its counts and retain the returned `cutoff` and `plan_hash`.
2. Explicitly call `Engine.ApplyRetention(cutoff, plan_hash)` to apply the exact
   plan. Changed candidates or journal contents produce `stale_plan` and delete
   nothing. Review a fresh dry-run plan before retrying.

Only committed or aborted turns with `updated_at` strictly before the cutoff
are eligible. Pending turns are always protected. Terminal turns with any
unresolved `started` tool are also protected: aborting does not resolve whether
an external effect occurred. Timestamp comparison parses fractional seconds and
timezone offsets rather than comparing strings.

Report `rows` counts describe journal rows to remove, except
`cmp_retired_turns`, which counts tombstones to create. It does not distinguish
a tombstone that will be created from one that already exists: both dry run and
apply report one per candidate. A candidate whose tombstone already exists is an
idempotent case, not a second tombstone. `protected_pending` and
`protected_unresolved` count protected turns regardless of age. `applied` becomes
true only after successful commit. Candidate IDs are not returned, keeping
protocol output bounded; the hash binds the complete turn and child rows for
every candidate, plus any tombstone those IDs already have.

Apply recomputes the plan under the same write transaction as deletion. It
inserts permanent request-ID/fingerprint/status tombstones before deleting model
responses, model requests, tool calls, and turns. A tombstone for a candidate
that already exists is left exactly as it is -- `INSERT OR IGNORE` treats it as
an idempotent conflict, so an out-of-band retirement no longer wedges the apply
in a UNIQUE violation. A pre-existing tombstone therefore keeps its own
fingerprint, status and `retired_at`; the write path never silently rewrites
them. After the deletes, each candidate is re-read inside the same transaction:
every one of the seven journal tables must be empty for that ID and exactly one
tombstone must exist. If any row survives, apply returns the coded error
`integrity` and the transaction rolls back rather than reporting `applied=true`.
The plan hash covers the candidate parent/child rows and their existing
tombstones, so a surviving row after a matching plan hash is evidence that a
delete did not converge rather than a change to the plan.

Begin rejects a retired ID with `retired` for matching input or `conflict` for
different input. The engine's retention path only inserts tombstones; it never
deletes one. This is engine behavior, not an enforced invariant: a caller that
issues its own SQL can still remove a tombstone, and no trigger prevents that.
Tombstones are never pruned by the engine, preserving durable deduplication
against replayed execution.

The engine matches a turn by its `request_id` value, so that column must be
stored as SQL text. A row whose `request_id` has any other storage class cannot
be read as a request ID: the plan fails with a coded error instead of reporting
a success that deleted nothing, because SQLite never compares a blob equal to
text. Check for such rows with
`SELECT rowid, typeof(request_id) FROM cmp_turns WHERE typeof(request_id) IS NOT 'text'`
before applying retention.

`cmp_turns.request_id` is declared `NOT NULL` as well. A `TEXT PRIMARY KEY` on a
rowid table is nullable in SQLite, which is how a NULL request ID could be
stored at all; the constraint removes the state rather than relying on every
reader to cope with it. `Engine.Open` rebuilds a database written before the
constraint with the documented create/copy/drop/rename procedure and skips the
rebuild once the column carries it, so the journal DDL that runs on every open
stays a no-op and no index or trigger accumulates. The rebuild switches foreign
key enforcement off before it begins -- the pragma is inert inside a transaction
-- because `DROP TABLE cmp_turns` would otherwise run the implicit delete that
the child tables' `ON DELETE RESTRICT` refuses, and it re-inserts every row
before committing, so no child row is lost or orphaned. A database that already
holds a NULL request ID is refused with `integrity` instead of migrated, because
the engine treats removal as an explicit audited operation: the refusal names
how many rows are affected, and
`SELECT rowid, typeof(request_id) FROM cmp_turns WHERE request_id IS NULL`
finds them. Retire or repair those rows yourself and reopen. `info` reports
`cmp_turns_request_id_not_null`, which distinguishes a rebuilt database from one
that still admits a NULL while the harness schema version stays 4.

Retention and export wait for a competing writer for up to the busy-timeout
floor (10000 ms; see [../native/README.md](../native/README.md) to raise it) and
then fail with `busy`, which is retryable. They do not retry by themselves, and
a large export or retention can exceed the floor while it holds the write lock,
so a caller that collides with maintenance should treat `busy` as contention
rather than damage and retry after the maintenance operation finishes.

Deletion frees SQLite pages for reuse; it does not promise smaller database/WAL
files, secure erasure, or deletion from previous backups or exports. Physical
compaction and backup lifecycle management are separate. No automatic VACUUM or
evidence deletion occurs. Export and retention hold a transaction and serialize
this engine connection throughout; schedule large maintenance accordingly.

The installed `cmpath-maintain` command exposes `info`, `export`, `plan` and explicit `apply` operations. Python equivalents are `harness.export_journal(path)`, `harness.retention_plan(cutoff)` and `harness.apply_retention(cutoff, plan_hash)`. Increase the configured native deadline for a large export; an interrupted operation may leave a private temporary export file or, on the POSIX fallback, a reservation marker, but never publishes a partial archive. A plan hash is not a durable token: it describes one journal state, so a caller that retries hours later should repeat the dry run rather than reuse an old hash.

The journal tables carry `BEFORE INSERT` and `BEFORE UPDATE OF request_id` triggers on `cmp_turns` that abort with `retired request ID cannot be reused`. A turn can therefore neither be inserted with nor renamed onto a retired ID, even by an older engine connection that predates tombstones or by direct SQL. Both guards, and the partial index that keeps one pending turn per task, are dropped with the table when `Engine.Open` rebuilds it for the `NOT NULL` upgrade, so the rebuild recreates all three in the same transaction and commits only after `PRAGMA foreign_key_check` is clean. That covers `cmp_turns` only: the six child journal tables have no retired-ID trigger of their own. They are held in place by ordinary foreign keys onto `cmp_turns`, which SQLite enforces only on connections that set `PRAGMA foreign_keys=ON`. The engine sets it on every connection it opens, so the residual write path is an external connection that leaves it off.

A native `ApplyRetention` holds one write transaction while it issues `1 + 7n` statements for `n` candidates. At 20,000 candidates that is roughly 11 seconds of write-lock time on a warm local database, and `RetentionPlan` needs roughly 8 seconds to recompute the same hash. Both exceed a short busy timeout on a contended database; callers should treat a `busy`/timeout failure as retryable after the competing writer finishes, and schedule large maintenance when nothing else writes. The operation is not chunked, so a failed or `stale_plan` apply still deletes nothing.
