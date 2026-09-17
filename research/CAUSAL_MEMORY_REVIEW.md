# Evidence freshness and causal memory review

Audit date: 2026-09-12. Scope: current `native/engine` sources and `docs/HARNESS.md`; the bundled `cmpath-0.4.0a2` copy was not the audit target. No model/provider endpoint or credentials are needed for the scenarios below.

## Assessment

An evidence freshness certificate would close real correctness gaps. Existing generation fencing identifies the current worker; selected-task revision fencing identifies the selected planner snapshot. Neither establishes that the evidence used by that worker remains current. The database transaction around `Begin` provides a coherent initial read, but ends before model/tool execution. SQLite's write serialization cannot validate observations across those separate transactions.

This is an application of established optimistic concurrency and dependency-validation principles, not a new transactional-memory idea. The potentially useful contribution is a precisely scoped, durable implementation and adversarial benchmark for this harness: positive and negative observations, cross-task scopes, selective conflicts, replay, recovery, and external-action boundaries. Do not claim general semantic truth maintenance, causal completeness, or first transactional agent memory.

## Concrete source findings

Line references identify the audited source before candidate edits.

| Gap | Exact source locations | Consequence |
|---|---|---|
| Fact revisions do not fence a pending turn | `context.go:278–296` inserts a new `facts.revision`; `harness.go:209–219` compares only `tasks.revision` | A same-task correction or retraction after `Begin` can be silently overwritten or ignored at commit, even though the worker generation and task snapshot remain current. |
| Appended evidence does not fence a pending turn | `engine.go:280–315` appends messages without revising the task; `context.go:230–246` searches and reads recent messages only while packing | New contradictory or disqualifying evidence can arrive during execution without being checked at action or commit. No mutation of an existing row is necessary. |
| Lineage is retrieval scope, not causal dependency validation | `context.go:106–133` traverses up to three parent hops; `context.go:159,230` searches these tasks; `engine.go:198–204` loads parent IDs; `harness.go:162–169,213–219` checks only the selected task | A parent turn may commit while a child is pending. The child's old parent evidence remains accepted despite the parent's changed revision. Facts themselves are selected only for the selected task (`context.go:207–225`), so parent fact corrections are not automatically attached to parent source messages. |
| Negative and bounded retrieval observations are not represented | `Package`, `context.go:31–40`, stores citations but no fact-head, predicate, corpus membership, or scope-version certificate; `engine.go:353–388` performs bounded OR-term BM25 retrieval | A cited-row hash cannot detect a new matching row, a newly created task under `all`, or an empty query result becoming nonempty. Budget omissions and top-k truncation also make absence from the prompt insufficient evidence of absence from the store. |
| New tool intent is not checked against the selected snapshot | `harness.go:58–69,318–351`; `Session.Tool`, `harness.go:460–482` | `StartTool` checks status/generation only. An already stale task snapshot can authorize a callback; later commit rejection cannot undo its effect. This is an action-boundary hole beyond the existing commit fence, rather than evidence that the commit fence is broken. |
| Recovery preserves obsolete evidence | `harness.go:156–173`, `25–43` | Recovery advances generation and reloads the original package; same-task facts/messages and parent changes can leave that package stale while recovery succeeds. |
| Derived snapshots have no persistent provenance validity | `harness.go:230–243` writes facts and arbitrary planner snapshot; `context.go:163–175` makes selected snapshot mandatory; `journal.sql:4–24` has no dependency certificates | After a successful child commit, correcting a parent does not mark the child's derived snapshot stale. Even a new turn certificate will certify the current database containing that old derived snapshot unless dependency provenance survives the producing turn. |
| Observation time is not validity time | `engine.go:259–278` omits `created_at` from `Evidence`; `context.go:225` packs revision/retraction but no effective interval | A lease/approval may expire without any database write. A revision certificate cannot detect this unless validity/expiry is explicitly modeled. Arbitrary `source` JSON is not enforced metadata. |

The current documentation is relatively careful: `docs/HARNESS.md:10,42–48,64` promises task-revision and generation checks and warns that direct writes bypass hooks; it does not promise full evidence freshness. Treat this work as a new bounded guarantee, and correct any broader claims elsewhere separately.

## Suggested certificate contract

Record a structured certificate transactionally with the original package, then validate it inside the same `BEGIN IMMEDIATE` transaction as a **new** tool intent or final commit. A separate read-only validation call followed by an unprotected intent insertion reintroduces a race. Include selected snapshot revision, admitted fact heads, exact inspected evidence identities, and explicit scope/predicate observations. Distinguish historical evidence being cited from evidence asserted to be current.

For a small first implementation, per-task evidence/fact epochs plus scope membership are easier to reason about than a semantic dependency graph. Scope epochs conservatively reject any relevant-scope write; they are broader than exact read-set validation. State this tradeoff and measure irrelevant-write false conflicts. Under `all`, capturing only current task IDs misses newly created tasks. A fixed lineage-ID set similarly misses later graph changes if the legacy API can mutate dependencies. Cover schema-compatible Python writers too; native-method-only increments do not cover the advertised interoperability contract.

An exact predicate alternative records the normalized query, scope, limits, selected fact heads and candidate/result identity, then reruns it at validation. Its guarantee is stability of that declared retrieval computation, not completeness of all semantically relevant evidence. Global BM25 corpus statistics can change ranking even when a new row is outside the selected scope, so either pin the intended corpus dependency or deliberately define a narrower guarantee. Do not silently call a top-k comparison a complete absence certificate.

Self-writes need an explicit rule. `Begin` calls `pack` at `harness.go:127` and appends its own user message at `135`; a naive epoch captured while packing is stale before `Begin` returns. `FinishTool` appends own tool evidence at `378–383`. Excluding all writes to the selected task would reopen the original hole. Prefer narrow recorded self-write accounting or a carefully defined baseline after known writes, preserving original evidence observations. Do not refresh all dependency versions when admitting a self-write: that would silently accept intervening foreign changes.

Validate only newly authorized effects. Completed tool replay remains a read of the recorded outcome; a duplicate committed request remains the historical saved reply. Abort and reconciliation must remain available even if evidence becomes stale. A freshness error should preserve the pending journal for explicit recovery/abort and give changed dependencies, rather than automatically rerun a model or uncertain action. Recovery should reject stale evidence or explicitly return a stale flag that cannot authorize new work; it must not relabel an old payload as fresh.

There remains an unavoidable interval between durable intent and arbitrary external execution (`harness.go:464–475`). A freshness check at intent means “current at local authorization,” not “current when the remote effect occurs.” Provider conditional writes/versions or an external transaction protocol are needed for a stronger claim. Likewise, post-commit derived snapshot invalidation requires persistent dependency edges or an explicit policy that old snapshots are unverified; an in-flight certificate alone does not solve it.

## Five independently runnable adversarial scenarios

The executable block below is a source-audit reproducer, not a model-quality test. Each scenario uses a fresh temporary database and independent native backend processes. Schedules are deterministic; no sleeps, network, model responses, or remote effects are used. The Python callback is a local effect counter. Assertions describe **current baseline vulnerabilities**, so a correct freshness implementation is expected to change their outcomes.

Run from project root, using the existing native binary or a freshly compiled candidate. To run any one scenario, pass its name (`fact`, `parent`, `phantom`, `derived`, `action`) as the first argument to the extracted Python block. `all` runs each in a separate temporary database. For example, without creating a test file:

```sh
PYTHONPATH=src python - fact <<'PY'
from pathlib import Path
doc = Path('research/CAUSAL_MEMORY_REVIEW.md').read_text()
code = doc.split('```python\n', 1)[1].split('\n```', 1)[0]
exec(compile(code, 'CAUSAL_MEMORY_REVIEW.md', 'exec'))
PY
```

```python
import json
import sys
import tempfile
from pathlib import Path
from cmpath.harness import NativeHarness, HarnessError
from cmpath.memory import TaskMemory

BINARY = Path('native/bin/cmpath-native').resolve()

def append(h, task, content):
    return h.append_batch([dict(task_id=task, role='document',
                                content=content, source={})])[0]['id']

def fact(h, task, evidence, value, retracted=False):
    return h.backend.call('set_fact', task_id=task,
        fact=dict(key='approval', value=value, evidence_id=evidence,
                  retracted=retracted))

def begin(h, request, task, query='approval', scope='task'):
    return h.begin(request, task, query, scope=scope, budget=50000,
                   recent=0, retrieval_limit=100)

def envelope(session):
    return json.loads(session.messages[0]['content'].split('\n', 1)[1])

def scenario_fact(h, writer, database):
    t = h.create_task('fact test')['id']
    evidence = append(h, t, 'approval granted')
    fact(h, t, evidence, True)
    s = begin(h, 'fact-reader', t)
    assert envelope(s)['facts'][0]['value'] is True
    revision = h.task(t)['revision']
    # Existing evidence can support an explicit retraction; isolate SetFact.
    fact(writer, t, evidence, None, retracted=True)
    assert writer.task(t)['revision'] == revision
    result = s.commit(dict(text='approval still granted',
        facts=[dict(key='approval', value=True, evidence_id=evidence)]))
    assert result['status'] == 'committed'
    assert envelope(begin(h, 'fact-check', t))['facts'][0]['value'] is True
    return 'stale turn overwrote a retraction with a higher fact revision'

def scenario_parent(h, writer, database):
    p = h.create_task('parent')['id']
    c = h.create_task('child', parents=[p])['id']
    ev = append(h, p, 'approval granted')
    s = begin(h, 'child-reader', c, scope='lineage')
    assert ev in [e['id'] for e in envelope(s)['evidence']]
    old_parent = h.task(p)['revision']
    ps = begin(writer, 'parent-corrector', p)
    ps.commit(dict(text='approval revoked', snapshot={'approval': False}))
    assert writer.task(p)['revision'] > old_parent
    recovered = h.recover('child-reader', s.turn['generation'])
    assert recovered.commit(dict(text='approval granted'))['status'] == 'committed'
    return 'parent commit did not invalidate child recovery or commit'

def scenario_phantom(h, writer, database):
    t = h.create_task('release')['id']
    s = begin(h, 'empty-reader', t, query='stopship', scope='all')
    assert envelope(s)['evidence'] == []
    # New scope member and matching evidence: there are no old rows to hash.
    new_task = writer.create_task('late incident')['id']
    append(writer, new_task, 'stopship: release must be blocked')
    assert writer.search('stopship', limit=100)
    fired = []
    s.tool('publish', 'local_counter', {}, lambda: fired.append(1) or 'done')
    assert fired == [1]
    assert s.commit(dict(text='no stopship evidence; released'))['status'] == 'committed'
    return 'empty all-scope evidence became nonempty; action and commit still succeeded'

def scenario_derived(h, writer, database):
    p = h.create_task('prerequisite')['id']
    c = h.create_task('dependent', parents=[p])['id']
    ev = append(h, p, 'approval granted')
    first = begin(h, 'derive', c, scope='lineage')
    assert ev in [e['id'] for e in envelope(first)['evidence']]
    first.commit(dict(text='derived approval', snapshot={'approval': True}))
    ps = begin(writer, 'revoke-later', p)
    ps.commit(dict(text='approval revoked', snapshot={'approval': False}))
    # Deliberately miss the correction in lexical retrieval, leaving the
    # mandatory planner snapshot to demonstrate missing persistent validity.
    next_turn = begin(h, 'reuse', c, query='execute', scope='lineage')
    assert next_turn.snapshot == {'approval': True}
    assert next_turn.commit(dict(text='executed remembered plan'))['status'] == 'committed'
    return 'new turn reused a stale derived snapshot after parent correction'

def scenario_action(h, writer, database):
    t = h.create_task('action test', snapshot={'approval': True})['id']
    s = begin(h, 'act', t)
    memory = TaskMemory(database)
    try:
        memory.set_snapshot(t, {'approval': False},
                            expected_revision=s.turn['task_revision'])
    finally:
        memory.close()
    fired = []
    s.tool('effect', 'local_counter', {}, lambda: fired.append(1) or 'done')
    assert fired == [1]
    try:
        s.commit(dict(text='effect completed'))
    except HarnessError as error:
        assert error.code == 'conflict', error.code
    else:
        raise AssertionError('existing task revision fence failed unexpectedly')
    return 'tool executed on stale selected snapshot; existing commit fence rejected too late'

SCENARIOS = dict(fact=scenario_fact, parent=scenario_parent,
                 phantom=scenario_phantom, derived=scenario_derived,
                 action=scenario_action)
requested = sys.argv[1] if len(sys.argv) > 1 else 'all'
for name in SCENARIOS if requested == 'all' else [requested]:
    with tempfile.TemporaryDirectory(prefix='cmpath-causal-review-') as tmp:
        database = Path(tmp) / 'memory.sqlite'
        with NativeHarness(BINARY, database, create=True) as h:
            with NativeHarness(BINARY, database) as writer:
                print(name + ': ' + SCENARIOS[name](h, writer, database))
```

Validation: all five scenarios above were executed successfully on 2026-09-12 against the existing `native/bin/cmpath-native` baseline binary, with independent temporary databases. The observed results were stale retraction overwrite, stale child recovery/commit, phantom action/commit, stale derived-snapshot reuse, and stale action followed by the expected task-revision commit conflict. Runtime source and test files were not edited. A source-rebuilt candidate should rerun these schedules with its intended new oracles.

The negative-result example deliberately demonstrates a host inference; the engine currently does not promise that an empty FTS result establishes release safety. A future certificate can establish stability of that declared query, while semantic sufficiency remains a host responsibility.

For candidate evaluation, change the oracles to require conflict before new callback dispatch and before stale local commit. Preserve separate positive controls: no intervening write; irrelevant out-of-scope task write; own begin/tool evidence; completed replay; uncertain-tool reconciliation; and a new turn prepared after the correction. The `derived` case is a separate stronger capability and should stay an explicit unsupported outcome unless persistent dependency validation is implemented. Report action prevention, stale commit rejection, false conflicts, retries, and validation overhead separately; aggregating them into an “accuracy” score obscures the guarantees.

## Primary sources and novelty boundary

Six primary sources were inspected. These support the comparisons below; this is a bounded review rather than an exhaustive priority search.

1. **Kung and Robinson, 1981, “On Optimistic Methods for Concurrency Control.”** Read/validation/write phases and read-set/write-set checks already establish the essential mechanism. Sections 3–5 require validation before publication, including an indivisible final validation/write step. A durable context read set applies this known discipline to a long-lived agent turn. [Author-hosted paper](https://www.eecs.harvard.edu/~htk/publication/1981-tods-kung-robinson.pdf).

2. **Mokhov, Mitchell, Peyton Jones, 2018, “Build Systems à la Carte.”** Verifying traces retain dependency keys and value hashes across builds, supporting dynamic dependency checking and reuse. This is close prior art for invalidating a derived planner snapshot when its recorded inputs change; recording a citation alone is weaker than checking a dependency trace before reuse. [Microsoft Research paper](https://www.microsoft.com/en-us/research/wp-content/uploads/2018/03/build-systems.pdf).

3. **Rasmussen et al., January 2025, “Zep: A Temporal Knowledge Graph Architecture for Agent Memory.”** Sections 2.1–2.2.3 describe source-episode links, event versus ingestion time, temporal fact validity, and contradiction-driven edge invalidation. This addresses temporal memory representation and historical provenance; the cited sections do not establish a harness action-time read-set validator. Freshness against database revisions and truth during a validity interval should remain separate contracts. [Paper](https://arxiv.org/html/2501.13956v1).

4. **Li et al., July 2026, “MemTX: Transactional Belief Commit for Stateful Agent Memory.”** This is the closest prior art: staged belief writes, snapshot isolation, commit validation, irreversible-action gating, provenance and typed cascading repair. Its §3.5 expressly limits the strong gate: having at least one action-safe record is existential and does not certify the action's own inputs. Section 3.3 also blocks out-of-snapshot tentative records. A selective certificate over an actual retrieval scope/predicate has a narrower, different predicate, but must be compared experimentally rather than advertised as unprecedented gating. The paper's bounded verification and adversarial/control evaluation also precede that methodology here. [Paper](https://arxiv.org/html/2607.23929v1).

5. **Chao et al., May 2026, “STALE: Can LLM Agents Know When Their Memories Are No Longer Valid?”** It distinguishes direct/co-referential invalidation from propagated invalidation across dependent attributes and evaluates downstream behavior after updates. A deterministic revision certificate cannot discover an unrecorded semantic dependency or infer an implicit contradiction. The `derived` schedule isolates a structural case without claiming to solve STALE's semantic problem. [Paper](https://arxiv.org/html/2605.06527v1).

6. **Sun et al., August 2026, “Agentic Transaction: Towards ACID-Compliant Agent Systems.”** The framework explicitly includes semantic evidence obligations, dependency-aware isolation and transaction-aware semantic state. It further rules out a broad originality claim based merely on applying ACID ideas to agent reasoning. The defensible deliverable here is a smaller reproducible contract integrated with native journal recovery, not the invention of agent transactions. [Paper](https://arxiv.org/html/2608.13900v1).

Recommended claim: “We implement and evaluate deterministic evidence-scope freshness validation for a durable local agent harness, including phantom observations and recovery.” Any claim of stronger selective action-input validation should identify exactly which inputs are declared and checked, and preserve the local-authorization and semantic-completeness limitations above.
