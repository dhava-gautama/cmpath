"""Run or inspect a provider-configured, local-document research agent."""
import argparse
import json
from pathlib import Path

from cmpath.harness import NativeHarness
from cmpath.agent import AgentConfig, ResearchAgent
from cmpath.request_counter import LocalTokenCounter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--create", action="store_true", help="Explicitly allow database creation")
    parser.add_argument("--task", type=int, help="Existing task ID")
    parser.add_argument("--new-task", help="Explicitly create a task with this title")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--endpoint", help="Full Chat Completions URL")
    parser.add_argument("--model")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--question")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--unknown-outcome-policy", choices=["error", "retry"], default="error")
    parser.add_argument("--consistency", choices=["snapshot", "scope"], default="snapshot",
                        help="Scope rejects new work when the captured memory scope changes")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--budget", type=int, default=16000)
    parser.add_argument("--max-tokens", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--key-env", default="CMP_API_KEY")
    parser.add_argument("--counter-command", help="JSON argv for a locally installed request counter")
    parser.add_argument("--counter-name", help="Explicit accounting scheme name")
    args = parser.parse_args()
    if bool(args.counter_command) != bool(args.counter_name):
        parser.error("--counter-command and --counter-name must be supplied together")
    counter = None
    if args.counter_command:
        counter = LocalTokenCounter(json.loads(args.counter_command), name=args.counter_name)
    with NativeHarness(args.binary, args.db, create=args.create) as harness:
        if args.inspect:
            print(json.dumps({"turn": harness.inspect(args.request_id),
                              "model_calls": harness.model_calls(args.request_id),
                              "tools": harness.backend.call("tools", request_id=args.request_id)}, indent=2))
            return
        if not args.endpoint or not args.model or not args.workspace:
            parser.error("Running requires --endpoint, --model and --workspace")
        config = AgentConfig(args.endpoint, args.model, args.request_id, args.workspace,
                             max_turns=args.max_turns, budget=args.budget, max_tokens=args.max_tokens,
                             timeout=args.timeout, key_env=args.key_env,
                             counting_scheme=args.counter_name or "estimated-json",
                             consistency=args.consistency)
        if not args.question or (args.task is None) == (args.new_task is None):
            parser.error("Supply --question and exactly one of --task or --new-task")
        if args.resume and args.new_task:
            parser.error("Resume requires the existing --task")
        task = harness.task(args.task) if args.task is not None else harness.create_task(args.new_task)
        agent = ResearchAgent(harness, config, counter=counter)
        reply = agent.run(task["id"], args.question, resume=args.resume,
                          unknown_outcome_policy=args.unknown_outcome_policy)
        print(json.dumps({"task_id": task["id"], "request_id": args.request_id,
                          "dispatches_this_process": agent.dispatch_count, "reply": reply}, indent=2))


if __name__ == "__main__":
    main()
