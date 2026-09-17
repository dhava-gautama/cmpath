"""Checks for evaluator leakage, baseline isolation, and whole-source packing."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('evaluate_pilot', ROOT / 'scripts/evaluate_pilot.py')
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


class PilotEvaluationTests(unittest.TestCase):
    def test_queries_and_labels_are_not_source_records(self):
        sources, queries, labels = pilot.fixture()
        self.assertEqual(len(queries), 40)
        self.assertEqual(len({q['case_id'] for q in queries}), 40)
        self.assertEqual({q['case_id'] for q in queries}, {l['case_id'] for l in labels})
        for source in sources:
            self.assertFalse({'query', 'required_citations', 'case_id', 'answer'} & source.keys())
            self.assertFalse(any(q['query'] in source['content'] for q in queries))
        ids = {f"T{s['task_id']}:M{s['id']}" for s in sources}
        self.assertTrue(all(set(l['required_citations']) <= ids for l in labels))

    def test_baseline_scope_and_whole_evidence_budget(self):
        sources, _, _ = pilot.fixture()
        baseline = pilot.Baseline(sources)
        self.addCleanup(baseline.db.close)
        ranked = baseline.search('procurement spending cap', 3)
        self.assertTrue(ranked)
        self.assertTrue(all(e['task_id'] == 3 for e in ranked))
        package = baseline.package('procurement spending cap', {'id': 3, 'title': 'Workflow 3', 'snapshot': {}}, ranked)
        self.assertLessEqual(package['used_units'], 1000)
        self.assertIn('T3:M62', package['citations'])
        import json
        packed = json.loads(package['messages'][1]['content'].split('\n', 1)[1])['evidence']
        self.assertTrue(all(e['content'] == baseline.sources[e['id']]['content'] for e in packed))

    def test_live_reader_matched_payloads_without_network(self):
        import json
        import tempfile
        from unittest.mock import patch
        from cmpath.agent import ResearchAgent
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            messages = [{'role': 'user', 'content': 'source only'}]
            pilot.jsonl(root / 'prompts.jsonl', [{'case_id': 'case', 'condition': condition, 'messages': messages} for condition in ['cmp_native', 'sqlite_bm25_task_filter']])
            pilot.jsonl(root / 'labels.jsonl', [{'case_id': 'case', 'required_citations': ['GOLD-NEVER-DISPATCH']}])
            dispatched = []
            def fake_dispatch(_, raw):
                dispatched.append(json.loads(raw))
                return {'choices': [{'message': {'content': 'fixture response'}}], 'usage': {'prompt_tokens': 7}}
            with patch.object(ResearchAgent, 'dispatch', fake_dispatch):
                result = pilot.live_reader(root, 'http://127.0.0.1:9999/v1/chat/completions', 'fixture-model', root / 'live', 1)
            self.assertEqual(dispatched[0], dispatched[1])
            self.assertNotIn('GOLD-NEVER-DISPATCH', json.dumps(dispatched))
            self.assertEqual(result['model_calls_attempted'], 2)
            self.assertEqual(result['status'], 'completed')
            with self.assertRaises(FileExistsError):
                pilot.live_reader(root, 'http://127.0.0.1:9999/v1/chat/completions', 'fixture-model', root / 'live', 1)


if __name__ == '__main__':
    unittest.main()
