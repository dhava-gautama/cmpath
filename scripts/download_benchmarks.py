"""Fetch pinned public data and verify its complete byte hash.

Datasets retain their own licenses; see NOTICE.md. They are not package assets.
"""
import argparse
import hashlib
from pathlib import Path
import os
import tempfile
import urllib.request

DATA = [
    ("longmemeval_s_cleaned.json",
     "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json",
     "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"),
    ("locomo10.json",
     "https://raw.githubusercontent.com/snap-research/locomo/3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376/data/locomo10.json",
     "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("data"))
    parser.add_argument("--dataset",choices=["both","longmemeval","locomo"],default="both")
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    for name,url,expected in DATA:
        if args.dataset != "both" and not name.startswith(args.dataset):
            continue
        destination = args.output/name
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected:
            print("Verified existing",destination)
            continue
        fd,temporary = tempfile.mkstemp(prefix=name+".",dir=args.output)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd,"wb") as output, urllib.request.urlopen(url,timeout=90) as response:
                while chunk := response.read(1024*1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Source hash mismatch for {name}; refusing changed bytes")
            os.replace(temporary,destination)
            print("Downloaded and verified",destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


if __name__ == "__main__":
    main()
