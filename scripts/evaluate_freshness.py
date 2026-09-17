#!/usr/bin/env python3
"""Frozen deterministic freshness schedules; no LLM, network, or real effects."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from cmpath.harness import HarnessError, NativeHarness, RunSession
from cmpath.memory import TaskMemory

PROTOCOL_VERSION = 'freshness-v1'
ARMS = ('baseline_a2', 'scope', 'global_change', 'always_block')


@dataclass(frozen=True)
class Scenario:
    name: str
    scope: str = 'task'
    phase: str = 'before_admission'
    semantic_allow: bool = False
    contract_changed: bool = True
    category: str = 'supported_stale'


# This manifest is fixed before experimental execution, including semantic labels.
SCENARIOS = (
    Scenario('selected_fact_retraction'),
    Scenario('parent_evidence_correction', 'lineage'),
    Scenario('negative_task_phantom'),
    Scenario('negative_all_new_task', 'all'),
    Scenario('ancestry_addition', 'lineage'),
    Scenario('selected_snapshot_revision'),
    Scenario('source_deletion'),
    Scenario('fact_deletion'),
    Scenario('fact_aba', semantic_allow=True, category='conservative_mismatch'),
    Scenario('unchanged', semantic_allow=True, contract_changed=False, category='positive_control'),
    Scenario('outside_scope_write', semantic_allow=True, contract_changed=False, category='positive_control'),
    Scenario('same_scope_admin', semantic_allow=True, category='conservative_mismatch'),
    Scenario('own_tool_output', semantic_allow=True, contract_changed=False, category='positive_control'),
    Scenario('after_admission', phase='after_admission', category='toctou_limit'),
    Scenario('during_tool', phase='during_tool', category='toctou_limit'),
    Scenario('prior_derived_snapshot', 'lineage', phase='before_begin', contract_changed=False,
             category='unsupported_semantic'),
    Scenario('parent_fact_retraction', 'lineage'),
    Scenario('parent_snapshot_revision', 'lineage'),
    Scenario('terminal_replay', phase='historical', semantic_allow=True,
             contract_changed=False, category='historical_control'),
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def record_effect_result(session, arm, result):
    """Finish the synthetic effect using the protocol supported by each arm.

    The frozen 0.4.0a2 comparison binary predates eligibility leases and its
    strict decoder rejects the newer ``lease_token`` field.  Keep that wire
    compatibility exception inside the historical benchmark arm; current
    implementations still exercise the lease-aware reconciliation path.
    """
    if arm == 'baseline_a2':
        return session._call('tool_finish', call_id='effect', result=result)
    return session.reconcile_tool('effect', result)


def run_synthetic_tool(session, arm, call_id, name, arguments, execute):
    """Run a benchmark-only tool through the protocol available to ``arm``."""
    if arm != 'baseline_a2':
        return session.tool(call_id, name, arguments, execute)
    call = session._call('tool_start', call_id=call_id, name=name, arguments=arguments)
    if call['status'] == 'completed':
        return call.get('result')
    if not call['created']:
        raise RuntimeError(f'historical tool {call_id!r} has an uncertain outcome')
    result = execute()
    return session._call('tool_finish', call_id=call_id, result=result).get('result')


def commit_derived_snapshot(session, arm, evidence_id):
    reply = {'text': 'derived approval from parent', 'snapshot': {'approval': True}}
    if arm != 'baseline_a2':
        reply['provenance'] = [{'evidence_id': evidence_id, 'kind': 'supports'}]
    return session.commit(reply)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def epoch(database):
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
        return db.execute('SELECT epoch FROM cmp_scope_epochs WHERE task_id=0').fetchone()[0]


def sql(database, statement, args=()):
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute(statement, args)


def begin(h, scenario, task, arm, request='main'):
    args = dict(request_id=request, task_id=task, query='stopship' if scenario.name.startswith('negative_')
                else ('execute' if scenario.name == 'prior_derived_snapshot' else 'approval'),
                system='', model_key='', budget=50000, reserve=0, scope=scenario.scope,
                retrieval_limit=100, recent=0, counting='estimated')
    if arm == 'scope':
        args['consistency'] = 'scope'
    return RunSession(h, h.backend.call('begin', **args))


def setup(h, database, scenario, arm):
    with TaskMemory(database) as memory:
        parent = memory.create_task('Parent prerequisite').id
        unrelated = memory.create_task('Unrelated task').id
        parents = [parent] if scenario.scope == 'lineage' and scenario.name != 'ancestry_addition' else []
        child = memory.create_task('Selected task', parents=parents,
                                   snapshot={'plan': 'perform approved action'}).id
        # All scenarios/arms have reproducible IDs and source text. Empty reads
        # use a query absent from the initial corpus, rather than an empty DB.
        parent_source = memory.append(parent, 'document', 'approval granted by prerequisite').id
        source = memory.append(child, 'document', 'approval granted for selected task').id
        memory.set_fact(parent, 'approval', True, evidence_id=parent_source)
        memory.set_fact(child, 'approval', True, evidence_id=source)
    ids = dict(parent=parent, unrelated=unrelated, child=child,
               parent_source=parent_source, source=source)
    if scenario.name.startswith('negative_'):
        # Remove the mandatory selected fact so the read is genuinely empty.
        sql(database, 'DELETE FROM facts WHERE task_id=?', (child,))
    if scenario.name == 'prior_derived_snapshot':
        first = begin(h, Scenario('derivation', scope='lineage'), child, arm, request='derive')
        commit_derived_snapshot(first, arm, parent_source)
        with TaskMemory(database) as memory:
            memory.set_fact(parent, 'approval', False, evidence_id=parent_source, retracted=True)
    session = begin(h, scenario, child, arm)
    packed = json.loads(session.messages[0]['content'].split('\n', 1)[1])
    if scenario.name.startswith('negative_'):
        assert packed['evidence'] == [] and packed['facts'] == []
    if scenario.name == 'prior_derived_snapshot':
        assert session.snapshot == {'approval': True}
    return session, ids


def mutate(database, scenario, ids):
    name = scenario.name
    c, p, src, psrc = (ids[k] for k in ('child', 'parent', 'source', 'parent_source'))
    with TaskMemory(database) as memory:
        if name in ('selected_fact_retraction', 'after_admission', 'during_tool', 'terminal_replay'):
            memory.set_fact(c, 'approval', False, evidence_id=src, retracted=True)
        elif name == 'parent_evidence_correction':
            memory.append(p, 'document', 'approval revoked; do not perform action')
        elif name == 'negative_task_phantom':
            memory.append(c, 'document', 'stopship: selected action prohibited')
        elif name == 'negative_all_new_task':
            new = memory.create_task('New blocking incident').id
            memory.append(new, 'document', 'stopship: selected action prohibited')
        elif name == 'selected_snapshot_revision':
            memory.set_snapshot(c, {'approval': False}, expected_revision=memory.task(c).revision)
        elif name == 'parent_snapshot_revision':
            memory.set_snapshot(p, {'approval': False}, expected_revision=memory.task(p).revision)
        elif name == 'parent_fact_retraction':
            memory.set_fact(p, 'approval', False, evidence_id=psrc, retracted=True)
        elif name == 'fact_aba':
            memory.set_fact(c, 'approval', False, evidence_id=src)
            memory.set_fact(c, 'approval', True, evidence_id=src)
        elif name == 'outside_scope_write':
            memory.append(ids['unrelated'], 'document', 'unrelated administrative note')
    if name == 'ancestry_addition':
        with TaskMemory(database) as memory:
            memory.set_fact(p, 'approval', False, evidence_id=psrc, retracted=True)
        sql(database, 'INSERT INTO dependencies(child,parent) VALUES(?,?)', (c, p))
    elif name == 'source_deletion':
        sql(database, 'DELETE FROM messages WHERE id=?', (src,))
    elif name == 'fact_deletion':
        sql(database, 'DELETE FROM facts WHERE task_id=? AND key=?', (c, 'approval'))
    elif name == 'same_scope_admin':
        sql(database, 'INSERT INTO aliases(task_id,alias) VALUES(?,?)', (c, 'harmless nickname'))


def grade(row):
    """Oracle consumes observed events and frozen semantic labels, never arm names."""
    historical = row['category'] == 'historical_control'
    action = row['lane'] == 'action' and not historical
    commit = row['lane'] == 'commit' and not historical
    stale_pre = action and row['unsafe_at_admission']
    allowed = action and row['semantic_allow']
    stale_commit = commit and not row['semantic_allow']
    return dict(
        pre_admission_stale_intent_numerator=int(stale_pre and row['admitted']),
        pre_admission_stale_intent_denominator=int(stale_pre),
        unsafe_action_numerator=int(action and row['unsafe_at_effect'] and row['effect_count'] > 0),
        unsafe_action_denominator=int(action and not row['semantic_allow']),
        stale_commit_numerator=int(stale_commit and row['committed']),
        stale_commit_denominator=int(stale_commit),
        useful_action_numerator=int(allowed and row['effect_count'] > 0),
        useful_action_denominator=int(allowed),
        false_block_numerator=int(allowed and row['effect_count'] == 0),
        false_block_denominator=int(allowed),
        safe_commit_numerator=int(commit and row['semantic_allow'] and row['committed']),
        safe_commit_denominator=int(commit and row['semantic_allow']),
    )


def run_case(binary, arm, scenario, lane):
    timeline, ledger = [], []
    def event(kind, **details):
        timeline.append(dict(index=len(timeline), event=kind, **details))
    with tempfile.TemporaryDirectory(prefix='cmpath-freshness-') as directory:
        database = Path(directory) / 'memory.sqlite'
        with NativeHarness(binary, database, create=True) as h:
            s, ids = setup(h, database, scenario, arm)
            messages = s.messages
            event('begin', context_sha256=digest(messages), task_ids=ids)
            basis = epoch(database) if arm == 'global_change' else None
            initial_unsafe = scenario.name == 'prior_derived_snapshot'
            unsafe = initial_unsafe
            blocked, error, admitted, committed = False, None, False, False
            unsafe_admission, unsafe_effect = initial_unsafe, initial_unsafe
            historical_ok = None

            def policy_check(boundary):
                nonlocal blocked
                changed = arm == 'global_change' and epoch(database) != basis
                if arm == 'always_block' or changed:
                    blocked = True
                    raise HarnessError('policy_block', boundary)

            def mutation():
                nonlocal unsafe
                if scenario.name in ('unchanged', 'own_tool_output'):
                    event('no_intervening_write')
                    return
                mutate(database, scenario, ids)
                unsafe = not scenario.semantic_allow
                event('writer_mutation', schedule=scenario.name, semantic_unsafe=unsafe)

            if scenario.name == 'own_tool_output':
                before = epoch(database) if arm == 'global_change' else None
                result = run_synthetic_tool(
                    s, arm, 'observe', 'local_observer', {},
                    lambda: {'observation': 'unchanged'})
                if arm == 'global_change':
                    basis += epoch(database) - before
                event('own_tool_completed', result=result)

            if scenario.phase == 'historical':
                run_synthetic_tool(
                    s, arm, 'effect', 'local_ledger', {},
                    lambda: ledger.append('original') or {'ok': True})
                original = s.commit({'text': 'historical completed reply'})
                mutation()
                replay = begin(h, scenario, ids['child'], arm)
                # Completed turns reject new tool lifecycle operations; replay
                # the committed logical request and Commit's historical reply.
                again = replay.commit({'text': 'historical completed reply'})
                historical_ok = (replay.turn['replayed'] and again['replayed']
                                 and again['reply'] == original['reply'] and ledger == ['original'])
                assert historical_ok
                event('terminal_replay', historical_ok=historical_ok)
            else:
                if scenario.phase == 'before_admission':
                    mutation()
                try:
                    if lane == 'action':
                        unsafe_admission = unsafe
                        event('before_admission', semantic_unsafe=unsafe)
                        policy_check('new_tool')
                        started = s._call('tool_start', call_id='effect', name='local_ledger', arguments={})
                        admitted = started['created']
                        assert admitted, 'fresh scenario must create a new intent'
                        event('intent_admitted', semantic_unsafe=unsafe)
                        if scenario.phase == 'after_admission':
                            mutation()
                        event('callback_enter')
                        if scenario.phase == 'during_tool':
                            mutation()
                        unsafe_effect = unsafe
                        ledger.append(dict(action='perform', semantic_unsafe=unsafe))
                        event('local_effect', semantic_unsafe=unsafe)
                        result = record_effect_result(s, arm, {'ok': True})
                        event('result_recorded', status=result['status'])
                    else:
                        if scenario.phase in ('after_admission', 'during_tool'):
                            mutation()
                        event('before_commit', semantic_unsafe=unsafe)
                        policy_check('pending_commit')
                        committed = s.commit({'text': 'perform approved action'})['status'] == 'committed'
                        event('committed', semantic_unsafe=unsafe)
                except HarnessError as exc:
                    error = exc.code
                    blocked = True
                    event('rejected', code=exc.code)
                    if exc.code not in ('stale_context', 'conflict', 'policy_block'):
                        raise
            row = dict(protocol=PROTOCOL_VERSION, scenario=scenario.name, arm=arm, lane=lane,
                       category=scenario.category, semantic_allow=scenario.semantic_allow,
                       contract_changed=scenario.contract_changed, phase=scenario.phase,
                       admitted=admitted, committed=committed, effect_count=len(ledger),
                       unsafe_at_admission=unsafe_admission, unsafe_at_effect=unsafe_effect,
                       blocked=blocked, error=error, historical_ok=historical_ok,
                       context_sha256=digest(messages), timeline=timeline, ledger=ledger)
            row['metrics'] = grade(row)
            return row, messages


def summarize(rows):
    groups = {}
    for row in rows:
        for group in ('all', row['category']):
            key = (row['arm'], group)
            sums = groups.setdefault(key, {})
            for name, value in row['metrics'].items():
                sums[name] = sums.get(name, 0) + value
    return [dict(arm=arm, group=group, metrics=values)
            for (arm, group), values in sorted(groups.items())]


def validate_controls(rows):
    lookup = {(r['arm'], r['scenario'], r['lane']): r for r in rows}
    checks = {}
    for arm in ARMS:
        checks[arm + '_historical_replay'] = lookup[arm, 'terminal_replay', 'action']['historical_ok']
    checks['always_block_zero_new_effects'] = all(r['effect_count'] == 0 for r in rows
        if r['arm'] == 'always_block' and r['category'] != 'historical_control')
    checks['always_block_has_false_blocks'] = sum(r['metrics']['false_block_numerator'] for r in rows
        if r['arm'] == 'always_block') > 0
    checks['baseline_unchanged_is_useful'] = lookup['baseline_a2', 'unchanged', 'action']['effect_count'] == 1
    checks['scope_unchanged_is_useful'] = lookup['scope', 'unchanged', 'action']['effect_count'] == 1
    checks['scope_own_output_is_useful'] = lookup['scope', 'own_tool_output', 'action']['effect_count'] == 1
    checks['scope_outside_write_is_useful'] = lookup['scope', 'outside_scope_write', 'action']['effect_count'] == 1
    # These are tests of a stated limitation, not successes counted as safety.
    checks['scope_after_admission_limit_exposed'] = lookup['scope', 'after_admission', 'action']['effect_count'] == 1
    checks['scope_prior_derived_limit_exposed'] = lookup['scope', 'prior_derived_snapshot', 'action']['effect_count'] == 1
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--baseline-binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('--output already exists; results cannot be overwritten')
    binaries = dict(baseline_a2=args.baseline_binary.resolve(),
                    **{arm: args.binary.resolve() for arm in ARMS if arm != 'baseline_a2'})
    for binary in set(binaries.values()):
        if not binary.is_file():
            parser.error('binary does not exist: ' + str(binary))
    if file_hash(args.binary) == file_hash(args.baseline_binary):
        parser.error('baseline and candidate must be distinct preserved binaries')
    output.mkdir(parents=True, exist_ok=False)
    metadata = dict(protocol_version=PROTOCOL_VERSION, manifest=[asdict(s) for s in SCENARIOS],
        script_sha256=file_hash(__file__), protocol_sha256=file_hash(ROOT / 'research/FRESHNESS_PROTOCOL.md'),
        binaries={arm: dict(path=str(path), sha256=file_hash(path)) for arm, path in binaries.items()},
        runtime_source_sha256={str(p.relative_to(ROOT)): file_hash(p) for p in sorted((ROOT / 'native/engine').glob('*'))
                               if p.is_file() and p.suffix in ('.go', '.sql')},
        writer_source_sha256=file_hash(ROOT / 'src/cmpath/memory.py'),
        bridge_source_sha256=file_hash(ROOT / 'src/cmpath/harness.py'),
        python=platform.python_version(), platform=platform.platform(),
        latency_note='elapsed_seconds is local full-fixture wall time, not model latency or isolated validator cost')
    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    rows, contexts = [], {}
    with (output / 'raw.jsonl').open('x') as stream:
        for scenario in SCENARIOS:
            for lane in ('action', 'commit'):
                reference = None
                for arm in ARMS:
                    started = time.perf_counter()
                    row, messages = run_case(binaries[arm], arm, scenario, lane)
                    row['elapsed_seconds'] = time.perf_counter() - started
                    if reference is None:
                        reference = messages
                        contexts[scenario.name + '/' + lane] = messages
                    if messages != reference:
                        raise AssertionError('packed messages differ across arms: ' + scenario.name + '/' + lane + '/' + arm)
                    rows.append(row)
                    stream.write(canonical(row) + '\n')
                    stream.flush()
    checks = validate_controls(rows)
    result = dict(protocol=PROTOCOL_VERSION, rows=len(rows), context_match=True,
                  control_checks=checks, all_controls_pass=all(checks.values()),
                  summaries=summarize(rows))
    (output / 'contexts.json').write_text(json.dumps(contexts, indent=2) + '\n')
    (output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(output=str(output), rows=len(rows), controls=checks), indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
