# Autosave — automatic session memory for Kimi Code

Every Kimi Code session can save its key content into a cmpath memory
database with no agent discipline required: the `cmpath-autosave` console
script (or the `scripts/autosave_session.py` shim that wraps it) is installed
as a hook for three events (`TurnStarted`, `Stop`, `SessionEnd`),
captures the payload prompt, and opportunistically ingests the conversation
transcripts that Kimi Code already writes to disk. Every event runs the same
idempotent pass, so whichever one fires first for a given turn wins and the
later ones add nothing. Evidence is redacted, deduplicated and
citation-addressed (`Tn:Mm`).

Stdlib-only, Python 3.10+. Default database: `~/.local/share/cmpath/kimi.db`.

## How it works

```
Kimi Code turn starts, ends, or the session ends
  └─ TurnStarted / Stop / SessionEnd hook → cmpath-autosave
       ├─ payload prompt  ─→ Autosave.save_turn()   → role="user" evidence
       └─ scan <session>/agents/*/wire.jsonl
            ├─ turn.prompt inputs            → role="user" evidence
            ├─ content.part (type="text")    → role="assistant" evidence
            └─ tool.call / tool.result       → role="tool" evidence
                 (redacted, capped at TOOL_MAX_CHARS; think parts are dropped)
```

- **Project tasks.** All evidence for a working directory lands on one task,
  title `Autosave: <basename(cwd)>`, alias `auto:<basename>` (alias stored
  normalized as `auto <basename>`). Two sessions in the same project share
  the task; an empty cwd maps to `auto:unknown`.
- **Provenance.** Hook prompts: `source = {"session", "kind": "prompt",
  "turn", "origin_kind", "origin_name", ["truncated"]}`. Transcript lines:
  `source = {"session", "kind": "wire", "path", "line", "agent",
  ["truncated"]}` where `line` is the first line of a coalesced group or a
  `"first-last"` range. Tool rows use this same wire shape, so a stored call
  points at the line it was read from (`"412"`) or at the range that covers a
  coalesced call+result pair (`"412-413"`).
- **Step coalescing.** Assistant text arrives in `content.part` events as
  streaming deltas; saving them verbatim produced fragment evidence
  (`"rank 1."`, `"retrieval"`). Deltas sharing `(turnId, stepUuid)` are
  joined into one whole message before storage (`iter_wire_coalesced`);
  events without a `stepUuid` get a per-line key and are never merged.
  Complete short messages are kept — coalescing undoes the splitting, it
  does not filter content.
- **Tool traffic.** Every `tool.call` and `tool.result` in a wire becomes a
  `role="tool"` message through the same `memory.append()` path as user and
  assistant text; no schema change was needed. A call renders its tool name,
  `toolCallId`, the first detail key present (`command`, `path`, `file_path`,
  `pattern`, `query`, `url`, `prompt`) and the remaining arguments as compact
  JSON; a result renders its status (`tool result`, or `tool result: error`),
  `toolCallId`, an optional `note` and the output. A call and the result that
  immediately follows it share a `toolCallId` key and coalesce into one row,
  so `source.line` can span both. Coalescing only joins *consecutive* events:
  parts emitted between a call and its result leave the pair as two rows.
  The `display` field of a result is never stored.
- **Dedup** lives in one per-task fact, `autosave_state`:
  `{"seen": ["<session>:<turn>" …], "hashes": [sha1[:16] of stored redacted
  content …], "cursors": {"<path>": {"line", "mtime", "size", "head",
  "complete", "parser"}}, "last_evidence"}`, where `head` is `sha1[:16]` of
  the file's first 512 bytes, `complete` says whether the pass that wrote the
  cursor reached the end of the file, and `parser` is the version of the wire
  parser the cursor was written by (see the next bullet).
  `seen` (last 500) blocks a re-delivered hook turn; `hashes` (last 1000)
  also blocks the same content arriving via the other path — the prompt the
  hook saved is not stored again when the transcript later reaches it.
  Identical redacted content is therefore stored once per project task.
- **Atomic turn save.** `save_turn()` appends the prompt row and writes the
  `autosave_state` fact that records its key and digest inside one
  `memory.batch()`, the same single-commit shape `ingest_transcript()` uses
  for its rows and cursors. The write is all-or-nothing: an error in either
  half rolls both back. Before, the two halves committed separately, so a
  crash between them left a task with messages but no state fact — the
  doctor's exit-1 rule 4 — and the eventual recovery re-scan ran from line 0
  without the stored-digest filter, which can duplicate up to 500 rows per
  pass. A save that appends nothing (an empty prompt, a duplicate turn)
  still writes no fact, exactly as before.
- **Idempotent ingest.** A per-path cursor records the last fully parsed line
  plus the file's mtime, size and head at scan time. An unchanged file
  (same mtime and size) is skipped without being read — but only when the
  cursor is `complete`. One pass stores at most 500 rows, and a pass that
  stops at that cap (or at the deadline) writes `complete: false`, so the
  next run resumes the path instead of mistaking the file for finished. A
  torn final line (writer mid-append) is retried next turn.
- **Truncation and rewrites.** A transcript is only resumed past its cursor
  when it genuinely grew: a strictly longer file with an unchanged head is an
  append, and an identical mtime *and* size is the same file resuming a pass
  that stopped at the cap (only a `complete` cursor short-circuits there).
  Anything else — truncated, rewritten in place, or replaced by a different
  file, including a same-size rewrite, which is detected by the changed mtime
  because the size cannot show it — resets the cursor to line 0 and re-scans
  the whole path, so new content is never silently skipped. The hash list
  keeps the re-scan from duplicating rows, and past its 1000-entry window it
  is no longer enough, so every re-scan that starts at line 0 — a rebuild or a
  cursor reset — additionally filters against the digests already stored for
  the path. Cursors written by older versions (`{"line", "mtime"}` or a bare
  integer) upgrade themselves to the current form on their first re-scan.
  `--doctor` applies the same trust test before it calls a cursor `current`:
  a cursor in the old format becomes the informational `legacy` status, and
  any other cursor it cannot confirm becomes `provisional` (see the
  staleness rule).
- **The parser stamp.** Every cursor records the version of the wire parser
  that wrote it (`parser` in the cursor dict). The contract: whenever a
  change to the parser — how it prepares content, or what it extracts from a
  wire line — would leave already-recorded cursors pointing past content the
  previous version did not store, the version is bumped. This exists because
  of a real incident: the parser began capturing tool traffic, but
  the cursors already recorded kept advancing past the lines the old parser
  had skipped, and every later pass resumed from them — silently leaving
  ~11k messages un-stored below the cursors, recoverable only by a manual
  `--heal`. A cursor stamped with a different version than the running
  parser — or with no version at all, which is what every cursor written
  before the stamp looks like — is therefore never resumed. The next ingest
  takes the same path a rebuild takes: it re-scans the wire from line 0 and
  filters every row against the digests already stored for the path, so the
  re-scan backfills what the older parser skipped and cannot duplicate
  anything. The report carries `reset_cursor: True` for that pass, and the
  cursor it writes is stamped with the current version, so the migration
  happens exactly once per path. A version mismatch is not a fault and not a
  rewrite: it is a dedup-safe re-scan, and `--doctor` still reports such a
  cursor as trusted — resuming is never what happens to it, and the doctor's
  trust test answers whether the wire on disk is still the file the cursor
  was written from, which a missing or stale stamp says nothing about.
- **Rebuild.** `ingest_transcript(..., rebuild=True)` (script: `--rebuild`)
  ignores the recorded cursor and re-scans the path from line 0. Nothing is
  deleted: stored evidence is append-only, so a rebuild cannot destroy rows.
  The re-scan appends only rows whose content this path has not stored yet,
  checked against the bounded `hashes` window *and* the stored wire rows of
  the path, so repeating a rebuild converges to the same rows however long
  the file is (each individual pass still appends at most 500). The returned
  report carries `reset_cursor: True` whenever the scan restarted at line 0.
- **What is skipped:** `think` parts, attachments, and empty content —
  content-free events that render no message. Tool traffic **is** kept: see
  the tool-traffic bullet above. It is redacted and capped like everything
  else.
- **Budget.** The hook always exits 0, prints nothing, and caps transcript
  work at ~8 s of its 10 s timeout. Any failure is swallowed (fail-open).

## Redaction contract

`cmpath.autosave.redact()` runs on **every** stored string, even content
that looks safe. Replacements, in application order:

| Kind | Pattern class |
|---|---|
| `[REDACTED:anthropic]` | `sk-ant-…` tokens |
| `[REDACTED:openai]` | generic `sk-…` tokens (16+ chars) |
| `[REDACTED:google]` | `AIza…` (39-char API keys) |
| `[REDACTED:github]` | `ghp_ gho_ ghu_ ghs_ ghr_` tokens |
| `[REDACTED:github_pat]` | fine-grained `github_pat_…` tokens |
| `[REDACTED:huggingface]` | `hf_…` tokens |
| `[REDACTED:gitlab]` | `glpat-…` tokens |
| `[REDACTED:slack]` | `xoxb/xoxa/xoxp/xoxr-…` tokens |
| `[REDACTED:aws]` | `AKIA…` / `ASIA…` access key ids |
| `[REDACTED:jwt]` | three-part `eyJ…` JWTs |
| `[REDACTED:bearer]` | `Bearer <token>` authorization values |
| `[REDACTED:keyval]` | generic `…KEY=`, `…TOKEN:`, `…SECRET=`, `…PASSWORD=`, `…CREDENTIAL=` assignments (value kept out, 8+ chars) |
| `[REDACTED:private_key]` | whole `-----BEGIN … PRIVATE KEY-----` blocks |
| `[REDACTED:hex64]` | bare 64-hex strings (digests, raw keys) |

Redaction is conservative, not infallible: arbitrary base64 blobs, novel
token formats, or secrets split across lines can slip through. Treat the
database as sensitive anyway.

Single messages are truncated to `MAX_MESSAGE_CHARS` (4000 characters); the
evidence `source` then carries `"truncated": true`. `tool` rows are truncated
to `TOOL_MAX_CHARS`, which is deliberately the same 4000 — the two constants
must stay equal, because `--doctor`'s pending-line check re-hashes
`content[:MAX_MESSAGE_CHARS]` for every wire row, so a lower tool cap would
leave those files looking permanently un-ingested.

## What gets saved — and what cannot be

Saved automatically:

- every user prompt (from the hook payload, the moment the turn starts),
- assistant text output per agent, ingested from the transcripts Kimi Code
  already writes (`agents/main/wire.jsonl` for the main session, same for
  each subagent),
- tool traffic — every `tool.call` and `tool.result`, one row per call+result
  pair where the two events are adjacent, each redacted and capped,
- thinking content is deliberately **not** saved; it is the one large stream
  that stays out.

Measured 2026-09-14 on one real session (36 wire files, 49.8 MB of
transcripts) copied into a throwaway database — never the live one:

| | before tool capture | after |
|---|---|---|
| rows | 614 (user 58, assistant 556) | 6,447 (user 57, assistant 526, tool 5,864) |
| stored bytes | 341,515 — 0.7 % of the wire | 9,594,794 — 19.3 % |
| database file | 1,339,392 B | 17,833,984 B |
| ingest wall time | 3.8 s | 19.6 s |

Of the 5,864 tool rows, 1,078 are truncated by the cap; those rows keep 55.5 %
of the 16.7 MB of text the parser renders for the events and drop 44.5 %.
Expect the tool rows to dominate a large project task's size from now on.

Not saved, honestly:

- **Nothing is captured in real time from within the turn.** A turn's own
  traffic is still ingested on the *next* turn's `TurnStarted`: `Stop` fires
  at turn end, when that turn's wire lines are not yet reliably on disk, so
  what `Stop` copies is gain only if the wire had already caught up. The real
  gain is the session's *last* turn — `SessionEnd` runs the same pass after
  the session is done, so the final turn of a session no longer waits for
  some later session (or a manual `--ingest`) to be archived. Wires whose
  lines land after their session's `SessionEnd` are still missed.
- Tool output is capped, not complete: a long `Bash` result or file dump is
  cut at 4000 characters and flagged `"truncated": true`. Whatever the
  redaction patterns miss inside that stored text stays stored — command
  output can carry a secret, so this database is now at least as sensitive as
  the transcripts it copies.
- Sessions whose payload `session_id` cannot be matched to a session
  directory still get their prompt saved, but no transcript ingest.

Where main-session text actually lives (verified on this machine):
`~/.kimi-code/sessions/wd_<slug>_<hash>/session_<uuid>/agents/main/wire.jsonl`.
`state.json` only holds the latest prompt and title; `~/.kimi-code/logs/`
holds app diagnostics, not conversation; `~/.kimi-code/audit.sqlite` holds
bash `commands` only (its `turns` table is empty because the hook was not
installed when those turns ran).

## Install

```
python3 scripts/install_autosave.py --print   # show the block, change nothing
python3 scripts/install_autosave.py           # backup + append + verify
python3 scripts/install_autosave.py --uninstall
```

The installer appends exactly this managed block to
`~/.kimi-code/config.toml` (after a timestamped `.bak` backup; a second
install is refused):

```toml
# >>> cmpath-autosave managed block >>>
[[hooks]]
event = "TurnStarted"
command = "python3 <repo>/scripts/autosave_session.py"
timeout = 10

[[hooks]]
event = "Stop"
command = "python3 <repo>/scripts/autosave_session.py"
timeout = 10

[[hooks]]
event = "SessionEnd"
command = "python3 <repo>/scripts/autosave_session.py"
timeout = 10
# <<< cmpath-autosave managed block <<<
```

`--print` writes exactly that region and touches nothing. Install refuses
when either marker comment is already present, so a *marked* config still
holding only the older single-event (`TurnStarted`-only) block reads as
installed: upgrading that one means `--uninstall` followed by a fresh
install.

A hand-added **marker-less** block — a top-level `[[hooks]]` section running
`autosave_session.py` that sits outside the marker comments — is a different
case. Install recognises it and replaces it in the same pass (it prints
`replaced the legacy marker-less autosave hook` before the usual lines), and
`--uninstall` removes it along with the marked region. A section running any
other script is never recognised and never touched.

Manual uninstall: delete the block between (and including) the two
`cmpath-autosave` marker lines. A marker-less section running the managed
script can be deleted by hand the same way, or left to `--uninstall`.

## Using it by hand

```
# what would the hook do right now? (writes nothing, not even the db file)
cmpath-autosave --dry-run --db ~/.local/share/cmpath/kimi.db

# one-shot ingest of a transcript
cmpath-autosave --ingest \
  ~/.kimi-code/sessions/wd_rocket_35f48ac79de6/session_48540d9c-…/agents/agent-94/wire.jsonl \
  --session-id 48540d9c-… --cwd /mnt/hdd/rocket

# re-ingest every recorded wire from line 0, healing holes the cursors skipped
cmpath-autosave --heal

# internal checks
cmpath-autosave --selftest

# read-only health check (alias: --status); --json for machine output
cmpath-autosave --doctor
```

## Diagnosing a silent failure (`--doctor`)

The hook fails open and prints nothing, so a broken install looks exactly
like a quiet one. `--doctor` (alias `--status`) inspects the automation and
says what is wrong. It is **strictly read-only** and safe to run at any time.

```
cmpath-autosave --doctor
cmpath-autosave --status --json
cmpath-autosave --doctor --db /tmp/copy.db \
  --config /tmp/config.toml --sessions-root /tmp/sessions --max-age 86400
```

| Flag | Meaning |
|---|---|
| `--doctor`, `--status` | run the health check |
| `--deep` | with `--doctor`: digest-audit every recorded cursor (slow — see below) |
| `--json` | machine-readable report instead of text (also for `--heal`) |
| `--config PATH` | config to inspect (default `~/.kimi-code/config.toml`) |
| `--sessions-root PATH` | sessions root for cursor reconciliation and `--heal` (default `~/.kimi-code/sessions`) |
| `--max-age SECONDS` | staleness window (default `604800` = 7 days) |

What it reports:

- **Hook.** Which of the three managed events (`TurnStarted`, `Stop`,
  `SessionEnd`) has a `[[hooks]]` entry whose command resolves to this script
  (realpath first, basename `autosave_session.py` as a fallback), reported per
  event on an `events:` line, plus the `timeout` and the exact matched command
  of the primary entry. The hook counts as installed if *any* managed event
  matches, so a legacy `TurnStarted`-only install is healthy — the events it
  is missing are simply named after `missing:`. An entry for a managed event
  that points at some other command is a mismatch, not an install. TOML is
  parsed with the stdlib `tomllib`; on Python 3.10 (no `tomllib`) it falls
  back to a text scan and says so.
- **Database.** The resolved `--db` path, whether the file exists, its size,
  and its tables. `messages` must be present — it is the evidence table, so
  there is no separate `evidence` table to report. The report also counts the
  messages overall and breaks them down by `role` and by `source.kind`.
- **Tasks.** Every task carrying the hook's creation signature: a title that
  starts `Autosave:` **and** a paired alias `auto <slug>` (the normalized
  form the hook stores for `auto:<slug>`), where `<slug>` is the title's
  text after the prefix. Reported with its message count and newest message.

  The conjunction is required, not either half alone. The hook creates each
  project task in one call with both halves (see
  `Autosave._project_task_by_name`), so the pair is its creation signature —
  but the CLI lets a human create any title and any alias, so either half
  alone is forgeable. A task holding only one half is never hook-managed:
  nothing ever writes its `autosave_state` fact, and before the conjunction
  was required the doctor's "messages but no current `autosave_state` fact"
  rule fired on such a task forever — a permanent red light masking real
  problems. The same predicate governs `--heal`'s task enumeration and the
  report's autosave counts, so all three agree. (Every task in the live
  database was verified to carry both halves, so tightening the match
  enumerates exactly the same real tasks; a hook task whose alias is later
  deleted by hand falls out of the enumeration — acceptable, since the hook
  resolves tasks by alias and would simply create a fresh task on the next
  save rather than keep writing to the tampered one.)
- **Last write.** Derived from `MAX(messages.created_at)` over the autosave
  tasks — stated in the output so the number is not mistaken for something it
  is not. Timestamps are UTC ISO-8601.
- **State.** For each task, the current `autosave_state` fact (revision, seen
  and hash counts, when it was written), plus each recorded cursor reconciled
  against the wire file on disk: how many messages are still un-ingested and
  how old that file is. A cursor is reported as `current` (nothing left),
  `legacy` (written in the pre-size/head format — informational, see the
  staleness rule), `pending` (un-ingested content on a fresh wire), `stale`
  (the same, on a wire older than `--max-age`), `unresolved` (its path cannot
  be found), `unreadable` (the file cannot be read), or `provisional` — see
  below.

### Staleness rule

Exit code **1** (stale/unhealthy) when any of these hold:

1. the hook is not installed, or its timeout is outside the sane range
   (`8`–`300` s);
2. the database file does not exist;
3. no autosave task exists;
4. an autosave task has messages but no current `autosave_state` fact;
5. the newest autosave message is older than `--max-age`;
6. a cursor's wire file has ingestable content beyond the recorded line
   **and** that file's mtime is older than `--max-age`;
7. a cursor's recorded line cannot be trusted to describe the wire it is
   compared against, so the next ingest re-scans that wire from line 0 and the
   recorded line is no evidence that it is stored — unless the cursor is in
   the old pre-size/head format, which is the informational `legacy` status
   and holds no exit code, or the un-trust is only a same-size rewrite, which
   a digest audit can settle (see below).

Rule 6 needs the mtime condition because during a live session the wire tail
is legitimately un-ingested — the last turn is archived when the next hook
event fires, which for a session's final turn is now its `SessionEnd`. A
recently-written wire is "pending", not stale. A cursor
whose file cannot be resolved is reported as `unresolved` and is
informational only.

Rule 7 is the `provisional` status. A cursor is trusted only when the wire is
demonstrably the same file it was recorded against: the same marker (mtime and
size, for a cursor that is still resuming an incomplete pass) or a genuine
append — a longer file that still begins with the bytes the cursor was
recorded over. Anything else makes it `provisional`: the wire shrank
(truncated or replayed shorter), no longer starts with the recorded bytes
(rewritten rather than appended to), or changed to a different same-size file
with only the mtime to show it (rewritten in place). `provisional` is a
deliberately loud status, unlike `unresolved`: it holds exit code **1** and
names the reason in `problems`. The pending count is reported as unknown
(`null`) rather than a number, because counting from line 0 re-reads a wire
whose dedup window may already have evicted the stored summaries, so any
count would be an over-estimate rather than a measurement. The authoritative
report is the next ingest's own `reset_cursor: True`.

The same-size rewrite is the one provisional case the doctor can settle on
its own, because it is the only one that leaves the file *length* unchanged —
a shrunken or re-prefixed wire changed shape, and there is nothing left to
compare against. For a same-size rewrite the doctor runs a **digest audit**:
every coalesced message in wire lines 1..recorded line is prepared exactly as
the ingester prepares it (the same redaction, truncation caps and hashing,
via one shared helper in `cmpath.autosave` — the doctor never re-implements
the redaction), and the resulting digests are compared against the digests
the database already holds for that cursor's path, queried through the
doctor's read-only connection.

- **Every digest already stored** — the rewrite changed nothing that was ever
  saved. This is the benign case a session restart provokes when it touches
  an old wire's mtime without changing its bytes: the recorded line is
  proven, not assumed, so the row falls back to the ordinary logic — `current`
  when nothing is pending, `pending`/`stale` otherwise — with a reason naming
  the audit, and no problem is recorded.
- **Some digest un-stored** — the rewrite replaced content that was never
  saved: a true positive. The row stays `provisional`, exit code **1**, with
  a sharper reason naming how many of the recorded message digests were not
  stored.
- **The audit cannot run** (unreadable wire, database error) — the row stays
  `provisional` with the generic rewritten-in-place reason. Fail loud, never
  crash.

The audit inherits one caveat from the ingester itself: two distinct messages
longer than the truncation cap that share a truncated prefix collapse to one
digest, so an audit pass is only as line-exact as the stored digests are.

A cursor in the old pre-size/head format (`{"line", "mtime"}` or a bare
integer) is a different case: it carries no size or head to compare, so
nothing about it is evidence — but nothing about it is a fault either. It is
reported as `legacy` (or as `pending`/`stale`, when its wire still has
un-ingested content) with a reason that says the next ingest re-scans the
wire from line 0 and skips the digests already stored for it, so no message
is duplicated — and it holds no exit code by itself. Only the ordinary
staleness rule 6 can still turn one red: a legacy cursor whose wire is both
stale and behind is a real backlog, not bookkeeping. The OK verdict names how
many recorded cursors predate the format.

The append test is prefix-aware rather than a bare head comparison, and that
is the one place the doctor is deliberately more permissive than the ingester.
The ingester hashes only the first 512 bytes of a file into its cursor, so on a
wire shorter than that window *every* append changes the head and the ingest
resets. Re-hashing the recorded prefix instead — the first `size` bytes the
cursor was written over — recognises the append, so a fresh short wire still
with a tail to ingest stays `pending` rather than becoming a false
`provisional`. The cost of the difference is at most a redundant re-scan,
which cannot duplicate rows; treating the healthy case as provisional would
cry wolf on every fresh session.

Two more conditions are flagged without changing the verdict class: no
autosave messages recorded at all, and an unparseable last timestamp.

Default window: **604800 s (7 days)**, settable with `--max-age SECONDS`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | healthy |
| `1` | stale or unhealthy (including a missing database file) |
| `2` | the check could not run: `--max-age` not positive, database present but unreadable, or not a cmpath database (no `messages`/`tasks`) |

Example on a healthy install:

```
$ cmpath-autosave --doctor
hook      installed  timeout=10s sane=True
          command: python3 <repo>/scripts/autosave_session.py
          events: TurnStarted,Stop,SessionEnd
db        /tmp/doctor-docs/kimi.db  122880 bytes
schema    messages=yes rows=1 by_role=user:1 by_kind=prompt:1 tables=aliases,dependencies,events,evidence_index,facts,messages,runtime_state,tasks
tasks     1 total, 1 autosave
          auto 1  Autosave: rocket  messages=1  last=2026-09-13T16:41:34.910954+00:00
last write 2026-09-13T16:41:34.910954+00:00  (age 0.1d)
          derived from MAX(messages.created_at) for autosave tasks (hook signature: title 'Autosave: <slug>' + alias 'auto <slug>'); timestamps are UTC ISO-8601
state     task 1  revision=1  seen=1 hashes=1
          fact written 2026-09-13T16:41:34.935948+00:00
verdict   OK — autosave looks healthy (max age 7.0d)  messages=1 by_role=user:1 by_kind=prompt:1
```

### The absence-detector (`--deep`)

Everything above proves content *is* stored when the wire happens to look
untrustworthy. None of it can prove content is not missing: a cursor whose
recorded line is ahead of what was ever stored is invisible to the ordinary
reconciliation, because the recorded line itself is the only evidence the
doctor checks — unless the wire fails the trust test, the cursor is
`current` and the doctor says OK while messages sit un-stored. That is the
gap `--deep` exists to close, run on demand:

```
cmpath-autosave --doctor --deep
cmpath-autosave --doctor --deep --json
```

`--deep` re-runs the same digest audit the same-size-rewrite path uses, but
for **every recorded cursor of every autosave task**: every coalesced
message in wire lines 1..recorded line is prepared exactly as the ingester
prepares it (the shared helper in `cmpath.autosave`, so redaction, caps and
hashing cannot drift), and the digests are compared against the digests the
database already holds for that cursor's path. The store is opened through
the doctor's ordinary read-only connection — the audit only ever queries —
so deep mode stays strictly read-only.

**Slow.** This is O(every wire line) across all wires: on the live store
(200+ recorded cursors, several multi-MB wires) expect it to take a while,
and a note is printed before the audit loop starts. No caching is kept —
a cached digest set would rot exactly the way the cursor already does.

Reporting:

- **Per cursor**, one compact line with `missing=0` — the normal case — or,
  when digests are un-stored, the count marked `UN-STORED` and a problem
  naming the cursor, the count, and the remedy: run `--heal` to re-ingest
  the wire from line 0. `--heal` is the remedy the audit points at because
  the doctor itself never writes.
- **Summary line**: the total missing across all wires.
- **`unauditable`** cursors — wire file gone or unreadable — are
  informational, like the ordinary `unresolved`: they hold no exit code.
- **Exit codes** are unchanged otherwise: `0` healthy, `1` when any digest
  is un-stored (or anything else the ordinary check flags), `2` cannot run.
- **`--json`** carries the per-cursor rows plus `total_missing` and
  `unauditable` counts under a `deep` key.

`--deep` applies only to `--doctor`; passing it with any other mode is a
usage error. The audit inherits the same caveat as the ingester's own
dedup: two distinct messages longer than the truncation cap that share a
truncated prefix collapse to one digest.

### Why it cannot corrupt anything

The check opens the database with a raw SQLite read-only URI (`mode=ro`,
plus `immutable=1` when no `-wal`/`-shm` sidecar exists, so a read never
creates one). It never constructs `TaskMemory` — whose constructor writes
pragmas and runs migrations — and never calls `set_fact`. There is no repair
mode: `--fix` is deliberately not implemented, because a diagnostic that
writes is a different tool. That tool exists as a separate mode — `--heal`
below opens the database for writing on purpose, and appends only.

Retrieval is plain cmpath: open the database with `TaskMemory` (or the MCP
server) and `search` / `context` on task `auto:<project>`.

### Healing the holes (`--heal`)

The hook only ever ingests the *current* session, and the ingester resumes
from the recorded cursor whenever the wire looks untouched. A wire whose
parser once skipped rows while still advancing its cursor past them therefore
keeps its hole forever once the session is dead: no hook pass will ever
revisit that cursor, and `--doctor` can report the gap but has no way to
close it. `--heal` is the remedy for exactly that "cursor ahead of stored"
damage — it sweeps every wire a recorded cursor points at and re-scans it
from line 0. It also clears the other dead-end the doctor can only name: a
`provisional` or stale cursor on a dead session sticks forever, and each
healed wire's cursor is rewritten to a trusted, complete state. `--heal` and
`--doctor` are mutually exclusive.

```
cmpath-autosave --heal
cmpath-autosave --heal --db /tmp/copy.db \
  --sessions-root /tmp/sessions
cmpath-autosave --heal --dry-run --json
```

- **Which tasks are swept.** The same hook-signature conjunction the doctor
  uses: a task counts as an autosave task only when its title starts
  `Autosave:` **and** it carries the paired `auto <slug>` alias (see the
  doctor's *Tasks* section for the rationale). A hook task always has both,
  and one whose alias was deleted by hand is no longer hook-resolved — the
  next save creates a fresh task — so leaving its cursors unswept repairs
  nothing that a future save would keep.
- **Append-only and dedup-safe.** Nothing is deleted and no row is
  rewritten. The re-scan appends only rows whose content the path has not
  stored yet, checked against the digests already stored for the path — the
  full set, recovered from the stored rows themselves, not the bounded
  `hashes` window a long re-scan would outrun — so a from-0 re-scan cannot
  duplicate a message however long the wire is, and running `--heal` twice
  converges: the second run adds nothing.
- **Bounded.** Each wire is re-scanned repeatedly until a pass adds nothing;
  one pass appends at most 500 rows, so a wire longer than one pass needs
  several passes, and `MAX_HEAL_PASSES` (64) caps the loop: a wire that will
  not converge is reported as `incomplete` rather than spun on forever.
- **The index is merged after a bulk run.** Every appended row also feeds the
  `evidence_index` FTS5 table one row at a time, and FTS5 accumulates a fresh
  segment per merge batch — a long heal fragments the index, and every later
  search pays for the fragmentation. When a real run appends at least
  `HEAL_OPTIMIZE_THRESHOLD` (1000) rows in total, the index is merged once at
  the end (`Autosave.optimize_index`, the FTS5 `'optimize'` command; merging
  an already-merged index is effectively a no-op, so a small heal skips it via
  the threshold). The plain summary notes it
  (`healed N message(s) across M wire(s)  (fts index optimized)`) and the JSON
  report carries `"index_optimized": true`. A dry-run never optimizes, and a
  failed merge is reported as an `error` row (exit code 1) rather than
  crashing a heal that already did its work.
- **Report.** One line per recorded cursor in plain mode
  (`heal  <key>  added=N passes=N status=…`, plus a
  `healed N message(s) across M wire(s)` summary), or a JSON object with
  `--json`: `{"healed": […], "index_optimized": bool}`. Statuses: `ingested` / `clean`
  (or `would_ingest` under `--dry-run`), `unresolved` (the wire file cannot
  be found), `no_state` (an autosave task has messages but no
  `autosave_state` fact), `error` (the wire could not be read — one bad wire
  is a finding, not a crash), and `incomplete` (the pass cap was reached
  with rows still to append).
- **Exit codes.** `0` when every wire ended `ingested`/`clean`/
  `would_ingest`/`unresolved`; `1` when any wire is `incomplete` or `error`,
  or the store could not be opened — something did not finish, so look at
  the report and re-run.
- **Dry-run is supported.** `--dry-run` writes nothing (not even the
  refreshed cursors, and it does not create the project task) and reports
  the `would_ingest` preview instead.

## Tests

```
cd <repo> && python -m unittest discover -s tests -v
```

covers redaction per pattern class (including the `github_pat_` and `hf_`
token shapes), dedup, truncation, ingest idempotency (a second hook pass
over an unchanged wire writes nothing — no message rows, no cursor change),
torn-line recovery,
hook subprocess end-to-end (including garbage stdin and a missing module),
dry-run isolation, and installer install/print/uninstall over the full
three-event managed region (plus refusal to install over an existing block,
`--uninstall` stripping the whole region, and a legacy single-event block
still reading as installed). The marker-less variant has its own coverage:
install over a hand-added `[[hooks]]` section replaces it in one pass and
`--uninstall` strips both a marked region and such a section from the same
config, leaving the surrounding hooks untouched.

The atomicity of `save_turn()` has its own coverage: a failure injected at
the state-fact write rolls the already-appended message back (no rows, no
fact), the rolled-back seen key and digest leave the retry a fresh save that
lands exactly once, a save that appends nothing (empty prompt) writes no
fact, a duplicate save writes no new fact revision, and the normal save keeps
the prompt row and the fact's seen/hashes/evidence anchor in step.

Concurrency between the heal sweep and the hook is carried by the SQLite
busy timeout on the TaskMemory connection. Under WAL a reader never blocks,
but the heal's ingest batches and a hook `save_turn` firing in a live
session are two writers on one file, and with no busy timeout the second
writer fails immediately with `database is locked` — which the fail-open
hook turns into silently lost evidence, or the heal report into an 'error'
row. The busy timeout makes the collided writer wait instead — comfortably
above the hook's own 8 s internal budget, so a hook save waits out a heal
batch rather than timing out first. Two tests prove it: a write that
collides with a lock held past the old 5 s connection default lands once
the lock is released, and a full 1,200-row heal run concurrently with 30
hook saves ends with no error rows, every save landed exactly once, and no
duplicated rows.

Three areas carry the weight of the behaviour described above. Dry-run
isolation is asserted byte-for-byte: the database file is hashed before and
after `--dry-run`, `--rebuild` included, and nothing may change — and a dry
run must not create the project task, so a `--dry-run` against a database
that does not yet hold the task leaves it absent. Re-scan behaviour is
covered from both directions: a path truncated to fewer lines, a path
rewritten at the same size, and a legacy `{line, mtime}` cursor entry
(upgraded in place, then re-scanned once) are all re-scanned, while a legacy
entry whose stored `mtime` still matches is deliberately left alone; rebuild
run twice converges with no duplicates and preserves prompt rows and other
paths. Larger paths have their own coverage, because every other fixture here
stays below both thresholds: a 900-line path is completed over two passes
instead of being short-circuited as unchanged once the 500-row cap stopped
the first pass, and a 1,200-line path converges over successive rebuilds with
no duplicates even after the 1000-entry `hashes` window has evicted the rows
a re-scan needs to skip — the window is asserted full and shorter than the
file, so only the digests already stored for the path stop the re-append.

The `--doctor` tests cover hook detection (present, absent, wrong command,
unsane timeout, no-`tomllib` fallback), the per-event `events:` line across
the full set, a legacy `TurnStarted`-only config (still healthy, with the
missing events named) and a `Stop`-only config, each health check and exit
code,
the JSON report shape, the message counts (`messages_total`, and the
`messages_by_role` / `messages_by_kind` breakdowns) in both the plain and
JSON reports, the cursor trust test — a wire rewritten shorter, and a
same-size rewrite that only moved the mtime (with replaced content), must
both come back `provisional` with exit code 1 and no pending count, while an
appended-to wire still resolves to `pending` and exit code 0; a
legacy-format cursor
comes back informational — `legacy`, or `pending` with the format named in
its reason — and exit code 0, whether or not its recorded mtime still
matches the wire — and the fail-open
guarantee that `run_hook` returns 0 on every error path. The digest audit
has its own coverage: a benign mtime touch (bytes untouched) and a
byte-identical rewrite both settle to `current` with exit code 0 and the
audit named in the reason; a rewrite whose stored rows no longer match stays
`provisional` with exit code 1; an audit that cannot run (a database error)
falls back to the generic provisional reason through the read-only path; and
the audit helper itself is unit-tested for an unreadable wire (`None`) and
for propagating database errors to its caller.

The `--heal` tests cover both the in-process mode and the real CLI. A cursor
forged past appended messages — trusted, because its mtime/size/head match
the wire — is the hole no ordinary ingest reaches (the test first asserts the
ordinary ingest skips the wire as `unchanged`), and a heal stores the missing
rows with no duplicates; a second heal adds nothing and leaves the stored row
ids untouched; a 1,200-line wire behind a forged cursor converges in three or
more passes through the 500-row cap; a deleted wire comes back `unresolved`
with exit code 0 instead of an exception; and a dry-run heal reports
`would_ingest` while the database file and its WAL stay byte-for-byte
identical. The optimize pass is covered at the method boundary: a heal that
appends fewer than `HEAL_OPTIMIZE_THRESHOLD` rows never attempts the merge,
one that appends more calls `Autosave.optimize_index` exactly once and
reports `index_optimized`, a dry-run above the threshold still never calls
it, and `optimize_index` itself runs cleanly on a store with indexed content
and is idempotent (called twice, the index still matches the stored rows).
Through the CLI, seeding a session directory and appending one
message shows the appended message landing in the database, the `--json`
report carries exactly the `{key, path, added, passes, status}` shape per
cursor with `unresolved` rows for a deleted wire, and passing `--heal`
together with `--doctor` is refused with exit code 2.
