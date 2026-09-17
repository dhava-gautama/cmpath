"""Explicit migration of portable version-2 CMPSession JSON checkpoints."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from .memory import TaskMemory


def migrate_v02(source: str | Path, destination: str | Path) -> dict:
    """Create a fresh database. Legacy IDs become aliases; no facts are inferred."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("Migration requires a new destination")
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    if data.get("version") != 2:
        raise ValueError("Only portable version-2 checkpoints, including evicted_rows, are supported")
    records = dict(data["graph"]["paths"])
    payloads = {}
    for row in data.get("evicted_rows",[]):
        pid = row["id"]
        if pid in records:
            raise ValueError("A legacy path exists in both the live graph and eviction archive")
        records[pid] = json.loads(row["record_json"])
        payloads[pid] = json.loads(row["messages_json"])
    mapping,owners = {},{}
    with TaskMemory() as memory:
        with memory.batch():
            pending = dict(records)
            while pending:
                ready = [pid for pid,rec in pending.items() if all(parent in mapping for parent in rec.get("depends_on",[]))]
                if not ready:
                    raise ValueError("Legacy dependencies contain a cycle or missing parent")
                for pid in sorted(ready):
                    rec = pending.pop(pid)
                    waypoint = rec.get("waypoint") or {}
                    task = memory.create_task(waypoint.get("title") or pid,
                                              parents=[mapping[p] for p in rec.get("depends_on",[])],
                                              aliases=[pid],snapshot=rec.get("snapshot") or {})
                    mapping[pid] = task.id
                    if pid in payloads:
                        messages = payloads[pid]
                    else:
                        segments = rec.get("segments",[])
                        messages = [m for m in data.get("log",[]) if any(m["ts"]>s["start_ts"] and (s.get("end_ts") is None or m["ts"]<=s["end_ts"]) for s in segments)]
                    for message in messages:
                        ts = message["ts"]
                        if ts in owners and owners[ts] != pid:
                            raise ValueError(f"Legacy timestamp {ts} is owned by multiple paths")
                        owners[ts] = pid
                        if not message["content"].strip():
                            continue
                        role = message["role"] if message["role"] in ("user","assistant","tool","document") else "document"
                        memory.append(task.id,role,message["content"],source={"legacy_path":pid,"legacy_ts":ts,"legacy_role":message["role"],"legacy_metadata":message.get("meta") or {}})
                    if waypoint:
                        memory.append(task.id,"document","Legacy waypoint, retained as derived summary data: "+json.dumps(waypoint,ensure_ascii=False),source={"legacy_path":pid,"kind":"derived_waypoint"})
                    if rec.get("state") in ("archived","evicted"):
                        memory.archive(task.id)
            active = data["graph"].get("active_id")
            if active:
                if active not in mapping:
                    raise ValueError("Legacy active path is missing")
                memory.resume(mapping[active])
        if not memory.check()["ok"]:
            raise ValueError("Migrated database failed validation")
        memory.backup(destination)
        return {"destination":str(destination),"legacy_to_task_id":mapping,"messages":memory.stats()["messages"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("destination",type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate_v02(args.source,args.destination),indent=2))


if __name__ == "__main__":
    main()
