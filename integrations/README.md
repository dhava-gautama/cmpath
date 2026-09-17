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
  Its `UserPromptSubmit`, tool, stop and interrupt hooks write one explicit
  `cmp_codex_event` record per event. Use `cmp_context` for a bounded,
  read-only prompt package; the hook itself is not an effect fence.
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

## Codex prompt routes

When asking Codex to recall memory, name the task and scope explicitly. These
prompts keep the host's intent clear:

```text
Use task scope for T3. Answer only from the returned evidence and cite every
claim as T3:Mm. If the evidence is insufficient, say so; do not write memory.

Use lineage scope for T3, including parent release decisions. Preserve each
returned citation and abstain when no cited source supports the answer.
```

The corresponding five-mode read-only MCP call is:

```json
{
  "task_id": 3,
  "query": "What budget was approved?",
  "route": "lineage",
  "budget": 1600,
  "reserve": 300,
  "retrieval_limit": 12,
  "recent": 3
}
```

`cmp_route` accepts `none`, `pinned`, `task`, `lineage`, or `deep` and returns a
`decision` plus the exact `messages` payload, `used_units`,
`input_allowance`, and source-preserving `citations`. Its pins and config are
ephemeral to that request. The lower-level `cmp_context` tool accepts
`task`, `lineage`, or `all` scope. Check `used_units <= input_allowance` before
dispatching a model. Context retrieval does not append evidence, resume a
task, switch active state, call a model, or execute tools.

For `cmp_codex_event` with `event: "UserPromptSubmit"`, route/task options are
passed through the conservative Codex router. An ordinary prompt uses the
current session's small pinned working set; an explicit task or recall/continue
prompt opts into its requested task, lineage, or deep route. An ambiguous
address returns no evidence and its candidates. Tool, stop, and interrupt hook
events are recorded as evidence only and do not receive prompt context.

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
