"""A small, authenticated HTTP boundary for the native harness lifecycle.

The control plane deliberately owns no planner, model client, tool registry, or
SQL connection.  It is only an allow-listed adapter around :class:`Harness`
and :class:`NativeHarness`: a caller records model/tool boundaries here, while
the caller remains responsible for actually dispatching the model and running
an approved external tool.

The HTTP server is intended for a same-host integration boundary.  It binds to
127.0.0.1 by default, accepts only bounded JSON request bodies, and can require
``Authorization: Bearer ...`` (or ``X-CMP-Token``) using the
``CMPATH_CONTROL_PLANE_TOKEN`` environment variable.  There is also a direct
``ControlPlane`` service class for hosts that do not need HTTP.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hmac
import http.server
import ipaddress
import json
import os
from pathlib import Path
import socket
import threading
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .harness import Harness, HarnessError, NativeHarness


DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024
TOKEN_ENVIRONMENT_VARIABLE = "CMPATH_CONTROL_PLANE_TOKEN"
_TOKEN_ENVIRONMENT_ALIASES = (TOKEN_ENVIRONMENT_VARIABLE, "CMP_CONTROL_PLANE_TOKEN")
_MISSING = object()


class ControlPlaneError(RuntimeError):
    """An expected control-plane error with a safe HTTP status mapping."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code)
        self.status = int(status)
        super().__init__(str(message))


def _status_for_code(code: str) -> int:
    if code in {"not_found", "retired"}:
        return 404
    if code in {"busy", "conflict", "fenced", "state", "in_progress",
                "indeterminate_tool", "stale_context", "provenance_required"}:
        return 409
    if code in {"budget", "payload_too_large"}:
        return 413
    if code in {"transport", "closed", "storage_error", "backend"}:
        return 503
    return 400


def _reject_json_constant(value: str):
    raise ValueError("JSON must be finite: " + value)


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON object key: " + str(key))
        out[key] = value
    return out


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant,
                          object_pairs_hook=_no_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ControlPlaneError("invalid", "request body must be UTF-8 JSON") from exc
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ControlPlaneError("invalid", "request body must contain one valid JSON value") from exc


def _payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlPlaneError("invalid", "request body must be a JSON object")
    return copy.deepcopy(value)


def _required_string(value: Any, name: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlPlaneError("invalid", f"{name} must be a nonempty string")
    if len(value.encode("utf-8")) > limit:
        raise ControlPlaneError("invalid", f"{name} is too long")
    return value


def _optional_string(value: Any, name: str, *, limit: int = 1 * 1024 * 1024) -> str:
    if not isinstance(value, str):
        raise ControlPlaneError("invalid", f"{name} must be a string")
    if len(value.encode("utf-8")) > limit:
        raise ControlPlaneError("invalid", f"{name} is too long")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None,
             maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlPlaneError("invalid", f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ControlPlaneError("invalid", f"{name} is below its minimum")
    if maximum is not None and value > maximum:
        raise ControlPlaneError("invalid", f"{name} is above its maximum")
    return value


def _known(data: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(data) - allowed
    if extra:
        raise ControlPlaneError("invalid", "unknown field: " + sorted(extra)[0])


def _resolve_token(token: str | None) -> str | None:
    if token is not None:
        if not isinstance(token, str):
            raise ValueError("auth_token must be a string")
        if not token:
            raise ValueError("auth_token must not be empty")
        return token
    for name in _TOKEN_ENVIRONMENT_ALIASES:
        value = os.environ.get(name)
        if value is not None:
            if not value:
                raise ValueError(f"{name} must not be empty")
            return value
    return None


def _turn_value(value: Any) -> Any:
    """Unwrap the Python ``RunSession`` returned by Harness.begin/recover."""
    turn = getattr(value, "turn", _MISSING)
    return copy.deepcopy(turn if turn is not _MISSING else value)


class ControlPlane:
    """Allow-listed managed-turn operations over a caller-owned harness.

    ``harness`` is normally a :class:`NativeHarness`, although a regular
    :class:`Harness` or a test double implementing the same backend boundary
    is useful for embedding and tests.  No method accepts executable code or
    SQL; tool operations only persist intent, eligibility, and a host-confirmed
    result.
    """

    def __init__(self, harness: Harness, *, auth_token: str | None = None):
        if harness is None:
            raise TypeError("harness is required")
        self.harness = harness
        self.auth_token = _resolve_token(auth_token)
        self._closed = False

    @classmethod
    def from_native(cls, binary: str | Path, database: str | Path, *,
                    create: bool = False, timeout: float = 15.0,
                    auth_token: str | None = None) -> "ControlPlane":
        return cls(NativeHarness(binary, database, create=create, timeout=timeout),
                   auth_token=auth_token)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            close = getattr(self.harness, "close", None)
            if callable(close):
                close()

    def _call(self, operation: str, **arguments):
        backend = getattr(self.harness, "backend", None)
        call = getattr(backend, "call", None)
        if not callable(call):
            call = getattr(self.harness, "call", None)
        if not callable(call):
            raise ControlPlaneError("backend", "harness does not expose a backend call boundary", 503)
        return call(operation, **arguments)

    def health(self) -> dict[str, Any]:
        """Return liveness and native backend metadata without executing work."""
        info = self._call("info")
        if not isinstance(info, Mapping):
            info = {"backend": info}
        return {"status": "ok", "service": "cmpath-control-plane",
                "backend": copy.deepcopy(dict(info))}

    def create_task(self, value: Mapping[str, Any] | str, *, parents=(),
                    aliases=(), snapshot=None) -> Any:
        if isinstance(value, Mapping):
            data = _payload(value)
            _known(data, {"title", "parents", "aliases", "snapshot"})
            title = _required_string(data.get("title"), "title", limit=4096)
            parents = data.get("parents", ())
            aliases = data.get("aliases", ())
            snapshot = data.get("snapshot", {})
        else:
            title = _required_string(value, "title", limit=4096)
            snapshot = {} if snapshot is None else snapshot
        if not isinstance(parents, (list, tuple)):
            raise ControlPlaneError("invalid", "parents must be an array")
        normalized_parents = [_integer(item, "parent task ID", minimum=1) for item in parents]
        if not isinstance(aliases, (list, tuple)):
            raise ControlPlaneError("invalid", "aliases must be an array")
        normalized_aliases = [_required_string(item, "alias", limit=4096) for item in aliases]
        if not isinstance(snapshot, dict):
            raise ControlPlaneError("invalid", "snapshot must be a JSON object")
        return self._call("create_task", title=title, parents=normalized_parents,
                          aliases=normalized_aliases, snapshot=copy.deepcopy(snapshot))

    def begin(self, value: Mapping[str, Any] | str, task_id: int | None = None,
              query: str | None = None, **options) -> Any:
        if isinstance(value, Mapping):
            data = _payload(value)
            allowed = {"request_id", "task_id", "query", "system", "model_key",
                       "budget", "reserve", "scope", "retrieval_limit", "recent",
                       "counting", "consistency"}
            _known(data, allowed)
            request_id = _required_string(data.get("request_id"), "request_id", limit=128)
            task_id = _integer(data.get("task_id"), "task_id", minimum=1)
            query = _required_string(data.get("query"), "query", limit=1 * 1024 * 1024)
            options = {key: data[key] for key in allowed if key not in {"request_id", "task_id", "query"} and key in data}
        else:
            request_id = _required_string(value, "request_id", limit=128)
            task_id = _integer(task_id, "task_id", minimum=1)
            query = _required_string(query, "query", limit=1 * 1024 * 1024)
            _known(options, {"system", "model_key", "budget", "reserve", "scope",
                             "retrieval_limit", "recent", "counting", "consistency"})
        for key in ("system", "model_key", "scope", "counting", "consistency"):
            if key in options:
                options[key] = _optional_string(options[key], key)
        for key in ("budget", "reserve", "retrieval_limit", "recent"):
            if key in options:
                options[key] = _integer(options[key], key, minimum=0)
        return _turn_value(self.harness.begin(request_id, task_id, query, **options))

    def inspect(self, request_id: str) -> Any:
        return self.harness.inspect(_required_string(request_id, "request_id", limit=128))

    def recover(self, request_id: str, expected_generation: int) -> Any:
        request_id = _required_string(request_id, "request_id", limit=128)
        expected_generation = _integer(expected_generation, "expected_generation", minimum=1)
        return _turn_value(self.harness.recover(request_id, expected_generation))

    def model_calls(self, request_id: str) -> Any:
        return self.harness.model_calls(_required_string(request_id, "request_id", limit=128))

    def tools(self, request_id: str) -> Any:
        return self._call("tools", request_id=_required_string(request_id, "request_id", limit=128))

    def _generation(self, value: Any) -> tuple[str, int]:
        data = _payload(value)
        request_id = _required_string(data.get("request_id"), "request_id", limit=128)
        generation = _integer(data.get("generation"), "generation", minimum=1)
        return request_id, generation

    def checkpoint_model_request(self, value: Mapping[str, Any], *,
                                 request_id: str | None = None,
                                 generation: int | None = None,
                                 call_id: str | None = None,
                                 payload: Any = _MISSING,
                                 units: int | None = None,
                                 counting: str = "estimated-json") -> Any:
        data = _payload(value)
        if request_id is not None:
            data["request_id"] = request_id
        if generation is not None:
            data["generation"] = generation
        if call_id is not None:
            data["call_id"] = call_id
        if payload is not _MISSING:
            data["payload"] = payload
        if units is not None:
            data["units"] = units
        if counting != "estimated-json":
            data["counting"] = counting
        _known(data, {"request_id", "generation", "call_id", "payload", "payload_json",
                      "units", "counting"})
        rid, gen = self._generation(data)
        cid = _required_string(data.get("call_id"), "call_id", limit=128)
        has_value = "payload" in data
        has_raw = "payload_json" in data
        if has_value == has_raw:
            raise ControlPlaneError("invalid", "exactly one of payload or payload_json is required")
        if has_raw:
            raw = data["payload_json"]
            if not isinstance(raw, str):
                raise ControlPlaneError("invalid", "payload_json must be a string")
            try:
                json.loads(raw, parse_constant=_reject_json_constant,
                           object_pairs_hook=_no_duplicate_keys)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ControlPlaneError("invalid", "payload_json must be valid JSON") from exc
        else:
            try:
                raw = json.dumps(data["payload"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ControlPlaneError("invalid", "payload cannot be serialized as finite JSON") from exc
        count_name = data.get("counting", "estimated-json")
        count_name = _required_string(count_name, "counting", limit=128)
        declared = data.get("units", (len(raw) + 3) // 4 + 8)
        declared = _integer(declared, "units", minimum=0)
        return self._call("model_request", request_id=rid, generation=gen,
                          call_id=cid, payload_json=raw, units=declared,
                          counting=count_name)

    def record_model_response(self, value: Mapping[str, Any], *,
                              request_id: str | None = None,
                              generation: int | None = None,
                              call_id: str | None = None,
                              response: Any = _MISSING) -> Any:
        data = _payload(value)
        if request_id is not None:
            data["request_id"] = request_id
        if generation is not None:
            data["generation"] = generation
        if call_id is not None:
            data["call_id"] = call_id
        if response is not _MISSING:
            data["response"] = response
        _known(data, {"request_id", "generation", "call_id", "response", "response_json"})
        rid, gen = self._generation(data)
        cid = _required_string(data.get("call_id"), "call_id", limit=128)
        has_value, has_raw = "response" in data, "response_json" in data
        if has_value == has_raw:
            raise ControlPlaneError("invalid", "exactly one of response or response_json is required")
        if has_raw:
            raw = data["response_json"]
            if not isinstance(raw, str):
                raise ControlPlaneError("invalid", "response_json must be a string")
            try:
                parsed = json.loads(raw, parse_constant=_reject_json_constant,
                                    object_pairs_hook=_no_duplicate_keys)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ControlPlaneError("invalid", "response_json must be valid JSON") from exc
        else:
            if isinstance(data["response"], str):
                raw = data["response"]
                try:
                    parsed = json.loads(raw, parse_constant=_reject_json_constant,
                                        object_pairs_hook=_no_duplicate_keys)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ControlPlaneError("invalid", "response string must be valid JSON") from exc
            else:
                try:
                    raw = json.dumps(data["response"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    parsed = data["response"]
                except (TypeError, ValueError) as exc:
                    raise ControlPlaneError("invalid", "response cannot be serialized as finite JSON") from exc
        if not isinstance(parsed, dict):
            raise ControlPlaneError("invalid", "provider response must be a JSON object")
        return self._call("model_response", request_id=rid, generation=gen,
                          call_id=cid, response_json=raw)

    def start_tool(self, value: Mapping[str, Any], *, request_id: str | None = None,
                   generation: int | None = None, call_id: str | None = None,
                   name: str | None = None, arguments: Any = _MISSING) -> Any:
        data = _payload(value)
        for key, supplied in (("request_id", request_id), ("generation", generation),
                              ("call_id", call_id), ("name", name), ("arguments", arguments)):
            if supplied is not None and supplied is not _MISSING:
                data[key] = supplied
        _known(data, {"request_id", "generation", "call_id", "name", "arguments"})
        rid, gen = self._generation(data)
        cid = _required_string(data.get("call_id"), "call_id", limit=128)
        tool_name = _required_string(data.get("name"), "name", limit=4096)
        if "arguments" not in data:
            raise ControlPlaneError("invalid", "arguments is required")
        return self._call("tool_start", request_id=rid, generation=gen,
                          call_id=cid, name=tool_name,
                          arguments=copy.deepcopy(data["arguments"]))

    def action_eligible(self, value: Mapping[str, Any], *, request_id: str | None = None,
                        generation: int | None = None, call_id: str | None = None,
                        ttl_ms: int | None = None) -> Any:
        data = _payload(value)
        for key, supplied in (("request_id", request_id), ("generation", generation),
                              ("call_id", call_id), ("ttl_ms", ttl_ms)):
            if supplied is not None:
                data[key] = supplied
        _known(data, {"request_id", "generation", "call_id", "ttl_ms"})
        rid, gen = self._generation(data)
        cid = _required_string(data.get("call_id"), "call_id", limit=128)
        ttl = _integer(data.get("ttl_ms", 0), "ttl_ms", minimum=0, maximum=600000)
        return self._call("action_eligible", request_id=rid, generation=gen,
                          call_id=cid, ttl_ms=ttl)

    def finish_tool(self, value: Mapping[str, Any], *, request_id: str | None = None,
                    generation: int | None = None, call_id: str | None = None,
                    result: Any = _MISSING, lease_token: str | None = None) -> Any:
        data = _payload(value)
        for key, supplied in (("request_id", request_id), ("generation", generation),
                              ("call_id", call_id), ("result", result),
                              ("lease_token", lease_token)):
            if supplied is not None and supplied is not _MISSING:
                data[key] = supplied
        _known(data, {"request_id", "generation", "call_id", "result", "lease_token"})
        rid, gen = self._generation(data)
        cid = _required_string(data.get("call_id"), "call_id", limit=128)
        if "result" not in data:
            raise ControlPlaneError("invalid", "result is required")
        lease = data.get("lease_token", "")
        lease = _optional_string(lease, "lease_token", limit=1024)
        return self._call("tool_finish", request_id=rid, generation=gen,
                          call_id=cid, result=copy.deepcopy(data["result"]),
                          lease_token=lease)

    def commit(self, value: Mapping[str, Any], *, request_id: str | None = None,
               generation: int | None = None, reply: Any = _MISSING) -> Any:
        data = _payload(value)
        for key, supplied in (("request_id", request_id), ("generation", generation),
                              ("reply", reply)):
            if supplied is not None and supplied is not _MISSING:
                data[key] = supplied
        _known(data, {"request_id", "generation", "reply"})
        rid, gen = self._generation(data)
        reply = data.get("reply")
        if not isinstance(reply, dict):
            raise ControlPlaneError("invalid", "reply must be a JSON object")
        _known(reply, {"text", "snapshot", "facts", "provenance"})
        return self._call("commit", request_id=rid, generation=gen,
                          reply=copy.deepcopy(reply))

    def abort(self, request_id: str, generation: int) -> Any:
        request_id = _required_string(request_id, "request_id", limit=128)
        generation = _integer(generation, "generation", minimum=1)
        return self._call("abort", request_id=request_id, generation=generation)

    def dispatch(self, operation: str, value: Mapping[str, Any] | None = None) -> Any:
        """Dispatch one named, non-executable operation.

        This is the single operation table used by HTTP and is also convenient
        for an in-process host.  Unknown operations fail closed.
        """
        operation = _required_string(operation, "operation", limit=64).lower().replace("-", "_")
        data = _payload({} if value is None else value)
        aliases = {
            "healthz": "health", "task_create": "create_task", "begin_turn": "begin",
            "inspect": "turn", "checkpoint_model_request": "model_request",
            "record_model_response": "model_response", "start_tool": "tool_start",
            "finish_tool": "tool_finish", "action_eligibility": "action_eligible",
        }
        operation = aliases.get(operation, operation)
        if operation == "health":
            _known(data, set())
            return self.health()
        if operation == "create_task":
            return self.create_task(data)
        if operation == "begin":
            return self.begin(data)
        if operation == "turn":
            _known(data, {"request_id"})
            return self.inspect(_required_string(data.get("request_id"), "request_id", limit=128))
        if operation == "recover":
            _known(data, {"request_id", "generation", "expected_generation"})
            generation = data.get("expected_generation", data.get("generation", _MISSING))
            return self.recover(data.get("request_id"), generation)
        if operation == "model_calls":
            _known(data, {"request_id"})
            return self.model_calls(data.get("request_id"))
        if operation == "tools":
            _known(data, {"request_id"})
            return self.tools(data.get("request_id"))
        if operation == "model_request":
            return self.checkpoint_model_request(data)
        if operation == "model_response":
            return self.record_model_response(data)
        if operation == "tool_start":
            return self.start_tool(data)
        if operation == "action_eligible":
            return self.action_eligible(data)
        if operation == "tool_finish":
            return self.finish_tool(data)
        if operation == "commit":
            return self.commit(data)
        if operation == "abort":
            _known(data, {"request_id", "generation"})
            return self.abort(data.get("request_id"), data.get("generation"))
        raise ControlPlaneError("operation", "unknown control-plane operation", 404)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        try:
            return any(ipaddress.ip_address(item[4][0]).is_loopback
                       for item in socket.getaddrinfo(host, None))
        except OSError:
            return False


class _ControlPlaneHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CMPControlPlane/1"

    def log_message(self, *_args):  # pragma: no cover - avoid leaking request data
        return

    @property
    def _plane_server(self) -> "ControlPlaneServer":
        return self.server  # type: ignore[return-value]

    def _write(self, status: int, body: Any, *, headers: Mapping[str, str] | None = None) -> None:
        try:
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            status, raw = 500, b'{"error":{"code":"internal","message":"response is not JSON serializable"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _fail(self, exc: BaseException) -> None:
        if isinstance(exc, ControlPlaneError):
            code, message, status = exc.code, str(exc), exc.status
        elif isinstance(exc, HarnessError):
            code, message, status = exc.code, str(exc), _status_for_code(exc.code)
        elif isinstance(exc, (ValueError, TypeError, KeyError, OverflowError)):
            code, message, status = "invalid", str(exc), 400
        else:
            code, message, status = "internal", "control-plane operation failed", 500
        error = {"code": code, "message": message}
        if urlsplit(self.path).path.rstrip("/") == "/v1/managed-turn":
            self._write(status, {"v": 1, "ok": False, "error": error})
        else:
            self._write(status, {"error": error})

    def _authorized(self, *, public: bool = False) -> bool:
        expected = self._plane_server.auth_token
        if expected is None or public:
            return True
        supplied = []
        bearer = self.headers.get("Authorization")
        if bearer:
            scheme, separator, token = bearer.partition(" ")
            if separator and scheme.lower() == "bearer" and token:
                supplied.append(token)
        header_token = self.headers.get("X-CMP-Token")
        if header_token:
            supplied.append(header_token)
        if not supplied or any(not hmac.compare_digest(item, expected) for item in supplied):
            error = {"code": "unauthorized", "message": "authentication required"}
            body = ({"v": 1, "ok": False, "error": error}
                    if urlsplit(self.path).path.rstrip("/") == "/v1/managed-turn"
                    else {"error": error})
            self._write(401, body,
                        headers={"WWW-Authenticate": "Bearer"})
            self.close_connection = True
            return False
        return True

    def _read_body(self) -> dict[str, Any]:
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1:
            raise ControlPlaneError("invalid", "exactly one Content-Length header is required")
        try:
            length = int(values[0])
        except (TypeError, ValueError) as exc:
            raise ControlPlaneError("invalid", "Content-Length must be an integer") from exc
        if length < 0:
            raise ControlPlaneError("invalid", "Content-Length must not be negative")
        if length > self._plane_server.max_body_bytes:
            self.close_connection = True
            raise ControlPlaneError("payload_too_large", "request body exceeds the configured limit", 413)
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ControlPlaneError("invalid", "Transfer-Encoding is not supported")
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            raise ControlPlaneError("invalid", "request body ended before Content-Length")
        return _payload(_decode_json(raw))

    def _request(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                raise ControlPlaneError("invalid", "query strings and fragments are not supported")
            public = method == "GET" and parsed.path.rstrip("/") in {"/health", "/healthz", "/v1/health"}
            if not self._authorized(public=public):
                return
            data = {} if method == "GET" else self._read_body()
            result, status = self._plane_server.route(method, parsed.path, data)
            self._write(status, result)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._fail(exc)

    def do_GET(self) -> None:
        self._request("GET")

    def do_POST(self) -> None:
        self._request("POST")

    def do_PUT(self) -> None:
        self._write(405, {"error": {"code": "method", "message": "method not allowed"}}, headers={"Allow": "GET, POST"})

    do_PATCH = do_PUT
    do_DELETE = do_PUT


class ControlPlaneServer(http.server.ThreadingHTTPServer):
    """Threaded loopback HTTP server for :class:`ControlPlane`.

    The first form is convenient for callers::

        server = ControlPlaneServer(control_plane, port=0).start()

    For code following ``HTTPServer`` conventions, ``(host, port), service``
    is also accepted.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service_or_address, service=None, *, host: str = "127.0.0.1",
                 port: int = 0, auth_token: str | None = None,
                 max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
                 close_service: bool = False):
        if isinstance(service_or_address, tuple):
            address = service_or_address
            source = service
            if source is None:
                raise TypeError("service is required")
            bind_host = str(address[0])
        else:
            source = service_or_address
            address = (host, port)
            bind_host = host
        if isinstance(source, ControlPlane):
            plane = source
        else:
            plane = ControlPlane(source, auth_token=auth_token)
        if not isinstance(max_body_bytes, int) or isinstance(max_body_bytes, bool) or max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")
        token = auth_token if auth_token is not None else plane.auth_token
        if not _is_loopback(bind_host) and token is None:
            raise ValueError("non-loopback control-plane binds require auth_token")
        if ":" in bind_host:
            self.address_family = socket.AF_INET6
        self.control_plane = self.service = plane
        self.auth_token = token
        self.max_body_bytes = max_body_bytes
        self.close_service = close_service
        self._thread: threading.Thread | None = None
        super().__init__(address, _ControlPlaneHandler)

    def route(self, method: str, path: str, data: Mapping[str, Any]):
        clean = path.rstrip("/") or "/"
        segments = []
        for raw in clean.split("/"):
            if not raw:
                continue
            try:
                segment = unquote(raw)
            except Exception as exc:
                raise ControlPlaneError("invalid", "invalid URL path") from exc
            if not segment or segment in {".", ".."} or "/" in segment or "\x00" in segment:
                raise ControlPlaneError("invalid", "invalid URL path")
            segments.append(segment)
        payload = _payload(data)
        if method == "GET":
            if clean in {"/health", "/healthz", "/v1/health"}:
                return self.service.dispatch("health", {}), 200
            if len(segments) == 3 and segments[:2] == ["v1", "turns"]:
                return self.service.dispatch("turn", {"request_id": segments[2]}), 200
            if len(segments) == 4 and segments[:2] == ["v1", "turns"] and segments[3] in {"model-calls", "model_calls"}:
                return self.service.dispatch("model_calls", {"request_id": segments[2]}), 200
            if len(segments) == 4 and segments[:2] == ["v1", "turns"] and segments[3] == "tools":
                return self.service.dispatch("tools", {"request_id": segments[2]}), 200
            raise ControlPlaneError("not_found", "control-plane route does not exist", 404)
        if method != "POST":
            raise ControlPlaneError("method", "method not allowed", 405)
        if segments == ["v1", "control"]:
            op = payload.pop("operation", None)
            return self.service.dispatch(op, payload), 200
        if segments == ["v1", "managed-turn"]:
            return {"v": 1, "ok": True, "result": self._managed_turn(payload)}, 200
        if len(segments) == 2 and segments == ["v1", "tasks"]:
            return self.service.dispatch("create_task", payload), 201
        if segments == ["v1", "tasks", "create"]:
            return self.service.dispatch("create_task", payload), 201
        if segments in (["v1", "turns"], ["v1", "turns", "begin"]):
            return self.service.dispatch("begin", payload), 200
        if len(segments) >= 3 and segments[:2] == ["v1", "operations"]:
            if len(segments) != 3:
                raise ControlPlaneError("not_found", "control-plane route does not exist", 404)
            return self.service.dispatch(segments[2], payload), 200
        if len(segments) < 4 or segments[:2] != ["v1", "turns"]:
            raise ControlPlaneError("not_found", "control-plane route does not exist", 404)
        rid = segments[2]
        action = segments[3]
        if action == "recover":
            return self.service.dispatch("recover", self._with_id(payload, rid)), 200
        if action in {"model-request", "model_request", "model-requests", "model_requests"}:
            return self.service.dispatch("model_request", self._with_id(payload, rid)), 200
        if action in {"model-response", "model_response", "model-responses", "model_responses"}:
            return self.service.dispatch("model_response", self._with_id(payload, rid)), 200
        if action in {"commit"}:
            return self.service.dispatch("commit", self._with_id(payload, rid)), 200
        if action in {"abort"}:
            return self.service.dispatch("abort", self._with_id(payload, rid)), 200
        if action in {"tool-start", "tool_start", "tool"}:
            return self.service.dispatch("tool_start", self._with_id(payload, rid)), 200
        if action in {"tool-finish", "tool_finish"}:
            return self.service.dispatch("tool_finish", self._with_id(payload, rid)), 200
        if action in {"action-eligible", "action_eligible", "eligibility"}:
            return self.service.dispatch("action_eligible", self._with_id(payload, rid)), 200
        if action == "tools" and len(segments) in {5, 6}:
            if len(segments) == 5:
                operation_name = segments[4]
                call_id = None
            else:
                call_id, operation_name = segments[4], segments[5]
            if operation_name in {"start", "finish", "eligible", "action-eligible", "action_eligible"}:
                mapped = {"start": "tool_start", "finish": "tool_finish", "eligible": "action_eligible",
                          "action-eligible": "action_eligible", "action_eligible": "action_eligible"}[operation_name]
                body = self._with_id(payload, rid)
                if call_id is not None:
                    if "call_id" in body and body["call_id"] != call_id:
                        raise ControlPlaneError("conflict", "call_id does not match the URL")
                    body["call_id"] = call_id
                return self.service.dispatch(mapped, body), 200
        raise ControlPlaneError("not_found", "control-plane route does not exist", 404)

    def _managed_turn(self, envelope: Mapping[str, Any]) -> Any:
        """Compatibility envelope shared by the dependency-free client SDKs."""
        data = _payload(envelope)
        _known(data, {"v", "op", "request_id", "generation", "call_id", "args"})
        if data.get("v") != 1:
            raise ControlPlaneError("protocol", "managed-turn envelope requires v=1")
        op = _required_string(data.get("op"), "op", limit=64).lower().replace("-", "_")
        request_id = _required_string(data.get("request_id"), "request_id", limit=128)
        args = data.get("args", {})
        if not isinstance(args, dict):
            raise ControlPlaneError("invalid", "args must be a JSON object")
        body = copy.deepcopy(args)
        body["request_id"] = request_id
        if "generation" in data:
            body["generation"] = data["generation"]
        if "call_id" in data:
            body["call_id"] = data["call_id"]
        mapping = {
            "inspect": "turn", "before_model": "model_request",
            "after_model": "model_response", "after_tool": "tool_finish",
        }
        if op == "before_tool":
            ttl_ms = body.pop("ttl_ms", 5000)
            started = self.service.dispatch("tool_start", body)
            eligibility = self.service.dispatch(
                "action_eligible",
                {"request_id": request_id, "generation": data.get("generation"),
                 "call_id": data.get("call_id"), "ttl_ms": ttl_ms},
            )
            return {"tool": started, "eligibility": eligibility}
        if op == "after_model" and "error" in body:
            if "response_json" in body:
                raise ControlPlaneError("invalid", "after_model accepts response_json or error, not both")
            body["response"] = {"error": body.pop("error")}
        if op == "after_tool" and "error" in body:
            if "result" in body:
                raise ControlPlaneError("invalid", "after_tool accepts result or error, not both")
            body["result"] = {"error": body.pop("error")}
        return self.service.dispatch(mapping.get(op, op), body)

    @staticmethod
    def _with_id(data: Mapping[str, Any], request_id: str) -> dict[str, Any]:
        body = _payload(data)
        if "request_id" in body and body["request_id"] != request_id:
            raise ControlPlaneError("conflict", "request_id does not match the URL")
        body["request_id"] = request_id
        return body

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        shown_host = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{shown_host}:{port}"

    def start(self) -> "ControlPlaneServer":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self.serve_forever,
                                            name="cmp-control-plane", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.shutdown()
            self._thread.join(timeout=5)
        self.server_close()
        if self.close_service:
            self.service.close()

    def __enter__(self) -> "ControlPlaneServer":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


@dataclass
class NativeControlPlane:
    """Owned native convenience wrapper used by small local applications."""

    server: ControlPlaneServer
    control_plane: ControlPlane

    @classmethod
    def create(cls, binary: str | Path, database: str | Path, *,
               create: bool = False, timeout: float = 15.0,
               host: str = "127.0.0.1", port: int = 0,
               auth_token: str | None = None,
               max_body_bytes: int = DEFAULT_MAX_BODY_BYTES) -> "NativeControlPlane":
        plane = ControlPlane.from_native(binary, database, create=create,
                                         timeout=timeout, auth_token=auth_token)
        server = ControlPlaneServer(plane, host=host, port=port,
                                    auth_token=auth_token,
                                    max_body_bytes=max_body_bytes,
                                    close_service=True)
        return cls(server, plane)

    def start(self) -> "NativeControlPlane":
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def base_url(self) -> str:
        return self.server.base_url


def create_server(harness: Harness | str | Path, database: str | Path | None = None, **options) -> ControlPlaneServer:
    """Create a server around a harness or ``(native_binary, database)`` pair."""
    if isinstance(harness, (str, Path)):
        if database is None:
            raise TypeError("database is required when harness is a native binary path")
        native_options = {key: options.pop(key) for key in ("create", "timeout") if key in options}
        plane = ControlPlane.from_native(harness, database, **native_options,
                                         auth_token=options.get("auth_token"))
        options["close_service"] = True
    else:
        plane = harness if isinstance(harness, ControlPlane) else ControlPlane(harness, auth_token=options.get("auth_token"))
    return ControlPlaneServer(plane, **options)


__all__ = ["DEFAULT_MAX_BODY_BYTES", "TOKEN_ENVIRONMENT_VARIABLE", "ControlPlaneError",
           "ControlPlane", "ControlPlaneServer", "NativeControlPlane", "create_server"]
