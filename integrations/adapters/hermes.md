# Hermes Agent adapter

Hermes can discover CMP through MCP, but an MCP declaration does not intercept
Hermes' model transport or external actions. Keep the executor's managed-turn
loop explicit and install all five hooks: `before-model`, `after-model`,
`before-tool`, `after-tool`, and `commit`.

Checkpoint the complete serialized model request before dispatch and record its
confirmed response immediately after. Before each tool callback record intent
and obtain eligibility; after the callback record the confirmed result and
lease token. Commit final reply/snapshot/facts/provenance only after those
records succeed. Inspect/reconcile any unknown provider/tool outcome; do not
silently retry. The lease cannot make an uncooperating remote API atomic.

`hermes.example.yaml` mirrors the MCP-only portion of a Hermes configuration;
adjust paths and merge it into the host config.
