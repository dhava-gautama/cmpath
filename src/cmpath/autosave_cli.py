"""Kimi Code autosave hook: save the session into cmpath memory.

Installed as the ``cmpath-autosave`` console script (or via the
``scripts/autosave_session.py`` shim) for three events

Installed for three events (```TurnStarted```, ``Stop``, ``SessionEnd``);
each invocation does the same save-and-ingest pass, so the work is
idempotent and whichever event fires first for a given turn wins.

Hook contract (confirmed against ~/.kimi-code/hooks/turn-marker.py):
the payload arrives as JSON on stdin,
``{session_id, cwd, turn_id, origin_kind, origin_name, prompt}``.

Hook mode (default) prints nothing and ALWAYS exits 0, with a hard internal
deadline (~8s of the 10s hook timeout) for transcript ingestion.

Manual modes:
  --selftest           run internal checks, print PASS/FAIL, exit 0/1
  --ingest PATH        one-shot transcript ingest (with --session-id/--cwd)
  --dry-run            with stdin or --ingest: report, never write
  --heal               re-ingest every recorded wire from line 0, healing
                       holes the recorded cursors skipped (append-only and
                       dedup-safe; --json for a per-wire report)
  --doctor             read-only health check of the autosave automation
                       (--status is an alias); exit 0 healthy, 1 stale, 2 cannot run
  --deep               with --doctor: digest-audit every recorded cursor
                       (slow: re-reads every wire line; detects content the
                       cursors claim is stored but the store never got)
  --json               with --doctor: machine-readable report
  --db PATH            database override (default ~/.local/share/cmpath/kimi.db)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib as _tomllib
except ImportError:  # Python 3.10
    _tomllib = None

from cmpath.autosave import (DEFAULT_DB_PATH, MAX_MESSAGE_CHARS, STATE_KEY,
                             Autosave, audit_wire_cursor, content_hash,
                             iter_wire_coalesced, locate_session_dir, redact)
from cmpath.memory import words

BUDGET_SECONDS = 8.0
DEFAULT_CONFIG_PATH = "~/.kimi-code/config.toml"
DEFAULT_SESSIONS_ROOT = "~/.kimi-code/sessions"
DEFAULT_MAX_AGE = 604800  # 7 days
MAX_HOOK_TIMEOUT = 300
# Events the installer manages; the hook script itself is event-agnostic.
MANAGED_EVENTS = ("TurnStarted", "Stop", "SessionEnd")
# Upper bound on heal passes per wire: each pass appends at most 500 rows, so
# a wire that is still growing after this many passes is not converging, and
# heal must report that (exit 1) rather than spin.
MAX_HEAL_PASSES = 64
# A heal appends rows in bulk and every appended row also feeds the FTS5
# evidence index one row at a time, which fragments the index into many
# segments. When a heal run appends this many rows in total, the index is
# worth merging once at the end (``Autosave.optimize_index``); below that the
# fragmentation is not enough to pay for the merge walk.
HEAL_OPTIMIZE_THRESHOLD = 1000


def _open(db_path, dry_run):
    return Autosave(db_path, write=not dry_run)


def run_selftest() -> int:
    """Minimal in-process checks; complements tests/test_autosave.py."""
    failures = []

    def check(name, ok):
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)

    secret = "sk-abcdef1234567890abcdef12"
    check("redact openai", redact("k " + secret) == "k [REDACTED:openai]")
    check("redact aws", "[REDACTED:aws]" in redact("AKIAIOSFODNN7EXAMPLE"))
    check("redact keyval", "[REDACTED:keyval]" in redact('MY_TOKEN="aabbccdd11223344"'))
    check("clean untouched", redact("plain note") == "plain note")

    with tempfile.TemporaryDirectory() as tmp:
        store = _open(os.path.join(tmp, "st.db"), dry_run=False)
        try:
            first = store.save_turn("s", "/tmp/proj", "hello", turn_id="1")
            second = store.save_turn("s", "/tmp/proj", "hello", turn_id="1")
            check("dedup same turn", first["status"] == "saved"
                  and second["status"] == "duplicate")
            long_text = "x" * 5000
            trunc = store.save_turn("s", "/tmp/proj", long_text, turn_id="2")
            check("truncation", trunc["status"] == "saved" and trunc["truncated"])
            wire = Path(tmp) / "wire.jsonl"
            wire.write_text(json.dumps(
                {"type": "turn.prompt", "input": [{"type": "text", "text": "q"}]}) + "\n",
                encoding="utf-8")
            one = store.ingest_transcript(wire, session_id="s", cwd="/tmp/proj")
            two = store.ingest_transcript(wire, session_id="s", cwd="/tmp/proj")
            check("ingest idempotent", one["added"] == 1
                  and two["status"] in ("clean", "unchanged"))
            check("task reuse", first["task_id"] == trunc["task_id"] == one["task_id"])
        finally:
            store.close()
        dry = _open(os.path.join(tmp, "dry.db"), dry_run=True)
        try:
            would = dry.save_turn("s", "/tmp/proj", "hello", turn_id="1")
            check("dry-run no write", would["status"] == "would_save"
                  and not os.path.exists(os.path.join(tmp, "dry.db")))
        finally:
            dry.close()

    if failures:
        print(f"SELFTEST FAIL ({len(failures)})")
        return 1
    print("SELFTEST PASS")
    return 0


def run_manual_ingest(args) -> int:
    store = _open(args.db, args.dry_run)
    try:
        result = store.ingest_transcript(
            args.ingest, session_id=args.session_id, cwd=args.cwd,
            rebuild=args.rebuild)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") != "missing" else 1
    finally:
        store.close()


def run_heal(args) -> int:
    """Re-ingest every recorded wire from line 0, healing what cursors skipped.

    The hook only ever ingests the current session (it locates the directory
    from the payload's ``session_id``), and the ingester resumes from the
    recorded cursor whenever the wire looks untouched. A wire whose parser once
    skipped rows while still advancing its cursor past them therefore keeps its
    hole forever once the session is dead: no hook pass will revisit that
    cursor again, and ``--doctor`` can only report the gap. ``--heal`` sweeps
    every wire a recorded cursor points at and re-scans it from line 0.

    ``rebuild=True`` is mandatory here, not an optimisation: a trusted cursor
    would resume from the recorded line and never reach the hole below it. The
    re-scan cannot duplicate a message, because it filters every row against
    the digests already stored for the path — ``Autosave._wire_digests``
    recovers that full set from the stored rows themselves, not from the
    bounded ``hashes`` window a re-scan longer than the window would outrun.
    Each wire is re-scanned repeatedly until a pass adds nothing: one pass
    appends at most 500 rows before it stops at the cap, so a wire longer than
    one pass needs several passes, and the first pass that adds nothing is the
    proof the wire is now fully stored. ``MAX_HEAL_PASSES`` bounds the loop so
    a wire that will not converge is reported as ``incomplete`` (exit code 1)
    instead of spinning. Nothing is deleted and no row is rewritten: stored
    evidence is append-only, and the only state written is the refreshed
    cursor fact, which leaves each wire in a trusted, complete state — that is
    also what clears a permanent ``provisional`` or stale cursor on a dead
    session, which no future hook pass would ever revisit either.

    Bulk appends fragment the FTS5 evidence index (one segment per merge
    batch), so when a real run appends at least :data:`HEAL_OPTIMIZE_THRESHOLD`
    rows in total the index is merged once at the end via
    :meth:`Autosave.optimize_index`; a dry-run never optimizes, and a failed
    merge is reported like any other finding (an ``error`` row, exit 1)
    instead of crashing a heal that already did its work.
    """
    try:
        store = _open(args.db, args.dry_run)
    except Exception as exc:
        print(f"heal  cannot open store: {exc}", file=sys.stderr)
        return 1
    rows: list[dict] = []
    index_optimized = False
    try:
        sessions_root = Path(os.path.expanduser(str(args.sessions_root)))
        # The same conjunction predicate as the doctor: a hook task always
        # carries both its 'Autosave: <slug>' title and its 'auto <slug>'
        # alias, while a task that lost the alias is no longer hook-resolved
        # (``Autosave._project_task_by_name`` scans aliases, so the next save
        # creates a fresh task and the old one never receives another wire) —
        # sweeping its cursors would repair nothing with a future.
        autosave_tasks = [t for t in store.memory.tasks()
                          if _is_autosave_task(t.title, t.aliases)]
        for task in autosave_tasks:
            cursors = store._state(task.id)["cursors"]
            if not cursors:
                fact = store.memory.fact(task.id, STATE_KEY)
                if fact is None and store.memory.transcript(task.id):
                    # Messages without bookkeeping: nothing points at a wire,
                    # so there is nothing to re-scan, but the doctor flags
                    # exactly this task and heal must name it.
                    rows.append({"key": f"task {task.id}", "path": None,
                                 "added": 0, "passes": 0,
                                 "title": task.title, "status": "no_state"})
                continue
            for key in cursors:
                row = {"key": key, "path": None, "added": 0, "passes": 0,
                       "status": "unresolved"}
                try:
                    path = _resolve_cursor(str(key), sessions_root)
                    if path is None:
                        rows.append(row)
                        continue
                    row["path"] = str(path)
                    added, passes, status = 0, 0, "clean"
                    while True:
                        result = store.ingest_transcript(
                            path, session_id=(key.split("/")[0] or None),
                            rebuild=True, deadline=None)
                        added += result["added"]
                        passes += 1
                        status = result["status"]
                        # A dry-run writes nothing, so every pass would report
                        # the same would-be rows forever; one pass is the
                        # report, and the real run converges separately.
                        if args.dry_run or result["added"] == 0:
                            break
                        if passes >= MAX_HEAL_PASSES:
                            status = "incomplete"
                            break
                    row["added"], row["passes"], row["status"] = \
                        added, passes, status
                except Exception as exc:
                    # One unreadable wire is a finding, not a crash: heal
                    # reports it, keeps the other wires' results, and exits 1.
                    row["status"] = "error"
                    row["reason"] = str(exc)
                rows.append(row)
        total_added = sum(row["added"] for row in rows)
        if not args.dry_run and total_added >= HEAL_OPTIMIZE_THRESHOLD:
            try:
                index_optimized = store.optimize_index()
            except Exception as exc:
                # The heal itself already succeeded; a failed index merge is a
                # finding like any other, reported and reflected in the exit
                # code, not a crash of a run that did its work.
                rows.append({"key": "index optimize", "path": None,
                             "added": 0, "passes": 0, "status": "error",
                             "reason": str(exc)})
    finally:
        store.close()
    code = 1 if any(r["status"] in ("incomplete", "error") for r in rows) else 0
    if args.as_json:
        print(json.dumps({"healed": rows, "index_optimized": index_optimized},
                         ensure_ascii=False))
    else:
        for row in rows:
            line = (f"heal  {row['key']}  added={row['added']}"
                    f" passes={row['passes']} status={row['status']}")
            if row.get("reason"):
                line += f"  {row['reason']}"
            print(line)
        total = sum(row["added"] for row in rows)
        wires = sum(1 for row in rows if row.get("path"))
        summary = f"healed {total} message(s) across {wires} wire(s)"
        if index_optimized:
            summary += "  (fts index optimized)"
        print(summary)
    return code


def run_hook(args) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    if not str(payload.get("prompt") or "").strip() and not payload.get("session_id"):
        return 0  # nothing to save; do not even open the database
    try:
        store = _open(args.db, args.dry_run)
    except Exception:
        return 0  # fail-open: a broken store must not break the session
    if store is None:
        return 0
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "") or None
    deadline = time.monotonic() + BUDGET_SECONDS
    try:
        with store:
            result = store.save_turn(
                session_id, cwd, str(payload.get("prompt") or ""),
                turn_id=payload.get("turn_id"),
                extra={"origin_kind": str(payload.get("origin_kind") or ""),
                       "origin_name": str(payload.get("origin_name") or "")})
            if args.dry_run:
                print(json.dumps({"save_turn": result}, ensure_ascii=False))
            session_dir = args.session_dir or locate_session_dir(session_id)
            if session_dir:
                results = store.ingest_session_dir(
                    session_dir, session_id=session_id, cwd=cwd, deadline=deadline)
                if args.dry_run:
                    print(json.dumps({"ingest": results}, ensure_ascii=False))
    except Exception:
        pass  # fail-open: autosave must never break the session
    finally:
        try:
            store.close()
        except Exception:
            pass
    return 0


# -- doctor (read-only diagnostics) ------------------------------------------

def _hook_target() -> Path:
    """The path a hook config's command is expected to reference.

    When run through the ``scripts/autosave_session.py`` shim or the
    ``cmpath-autosave`` console script, that entry point is ``sys.argv[0]``,
    not this module's file. Falls back to the module file when ``argv[0]``
    is not a filesystem path (in-process ``main()`` calls).
    """
    raw = sys.argv[0] if sys.argv and sys.argv[0] else ""
    candidate = Path(raw) if raw else None
    if candidate is not None and candidate.name not in ("", "-c", "-m"):
        try:
            return candidate.resolve()
        except OSError:
            return candidate
    return Path(__file__).resolve()


def detect_hook(config_text: str, script_path, *,
                toml_available: bool | None = None) -> dict:
    """Find the managed-event hooks that run ``script_path`` in ``config_toml``.

    Returns ``{installed, command, timeout, timeout_sane, parse, note, error,
    events, missing_events}``. ``events`` maps each managed event to whether a
    hook for it points at this script; ``missing_events`` lists the managed
    events with no such hook (informational — a legacy TurnStarted-only
    install is still considered installed). ``command``/``timeout`` describe
    the TurnStarted hook when present, else the first managed event that is.
    ``parse`` is ``"toml"`` when ``tomllib`` handled the file, ``"text"`` for
    the regex fallback used on Python 3.10 or an unparseable config.
    """
    target = Path(script_path).resolve()
    result = {"installed": False, "command": None, "timeout": None,
              "timeout_sane": False, "parse": "none", "note": "", "error": None,
              "events": {event: False for event in MANAGED_EVENTS},
              "missing_events": list(MANAGED_EVENTS)}
    if toml_available is None:
        toml_available = _tomllib is not None
    entries = []
    if toml_available and _tomllib is not None:
        result["parse"] = "toml"
        try:
            data = _tomllib.loads(config_text)
            raw = data.get("hooks")
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
            else:
                result["error"] = "no [[hooks]] array in config"
        except Exception as exc:
            result["error"] = f"toml parse failed: {exc}"
            result["parse"] = "text"
    if result["parse"] != "toml":
        result["parse"] = "text"
        entries = _hooks_from_text(config_text)
    matched: dict[str, dict] = {}
    command_matched_other_event = None
    for entry in entries:
        if not _command_matches(entry.get("command"), target):
            continue
        event = str(entry.get("event") or "")
        if event in MANAGED_EVENTS:
            matched.setdefault(event, entry)
        elif command_matched_other_event is None:
            command_matched_other_event = entry
    if not matched:
        if command_matched_other_event is not None:
            result["note"] = ("command matches this script but the hook event is "
                              f"{command_matched_other_event.get('event')!r},"
                              " not one of "
                              + ", ".join(repr(e) for e in MANAGED_EVENTS))
        elif result["error"]:
            result["note"] = result["error"]
        else:
            result["note"] = ("no hook for "
                              + ", ".join(MANAGED_EVENTS)
                              + " pointing at this script")
        return result
    result["installed"] = True
    result["events"] = {event: event in matched for event in MANAGED_EVENTS}
    result["missing_events"] = [e for e in MANAGED_EVENTS if e not in matched]
    primary_event = "TurnStarted" if "TurnStarted" in matched else next(
        e for e in MANAGED_EVENTS if e in matched)
    primary = matched[primary_event]
    result["command"] = str(primary.get("command") or "")
    timeout = _as_int(primary.get("timeout"))
    result["timeout"] = timeout
    result["timeout_sane"] = (timeout is not None
                              and BUDGET_SECONDS <= timeout <= MAX_HOOK_TIMEOUT)
    found = ",".join(e for e in MANAGED_EVENTS if e in matched)
    note = f"event={found} timeout={timeout}s"
    if result["missing_events"]:
        note += f" (missing: {','.join(result['missing_events'])})"
    result["note"] = note
    return result


def _hooks_from_text(config_text: str) -> list[dict]:
    """Regex fallback for a config ``tomllib`` cannot parse (Python 3.10)."""
    parts = re.split(r"(?m)^\s*\[\[hooks\]\]\s*$", config_text)
    sections = parts[1:] if len(parts) > 1 else [config_text]
    entries = []
    for section in sections:
        command = re.search(r'(?m)^\s*command\s*=\s*"([^"]*)"', section)
        event = re.search(r'(?m)^\s*event\s*=\s*"([^"]*)"', section)
        timeout = re.search(r"(?m)^\s*timeout\s*=\s*(\d+)", section)
        entries.append({"command": command.group(1) if command else None,
                        "event": event.group(1) if event else None,
                        "timeout": timeout.group(1) if timeout else None})
    return entries


def _command_matches(command, target: Path) -> bool:
    if not command:
        return False
    try:
        tokens = shlex.split(str(command))
    except ValueError:
        tokens = str(command).split()
    for token in tokens:
        if not token or token.startswith("-"):
            continue
        try:
            if Path(os.path.expanduser(token)).resolve() == target:
                return True
        except Exception:
            pass
        if os.path.basename(token) == target.name:
            return True
    return False


def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _open_ro(db_path) -> sqlite3.Connection:
    """Read-only connection; never creates the file, never writes pragmas.

    When no ``-wal``/``-shm`` sidecar exists (no live writer) the connection
    also sets ``immutable=1`` so SQLite creates no side files of its own.
    """
    resolved = Path(os.path.expanduser(str(db_path))).resolve()
    uri = resolved.as_uri()
    sidecars = [Path(str(resolved) + suffix) for suffix in ("-wal", "-shm")]
    if not any(p.exists() for p in sidecars):
        try:
            return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
        except sqlite3.Error:
            pass
    return sqlite3.connect(uri + "?mode=ro", uri=True)


def _parse_ts(text) -> datetime | None:
    if not text:
        return None
    value = str(text).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(text) -> float | None:
    parsed = _parse_ts(text)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


_AUTOSAVE_TITLE_PREFIX = "Autosave:"
"""Title half of the hook's task-creation signature (see
``Autosave._project_task_by_name``, which writes title ``Autosave: <slug>``
and alias ``auto:<slug>`` — stored words-normalized as ``auto <slug>`` — in
one call)."""


def _autosave_alias_signature(title: str) -> str | None:
    """Normalized alias the hook pairs with ``title``, or ``None``.

    The hook's alias is ``auto:<title slug>``, so the signature is a *paired*
    alias: ``Autosave: rocket`` must carry ``auto rocket``, not any
    ``auto ...`` phrase. ``None`` means the title lacks the ``Autosave:``
    prefix and cannot be a hook task at all.
    """
    if not title.startswith(_AUTOSAVE_TITLE_PREFIX):
        return None
    slug = title[len(_AUTOSAVE_TITLE_PREFIX):].strip()
    return " ".join(words(f"auto:{slug}"))


def _is_autosave_task(title: str, aliases) -> bool:
    """Is this task one the autosave hook created (and still manages)?

    The hook creates each project task in one call with both a title
    ``Autosave: <slug>`` and an alias ``auto:<slug>`` (stored normalized as
    ``auto <slug>``), so the pair is its creation signature. Either half
    alone is forgeable by a human — the CLI accepts any title and any alias —
    and a task carrying only one half is never hook-managed: nothing ever
    writes its ``autosave_state`` fact, so counting it as an autosave task
    made the doctor's missing-fact rule fire forever (a permanent red light
    masking real problems). The conjunction is therefore required: the title
    must start with ``Autosave:`` **and** one alias must be the paired
    ``auto <slug>`` form.
    """
    expected = _autosave_alias_signature(str(title or "").strip())
    return expected is not None and any(
        " ".join(words(str(alias or ""))) == expected for alias in aliases)


def _visible_tables(names) -> list[str]:
    """Drop FTS5 shadow tables and SQLite internals from the schema report."""
    hidden = re.compile(r"_(config|content|data|docsize|idx)$")
    return [n for n in names
            if n != "sqlite_sequence" and not hidden.search(n)]


def _tasks_report(conn) -> list[dict]:
    counts = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT task_id, COUNT(*), MAX(created_at) FROM messages GROUP BY task_id")}
    aliases: dict[int, list[str]] = {}
    for task_id, alias in conn.execute("SELECT task_id, alias FROM aliases"):
        aliases.setdefault(task_id, []).append(str(alias or ""))
    tasks = []
    for task_id, title in conn.execute("SELECT id, title FROM tasks ORDER BY id"):
        messages, last = counts.get(task_id, (0, None))
        task_aliases = aliases.get(task_id, [])
        tasks.append({"task_id": task_id, "title": title,
                      "autosave": _is_autosave_task(str(title or ""),
                                                    task_aliases),
                      "messages": messages, "last_message_at": last})
    return tasks


def _state_facts(conn) -> dict[int, dict]:
    """Current ``autosave_state`` fact per task (``invalid_at IS NULL``)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
    if "key" not in columns or "value" not in columns:
        return {}
    select = "task_id, revision, value, created_at, retracted"
    if "invalid_at" in columns:
        select += ", invalid_at"
    current: dict[int, dict] = {}
    for row in conn.execute(
            f"SELECT {select} FROM facts WHERE key=? ORDER BY task_id, revision",
            (STATE_KEY,)):
        task_id, revision, value, created_at, retracted = row[:5]
        invalid = row[5] if len(row) > 5 else None
        if invalid is not None or retracted:
            continue
        try:
            parsed = json.loads(value) if value else {}
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        current[task_id] = {"revision": revision, "value": parsed,
                            "created_at": created_at}
    return current


def _resolve_cursor(key: str, sessions_root: Path) -> Path | None:
    path = Path(key)
    if path.is_absolute():
        return path if path.is_file() else None
    if not sessions_root.is_dir():
        return None
    matches = []
    for workspace in sorted(sessions_root.glob("wd_*")):
        candidate = workspace / key
        if candidate.is_file():
            matches.append(candidate)
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _pending_count(path: Path, start_line: int, hashes, limit: int = 500) -> int | None:
    """Un-ingested messages in ``path`` beyond the recorded cursor line."""
    count = 0
    try:
        for _first, _last, _role, text in iter_wire_coalesced(path, start_line=start_line):
            content = redact(text).strip()
            if not content:
                continue
            if content_hash(content[:MAX_MESSAGE_CHARS]) in hashes:
                continue
            count += 1
            if count >= limit:
                break
    except Exception:
        return None
    return count


_WIRE_HEAD_BYTES = 512
"""Head window the ingester hashes when it stamps a cursor.

Mirrors the default of ``Autosave._file_marker``; the doctor needs the number
to re-hash a recorded prefix on its own.
"""


def _wire_marker(path: Path, head_bytes: int = _WIRE_HEAD_BYTES):
    """``(mtime, size, head)`` for ``path``; ``(None, None, None)`` if unreadable.

    Delegates to ``Autosave._file_marker`` so the doctor and the ingester judge
    a recorded cursor against the same three values; two definitions would
    drift and disagree about whether a wire was rewritten.
    """
    marker = getattr(Autosave, "_file_marker", None)
    if marker is None:
        return None, None, None
    return marker(path, head_bytes)


def _recorded_prefix_intact(path, seen_size, seen_head, head) -> bool:
    """Does the wire still start with the bytes that produced ``seen_head``?

    ``Autosave._file_marker`` hashes only the first ``_WIRE_HEAD_BYTES`` of the
    file, so on a wire shorter than that window *any* append changes the head
    and looks like a rewrite. Re-hashing the recorded prefix settles it: the
    old head is intact exactly when the first ``seen_size`` bytes still hash to
    it. Only then is the recorded line evidence that the wire was appended to.
    """
    if head is not None and seen_head is not None and head == seen_head:
        return True
    if seen_head is None or seen_size is None or seen_size >= _WIRE_HEAD_BYTES:
        return False
    _mtime, _size, shorter = _wire_marker(path, seen_size)
    return shorter is not None and shorter == seen_head


def _trusted_cursor(cursor, path, mtime, size, head) -> bool:
    """Restate the ingester's trust test for a cursor recorded past line 0.

    Only an append (same head, longer) or a resume of a pass that stopped at
    the cap (same head, same marker) may trust a recorded line. See
    ``Autosave.ingest_session_dir``; keep the two in step -- with one
    deliberate exception: the ingester compares bare head digests, so a wire
    under the head window that was appended to resets its cursor. Re-hashing
    the recorded prefix (see ``_recorded_prefix_intact``) recognises that
    append instead of calling it a rewrite, which keeps a fresh wire's healthy
    pending cursor healthy.
    """
    seen_mtime, seen_size = cursor.get("mtime"), cursor.get("size")
    seen_head = cursor.get("head")
    complete = bool(cursor.get("complete",
                              not ("size" in cursor and "head" in cursor)))
    same_marker = (seen_mtime is not None and seen_mtime == mtime
                   and (seen_size is None or seen_size == size))
    if complete and same_marker:
        return True
    grown = seen_size is not None and size is not None and size > seen_size
    same_head = seen_head is not None and (
        head == seen_head
        or (grown and _recorded_prefix_intact(path, seen_size, seen_head, head)))
    return same_head and (grown or same_marker)


_LEGACY_CURSOR_REASON = "cursor records no size/head (older cursor format)"
"""``_untrusted_reason``'s word for a cursor in the pre-size/head format."""


_REASON_LEGACY = "legacy"
_REASON_SHRANK = "shrank"
_REASON_PREFIX_GONE = "prefix-gone"
_REASON_NO_MTIME = "no-mtime"
_REASON_REWRITE = "same-size-rewrite"
"""Codes ``_untrusted_reason`` returns alongside its message.

The doctor branches on the code, never on the message text: the same-size
rewrite is the one untrusted case a digest audit can settle, and a reason
code keeps that branch structural instead of string-matching a sentence.
"""


def _legacy_cursor(cursor) -> bool:
    """Is this cursor written in the format that predates size and head?

    A bare ``int`` and a ``{"line", "mtime"}`` dict alike qualify: neither
    records a size or a head for the ingester to compare against the wire (see
    ``Autosave.ingest_session_dir``). When such a cursor is not trusted the
    ingester re-scans the wire from line 0, and that scan filters against the
    digests already stored for the path, so the re-scan cannot duplicate a
    message.
    """
    if isinstance(cursor, bool):
        return False
    if isinstance(cursor, int):
        return True
    return (isinstance(cursor, dict) and "size" not in cursor
            and "head" not in cursor)


def _untrusted_reason(cursor, path, mtime, size, head) -> tuple[str, str] | None:
    """``(code, message)`` for why the cursor cannot be trusted, else ``None``.

    Priority follows the ingester's order of checks: a shrinking file, then a
    cursor too old to carry a head, then a changed head, then a same-size
    rewrite that only moved the mtime. The code (see the ``_REASON_*``
    constants) lets the doctor tell the one decidable case — the same-size
    rewrite, whose recorded lines a digest audit can settle — apart from the
    cases where the file itself changed shape and nothing can be audited.
    """
    if isinstance(cursor, dict) and _trusted_cursor(cursor, path, mtime, size,
                                                    head):
        return None
    if not isinstance(cursor, dict):
        return _REASON_LEGACY, _LEGACY_CURSOR_REASON
    seen_mtime, seen_size = cursor.get("mtime"), cursor.get("size")
    seen_head = cursor.get("head")
    if seen_size is not None and size is not None and size < seen_size:
        return _REASON_SHRANK, (f"its wire shrank from {seen_size} to {size}"
                                f" bytes (truncated or replayed)")
    if seen_head is None or head is None:
        return _REASON_LEGACY, _LEGACY_CURSOR_REASON
    if head != seen_head and not _recorded_prefix_intact(path, seen_size,
                                                         seen_head, head):
        return _REASON_PREFIX_GONE, ("its wire no longer starts with the bytes"
                                     " that produced the recorded line, so it"
                                     " was rewritten rather than appended to")
    if seen_mtime is None or mtime is None:
        return _REASON_NO_MTIME, "cursor records no mtime"
    return _REASON_REWRITE, ("its wire was rewritten in place (same size and"
                             " head, new mtime)")


def _audit_same_size_rewrite(code, conn, task_id, cursor_key, path,
                             recorded_line) -> dict | None:
    """Run the digest audit for a same-size rewrite, or ``None`` if it cannot.

    Only the same-size-rewrite case is audited: it is the one untrusted reason
    that leaves the wire's length -- and so its recorded lines -- in place;
    a shrunken or re-prefixed wire changed shape and there is nothing to
    compare against. The audit reuses the ingester's own preparation
    (``audit_wire_cursor``) and queries through the doctor's read-only
    connection; any failure -- a database error, an unreadable wire -- returns
    ``None`` so the caller falls back to the generic provisional reason
    instead of crashing.
    """
    if code != _REASON_REWRITE or conn is None:
        return None
    try:
        return audit_wire_cursor(path, recorded_line, conn, task_id,
                                 cursor_key)
    except Exception:
        return None


def _deep_audit_cursor(key, cursor, conn, task_id, sessions_root) -> dict:
    """Digest-audit one recorded cursor from line 0 to its recorded line.

    The ordinary doctor trusts a recorded line once the wire marker matches;
    the deep audit proves it instead: every coalesced message in wire lines
    1..recorded line is prepared exactly as the ingester prepares it
    (``audit_wire_cursor``, which reuses the shared ``_prepare_text``) and
    compared against the digests the database holds for that cursor key. The
    result is ``{"status": "clean", ...}`` when every digest is stored,
    ``{"status": "missing", ...}`` when some are not — the absence the
    ordinary doctor cannot see — and ``{"status": "unauditable", ...}``
    (informational, holds no exit code) when the wire cannot be read and
    there is nothing to compare against. ``conn`` is the doctor's read-only
    connection; the audit only ever queries it.
    """
    start = 0
    if isinstance(cursor, dict):
        start = _as_int(cursor.get("line")) or 0
    elif isinstance(cursor, int) and not isinstance(cursor, bool):
        start = cursor
    row = {"task_id": task_id, "key": key, "line": start,
           "status": "unauditable", "digests": 0, "missing": 0}
    path = _resolve_cursor(str(key), sessions_root)
    if path is None:
        return row
    try:
        result = audit_wire_cursor(path, start, conn, task_id, str(key))
    except Exception:
        return row
    if result is None:
        return row
    row["digests"] = result["digests"]
    row["missing"] = result["missing"]
    row["status"] = "missing" if result["missing"] else "clean"
    return row


def _deep_audit_all(conn, autosave_tasks, states, sessions_root,
                    problems) -> dict:
    """Run the digest audit for every recorded cursor of every autosave task.

    This is the doctor's absence-detector: a cursor recorded past what was
    ever stored is invisible to the ordinary reconciliation unless the wire
    happens to fail the trust test, and the only proof that the cursor's
    lines are really in the store is to re-prepare every one of them and
    compare digests. That is O(every wire line) across all wires — slow on a
    large store — which is why it lives behind ``--deep`` instead of running
    on every doctor pass. Caches would rot; none are kept.

    Cursors whose wire is unreadable are reported ``unauditable``
    (informational); a cursor with un-stored digests appends a problem naming
    the cursor, the count, and the remedy (``--heal``). The returned dict
    carries the per-cursor rows plus ``total_missing`` and ``unauditable``
    counts for the summary line and the ``deep`` section of ``--json``.
    """
    rows = []
    for task in autosave_tasks:
        entry = states.get(task["task_id"])
        if entry is None:
            continue
        cursors = entry["value"].get("cursors")
        cursors = cursors if isinstance(cursors, dict) else {}
        for key, cursor in cursors.items():
            rows.append(_deep_audit_cursor(key, cursor, conn, task["task_id"],
                                           sessions_root))
    total_missing = sum(r["missing"] for r in rows)
    unauditable = sum(1 for r in rows if r["status"] == "unauditable")
    for r in rows:
        if r["missing"]:
            problems.append(
                f"cursor {r['key']!r} at line {r['line']}: deep digest audit"
                f" found {r['missing']} of {r['digests']} recorded message"
                f" digest(s) un-stored -- run --heal to re-ingest the wire"
                f" from line 0")
    return {"cursors": rows, "total_missing": total_missing,
            "unauditable": unauditable}


def _cursor_report(key, cursor, hashes, sessions_root, now, max_age,
                   problems, conn=None, task_id=None) -> dict:
    start = 0
    if isinstance(cursor, dict):
        start = _as_int(cursor.get("line")) or 0
    elif isinstance(cursor, int) and not isinstance(cursor, bool):
        start = cursor
    row = {"key": key, "line": start, "path": None, "pending": None,
           "mtime": None, "status": "unresolved", "reason": None,
           "legacy": False}
    path = _resolve_cursor(str(key), sessions_root)
    if path is None:
        return row
    row["path"] = str(path)
    if _legacy_cursor(cursor):
        # Being in the old format is a fact about the cursor, not about its
        # trust: a matching mtime can still make the ingester resume from it,
        # and the OK note must name those too, not only the untrusted ones.
        row["legacy"] = True
    mtime, size, head = _wire_marker(path)
    row["mtime"] = mtime
    pending = _pending_count(path, start, hashes)
    row["pending"] = pending
    if pending is None:
        row["status"] = "unreadable"
        return row
    if start > 0:
        # The recorded line only means "everything before it is stored" while
        # the wire is still the file that produced it; once it is not, the next
        # ingest re-scans from line 0 and this cursor is evidence of nothing.
        reason = _untrusted_reason(cursor, path, mtime, size, head)
        if reason is not None and _legacy_cursor(cursor):
            # A cursor in the old format carries no size or head to compare, so
            # nothing about it is evidence -- but nothing about it is a fault
            # either. ``_untrusted_reason`` applies the ingester's own trust
            # test, so a cursor reaching here is one the ingester will not
            # resume from either: it re-scans that wire from line 0, filtering
            # the scan against the digests already stored for the path (see
            # ``Autosave.ingest_session_dir``), and that is what lets the
            # recorded line be ignored without a message reaching the store
            # twice.
            row["legacy"] = True
            row["status"] = "legacy"
            row["reason"] = (
                "its cursor predates the size/head format, so the next ingest"
                " re-scans its wire from line 0 and skips the digests already"
                " stored for it (no message is duplicated)")
        elif reason is not None:
            code, message = reason
            settled = _audit_same_size_rewrite(
                code, conn, task_id, str(key), path, start)
            if settled is not None and settled["stored"]:
                # The rewrite is proven to have changed nothing that was ever
                # saved: the recorded line is no longer an assumption, so the
                # pending count above is trustworthy again and this row falls
                # through to the ordinary current/pending/stale logic.
                row["reason"] = (
                    "rewritten in place (same size and head, new mtime) but a"
                    f" digest audit of its {settled['digests']} recorded"
                    " message digest(s) found every one already stored")
            elif settled is not None:
                detail = (
                    "its wire was rewritten in place (same size and head, new"
                    " mtime) and the digest audit found"
                    f" {settled['missing']} of {settled['digests']} recorded"
                    " message digest(s) not stored -- the rewrite replaced"
                    " content that was never saved")
                row["status"] = "provisional"
                row["pending"] = None
                row["reason"] = detail
                problems.append(
                    f"cursor {key!r} at line {start} cannot be trusted:"
                    f" {detail}; the next ingest re-scans the wire from line"
                    f" 0, so the recorded line does not show that the wire is"
                    f" stored")
                return row
            else:
                row["status"] = "provisional"
                row["pending"] = None
                row["reason"] = message
                problems.append(
                    f"cursor {key!r} at line {start} cannot be trusted:"
                    f" {message}; the next ingest re-scans the wire from line"
                    f" 0, so the recorded line does not show that the wire is"
                    f" stored")
                return row
    if pending == 0:
        row["status"] = "legacy" if row["legacy"] else "current"
        return row
    fresh = mtime is not None and (now.timestamp() - mtime) <= max_age
    row["status"] = "pending" if fresh else "stale"
    if not fresh:
        age = (now.timestamp() - mtime) / 86400 if mtime is not None else None
        where = f"{age:.1f}d old" if age is not None else "undatable"
        problems.append(f"cursor {key!r} has {pending} un-ingested message(s)"
                        f" and its wire is {where}")
    return row


def _last_write(conn, autosave_tasks) -> dict:
    source = ("derived from MAX(messages.created_at) for autosave tasks "
              "(hook signature: title 'Autosave: <slug>' + alias "
              "'auto <slug>'); timestamps are UTC ISO-8601")
    stamp = None
    ids = [t["task_id"] for t in autosave_tasks]
    if ids:
        marks = ",".join("?" * len(ids))
        row = conn.execute(
            f"SELECT MAX(created_at) FROM messages WHERE task_id IN ({marks})",
            ids).fetchone()
        stamp = row[0] if row else None
    return {"timestamp": stamp, "source": source, "age_seconds": _age_seconds(stamp)}


def _fmt_days(seconds) -> str:
    if seconds is None:
        return "?"
    return f"{seconds / 86400:.1f}d"


def _fmt_counts(counts) -> str:
    if not isinstance(counts, dict) or not counts:
        return "-"
    return ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))


def _emit_doctor(report: dict, code: int, as_json: bool) -> int:
    report["ok"] = code == 0
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_doctor(report, code)
    return code


def _print_doctor(report: dict, code: int) -> None:
    hook = report.get("hook") or {}
    state_word = "installed" if hook.get("installed") else "MISSING"
    timeout = hook.get("timeout")
    print(f"hook      {state_word}  timeout="
          f"{timeout if timeout is not None else '?'}s"
          f" sane={bool(hook.get('timeout_sane'))}")
    if hook.get("command"):
        print(f"          command: {hook['command']}")
    if hook.get("installed"):
        events = hook.get("events") or {}
        found = ",".join(e for e in MANAGED_EVENTS if events.get(e)) or "-"
        missing = hook.get("missing_events") or []
        line = f"          events: {found}"
        if missing:
            line += f" (missing: {','.join(missing)})"
        print(line)
    if hook.get("note") and not hook["installed"]:
        print(f"          {hook['note']}")
    if hook.get("parse") == "text":
        print("          parsed with the regex fallback (tomllib unavailable)")
    db = report["db"]
    if not db.get("exists"):
        print(f"db        MISSING  {db.get('path') or 'not checked'}")
    else:
        print(f"db        {db.get('path')}  {db.get('size_bytes', '?')} bytes")
        print(f"schema    messages={'yes' if db.get('has_messages') else 'NO'}"
              f" rows={db.get('messages_total', '?')}"
              f" by_role={_fmt_counts(db.get('messages_by_role'))}"
              f" by_kind={_fmt_counts(db.get('messages_by_kind'))}"
              f" tables={','.join(db.get('tables') or [])}")
    autosave = [t for t in report["tasks"] if t["autosave"]]
    print(f"tasks     {len(report['tasks'])} total, {len(autosave)} autosave")
    for task in report["tasks"]:
        mark = "auto" if task["autosave"] else "    "
        print(f"          {mark} {task['task_id']}  {task['title']}"
              f"  messages={task['messages']}"
              f"  last={task['last_message_at']}")
    last = report["last_write"]
    print(f"last write {last['timestamp']}  (age {_fmt_days(last['age_seconds'])})")
    if last["source"]:
        print(f"          {last['source']}")
    for row in report["state"]:
        print(f"state     task {row['task_id']}  revision={row['revision']}"
              f"  seen={row['seen']} hashes={row['hashes']}")
        print(f"          fact written {row['created_at']}")
        for cursor in row["cursors"]:
            print(f"          cursor {cursor['key']}  line={cursor['line']}"
                  f"  {cursor['status']}"
                  + (f" (pending={cursor['pending']})"
                     if cursor["pending"] else "")
                  + (f"  {cursor['reason']}" if cursor.get("reason") else ""))
    deep = report.get("deep")
    if deep is not None:
        for row in deep["cursors"]:
            if row["status"] == "unauditable":
                print(f"deep      cursor {row['key']}  line={row['line']}"
                      "  unauditable (wire unreadable)")
            elif row["missing"]:
                print(f"deep      cursor {row['key']}  line={row['line']}"
                      f"  digests={row['digests']}  missing={row['missing']}"
                      "  UN-STORED")
            else:
                print(f"deep      cursor {row['key']}  line={row['line']}"
                      f"  missing=0")
        print(f"deep      total missing={deep['total_missing']} across"
              f" {len(deep['cursors'])} cursor(s)"
              f", unauditable={deep['unauditable']}")
    counts = ""
    if db.get("exists") and db.get("messages_total") is not None:
        counts = (f"  messages={db['messages_total']}"
                  f" by_role={_fmt_counts(db.get('messages_by_role'))}"
                  f" by_kind={_fmt_counts(db.get('messages_by_kind'))}")
    if code == 0:
        legacy = sum(1 for row in report["state"]
                     for cursor in row["cursors"] if cursor.get("legacy"))
        note = ""
        if legacy:
            note = (f"; {legacy} cursor(s) predate the size/head format —"
                    " should one of their wires be re-scanned from line 0, the"
                    " digests already stored for it are skipped, so nothing is"
                    " duplicated")
        print(f"verdict   OK — autosave looks healthy"
              f" (max age {_fmt_days(report['max_age_seconds'])})"
              + counts + note)
    elif code == 2:
        print("verdict   CANNOT DIAGNOSE — " + "; ".join(report["problems"])
              + counts)
    else:
        print("verdict   STALE — " + "; ".join(report["problems"]) + counts)


def run_doctor(args) -> int:
    """Read-only health check. Returns 0 healthy, 1 stale, 2 cannot diagnose."""
    report = {"ok": True, "problems": [],
              "hook": {}, "db": {}, "tasks": [], "state": [],
              "last_write": {"timestamp": None, "source": None, "age_seconds": None},
              "max_age_seconds": args.max_age}
    problems = report["problems"]

    if args.max_age <= 0:
        problems.append(f"invalid --max-age {args.max_age} (must be positive seconds)")
        return _emit_doctor(report, 2, args.as_json)

    config_path = Path(os.path.expanduser(str(args.config)))
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        note = f"config not readable: {exc}"
        hook = {"installed": False, "command": None, "timeout": None,
                "timeout_sane": False, "parse": "none", "note": note,
                "error": note}
    else:
        hook = detect_hook(config_text, _hook_target())
    report["hook"] = hook
    if not hook["installed"]:
        problems.append("hook: " + (hook["note"] or "not installed"))
    elif not hook["timeout_sane"]:
        problems.append(f"hook: timeout {hook['timeout']} outside sane range"
                        f" {BUDGET_SECONDS:g}-{MAX_HOOK_TIMEOUT}s")

    resolved = Path(os.path.expanduser(str(args.db))).resolve()
    db_report = {"path": str(resolved), "exists": resolved.is_file(),
                 "size_bytes": None, "tables": [], "has_messages": False,
                 "messages_total": None, "messages_by_role": {},
                 "messages_by_kind": {}, "error": None}
    report["db"] = db_report
    if not db_report["exists"]:
        problems.append(f"db: file not found: {resolved}")
        return _emit_doctor(report, 1, args.as_json)
    try:
        db_report["size_bytes"] = resolved.stat().st_size
    except OSError:
        pass
    try:
        conn = _open_ro(resolved)
    except sqlite3.Error as exc:
        db_report["error"] = f"cannot open read-only: {exc}"
        problems.append("db: " + db_report["error"])
        return _emit_doctor(report, 2, args.as_json)
    try:
        try:
            tables = sorted(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
        except sqlite3.Error as exc:
            db_report["error"] = f"cannot read schema: {exc}"
            problems.append("db: " + db_report["error"])
            return _emit_doctor(report, 2, args.as_json)
        db_report["tables"] = _visible_tables(tables)
        db_report["has_messages"] = "messages" in tables
        try:
            db_report["messages_total"] = conn.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]
            db_report["messages_by_role"] = {
                (k if isinstance(k, str) else "other"): int(c)
                for k, c in conn.execute(
                    "SELECT role, COUNT(*) FROM messages GROUP BY role")}
            db_report["messages_by_kind"] = {
                (k if isinstance(k, str) else "other"): int(c)
                for k, c in conn.execute(
                    "SELECT json_extract(source,'$.kind'), COUNT(*) FROM messages"
                    " GROUP BY 1")}
        except sqlite3.Error:
            pass
        if not db_report["has_messages"] or "tasks" not in tables:
            problems.append("db: not a cmpath database"
                            f" (tables: {','.join(tables) or 'none'})")
            return _emit_doctor(report, 2, args.as_json)

        report["tasks"] = _tasks_report(conn)
        autosave_tasks = [t for t in report["tasks"] if t["autosave"]]
        if not autosave_tasks:
            problems.append("no autosave task found"
                            " (expected a task titled 'Autosave: <project>'"
                            " with the paired alias 'auto <project>')")
        states = _state_facts(conn)
        sessions_root = Path(os.path.expanduser(str(args.sessions_root)))
        now = datetime.now(timezone.utc)
        for task in autosave_tasks:
            entry = states.get(task["task_id"])
            if entry is None:
                if task["messages"]:
                    problems.append(f"task {task['task_id']} has {task['messages']}"
                                    f" message(s) but no current {STATE_KEY} fact")
                continue
            value = entry["value"]
            cursors = value.get("cursors")
            cursors = cursors if isinstance(cursors, dict) else {}
            hashes = value.get("hashes")
            hashes = hashes if isinstance(hashes, list) else []
            row = {"task_id": task["task_id"], "title": task["title"],
                   "revision": entry["revision"], "created_at": entry["created_at"],
                   "seen": len(value.get("seen") or []), "hashes": len(hashes),
                   "cursors": []}
            for key, cursor in cursors.items():
                row["cursors"].append(_cursor_report(
                    key, cursor, hashes, sessions_root, now, args.max_age,
                    problems, conn=conn, task_id=task["task_id"]))
            report["state"].append(row)

        if getattr(args, "deep", False):
            cursor_count = sum(
                len(e["value"].get("cursors"))
                if isinstance(e["value"].get("cursors"), dict) else 0
                for e in (states.get(t["task_id"]) for t in autosave_tasks)
                if e)
            # Printed before the slow loop, and to stderr so --json's stdout
            # stays a single parseable document.
            print(f"deep      digest-auditing {cursor_count} recorded"
                  f" cursor(s), every wire line each -- this is slow on a"
                  f" large store", file=sys.stderr)
            report["deep"] = _deep_audit_all(conn, autosave_tasks, states,
                                             sessions_root, problems)

        last = _last_write(conn, autosave_tasks)
        report["last_write"] = last
        if last["timestamp"] is None:
            problems.append("no autosave messages recorded")
        elif last["age_seconds"] is None:
            problems.append(f"unparseable last autosave timestamp"
                            f" {last['timestamp']!r}")
        elif last["age_seconds"] > args.max_age:
            problems.append(f"last autosave {_fmt_days(last['age_seconds'])} ago"
                            f" exceeds max age {_fmt_days(args.max_age)}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return _emit_doctor(report, 1 if problems else 0, args.as_json)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.path.expanduser(DEFAULT_DB_PATH),
                        help="cmpath database path (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be saved; write nothing")
    parser.add_argument("--ingest", metavar="PATH",
                        help="one-shot transcript ingest of a wire.jsonl")
    parser.add_argument("--rebuild", action="store_true",
                        help="with --ingest: reset this path's cursor and"
                             " re-scan from line 0 (nothing is deleted)")
    parser.add_argument("--session-id", default=None,
                        help="session id for --ingest (source provenance)")
    parser.add_argument("--cwd", default=None,
                        help="project cwd for --ingest (task resolution)")
    parser.add_argument("--session-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--selftest", action="store_true",
                        help="run internal checks and exit")
    parser.add_argument("--heal", action="store_true",
                        help="re-ingest every recorded wire from line 0 to"
                             " heal holes the cursors skipped (append-only,"
                             " dedup-safe; exit 1 if a wire did not finish)")
    parser.add_argument("--doctor", "--status", action="store_true", dest="doctor",
                        help="read-only health check of the autosave automation"
                             " (exit 0 healthy, 1 stale, 2 cannot diagnose)")
    parser.add_argument("--deep", action="store_true",
                        help="with --doctor: digest-audit every recorded"
                             " cursor (lines 1..recorded, prepared exactly as"
                             " the ingester does) against the stored digests."
                             " SLOW: re-reads every wire line of every wire;"
                             " on a large store (200+ cursors, multi-MB"
                             " wires) expect it to take a while. This is the"
                             " absence-detector the ordinary doctor cannot"
                             " be: a cursor recorded past what was ever"
                             " stored is otherwise invisible. Exit 1 when"
                             " any digest is un-stored; --heal is the remedy")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="with --doctor/--heal: machine-readable report")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="with --doctor: kimi-code config.toml"
                             " (default: %(default)s)")
    parser.add_argument("--sessions-root", default=DEFAULT_SESSIONS_ROOT,
                        help="with --doctor/--heal: Kimi Code sessions root"
                             " (default: %(default)s)")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                        metavar="SECONDS",
                        help="with --doctor: staleness window in seconds"
                             " (default: %(default)s = 7 days)")
    args = parser.parse_args(argv)

    if args.heal and args.doctor:
        parser.error("--heal and --doctor are mutually exclusive")
    if args.deep and not args.doctor:
        parser.error("--deep applies only to --doctor")
    if args.selftest:
        return run_selftest()
    if args.ingest:
        return run_manual_ingest(args)
    if args.heal:
        return run_heal(args)
    if args.doctor:
        return run_doctor(args)
    return run_hook(args)


if __name__ == "__main__":
    sys.exit(main())
