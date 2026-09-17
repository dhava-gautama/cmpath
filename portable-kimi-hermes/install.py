from __future__ import annotations

import argparse
import json
from pathlib import Path
import yaml

TOOLS = ["cmp_health", "cmp_create_task", "cmp_list_tasks", "cmp_get_task", "cmp_resolve_task", "cmp_append_evidence", "cmp_search", "cmp_context", "cmp_set_fact", "cmp_backup"]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    root, home = Path(args.root).resolve(), Path(args.home).resolve()
    launcher = str(root / "scripts" / "run_mcp_wsl.sh")

    kp = home / ".kimi-code" / "mcp.json"
    kp.parent.mkdir(parents=True, exist_ok=True)
    kimi = json.loads(kp.read_text(encoding="utf-8")) if kp.exists() else {}
    if not isinstance(kimi, dict): raise SystemExit(f"Expected a JSON object in {kp}")
    kimi.setdefault("mcpServers", {})["cmpath-memory"] = {"command": "bash", "args": [launcher, str(home / ".local/share/cmpath/kimi.db")], "startupTimeoutMs": 120000, "toolTimeoutMs": 60000}
    kp.write_text(json.dumps(kimi, indent=2) + "\n", encoding="utf-8")

    hp = home / ".hermes" / "config.yaml"
    hp.parent.mkdir(parents=True, exist_ok=True)
    hermes = (yaml.safe_load(hp.read_text(encoding="utf-8")) or {}) if hp.exists() else {}
    if not isinstance(hermes, dict): raise SystemExit(f"Expected a YAML mapping in {hp}")
    hermes.setdefault("mcp_servers", {})["cmpath-memory"] = {"command": "bash", "args": [launcher, str(home / ".local/share/cmpath/hermes.db")], "enabled": True, "tools": {"include": TOOLS}}
    hp.write_text(yaml.safe_dump(hermes, sort_keys=False, allow_unicode=True), encoding="utf-8")

if __name__ == "__main__": main()
