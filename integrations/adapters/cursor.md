# Cursor adapter

Register CMP's MCP server for memory operations, then put the managed-turn
client in the executor that owns Cursor's provider and tool calls. MCP config
alone is not transparent effect fencing and cannot observe a tool invoked by a
different process.

The executor must run every hook in order: `before-model` with the complete
serialized request, the provider call, `after-model` with the confirmed
response/error, `before-tool` immediately before a tool invocation,
`after-tool` with its confirmed result/error, and `commit` after the final
reply. Refuse a tool when `before-tool` rejects. Reconcile unknown outcomes
before recovery or retry, using an idempotency key where supported.

`cursor.mcp.json` is a mergeable MCP example, not a Cursor managed-turn schema.
