# Frozen evidence freshness protocol v1

Protocol identifier: `freshness-v1`. Frozen scenario names, schedule phases, semantic labels and categories are declared in `scripts/evaluate_freshness.py:SCENARIOS` before execution of an experimental arm. This file and that executable manifest are hashed into each result. The protocol was authored without running the new scope policy. Implementation debugging after the freeze must be disclosed; labels must not be changed to improve measured outcomes.

## Question and bounded claim

Does native scope freshness validation prevent new local tool intents and final commits based on evidence invalidated after context preparation, while permitting useful unaffected actions? Does it differ from the actual preserved a2 implementation and a conservative global-change observer under the same supplied context?

This is a deterministic protocol experiment. It measures neither LLM intelligence nor semantic inference accuracy. A local scripted writer establishes ground truth; a local callback appends to an action ledger. No provider, network endpoint, model, or external effect is involved. Schedules enumerate concrete orderings; they are not a probabilistic workload or exhaustive model check. Report counts with denominators, without significance tests or aggregate LLM scores.

## Conditions

| Arm | Execution | New action/commit policy |
|---|---|---|
| `baseline_a2` | Actual preserved `native/bin/baseline-0.4.0a2/cmpath-native` supplied by `--baseline-binary` | Original default snapshot/revision behavior; no simulated baseline flags in the new binary. |
| `scope` | Candidate supplied by `--binary` | Native `consistency="scope"` in `Begin`; native checks guard new tool intents and pending commits. |
| `global_change` | Candidate with default snapshot behavior | Read-only observer of `cmp_scope_epochs(task_id=0).epoch`; reject if it differs from the post-Begin basis. |
| `always_block` | Candidate with default snapshot behavior | Refuse every benchmark's new target action and pending final commit. Historical replay and preparatory observation are allowed. |

The global observer is deliberately a host policy using the same retrieval context, not a second retrieval strategy. It establishes its baseline after the known Begin self-write. A known completed preparatory observation advances its basis by the measured own-write delta; there is no interleaved foreign mutation in that stage. Its observer/read followed by native intent is not an atomic transaction, so this arm is not claimed to provide a stronger concurrency contract than the native scope implementation.

Each `(scenario, arm, lane)` gets a fresh temporary database. The action and commit lanes have separate fixtures, preventing a pending tool intent from making a commit fail for an unrelated reason. Every arm receives identical task IDs, titles, source content, facts, query, scope, limits and packed model messages. The evaluator asserts exact equality of `session.messages` across all four arms for each scenario/lane and writes a shared context artifact. Certificate fields outside model messages may differ. The setup uses the existing Python `TaskMemory` writer and canonical SQL mutations, exercising cross-language schema compatibility and database triggers rather than only native mutation methods.

## Frozen scenarios and semantic oracle

Approval denotes permission to perform the single local target action. Any revoked prerequisite prohibits that action. Deleting the only selected approval fact or its supporting source makes authorization unsupported, which also prohibits execution. Absence of `stopship` in the explicitly declared scope authorizes the synthetic action; inserting a matching blocker prohibits it. These are benchmark domain rules, not a claim that arbitrary FTS absence establishes real-world safety.

| Schedule | Mutation relative to Begin/admission | Semantic action | Category |
|---|---|---|---|
| selected_fact_retraction | Retract selected fact, without changing task revision, before admission | Deny | Supported stale evidence |
| parent_evidence_correction | Append parent revocation before child admission | Deny | Supported stale evidence |
| negative_task_phantom | Empty task query becomes nonempty before admission | Deny | Supported phantom |
| negative_all_new_task | Empty all-scope query gains blocker in a newly created task | Deny | Supported membership/phantom |
| ancestry_addition | Add a revoked prerequisite after child Begin | Deny | Supported scope membership change |
| selected_snapshot_revision | Legacy API updates selected snapshot and revision | Deny | Existing commit fence, new action fence |
| source_deletion | Delete selected source; FK cascades its fact | Deny | Supported evidence disappearance |
| fact_deletion | Delete the selected approval fact | Deny | Supported fact disappearance |
| fact_aba | Selected approval true→false→true in committed history | Allow | Conservative contract mismatch; final domain state permits action |
| unchanged | No intervening write | Allow | Positive control |
| outside_scope_write | Append an unrelated task's administrative message | Allow | Positive control; global observer is expected to overblock |
| same_scope_admin | Add a harmless alias to selected task | Allow | Conservative contract mismatch |
| own_tool_output | Finish a preparatory local observation after Begin | Allow | Positive control; own writes must not self-invalidate |
| after_admission | Revoke after successful target intent, before callback entry | Deny at effect time | TOCTOU limit; not stale at admission |
| during_tool | Callback enters, then writer revokes before ledger append | Deny at effect time | TOCTOU limit; not stale at admission |
| prior_derived_snapshot | Commit child derivation, revoke parent, then Begin a new child turn | Deny | Known unsupported semantic persistence |
| parent_fact_retraction | Retract parent fact without changing parent snapshot revision | Deny | Supported cross-task fact change |
| parent_snapshot_revision | Legacy API changes parent snapshot/revision after child Begin | Deny | Supported cross-task state change |
| terminal_replay | Commit original response/effect, revoke, replay logical request | Return historical result; no new effect | Historical control, excluded from fresh-action/commit metrics |

In the independent commit lane, the `after_admission` and `during_tool` mutations occur immediately before commit, because no tool executes in that lane. Their action-lane timelines demonstrate the external-execution gap, while their commit lanes test that the now-visible correction prevents a new local final commit. Do not count those action cases as successful pre-admission blocking opportunities: their evidence was current when the intent was admitted.

The prior-derived-snapshot fixture really performs a prior commit from a lineage context containing parent approval. The parent is corrected before the evaluated Begin. The new certificate legitimately captures the new epochs; it does not know that the mandatory snapshot derived from old evidence remains semantically stale. This case remains unsupported rather than being removed or scored as fresh merely because its current revision matches.

Terminal replay is a read of an already committed logical result. The fixture executes its original effect once, then asserts Begin and Commit replay their saved result after correction. It does not try to start a new tool on a terminal turn. A new logical request has separate semantics.

## Observations and grading

The grader does not branch on an arm name. It reads frozen semantic labels and observed admission, callback ledger, and commit outcomes. Every raw row includes ordered event timeline, local ledger, error code, category, context hash, and wall-clock fixture duration.

Separate metrics, each with an explicit denominator:

- **Pre-admission stale intents:** new target intents admitted when the scheduled domain state was already invalid before admission, divided by such action schedules. This is distinct from whether a later effect happened.
- **Unsafe actions:** ledger actions performed when prohibited, divided by semantically prohibited action schedules. Category breakdown separates supported cases, TOCTOU, and unsupported persistent derivation.
- **Stale commits:** accepted new replies authorizing the prohibited action, divided by semantically prohibited commit schedules.
- **Useful allowed actions:** effects performed in semantically permitted schedules, divided by permitted action schedules.
- **False blocks:** permitted actions not performed, divided by permitted action schedules. ABA and harmless metadata cases are marked conservative contract mismatches and reported separately from unsafe acceptance.
- **Safe commits:** accepted replies in permitted commit schedules, divided by those schedules.

There is no combined “best” score. Always-block must show zero new effects and strictly positive false blocks. Baseline and scope must perform the unchanged control; scope must also permit own observation output and outside-scope writes. Every arm must preserve historical replay. The current scope mechanism must visibly admit the after-admission race and prior-derived case; these are explicit checks of the documented limitations, not favorable safety outcomes. `summary.json` carries all control checks. A failed control yields process exit 1 after results are saved; it cannot be hidden by a low unsafe-action count.

Metric summaries are emitted both for all cases and for each category. The headline safety comparison should use `supported_stale`; disclose `toctou_limit` and `unsupported_semantic` alongside it. Do not silently omit their raw results. Conservative mismatches measure the cost of a write-sensitive contract, not failures of the declared epoch mechanism.

## Execution and artifacts

After candidate build, run from project root:

```sh
python scripts/evaluate_freshness.py --binary native/bin/cmpath-native --baseline-binary native/bin/baseline-0.4.0a2/cmpath-native --output results/freshness-v1
```

The CLI refuses an existing output path, missing binaries, or byte-identical baseline/candidate binaries. It writes `metadata.json` before executing cases, then streams `raw.jsonl`, and finishes `contexts.json` and `summary.json`. Interrupted runs retain partial raw results and metadata rather than overwriting them on retry. There are 19 schedules × 4 arms × 2 lanes = 152 raw rows.

Metadata includes protocol manifest and hash, evaluator hash, runtime Go/SQL source hashes, legacy writer and bridge hashes, executable paths and SHA-256 hashes, Python version and platform. Per-row elapsed seconds measure local full-fixture wall time, including process start, temporary database creation and scripted operations. They are not isolated validation cost, model latency, or a throughput estimate. Single-run timing differences support debugging only.

## Exclusions and limitations

This protocol does not establish semantic contradiction discovery, arbitrary dependency completeness, knowledge truth, remote exactly-once execution, expiry without a write, linearizability of the host observer, or statistical performance generalization. It has no concurrent thread scheduler: deterministic cross-connection interleavings provide repeatable adversarial orderings. More schedules or a model checker would be needed for exhaustive concurrency claims. Model-request checkpoint fencing is implemented by the runtime but is not independently measured by these tool/commit lanes. Recovery and existing pending-tool replay need separate lifecycle tests; terminal replay is covered here.
