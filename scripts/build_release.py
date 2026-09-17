"""Build reproducible Python release archives and a checksum manifest.

The script uses the configured, locally installed PEP 517 backend. Native
executables are deliberately not compiled here: use ``build_native.py`` in each
target environment. A complete ``native/bin/windows`` build is packaged
deterministically and covered by ``SHA256SUMS``. Other target ZIPs placed beside
the Python artifacts are also included. An OpenPGP signature is optional and only
attempted when a maintainer explicitly supplies a signing key.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

import setuptools.build_meta

from build_portable_bundle import build_bundle
from verify_artifacts import (
    ArtifactMismatch,
    deterministic_zip_info,
    normalize_sdist,
    project_version,
    source_date_epoch,
    write_checksum_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _build_windows_native_bundle(root: Path, output: Path, version: str, epoch: int) -> Path | None:
    """Package a locally built Windows native set, avoiding stale target ZIPs."""

    binary_root = root / "native" / "bin" / "windows"
    names = ("cmpath-native.exe", "cmpath-bench.exe", "cmpath-embedded-example.exe")
    sources = [binary_root / name for name in names]
    if not any(path.exists() for path in sources):
        return None
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise ArtifactMismatch(["incomplete Windows native build: " + ", ".join(missing)])
    extras = [(root / "native" / "README.md", "README.md"),
              (root / "LICENSE", "LICENSE"), (root / "NOTICE.md", "NOTICE.md")]
    destination = output / f"cmpath-native-{version}-windows-amd64.zip"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=output,
                                         prefix=f".{destination.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=9) as archive:
                for source, name in sorted(
                    [(path, path.name) for path in sources] + extras,
                    key=lambda item: item[1],
                ):
                    archive.writestr(deterministic_zip_info(name, epoch=epoch),
                                     source.read_bytes())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def _sign_manifest(
    manifest: Path,
    *,
    key: str,
    gpg: str = "gpg",
) -> Path:
    """Create a detached ASCII-armored signature using an existing private key."""

    executable = shutil.which(gpg) or (gpg if Path(gpg).is_file() else None)
    if executable is None:
        raise ValueError(f"signature tool was not found: {gpg}")
    signature = manifest.with_name(manifest.name + ".asc")
    command = [
        executable, "--batch", "--yes", "--armor", "--detach-sign",
        "--local-user", key, "--output", str(signature), str(manifest),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise ValueError(f"could not execute signature tool {gpg}: {error}") from error
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ValueError(f"manifest signing failed{suffix}")
    if not signature.is_file():
        raise ValueError(f"signature tool did not create {signature}")
    return signature


def _release_artifacts(output: Path, version: str) -> list[Path]:
    """Select current-version release payloads without picking stale archives."""

    names = {
        f"cmpath-{version}-py3-none-any.whl",
        f"cmpath-{version}.tar.gz",
        f"cmpath-kimi-hermes-{version}.zip",
    }
    names.update(
        path.name for path in output.glob(f"cmpath-native-{version}-*.zip") if path.is_file()
    )
    return [output / name for name in sorted(names) if (output / name).is_file()]


def build_release(
    *,
    root: Path = ROOT,
    output: Path | None = None,
    portable: bool = True,
    epoch: int | str | None = None,
    signing_key: str | None = None,
    gpg: str = "gpg",
) -> dict[str, object]:
    """Build current-version artifacts and return their paths/checksum metadata."""

    root = Path(root).resolve()
    output = Path(output or root / "dist").resolve()
    output.mkdir(parents=True, exist_ok=True)
    timestamp = source_date_epoch(epoch)
    # Setuptools' wheel backend honors this variable. We additionally
    # normalize its sdist output below because older setuptools releases still
    # preserve source checkout mtimes in tar members.
    os.environ["SOURCE_DATE_EPOCH"] = str(timestamp)
    version = project_version(root)
    previous_cwd = Path.cwd()
    try:
        os.chdir(root)
        sdist_name = setuptools.build_meta.build_sdist(str(output))
        wheel_name = setuptools.build_meta.build_wheel(str(output))
    finally:
        os.chdir(previous_cwd)
    sdist = output / Path(str(sdist_name)).name
    wheel = output / Path(str(wheel_name)).name
    if not sdist.is_file() or not wheel.is_file():
        raise ArtifactMismatch([
            f"PEP 517 backend did not create expected artifacts: {sdist}, {wheel}"
        ])
    normalize_sdist(sdist, epoch=timestamp)

    portable_path: Path | None = None
    portable_root = root / "portable-kimi-hermes"
    if portable and portable_root.is_dir():
        portable_path = build_bundle(
            root=root,
            output=output / f"cmpath-kimi-hermes-{version}.zip",
            force=True,
            epoch=timestamp,
        )

    _build_windows_native_bundle(root, output, version, timestamp)

    artifacts = _release_artifacts(output, version)
    if not artifacts:
        raise ArtifactMismatch([f"no release artifacts were created in {output}"])
    manifest = write_checksum_manifest(artifacts, output / "SHA256SUMS")
    signature: Path | None = None
    if signing_key:
        signature = _sign_manifest(manifest, key=signing_key, gpg=gpg)
    result: dict[str, object] = {
        "version": version,
        "source_date_epoch": timestamp,
        "artifacts": [str(path) for path in artifacts],
        "checksums": str(manifest),
    }
    if signature is not None:
        result["signature"] = str(signature)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="artifact directory (default: dist)")
    parser.add_argument("--skip-portable", action="store_true",
                        help="build only wheel/sdist and do not write the portable ZIP")
    parser.add_argument("--source-date-epoch", type=int,
                        help="UTC timestamp for deterministic metadata (default: SOURCE_DATE_EPOCH or 0)")
    parser.add_argument("--signing-key",
                        help="optional GPG key ID/fingerprint for a detached SHA256SUMS signature")
    parser.add_argument("--gpg", default="gpg", help="GPG executable used for optional signing")
    args = parser.parse_args(argv)
    try:
        result = build_release(
            root=ROOT,
            output=args.output,
            portable=not args.skip_portable,
            epoch=args.source_date_epoch,
            signing_key=args.signing_key,
            gpg=args.gpg,
        )
    except (ArtifactMismatch, OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    for path in result["artifacts"]:
        print(path)
    print(result["checksums"])
    if "signature" in result:
        print(result["signature"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
