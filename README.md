# Context Memory Path

Durable task memory and recoverable execution hooks for agent harnesses.

[GitHub](https://github.com/dhava-gautama/cmpath) ·
[Python API](docs/API.md) · [Harness guide](docs/HARNESS.md) ·
[Release guide](docs/RELEASE.md)

**0.4.0a5 — integration and release-integrity consolidation.** This alpha
builds on 0.4.0a4's scope-validated provenance and short-lived pre-effect
eligibility certificate, and consolidates the MCP adapter, authenticated
control-plane API, dependency-free SDKs, offline conformance suite, portable
Kimi/Hermes bundle, local doctor diagnostics, and the bounded hybrid memory
router. Read
[BREAKTHROUGH_REPORT.md](BREAKTHROUGH_REPORT.md) for the bounded safety result;
it remains explicit about residual TOCTOU limits.

**Live validation update:** SumoPod `glm-5.3-flash` passed the compatibility gate,
the complete 80-response paired fixed-reader evaluation, and a separate live
document-agent recovery stage. Both retrieval conditions answered or abstained
appropriately on all 40 clustered synthetic cases; committed replay and
controlled pause/resume checks passed. Read [LIVE_RESULTS.md](LIVE_RESULTS.md)
for limitations and evidence. The earlier Cloudflare-blocked endpoint result is
retained as historical evidence.

## Install

Python 3.10+ with SQLite FTS5. The runtime has no external Python dependencies. Native executables are validated on Linux x86-64 with glibc 2.39 and Windows AMD64 with MSYS2 UCRT64 GCC. Document tools preserve root confinement on both platforms, including Windows reparse-point rejection. Rebuild native code for other targets; they have not been validated.

```bash
git clone https://github.com/dhava-gautama/cmpath.git
cd cmpath
python -m pip install -e .
cmpath-agent --help
cmpath-maintain --help
python examples/native_workflow.py --binary native/bin/cmpath-native --file README.md --db checksum.db --request-id readme-1
```

For an offline release artifact, replace the editable install with
`python -m pip install --no-index --no-deps dist/cmpath-0.4.0a5-py3-none-any.whl`
after building or downloading that wheel. This project is not currently
published on PyPI.

## Lean hybrid memory routing

`MemoryRouter` keeps ordinary turns small and retrieves deeper CMP evidence
only when needed. It supports `none`, `pinned`, `task`, `lineage`, and `deep`
routes. Pinned state is bounded, retrieved memory remains quoted data, and task
ambiguity fails closed rather than selecting a candidate. `RoutedHarness`
combines the router with the durable model/tool lifecycle journal.

```python
from cmpath import MemoryRouter, NativeHarness, RoutedHarness, TaskMemory

memory = TaskMemory("cmpath.db")
router = MemoryRouter(memory)
router.pin(1)
context = router.retrieve("continue the release work", task_id=1, requested_route="task")
print(context.citations, context.used_units, context.as_messages())
```

The equivalent read-only CLI is:

```bash
cmpath --db cmpath.db route "continue the release work" --task 1 --route task --budget 500
```

See [docs/API.md](docs/API.md) for routing and budget contracts and
[docs/HARNESS.md](docs/HARNESS.md) for checkpointed model/tool execution.

The checksum example executes a real local tool and records its result. Repeating the same logical request returns the recorded reply. Use a new ID when you intend a new execution. The wheel contains Python code; the complete archive also contains the native executables and vendored source.

## Use inside a harness

| Host | Available integration |
| --- | --- |
| Go executor | Import `cmpath.local/native/engine`; call lifecycle hooks directly |
| Python executor | `NativeHarness` or its low-level session methods |
| Token-lean Python host | `MemoryRouter` plus `RoutedHarness` |
| Local document research agent | Installed `cmpath-agent` command with your configured model endpoint |
| Codex, Kimi Code, Cursor, Hermes, MCP clients | `cmpath-mcp` stdio server with 10 source-preserving tools |
| Managed host control plane | `cmpath-control` authenticated HTTP lifecycle API |
| Python application needing storage only | Existing `TaskMemory` API |

Install the portable adapter with the `integrations` extra or run it from this
checkout with `scripts/run_mcp_wsl.sh`. Project configurations are included for
Kimi Code and Cursor; Codex uses the personal `cmpath-memory` plugin. Hermes and
generic MCP templates are documented in [integrations/README.md](integrations/README.md).
Hosts that own the model and tool loop can use the dependency-free clients in
`integrations/sdk/` and the five-hook guides in `integrations/adapters/`.

The host retains task selection, model dispatch, planner state and tool authorization. `Begin` saves the exact initial context and user input. Each model request and confirmed response can be journaled. Tool intent/result records allow controlled recovery. `Commit` atomically stores the response, snapshot and explicit facts. Task revisions and worker generations reject stale updates.

A pending request is inspected and explicitly recovered. Unknown model/tool outcomes are never automatically retried. A retired request ID remains blocked by a permanent tombstone. These are local persistence contracts, not an exactly-once guarantee for arbitrary external effects.

## Run the research agent

Set `CMP_MODEL_ENDPOINT` to your complete Chat Completions URL and `CMP_MODEL_NAME` to its model identifier. Configure `CMP_API_KEY` if the endpoint needs authentication. The command below uses this archive's documentation as its read-only research workspace:

```bash
cmpath-agent --binary native/bin/cmpath-native --db research.db --create --new-task "CMP release research" --workspace docs --endpoint "$CMP_MODEL_ENDPOINT" --model "$CMP_MODEL_NAME" --request-id research-1 --question "How does recovery handle an interrupted tool? Cite the source document."
```

This command makes actual model requests once configured. Inspect a saved run without provider configuration:

```bash
cmpath-agent --binary native/bin/cmpath-native --db research.db --request-id research-1 --inspect
```

See [docs/AGENT_PILOT.md](docs/AGENT_PILOT.md) for resume, read-only tools, exact provider transcripts and custom complete-request counters. Default counts are estimates, not model tokens.

## Maintenance

```bash
cmpath-maintain --binary native/bin/cmpath-native --db checksum.db info
cmpath-maintain --binary native/bin/cmpath-native --db checksum.db export checksum-journal.jsonl
cmpath-maintain --binary native/bin/cmpath-native --db checksum.db plan --before 2026-09-01T00:00:00Z
```

The plan is read-only. The explicit `apply` command requires its exact cutoff and plan hash. Retention removes eligible terminal journals while preserving evidence and retired-ID protection. JSONL export is for inspection; use a complete SQLite backup for recovery. [Retention contracts](docs/RETENTION.md).

## What the comparison found

Forty synthetic cases over 240 source messages produced eighty condition observations. CMP and a simple SQLite/BM25 baseline each recovered the required source in **32/40 cases**. Both respected the same estimated input allowance, preserved selected evidence and maintained task isolation in **40/40 cases**. Both missed all eight paraphrases without matching vocabulary. Eight separate CMP recovery contracts passed; the baseline has no journal and was not scored on those contracts.

This shows parity on a small diagnostic workload, not an agent-quality
advantage. No LLM was called in that local study. [Protocol](research/PILOT_PROTOCOL.md)
and [raw results](results/pilot/summary.json) are included. The separate
13 September live run is reported in [LIVE_RESULTS.md](LIVE_RESULTS.md);
its provider responses and recovery checks must not be conflated with this
historical local comparison.

The current Windows integration run passes 139 Python tests with four
intentional platform/optional-dependency skips, plus the Go race-tested suite
and all 17 isolated release checks. Run the commands below for the current
checkout rather than treating historical result logs as current counts.
Earlier native timings remain in `results/native/benchmark.json`: Go showed no
search-speed advantage in that run. The earlier public-data paper and workbook
are the 0.3 Python baseline. Historical results are not relabeled as new native
or live-model results.

## Build and reproduce

```bash
python scripts/build_native.py
CMP_NATIVE_BINARY="$PWD/native/bin/cmpath-native" python -m unittest discover -s tests -v
python scripts/build_release.py
python scripts/verify_artifacts.py
python scripts/validate_release.py
python scripts/evaluate_pilot.py --output results/pilot-reproduction
```

Native builds need Go 1.26+ and a GCC-compatible C compiler (MinGW-w64 or LLVM on Windows; MSVC `cl.exe` is not supported by Go cgo). SQLite 3.53.4 and `golang.org/x/text` 0.42.0 are vendored; the build disables dependency/toolchain downloads and can select a compatible Go executable already cached locally. Use `python scripts/build_native.py --check-only` to validate tools, or pass `--goos`, `--goarch` and `--cc` for an explicit target. Run `go test -buildvcs=false -race -count=1 ./...` from `native/`. For source development set `PYTHONPATH=src` or install the wheel first.

The read-only verify_artifacts.py command compares the current-version wheel,
source distribution and portable adapter bundle with canonical source,
including wheel RECORD hashes. To verify a Codex plugin checkout and its cache,
add --plugin-root PATH --plugin-cache PATH; a plugin ZIP can be supplied with
--plugin-artifact PATH. Stale artifacts are rejected instead of silently
accepted from an older source snapshot.

| Document | Purpose |
| --- | --- |
| [PILOT_RESULTS.md](PILOT_RESULTS.md) | Current integrated release and local study |
| [LIVE_RESULTS.md](LIVE_RESULTS.md) | Live access evidence and runnable validation supplement |
| [docs/AGENT_PILOT.md](docs/AGENT_PILOT.md) | Model/tool loop, configuration and recovery |
| [docs/HARNESS.md](docs/HARNESS.md) | Native lifecycle APIs |
| [docs/RETENTION.md](docs/RETENTION.md) | Export, reviewed plans and tombstones |
| [native/README.md](native/README.md) | Go module and build constraints |
| [docs/API.md](docs/API.md) | Existing Python storage API |
| [docs/RELEASE.md](docs/RELEASE.md) | Install, validate and publication handoff |

Use one database per application isolation domain. No Rust backend, hosted
service, production-load validation or package-index publication is included;
the managed control plane's authentication is a caller-supplied bearer token,
not a hosted identity service. Original code is MIT licensed; [NOTICE.md](NOTICE.md)
preserves provenance and third-party terms.
