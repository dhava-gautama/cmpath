# LangGraph adapter

Keep `request_id`, `task_id`, and `generation` in graph state. Add ordinary
nodes around model and tool nodes; graph tracing does not intercept calls.

The entry node calls `begin`. A `before-model` node builds and counts the
complete provider request, then calls `cmp.before_model`; the model node runs
only after that succeeds. An `after-model` node calls `cmp.after_model` with
the response or known error. A `before-tool` node calls `cmp.before_tool` and
only then invokes the tool; an `after-tool` node calls `cmp.after_tool` with
the confirmed result/error and lease token. A terminal `commit` node calls
`cmp.commit` with the final reply, snapshot, facts, and provenance.

The graph must route every model/tool edge, including retries and interrupt
recovery, through all five explicit hooks: `before-model`, `after-model`,
`before-tool`, `after-tool`, and `commit`. Reconcile a timeout before recovery;
a CMP lease does not make an uncooperating remote service atomic.
