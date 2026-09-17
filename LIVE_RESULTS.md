# CMP live validation

## SumoPod GLM-5.3-Flash run (13 September 2026)

The direct compatibility gate at `https://ai.sumopod.com/v1/chat/completions`
passed with model `glm-5.3-flash`. The complete matched fixed-reader stage then
received 80/80 provider responses across 40 paired synthetic cases. Manual
semantic review found the requested fact or appropriate evidence-insufficient
abstention in 40/40 CMP-native answers and 40/40 task-filtered SQLite/BM25
answers. Each condition included the required exact source citation in all 32
answerable cases. One SQLite/BM25 abstention was truncated after already stating
the supported insufficiency conclusion; CMP-native had no truncated answers.

The live document-agent stage completed separately on WSL's native filesystem:
11/11 responses including its gate, two correct citation-supported answers, two
committed replays with identical replies and zero new dispatches, and a
controlled pause/resume with a higher worker generation and no re-execution of
completed tools. The copied document workspace was unchanged.

An attempted combined run on the Windows-mounted project filesystem completed
the fixed-reader stage and the first agent task, then failed at atomic journal
export with `storage_error`. CMP intentionally requires hard-link publication
for journal exports; the same agent stage completed when its database and export
were placed on WSL's native filesystem. A preceding transient HTTP 502 attempt
is retained separately and was never overwritten or automatically retried.

These are eight templated synthetic workflows and two local-document tasks, not
independent users or a general model-quality benchmark. The two retrieval
conditions tied on this dataset; no retrieval superiority is claimed. Provider
cost remains unknown and must be checked against the provider invoice.

Evidence is under `results/live/sumopod-full-retry-20260913/` and
`results/live/sumopod-agent-20260913/`. The historical blocked-endpoint report
follows.

## Historical endpoint-access result

11 September 2026. Supplement to CMP 0.4.0a2.

The supplied model endpoint was tested. **No model completion was received.**
The authenticated request through this runtime's proxy returned HTTP 403 with
Cloudflare error 1010. The fixed-reader comparison and autonomous document-agent
study therefore remain unrun. This access failure provides no evidence for or
against CMP's answer quality.

## Latest retry

At **23:13 UTC on 11 September 2026**, the user's instruction to continue
triggered one new authenticated probe through the completed validation runner.
It again returned **HTTP 403 / Cloudflare 1010**, after about 17.093 seconds.
The runner stopped after one dispatch: zero completion objects were received,
and neither the 80-request comparison nor the document-agent stage started.
Exact request bytes, the error body, configuration and input hashes are saved
under `results/live/provider-run-20260911/`. This was an explicitly requested
new attempt; the runner did not retry automatically.

## Observed requests

Configuration: Chat Completions at
`https://omniroute.kotak.web.id/v1/chat/completions`, requested model
`baseten/zai-org/GLM-5.3-Flash`. The model identifier was supplied by the user;
its availability and tool support could not be verified.

| Attempt | Authentication | Result |
| --- | --- | --- |
| Direct POST through CMP's provider transport | Authorization header configured | DNS resolution failed before an HTTP response; about 0.019 seconds |
| HEAD to `/v1` through the configured runtime proxy | None | HTTP 403; diagnostic request only |
| POST to `/v1/chat/completions` through that proxy | Authorization header configured | HTTP 403, body `error code: 1010`; about 10.103 seconds |

The two original POST attempts used the same recorded request bytes: temperature 0,
`tools: []`, max_tokens 200, and the instruction to reply with `CMP_OK`.
There were two unauthenticated HEAD diagnostics during route investigation;
one is preserved as the standalone diagnostic JSON. They were not model calls.
No successful completion JSON or provider usage object was received. Billing is
unknown; elapsed failed-request time is not model inference latency.

Cloudflare documents error 1010 as a denial based on the client's browser
signature and directs the visitor to the site owner. The endpoint owner needs
to permit this authorized API client, or supply an API endpoint accessible from
this runtime, before the live study can proceed. This response does not establish
that the API key is invalid. [Cloudflare error 1010 documentation](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).

The requests retained TLS verification and refused redirects. The credential is
absent from the saved code, reports and request/response artifacts.

## Completed work

The archive now includes `scripts/run_live_validation.py` and a separate
`research/LIVE_PROTOCOL.md`. The runner provides one reproducible command for:

1. A bounded provider compatibility probe, stopping before the study on failure.
2. The full 40-case, 80-request fixed-reader comparison, with the same frozen
   prompts and generation settings for CMP and the SQLite/BM25 baseline.
3. Two document research tasks using only three copied project documents.
4. Committed replay after reopening the native process and a controlled pause
   after a saved model response and completed tool, followed by explicit resume.

It records exact request bytes before dispatch, exact UTF-8 provider JSON after
response, per-response usage, dispatch counts, document hashes, tool execution
events and journal exports. There are no automatic retries. Existing result
directories are refused. The complete run permits at most 97 HTTP dispatches;
this is a request ceiling, not a dollar cost estimate.

The new citation diagnostic matches complete IDs, correcting the historical
substring check that could confuse `T1:M2` with `T1:M20`. The original evaluator
and original observations remain unchanged with their original hashes. Citation
presence still does not prove a correct or supported answer; the new output
explicitly requires semantic review. Empty and truncated answers stay visible.

## Local verification

**82 Python tests passed**, including five new local HTTP/native integration
tests. These verified exact request and response preservation, credential
omission from traces, malformed HTTP-body preservation before JSON parsing,
redirect refusal, dispatch limits, no retry after a 403,
label separation, complete paired execution, exact citation parsing, actual
local document reads, and recovery without repeated completed tools or model
responses. The HTTP servers in those tests returned explicit fixtures: they
were not language models and did not contact the supplied endpoint.

The existing native binary and runtime code are unchanged. Their earlier
22 Go tests with the race detector, clean installation and source-rebuild checks
remain historical evidence; they were not rerun for this research supplement.
The original 0.4.0a2 wheel and source distribution are preserved byte-for-byte.
The new runner, tests, protocol and this report are in the complete ZIP.

The earlier local comparison remains **32/40 required-source coverage for each
condition**. It neither measures downstream answer correctness nor establishes
superiority over other memory products. No live score has been added to it.

## Run once access is available

From the extracted `cmpath/` directory, on a compatible Linux host with the
included native executable, supply the key interactively so it is not written
into the command or its history:

```bash
read -rsp 'API key: ' CMP_API_KEY
export CMP_API_KEY
python scripts/run_live_validation.py --stage all --output results/live-authorized-run
unset CMP_API_KEY
```

The supplied endpoint and model are defaults; `--endpoint` accepts a full
Chat Completions URL and `--model` can replace the model identifier.
Use `--stage probe` for one compatibility call, or `--stage fixed` / `--stage agent`
for a separately recorded stage. Every stage begins with the probe. Add
`--use-env-proxy` only when intentionally using the runtime's configured proxy;
the production agent's default transport remains direct.

Inspect a failed run's JSON before attempting another run. A timeout can leave
a provider outcome uncertain. If max_tokens 200 is incompatible with the model's
reasoning mode, record a protocol amendment and use identical settings for both
conditions before running a new comparison. The runner does not silently tune
generation settings or repeat potentially billable requests.

## Evidence map

| File in the archive | Contents |
| --- | --- |
| `results/live/probe/request.json` | The exact nonsecret POST payload |
| `results/live/probe/summary.json` | Direct transport DNS failure |
| `results/live/probe/network_diagnostic.json` | Unauthenticated proxy HEAD result |
| `results/live/probe/proxy_probe_summary.json` | Authenticated proxy POST failure and body |
| `results/live/local_runner_tests.txt` | Complete 82-test local validation log |
| `research/LIVE_PROTOCOL.md` | Frozen live extension and review criteria |
| `scripts/run_live_validation.py` | Reproducible provider/reader/agent runner |
| `PILOT_RESULTS.md` | Historical local comparison and product limitations |

No package index upload, public repository publication or deployment was performed.
