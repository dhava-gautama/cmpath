#!/usr/bin/env sh
# Run the offline contract from a source checkout on POSIX shells.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python 3.10+ is required" >&2
    exit 127
fi

cd "$ROOT"
if [ -n "${PYTHONPATH:-}" ]; then
    export PYTHONPATH="$ROOT/src:$PYTHONPATH"
else
    export PYTHONPATH="$ROOT/src"
fi
exec "$PYTHON" conformance/run.py "$@"

