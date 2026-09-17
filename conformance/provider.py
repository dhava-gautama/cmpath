"""Offline provider boundary checks used by the conformance suite.

The callable transport is injected by the caller, so exercising this module
never opens a socket.  A production HTTP adapter can use the same rules around
its own request library.
"""
from __future__ import annotations

import json
from typing import Any, Callable

try:
    from .reference import AdapterError, _wire
except ImportError:  # unittest discover -s conformance imports as top-level
    from reference import AdapterError, _wire


class ProviderBoundary:
    """Serialize once, call once, and bound the returned provider body."""

    MAX_REQUEST_BYTES = 16 * 1024 * 1024
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(self, transport: Callable[[bytes, str | None], Any], *, secret: str | None = None):
        self.transport = transport
        self.secret = secret
        self.calls = 0

    def send(self, payload: Any):
        try:
            raw = _wire(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AdapterError("invalid", "provider request is not finite JSON") from exc
        if len(raw) > self.MAX_REQUEST_BYTES:
            raise AdapterError("body_limit", "provider request exceeds 16 MiB")
        self.calls += 1
        try:
            response = self.transport(raw, self.secret)
        except AdapterError:
            raise
        except PermissionError as exc:
            raise AdapterError("auth", "provider authentication was rejected") from exc
        if isinstance(response, str):
            body = response.encode("utf-8")
        elif isinstance(response, bytes):
            body = response
        else:
            try:
                body = _wire(response).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise AdapterError("invalid", "provider response is not finite JSON") from exc
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise AdapterError("body_limit", "provider response exceeds 8 MiB")
        try:
            value = json.loads(body.decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("invalid", "provider response is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError("invalid", "provider response must be a JSON object")
        return value
