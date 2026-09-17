# Bounded local pilot protocol (frozen before execution)

This is a synthetic retrieval/context and persistence diagnostic, not an LLM
benchmark, downstream task-success study, or evidence of production superiority.
No model endpoint, credentials, installed model, or paid calls are required.

## Corpus and conditions

Eight independent invented workflows each have five probes: exact lookup,
return after task switching, revised fact, anchored paraphrase, and unanchored
paraphrase (40 cases, 80 condition rows). Each task has 30 original source
messages, including old and revised deadlines, unrelated administrative notes,
and a next-action instruction. Case questions and answer/citation labels live
in separate JSONL files; neither is inserted in a retrievable database. Sources
are synthetic, explicitly identified by original source ID and order.

CMP uses the native engine's preview; the baseline is SQLite FTS5 BM25, scoped
to the same explicitly known task ID. Both receive all 240 source messages,
the same task metadata, query, system instruction, empty fact/snapshot metadata,
24 ranked candidates, four recent candidates, and a 1200 estimated-unit budget
with 200 reserved (1000 input allowance). Both use the same quoted JSON envelope
and whole-evidence greedy packing, with ranking score included. Query terms use
the existing lexical normalization; neither gets semantic expansion. Baseline
uses FTS5 unicode61 and recency tie breaking. Ranking implementations differ,
which is an explicit part of this narrow comparison. No annotations are granted
to CMP alone. Revised facts are plain source updates, not gold-generated facts.

Five rounds interleave tasks, so the return probe follows visits to other tasks.
Read-only preview does not itself exercise persistent task navigation; recovery
and saved planner state are tested separately below. Each condition runs once
per case; no tuning, random seed, latency conclusion, significance test, or
confidence interval is appropriate for these eight templated workflows.

## Preregistered diagnostics

Required-source coverage means all case-labeled original citations appear in the
packed context. Reciprocal rank measures first required source in lexical top 24
(zero if absent). Context budget pass means estimated units <=1000. Citation
integrity requires every packed item to match its original source/content/task.
Task isolation requires every packed item to belong to the supplied task.
Revised-fact coverage requires the revised source, but does NOT measure whether
a reader selects the new value when both versions appear. Unanchored paraphrases
intentionally expose the limits of lexical retrieval. These are diagnostics;
there is no overall product pass threshold or claim that coverage equals an answer.

Separate CMP-only contract checks: committed replay avoids a second callback;
kill/reopen preserves a pending context; recovery increments a fencing generation;
uncertain tool execution is blocked; reconciled result is replayed; stale commit
is fenced; planner snapshot survives reopen and task switch. Baseline journaling
is not implemented and is reported not applicable, never scored as a loss.
Callbacks and tools in these checks are deterministic test fixtures, not LLMs.

## Artifacts and interpretation

Emit corpus, queries, labels, exact prompt packages, raw per-condition rows,
contract rows, summary counts, protocol/script/binary SHA256 and runtime versions.
Token units are character-based estimates, not model tokens. Provider usage is
null and model calls zero. Timing is omitted because execution shares a machine
with concurrent implementation/build work. The script creates fresh temporary
databases. Generated files contain no credentials. Live end-to-end model evaluation
is outside this measured local run and requires an explicitly configured provider;
it must use identical model/endpoint/temperature/tools/budget in both conditions,
keep prompts separate from labels, and report actual provider usage independently.

## Optional live fixed-reader extension (not run for the local result)

`--live --endpoint URL --model NAME` executes 80 requests using the shared
ResearchAgent provider dispatch transport, over the already packed condition
prompts. Both conditions use the exact supplied endpoint/model, temperature 0,
no tools (`tools: []`), max_tokens 200, and a full-request allowance of 1800
estimated units within 2000 total. This larger outer envelope accounts for the
provider request fields; the original 1000-unit packed context limit remains.
This is a fixed-reader extension, not the autonomous research agent/tool loop.
Case order is fixed and first condition alternates. `--live-cases N` explicitly
limits the first N cases; it must be reported as a subset. Requests and responses
are persisted separately, no labels enter provider payloads, and labels are read
only after dispatch for required-citation-string diagnostics. Answer correctness
is unmeasured. Actual provider usage objects remain separate from local estimates.
No automatic retries occur; an existing live output directory is refused to
prevent accidental repeated spending. A failed dispatch can have unknown outcome.
