"""Run CMP's authenticated managed-turn control plane."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .control_plane import DEFAULT_MAX_BODY_BYTES, ControlPlane, ControlPlaneServer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-body-bytes", type=int, default=DEFAULT_MAX_BODY_BYTES)
    parser.add_argument("--token-env", default="CMPATH_CONTROL_PLANE_TOKEN")
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    plane = ControlPlane.from_native(
        args.binary, args.db, create=args.create, timeout=args.timeout, auth_token=token
    )
    server = ControlPlaneServer(
        plane, host=args.host, port=args.port, auth_token=token,
        max_body_bytes=args.max_body_bytes, close_service=True,
    )
    try:
        print(f"CMP control plane listening on {server.base_url}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        plane.close()


if __name__ == "__main__":
    main()
