# OpenAI Agents SDK adapter

Wrap the actual model transport and tool function in the SDK version used by
the host. A run hook or trace span alone is not a CMP enforcement point.

The wrapper begins with `cmp.begin(request_id, task_id, user_text)`. Its
`before-model` hook serializes the complete provider request (messages, tools,
and options) and calls `cmp.before_model` immediately before dispatch. Its
`after-model` hook calls `cmp.after_model` with the confirmed response or known
provider error. Before every tool function, `before-tool` calls
`cmp.before_tool` and refuses the callback if eligibility is rejected. The
`after-tool` hook calls `cmp.after_tool` with the confirmed result/error and
lease token. The `commit` hook calls `cmp.commit` after the final answer and
all tool records are complete.

This adapter must execute all five explicit hooks: `before-model`,
`after-model`, `before-tool`, `after-tool`, and `commit`. If a process or
network failure makes a provider/tool result uncertain, inspect and reconcile
it with an idempotency key before resuming; do not silently retry. The wrapper
does not fence effects from code paths that bypass it.

`openai-agents.example.json` contains host-owned settings only; it is not an
OpenAI Agents SDK configuration schema.
