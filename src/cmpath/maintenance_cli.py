"""Inspect, export and explicitly retire native execution journals."""
import argparse
import json
from pathlib import Path

from .harness import NativeHarness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60,
                        help="Native maintenance deadline in seconds")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    export = commands.add_parser("export")
    export.add_argument("destination", type=Path, help="New JSONL file; never overwrites")
    plan = commands.add_parser("plan")
    plan.add_argument("--before", required=True, help="RFC3339 timestamp with timezone")
    apply = commands.add_parser("apply")
    apply.add_argument("--before", required=True)
    apply.add_argument("--plan-hash", required=True, help="Exact hash returned by plan")
    args = parser.parse_args()
    with NativeHarness(args.binary, args.db, timeout=args.timeout) as harness:
        if args.command == "info":
            result = harness.backend.call("info")
        elif args.command == "export":
            result = harness.export_journal(args.destination)
        elif args.command == "plan":
            result = harness.retention_plan(args.before)
        else:
            result = harness.apply_retention(args.before, args.plan_hash)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
