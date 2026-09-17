import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

script = Path(__file__).resolve().parents[1]/"scripts"/"benchmark_public.py"
spec = importlib.util.spec_from_file_location("public_eval",script)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class EvaluationTests(unittest.TestCase):
    def test_labels_are_excluded_from_index_and_prompts(self):
        # The expected answer is deliberately unrelated to every source token.
        # A mistaken answer-as-query implementation would retrieve nothing.
        case = {"question_id":"fixture","question_type":"single-session-user",
                "question":"What was the cobalt budget?","answer":"GOLD_LABEL_MUST_NOT_BE_SENT",
                "answer_session_ids":["s1"],"haystack_session_ids":["s1","s2"],
                "haystack_dates":["2025-01-01","2025-01-02"],
                "haystack_sessions":[[{"role":"user","content":"The cobalt budget is 4900.","has_answer":True},{"role":"assistant","content":""}],
                                     [{"role":"user","content":"A new unrelated coffee article."}]]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"fixture.json"
            path.write_text(json.dumps([case]))
            rows,_,excluded = evaluation.load_longmemeval(path,io.StringIO(),prompts:=io.StringIO())
        self.assertNotIn("GOLD_LABEL_MUST_NOT_BE_SENT",prompts.getvalue())
        self.assertNotIn("has_answer",prompts.getvalue())
        bm25 = next(r for r in rows if r["method"]=="bm25")
        self.assertEqual(bm25["recall_8"],1.0)
        self.assertEqual(excluded["empty_source_messages"],1)
        self.assertEqual(bm25["gold_count"],1)

    def test_partial_recall_and_all_gold_are_distinct(self):
        from cmpath import TaskMemory
        with TaskMemory() as memory:
            t = memory.create_task("Evidence")
            one = memory.append(t.id,"user","cobalt value")
            two = memory.append(t.id,"user","unrelated second value")
            rows = evaluation.benchmark_case(memory,[one,two],"cobalt",question_id="q",category="x",cluster="q",dataset="unit",gold_message_ids={"one","two"},gold_session_ids={"s"},source_ids={one.id:"one",two.id:"two"},session_ids={one.id:"s",two.id:"s"},stream=io.StringIO())
        bm25 = next(r for r in rows if r["method"]=="bm25")
        self.assertEqual(bm25["recall_8"],.5)
        self.assertEqual(bm25["all_gold_8"],0)
