"""Explicit complete-request counters and a bounded local tokenizer-command adapter.

The host must choose a tokenizer/chat template matching its model endpoint.
This module never downloads a model or silently calls an inference service.
"""
from __future__ import annotations

import json
import math
import subprocess
import tempfile


def estimated_request_units(serialized: str) -> int:
    """Character-based estimate of a full request; not provider token accounting."""
    return (len(serialized) + 3) // 4 + 8


def utf8_request_bytes(serialized: str) -> int:
    """Exact UTF-8 bytes of the complete request, not model tokens."""
    return len(serialized.encode("utf-8"))


class LocalTokenCounter:
    """Run an explicit argv with full request JSON on stdin and JSON units on stdout.

    The command must consume the complete provider payload, apply the actual
    model's chat template including tool schemas, and print {"units": integer}.
    It runs without a shell. Failures and timeouts stop dispatch; there is no
    estimate fallback. Commands are trusted host configuration, never model tools.
    """

    def __init__(self, command, *, name: str, timeout: float = 10.0):
        if not isinstance(command, (list, tuple)) or not command or any(not isinstance(x, str) or not x or "\x00" in x for x in command):
            raise ValueError("counter command must be a nonempty list of argv strings")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("counter needs a descriptive model/template name")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("counter timeout must be positive and finite")
        self.command = list(command)
        self.name = name
        self.timeout = timeout

    def __call__(self, serialized: str) -> int:
        if not isinstance(serialized, str):
            raise TypeError("counter requires serialized full-request JSON")
        # Spool command output so accidental verbosity cannot fill Python memory.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(self.command, input=serialized.encode("utf-8"),
                                        stdout=output, stderr=errors, timeout=self.timeout,
                                        check=False, shell=False)
            except subprocess.TimeoutExpired as exc:
                raise ValueError("token counter timed out; model dispatch stopped") from exc
            if result.returncode:
                raise ValueError(f"token counter failed with exit code {result.returncode}")
            if output.tell() > 4096:
                raise ValueError("token counter output exceeds 4096 bytes")
            output.seek(0)
            try:
                answer = json.loads(output.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("token counter must return one JSON object") from exc
        if not isinstance(answer, dict) or set(answer) != {"units"}:
            raise ValueError("token counter must return exactly {'units': integer}")
        units = answer["units"]
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise ValueError("token counter units must be a nonnegative integer")
        return units
