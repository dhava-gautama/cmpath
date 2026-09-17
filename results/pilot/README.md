# Local pilot results

This run measures retrieval/context diagnostics across **eight synthetic,
templated workflows and 40 probes**, not 40 independent real-world tasks.
There were **zero model calls** and no downstream task-success measurement.

Both native CMP and SQLite FTS5 BM25 with the same task filters covered the
required source in **32/40 contexts** (MRR **0.8**). Both missed all eight
unanchored paraphrases. Each covered all eight probes in each of the other four
categories. Budget compliance, original-citation integrity, and task isolation
passed **40/40** for each condition. These observations do not show a retrieval
advantage for CMP. A covered revised source does not establish correct selection
of the revised value by a reader.

The separate deterministic CMP persistence checks passed **8/8**. The retrieval
baseline has no execution journal, so these checks are **not applicable**, not
baseline failures. They validate narrow contracts, not general crash safety.

`summary.json` records exact counts, environment and artifact hashes;
`rows.jsonl` records each diagnostic; `contracts.jsonl` records each persistence
check. `corpus.jsonl` contains original synthetic source records. `queries.jsonl`
and `labels.jsonl` remain separate from the retrievable corpus. `prompts.jsonl`
contains the actual packed messages and no gold labels.

Reproduce from the repository root:

```sh
python scripts/evaluate_pilot.py
python -m unittest discover -s tests -p test_pilot_evaluation.py -v
```

See `research/PILOT_PROTOCOL.md` for the frozen local design and limitations.
Token units are estimates; provider usage is null. Timing was omitted because
implementation and builds ran concurrently on the same machine.

The live extension is **unrun**: no model endpoint or credentials were available.
With an explicitly configured OpenAI-compatible endpoint, use:

```sh
python scripts/evaluate_pilot.py --live --endpoint https://YOUR-HOST/v1/chat/completions --model YOUR-MODEL --live-output results/pilot/live-run-1
```

This issues 80 matched fixed-reader requests (40 cases × two conditions), with
no tools. It uses the research agent's provider transport, but does not measure
its autonomous tool loop. `CMP_API_KEY` is optional for endpoints that need it.
Requests, raw responses, provider usage, and post-hoc citation-string diagnostics
are separated. An existing live output directory is refused; failed requests are
not automatically retried. Model answer correctness still requires review.
