#!/usr/bin/env python3
"""Bounded live validation. Credentials come only from CMP_API_KEY.

No automatic retries or overwrite. Every dispatch has an fsynced request record;
raw provider responses and usage are retained independently of native journals.
The original local pilot script and its frozen results are left intact.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cmpath.agent import AgentConfig, ProviderResponse, ResearchAgent, _NoRedirect
from cmpath.harness import HarnessError, NativeHarness

DOCS = ("HARNESS.md", "RETENTION.md", "AGENT_PILOT.md")
QUESTIONS = (
    "Use the local document tools. Can the journal JSONL export restore an "
    "interrupted turn? What backup method is recommended? Cite supporting lines.",
    "Use the local document tools. What happens to a checkpointed model request "
    "without a saved response, and can retention remove pending turns or turns "
    "with unresolved started tools? Cite supporting lines from each document.",
)
CITATION = re.compile(r"(?<![\w:])T[1-9]\d*:M[1-9]\d*(?![\w:])")


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append(path, value):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RecordedHTTP:
    def __init__(self, config, output, *, use_env_proxy=False, limit=97):
        self.config, self.output = config, Path(output)
        self.limit, self.attempts, self.responses = limit, 0, 0
        self.http_bodies_received = 0
        self.use_env_proxy = use_env_proxy
        self.phase = "probe"
        self.identity = {}

    def __call__(self, raw):
        if self.attempts >= self.limit:
            raise ValueError("configured HTTP dispatch limit reached")
        self.attempts += 1
        record = {"attempt": self.attempts, "phase": self.phase,
                  **self.identity, "timestamp_utc": utc(),
                  "payload_json": raw.decode("utf-8")}
        append(self.output / "requests.jsonl", record)
        write_json(self.output / "transport_status.json", {
            "attempts": self.attempts, "responses": self.responses,
            "http_bodies_received": self.http_bodies_received,
            "status": "dispatch_pending_or_outcome_unknown"})
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.config.key_env)
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(self.config.endpoint, data=raw,
                                         headers=headers, method="POST")
        proxy = urllib.request.ProxyHandler() if self.use_env_proxy else urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy, _NoRedirect())
        start = time.monotonic()
        http_status = None
        try:
            with opener.open(request, timeout=self.config.timeout) as response:
                http_status = response.status
                data = response.read(8 * 1024 * 1024 + 1)
            self.http_bodies_received += 1
            receipt = {**{k: v for k, v in record.items() if k != "payload_json"},
                       "http_status": http_status, "received_bytes": len(data),
                       "elapsed_seconds": time.monotonic() - start,
                       "parse_status": "not_yet_parsed"}
            raw_json = None
            if len(data) > 8 * 1024 * 1024:
                receipt["body_omitted"] = "size_limit; received_bytes_is_lower_bound"
            elif key and key.encode() in data:
                receipt["body_omitted"] = "credential_echo"
            else:
                try:
                    raw_json = data.decode("utf-8", errors="strict")
                    receipt["response_json"] = raw_json
                except UnicodeError:
                    receipt["body_base64"] = base64.b64encode(data).decode("ascii")
            # Save receipt before parsing: malformed or non-object JSON may
            # still represent a potentially billed provider response.
            append(self.output / "http_receipts.jsonl", receipt)
            if "body_omitted" in receipt or raw_json is None:
                raise ValueError("provider body withheld, oversized, or not UTF-8; inspect receipt")
            value = ProviderResponse(raw_json)
        except Exception as exc:
            error = {"attempt": self.attempts, "phase": self.phase,
                     "error_type": type(exc).__name__,
                     "elapsed_seconds": time.monotonic() - start,
                     "status": "stopped_transport_error_no_retry"}
            if http_status is not None:
                error["http_status"] = http_status
            if isinstance(exc, urllib.error.HTTPError):
                error["http_status"] = exc.code
                body = exc.read(4096).decode("utf-8", errors="replace")
                error["body_excerpt"] = body.replace(key, "[REDACTED]") if key else body
            append(self.output / "errors.jsonl", error)
            write_json(self.output / "transport_status.json", {
                **error, "attempts": self.attempts, "responses": self.responses,
                "http_bodies_received": self.http_bodies_received})
            raise
        self.responses += 1
        append(self.output / "responses.jsonl", {
            **{k: v for k, v in record.items() if k != "payload_json"},
            "elapsed_seconds": time.monotonic() - start,
            "response_json": value.raw_json, "provider_usage": value.get("usage")})
        write_json(self.output / "transport_status.json", {
            "attempts": self.attempts, "responses": self.responses,
            "http_bodies_received": self.http_bodies_received,
            "status": "response_recorded"})
        print(json.dumps({"phase": self.phase, "attempts": self.attempts,
                          "responses": self.responses}), flush=True)
        return value


def payload_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def probe(transport):
    transport.phase, transport.identity = "probe", {}
    response = transport(payload_bytes({"model": transport.config.model,
        "messages": [{"role": "user", "content": "Reply with exactly CMP_OK."}],
        "temperature": 0, "tools": [], "max_tokens": 200}))
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop" or choice["message"].get("content", "").strip() != "CMP_OK":
        raise ValueError("probe did not finish with CMP_OK; inspect response before further calls")


def fixed_reader(transport, source, output):
    # Labels are not opened until all planned model requests have finished.
    prompts = rows(source / "prompts.jsonl")
    cases = list(dict.fromkeys(p["case_id"] for p in prompts))
    if len(cases) != 40 or len(prompts) != 80:
        raise ValueError("expected the complete 40-case, 80-request paired pilot")
    condition_names = {"cmp_native", "sqlite_bm25_task_filter"}
    if any({p["condition"] for p in prompts if p["case_id"] == case} != condition_names for case in cases):
        raise ValueError("each case must contain exactly the two declared conditions")
    prompts.sort(key=lambda p: (cases.index(p["case_id"]),
        (p["condition"] == "cmp_native") == (cases.index(p["case_id"]) % 2 == 0)))
    answers = []
    for prompt in prompts:
        transport.phase = "fixed_reader"
        transport.identity = {k: prompt[k] for k in ("case_id", "condition")}
        raw = payload_bytes({"model": transport.config.model, "messages": prompt["messages"],
                             "temperature": 0, "tools": [], "max_tokens": 200})
        units = (len(raw.decode()) + 3) // 4 + 8
        if units > 1800:
            raise ValueError("complete fixed-reader input exceeds 1800 estimated units")
        response = transport(raw)
        choice = (response.get("choices") or [{}])[0]
        text = choice.get("message", {}).get("content") or ""
        if not isinstance(text, str):
            raise ValueError("assistant content must be text or null")
        answers.append({**transport.identity, "text": text,
                        "finish_reason": choice.get("finish_reason"),
                        "estimated_request_units": units})
    labels = {r["case_id"]: r["required_citations"] for r in rows(source / "labels.jsonl")}
    for answer in answers:
        found = set(CITATION.findall(answer["text"]))
        answer.update(required_exact_citations_present=set(labels[answer["case_id"]]) <= found,
                      answer_correctness="requires_semantic_review")
        append(output / "fixed_reader_answers.jsonl", answer)
    return {"case_count": len(cases), "responses": len(answers),
            "answer_correctness": "requires_semantic_review",
            "conditions": {name: {
                "required_exact_citations_present": sum(a["required_exact_citations_present"] for a in answers if a["condition"] == name),
                "empty_answers": sum(not a["text"].strip() for a in answers if a["condition"] == name),
                "truncated_answers": sum(a["finish_reason"] == "length" for a in answers if a["condition"] == name),
            } for name in sorted(condition_names)}}


class InjectedPause(RuntimeError):
    pass


class ObservedHarness(NativeHarness):
    def __init__(self, *args, events, pause=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.events, self.pause = events, pause

    def wrap(self, session):
        original_checkpoint, original_tool = session.checkpoint_model_request, session.tool

        def checkpoint(call_id, payload, **kwargs):
            if self.pause and call_id == "agent-model-2":
                raise InjectedPause("controlled pause before second model checkpoint")
            return original_checkpoint(call_id, payload, **kwargs)

        def tool(call_id, name, arguments, execute):
            def observed():
                append(self.events, {"request_id": session.turn["request_id"],
                    "call_id": call_id, "name": name, "timestamp_utc": utc()})
                return execute()
            return original_tool(call_id, name, arguments, observed)

        session.checkpoint_model_request, session.tool = checkpoint, tool
        return session

    def begin(self, *args, **kwargs):
        return self.wrap(super().begin(*args, **kwargs))

    def recover(self, *args, **kwargs):
        return self.wrap(super().recover(*args, **kwargs))


def agent_checks(transport, binary, output):
    workspace = output / "workspace"
    (workspace / "docs").mkdir(parents=True)
    for name in DOCS:
        shutil.copyfile(ROOT / "docs" / name, workspace / "docs" / name)
    source_hashes = {str(p.relative_to(workspace)): digest(p) for p in workspace.rglob("*.md")}
    write_json(output / "workspace_sources.json", source_hashes)
    events, database = output / "tool_executions.jsonl", output / "agent.db"
    results = []
    for number, question in enumerate(QUESTIONS, 1):
        request_id = f"live-agent-{number}"
        transport.phase, transport.identity = "agent", {"request_id": request_id}
        config = AgentConfig(endpoint=transport.config.endpoint, model=transport.config.model,
            request_id=request_id, workspace=workspace, max_tokens=1000,
            max_turns=8, budget=16000, timeout=transport.config.timeout)
        paused = False
        with ObservedHarness(binary, database, create=number == 1,
                             events=events, pause=number == 2) as harness:
            task = harness.create_task(f"Live document research {number}")
            task_id = task["id"]
            agent = ResearchAgent(harness, config, transport=transport)
            try:
                reply = agent.run(task_id, question)
            except InjectedPause:
                paused = True
                before = harness.inspect(request_id)
                saved_models = harness.model_calls(request_id)
                saved_tools = harness.backend.call("tools", request_id=request_id)
        recovery = {"controlled_pause_exercised": paused}
        if paused:
            event_count = len(rows(events)) if events.exists() else 0
            with ObservedHarness(binary, database, events=events) as harness:
                agent = ResearchAgent(harness, config, transport=transport)
                reply = agent.run(task_id, question, resume=True)
                current = harness.inspect(request_id)
                models = harness.model_calls(request_id)
                tool_ids = {r["call_id"] for r in saved_tools}
                new_events = rows(events)[event_count:]
                recovery.update(
                    generation_increased=current["generation"] > before["generation"],
                    saved_responses_unchanged=models[:len(saved_models)] == saved_models,
                    completed_tools_not_reexecuted=not any(r["call_id"] in tool_ids for r in new_events),
                    confirmed_tools_before_pause=len(saved_tools),
                    confirmed_models_before_pause=len(saved_models))
        with NativeHarness(binary, database) as harness:
            before_models = harness.model_calls(request_id)
            before_tools = harness.backend.call("tools", request_id=request_id)
            def forbid_dispatch(raw):
                raise AssertionError("committed replay attempted provider dispatch")
            replay = ResearchAgent(harness, config, transport=forbid_dispatch)
            replay_reply = replay.run(task_id, question)
            checks = {"reply_identical": replay_reply == reply,
                      "zero_replay_dispatches": replay.dispatch_count == 0,
                      "model_records_unchanged": harness.model_calls(request_id) == before_models,
                      "tool_records_unchanged": harness.backend.call("tools", request_id=request_id) == before_tools}
            successful_reads = sum(r["name"] in {"read_document", "search_documents"}
                and r["status"] == "completed" and "error" not in (r.get("result") or {}) for r in before_tools)
            result = {"request_id": request_id, "question": question,
                "reply": reply, "successful_document_tool_calls": successful_reads,
                "committed_replay": checks, "recovery": recovery,
                "answer_and_citation_support": "requires_semantic_review"}
            results.append(result)
            write_json(output / "agent_results.json", results)
            harness.export_journal(output / f"journal-after-{number}.jsonl")
    unchanged = source_hashes == {str(p.relative_to(workspace)): digest(p) for p in workspace.rglob("*.md")}
    if not unchanged:
        raise ValueError("document workspace changed during evaluation")
    return {"tasks": results, "document_workspace_unchanged": unchanged}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://ai.sumopod.com/v1/chat/completions")
    parser.add_argument("--model", default="glm-5.3-flash")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("probe", "fixed", "agent", "all"), default="all")
    parser.add_argument("--use-env-proxy", action="store_true", help="Explicitly use the runtime proxy; no fallback")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--native-binary", type=Path, default=ROOT / "native/bin/cmpath-native")
    args = parser.parse_args()
    if not os.environ.get("CMP_API_KEY"):
        parser.error("set CMP_API_KEY in the environment; never pass credentials as command arguments")
    config = AgentConfig(endpoint=args.endpoint, model=args.model, request_id="live-validation",
                         workspace=ROOT, timeout=args.timeout)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    planned = {"probe": 1, "fixed": 81, "agent": 17, "all": 97}[args.stage]
    metadata = {"started_utc": utc(), "endpoint": args.endpoint, "model": args.model,
        "stage": args.stage, "maximum_dispatches": planned,
        "proxy_route": "environment_explicit" if args.use_env_proxy else "direct",
        "retry_policy": "none; existing output refused", "status": "started",
        "hashes": {str(p.relative_to(ROOT)): digest(p) for p in [Path(__file__),
            ROOT / "research/LIVE_PROTOCOL.md", ROOT / "results/pilot/prompts.jsonl",
            ROOT / "results/pilot/labels.jsonl", ROOT / "results/pilot/summary.json"]}}
    write_json(args.output / "summary.json", metadata)
    transport = RecordedHTTP(config, args.output, use_env_proxy=args.use_env_proxy, limit=planned)
    exit_code = 0
    try:
        probe(transport)
        if args.stage in {"fixed", "all"}:
            metadata["fixed_reader"] = fixed_reader(transport, ROOT / "results/pilot", args.output)
        if args.stage in {"agent", "all"}:
            metadata["agent"] = agent_checks(transport, args.native_binary.resolve(), args.output)
        metadata["status"] = "completed_review_required" if args.stage != "probe" else "probe_passed"
    except Exception as exc:
        # Do not print exception text, response headers, proxy values, or credentials.
        metadata.update(status="stopped_no_automatic_retry", error_type=type(exc).__name__)
        if isinstance(exc, HarnessError):
            metadata["harness_error_code"] = exc.code
        exit_code = 1
    metadata.update(finished_utc=utc(), dispatch_attempts=transport.attempts,
                    provider_responses_received=transport.responses,
                    successful_http_bodies_received=transport.http_bodies_received,
                    billing_cost="unknown; use actual provider invoice")
    write_json(args.output / "summary.json", metadata)
    print(json.dumps(metadata, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
