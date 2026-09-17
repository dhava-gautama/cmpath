# CMP 0.4.0a2: integrated pilot and maintenance

11 September 2026. Implementation and local evaluation report.

**Later update:** an endpoint and model were subsequently supplied. The authenticated access probe returned HTTP 403 with Cloudflare error 1010, so no model completion was received. See [LIVE_RESULTS.md](LIVE_RESULTS.md) for the failure evidence, bounded live runner and the expanded 82-test local suite. The local measurements below remain the original study.

## Decision and scope

The three parallel workstreams produced a provider-configured research agent, a matched SQLite/BM25 comparison, and journal export/retention. The code is integrated into a new alpha release. The local results do not show a retrieval advantage: both conditions recovered required evidence in 32 of 40 synthetic cases. The agent's actual model-quality evaluation is ready to run but remains unrun because no model endpoint, credentials or local runtime were configured.

This is a concrete integration and a bounded diagnostic, not evidence that CMP outperforms Mem0, Letta, Graphiti or LangGraph. None of those systems was executed in this study. The immediate comparison is deliberately a simple SQLite/BM25 implementation with the same task IDs, corpus and context envelope. Its purpose is to determine whether CMP adds value beyond ordinary task-filtered lexical retrieval.

## Implemented product changes

| Workstream | Delivered | Verified locally | Still unmeasured |
| --- | --- | --- | --- |
| Research agent | Installed `cmpath-agent` command; real HTTP Chat Completions loop; local document listing, reading and search; full request checkpoints; source citations | Local HTTP protocol fixtures, actual local file tools, multi-round tool sequence, interrupted response/tool recovery, exact wire JSON preservation | Live-model answer correctness, cost, latency and citation behavior |
| Baseline comparison | Forty-case local evaluator and optional matched fixed-reader provider runner; raw corpus, prompts, labels and observations | Required-source coverage, task isolation, intact evidence and counted context; separate recovery contracts | Real-world task success and comparisons against named memory products |
| Maintenance | Installed `cmpath-maintain` command; streaming JSONL audit export; reviewed-plan journal retirement with permanent ID tombstones | Coherent content, no overwrite, pending/uncertain protection, stale-plan rollback, backup and retired-ID protection | Windows/macOS operation, sustained contention and physical disk-loss recovery |

The research agent owns its tool loop and invokes the existing native lifecycle hooks. A host with its own executor can call those hooks directly. It records the complete request before dispatch, including tool definitions and every prior assistant/tool message. Confirmed HTTP responses retain their original UTF-8 JSON text, including whitespace and large integer spellings. Responses are recorded before tool execution so a restart can reconstruct the provider conversation and reuse already completed tool results.

A missing response after a recorded request remains an uncertain remote outcome. Recovery stops until the caller reconciles a known response or explicitly selects retry of the identical request. The provider might already have generated or charged for the first attempt. An uncertain started tool also requires reconciliation. Local generation fences reject stale writes; they cannot cancel an already running external effect.

The tools are read-only and confined to the selected local document root. On POSIX systems, directory-descriptor opens reject symlink traversal component by component and require regular files. File size, result and file-count bounds are explicit. The configured provider receives selected document content. This integration is not an operating-system sandbox or an authenticated multi-tenant service.

## Local comparison protocol

Eight invented workflows each contain thirty original source messages and five probes: exact lookup, return to a task, a revised deadline, a paraphrase retaining an anchor term, and a paraphrase with no matching vocabulary. There are 240 source records, forty cases and eighty condition rows. These are synthetic, templated diagnostics, not forty independent real-world agent tasks.

Both conditions receive the same known task ID, full source corpus, task metadata, query and system instruction. Neither receives curated answer facts or planner snapshots. Both admit up to twenty-four ranked candidates plus four recent candidates into the same quoted JSON envelope, including ranking scores. The envelope uses a 1,200 estimated-unit budget with 200 reserved, leaving a 1,000-unit input allowance. Source atoms remain complete. Questions and expected citation labels are stored separately from the retrievable corpus.

The native condition uses CMP preview and lexical search. The baseline uses SQLite FTS5 BM25 with task filtering and the same recency tie break and lexical query normalization. Both retain original evidence and citation identifiers. A task-return probe interleaves tasks but remains a preview read; it is not treated as a persistence test. Persistence and planner restoration are checked separately.

Required-source coverage asks whether every labeled source is present in packed context. Reciprocal rank uses the first required source in the top twenty-four lexical candidates. Revised-fact coverage requires the revised source; it does not establish whether a model chooses the newer value if both versions appear. No reader was called in the local experiment.

## Observed results

| Diagnostic | CMP native | SQLite/BM25 + task filter |
| --- | ---: | ---: |
| Cases | 40 | 40 |
| Required source present in context | 32/40 | 32/40 |
| Mean reciprocal rank | 0.80 | 0.80 |
| Estimated input allowance respected | 40/40 | 40/40 |
| Packed citation/content integrity | 40/40 | 40/40 |
| Task isolation | 40/40 | 40/40 |
| Unanchored paraphrase sources recovered | 0/8 | 0/8 |

All eight separate CMP recovery checks passed: completed-request replay, saved state after task switching, exact pending context after process termination, state after reopen, generation increment, uncertain-tool blocking, confirmed-tool result replay and stale-generation rejection. The baseline has no execution journal; these checks are reported as not applicable for it and are not scored as baseline failures.

The unanchored question asks who keeps a password, while its source describes the custodian of an access credential. Neither lexical implementation bridges that vocabulary gap. This motivates evaluating a semantic retrieval option against held-out queries. The present result supplies no evidence that adding a dependency graph improves retrieval.

The report in `results/pilot/summary.json` records versions, input counts and protocol/script/binary hashes. `rows.jsonl`, `contracts.jsonl`, `corpus.jsonl`, `queries.jsonl`, `labels.jsonl` and `prompts.jsonl` retain the observations and exact packages. Runtime latency is omitted because implementation and builds ran concurrently on the same machine. Eight templated workflows do not justify significance tests or claims about general agent behavior.

## Live evaluation handoff

`examples/research_agent.py` and the installed `cmpath-agent` command are executable integrations. They require a supplied endpoint/model and a chosen document workspace. They are not mocks. Automated tests intentionally substitute local protocol fixtures, and their fixture dispatch counts are not inference counts.

The optional evaluator `--live` mode uses the same provider dispatch code for eighty fixed-reader calls over the saved condition prompts. Both conditions receive the same endpoint, model, temperature zero, no tools, a 200-token output cap and a 1,800 estimated-unit full-request allowance. This outer allowance includes provider request fields in addition to the original packed context. First condition alternates by case. Exact request JSON and received raw response JSON are recorded; provider-reported usage is kept separate from local estimates. Labels enter scoring only after dispatch. Scoring checks citation strings and explicitly leaves semantic answer correctness unmeasured.

No automatic live calls occur. An existing live-output directory is rejected to avoid inadvertently repeating a partially completed paid experiment. `--live-cases` can restrict the first run to a declared subset. The full autonomous research-agent comparison on representative real documents remains a subsequent experiment requiring a model and human or task-specific answer judgments.

A bounded `LocalTokenCounter` adapter can invoke an explicitly supplied local tokenizer command. It passes the entire request via stdin, never invokes a shell, validates the returned integer and stops dispatch on error or timeout. Default counting remains an estimate. A tokenizer and chat template matching the selected deployment must be supplied and validated; no provider-specific token-accuracy claim is made in this release.

## Maintenance and compatibility

The base task/evidence schema stays at version 1. Opening a supported older journal with the new engine adds response/tombstone tables and upgrades the journal metadata to version 2. Tests migrate an actual pending database created by the previous native executable. The older executable refuses a new connection to schema 2. A database trigger also prevents an already-open older process from reinserting a retired request ID. Applications should still stop older workers during an upgrade and keep a complete SQLite backup.

JSONL export includes base records, execution journals, confirmed model responses and retired-ID tombstones in one coherent transaction. It streams database rows, preserves JSON-valued text exactly and publishes a new file without overwrite. It is an audit/interchange export, not an importable recovery format. SQLite backup remains the supported resumable backup.

Retention first returns counts and a hash of eligible journal contents. Apply recomputes the plan in its transaction and rejects changed candidates. It protects pending turns and terminal turns with unresolved tools, retains source evidence, and creates permanent minimal tombstones before deleting execution details. Thus journal retirement cannot make an old logical request executable again. Tombstones and source history still grow; retirement does not imply physical file shrinkage or secure erasure. Large exports and retention operations serialize this engine connection and should be scheduled accordingly.

## Validation and release status

The full release records 77 Python tests and 22 Go tests exercised with the race detector. The Python suite includes local HTTP fixtures, raw response recovery, old/new engine interaction, command-counter failures and the prior storage suite. Clean-install validation separately exercises all installed entry points, real file tools, replay after input modification, export/retention, SQLite backup, Python source rebuild and vendored native rebuild.

See `results/native/python_tests_a2.txt`, `results/native/go_tests_a2.txt` and `results/release_validation.json` for actual commands/results. Linux x86-64 is the validated deployment environment. Other operating systems, production load, actual provider counting and live answer quality remain unvalidated. The archive includes working source and Linux executables; it has not been published to a repository or package index.

The prior `NATIVE_ARCHITECTURE.md` and native timing report describe 0.4.0a1. Their exact measured executables are preserved under `native/bin/measured-0.4.0a1/`. The public-data paper, PDF and workbook remain the 0.3 Python baseline. They must not be presented as new pilot or live-model results.
