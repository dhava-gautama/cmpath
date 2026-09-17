"""Connect the native memory harness to a caller-configured text generation endpoint.

This example makes an actual model request only when invoked with an endpoint,
model, question and logical request ID. The research study did not invoke it.
"""
import argparse
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from cmpath import NativeHarness


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path("native-chat.db"))
    parser.add_argument("--task", type=int)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--endpoint", required=True, help="Full Chat Completions URL")
    parser.add_argument("--model", required=True)
    parser.add_argument("--key-env", default="CMP_API_KEY")
    parser.add_argument("--budget", type=int, default=8000)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    url = urllib.parse.urlparse(args.endpoint)
    local = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if not url.hostname or (url.scheme != "https" and not local) or url.username or url.password or url.query or url.fragment:
        parser.error("Use HTTPS or loopback HTTP, with no credentials, query or fragment in the URL")
    if args.max_tokens < 1 or args.budget <= args.max_tokens or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("Use positive token/time limits and a budget above the output allowance")
    opener = urllib.request.build_opener(NoRedirect)
    with NativeHarness(args.binary, args.db, create=True) as harness:
        if args.task is not None:
            task = harness.task(args.task)
        else:
            route = harness.resolve("Chat workflow")
            if route["status"] == "resolved":
                task = harness.task(route["task_id"])
            elif route["status"] == "not_found":
                task = harness.create_task("Chat workflow")
            else:
                parser.error("Specify --task because the chat task name is ambiguous")

        def complete(session):
            payload = {"model": args.model, "messages": session.messages,
                       "max_tokens": args.max_tokens, "temperature": 0}
            body = session.checkpoint_model_request("answer-1", payload)
            headers = {"Content-Type": "application/json"}
            if os.environ.get(args.key_env):
                headers["Authorization"] = "Bearer " + os.environ[args.key_env]
            request = urllib.request.Request(args.endpoint, data=body, headers=headers, method="POST")
            with opener.open(request, timeout=args.timeout) as response:
                result = json.load(response)
            text = result["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("The text-only example requires a nonempty assistant response")
            state = session.snapshot
            state.update({"last_model": result.get("model", args.model), "last_usage": result.get("usage")})
            return {"text": text, "snapshot": state}

        reply = harness.run(args.request_id, task["id"], args.question, complete,
                            model_key=args.endpoint + "|" + args.model,
                            system="Use the supplied source evidence and identify uncertainty.",
                            budget=args.budget, reserve=args.max_tokens)
        print(json.dumps({"task_id": task["id"], "request_id": args.request_id, "reply": reply}, indent=2))


if __name__ == "__main__":
    main()
