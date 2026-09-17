# Scope-Consistent Agent Memory

## Executive result

CMP 0.4.0a4 adds an opt-in scope-freshness certificate plus provenance and
pre-effect eligibility controls for durable agent turns. The underlying a3
certificate records a monotonic epoch for every task in a declared task or
lineage scope, plus a global epoch for all-scope reads. Before a new model
request, new tool intent, or pending commit, it verifies that every scoped epoch
is unchanged. SQLite triggers advance affected clocks for inserts, updates,
deletions, fact revisions, dependencies and aliases, including writes made by
the legacy Python API.

For scoped replacement snapshots, a4 additionally requires immutable evidence
IDs in `Reply.provenance`. CMP validates source existence, scope admissibility
and retraction in the same transaction as commit. Native and Python session
helpers also request a short-lived `action_eligible` certificate immediately
before invoking a host tool callback.

This is not a claim that CMP invented transactions, provenance, temporal memory,
or semantic contradiction resolution. Optimistic validation is established
database machinery. The contribution is a small source-preserving,
cross-language certificate integrated at model/tool/commit boundaries, paired
with an adversarial suite that reports unsafe actions and false blocks separately.
The default snapshot mode remains backward compatible.

## Failure addressed

The prior release fenced task snapshot revisions and worker generations, but a
fact revision or parent evidence append could occur without changing the selected
task revision. A stale turn could then admit a tool or commit using obsolete
evidence. An audit reproduced five failures: selected-fact retraction, parent
correction, empty-result phantoms, lineage changes, and stale derived snapshots.

The certificates are deliberately conservative. They invalidate any scoped
data change, even if a value returns to its previous state or only an alias
changes, and reject derived writes whose declared sources are no longer valid.
The action lease narrows the pre-effect race, but does not make an arbitrary
remote effect atomic or repair a remote system after a race.

## a4 validation

The new provenance and eligibility controls are covered by native race tests and
Python bridge tests. The complete Python integration suite passes 93 tests; the
Go engine suite passes with `-race`; and the release validator passes isolated
wheel installation, source rebuild, native bridge, embedded real-tool replay,
backup/export/retention and migration checks. No model or network call is used
in these checks.

## Frozen experiment

`research/FRESHNESS_PROTOCOL.md` and `scripts/evaluate_freshness.py` define 19
deterministic schedules, four arms, and two lanes per schedule (152 rows):

| Arm | Policy |
|---|---|
| baseline_a2 | Preserved 0.4.0a2 binary |
| scope | 0.4.0a3 `consistency=scope` |
| global_change | Host observer rejecting any global epoch change |
| always_block | Negative control refusing every new action/commit |

Schedules cover fact retraction without task revision, parent/dependency changes,
absent-to-present blockers, source/fact deletion, ABA restoration, unchanged and
outside-scope controls, own tool output, post-admission/during-tool races, stale
derived snapshots and historical replay. Context messages are asserted identical
across arms; no model, network call or external effect is used.

## Results

On 10 supported-stale schedules, the old arm admitted and executed a prohibited
new intent in 10/10 cases and accepted a stale commit in 9/10. The scope arm
recorded 0/10 stale intents, 0/10 unsafe actions and 0/10 stale commits.

The scope arm retained all positive-control actions (3/3) and commits (3/3),
including an outside-scope write and its own completed observation output. Its
conservative cost appeared in two labeled mismatch schedules: ABA restoration
and harmless same-scope metadata caused 2/2 false blocks. The global observer
blocked 1/3 positive controls. Always-block had zero new effects but 5/5 false
blocks, so it cannot win by refusing useful work.

These frozen results are the a3 scope-arm measurements. The a4 focused checks
then rejected the missing-provenance write (1/1), rejected the retracted-source
write (1/1), accepted the valid provenance write (1/1), and invalidated a second
eligibility request after a same-task change (1/1). The action lease still does
not eliminate a race that occurs after the lease and before a non-participating
remote effect; that residual TOCTOU rate remains an explicit measurement target.

## Cost

A 15-repetition serial microbenchmark measured the Python-to-Go lifecycle on
Linux x86-64, Python 3.12.14, with 100 messages per scoped task. Median total
round-trip was 2.214 ms for a2, 2.238 ms for a3 snapshot, and 2.145 ms for a3
scope at one task. At 16 tasks, the medians were 2.366, 2.352 and 2.813 ms.
These are local bridge timings, not model latency, throughput or production-load
estimates; scope size and corpus size grew together.

## Research position

LongMemEval separates retrieval from generated-answer evaluation and includes
updates and abstention. LoCoMo-Plus studies semantically disconnected triggers,
close to CMP’s earlier unanchored-paraphrase failure. A 2026 evaluation reports
that no memory architecture dominates all workloads and that dynamic updates,
long-horizon stability and operational cost trade off ([MemoryData evaluation](https://arxiv.org/html/2606.24775v1)).

The closest overlap is transactional agent-memory work. MemTX describes staged
belief commits, provenance, action gating and typed cascade repair, and explicitly
identifies source-scope gaps ([MemTX](https://arxiv.org/html/2607.23929v1)).
ACID-Agent frames semantic atomicity, consistency, isolation and durability for
long-horizon agent transactions ([Agentic Transaction](https://arxiv.org/html/2608.13900v1)).
STALE reports that frontier systems struggle with outdated memories and implicit
conflicts ([STALE](https://arxiv.org/html/2605.06527v1)). CMP therefore claims an
implemented, reproducible scope-certificate experiment, not priority over these
concepts or broader systems.

## Publishable claim

For changes visible before admission in the declared scope, this implementation
prevented all 10/10 stale intents and 10/10 unsafe actions in the frozen suite
while preserving 3/3 positive-control actions. The publishable unit is the
combination of source-preserving monotonic scope epochs, explicit provenance
validation for derived state, short-lived pre-effect eligibility, own-write
handling that preserves intervening changes, and a falsification suite with
positive and always-block controls plus explicit residual TOCTOU measurement.
Larger independent workloads are required before generalization.

## Sources

1. Wu et al., “LongMemEval,” 2024, https://arxiv.org/html/2410.10813v1.
2. Maharana et al., “LoCoMo,” 2024, https://arxiv.org/abs/2402.17753.
3. Zhang et al., “A-Mem,” 2025, https://arxiv.org/html/2502.12110v1.
4. Rasmussen et al., “Zep,” 2025, https://arxiv.org/html/2501.13956v1.
5. LoCoMo-Plus, 2026, https://arxiv.org/abs/2602.10715.
6. MemoryData evaluation, 2026, https://arxiv.org/html/2606.24775v1.
7. Li et al., “MemTX,” 2026, https://arxiv.org/html/2607.23929v1.
8. Sun et al., “Agentic Transaction,” 2026, https://arxiv.org/html/2608.13900v1.
9. Chao et al., “STALE,” 2026, https://arxiv.org/html/2605.06527v1.
10. Kung and Robinson, “On Optimistic Methods for Concurrency Control,” VLDB, 1979, https://www.vldb.org/dblp/db/conf/vldb/KungR79.html.
