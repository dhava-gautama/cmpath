# Generic harness middleware

Use this framework-neutral sequence when the host owns the model transport and
tool registry: `begin(request_id, task_id, query)`; `before-model` with the
exact request and count; the model transport; `after-model` with the exact
response or known error; repeated `before-tool`, eligible tool callback, and
`after-tool`; then `commit` with the reply. Abort or reconcile when work cannot
be completed.

The five hook names are intentionally explicit: `before-model`, `after-model`,
`before-tool`, `after-tool`, and `commit`. Middleware should reject failed
before-hooks and commits with unresolved tool calls. On a transport timeout,
mark the operation indeterminate and expose inspect/reconcile; do not issue an
automatic mutating retry.

Eligibility is a short-lived local certificate and audit record, not a
transparent fence or distributed transaction. A remote service must
participate in its own transaction if atomicity is required.
