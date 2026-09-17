"""Operational behavior and local scaling study; no generated model answers."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import random
import statistics
import tempfile
import time

from cmpath import TaskMemory


def task_study():
    rows = []
    for seed in range(40):
        rng = random.Random(seed+4000)
        with TaskMemory() as m:
            tasks,values = [],{}
            with m.batch():
                for index in range(12):
                    name = f"{rng.choice(['Cedar','Coral','Juniper','Willow'])} {seed:02d}{index:02d}"
                    alias = f"assignment {seed:02d}{index:02d}"
                    task = m.create_task(name,aliases=[alias,"shared review"],snapshot={"next_action":f"send document {index}","revision":2})
                    for revision in (1,2):
                        value = rng.randrange(1000,9999)
                        evidence = m.append(task.id,"user",f"The approved budget is {value} USD for {name}; revision {revision}.")
                        m.set_fact(task.id,"approved_budget",value,evidence_id=evidence.id)
                    values[task.id] = value
                    tasks.append(task)
                    m.archive(task.id)
            m.resume(tasks[-1].id)
            target = tasks[seed%11]
            queries = [
                ("explicit_id",f"Resume T{target.id}",target.id,True),
                ("registered_alias",f"Kembali ke assignment {seed:02d}{seed%11:02d}, lanjutkan.",target.id,True),
                ("unregistered_paraphrase","Continue our earlier financial signoff exercise",target.id,True),
                ("ambiguous_alias","Resume the shared review",None,False),
                ("absent_id","Resume T999999",None,False),
            ]
            for category,query,expected,answerable in queries:
                before = m.state()
                resolution = m.resolve(query)
                unchanged = before == m.state()
                correct = resolution.task_id == expected and expected is not None
                rows.append({"seed":seed,"category":category,"answerable":answerable,
                             "resolution":resolution.status,"resolved":resolution.task_id is not None,
                             "correct_resolution":correct,"false_resolution":resolution.task_id is not None and not correct,
                             "state_unchanged":unchanged})
            restored = m.resume(target.id)
            rows.append({"seed":seed,"category":"resume_snapshot","success":restored["snapshot"] == target.snapshot})
            for scope in ("task","all"):
                ctx = m.context(target.id,"What is the approved budget?",budget=2000,scope=scope)
                payload = json.loads(ctx.as_messages()[1]["content"])["memory_context"]
                facts = payload["facts"]
                latest = any(f["key"] == "approved_budget" and f["value"] == values[target.id] for f in facts)
                wrong = sum(e["task_id"] != target.id for e in payload["evidence"])
                rows.append({"seed":seed,"category":f"context_{scope}","latest_revision_present":latest,
                             "unrelated_evidence":wrong,"budget_units":ctx.used_units,
                             "budget_ok":ctx.used_units<=2000})
    return rows


def scaling():
    rows,groups = [],[]
    for size in (1000,10000,50000):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp)/"scale.db"
            with TaskMemory(db) as m:
                start = time.perf_counter()
                with m.batch():
                    tasks = [m.create_task(f"Project {i}") for i in range(100)]
                    for index in range(size):
                        m.append(tasks[index%100].id,"document",
                                 f"Record marker{index:07d}. Project {index%100} approved the budget and document review. "
                                 "The delivery owner recorded a revision and scheduled a follow-up discussion.",
                                 source={"sequence":index})
                ingest_seconds = time.perf_counter()-start
                # Fixed warmups are omitted from timing samples.
                m.search("marker0000001")
                m.search("approved budget")
                rng = random.Random(1102)
                for index in range(100):
                    kind = "selective" if index%2 == 0 else "broad"
                    query = f"marker{rng.randrange(size):07d}" if kind == "selective" else "approved budget document review"
                    start = time.perf_counter()
                    hits = m.search(query,limit=8)
                    elapsed = (time.perf_counter()-start)*1000
                    rows.append({"size":size,"query_index":index,"query_kind":kind,"query_ms":elapsed,"hits":len(hits)})
                file_bytes = sum(p.stat().st_size for p in Path(temp).iterdir() if p.is_file())
                for kind in ("selective","broad"):
                    times = sorted(r["query_ms"] for r in rows if r["size"]==size and r["query_kind"]==kind)
                    groups.append({"messages":size,"query_kind":kind,"queries":len(times),
                                   "median_ms":statistics.median(times),"p95_ms":times[int(.95*(len(times)-1))],
                                   "ingest_seconds":ingest_seconds,"database_and_journal_bytes":file_bytes})
                assert m.check()["ok"]
                print(f"Scaling {size} messages indexed in {ingest_seconds:.2f}s",flush=True)
    return rows,groups


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,default=Path("results/operations"))
    args = p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    started = time.perf_counter()
    rows = task_study()
    queries,groups = scaling()
    (args.output/"task_rows.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    (args.output/"scaling_rows.jsonl").write_text("".join(json.dumps(r)+"\n" for r in queries))
    summary = {"schema_version":1,"seeds":40,"task_probe_rows":len(rows),"scaling_queries":len(queries),"elapsed_seconds":time.perf_counter()-started,"scaling":groups,"model_calls":0}
    task_groups = []
    for category in sorted({r["category"] for r in rows}):
        group = [r for r in rows if r["category"]==category]
        aggregate = {"category":category,"n":len(group)}
        for key in group[0]:
            if key not in ("seed","category","resolution"):
                aggregate[key] = statistics.mean(r[key] for r in group)
        task_groups.append(aggregate)
    summary["task_groups"] = task_groups
    (args.output/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary),flush=True)


if __name__ == "__main__":
    main()
