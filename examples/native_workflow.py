"""Execute a real file-checksum tool through the durable Python/Go harness."""
import argparse
import hashlib
import json
from pathlib import Path

from cmpath import NativeHarness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path("native-workflow.db"))
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    path = args.file.resolve()
    with NativeHarness(args.binary, args.db, create=True) as harness:
        route = harness.resolve("File integrity workflow")
        if route["status"] == "resolved":
            task = harness.task(route["task_id"])
        elif route["status"] == "not_found":
            task = harness.create_task("File integrity workflow", snapshot={"next_action": "compute checksum"})
        else:
            raise RuntimeError("The file integrity task is ambiguous")

        def complete(session):
            def checksum():
                data = path.read_bytes()
                return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            result = session.tool("file-hash", "sha256_file", {"path": str(path)}, checksum)
            return {"text": json.dumps(result), "snapshot": {"next_action": "review checksum", "file": str(path)}}

        reply = harness.run(args.request_id, task["id"], "Calculate the SHA-256 of " + str(path), complete)
        print(json.dumps(reply, indent=2))


if __name__ == "__main__":
    main()
