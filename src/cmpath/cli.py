"""JSON-first command line interface. No inference service is required."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import inspect
import json
from pathlib import Path
import sqlite3
import sys
from collections.abc import Mapping

from . import __version__
from .doctor import format_report, run_doctor
from .memory import TaskMemory


def _object(text):
    value = json.loads(text)
    if not isinstance(value,dict):
        raise ValueError("Expected a JSON object")
    return value


def _json_input(value, name: str, *, object_only: bool = False):
    """Decode an inline JSON value or a read-only JSON file reference.

    CLI routing accepts small, caller-pinned values without making the router
    responsible for filesystem writes or configuration discovery.  A value is
    first interpreted as JSON; if that fails it is treated as a path and read
    with UTF-8.  Prefixing a path with ``@`` is also supported for values that
    happen to be valid JSON strings.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a JSON value or file path")
    raw = value
    path_value = raw[1:] if raw.startswith("@") else raw
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        path = Path(path_value)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} file is not valid JSON: {path}") from exc
    if object_only and not isinstance(decoded, dict):
        raise ValueError(f"{name} must be a JSON object")
    return decoded


def _call_supported(function, kwargs):
    """Call a shared-router method without guessing unsupported options.

    The router is a separately versioned integration surface.  Filtering
    optional CLI knobs against its signature keeps this CLI compatible with a
    small router implementation while still forwarding all options when the
    implementation opts into ``**kwargs``.
    """
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return function(**kwargs)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD
           for parameter in parameters.values()):
        return function(**kwargs)
    return function(**{key: value for key, value in kwargs.items()
                       if key in parameters})


def _router_task_id(value):
    """Extract a resolved task ID from common Resolution representations."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        status = value.get("status")
        task_id = value.get("task_id")
    else:
        status = getattr(value, "status", None)
        task_id = getattr(value, "task_id", None)
    if status not in (None, "resolved"):
        return None
    return task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else None


def _route_result_record(value):
    """Convert a RoutedContext-like result into the CLI's JSON record."""
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
    elif not isinstance(value, (dict, list, str, int, float, bool, type(None))):
        try:
            value = asdict(value)
        except TypeError:
            value = vars(value)
    return value


def _pinned_specs(value):
    """Return explicit ``(task_id, evidence_ids)`` pin requests.

    ``--pinned-input`` is intentionally permissive for forward compatibility:
    arbitrary JSON remains caller-owned input, while the small pin schema is
    recognized when present.  A pin may be one object (``task_id`` plus an
    optional ``evidence_ids`` array), an object containing a ``pins`` array,
    a list of pin objects, or a bare positive task ID.
    """
    if value is None:
        return ()
    if isinstance(value, int) and not isinstance(value, bool):
        return ((value, ()),)
    if isinstance(value, Mapping):
        if "pins" in value:
            value = value["pins"]
        elif "task_id" in value or (
            isinstance(value.get("id"), int) and not isinstance(value.get("id"), bool)
        ):
            value = [value]
        else:
            return ()
    if not isinstance(value, (list, tuple)):
        return ()
    specs = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            specs.append((item, ()))
            continue
        if not isinstance(item, Mapping):
            # A list that does not describe pins is still valid arbitrary
            # caller-owned input; only an explicit ``pins`` envelope is strict.
            return ()
        if "task_id" not in item and not (
            isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool)
        ):
            return ()
        task_id = item.get("task_id", item.get("id"))
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise ValueError("pinned input pin task_id must be an integer")
        evidence_ids = item.get("evidence_ids", ())
        if isinstance(evidence_ids, (str, bytes)) or not isinstance(
            evidence_ids, (list, tuple)
        ):
            raise ValueError("pinned input evidence_ids must be an array")
        specs.append((task_id, tuple(evidence_ids)))
    return tuple(specs)


def _apply_pinned_input(router, value):
    """Apply recognized pins to one router instance, without persistence."""
    pin = getattr(router, "pin", None)
    specs = _pinned_specs(value)
    if not specs or not callable(pin):
        return
    for task_id, evidence_ids in specs:
        pin(task_id, evidence_ids=evidence_ids)


def _resolve_router_hint(router, memory, hint):
    """Resolve a hint without allowing an active-task fallback to guess."""
    resolver = getattr(router, "resolve_task", None)
    if callable(resolver):
        try:
            parameters = inspect.signature(resolver).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "query" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs = {"query": hint}
            if "use_active" in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                kwargs["use_active"] = False
            return resolver(**kwargs)
        # Minimal compatibility routers often name their one positional
        # argument ``hint`` or ``text``; do not force the canonical name.
        return resolver(hint)
    resolver = getattr(memory, "resolve", None)
    if not callable(resolver):
        raise RuntimeError("router does not expose task resolution")
    return resolver(hint)


def _route_memory(memory, args):
    """Run one read-only router retrieval using the shared router module.

    This function deliberately does not import or implement routing policy.
    The core module owns route selection and context construction; the CLI only
    validates input, resolves an optional task hint, and serializes the result.
    """
    try:
        from . import router as router_module
    except ImportError as exc:
        raise RuntimeError(
            "Hybrid memory router is unavailable; install the router-enabled cmpath build"
        ) from exc

    router_type = getattr(router_module, "MemoryRouter", None)
    if router_type is None:
        router_type = getattr(router_module, "HybridMemoryRouter", None)
    if router_type is None:
        raise RuntimeError("cmpath.router does not expose MemoryRouter")

    pinned_config = _json_input(args.pinned_config, "pinned config", object_only=True)
    pinned_input = _json_input(args.pinned_input, "pinned input")

    # The canonical router accepts a RouterConfig instance rather than a raw
    # mapping.  Keep the mapping for forwarding to compatibility routers and
    # caller diagnostics, but construct the typed config when the module
    # exposes the class.
    router_config = pinned_config
    config_type = getattr(router_module, "RouterConfig", None)
    if pinned_config is not None and config_type is not None:
        try:
            router_config = config_type(**pinned_config)
        except TypeError as exc:
            raise ValueError(f"invalid pinned config: {exc}") from exc
    constructor_kwargs = {}
    constructor_parameters = {}
    constructor_options = {
        "config": pinned_config,
        "router_config": pinned_config,
        "pinned_config": pinned_config,
    }
    if pinned_config is not None:
        try:
            constructor_parameters = inspect.signature(router_type).parameters
        except (TypeError, ValueError):
            constructor_parameters = {}
        for key, value in constructor_options.items():
            if key in constructor_parameters:
                constructor_kwargs[key] = (
                    router_config if key in ("config", "router_config") else value
                )
                break
    try:
        router = router_type(memory, **constructor_kwargs)
    except TypeError:
        # A tiny compatibility fallback for implementations whose constructor
        # only accepts the memory object and receives config at retrieval time.
        if constructor_kwargs and not constructor_parameters:
            router = router_type(memory)
        else:
            raise

    _apply_pinned_input(router, pinned_input)

    task_id = args.task_id
    if task_id is None and args.task_hint:
        resolution = _resolve_router_hint(router, memory, args.task_hint)
        task_id = _router_task_id(resolution)

    retrieve = getattr(router, "retrieve", None)
    if not callable(retrieve):
        raise RuntimeError("cmpath.router does not expose retrieve")
    if args.query is not None and args.query_option is not None:
        raise ValueError("route query must be supplied positionally or with --query, not both")
    query = args.query_option if args.query_option is not None else args.query
    if query is None:
        raise ValueError("route requires a query (positional or --query)")

    options = {
        "query": query,
        "task_id": task_id,
        "requested_route": args.requested_route,
        "route_kind": args.requested_route,
        "budget": args.budget,
        "reserve": args.reserve,
        "scope": args.scope,
        "retrieval_limit": args.retrieval_limit,
        "recent": args.recent,
        "system": args.system,
        "use_active": args.use_active,
        "task_hint": args.task_hint,
        "pinned_config": pinned_config,
        "pinned_input": pinned_input,
        "config": pinned_config,
        "input": pinned_input,
    }
    result = _call_supported(
        retrieve,
        {key: value for key, value in options.items() if value is not None},
    )
    return _route_result_record(result)


def parser():
    p = argparse.ArgumentParser(description="Durable task memory with citable evidence")
    p.add_argument("--version",action="version",version=__version__)
    p.add_argument("--db",default="cmpath.db",help="SQLite database path")
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("init",help="Create a database, or check an existing CMP database")
    sub.add_parser("tasks",help="List all tasks")
    sub.add_parser("stats",help="Show database counts and active task")
    sub.add_parser("check",help="Check SQLite, foreign keys and the search index")
    doctor = sub.add_parser("doctor",help="Diagnose the local Python, SQLite, native, MCP and export environment")
    doctor.add_argument("--json",action="store_true",help="Emit the diagnostic report as JSON")
    doctor.add_argument("--format",choices=("text","json"),default="text",
                        help="Diagnostic output format (default: text)")
    doctor.add_argument("--db",dest="doctor_db",type=Path,
                        help="Database path to inspect (also accepted before the doctor command)")
    doctor.add_argument("--native","--binary","--native-binary",dest="native_binary",type=Path,
                        help="Path to cmpath-native (defaults to CMP_NATIVE_BINARY or a checkout binary)")
    doctor.add_argument("--mcp-config",dest="mcp_configs",action="append",type=Path,
                        help="MCP client JSON config to validate; may be repeated")
    doctor.add_argument("--export-dir",type=Path,
                        help="Directory in which to exercise atomic export (defaults to the database directory)")
    doctor.add_argument("--skip-native",action="store_true",help="Skip the optional native executable probe")
    doctor.add_argument("--skip-mcp",action="store_true",help="Skip the optional MCP dependency/config probe")
    doctor.add_argument("--strict",action="store_true",help="Treat warnings (including optional integration warnings) as failures")
    create = sub.add_parser("create",help="Create an independent or dependent task")
    create.add_argument("title")
    create.add_argument("--parent",type=int,action="append",default=[])
    create.add_argument("--alias",action="append",default=[])
    create.add_argument("--snapshot",default="{}",help="JSON planner state")
    for name in ("show","resume","archive","transcript"):
        q = sub.add_parser(name)
        q.add_argument("task",type=int)
        if name == "resume":
            q.add_argument("--expected-version",type=int)
    append = sub.add_parser("append",help="Store source evidence without extracting facts")
    append.add_argument("task",type=int)
    append.add_argument("role",choices=["user","assistant","tool","document"])
    text = append.add_mutually_exclusive_group(required=True)
    text.add_argument("--text")
    text.add_argument("--file",type=Path)
    append.add_argument("--source",default="{}",help="JSON source metadata")
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("task",type=int)
    snapshot.add_argument("value",help="JSON object")
    snapshot.add_argument("--expected-revision",type=int,required=True)
    fact = sub.add_parser("set-fact",help="Record a sourced fact revision")
    fact.add_argument("task",type=int)
    fact.add_argument("key")
    fact.add_argument("value",help="JSON value, e.g. 3400 or '\"approved\"'")
    fact.add_argument("--evidence",type=int,required=True)
    fact.add_argument("--retracted",action="store_true")
    fact = sub.add_parser("fact")
    fact.add_argument("task",type=int)
    fact.add_argument("key")
    fact.add_argument("--revision",type=int)
    for name in ("search","resolve"):
        query = sub.add_parser(name)
        query.add_argument("query")
        if name == "search":
            query.add_argument("--task",type=int,action="append")
            query.add_argument("--limit",type=int,default=8)
    route = sub.add_parser("route", help="Retrieve memory through the hybrid router (read-only)")
    route.add_argument("query", nargs="?", help="Current query; may be supplied with --query")
    route.add_argument("--query", dest="query_option",
                       help="Current query (alternative to the positional query)")
    route.add_argument("--task", "--task-id", dest="task_id", type=int,
                       help="Explicit task ID; takes precedence over task hints")
    route.add_argument("--task-hint", help="Task title or alias hint to resolve without guessing")
    route.add_argument("--route", "--route-kind", dest="requested_route",
                       choices=("none", "pinned", "task", "lineage", "deep"),
                       help="Request a specific route kind (default: router policy)")
    route.add_argument("--budget", type=int, default=None,
                       help="Input budget passed to the router (default: router config)")
    route.add_argument("--reserve", type=int, default=None,
                       help="Units reserved for caller-owned output/protocol overhead")
    route.add_argument("--scope", choices=("task", "lineage", "all"), default=None,
                       help="Context scope/route selector (default: router policy)")
    route.add_argument("--retrieval-limit", type=int, default=None,
                       help="Maximum lexical candidates for task-aware routes")
    route.add_argument("--recent", type=int, default=None,
                       help="Recent task messages to consider (default: router config)")
    route.add_argument("--system", default="",
                       help="Optional caller-owned system text for the context envelope")
    route.add_argument("--pinned-config", "--config", dest="pinned_config",
                       help="Pinned router config as a JSON object or UTF-8 JSON file path")
    route.add_argument("--pinned-input", "--input", dest="pinned_input",
                       help="Pinned router input as JSON or a UTF-8 JSON file path")
    route.add_argument("--no-active", dest="use_active", action="store_false", default=None,
                       help="Do not let the router use the active task as a fallback")
    route.add_argument("--json", action="store_true",
                       help="Emit JSON (the CLI is JSON-first and does so by default)")
    context = sub.add_parser("context",help="Build a counted payload; does not change task state")
    context.add_argument("task",type=int)
    context.add_argument("query")
    context.add_argument("--budget",type=int,default=2000)
    context.add_argument("--reserve",type=int,default=0)
    context.add_argument("--scope",choices=["task","lineage","all"],default="lineage")
    context.add_argument("--system",default="")
    for name in ("backup","export"):
        output = sub.add_parser(name)
        output.add_argument("destination",type=Path)
    return p


def main(argv=None) -> int:
    p = parser()
    a = p.parse_args(argv)
    try:
        if a.command == "doctor":
            report = run_doctor(
                a.doctor_db if a.doctor_db is not None else a.db,
                native_binary=a.native_binary,
                mcp_configs=a.mcp_configs,
                export_dir=a.export_dir,
                skip_native=a.skip_native,
                skip_mcp=a.skip_mcp,
                strict=a.strict,
            )
            print(format_report(report, as_json=a.json or a.format == "json"))
            return 0 if report["ok"] else 2
        if a.command != "init" and a.db != ":memory:" and not Path(a.db).is_file():
            raise FileNotFoundError("Database does not exist. Run cmpath --db PATH init first.")
        if a.command == "init" and a.db != ":memory:":
            Path(a.db).parent.mkdir(parents=True,exist_ok=True)
        with TaskMemory(a.db) as m:
            if a.command in ("init","stats"):
                result = m.stats()
            elif a.command == "check":
                result = m.check()
                if not result["ok"]:
                    print(json.dumps(result),file=sys.stderr)
                    return 2
            elif a.command == "tasks":
                result = [asdict(t) for t in m.tasks()]
            elif a.command == "create":
                result = asdict(m.create_task(a.title,parents=a.parent,aliases=a.alias,snapshot=_object(a.snapshot)))
            elif a.command == "show":
                result = asdict(m.task(a.task))
            elif a.command == "resume":
                result = m.resume(a.task,expected_version=a.expected_version)
            elif a.command == "archive":
                m.archive(a.task)
                result = asdict(m.task(a.task))
            elif a.command == "transcript":
                result = [e.as_record() for e in m.transcript(a.task)]
            elif a.command == "append":
                content = a.text if a.text is not None else a.file.read_text(encoding="utf-8")
                result = m.append(a.task,a.role,content,source=_object(a.source)).as_record()
            elif a.command == "snapshot":
                result = asdict(m.set_snapshot(a.task,_object(a.value),expected_revision=a.expected_revision))
            elif a.command == "set-fact":
                result = m.set_fact(a.task,a.key,json.loads(a.value),evidence_id=a.evidence,retracted=a.retracted)
            elif a.command == "fact":
                result = m.fact(a.task,a.key,revision=a.revision)
            elif a.command == "search":
                result = [{**e.as_record(),"score":e.score} for e in m.search(a.query,task_ids=a.task,limit=a.limit)]
            elif a.command == "resolve":
                result = asdict(m.resolve(a.query))
            elif a.command == "route":
                result = _route_memory(m, a)
            elif a.command == "context":
                package = m.context(a.task,a.query,budget=a.budget,reserve=a.reserve,scope=a.scope,system=a.system)
                result = {"messages":package.as_messages(),"used_units":package.used_units,
                          "input_allowance":package.input_allowance,"counting":package.counting,
                          "citations":package.citations,"omitted_candidates":package.omitted_candidates}
            elif a.command == "backup":
                m.backup(a.destination)
                result = {"saved":str(a.destination)}
            elif a.command == "export":
                # Exclusive creation avoids silently replacing an unrelated file.
                with a.destination.open("x",encoding="utf-8") as stream:
                    json.dump(m.export(),stream,ensure_ascii=False,indent=2)
                result = {"saved":str(a.destination)}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (TypeError,ValueError,KeyError,RuntimeError,OSError,sqlite3.Error) as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
