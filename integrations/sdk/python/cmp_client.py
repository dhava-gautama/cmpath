"""Tiny dependency-free client for the CMP managed-turn JSON HTTP contract.

The client deliberately exposes lifecycle calls instead of wrapping a model or
tool SDK.  Hosts must call ``before_model``, ``after_model``, ``before_tool``,
``after_tool`` and ``commit`` at their real execution boundaries.  A transport
failure while writing a mutating operation is reported as an unknown outcome;
this client never retries it.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping


_MISSING = object()
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class CMPError(RuntimeError):
    """A protocol or server error returned by CMP."""

    def __init__(self, code: str, message: str, *, status: int | None = None,
                 uncertain: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.uncertain = uncertain


class CMPTransportError(CMPError):
    """The server outcome is unknown because HTTP I/O failed."""

    def __init__(self, message: str):
        super().__init__("transport", message, uncertain=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


Transport = Callable[[str, bytes, Mapping[str, str], float], tuple[int, bytes]]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _wire_json(value: Any) -> str:
    """Return exact caller text, or a strict compact JSON representation."""

    if isinstance(value, str):
        # Validate before sending so malformed provider payloads fail locally.
        json.loads(value)
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class CMPClient:
    """A small synchronous client using only Python's standard library.

    ``transport`` is an optional test seam with signature
    ``(url, request_bytes, headers, timeout) -> (status, response_bytes)``.
    Production calls use ``urllib`` with redirects and environment proxies
    disabled.  Mutating calls are intentionally not retried.
    """

    def __init__(self, base_url: str, *, api_key: str | None = None,
                 timeout: float = 30.0, path: str = "/v1/managed-turn",
                 transport: Transport | None = None):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("timeout must be a number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("path must be an absolute path without query or fragment")
        parsed = urllib.parse.urlparse(_text(base_url, "base_url"))
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or (parsed.scheme != "https" and not local_http):
            raise ValueError("base_url must use HTTPS or loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        self.url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc,
                                             parsed.path.rstrip("/") + path, "", "", ""))
        self.api_key = api_key if api_key is not None else os.environ.get("CMP_API_KEY")
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise ValueError("api_key must be text")
        self.timeout = float(timeout)
        self._transport = transport

    def _post(self, raw: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        if self._transport is not None:
            try:
                status, data = self._transport(self.url, raw, headers, self.timeout)
            except CMPError:
                raise
            except Exception as exc:  # pragma: no cover - exercised by callers
                raise CMPTransportError(str(exc)) from exc
            if not isinstance(status, int) or not isinstance(data, (bytes, bytearray)):
                raise CMPTransportError("transport must return (int, bytes)")
            return status, bytes(data)

        request = urllib.request.Request(self.url, data=raw,
                                         headers=dict(headers), method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return int(response.getcode()), response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Preserve an error body for the common JSON error response path.
            try:
                return int(exc.code), exc.read(_MAX_RESPONSE_BYTES + 1)
            except Exception as read_exc:
                raise CMPTransportError(str(read_exc)) from read_exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise CMPTransportError(str(exc)) from exc

    def _call(self, op: str, request_id: str, *, generation: int | None = None,
              call_id: str | None = None, args: Mapping[str, Any] | None = None) -> Any:
        _text(op, "op")
        _text(request_id, "request_id")
        if generation is not None and (type(generation) is not int or generation < 1):
            raise ValueError("generation must be a positive integer")
        if call_id is not None:
            _text(call_id, "call_id")
        envelope: dict[str, Any] = {"v": 1, "op": op, "request_id": request_id,
                                    "args": dict(args or {})}
        if generation is not None:
            envelope["generation"] = generation
        if call_id is not None:
            envelope["call_id"] = call_id
        try:
            raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"request is not strict JSON: {exc}") from exc
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        status, data = self._post(raw, headers)
        if len(data) > _MAX_RESPONSE_BYTES:
            raise CMPError("protocol", "response exceeds 16 MiB", status=status)
        try:
            response = json.loads(bytes(data).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CMPError("protocol", f"response is not UTF-8 JSON: {exc}", status=status) from exc
        if not isinstance(response, dict) or response.get("v") != 1:
            raise CMPError("protocol", "response must be a v=1 object", status=status)
        if status < 200 or status >= 300 or response.get("ok") is False:
            error = response.get("error")
            if isinstance(error, dict):
                code = str(error.get("code", "server_error"))
                message = str(error.get("message", "CMP request failed"))
            else:
                code, message = "server_error", f"HTTP {status}"
            raise CMPError(code, message, status=status)
        if response.get("ok") is not True:
            raise CMPError("protocol", "success response must set ok=true", status=status)
        return response.get("result")

    def begin(self, request_id: str, task_id: int, query: str, *, system: str = "",
              model_key: str = "", budget: int = 2000, reserve: int = 0,
              scope: str = "lineage", retrieval_limit: int = 24, recent: int = 4,
              counting: str = "estimated-json") -> Any:
        if type(task_id) is not int or task_id < 1:
            raise ValueError("task_id must be a positive integer")
        if any(type(value) is not int or value < 0 for value in (budget, reserve, retrieval_limit, recent)):
            raise ValueError("budget, reserve, retrieval_limit, and recent must be nonnegative integers")
        if budget <= reserve:
            raise ValueError("budget must exceed reserve")
        _text(query, "query")
        _text(scope, "scope")
        args = {"task_id": task_id, "query": query, "system": system,
                "model_key": model_key, "budget": budget, "reserve": reserve,
                "scope": scope, "retrieval_limit": retrieval_limit, "recent": recent,
                "counting": counting}
        return self._call("begin", request_id, args=args)

    def inspect(self, request_id: str) -> Any:
        return self._call("inspect", request_id)

    def recover(self, request_id: str, generation: int) -> Any:
        return self._call("recover", request_id, generation=generation)

    def before_model(self, request_id: str, generation: int, call_id: str,
                     payload: Any, *, units: int, counting: str) -> Any:
        if type(units) is not int or units < 0:
            raise ValueError("units must be a nonnegative integer")
        args = {"payload_json": _wire_json(payload), "units": units,
                "counting": _text(counting, "counting")}
        return self._call("before_model", request_id, generation=generation,
                          call_id=call_id, args=args)

    def after_model(self, request_id: str, generation: int, call_id: str, *,
                    response: Any = _MISSING, error: Any = _MISSING) -> Any:
        if (response is _MISSING) == (error is _MISSING):
            raise ValueError("after_model requires exactly one of response or error")
        args: dict[str, Any]
        if response is not _MISSING:
            args = {"response_json": _wire_json(response)}
        else:
            args = {"error": error}
        return self._call("after_model", request_id, generation=generation,
                          call_id=call_id, args=args)

    def before_tool(self, request_id: str, generation: int, call_id: str,
                    name: str, arguments: Any, *, ttl_ms: int = 5000) -> Any:
        if type(ttl_ms) is not int or ttl_ms < 1:
            raise ValueError("ttl_ms must be a positive integer")
        args = {"name": _text(name, "name"), "arguments": arguments, "ttl_ms": ttl_ms}
        return self._call("before_tool", request_id, generation=generation,
                          call_id=call_id, args=args)

    def after_tool(self, request_id: str, generation: int, call_id: str, *,
                   result: Any = _MISSING, error: Any = _MISSING,
                   lease_token: str | None = None) -> Any:
        if (result is _MISSING) == (error is _MISSING):
            raise ValueError("after_tool requires exactly one of result or error")
        args: dict[str, Any] = {}
        if result is not _MISSING:
            args["result"] = result
        else:
            args["error"] = error
        if lease_token is not None:
            args["lease_token"] = _text(lease_token, "lease_token")
        return self._call("after_tool", request_id, generation=generation,
                          call_id=call_id, args=args)

    def commit(self, request_id: str, generation: int, reply: Mapping[str, Any]) -> Any:
        if not isinstance(reply, Mapping):
            raise ValueError("reply must be a JSON object")
        return self._call("commit", request_id, generation=generation,
                          args={"reply": dict(reply)})

    def abort(self, request_id: str, generation: int) -> Any:
        return self._call("abort", request_id, generation=generation)


if __name__ == "__main__":  # A tiny inspect smoke example; no request is retried.
    client = CMPClient(os.environ.get("CMP_URL", "http://127.0.0.1:8080"))
    request_id = os.environ.get("CMP_REQUEST_ID", "sdk-example")
    print(json.dumps(client.inspect(request_id), ensure_ascii=False, indent=2))
