"""Run the offline CMP adapter conformance suite from a source checkout."""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from conformance.test_conformance import run_suite


if __name__ == "__main__":
    raise SystemExit(0 if run_suite() else 1)

