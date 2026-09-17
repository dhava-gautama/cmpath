# Kimi adapter

The MCP entry in `kimi.mcp.json` gives Kimi access to CMP memory tools. MCP
transport does not wrap Kimi's model call or arbitrary tools. A host/plugin
using managed turns must explicitly execute all five hooks:
`before-model`, `after-model`, `before-tool`, `after-tool`, and `commit`.

For each turn call `begin`; call `before-model` with the exact serialized
request before dispatch and `after-model` with the confirmed response or known
error. Before each tool callback call `before-tool`, refuse on rejection, then
call `after-tool` with the confirmed result. Call `commit` only after the final
answer is assembled. A broken connection is indeterminate: inspect/reconcile
it rather than replaying the provider or tool automatically.

`kimi.mcp.json` is only an MCP transport template. Configure the HTTP CMP URL
in the host-side SDK client and replace its command/database path.
