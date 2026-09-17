#!/usr/bin/env python3
"""Reproduce research/NATIVE_PROTOCOL.md with fresh local synthetic databases."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cmpath.memory import TaskMemory
from cmpath.harness import NativeHarness


def summarize(rows, field):
    values = sorted(row[field] for row in rows)
    return {"n": len(values), "median_ms": statistics.median(values),
            "p95_ms": values[math.ceil(.95 * len(values)) - 1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-binary", type=Path, default=ROOT / "native/bin/cmpath-native")
    parser.add_argument("--bench-binary", type=Path, default=ROOT / "native/bin/cmpath-bench")
    parser.add_argument("--output", type=Path, default=ROOT / "results/native")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 50000])
    args = parser.parse_args()
    if any(n < 50 for n in args.sizes):
        parser.error("corpus sizes must be at least 50")
    args.output.mkdir(parents=True, exist_ok=True)
    native, bench = str(args.native_binary.resolve()), str(args.bench_binary.resolve())
    report = {"protocol": "research/NATIVE_PROTOCOL.md", "utc_started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "environment": {"python": sys.version, "python_sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(),
                "binary_sha256": {Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (native, bench)}},
              "search_warmups_per_method_size": 10, "lifecycle_warmups_per_method": 5,
              "model_calls": 0, "method_order": [], "native_info": [], "search_summary": [], "agreement": [], "lifecycle_summary": []}
    report["protocol_sha256"] = hashlib.sha256((ROOT / report["protocol"]).read_bytes()).hexdigest()
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        report["environment"]["cpu_model"] = next((line.split(":", 1)[1].strip() for line in cpuinfo.read_text().splitlines() if line.startswith("model name")), "unknown")
    search_rows, lifecycle_rows = [], []
    def direct(*params):
        data = json.loads(subprocess.check_output([bench, *map(str, params)], text=True))
        report["environment"]["go"] = data["go_version"]
        report["environment"]["gomaxprocs"] = data["gomaxprocs"]
        return data
    with tempfile.TemporaryDirectory(prefix="cmpath-benchmark-") as temp:
        temp = Path(temp)
        methods = ["python_embedded", "go_embedded", "python_go_bridge"]
        for index, size in enumerate(args.sizes):
            base = temp / f"base-{size}.db"
            with TaskMemory(base) as memory:
                task = memory.create_task("Synthetic benchmark")
                with memory.batch():
                    for i in range(size):
                        memory.append(task.id, "document", f"marker{i:06d} shared evidence benchmark corpus group{i % 17:02d}")
            queries = []
            for i in range(50):
                queries.extend([{"query_id": f"selective-{i:02d}", "kind": "selective", "query": f"marker{i * size // 50:06d}"},
                                {"query_id": f"broad-{i:02d}", "kind": "broad", "query": ["shared", "evidence", "benchmark", "corpus"][i % 4]}])
            query_path = temp / "queries.json"
            query_path.write_text(json.dumps(queries))
            order = methods[index % 3:] + methods[:index % 3]
            report["method_order"].append({"corpus_size": size, "methods": order})
            for method in order:
                print(f"Search: {size} messages, {method}", flush=True)
                db = temp / f"{method}-{size}.db"
                shutil.copyfile(base, db)
                if method == "go_embedded":
                    data = direct("-db", db, "-queries", query_path, "-size", size)
                    search_rows.extend(data["rows"])
                    report["native_info"].append({"method": method, "corpus_size": size, **data["info"]})
                else:
                    memory = TaskMemory(db) if method == "python_embedded" else NativeHarness(native, db)
                    try:
                        for q in queries[:10]:
                            memory.search(q["query"], limit=8)
                        for q in queries:
                            start = time.perf_counter_ns()
                            hits = memory.search(q["query"], limit=8)
                            elapsed = (time.perf_counter_ns() - start) / 1e6
                            ids = [h.id for h in hits] if method == "python_embedded" else [h["id"] for h in hits]
                            search_rows.append({"method": method, "corpus_size": size, **q, "hit_ids": ids, "search_ms": elapsed})
                        if method == "python_go_bridge":
                            report["native_info"].append({"method": method, "corpus_size": size, **memory.backend.call("info")})
                    finally:
                        memory.close()
            for method in methods:
                subset = [r for r in search_rows if r["corpus_size"] == size and r["method"] == method]
                for kind in ("all", "selective", "broad"):
                    selected = subset if kind == "all" else [r for r in subset if r["kind"] == kind]
                    report["search_summary"].append({"corpus_size": size, "method": method, "kind": kind, **summarize(selected, "search_ms")})
                if method != "python_embedded":
                    reference = {r["query_id"]: r["hit_ids"] for r in search_rows if r["corpus_size"] == size and r["method"] == "python_embedded"}
                    mismatches = [r["query_id"] for r in subset if reference[r["query_id"]] != r["hit_ids"]]
                    report["agreement"].append({"corpus_size": size, "method": method, "exact_ordered_matches": len(subset) - len(mismatches), "queries": len(subset), "mismatched_query_ids": mismatches})
        print("Lifecycle: embedded Go and persistent bridge", flush=True)
        data = direct("-db", temp / "lifecycle-go.db", "-mode", "lifecycle", "-warmup", 5, "-count", 100)
        lifecycle_rows.extend(data["rows"])
        report["native_info"].append({"method": "go_embedded", "experiment": "lifecycle", **data["info"]})
        with NativeHarness(native, temp / "lifecycle-bridge.db", create=True) as harness:
            task = harness.create_task("Lifecycle benchmark")
            for i in range(105):
                operation_id = f"operation-{i:03d}"
                start = time.perf_counter_ns()
                session = harness.begin(operation_id, task["id"], "Record a benchmark acknowledgement", budget=2000, scope="task", retrieval_limit=24, recent=4, counting="estimated")
                result = session.commit({"text": "Acknowledged."})
                elapsed = (time.perf_counter_ns() - start) / 1e6
                if i >= 5:
                    lifecycle_rows.append({"method": "python_go_bridge", "operation_id": operation_id, "lifecycle_ms": elapsed, "status": result["status"]})
            report["native_info"].append({"method": "python_go_bridge", "experiment": "lifecycle", **harness.backend.call("info")})
    for method in ("go_embedded", "python_go_bridge"):
        report["lifecycle_summary"].append({"method": method, **summarize([r for r in lifecycle_rows if r["method"] == method], "lifecycle_ms")})
    assert all(r["status"] == "committed" for r in lifecycle_rows)
    for info in report["native_info"]:
        assert info["cmp_model_calls"] == 0 and info["cmp_tool_calls"] == 0
        if info.get("experiment") == "lifecycle":
            assert info["cmp_turns"] == 105 and info["messages"] == 210
        else:
            assert info["messages"] == info["corpus_size"]
    report["actual_counts"] = {"corpus_sizes": args.sizes, "search_timed": len(search_rows), "search_warmup": len(args.sizes) * 3 * 10, "lifecycle_timed": len(lifecycle_rows), "lifecycle_warmup": 10, "lifecycle_begin_calls": len(lifecycle_rows) + 10, "lifecycle_commit_calls": len(lifecycle_rows) + 10, "model_calls": 0}
    for filename, rows in (("search_rows.jsonl", search_rows), ("lifecycle_rows.jsonl", lifecycle_rows)):
        (args.output / filename).write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    (args.output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    native_sqlite = sorted({item["sqlite"] for item in report["native_info"]})
    lines = ["# Local native benchmark", "", f"Started {report['utc_started']}. One serial run; p95 is nearest rank ceil(0.95*n). See research/NATIVE_PROTOCOL.md for timing boundaries and limitations.", "", f"Python {platform.python_version()}, SQLite {sqlite3.sqlite_version}; Go {report['environment']['go']}, native SQLite {', '.join(native_sqlite)}. Platform: {platform.platform()}. CPU: {report['environment'].get('cpu_model', 'unknown')}.", "", "| Messages | Method | Query kind | n | Median ms | p95 ms |", "|---:|---|---|---:|---:|---:|"]
    lines += [f"| {r['corpus_size']} | {r['method']} | {r['kind']} | {r['n']} | {r['median_ms']:.4f} | {r['p95_ms']:.4f} |" for r in report["search_summary"]]
    lines += ["", "| Lifecycle method | n | Median ms | p95 ms |", "|---|---:|---:|---:|"]
    lines += [f"| {r['method']} | {r['n']} | {r['median_ms']:.4f} | {r['p95_ms']:.4f} |" for r in report["lifecycle_summary"]]
    lines += ["", "Exact ordered-hit agreement against Python:"] + [f"- {r['corpus_size']} messages, {r['method']}: {r['exact_ordered_matches']}/{r['queries']}" for r in report["agreement"]]
    lines += ["", "Actual counts: " + json.dumps(report["actual_counts"]), "", "Environment and final native counts are retained in benchmark.json. All raw measurements are in search_rows.jsonl and lifecycle_rows.jsonl. Search warmups, process startup and corpus construction are excluded. Lifecycle includes persistence but no model or tool calls. Cache state is uncontrolled; methods use separate connection caches, rotated run order and potentially different SQLite versions. Results do not establish production performance or agent quality."]
    (args.output / "benchmark.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(report["actual_counts"]))


if __name__ == "__main__":
    main()
