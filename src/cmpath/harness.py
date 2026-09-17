"""Harness lifecycle integration with a replaceable backend and native Go transport.

The backend protocol is language-neutral. The included NativeBackend keeps one
Go process alive; a Go harness can instead import the engine directly.
"""
from __future__ import annotations

from collections import deque
import copy
import inspect
import json
import math
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any, Callable, Mapping, Protocol
import uuid


def _invalid_json_constant(value):
    raise ValueError("JSON must be finite: " + value)


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


_MISSING = object()
_ROUTE_MISSING = object()


class Backend(Protocol):
    """Implement this boundary to use another native runtime or transport."""

    def call(self, operation: str, **arguments): ...
    def close(self) -> None: ...


class NativeBackend:
    """A serialized, persistent stdio connection. Requests are never auto-retried."""

    MAX_FRAME = 16 * 1024 * 1024

    def __init__(self, binary: str | Path, database: str | Path, *,
                 create: bool = False, timeout: float = 15.0):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        command = [str(Path(binary).resolve()), "--db", str(database)]
        if create:
            command.append("--create")
        self.timeout = timeout
        self._lock = threading.RLock()
        self._closed = False
        self._responses: queue.Queue = queue.Queue()
        self._stderr: deque[bytes] = deque(maxlen=32)
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         bufsize=-1)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._errors = threading.Thread(target=self._read_errors, daemon=True)
        self._reader.start()
        self._errors.start()
        try:
            self.info = self.call("info")
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while True:
                raw = self._process.stdout.readline(self.MAX_FRAME + 1)
                if not raw:
                    raise EOFError("native process closed its response stream")
                if len(raw) > self.MAX_FRAME or not raw.endswith(b"\n"):
                    raise ValueError("native response exceeds the frame limit")
                self._responses.put(json.loads(raw))
        except Exception as exc:
            self._responses.put(exc)

    def _read_errors(self):
        try:
            while chunk := self._process.stderr.read(2048):
                self._stderr.append(chunk)
        except (OSError, ValueError):
            # ``close`` may close the pipe while this daemon is blocked in a
            # read.  The process has already been waited on at that point;
            # there is no stderr response to recover, so let the thread drain.
            pass

    def _kill(self):
        if self._process.poll() is not None:
            return
        try:
            self._process.kill()
        except (OSError, ProcessLookupError):
            # A process can exit between poll() and kill().  Do not hide a
            # failure when it is still alive, since that would leave its DB
            # handle open and make the transport outcome harder to diagnose.
            if self._process.poll() is None:
                raise

    @staticmethod
    def _close_stream(stream):
        if stream is None or stream.closed:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            # Closing an already-broken Windows pipe is best effort.  The
            # process wait and reader joins below are the ownership boundary.
            pass

    def _wait_for_exit(self):
        """Wait for the child, force it down if it ignores stdin EOF."""
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._kill()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # TerminateProcess should be definitive on Windows.  A final
                # unbounded wait keeps close from returning while the child
                # still owns the SQLite database or pipe handles.
                self._kill()
                self._process.wait()

    def _drain_readers(self):
        """Close inherited pipes and wait until both reader threads finish."""
        self._close_stream(self._process.stdout)
        self._close_stream(self._process.stderr)
        self._reader.join()
        self._errors.join()

    def call(self, operation: str, **arguments):
        with self._lock:
            if self._closed:
                raise HarnessError("closed", "native backend is closed")
            request_id = uuid.uuid4().hex
            raw = (json.dumps({"v": 1, "id": request_id, "op": operation,
                               "args": arguments}, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            if len(raw) > self.MAX_FRAME:
                raise ValueError("native request exceeds the 16 MiB frame limit")
            deadline = threading.Timer(self.timeout, self._kill)
            deadline.daemon = True
            deadline.start()
            try:
                # FileIO writes can be partial, particularly for large batches.
                remaining = memoryview(raw)
                while remaining:
                    written = self._process.stdin.write(remaining)
                    if not written:
                        raise BrokenPipeError("native request stream closed")
                    remaining = remaining[written:]
                self._process.stdin.flush()
                response = self._responses.get(timeout=self.timeout)
                if isinstance(response, Exception):
                    raise response
                if response.get("v") != 1 or response.get("id") != request_id:
                    raise ValueError("native response identity mismatch")
            except Exception as exc:
                self._kill()
                self._closed = True
                self._process.wait(timeout=5)
                raise HarnessError("transport", "Native request outcome is uncertain. Reopen the database and inspect the logical request ID before retrying.") from exc
            finally:
                deadline.cancel()
            if "error" in response:
                error = response["error"]
                raise HarnessError(error["code"], error["message"])
            return response.get("result")

    def close(self):
        with self._lock:
            self._closed = True
            try:
                # Closing stdin is the graceful protocol shutdown.  If a
                # peer has already gone away, continue to wait/close all
                # remaining handles rather than returning early.
                self._close_stream(self._process.stdin)
            finally:
                try:
                    self._wait_for_exit()
                finally:
                    self._drain_readers()


def _default_hybrid_route():
    """Return the router's hybrid enum value when it is available.

    The router is deliberately imported lazily.  ``Harness`` remains usable
    with older installations and with small test doubles that do not install
    ``cmpath.router``.  The compatibility spellings cover the public enum
    names used by the router implementation; if none is present, ``None``
    tells the router to use its own default route.
    """
    try:
        from . import router as router_module
    except (ImportError, AttributeError):
        return None
    for enum_name in ("MemoryRoute", "Route"):
        enum = getattr(router_module, enum_name, None)
        members = getattr(enum, "__members__", {})
        for member_name in ("HYBRID", "PINNED_ON_DEMAND", "PINNED_AND_ON_DEMAND",
                            "PINNED_PLUS_ON_DEMAND"):
            if member_name in members:
                return members[member_name]
    return None


def _router_context(router, query: str, task_id: int | None,
                    requested_route=_ROUTE_MISSING, **route_options):
    """Ask a router for one bounded context without implementing routing here.

    ``retrieve`` is the current public API.  ``prepare`` and ``query`` are
    accepted as compatibility seams for plugin adapters in the wild; all
    methods receive the same route arguments when their signature supports
    them.  This function never retries a call.  Retrieval is read-only, but a
    second call could still produce a different snapshot and would undermine
    the turn's source identity.
    """
    if router is None:
        raise TypeError("router is required for routed turns")
    retrieve = None
    for name in ("retrieve", "prepare", "query", "context",
                 "context_for_turn", "route_context"):
        candidate = getattr(router, name, None)
        if callable(candidate):
            retrieve = candidate
            break
    if retrieve is None:
        raise TypeError("router must expose retrieve(query, ...) or a compatible context method")

    # Do not catch TypeError from inside a router: silently trying another
    # signature could route against a different database snapshot.  Signature
    # inspection lets simple plugin doubles omit optional keywords while the
    # canonical MemoryRouter receives the complete request.
    try:
        parameters = inspect.signature(retrieve).parameters
    except (TypeError, ValueError):
        # Opaque extension callables (some C-backed plugin functions and
        # mocks) cannot expose a signature.  The canonical router contract is
        # the safest call shape in that case; do not silently drop task or
        # bound options and route a broader context.
        parameters = None
    if parameters is None:
        accepts_kwargs = True
        parameters = {}
    else:
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                             for p in parameters.values())
    kwargs: dict[str, Any] = {}
    if task_id is not None and (accepts_kwargs or "task_id" in parameters):
        kwargs["task_id"] = task_id
    if requested_route is not _ROUTE_MISSING and (accepts_kwargs or "requested_route" in parameters):
        kwargs["requested_route"] = requested_route
    for name, value in route_options.items():
        if value is not _MISSING and (accepts_kwargs or name in parameters):
            kwargs[name] = value
    return retrieve(query, **kwargs)


def _context_value(context, name: str, default=_MISSING):
    """Read a value from a routed context using method/property/dict forms."""
    if context is None:
        return default
    value = context.get(name, default) if isinstance(context, Mapping) else getattr(context, name, default)
    if callable(value):
        return value()
    return value


class Harness:
    """Binds memory hooks to a caller-owned model/planner/tool loop.

    The normal :meth:`begin`/:meth:`run` path is intentionally unchanged.
    Hosts that want hybrid memory routing must opt in with
    :meth:`prepare_routed_turn` (or :meth:`prepare_turn`) and supply a router.
    The router only prepares quoted model context; the native harness remains
    the owner of the durable turn and all lifecycle checkpoints.
    """

    def __init__(self, backend: Backend, *, router=None):
        self.backend = backend
        self.router = router

    def close(self):
        self.backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def create_task(self, title, *, parents=(), aliases=(), snapshot=None):
        return self.backend.call("create_task", title=title, parents=list(parents),
                                 aliases=list(aliases), snapshot=snapshot or {})

    def task(self, task_id):
        return self.backend.call("task", task_id=task_id)

    def append_batch(self, messages):
        return self.backend.call("append_batch", messages=messages)

    def search(self, query, *, task_ids=None, limit=8):
        return self.backend.call("search", query=query, task_ids=task_ids, limit=limit)

    def resolve(self, query):
        return self.backend.call("resolve", query=query)

    def begin(self, request_id, task_id, query, *, system="", model_key="",
              budget=2000, reserve=0, scope="lineage", retrieval_limit=24,
              recent=4, counting="estimated", consistency="snapshot"):
        """Begin a durable turn with snapshot or scope freshness enforcement.

        Snapshot retains the original request wire format. Scope asks the native
        backend to reject new work when its captured task/source scope changes.
        """
        if consistency not in ("snapshot", "scope"):
            raise ValueError("consistency must be snapshot or scope")
        options = {"consistency": consistency} if consistency != "snapshot" else {}
        turn = self.backend.call("begin", request_id=request_id, task_id=task_id,
                                 query=query, system=system, model_key=model_key,
                                 budget=budget, reserve=reserve, scope=scope,
                                 retrieval_limit=retrieval_limit, recent=recent,
                                 counting=counting, **options)
        return RunSession(self, turn)

    def inspect(self, request_id):
        return self.backend.call("turn", request_id=request_id)

    def model_calls(self, request_id):
        """Read ordered requests and confirmed responses; null means unknown outcome."""
        return self.backend.call("model_calls", request_id=request_id)

    def export_journal(self, destination):
        """Write a coherent JSONL audit export without overwriting an existing file."""
        return self.backend.call("export_file", path=str(Path(destination).resolve()))

    def retention_plan(self, cutoff):
        """Preview terminal journals eligible for retirement before an RFC3339 cutoff."""
        return self.backend.call("retention_plan", cutoff=cutoff)

    def apply_retention(self, cutoff, plan_hash):
        """Retire only the exact previewed journal contents; preserve evidence and ID tombstones."""
        return self.backend.call("retention_apply", cutoff=cutoff, plan_hash=plan_hash)

    def recover(self, request_id, expected_generation):
        turn = self.backend.call("recover", request_id=request_id,
                                 generation=expected_generation)
        return RunSession(self, turn)

    def prepare_routed_turn(self, request_id, task_id, query, *, router=None,
                            requested_route=_ROUTE_MISSING, route=_ROUTE_MISSING,
                            route_kind=_ROUTE_MISSING, router_options=None,
                            **options):
        """Begin a durable turn and prepare one bounded routed context.

        ``router`` is explicit unless one was supplied to the harness
        constructor.  The canonical router receives the hybrid route by
        default; callers may pass a different ``requested_route`` for a
        deliberate route selection.  A committed replay returns without
        invoking the router, while a pending replay raises ``in_progress`` in
        the same way as :meth:`run`; this avoids manufacturing a new context
        for an already-journaled request.

        Routing happens after ``begin`` has durably captured the native turn.
        If routing fails, the pending turn remains inspectable and recoverable;
        this method never retries it or any later model/tool effect.
        """
        selected_router = router if router is not None else self.router
        if selected_router is None:
            raise TypeError("router is required for routed turns")
        if not any(callable(getattr(selected_router, name, None))
                   for name in ("retrieve", "prepare", "query", "context",
                                "context_for_turn", "route_context")):
            raise TypeError("router must expose retrieve(query, ...) or a compatible context method")
        route_values = [value for value in (requested_route, route, route_kind)
                        if value is not _ROUTE_MISSING]
        if route_values and any(value != route_values[0] for value in route_values[1:]):
            raise ValueError("route selectors disagree")
        selected_route = route_values[0] if route_values else _ROUTE_MISSING
        if router_options is None:
            router_options = {}
        if not isinstance(router_options, Mapping):
            raise TypeError("router_options must be a mapping")
        router_options = dict(router_options)
        if "requested_route" in router_options:
            if selected_route is not _ROUTE_MISSING and router_options["requested_route"] != selected_route:
                raise ValueError("route selectors disagree")
            selected_route = router_options.pop("requested_route")
        if selected_route is _ROUTE_MISSING:
            hybrid = _default_hybrid_route()
            # ``None`` means that the installed router has no dedicated
            # hybrid enum.  Leave the keyword omitted so its configured
            # default (or the native ``scope`` below) remains authoritative.
            selected_route = hybrid if hybrid is not None else _ROUTE_MISSING
        # Keep the native allowance and the router's package bound aligned
        # when callers provide those options through ``router_options``.  Only
        # shared begin fields are copied; router-specific knobs stay local to
        # the read-only router call.
        for name in ("system", "budget", "reserve", "scope",
                     "retrieval_limit", "recent"):
            if name not in options and name in router_options:
                value = router_options[name]
                if name == "scope" and value not in ("task", "lineage", "all"):
                    continue
                options[name] = value
        session = self.begin(request_id, task_id, query, **options)
        status = session.turn.get("status")
        if status == "committed":
            return RoutedTurn(session, None)
        if status != "pending" or not session.turn.get("created"):
            raise HarnessError("in_progress", "Inspect and explicitly recover the unfinished request before continuing it.")
        # Passing ``None`` as an explicit route is useful to callers that want
        # the router's own default.  The route value is never interpreted here.
        # Forward only read-only package options that the canonical router
        # understands; ``router_options`` supplies optional plugin-specific
        # knobs such as a caller-owned counter or active-task policy.
        forwarded = {
            name: options[name]
            for name in ("system", "budget", "reserve", "retrieval_limit", "recent")
            if name in options
        }
        # ``scope`` is a route selector in MemoryRouter and a native context
        # scope in Harness.begin; forward it only when no explicit route was
        # selected, avoiding contradictory selectors.
        if selected_route is _ROUTE_MISSING and "scope" in options:
            forwarded["scope"] = options["scope"]
        extra_router_options = dict(router_options)
        if selected_route is not _ROUTE_MISSING:
            # A caller-selected route is authoritative; a second scope
            # selector supplied as an implementation option must not create a
            # contradictory router request.
            extra_router_options.pop("scope", None)
        forwarded.update(extra_router_options)
        context = _router_context(selected_router, query, task_id,
                                  selected_route, **forwarded)
        return RoutedTurn(session, context)

    # ``prepare_turn`` is the short plugin-facing spelling.  Keeping the
    # longer name above makes call sites self-documenting and avoids changing
    # the existing ``begin`` contract.
    def prepare_turn(self, request_id, task_id, query, *, router=None,
                     requested_route=_ROUTE_MISSING, **options):
        return self.prepare_routed_turn(request_id, task_id, query,
                                        router=router,
                                        requested_route=requested_route,
                                        **options)

    def prepare_hybrid_turn(self, request_id, task_id, query, *, router=None,
                            **options):
        """Explicit alias for :meth:`prepare_routed_turn`'s hybrid default."""
        return self.prepare_routed_turn(request_id, task_id, query,
                                        router=router, **options)

    def run(self, request_id, task_id, query, complete: Callable, *,
            router=None, requested_route=_ROUTE_MISSING,
            route=_ROUTE_MISSING, route_kind=_ROUTE_MISSING,
            router_options=None, **options):
        if not callable(complete):
            raise TypeError("complete must be a callable")
        # Supplying a router is an explicit opt-in.  Existing callers retain
        # the historical native context path and callback type.
        if (router is not None or self.router is not None or
                requested_route is not _ROUTE_MISSING or
                route is not _ROUTE_MISSING or route_kind is not _ROUTE_MISSING or
                router_options is not None):
            session = self.prepare_routed_turn(
                request_id, task_id, query, router=router,
                requested_route=requested_route, route=route,
                route_kind=route_kind, router_options=router_options,
                **options)
        else:
            session = self.begin(request_id, task_id, query, **options)
        if session.turn["status"] == "committed":
            return copy.deepcopy(session.turn["reply"])
        if session.turn["status"] != "pending" or not session.turn["created"]:
            raise HarnessError("in_progress", "Inspect and explicitly recover the unfinished request before continuing it.")
        reply = complete(session)
        if isinstance(reply, str):
            reply = {"text": reply}
        return session.commit(reply)["reply"]


class NativeHarness(Harness):
    def __init__(self, binary, database, *, create=False, timeout=15.0,
                 router=None):
        super().__init__(NativeBackend(binary, database, create=create, timeout=timeout),
                         router=router)


class RunSession:
    def __init__(self, harness: Harness, turn: dict):
        self.harness = harness
        self.turn = copy.deepcopy(turn)

    @property
    def messages(self):
        return copy.deepcopy(self.turn["package"]["messages"])

    @property
    def snapshot(self):
        return copy.deepcopy(self.turn["package"]["snapshot"])

    def _call(self, operation, **arguments):
        return self.harness.backend.call(operation, request_id=self.turn["request_id"],
                                         generation=self.turn["generation"], **arguments)

    def tools(self):
        return self.harness.backend.call("tools", request_id=self.turn["request_id"])

    def action_eligible(self, call_id, *, ttl_ms=5000):
        """Revalidate a started tool immediately before its external effect.

        The returned local lease narrows the race window; it cannot make an
        arbitrary remote API transactionally atomic.
        """
        return self._call("action_eligible", call_id=call_id, ttl_ms=ttl_ms)

    def model_calls(self):
        return self.harness.model_calls(self.turn["request_id"])

    def record_model_response(self, call_id, response):
        """Durably store the confirmed provider response before using it to execute tools."""
        if isinstance(response, str):
            value = json.loads(response, parse_constant=_invalid_json_constant)
            raw = response
        else:
            value = response
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if not isinstance(value, dict):
            raise ValueError("provider response must be a JSON object")
        return self._call("model_response", call_id=call_id, response_json=raw)

    def tool(self, call_id, name, arguments, execute: Callable):
        if not callable(execute):
            raise TypeError("execute must be a callable")
        call = self._call("tool_start", call_id=call_id, name=name, arguments=arguments)
        if call["status"] == "completed":
            return call.get("result")
        if not call["created"]:
            raise HarnessError("indeterminate_tool", f"Reconcile the outcome of tool {call_id!r} before retrying it.")
        lease = self.action_eligible(call_id)
        result = execute()
        return self.reconcile_tool(call_id, result, lease_token=lease["token"]).get("result")

    def reconcile_tool(self, call_id, confirmed_result, *, lease_token=None):
        """Record a result verified by the host; this method executes no tool."""
        return self._call("tool_finish", call_id=call_id, result=confirmed_result,
                          lease_token=lease_token or "")

    def checkpoint_model_request(self, call_id, payload, *, counter=None,
                                 counting="estimated-json") -> bytes:
        """Return the exact UTF-8 bytes to dispatch after durable size checking.

        A custom counter accepts the complete serialized request string. Include
        actual chat framing and tool schemas in the chosen accounting scheme.
        The default estimate is not a provider tokenizer.
        """
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if counter is None:
            if counting != "estimated-json":
                raise ValueError("A named custom counting scheme needs a counter")
            units = (len(raw) + 3) // 4 + 8
        else:
            units = counter(raw)
            if isinstance(units, bool) or not isinstance(units, int) or units < 0 or counter(raw) != units:
                raise ValueError("counter must return a deterministic nonnegative integer")
            if counting == "estimated-json":
                counting = "caller"
        self._call("model_request", call_id=call_id, payload_json=raw,
                   units=units, counting=counting)
        return raw.encode("utf-8")

    def commit(self, reply):
        result = self._call("commit", reply=reply)
        self.turn = copy.deepcopy(result)
        return result

    def abort(self):
        return self._call("abort")


class RoutedTurn:
    """A native :class:`RunSession` paired with a router-produced context.

    The object is intentionally a thin adapter.  The router decides which
    pinned and on-demand evidence belongs in the bounded context; this class
    only makes that context convenient to send through the existing native
    lifecycle journal.  It never dispatches a model or executes a tool.
    """

    def __init__(self, session: RunSession, context):
        if not isinstance(session, RunSession):
            raise TypeError("session must be a RunSession")
        self.session = session
        self.context = context
        # These aliases make the object easy to pass through plugin adapters
        # that call their prepared value a ``route`` or ``routed_context``.
        self.routed_context = context
        self.route_context = context
        self._leases: dict[str, str] = {}

    def __getattr__(self, name):
        # Delegate turn, lifecycle, and recovery operations to RunSession.  A
        # delegated method still reaches the exact NativeHarness boundary.
        return getattr(self.session, name)

    @property
    def turn(self):
        return self.session.turn

    @property
    def route(self):
        return _context_value(self.context, "route", None)

    @property
    def task_id(self):
        value = _context_value(self.context, "task_id", _MISSING)
        if value is _MISSING:
            return self.session.turn.get("task_id")
        return value

    @property
    def reason(self):
        return _context_value(self.context, "reason", None)

    @property
    def citations(self):
        value = _context_value(self.context, "citations", ())
        return copy.deepcopy(value)

    @property
    def package(self):
        value = _context_value(self.context, "package", _MISSING)
        if value is _MISSING:
            return None
        return copy.deepcopy(value)

    @property
    def messages(self):
        value = _context_value(self.context, "as_messages", _MISSING)
        if value is _MISSING:
            value = self.session.messages
        return copy.deepcopy(value)

    @property
    def snapshot(self):
        # The durable native session is authoritative for writes.  A routed
        # context may expose a snapshot for display, but commits must use the
        # snapshot captured by NativeHarness so task-revision fencing remains
        # effective.
        return self.session.snapshot

    def as_dict(self):
        """Return the router's source-preserving representation, if present."""
        value = _context_value(self.context, "as_dict", _MISSING)
        if value is _MISSING:
            return {"route": self.route, "task_id": self.task_id,
                    "messages": self.messages, "citations": self.citations,
                    "reason": self.reason}
        return copy.deepcopy(value)

    def model_payload(self, payload=_MISSING):
        """Add routed messages to a caller's provider payload when absent.

        Callers may supply the complete provider payload (including tool
        schemas and provider options).  The adapter only supplies ``messages``
        by default; it does not rewrite provider-specific structure.
        """
        if payload is _MISSING:
            return {"messages": self.messages}
        if isinstance(payload, Mapping):
            value = copy.deepcopy(payload)
            value.setdefault("messages", self.messages)
            return value
        return payload

    def checkpoint_model_request(self, call_id, payload=_MISSING, *,
                                 counter=None, counting="estimated-json") -> bytes:
        """Checkpoint one complete routed provider request before dispatch."""
        return self.session.checkpoint_model_request(
            call_id, self.model_payload(payload), counter=counter,
            counting=counting)

    def before_model(self, call_id, payload=_MISSING, *, counter=None,
                     counting="estimated-json") -> bytes:
        """Explicit before-model boundary; this performs no provider call."""
        return self.checkpoint_model_request(call_id, payload, counter=counter,
                                              counting=counting)

    def record_model_response(self, call_id, response):
        """Checkpoint a confirmed provider response; never retries dispatch."""
        return self.session.record_model_response(call_id, response)

    def after_model(self, call_id, response=_MISSING, *, error=_MISSING):
        """Explicit after-model boundary for a response or known failure."""
        if (response is _MISSING) == (error is _MISSING):
            raise ValueError("after_model requires exactly one of response or error")
        if response is _MISSING:
            response = {"error": copy.deepcopy(error)}
        return self.record_model_response(call_id, response)

    def before_tool(self, call_id, name, arguments, *, ttl_ms=5000):
        """Record tool intent and return its local action-eligibility lease.

        No executable callback is accepted here.  The host invokes the tool
        only after this method succeeds, then calls :meth:`after_tool`.
        """
        started = self.session._call("tool_start", call_id=call_id,
                                     name=name, arguments=arguments)
        if started.get("status") == "completed":
            return {"tool": started, "eligibility": None}
        if not started.get("created"):
            raise HarnessError("indeterminate_tool",
                               f"Reconcile tool {call_id!r} before retrying it.")
        lease = self.session.action_eligible(call_id, ttl_ms=ttl_ms)
        token = lease.get("token") if isinstance(lease, Mapping) else None
        if isinstance(token, str) and token:
            self._leases[call_id] = token
        return {"tool": started, "eligibility": lease}

    def after_tool(self, call_id, result=_MISSING, *, error=_MISSING,
                   lease_token=None):
        """Record a host-confirmed tool result; this method executes no tool."""
        if (result is _MISSING) == (error is _MISSING):
            raise ValueError("after_tool requires exactly one of result or error")
        if result is _MISSING:
            result = {"error": copy.deepcopy(error)}
        if lease_token is None:
            lease_token = self._leases.get(call_id)
        return self.session.reconcile_tool(call_id, result,
                                           lease_token=lease_token)

    def tool(self, call_id, name, arguments, execute: Callable):
        """Use the existing combined safe tool helper without changing it."""
        return self.session.tool(call_id, name, arguments, execute)

    def commit(self, reply):
        """Commit the final answer through NativeHarness."""
        return self.session.commit(reply)

    def abort(self):
        return self.session.abort()


class RoutedHarness:
    """Opt-in facade that binds one router to an existing harness."""

    def __init__(self, harness: Harness, router):
        if harness is None:
            raise TypeError("harness is required")
        if router is None:
            raise TypeError("router is required")
        self.harness = harness
        self.router = router

    @property
    def backend(self):
        return self.harness.backend

    def prepare_turn(self, request_id, task_id, query, *,
                     requested_route=_ROUTE_MISSING, **options):
        return self.harness.prepare_routed_turn(
            request_id, task_id, query, router=self.router,
            requested_route=requested_route, **options)

    prepare_routed_turn = prepare_turn
    prepare_hybrid_turn = prepare_turn

    def close(self):
        return self.harness.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __getattr__(self, name):
        return getattr(self.harness, name)


# A descriptive spelling for plugin integrations that call the route hybrid
# memory explicitly.  Both names refer to the same thin facade.
HybridHarness = RoutedHarness
