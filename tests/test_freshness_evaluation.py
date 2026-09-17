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


if __name__ == '__main__':
    unittest.main()
