"""Build the portable Kimi/Hermes adapter bundle for the current source."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import zipfile

from verify_artifacts import (
    ROOT,
    _portable_expected,
    deterministic_zip_info,
    project_version,
    source_date_epoch,
)


def build_bundle(
    *,
    root: Path = ROOT,
    output: Path | None = None,
    force: bool = False,
    epoch: int | str | None = None,
) -> Path:
    """Write a deterministic portable bundle and return its absolute path."""

    root = Path(root).resolve()
    version = project_version(root)
    destination = Path(output or root / "dist" / f"cmpath-kimi-hermes-{version}.zip").resolve()
    if destination.exists() and not force:
        raise FileExistsError(
            f"refusing to replace existing output: {destination}; use --force explicitly"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = _portable_expected(root)
    timestamp = source_date_epoch(epoch)
    temporary: Path | None = None
    try:
        # Publish by replacement only after the ZIP has closed successfully;
        # interrupted builds therefore do not leave a partial release.
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=f".{destination.name}.",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
            ) as archive:
                for name, data in sorted(files.items()):
                    executable = name.endswith("/install.sh")
                    archive.writestr(
                        deterministic_zip_info(
                            name, epoch=timestamp, executable=executable
                        ),
                        data,
                    )
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="output ZIP path")
    parser.add_argument("--force", action="store_true", help="replace an existing output")
    parser.add_argument("--source-date-epoch", type=int,
                        help="UTC timestamp for deterministic ZIP metadata (default: SOURCE_DATE_EPOCH or 0)")
    args = parser.parse_args(argv)
    try:
        output = build_bundle(
            root=ROOT, output=args.output, force=args.force,
            epoch=args.source_date_epoch,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
