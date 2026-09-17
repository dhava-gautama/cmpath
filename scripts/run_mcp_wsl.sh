#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
database="${1:-${CMP_DB_PATH:-$HOME/.local/share/cmpath/memory.db}}"
mkdir -p "$(dirname "$database")"
export UV_PROJECT_ENVIRONMENT="${CMPATH_UV_ENVIRONMENT:-$HOME/.cache/cmpath/venv}"
exec uv run --project "$project_root" --extra integrations cmpath-mcp --db "$database"
