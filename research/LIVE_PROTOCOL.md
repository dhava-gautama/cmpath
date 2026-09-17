# Live validation extension, 11 September 2026

This protocol accompanies `scripts/run_live_validation.py`. It preserves the
original `PILOT_PROTOCOL.md`, evaluator and local observations. It was written
after transport probes failed, before any successful provider response or live
answer was available. It is not a preregistration of those transport probes.

## Access evidence and execution boundary

The supplied endpoint was normalized by appending `/chat/completions` to its
`/v1` base. A direct authenticated POST failed DNS resolution. An unauthenticated
HEAD through the runtime's configured proxy returned HTTP 403. A subsequent
authenticated POST through that same proxy returned HTTP 403 with the body
`error code: 1010`. No completion JSON or usage was received. These are transport
observations, not model failures or memory-system scores.

The new runner selects a direct connection by default. `--use-env-proxy`
explicitly selects the configured runtime proxy. Both verify TLS and refuse
redirects; neither switches routes automatically. It reads `CMP_API_KEY` only
from the environment, keeps authorization headers out of artifacts, and never
retries automatically. A destination directory must be new. An interrupted run
requires inspection, not blindly rerunning the command in another directory.

Every HTTP attempt writes and fsyncs the exact request JSON before dispatch.
Successful responses retain original UTF-8 JSON, per-response provider usage and
elapsed transport time. Errors and dispatch/response counts are separate. Missing
usage stays unknown; no token price or billing total is inferred. These elapsed
times include proxy, network and provider work, and are not backend benchmarks.

Successful HTTP bodies are recorded before JSON parsing. Malformed/non-object
JSON remains inspectable; invalid UTF-8 is retained as base64. Oversized bodies
and credential echoes are withheld with an explicit receipt status. HTTP-body
receipts and accepted JSON-object responses have separate counters. A valid JSON
object alone does not prove a usable Chat Completions answer.

## Compatibility gate

One no-tool request asks for `CMP_OK`, using temperature 0 and max_tokens 200.
Only a nontruncated `stop` response containing exactly that text admits the rest
of the study. If provider reasoning exhausts this allowance, or a provider format
differs, stop and document a separate protocol amendment before changing either
condition. This gate alone does not verify tool support or token counting.

## Matched fixed reader

Use all 40 cases and both conditions from `results/pilot/prompts.jsonl`: 80
requests. There is no smaller convenience subset. The original synthetic sources,
packed contexts, task scope and question order remain unchanged. Condition order
alternates within pairs. Both conditions receive the same endpoint, model,
temperature 0, `tools: []` and max_tokens 200. Full input must fit 1,800 estimated
units; packed context retains its original 1,000-unit limit. No tokenizer accuracy
is claimed. A successful gate plus this stage costs at most 81 dispatch attempts.

Labels are hashed for provenance before execution but not parsed for scoring
until all 80 requests finish. They never become provider payloads. The new
diagnostic parses complete citation IDs: `T1:M2` does not match `T1:M20`.
The historical evaluator's substring diagnostic is preserved with its original
hash and should not be interpreted as exact citation matching.

Every response, including empty and truncated answers, remains in the denominator.
Exact citation presence is not answer correctness or evidence support. After
successful execution, review every answer for the requested fact, contradictions,
abstention and claim support using the original sources. The eight unanchored
paraphrases deliberately lack the gold source in both packed contexts; suitable
abstention should be distinguished from successful fact recall. Report the eight
templated workflows as clustered synthetic cases, not independent real users.
No superiority threshold or generalization claim is specified.

## Document tools and controlled recovery

Copy only `docs/HARNESS.md`, `docs/RETENTION.md`, and `docs/AGENT_PILOT.md`
into a separate workspace. Preserve their `docs/` paths and hash every file
before and after execution. Keep databases, labels, traces and outputs outside
that workspace. Each of two tasks starts with an empty task snapshot and no
preloaded answer facts. The exact questions are in the runner.

Each task uses max_tokens 1,000, 8 model rounds at most, a total 16,000-unit
estimated request budget with 1,000 reserved, temperature 0 and the same three
read-only document tools. This CMP-only integration exercise has no competing
memory condition. Including the gate, it permits at most 17 dispatches; the
complete fixed-reader plus agent run permits at most 97. These are request and
output ceilings, not a dollar spending cap.

1. Ask whether journal JSONL can restore an interrupted turn and what backup
   method is recommended. Expect no importer/resumable backup and SQLite-aware
   backup guidance, supported by `RETENTION.md`.
2. Ask about a checkpointed model request without a response, and protection of
   pending turns and unresolved started tools during retention. Expect visible
   uncertainty, explicit reconciliation/retry, and retention protection supported
   by `AGENT_PILOT.md` and `RETENTION.md`.

Score successful document reads/searches separately from final answer quality
and supporting citation ranges. A model can commit an incorrect or uncited answer.
The runner therefore emits review-required results rather than a product pass.

After each commit, close and reopen the native process, recreate the agent and
repeat the identical request. Check exact saved reply equality, zero new model
dispatches, and unchanged model/tool journal records.

For task 2, inject a controlled pause immediately before its second model request
is checkpointed. The first model response and its tools have already been
journaled. Close/reopen the native process and explicitly resume. Check a higher
generation, unchanged saved model/response rows, and no second execution of
completed tool IDs. Record actual tool executions separately. If the model never
calls tools, this pause is not exercised and must not be counted as passing.
This is deliberate fault injection around a live trace, not a spontaneous crash
or proof of exactly-once external effects.

The independent unit suite also checks that an absent model response blocks
resume with `indeterminate_model` and zero new dispatches. Those are local
fixtures; the failed access probes did not exercise a live native-agent journal.

## Artifacts

The runner records protocol/script/input hashes, configuration, attempts, raw
request and response JSONL, usage objects, fixed-reader diagnostic rows, tool
execution events, document hashes, agent answers and coherent journal exports.
The SQLite database supports local inspection/recovery; JSONL exports do not
constitute resumable backups. Package reports must keep local fixtures, failed
access probes and completed live studies clearly distinguished.
