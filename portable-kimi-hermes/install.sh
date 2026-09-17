#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v uv >/dev/null 2>&1 || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
uv run --quiet --no-project --with pyyaml python "$root/portable-kimi-hermes/install.py" --root "$root" --home "$HOME"
echo "CMP installed for Kimi CLI and Hermes Agent; model credentials were unchanged."
