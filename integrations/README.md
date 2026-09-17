# CMP integrations

`cmpath-mcp` exposes CMP as a local MCP server over stdio. It is the portable
adapter for Codex, Kimi Code CLI, Cursor Agents, Hermes Agent, and other MCP
clients. The server provides task creation and resolution, immutable evidence,
scoped search and context packing, versioned facts, health checks, and standalone
SQLite backups. It deliberately exposes no delete or arbitrary-SQL tool.

Install from this checkout with the integration dependency:

```bash
uv tool install '.[integrations]'
cmpath-mcp --db ~/.local/share/cmpath/memory.db
```

For development, use `uv run --project /path/to/cmpath --extra integrations
cmpath-mcp --db /path/to/memory.db`. `mcp-stdio.json` is a generic config template.

## Client adapters

- Codex: the personal `cmpath-memory` marketplace plugin launches this server
  through WSL and stores data at `~/.local/share/cmpath/codex.db` inside WSL.
- Kimi Code CLI: `.kimi-code/mcp.json` is ready for this checkout. Restart Kimi,
  trust the workspace command, then inspect it with `/mcp`.
- Cursor Agents: `.cursor/mcp.json` is ready for this checkout. Restart the
  editor/agent and run `agent mcp list` or its MCP UI.
- Hermes Agent: merge `hermes-config.yaml` into `~/.hermes/config.yaml`, replacing
  both absolute paths. Keep the included tool allowlist unless more tools are
  intentionally added.

Each client uses its own database by default so access domains do not leak into
one another. To share memory intentionally, point clients at the same database,
but serialize writes through one long-running server and keep a SQLite-aware
backup policy. Do not place databases on filesystems with uncertain locking.

## Native harness integration

MCP covers broad tool compatibility. Hosts needing durable model/tool recovery,
provenance enforcement, or action-eligibility leases should use `NativeHarness`
or embed `native/engine` directly; MCP clients cannot automatically fence effects
performed by unrelated tools.

For a separate host process, run the authenticated managed-turn service:

```bash
export CMPATH_CONTROL_PLANE_TOKEN='use-a-secret-manager'
cmpath-control --binary native/bin/cmpath-native --db ~/.local/share/cmpath/control.db --create
```

It binds to loopback by default. Non-loopback binds are rejected without a token.
Dependency-free Python, TypeScript, and Go clients live in `integrations/sdk/`;
framework hook guides live in `integrations/adapters/`. The executable contract
and offline failure-path suite are under `conformance/`.
