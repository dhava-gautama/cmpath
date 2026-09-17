# Provider-configured research agent pilot

`examples/research_agent.py` runs an actual Chat Completions tool loop when given
an endpoint and model. It uses the same native Go journal as `NativeHarness`.
The included automated tests are local protocol fixtures, **not live-model
quality, latency, cost, or token-accuracy measurements**. No live model was
invoked during this implementation.

Build the native binary using the repository build instructions, install the
Python package, then set `CMP_MODEL_ENDPOINT` and `CMP_MODEL_NAME` to your provider configuration:

```sh
cmpath-agent --binary native/bin/cmpath-native \
  --db research.db --create --new-task 'Release research' \
  --workspace docs --endpoint "$CMP_MODEL_ENDPOINT" \
  --model "$CMP_MODEL_NAME" --request-id research-001 \
  --question 'Who owns the launch, and which document supports the date?'
```

Credentials are read from `CMP_API_KEY` (or `--key-env`); they are never stored
in the request journal. Select an existing task with `--task ID`. Database
creation requires `--create`. The endpoint must use HTTPS or loopback HTTP;
redirects and environment proxies are disabled. There are no other HTTP tools,
shell tools, or write tools. Provider calls receive the selected document text,
so choose the workspace and provider accordingly.

The three tools list documents, read numbered lines and perform literal searches.
They support UTF-8 `.txt`, `.md`, `.rst`, `.csv`, `.json` and `.log` files, with
bounded file sizes, file counts and results. Tools return local line citations;
the system prompt requests these citations and preserves CMP evidence citations.
Citation correctness remains a model behavior to evaluate, not a guarantee of
the loop. Parent traversal, absolute paths and symlinks are rejected. This pilot
uses POSIX directory-descriptor reads and assumes a trusted local workspace with no adversarial concurrent path changes;
it is not an operating-system sandbox.

Every round checkpoints the **entire serialized request**, including tool schemas,
all assistant tool calls, tool results and generation options, before dispatch.
The default `estimated-json` count is a character-based estimate, **not exact
provider tokens**. `--budget` and `--max-tokens` reserve the selected output
allowance; count-unit compatibility is the caller's responsibility. For a locally
installed provider-aware counter, pass `--counter-command '["/path/to/counter"]'`
and `--counter-name YOUR-SCHEME`. The command receives the complete JSON request
on stdin and returns `{"units": INTEGER}`. No tokenizer is downloaded implicitly.
Python callers can inject `counter(serialized_json: str) -> int` and explicitly
name its scheme in `AgentConfig`. The endpoint/model, tools, workspace and loop
settings form the logical request configuration identity.

Inspect an interrupted request using `--binary PATH --db PATH
--request-id ID --inspect`; no provider configuration is needed for inspection. Resume with the original question,
existing `--task ID` and `--resume`. Saved provider responses reconstruct the
exact assistant messages; completed tools reuse their journaled results. The
reconstructed follow-up payload must exactly match any saved request. Tool IDs
remain intact in the provider transcript, while journal IDs include the round.

If a model request was checkpointed but its response is absent, resume stops with
`indeterminate_model`. The provider may already have completed or charged for it.
Reconcile a known response through `RunSession.record_model_response`, or choose
`--unknown-outcome-policy retry` explicitly to dispatch the identical saved
request again. An uncertain started tool likewise requires explicit reconciliation
through the harness. There is no automatic retry of either unknown outcome.
`dispatches_this_process` counts actual dispatch attempts separately from distinct
logical model-call journal records; it is not a durable cross-process billing
counter. `--max-turns` caps rounds; reaching it leaves the request unfinished and
inspectable. Changing configuration under the same request ID is rejected.

The Python API lives in `cmpath.agent`: `AgentConfig`, `WorkspaceTools`, and
`ResearchAgent(harness, config, counter=None, transport=None)`. Call
`run(task_id, prompt, resume=False, unknown_outcome_policy="error")` for the saved
reply. The optional transport receives exact UTF-8 request bytes and returns a
Chat Completions response dictionary; local tests inject protocol fixtures here.
