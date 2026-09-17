from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cmpath import (
    BudgetError,
    HybridMemoryRouter,
    MemoryRoute,
    MemoryRouter,
    Route,
    RouterConfig,
    TaskMemory,
    TaskResolutionError,
)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = TaskMemory(Path(self.temp.name) / "memory.db")
        self.root = self.memory.create_task("Root report", aliases=["root"])
        self.child = self.memory.create_task(
            "Child report", aliases=["child"], parents=[self.root.id]
        )
        self.sibling = self.memory.create_task("Sibling report", aliases=["report"])
        self.root_evidence = self.memory.append(
            self.root.id, "user", "Root budget is 1200 USD.", source={"file": "root.md"}
        )
        self.child_evidence = self.memory.append(
            self.child.id, "user", "Child budget is 3400 USD.", source={"file": "child.md"}
        )
        self.sibling_evidence = self.memory.append(
            self.sibling.id, "user", "Sibling-only secret.", source={"file": "sibling.md"}
        )
        self.memory.resume(self.child.id)
        self.router = MemoryRouter(self.memory)

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def test_route_kinds_and_alias(self):
        self.assertIs(HybridMemoryRouter, MemoryRouter)
        self.assertEqual(Route.TASK, MemoryRoute.TASK)
        self.assertEqual(self.router.route("child").route, "task")
        self.assertEqual(self.router.route("child", scope="lineage").route, "lineage")
        self.assertEqual(self.router.route("child", deep=True).route, "deep")
        self.assertEqual(self.router.route("anything", route="pinned").route, "pinned")
        self.assertEqual(self.router.route("anything", route="none").route, "none")

    def test_ambiguous_resolution_fails_closed_without_context_read(self):
        # ``report`` is an alias for the sibling and appears in the root/child
        # titles as well; add a second explicit alias to make ambiguity clear.
        other = self.memory.create_task("Another", aliases=["report"])
        decision = self.router.route("report", use_active=False)
        self.assertEqual(decision.route, "none")
        self.assertEqual(decision.status, "ambiguous")
        self.assertIsNone(decision.task_id)
        self.assertEqual(decision.candidates, (self.sibling.id, other.id))
        context = self.router.retrieve("report", use_active=False)
        self.assertEqual(context.route, "none")
        self.assertIsNone(context.task_id)
        self.assertEqual(context.citations, ())
        with self.assertRaises(TaskResolutionError):
            self.router.retrieve("report", use_active=False, strict=True)

    def test_retrieval_scope_is_on_demand_and_preserves_citations(self):
        calls = []
        original = self.memory.context

        def context(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        self.memory.context = context
        decision = self.router.route("child", use_active=False)
        self.assertEqual(calls, [])
        package = self.router.retrieve("child budget", use_active=False)
        self.assertEqual(package.route, "task")
        self.assertEqual(calls[0][1]["scope"], "task")
        self.assertIn(self.child_evidence.citation, package.citations)
        self.assertNotIn(self.root_evidence.citation, package.citations)
        lineage = self.router.retrieve("budget", route="lineage", task_id=self.child.id)
        self.assertEqual(calls[-1][1]["scope"], "lineage")
        self.assertIn(self.root_evidence.citation, lineage.citations)
        deep = self.router.retrieve("secret", route="deep", task_id=self.child.id)
        self.assertEqual(calls[-1][1]["scope"], "all")
        self.assertIn(self.sibling_evidence.citation, deep.citations)

    def test_pinned_set_is_bounded_deterministic_and_source_preserving(self):
        router = MemoryRouter(self.memory, max_pinned_tasks=2)
        router.pin(self.root.id, evidence_ids=[self.root_evidence.id])
        router.pin(self.child.id, evidence_ids=[self.child_evidence.id])
        router.pin(self.sibling.id, evidence_ids=[self.sibling_evidence.id])
        self.assertEqual(router.pinned_task_ids, (self.child.id, self.sibling.id))
        context = router.retrieve("unrelated", route="pinned", budget=2000)
        self.assertEqual(context.route, "pinned")
        self.assertNotIn(self.root_evidence.citation, context.citations)
        self.assertIn(self.child_evidence.citation, context.citations)
        self.assertIn(self.sibling_evidence.citation, context.citations)
        payload = json.loads(context.as_messages()[1]["content"])["memory_context"]
        self.assertEqual(payload["memory_route"], "pinned")
        self.assertEqual(payload["citations"], list(context.citations))
        self.assertLessEqual(context.used_units, context.input_allowance)

    def test_pinned_context_uses_full_custom_counter_and_reserve(self):
        router = MemoryRouter(self.memory)
        router.pin(self.child.id, evidence_ids=[self.child_evidence.id])
        counter = lambda messages: len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        context = router.retrieve(
            "budget", route="pinned", budget=3000, reserve=100, counter=counter
        )
        self.assertEqual(context.counting, "custom")
        self.assertEqual(context.used_units, counter(context.as_messages()))
        self.assertLessEqual(context.used_units, 2900)

    def test_impossible_base_budget_is_rejected_for_every_local_route(self):
        for route in ("none", "pinned"):
            with self.subTest(route=route), self.assertRaises(BudgetError):
                self.router.retrieve("x" * 2000, route=route, budget=20)

    def test_active_fallback_is_deterministic_and_can_be_disabled(self):
        self.assertEqual(self.router.resolve_task("What is the budget?").task_id, self.child.id)
        unresolved = self.router.resolve_task("What is the budget?", use_active=False)
        self.assertEqual(unresolved.status, "not_found")

    def test_pinned_route_does_not_search_or_call_context(self):
        router = MemoryRouter(self.memory)
        router.pin(self.child.id)
        original_context = self.memory.context
        original_search = self.memory.search
        self.memory.context = lambda *a, **k: self.fail("pinned route must not call context")
        self.memory.search = lambda *a, **k: self.fail("pinned route must not search")
        try:
            result = router.retrieve("budget", route="pinned")
        finally:
            self.memory.context = original_context
            self.memory.search = original_search
        self.assertEqual(result.route, "pinned")

    def test_conflicting_route_selectors_are_rejected(self):
        with self.assertRaises(ValueError):
            self.router.route("child", mode="task", requested_route="deep")

    def test_default_route_aliases_are_stored_canonically(self):
        for alias, canonical in (("all", "deep"), ("full", "deep"), ("related", "lineage")):
            with self.subTest(alias=alias):
                config = RouterConfig(default_route=alias)
                self.assertEqual(config.default_route, canonical)

                router = MemoryRouter(self.memory, config=config)
                decision = router.route("child", use_active=False)
                self.assertEqual(decision.route, canonical)
                context = router.retrieve("budget", task_id=self.child.id, use_active=False)
                self.assertEqual(context.route, canonical)


if __name__ == "__main__":
    unittest.main()
