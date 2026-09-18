# CMP 0.4.0a6 consolidation checkpoint

The source version is now 0.4.0a6. The a4 and a5 validation evidence and
hashes described below are retained as historical evidence; build
target-specific native binaries and current a6 distributions before calling the
release validated. The a6 change set is the autosave subsystem, the
`check()`/`busy_timeout` memory fixes, and the native hardening (type-faithful
row decoding, coded `busy` and `integrity` errors, converging retention apply,
and the `NOT NULL` `cmp_turns.request_id` migration). The 13 September live
SumoPod run described below remains the last live evaluation; no live model run
was made for a6.

Continuation on 13 September 2026: configured the bounded live runner for
`https://ai.sumopod.com/v1/chat/completions` and `glm-5.3-flash`. The WSL suite
passed 93/93 tests. Live evaluation received all 80 fixed-reader responses and
11/11 responses in the separately completed agent stage. Manual review found
40/40 appropriate answers or abstentions in each retrieval condition, with the
required exact citation in all 32 answerable cases per condition. Both live
agent answers were correct and supported; committed replay and controlled
pause/resume checks passed. Run databases and journal exports must live on a
filesystem supporting hard links; atomic journal export returned `storage_error`
on the Windows-mounted H: drive and passed on WSL's native filesystem. See
`LIVE_RESULTS.md` and `results/live/sumopod-*20260913/`.

The current release adds two bounded safety controls on top of 0.4.0a3:
scoped replacement snapshots require immutable `supports` provenance edges,
and native/Python session tool helpers request a short-lived `action_eligible`
certificate immediately before the external callback. Missing, deleted,
out-of-scope or retracted sources fail closed. The lease narrows the effect race
but cannot make an arbitrary remote API atomic.

Validation on 12 September 2026: 93 Python integration tests passed; the Go
engine race suite passed; release validation passed for isolated wheel install,
source rebuild, native bridge, embedded real-tool replay, export/retention and
backup interoperability. The prior 0.4.0a4 hashes are in `SHA256SUMS` and
`results/release_validation.json`. No model call was made.

The prior live-validation and pilot notes below are retained as historical
evidence and are not claims about the current release.

The integrated alpha remains implemented: a Go engine, embedded Go lifecycle hooks, Python persistent-native harness, research-agent tool loop, durable response/tool recovery, journal export/retention and a matched lexical baseline diagnostic. Runtime source and the native binary are unchanged in this supplement.

On 11 September 2026 the user supplied an endpoint, credential and requested model baseten/zai-org/GLM-5.3-Flash. Base URL https://omniroute.kotak.web.id/v1 was normalized to /v1/chat/completions. A direct authenticated POST failed DNS. The runtime's configured proxy returned HTTP 403 with body "error code: 1010" for the authenticated POST. Two unauthenticated HEAD diagnostics also returned 403. No completion JSON or usage was received. Do not claim that the model, tool loop or answer quality was evaluated live. The endpoint owner must admit this API client or provide an accessible endpoint. Do not attempt to bypass the denial.

The API key is not in the project or saved artifacts. Future execution must obtain it from the user's authorized environment, never from source or journal records.

Added scripts/run_live_validation.py and research/LIVE_PROTOCOL.md. The runner gates on a compatibility probe; fixed stage runs all 40 paired cases / 80 requests; agent stage runs two document tasks plus native-process reopen/replay and controlled saved-response/tool recovery. Complete run: at most 97 HTTP attempts. Exact request/response JSON, usage, hashes and tool execution traces are recorded separately. No retries, no overwritten output directories, TLS verification, no redirects. Optional --use-env-proxy selects the existing configured proxy explicitly. The original evaluate_pilot.py and local protocol hashes remain unchanged; new citation matching fixes substring-ID ambiguity. Semantic answer and citation-support review still follow a successful future live run.

Validation: 82 Python tests passed in results/live/local_runner_tests.txt, including five new local HTTP/native fixtures. Prior 22 Go race tests and clean-install/source-rebuild checks remain historical; no native rerun was needed. Do not mistake local fixtures for models. Original wheel/sdist retain their original hashes; the new research runner and reports are provided in the complete archive.

Original local measurement is unchanged: 240 synthetic messages, 8 workflows, 40 paired cases, 80 observation rows. Both CMP and task-filtered SQLite/BM25 recovered required sources in 32/40 and missed all 8 unanchored paraphrases. Budget, citation-integrity and task-isolation checks: 40/40 each. CMP-only recovery contracts: 8/8; baseline journaling: N/A. No retrieval superiority, live quality or comparison against named memory vendors is established.

Read LIVE_RESULTS.md for current status and command, PILOT_RESULTS.md for the historical study, and docs/ for product APIs. results/live/probe/ contains real failed-access evidence. No publication, deployment or package-index upload was performed.

Latest continuation: at 23:13 UTC on 11 September 2026, ran the new validation runner with stage all, explicit environment proxy and a 60-second timeout. It stopped at the first probe: HTTP 403, Cloudflare 1010, about 17.093 seconds, one dispatch and zero completion objects. No fixed-reader or agent requests followed. Evidence is in results/live/provider-run-20260911/. No runtime or test changes were made; the 82-test result remains the prior validated suite. Live testing still requires endpoint access.
