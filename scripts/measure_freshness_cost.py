"""Serial local microbenchmark; no model calls or production latency claim."""
from pathlib import Path
import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cmpath.harness import NativeHarness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "native/bin/cmpath-native")
    parser.add_argument("--baseline-binary", type=Path, default=ROOT / "native/bin/baseline-0.4.0a2/cmpath-native")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=40)
    args = parser.parse_args()
    if args.repetitions < 10:
        parser.error("at least ten measured repetitions required")
    args.output.mkdir(parents=True, exist_ok=False)
    observations = []
    arms = [("a2_snapshot", args.baseline_binary, "snapshot"),
            ("a3_snapshot", args.binary, "snapshot"),
            ("a3_scope", args.binary, "scope")]
    for position, width in enumerate((1, 4, 16)):
        ordered = arms[position:] + arms[:position]
        for arm, binary, consistency in ordered:
            with tempfile.TemporaryDirectory(prefix="cmp-fresh-cost-") as temp:
                with NativeHarness(binary.resolve(), Path(temp) / "memory.db", create=True) as harness:
                    parents = [harness.create_task(f"Parent {i}")["id"] for i in range(width - 1)]
                    task = harness.create_task("Cost probe", parents=parents)
                    ids = [task["id"], *parents]
                    records = [{"task_id": task_id, "role": "document",
                                "content": f"Administrative inventory item {i}.", "source": {}}
                               for task_id in ids for i in range(100)]
                    start = time.perf_counter_ns()
                    harness.append_batch(records)
                    append_ms = (time.perf_counter_ns() - start) / 1e6
                    for iteration in range(args.repetitions + 5):
                        begin_at = time.perf_counter_ns()
                        session = harness.begin(f"cost-{iteration}", task["id"],
                            "What is the launch approval?", consistency=consistency,
                            scope="lineage", retrieval_limit=1, recent=0, budget=4000)
                        after_begin = time.perf_counter_ns()
                        session.tool("local-result", "count_documents", {}, lambda: {"count": len(records)})
                        after_tool = time.perf_counter_ns()
                        session.commit({"text": "No approval evidence was supplied."})
                        after_commit = time.perf_counter_ns()
                        if iteration >= 5:
                            observations.append({"arm": arm, "scope_tasks": width,
                                "initial_messages": len(records), "iteration": iteration - 5,
                                "initial_append_batch_ms": append_ms,
                                "begin_ms": (after_begin - begin_at) / 1e6,
                                "tool_roundtrip_ms": (after_tool - after_begin) / 1e6,
                                "commit_ms": (after_commit - after_tool) / 1e6,
                                "total_ms": (after_commit - begin_at) / 1e6})
    (args.output / "rows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in observations))
    groups = []
    for width in (1, 4, 16):
        for arm, _, _ in arms:
            selected = [r for r in observations if r["arm"] == arm and r["scope_tasks"] == width]
            groups.append({"arm": arm, "scope_tasks": width, "n": len(selected),
                "median_ms": {key: statistics.median(r[key] for r in selected)
                              for key in ("begin_ms", "tool_roundtrip_ms", "commit_ms", "total_ms")},
                "initial_append_batch_ms": selected[0]["initial_append_batch_ms"]})
    summary = {"measurement": "local Python-to-Go bridge lifecycle latency",
        "groups": groups, "warmup_per_group": 5, "python": platform.python_version(),
        "platform": platform.platform(), "model_calls": 0,
        "limits": "One serial run; rotated arm order by scope size, uncontrolled OS caches; no inference latency or production-load claim. Initial ingestion timed once per group, not repeated statistics. Callback returns a deterministic local value. Scope size and corpus size both grow; these are workload points, not isolated asymptotic proof.",
        "sha256": {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (Path(__file__), args.binary.resolve(), args.baseline_binary.resolve())}}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
