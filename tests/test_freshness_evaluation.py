"""Discriminating oracle checks; do not execute a candidate benchmark arm."""
import importlib.util
from pathlib import Path
import sys
import unittest

SPEC = importlib.util.spec_from_file_location('freshness_eval',
    Path(__file__).resolve().parents[1] / 'scripts/evaluate_freshness.py')
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class FreshnessOracleTests(unittest.TestCase):
    def row(self, **changes):
        value = dict(category='supported_stale', lane='action', semantic_allow=False,
                     unsafe_at_admission=True, unsafe_at_effect=True,
                     admitted=True, effect_count=1, committed=False)
        return {**value, **changes}

    def test_always_block_cannot_hide_lost_useful_work(self):
        metrics = MOD.grade(self.row(semantic_allow=True, unsafe_at_admission=False,
                                    unsafe_at_effect=False, admitted=False, effect_count=0))
        self.assertEqual(metrics['unsafe_action_numerator'], 0)
        self.assertEqual(metrics['false_block_numerator'], 1)
        self.assertEqual(metrics['useful_action_denominator'], 1)
        self.assertEqual(metrics['useful_action_numerator'], 0)

    def test_post_admission_race_is_unsafe_but_not_stale_admission(self):
        metrics = MOD.grade(self.row(category='toctou_limit', unsafe_at_admission=False))
        self.assertEqual(metrics['pre_admission_stale_intent_denominator'], 0)
        self.assertEqual(metrics['unsafe_action_numerator'], 1)

    def test_safe_aba_is_false_block_not_unsafe_action(self):
        metrics = MOD.grade(self.row(category='conservative_mismatch', semantic_allow=True,
                                    admitted=False, effect_count=0, unsafe_at_admission=False,
                                    unsafe_at_effect=False))
        self.assertEqual(metrics['false_block_numerator'], 1)
        self.assertEqual(metrics['unsafe_action_denominator'], 0)

    def test_historical_effect_is_excluded_from_fresh_metrics(self):
        metrics = MOD.grade(self.row(category='historical_control'))
        self.assertTrue(all(value == 0 for value in metrics.values()))

    def test_commit_is_independent_of_tool_admission(self):
        metrics = MOD.grade(self.row(lane='commit', committed=True, admitted=False, effect_count=0))
        self.assertEqual(metrics['stale_commit_numerator'], 1)
        self.assertEqual(metrics['pre_admission_stale_intent_denominator'], 0)

    def test_historical_baseline_omits_unsupported_lease_field(self):
        class Session:
            def __init__(self):
                self.calls = []

            def _call(self, operation, **kwargs):
                self.calls.append((operation, kwargs))
                return {'status': 'completed'}

            def reconcile_tool(self, *args, **kwargs):
                raise AssertionError('historical baseline must use its legacy wire protocol')

        session = Session()
        result = MOD.record_effect_result(session, 'baseline_a2', {'ok': True})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(session.calls, [('tool_finish', {
            'call_id': 'effect', 'result': {'ok': True}})])

    def test_current_arms_keep_lease_aware_reconciliation(self):
        class Session:
            def __init__(self):
                self.calls = []

            def reconcile_tool(self, call_id, result):
                self.calls.append((call_id, result))
                return {'status': 'completed'}

        for arm in ('scope', 'global_change', 'always_block'):
            session = Session()
            result = MOD.record_effect_result(session, arm, {'ok': True})
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(session.calls, [('effect', {'ok': True})])

    def test_historical_synthetic_tool_uses_legacy_boundaries(self):
        class Session:
            def __init__(self):
                self.calls = []

            def _call(self, operation, **kwargs):
                self.calls.append((operation, kwargs))
                if operation == 'tool_start':
                    return {'status': 'started', 'created': True}
                return {'status': 'completed', 'result': kwargs['result']}

        session = Session()
        result = MOD.run_synthetic_tool(
            session, 'baseline_a2', 'observe', 'reader', {}, lambda: {'ok': True})
        self.assertEqual(result, {'ok': True})
        self.assertEqual([call[0] for call in session.calls], ['tool_start', 'tool_finish'])
        self.assertNotIn('lease_token', session.calls[-1][1])

    def test_current_snapshot_derivation_declares_provenance(self):
        class Session:
            def __init__(self):
                self.reply = None

            def commit(self, reply):
                self.reply = reply
                return {'status': 'committed'}

        current = Session()
        MOD.commit_derived_snapshot(current, 'scope', 41)
        self.assertEqual(current.reply['provenance'], [
            {'evidence_id': 41, 'kind': 'supports'}])

        historical = Session()
        MOD.commit_derived_snapshot(historical, 'baseline_a2', 41)
        self.assertNotIn('provenance', historical.reply)


if __name__ == '__main__':
    unittest.main()
