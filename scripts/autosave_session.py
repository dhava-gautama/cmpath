#!/usr/bin/env python3
"""Thin shim over ``cmpath.autosave_cli.main`` (console script: ``cmpath-autosave``).

Kept as a stable file path for Kimi Code hook installs whose command runs a
script (``python3 .../scripts/autosave_session.py``) and for the subprocess
tests that invoke the CLI by path. All logic lives in the installed module.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from cmpath.autosave_cli import main
except ImportError:  # hook mode must never break the session
    sys.exit(0)

if __name__ == "__main__":
    sys.exit(main())
