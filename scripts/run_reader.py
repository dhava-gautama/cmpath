"""Send exported benchmark prompts to a caller-configured Chat Completions API.

Only `messages`, model and generation parameters enter the request body.
Expected answers stay in the original dataset and are never sent here.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url,code,"Redirect refused",headers,fp)


def request_body(row,model,max_tokens,temperature):
    messages = row["messages"]
    if not isinstance(messages,list) or not messages:
        raise ValueError("A nonempty messages list is required")
    for message in messages:
        if set(message) != {"role","content"} or message["role"] not in ("system","user","assistant") or not isinstance(message["content"],str):
            raise ValueError("Unexpected prompt message schema")
    return {"model":model,"messages":messages,"temperature":temperature,"max_tokens":max_tokens}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompts",type=Path)
    parser.add_argument("output",type=Path)
    parser.add_argument("--endpoint",required=True,help="Full /chat/completions URL")
    parser.add_argument("--model",required=True)
    parser.add_argument("--key-env",default="CMP_API_KEY")
    parser.add_argument("--max-tokens",type=int,default=256)
    parser.add_argument("--temperature",type=float,default=0.0)
    parser.add_argument("--timeout",type=float,default=120.0)
    parser.add_argument("--limit",type=int)
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.endpoint)
    if parsed.scheme != "https" and not (parsed.scheme=="http" and parsed.hostname in ("localhost","127.0.0.1","::1")):
        parser.error("Use HTTPS, or HTTP on a loopback endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Do not put credentials, query strings or fragments in the endpoint URL")
    if args.max_tokens <= 0 or args.timeout <= 0 or (args.limit is not None and args.limit<=0):
        parser.error("Token, timeout and limit values must be positive")
    headers = {"Content-Type":"application/json"}
    if os.environ.get(args.key_env):
        headers["Authorization"] = "Bearer " + os.environ[args.key_env]
    opener = urllib.request.build_opener(NoRedirect)
    completed = 0
    # Exclusive output creation prevents a partial run being silently replaced.
    with args.prompts.open() as source, args.output.open("x",encoding="utf-8") as output:
        for index,line in enumerate(source):
            if args.limit is not None and index >= args.limit:
                break
            row = json.loads(line)
            body = request_body(row,args.model,args.max_tokens,args.temperature)
            encoded = json.dumps(body,ensure_ascii=False).encode()
            started = time.perf_counter()
            record = {key:row[key] for key in ("dataset","question_id","method")}
            record.update({"requested_model":args.model,"request_sha256":hashlib.sha256(encoded).hexdigest(),"temperature":args.temperature,"max_tokens":args.max_tokens})
            try:
                request = urllib.request.Request(args.endpoint,data=encoded,headers=headers,method="POST")
                with opener.open(request,timeout=args.timeout) as response:
                    result = json.load(response)
                record.update({"model":result.get("model",args.model),"hypothesis":result["choices"][0]["message"]["content"],"usage":result.get("usage"),"status":"ok"})
                completed += 1
            except urllib.error.HTTPError as exc:
                record.update({"status":"error","error":f"HTTP {exc.code}"})
            except (urllib.error.URLError,TimeoutError,ValueError,KeyError,IndexError,TypeError) as exc:
                record.update({"status":"error","error":type(exc).__name__})
            record["elapsed_seconds"] = time.perf_counter()-started
            output.write(json.dumps(record,ensure_ascii=False)+"\n")
            output.flush()
    print(json.dumps({"completed":completed,"output":str(args.output)}))


if __name__ == "__main__":
    main()
