# Explicit CMP adapter guides

These guides show where a host should call the HTTP client in
[`../sdk`](../sdk). Framework hook names change, but the execution boundaries
do not.

CMP is not transparent effect fencing. An MCP registration, tracing callback,
or middleware declaration does not intercept a model provider or an external
tool by itself. A valid adapter must install and execute all five hooks in the
host that owns the real call:

1. `before-model`: send the complete serialized provider request before
   dispatch; do not dispatch if CMP rejects it.
2. `after-model`: record the confirmed response or known provider error. An
   unknown outcome must be inspected or reconciled, not guessed or retried.
3. `before-tool`: record exact intent and obtain fresh eligibility immediately
   before invoking the tool.
4. `after-tool`: record the confirmed result or known error after the callback.
5. `commit`: persist reply, snapshot, facts, and provenance after the work.

The order is:

```text
begin -> before-model -> provider -> after-model
      -> before-tool -> tool -> after-tool (repeat)
      -> commit                 (or abort/reconcile)
```

`before-tool` narrows a local stale-state window. It cannot make an arbitrary
non-participating remote API atomic, undo a side effect, or protect a host that
bypasses the hooks. Use provider/tool idempotency keys and retain the CMP
request ID for recovery.

## Guides and configuration examples

- [OpenAI Agents SDK](openai-agents.md)
- [LangGraph](langgraph.md)
- [Kimi](kimi.md)
- [Cursor](cursor.md)
- [Hermes](hermes.md)
- [Generic harness middleware](harness-middleware.md)

The `*.example.json`, `*.example.yaml`, and `*.mcp.json` files are templates,
not claims that a framework consumes a `cmp` key automatically. Merge MCP
entries into the host configuration and wire the five hooks in code.
