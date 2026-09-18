"""Automatic session-evidence capture for Kimi Code sessions.

Design:

- One *project task* per working-directory basename, alias ``auto:<basename>``
  and title ``Autosave: <basename>``. Everything a session produces is
  appended to that single task; no per-session child tasks.
- ``save_turn()`` stores the TurnStarted hook payload prompt as ``role="user"``
  evidence with ``source = {"session", "kind": "prompt", "turn", ...}``. The
  append and the ``autosave_state`` fact that records the prompt's key and
  digest are written in one atomic ``memory.batch()``, so a crash cannot leave
  a task with messages but no dedup state.
- ``ingest_transcript()`` extracts user, assistant and tool messages from a
  ``wire.jsonl`` transcript: ``turn.prompt`` inputs -> user, ``content.part``
  with ``part.type == "text"`` -> assistant, ``tool.call`` / ``tool.result``
  -> tool. Only thinking parts are skipped. Assistant streaming deltas are
  coalesced per model step into whole messages (``iter_wire_coalesced``; group
  key ``(turnId, stepUuid)``, per-line fallback when no ``stepUuid`` is
  present). Tool traffic is coalesced per ``toolCallId``, so a call and its
  result — adjacent in practice — become a single ``role="tool"`` row whose
  text is rendered by ``_tool_call_text`` / ``_tool_result_text``: a header
  naming the tool, ``path`` / ``command`` (or another well-known arg) on its
  own line, the remaining args as bounded one-line JSON, and for a result the
  ``result.output`` string with ``isError`` / ``note`` flagged. The UI-only
  ``display`` field is ignored. Tool rows are capped at ``TOOL_MAX_CHARS``
  and ``source`` gains ``"truncated": true`` when that cap bites. Rows are
  appended with ``source = {"kind": "wire", "path", "line", "agent"}`` where
  ``line`` is the first line of the group or a ``"first-last"`` range.
  ``rebuild=True`` ignores the recorded cursor, re-scans the path from line 0
  and appends only rows this path has not stored yet. Nothing is deleted.
- Idempotency lives in a single per-task fact ``autosave_state``:
  ``{"seen": [ "<session>:<turn>" ... ], "hashes": [ sha1[:16] of saved
  redacted content ], "cursors": { "<path>": {"line", "mtime", "size",
  "head", "complete", "parser"} }, "last_evidence": N}`` where ``head`` is
  ``sha1[:16]`` of the file's first 512 bytes and ``parser`` is
  :data:`_PARSER_VERSION` (see below).
  ``seen`` dedups repeated hook deliveries; ``hashes`` also dedups across the
  two paths (a prompt saved by the hook is not saved again when the same text
  is later seen in the wire transcript), and a scan that restarts at line 0 —
  a rebuild or a cursor reset — additionally filters against the digests
  already stored for that path, because ``hashes`` is bounded by
  :data:`HASH_CAP` and forgets older rows. The cursor requires an
  evidence id to write a fact, so a pass that appends nothing leaves the cursor
  behind and is simply re-scanned next time (hashes prevent double appends).
- A transcript is only resumed past its cursor when it genuinely grew:
  unchanged mtime+size short-circuits, a strictly longer file with an
  unchanged head is an append, and an identical mtime+size resumes a pass that
  stopped at the cap. Anything else — a new mtime at the same size, truncation,
  replacement — resets the cursor to line 0 so new content is never silently
  skipped. A cursor records ``complete: false`` when its pass stopped at the
  row cap or the deadline: such a file is short-circuited only when it is also
  byte-identical, since it was only partly read. Legacy ``{line, mtime}``
  entries upgrade themselves on the first re-scan.
- Every cursor is stamped with :data:`_PARSER_VERSION`, the version of the
  wire parser whose behaviour it describes. A recorded cursor stamped with a
  different version — or with no version, which is what the pre-stamp cursors
  look like — is never resumed: the next ingest takes the rebuild path,
  re-scanning the wire from line 0 against the digests already stored for the
  path, so a change in what the parser stores backfills the rows the previous
  version skipped instead of leaving a silent hole below the cursor.
- All content passes :func:`redact` before it touches the database.
- ``write=False`` (dry-run) never appends, never writes a fact, never creates
  the project task, and returns before any mutation; when the target database
  file does not exist yet, an in-memory database is used so even the file is
  not created.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .memory import TaskMemory, words

__all__ = [
    "Autosave",
    "content_hash",
    "redact",
    "iter_wire_messages",
    "iter_wire_lines",
    "iter_wire_events",
    "iter_wire_coalesced",
    "wire_event_messages",
    "locate_session_dir",
    "audit_wire_cursor",
    "project_name_from_path",
    "DEFAULT_DB_PATH",
    "MAX_MESSAGE_CHARS",
    "TOOL_MAX_CHARS",
    "SEEN_CAP",
    "HASH_CAP",
    "CURSOR_CAP",
]

DEFAULT_DB_PATH = "~/.local/share/cmpath/kimi.db"
MAX_MESSAGE_CHARS = 4000
#: Cap for ``role="tool"`` rows. Kept numerically equal to
#: :data:`MAX_MESSAGE_CHARS` because ``scripts/autosave_session.py`` recomputes
#: ``content_hash(content[:MAX_MESSAGE_CHARS])`` to decide whether a row is
#: already stored; a smaller tool cap would make every over-cap tool row look
#: forever un-ingested to that check. The value is a compromise measured on a
#: real 4.6 MB wire: tool ``args`` ran p90 3.5 kB / max 11.5 kB (29 of 351
#: calls, 8%, truncated) and ``result.output`` p90 5.9 kB / max 35.6 kB (46 of
#: 348 results, 13%, truncated).
TOOL_MAX_CHARS = 4000
SEEN_CAP = 500
HASH_CAP = 1000
CURSOR_CAP = 200
STATE_KEY = "autosave_state"

_PARSER_VERSION = 2
"""Version of the wire parser whose behaviour the recorded cursors describe.

0 is the parser before tool traffic was captured, 1 is the
tool-capturing parser before cursors carried a stamp, 2 is the first stamped
parser. CONTRACT: bump this whenever a change to ``_prepare`` or to the
scan/append semantics would leave already-recorded cursors pointing past
content the previous version did not store. A recorded cursor stamped with
another version — or with no version at all, which is what every pre-stamp
cursor looks like — is then treated as a rebuild: the next ingest re-scans the
wire from line 0 and filters every row against the digests already stored for
the path, backfilling what the older parser skipped instead of silently
resuming past it. The re-scan cannot duplicate a message."""

#: Content classes scrubbed before anything is stored. Order matters: the
#: specific token formats run first so the generic ``key=value`` rule does not
#: double-wrap values already replaced by them.
_REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("gitlab", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{16,}")),
    ("keyval", re.compile(
        r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|credential)[A-Za-z0-9_]*"
        r"|[A-Za-z0-9_]*key[A-Za-z0-9_]*)"
        r"([ \t]*[:=][ \t]*)"
        r"(?:\"(?!\[REDACTED:)[^\"]{8,}\"|'(?!\[REDACTED:)[^']{8,}'|[^\s\"']{8,})")),
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("hex64", re.compile(r"\b[0-9a-fA-F]{64}\b")),
)


def redact(text: str) -> str:
    """Replace secret-looking spans with ``[REDACTED:<kind>]`` markers.

    Conservative by design: anything that matches runs even when the
    surrounding content looks harmless. The generic ``key=value`` rule only
    fires for values of 8+ characters and never re-wraps an already-redacted
    value.
    """
    if not isinstance(text, str):
        text = str(text)
    for kind, pattern in _REDACTIONS:
        if kind == "keyval":
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:keyval]", text)
        else:
            text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


def content_hash(text: str) -> str:
    """Short digest used for autosave dedup (``sha1[:16]`` of UTF-8 text)."""
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:16]


#: Argument names lifted onto their own line in a rendered tool call, in
#: priority order: the first one present wins.
_TOOL_DETAIL_KEYS = ("command", "path", "file_path", "pattern", "query", "url",
                     "prompt")

_GroupKey = tuple


def _compact_json(value: object) -> str:
    """One-line JSON for an arbitrary wire payload (never raises)."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _tool_call_text(inner: dict) -> str:
    """Render one ``tool.call`` payload as plain text: header, detail, args."""
    lines = [f"tool call: {inner.get('name') or '?'}"]
    call_id = inner.get("toolCallId")
    if isinstance(call_id, str) and call_id:
        lines.append(f"toolCallId: {call_id}")
    args = inner.get("args")
    if isinstance(args, dict):
        rest = dict(args)
        for key in _TOOL_DETAIL_KEYS:
            value = rest.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{key}: {value}")
                del rest[key]
                break
        if rest:
            lines.append(f"args: {_compact_json(rest)}")
    elif args not in (None, ""):
        lines.append(f"args: {_compact_json(args)}")
    return "\n".join(lines) + "\n\n"


def _tool_result_text(inner: dict) -> str:
    """Render one ``tool.result`` payload as plain text: header, note, output."""
    result = inner.get("result")
    is_error = isinstance(result, dict) and result.get("isError") is True
    lines = ["tool result: error" if is_error else "tool result"]
    call_id = inner.get("toolCallId")
    if isinstance(call_id, str) and call_id:
        lines.append(f"toolCallId: {call_id}")
    note = inner.get("note")
    if isinstance(note, str) and note.strip():
        lines.append(f"note: {note}")
    if isinstance(result, dict):
        output = result.get("output")
        if isinstance(output, str):
            if output.strip():
                lines.append("output:")
                lines.append(output)
        elif output is not None:
            lines.append(f"output: {_compact_json(output)}")
    elif result is not None:
        lines.append(f"result: {_compact_json(result)}")
    return "\n".join(lines) + "\n\n"


def _tool_key(inner: dict, lineno: int) -> _GroupKey:
    """Group key for a tool event: per ``toolCallId``, else per line."""
    call_id = inner.get("toolCallId")
    if isinstance(call_id, str) and call_id:
        return ("tool", call_id)
    return ("tool-line", lineno)


def _inner_items(inner: object, lineno: int) -> list[tuple[str, str, _GroupKey]]:
    """``(role, text, group_key)`` for one loop-event payload."""
    if not isinstance(inner, dict):
        return []
    kind = inner.get("type")
    if kind == "content.part":
        part = inner.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            return []
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            return []
        step_uuid = inner.get("stepUuid")
        key = ("step", inner.get("turnId"), step_uuid) if step_uuid \
            else ("event", lineno)
        return [("assistant", text, key)]
    if kind == "tool.call":
        return [("tool", _tool_call_text(inner), _tool_key(inner, lineno))]
    if kind == "tool.result":
        return [("tool", _tool_result_text(inner), _tool_key(inner, lineno))]
    return []


def _wire_items(event: object, lineno: int) -> list[tuple[str, str, _GroupKey]]:
    """``(role, text, group_key)`` triples carried by one wire.jsonl event."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == "turn.prompt":
        blocks = [
            part["text"]
            for part in event.get("input") or ()
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str) and part["text"].strip()
        ]
        return [("user", "\n".join(blocks), ("prompt", lineno))] if blocks else []
    if kind == "context.append_loop_event":
        return _inner_items(event.get("event"), lineno)
    if kind in ("tool.call", "tool.result"):
        return _inner_items(event, lineno)  # tolerates a flattened writer
    return []


def wire_event_messages(event: object) -> list[tuple[str, str]]:
    """User, assistant and tool messages carried by one wire.jsonl event.

    Observed wire schema: ``turn.prompt`` inputs are
    ``{"type": "text", "text": ...}`` blocks (the user prompt); assistant
    output text arrives as ``context.append_loop_event`` events whose inner
    event is ``content.part`` with ``part.type == "text"``; tool traffic is the
    inner ``tool.call`` / ``tool.result`` events, rendered by
    :func:`_tool_call_text` / :func:`_tool_result_text`. Only ``think`` parts
    are dropped; the UI-only ``display`` field is ignored. Tool text is capped
    at :data:`TOOL_MAX_CHARS` downstream in :meth:`Autosave.ingest_transcript`.
    """
    return [(role, text) for role, text, _key in _wire_items(event, 0)]


def iter_wire_lines(path: str | os.PathLike[str], start_line: int = 0):
    """Yield ``(lineno, messages)`` for each fully parsed wire.jsonl line.

    ``messages`` is the :func:`wire_event_messages` list for that line (it may
    be empty). Lines up to ``start_line`` are skipped. Iteration stops at the
    first line that is not valid JSON: wire files are append-only JSONL, so a
    parse failure means the writer is mid-append on that line (or the file is
    corrupt there) and it will be re-read on a later pass.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            if lineno <= start_line:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                break
            yield lineno, wire_event_messages(event)


def iter_wire_messages(path: str | os.PathLike[str], start_line: int = 0):
    """Yield ``(lineno, role, text)`` from a wire.jsonl, 1-based line numbers."""
    for lineno, messages in iter_wire_lines(path, start_line):
        for role, text in messages:
            yield lineno, role, text


def iter_wire_events(path: str | os.PathLike[str], start_line: int = 0):
    """Yield ``(lineno, role, text, group_key)`` for content-carrying events.

    ``group_key`` identifies the logical message a fragment belongs to:

    - assistant streaming deltas share ``("step", turnId, stepUuid)`` (observed
      schema: ``context.append_loop_event`` → ``content.part`` events carry
      ``turnId``/``stepUuid``; empty ``think`` parts interleave freely and do
      not break a group). Events without a ``stepUuid`` get a per-line key so
      they are never merged into unrelated text;
    - user prompts get a per-line key so they always stand alone;
    - tool traffic shares ``("tool", toolCallId)``, so a ``tool.call`` and its
      ``tool.result`` coalesce into a single ``role="tool"`` row. Parallel calls
      carry distinct ids and stay separate rows; a tool event without an id
      falls back to a per-line ``("tool-line", lineno)`` key.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            if lineno <= start_line:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                break  # append-only file: mid-append line, re-read next pass
            yield from ((lineno, role, text, key)
                        for role, text, key in _wire_items(event, lineno))


def iter_wire_coalesced(path: str | os.PathLike[str], start_line: int = 0):
    """Coalesce streaming deltas into whole messages.

    Yields ``(first_line, last_line, role, text)`` where consecutive fragments
    sharing a role and a group key (see :func:`iter_wire_events`) are joined
    into one message. That joins assistant deltas back into whole messages and
    merges a ``tool.call`` with its ``tool.result``; complete short messages are
    kept as-is. Note that joining only ever happens across *contiguous*
    fragments: an interleaved event of another group (in practice, a tool call
    arriving mid-step) ends the current row, so the row boundaries follow the
    file order.
    """
    buf_role = buf_key = None
    buf_parts: list[str] = []
    first = last = 0

    def flush():
        nonlocal buf_role, buf_key, buf_parts
        if buf_role is None:
            return None
        text = "".join(buf_parts)
        role, f, l = buf_role, first, last
        buf_role = buf_key = None
        buf_parts = []
        return (f, l, role, text)

    for lineno, role, text, key in iter_wire_events(path, start_line):
        if role != buf_role or key != buf_key:
            out = flush()
            if out is not None:
                yield out
            buf_role, buf_key = role, key
            first = lineno
        buf_parts.append(text)
        last = lineno
    out = flush()
    if out is not None:
        yield out


def project_name_from_path(path: str | os.PathLike[str]) -> str | None:
    """Project slug from a path under ``~/.kimi-code/sessions/wd_<slug>_<hash>/``."""
    for parent in Path(path).parents:
        match = re.fullmatch(r"wd_(.+)_[0-9a-f]{6,}", parent.name)
        if match:
            return match.group(1)
    return None


def locate_session_dir(session_id: str, root: str | os.PathLike[str] | None = None):
    """Find the session directory for a hook payload ``session_id``.

    Session dirs are ``<root>/wd_<slug>_<hash>/session_<uuid>``; payloads have
    carried the bare uuid in practice, so matching tolerates an optional
    ``session_`` / ``ses_`` prefix. Returns the newest match, or None.
    """
    if not session_id:
        return None
    root = Path(root) if root else Path(os.path.expanduser("~/.kimi-code/sessions"))
    if not root.is_dir():
        return None
    sid = str(session_id)
    bare = sid
    for prefix in ("session_", "ses_"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
    matches: list[Path] = []
    for workspace in root.iterdir():
        if not workspace.is_dir():
            continue
        for child in workspace.iterdir():
            if child.is_dir() and (child.name == sid or child.name.endswith(bare)):
                matches.append(child)
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _prepare_text(text: str,
                  limit: int = MAX_MESSAGE_CHARS) -> tuple[str, str, bool] | None:
    """Redact, strip and truncate; returns ``(content, hash, truncated)``.

    The one preparation every stored message goes through; both the ingester
    (``Autosave._prepare``) and the doctor's cursor audit call this, so the
    two can never disagree about what a wire line's digest is.
    """
    content = redact(text).strip()
    if not content:
        return None
    if len(content) > limit:
        content = content[:limit]
        truncated = True
    else:
        truncated = False
    return content, content_hash(content), truncated


def _stored_wire_digests(db, task_id: int | None, cursor_key: str) -> set[str]:
    """Digests of the wire rows ``db`` already holds for ``cursor_key``.

    Shared by ``Autosave._wire_digests`` and :func:`audit_wire_cursor`; the
    query and the hashing of the stored content live here so both callers
    read the same set. ``db`` is only ever queried.
    """
    if task_id is None:
        return set()
    rows = db.execute(
        "SELECT content FROM messages WHERE task_id=? "
        "AND json_extract(source,'$.kind')='wire' "
        "AND json_extract(source,'$.path')=?",
        (task_id, cursor_key)).fetchall()
    return {content_hash(row[0]) for row in rows}


def audit_wire_cursor(path: str | os.PathLike[str], recorded_line: int,
                      db, task_id: int | None, cursor_key: str) -> dict | None:
    """Digest-audit a cursor recorded on a wire rewritten in place.

    A same-size rewrite (same size, same head, new mtime) is the only
    untrusted-cursor case that leaves the file *length* unchanged, so the
    recorded line can be proven instead of assumed: every coalesced message
    in wire lines 1..``recorded_line`` is prepared exactly as
    :meth:`Autosave.ingest_transcript` prepares it (same redaction, caps and
    hashing, via :func:`_prepare_text`) and compared against the digests the
    database already holds for ``cursor_key``. Every digest stored means the
    rewrite changed nothing that was ever saved; any un-stored digest means
    the rewrite replaced content that was never saved.

    Returns ``{"stored": bool, "digests": <count>, "missing": <count>}``
    (``missing`` is 0 when ``stored``), or ``None`` when the wire cannot be
    read at all. Database errors propagate to the caller, which decides how
    loudly to fail. ``db`` is only ever queried: the doctor passes its
    read-only connection, the ingester would pass its own database.

    Caveat inherited from the ingester: two distinct messages longer than the
    truncation cap that share a truncated prefix collapse to one digest, so
    the audit can pass where a line-exact check would not.
    """
    expected: set[str] = set()
    try:
        for first, _last, role, text in iter_wire_coalesced(path):
            if first > recorded_line:
                break
            limit = TOOL_MAX_CHARS if role == "tool" else MAX_MESSAGE_CHARS
            prepared = _prepare_text(text, limit)
            if prepared is not None:
                expected.add(prepared[1])
    except OSError:
        return None
    stored = _stored_wire_digests(db, task_id, cursor_key)
    missing = len(expected - stored)
    return {"stored": not missing, "digests": len(expected),
            "missing": missing}


class Autosave:
    """Save Kimi Code session content into a cmpath TaskMemory database."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None, *,
                 write: bool = True,
                 seen_cap: int = SEEN_CAP,
                 hash_cap: int = HASH_CAP,
                 cursor_cap: int = CURSOR_CAP):
        self.write = write
        self.seen_cap = max(1, int(seen_cap))
        self.hash_cap = max(1, int(hash_cap))
        self.cursor_cap = max(1, int(cursor_cap))
        path = os.path.expanduser(str(db_path or DEFAULT_DB_PATH))
        if not write and not os.path.exists(path):
            path = ":memory:"  # dry-run must not create the database file
        self.db_path = path
        self.memory = TaskMemory(self.db_path)

    def close(self) -> None:
        self.memory.close()

    def __enter__(self) -> "Autosave":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- project task -----------------------------------------------------

    def project_task(self, cwd: str | None, *,
                     create: bool = True) -> tuple[int | None, bool]:
        """Task id for a working directory, creating it on first use.

        Returns ``(task_id, created)``. Resolution is an exact normalized-
        alias scan for ``auto:<basename(cwd)>``; an empty cwd maps to
        ``auto:unknown``. With ``create=False`` an unknown project yields
        ``(None, False)`` instead of a new task.
        """
        return self._project_task_by_name(self._project_name(cwd), create=create)

    def _project_name(self, cwd: str | None) -> str:
        name = os.path.basename(os.path.normpath(cwd or "")) if cwd else ""
        return name or "unknown"

    def _project_task_by_name(self, name: str, *,
                              create: bool = True) -> tuple[int | None, bool]:
        alias = f"auto:{name}"
        normalized = " ".join(words(alias))
        for task in self.memory.tasks():
            if any(" ".join(words(a)) == normalized for a in task.aliases):
                return task.id, False
        if not create:
            return None, False
        title = f"Autosave: {name}"
        return self.memory.create_task(title, aliases=[alias]).id, True

    # -- dedup state ------------------------------------------------------

    def _state(self, task_id: int | None) -> dict:
        if task_id is None:
            return {"seen": [], "hashes": [], "cursors": {},
                    "last_evidence": None}
        fact = self.memory.fact(task_id, STATE_KEY)
        value = fact["value"] if fact and isinstance(fact.get("value"), dict) else {}
        state = {
            "seen": list(value.get("seen") or []),
            "hashes": list(value.get("hashes") or []),
            "cursors": dict(value.get("cursors") or {}),
        }
        last = value.get("last_evidence")
        state["last_evidence"] = int(last) if isinstance(last, int) else None
        return state

    def _store_state(self, task_id: int, state: dict) -> None:
        evidence_id = state.get("last_evidence")
        if evidence_id is None:
            return  # facts require same-task provenance; nothing to anchor to yet
        payload = {
            "seen": state["seen"][-self.seen_cap:],
            "hashes": state["hashes"][-self.hash_cap:],
            "cursors": dict(list(state["cursors"].items())[-self.cursor_cap:]),
            "last_evidence": evidence_id,
        }
        self.memory.set_fact(task_id, STATE_KEY, payload, evidence_id=evidence_id)

    @staticmethod
    def _file_marker(path: Path, head_bytes: int = 512):
        """``(mtime, size, sha1[:16] of the first ``head_bytes``)``.

        All three are ``None`` when the file cannot be read.
        """
        try:
            with open(path, "rb") as fh:
                head = hashlib.sha1(fh.read(head_bytes)).hexdigest()[:16]
            stat = path.stat()
            return stat.st_mtime, stat.st_size, head
        except OSError:
            return None, None, None

    def _prepare(self, text: str,
                 limit: int = MAX_MESSAGE_CHARS) -> tuple[str, str, bool] | None:
        """Redact, strip and truncate; returns ``(content, hash, truncated)``."""
        return _prepare_text(text, limit)

    def _wire_digests(self, task_id: int | None, cursor_key: str) -> set[str]:
        """Digests of the wire rows this path has already stored.

        The bounded ``hashes`` list cannot dedup a re-scan on its own: it keeps
        only :data:`HASH_CAP` entries, so a re-scan longer than that window
        would re-append rows the database already holds. The stored content of
        the path recovers the full set.
        """
        return _stored_wire_digests(self.memory._db, task_id, cursor_key)

    # -- public capture API -------------------------------------------------

    def save_turn(self, session_id: str, cwd: str | None, prompt: str, *,
                  turn_id: str | int | None = None,
                  extra: dict | None = None) -> dict:
        """Append the user prompt of one turn; idempotent per session+turn.

        The message row and the ``autosave_state`` fact that records its key
        and digest commit in one atomic batch: an error in either rolls both
        back, so a saved prompt never outlives its dedup state.

        Returns ``{"status": "saved"|"duplicate"|"empty"|"would_save", ...}``.
        """
        task_id, _created = self.project_task(cwd, create=self.write)
        state = self._state(task_id)
        prepared = self._prepare(prompt or "")
        if prepared is None:
            return {"status": "empty", "task_id": task_id}
        content, digest, truncated = prepared
        key = f"{session_id or '?'}:{turn_id if turn_id is not None else '?'}"
        if key in state["seen"] or digest in state["hashes"]:
            return {"status": "duplicate", "task_id": task_id, "key": key}
        source = {"session": str(session_id or ""), "kind": "prompt"}
        if turn_id is not None:
            source["turn"] = str(turn_id)
        if truncated:
            source["truncated"] = True
        if extra:
            source.update(extra)
        if not self.write:
            return {"status": "would_save", "task_id": task_id, "key": key,
                    "chars": len(content), "truncated": truncated}
        # One atomic group: the message row and the autosave_state fact that
        # records its digest either both land or neither does. A crash between
        # them used to leave a task with messages but no state fact — the
        # doctor's exit-1 rule 4 — and its recovery re-scan from line 0 without
        # the stored-digest filter, able to duplicate rows. ingest_transcript
        # already commits its rows and cursors the same way.
        with self.memory.batch():
            evidence = self.memory.append(task_id, "user", content, source=source)
            state["seen"] = (state["seen"] + [key])[-self.seen_cap:]
            state["hashes"] = (state["hashes"] + [digest])[-self.hash_cap:]
            state["last_evidence"] = evidence.id
            self._store_state(task_id, state)
        return {"status": "saved", "task_id": task_id, "citation": evidence.citation,
                "chars": len(content), "truncated": truncated}

    def ingest_transcript(self, path: str | os.PathLike[str], *,
                          session_id: str | None = None,
                          cwd: str | None = None,
                          deadline: float | None = None,
                          rebuild: bool = False) -> dict:
        """Append user, assistant and tool messages from a wire.jsonl transcript.

        Assistant streaming deltas are coalesced into whole messages per model
        step and tool traffic is coalesced per ``toolCallId`` (so a tool call
        and its adjacent result become one ``role="tool"`` row). A complete
        short message is kept as-is, and only text that is empty after
        redaction is dropped. ``role="tool"`` rows are capped at
        :data:`TOOL_MAX_CHARS` instead of :data:`MAX_MESSAGE_CHARS`, and the
        row's ``source`` records ``"truncated": true`` when the cap bit.
        Idempotent: a per-path cursor in ``autosave_state`` records the last
        fully parsed line, and the shared hash list drops content that another
        path (or the hook prompt path) already saved.

        With ``rebuild=True`` the recorded cursor is ignored and the path is
        re-scanned from line 0, appending only rows whose digest this path has
        not already stored. A cursor reset (a truncated, rewritten or replaced
        file) re-scans from line 0 too and filters against those same stored
        digests. So does a cursor stamped with another :data:`_PARSER_VERSION`
        — or with none, which is every pre-stamp cursor: that mismatch means
        the recorded line may sit past content an older parser never stored,
        so the next ingest migrates it the same dedup-safe way and re-stamps
        the cursor once the scan completes. Nothing is deleted: stored
        evidence is append-only, so running rebuild twice converges to the
        same rows and never duplicates, even once the digest window has
        evicted the rows a re-scan needs to skip. The report carries
        ``reset_cursor=True`` whenever the scan restarted at line 0, which
        includes the version-migration re-scan.

        With ``write=False`` nothing is appended, no state fact is written and
        the project task is not created; the report only describes what a real
        run would do.
        """
        path = Path(path)
        if not path.is_file():
            return {"status": "missing", "path": str(path)}
        name = (self._project_name(cwd) if cwd else None) \
            or project_name_from_path(path) or "unknown"
        task_id, _created = self._project_task_by_name(name, create=self.write)
        cursor_key = self._cursor_key(path)
        state = self._state(task_id)

        entry = state["cursors"].get(cursor_key)
        complete = True
        if isinstance(entry, dict):
            start = int(entry.get("line", 0))
            seen_mtime = entry.get("mtime")
            seen_size = entry.get("size")
            seen_head = entry.get("head")
            # A full modern entry with no flag may come from a pass that stopped
            # at the cap, so it is treated as incomplete and re-scanned. Legacy
            # ``{line, mtime}`` entries carry no size/head and keep their old
            # behaviour: they reset on the next append anyway.
            complete = bool(entry.get("complete",
                                      not ("size" in entry and "head" in entry)))
        elif isinstance(entry, int):
            start, seen_mtime, seen_size, seen_head = entry, None, None, None
        else:
            start, seen_mtime, seen_size, seen_head = 0, None, None, None

        marker_mtime, marker_size, marker_head = self._file_marker(path)
        reset_cursor = False
        if rebuild:
            start, reset_cursor = 0, True
        elif isinstance(entry, dict) and start > 0 \
                and entry.get("parser") != _PARSER_VERSION:
            # A cursor written by another parser version — including every
            # pre-stamp cursor, which records no version at all — points past
            # lines that version may never have stored (the incident that
            # the parser began capturing tool traffic, but the cursors it had
            # already written kept advancing past the lines the old parser
            # skipped). Resuming from such a cursor is exactly the silent
            # hole the stamp exists to prevent, so it takes the rebuild path:
            # the scan below restarts at line 0 and filters against the
            # digests already stored for the path, backfilling without
            # duplicating. The ``reset_cursor`` in the report makes this
            # one-time migration visible, and the cursor written below is
            # stamped with the current version, so it happens once.
            start, reset_cursor = 0, True
        elif start > 0:
            if complete and seen_mtime is not None \
                    and seen_mtime == marker_mtime \
                    and (seen_size is None or seen_size == marker_size):
                return {"status": "unchanged", "task_id": task_id,
                        "path": str(path), "scanned_to": start, "added": 0,
                        "reset_cursor": False}
            same_head = seen_head is not None and marker_head == seen_head
            longer = (seen_size is not None and marker_size is not None
                      and marker_size > seen_size)
            # Same file, same mtime and size, with an incomplete cursor: the
            # pass stopped at the cap, so resuming is right. A same-size file
            # with a *new* mtime is a rewrite and must reset.
            untouched = (seen_mtime is not None and seen_mtime == marker_mtime
                         and (seen_size is None or seen_size == marker_size))
            if same_head and (longer or untouched):
                pass  # an append, or the same file resuming an incomplete pass
            else:
                start, reset_cursor = 0, True  # truncated, rewritten or legacy

        entries: list[tuple[int | str, str, str, str, bool]] = []
        scanned_to = start
        stopped_early = False
        # A reset re-scans a path whose older rows may have fallen outside the
        # bounded ``hashes`` window, so it needs the same stored-digest filter
        # a rebuild uses. The hot paths (fresh path, genuine append, unchanged
        # short-circuit) never reset and so never pay for this SELECT.
        stored = (self._wire_digests(task_id, cursor_key)
                  if (rebuild or reset_cursor) else set())
        for first, last, role, text in iter_wire_coalesced(path, start_line=start):
            if deadline is not None and _monotonic() > deadline:
                stopped_early = True
                break
            scanned_to = last
            limit = TOOL_MAX_CHARS if role == "tool" else MAX_MESSAGE_CHARS
            prepared = self._prepare(text, limit)
            if prepared is None:
                continue
            content, digest, truncated = prepared
            if digest in state["hashes"] or digest in stored:
                continue
            line_ref: int | str = first if first == last else f"{first}-{last}"
            entries.append((line_ref, role, content, digest, truncated))
            if len(entries) >= 500:  # one pass, bounded work
                stopped_early = True
                break

        if not self.write:
            return {"status": "would_ingest" if entries else "clean",
                    "task_id": task_id, "path": str(path), "scanned_to": scanned_to,
                    "added": len(entries), "reset_cursor": reset_cursor,
                    "preview": [{"line": n, "role": r, "chars": len(c)}
                                for n, r, c, _h, _t in entries[:5]]}
        if not entries and scanned_to <= start:
            return {"status": "clean", "task_id": task_id, "path": str(path),
                    "scanned_to": scanned_to, "added": 0,
                    "reset_cursor": reset_cursor}

        added = 0
        with self.memory.batch():
            agent = path.parent.name if path.parent.name not in ("agents", ".") else "main"
            for line_ref, role, content, digest, truncated in entries:
                source = {"session": str(session_id or ""), "kind": "wire",
                          "path": cursor_key, "line": line_ref, "agent": agent}
                if truncated:
                    source["truncated"] = True
                evidence = self.memory.append(task_id, role, content, source=source)
                state["hashes"] = (state["hashes"] + [digest])[-self.hash_cap:]
                state["last_evidence"] = evidence.id
                added += 1
            if scanned_to > start:
                state["cursors"][cursor_key] = {"line": scanned_to,
                                                "mtime": marker_mtime,
                                                "size": marker_size,
                                                "head": marker_head,
                                                "complete": not stopped_early,
                                                "parser": _PARSER_VERSION}
            self._store_state(task_id, state)
        return {"status": "ingested" if added else "clean", "task_id": task_id,
                "path": str(path), "scanned_to": scanned_to, "added": added,
                "reset_cursor": reset_cursor}

    def ingest_session_dir(self, session_dir: str | os.PathLike[str], *,
                           session_id: str | None = None,
                           cwd: str | None = None,
                           deadline: float | None = None) -> list[dict]:
        """Ingest every ``agents/*/wire.jsonl`` under a session directory.

        The main agent wire (``agents/main/wire.jsonl``) is processed before
        subagent wires. Stops early when ``deadline`` (a monotonic timestamp)
        passes. Files that do not exist are skipped.
        """
        session_dir = Path(session_dir)
        wires: list[Path] = []
        top = session_dir / "wire.jsonl"
        if top.is_file():
            wires.append(top)
        agents_dir = session_dir / "agents"
        if agents_dir.is_dir():
            found = [p for p in agents_dir.glob("*/wire.jsonl") if p.is_file()]
            found.sort(key=lambda p: (0 if p.parent.name == "main" else 1, p.parent.name))
            wires.extend(found)
        results = []
        for wire in wires:
            if deadline is not None and _monotonic() > deadline:
                break
            results.append(self.ingest_transcript(
                wire, session_id=session_id, cwd=cwd, deadline=deadline))
        return results

    def optimize_index(self) -> bool:
        """Merge the FTS5 segments of the evidence index (``'optimize'``).

        Every appended message also feeds the ``evidence_index`` FTS5 table one
        row at a time, and FTS5 accumulates a fresh segment per merge batch: a
        bulk append — a ``--heal`` sweep above all — fragments the index, and
        every later search pays for the fragmentation. Running the FTS5
        ``optimize`` command once after a bulk append merges those segments
        back into one. On an index that is already fully merged the command is
        effectively a no-op cost-wise (it walks the index but merges nothing),
        so calling it speculatively after a small append is safe.

        With ``write=False`` this is a no-op returning ``False``: a dry run
        must not touch the database at all, and the optimize command writes.
        Returns ``True`` when the command ran; sqlite errors propagate to the
        caller, which decides how loudly to fail.
        """
        if not self.write:
            return False
        self.memory._db.execute(
            "INSERT INTO evidence_index(evidence_index) VALUES('optimize')")
        return True

    def _cursor_key(self, path: Path) -> str:
        session_slug = project_name_from_path(path)
        if session_slug:
            for parent in path.parents:
                if re.fullmatch(r"wd_.+_[0-9a-f]{6,}", parent.name):
                    try:
                        return str(path.relative_to(parent))
                    except ValueError:
                        break
        return str(path)


def _monotonic() -> float:
    return time.monotonic()
