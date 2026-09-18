"""Verify release archives are byte-for-byte views of the source checkout.

An install smoke test can pass when a wheel was built from an older checkout.
This module compares wheel, source-distribution, portable-adapter, optional
native ZIP and (when supplied) Codex plugin payloads with canonical source.
It can also verify a generated SHA-256 manifest and optional detached
signature. It is read-only: no archive is extracted and no generated metadata
is rewritten.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as _datetime
import fnmatch
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None
from typing import Mapping, Sequence
import zipfile


ROOT = Path(__file__).resolve().parents[1]
_GENERATED_SDIST_FILES = {"PKG-INFO", "setup.cfg"}
_GENERATED_SDIST_PREFIXES = ("src/cmpath.egg-info/",)
_PORTABLE_TOP_LEVEL = ("LICENSE", "NOTICE.md", "README.md", "pyproject.toml")
_PLUGIN_MANIFEST = ".codex-plugin/plugin.json"
_CHECKSUM_NAME = "SHA256SUMS"
_SIGNATURE_SUFFIX = ".asc"
_PE_SIGNATURE = b"PE\0\0"
_ZIP_EPOCH = 315532800  # 1980-01-01T00:00:00Z, the earliest ZIP timestamp.


class ArtifactMismatch(RuntimeError):
    """Raised when an artifact cannot be reconciled with canonical source."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def project_version(root: Path = ROOT) -> str:
    """Return the version declared by canonical pyproject.toml."""

    project_file = root / "pyproject.toml"
    if tomllib is not None:
        with project_file.open("rb") as stream:
            return str(tomllib.load(stream)["project"]["version"])
    # Python 3.10 has no stdlib TOML reader and the core package deliberately
    # has no runtime dependencies.  Parse only the simple, required field used
    # by this verifier rather than silently adding a packaging dependency.
    section = None
    for line in project_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section == "project":
            match = re.fullmatch(r"version\s*=\s*(['\"])([^'\"]+)\1\s*", stripped)
            if match:
                return match.group(2)
    raise ValueError(f"project.version is missing from {project_file}")


def source_date_epoch(value: int | str | None = None) -> int:
    """Return a validated reproducible-build timestamp.

    ``SOURCE_DATE_EPOCH`` is the standard opt-in build reproducibility
    variable.  Release helpers use epoch zero when it is not supplied so a
    local build does not silently acquire the wall-clock time.  A caller can
    still choose a meaningful project/release timestamp explicitly.
    """

    raw = os.environ.get("SOURCE_DATE_EPOCH", "0") if value is None else value
    try:
        epoch = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"SOURCE_DATE_EPOCH must be an integer, got {raw!r}") from exc
    if epoch < 0:
        raise ValueError(f"SOURCE_DATE_EPOCH must be non-negative, got {epoch}")
    return epoch


def _checksum_relative(path: Path, manifest: Path) -> str:
    """Return a safe POSIX path relative to a checksum manifest."""

    base = manifest.resolve().parent
    candidate = Path(path).resolve()
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise ArtifactMismatch([
            f"checksum target is outside manifest directory: {path}"
        ]) from exc
    return relative.as_posix()


def write_checksum_manifest(
    paths: Sequence[Path],
    output: Path,
) -> Path:
    """Write a deterministic GNU-compatible SHA-256 manifest.

    Entries are sorted by their POSIX path and use the conventional two-space
    separator.  The manifest itself is intentionally not included; this
    keeps a detached signature over the manifest feasible and avoids a
    self-referential hash.
    """

    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, str] = {}
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise ArtifactMismatch([f"checksum target is missing: {path}"])
        name = _checksum_relative(path, output)
        if name == output.name:
            continue
        if name in entries:
            raise ArtifactMismatch([f"duplicate checksum target: {name}"])
        entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    text = "".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items()))
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return output


def _read_checksum_manifest(path: Path) -> dict[str, str]:
    """Parse a SHA-256 manifest and reject ambiguous/path-traversal entries."""

    path = Path(path).resolve()
    if not path.is_file():
        raise ArtifactMismatch([f"missing checksum manifest: {path}"])
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ArtifactMismatch([f"cannot read checksum manifest {path}: {exc}"]) from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9A-Fa-f]{64})\s+(\*?)(.+?)\s*", line)
        if match is None:
            raise ArtifactMismatch([f"invalid checksum manifest line {number}"])
        digest, marker, raw_name = match.groups()
        name = raw_name.strip().replace("\\", "/")
        if not name:
            raise ArtifactMismatch([f"empty checksum target on line {number}"])
        normalized = PurePosixPath(name)
        # ``PurePosixPath`` does not treat a Windows drive as absolute on a
        # POSIX host, so reject drive-qualified names explicitly as well.
        if (
            normalized.is_absolute()
            or len(name) >= 2 and name[1] == ":"
            or ".." in normalized.parts
        ):
            raise ArtifactMismatch([f"unsafe checksum target on line {number}: {raw_name}"])
        name = "/".join(part for part in normalized.parts if part not in ("", "."))
        if not name:
            raise ArtifactMismatch([f"empty checksum target on line {number}"])
        if name in entries:
            raise ArtifactMismatch([f"duplicate checksum target: {name}"])
        entries[name] = digest.lower()
    if not entries:
        raise ArtifactMismatch([f"checksum manifest has no entries: {path}"])
    return entries


def verify_checksum_manifest(
    manifest: Path,
    *,
    required: Sequence[Path] = (),
) -> dict[str, object]:
    """Verify every manifest entry and require selected artifacts to be listed."""

    manifest = Path(manifest).resolve()
    entries = _read_checksum_manifest(manifest)
    errors: list[str] = []
    base = manifest.parent
    for name, expected in sorted(entries.items()):
        target = (base / Path(*name.split("/"))).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            errors.append(f"checksum target escapes manifest directory: {name}")
            continue
        if not target.is_file():
            errors.append(f"checksum target is missing: {name}")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"checksum mismatch for {name}: {actual} != {expected}")
    for raw_path in required:
        try:
            name = _checksum_relative(Path(raw_path), manifest)
        except ArtifactMismatch as exc:
            errors.extend(exc.errors)
            continue
        if name not in entries:
            errors.append(f"checksum manifest omits required artifact: {name}")
    if errors:
        raise ArtifactMismatch(errors)
    return {"path": str(manifest), "ok": True, "entries": len(entries)}


def verify_detached_signature(
    manifest: Path,
    signature: Path,
    *,
    gpg: str = "gpg",
) -> dict[str, object]:
    """Verify an optional detached OpenPGP signature without requiring a key to build."""

    manifest = Path(manifest).resolve()
    signature = Path(signature).resolve()
    if not signature.is_file():
        raise ArtifactMismatch([f"missing detached signature: {signature}"])
    executable = shutil.which(gpg) or (gpg if Path(gpg).is_file() else None)
    if executable is None:
        raise ArtifactMismatch([f"signature verifier was not found: {gpg}"])
    result = subprocess.run(
        [executable, "--batch", "--verify", str(signature), str(manifest)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ArtifactMismatch([f"detached signature verification failed{suffix}"])
    return {"path": str(signature), "ok": True, "algorithm": "openpgp"}


def _portable(path: Path) -> str:
    return path.as_posix()


def _is_ignored(path: Path, *, include_generated: bool = False) -> bool:
    """Exclude generated/cache state that cannot be a release source file."""

    parts = set(path.parts)
    if "__pycache__" in parts or path.suffix == ".pyc":
        return True
    if path.name.endswith((".db", ".db-wal", ".db-shm")):
        return True
    if not include_generated and "dist" in parts:
        return True
    return False


def _source_files(root: Path, *, include_generated: bool = False) -> dict[str, bytes]:
    """Read regular source files below root using POSIX relative paths."""

    result: dict[str, bytes] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or _is_ignored(relative, include_generated=include_generated):
            continue
        result[_portable(relative)] = path.read_bytes()
    return result


def _manifest_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def _matches(path: str, pattern: str) -> bool:
    """Match a manifest pattern against a POSIX relative path."""

    pattern = _manifest_path(pattern)
    path = _manifest_path(path)
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)


def manifest_files(root: Path = ROOT) -> dict[str, bytes]:
    """Resolve the source payload selected by MANIFEST.in.

    Setuptools writes generated SOURCES.txt beside the package. An old copy of
    that file is precisely the stale-build problem this verifier catches, so a
    small resolver keeps checks independent of generated state.
    """

    all_files = _source_files(root)
    manifest = root / "MANIFEST.in"
    if not manifest.is_file():
        raise ArtifactMismatch([f"missing canonical source: {manifest}"])
    # Setuptools always includes files belonging to discovered packages even
    # when MANIFEST.in has no explicit src/ rule. Keep that behaviour here so
    # a source archive with an old package payload is reported as stale.
    selected: set[str] = {
        "MANIFEST.in",
        "pyproject.toml",
        *(path for path in all_files if path.startswith("src/cmpath/")),
    }
    lines = [line.split("#", 1)[0].strip() for line in manifest.read_text(encoding="utf-8").splitlines()]
    for line in lines:
        if not line:
            continue
        fields = line.split()
        command, values = fields[0], fields[1:]
        if command == "include":
            selected.update(
                path for path in all_files
                if any(_matches(path, pattern) for pattern in values)
            )
        elif command == "recursive-include" and values:
            directory, patterns = _manifest_path(values[0]), values[1:]
            prefix = directory.rstrip("/") + "/"
            for path in all_files:
                if path.startswith(prefix):
                    relative = path[len(prefix):]
                    if any(_matches(relative, pattern) for pattern in patterns):
                        selected.add(path)
        elif command == "graft" and values:
            directory = _manifest_path(values[0]).rstrip("/")
            prefix = directory + "/"
            selected.update(path for path in all_files if path.startswith(prefix))

    excluded: set[str] = set()
    for line in lines:
        if not line:
            continue
        fields = line.split()
        command, values = fields[0], fields[1:]
        if command == "prune" and values:
            directory = _manifest_path(values[0]).rstrip("/") + "/"
            excluded.update(path for path in selected if path.startswith(directory))
        elif command == "exclude":
            excluded.update(
                path for path in selected
                if any(_matches(path, pattern) for pattern in values)
            )
        elif command == "global-exclude":
            excluded.update(
                path for path in selected
                if any(_matches(path, pattern) for pattern in values)
            )
    selected -= excluded
    selected = {path for path in selected if path in all_files}
    return {path: all_files[path] for path in sorted(selected)}


def _safe_member_name(name: str) -> str:
    """Normalize an archive member, rejecting traversal and absolute paths."""

    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ArtifactMismatch([f"unsafe archive member: {name}"])
    return "/".join(part for part in path.parts if part not in ("", "."))


def _zip_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        result: dict[str, bytes] = {}
        for info in archive.infolist():
            name = _safe_member_name(info.filename)
            if not name or info.is_dir():
                continue
            if name in result:
                raise ArtifactMismatch([f"duplicate archive member: {name}"])
            result[name] = archive.read(info)
        return result


def _tar_files(path: Path) -> tuple[str, dict[str, bytes]]:
    with tarfile.open(path, mode="r:*") as archive:
        result: dict[str, bytes] = {}
        for info in archive.getmembers():
            name = _safe_member_name(info.name)
            if not name or info.isdir():
                continue
            if not info.isfile():
                raise ArtifactMismatch([f"non-regular source-distribution member: {info.name}"])
            if name in result:
                raise ArtifactMismatch([f"duplicate archive member: {name}"])
            stream = archive.extractfile(info)
            if stream is None:
                raise ArtifactMismatch([f"unreadable source-distribution member: {name}"])
            result[name] = stream.read()
    prefixes = {name.split("/", 1)[0] for name in result}
    if len(prefixes) != 1:
        raise ArtifactMismatch([f"source distribution has multiple roots: {sorted(prefixes)}"])
    return next(iter(prefixes)), result


def normalize_sdist(path: Path, *, epoch: int | str | None = None) -> Path:
    """Normalize generated source-distribution metadata for reproducible builds.

    Setuptools currently preserves checkout mtimes in the sdist tar stream,
    even when ``SOURCE_DATE_EPOCH`` is set.  Repacking the generated archive
    keeps its payload untouched while fixing timestamps, ownership, modes and
    member order.  This function is intended for archives produced by the
    local build helper, not arbitrary untrusted tarballs.
    """

    path = Path(path).resolve()
    timestamp = source_date_epoch(epoch)
    try:
        with tarfile.open(path, mode="r:*") as archive:
            members = archive.getmembers()
            payload: dict[str, bytes] = {}
            for info in members:
                if info.isfile():
                    stream = archive.extractfile(info)
                    if stream is None:
                        raise ArtifactMismatch([f"unreadable source-distribution member: {info.name}"])
                    payload[info.name] = stream.read()
                elif not info.isdir():
                    raise ArtifactMismatch([
                        f"cannot normalize non-regular source-distribution member: {info.name}"
                    ])
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactMismatch([f"cannot read source distribution {path}: {exc}"]) from exc

    # Keep all original directories, including empty ones, and sort the
    # complete member list so extraction order is stable across platforms.
    names = sorted({info.name for info in members})
    directories = {info.name for info in members if info.isdir()}
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with gzip.GzipFile(
                fileobj=stream, mode="wb", filename="", mtime=timestamp,
                compresslevel=9,
            ) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
                    for name in names:
                        info = tarfile.TarInfo(name)
                        info.mtime = timestamp
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        if name in directories or name.endswith("/"):
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            info.size = 0
                            output.addfile(info)
                        else:
                            info.type = tarfile.REGTYPE
                            info.mode = 0o644
                            data = payload.get(name)
                            if data is None:
                                raise ArtifactMismatch([
                                    f"source-distribution member has no payload: {name}"
                                ])
                            info.size = len(data)
                            output.addfile(info, io.BytesIO(data))
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def zip_timestamp(epoch: int | str | None = None) -> tuple[int, int, int, int, int, int]:
    """Return a ZIP-safe UTC timestamp for deterministic archive entries."""

    timestamp = max(source_date_epoch(epoch), _ZIP_EPOCH)
    # ZIP stores years through 2107.  Clamping avoids an obscure overflow when
    # a caller supplies a far-future CI timestamp.
    timestamp = min(timestamp, 4354819199)  # 2107-12-31T23:59:59Z
    value = _datetime.datetime.fromtimestamp(timestamp, _datetime.timezone.utc)
    return value.year, value.month, value.day, value.hour, value.minute, value.second - value.second % 2


def deterministic_zip_info(
    name: str,
    *,
    epoch: int | str | None = None,
    executable: bool = False,
) -> zipfile.ZipInfo:
    """Build a ZIP member descriptor with stable metadata."""

    info = zipfile.ZipInfo(filename=name, date_time=zip_timestamp(epoch))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # POSIX; makes external_attr interpretation stable.
    mode = 0o100755 if executable else 0o100644
    info.external_attr = mode << 16
    info.extra = b""
    info.comment = b""
    return info


def _pe_symbol_info(data: bytes) -> dict[str, int | bool]:
    """Read the PE/COFF symbol and debug-directory fields without a toolchain."""

    if len(data) < 0x40 or data[:2] != b"MZ":
        return {"pe": False, "coff_symbols": 0, "debug_bytes": 0}
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset + 24 > len(data) or data[offset:offset + 4] != _PE_SIGNATURE:
        return {"pe": False, "coff_symbols": 0, "debug_bytes": 0}
    coff = offset + 4
    _machine, sections, _timestamp, pointer, symbols, optional_size, _characteristics = struct.unpack_from(
        "<HHIIIHH", data, coff
    )
    optional = coff + 20
    if optional + optional_size > len(data) or optional_size < 2:
        return {"pe": True, "coff_symbols": symbols, "debug_bytes": 0}
    magic = struct.unpack_from("<H", data, optional)[0]
    # IMAGE_OPTIONAL_HEADER32 data directories begin at byte 96; PE32+ at 112.
    directory_offset = optional + (112 if magic == 0x20B else 96)
    debug_bytes = 0
    if directory_offset + 7 * 8 <= optional + optional_size:
        _debug_rva, debug_bytes = struct.unpack_from("<II", data, directory_offset + 6 * 8)
    return {"pe": True, "coff_symbols": symbols, "debug_bytes": debug_bytes}


def _verify_native(path: Path, version: str) -> list[str]:
    """Verify a native release ZIP and reject unstripped Windows executables."""

    errors: list[str] = []
    if not path.name.startswith(f"cmpath-native-{version}-"):
        errors.append(f"native artifact filename is not for {version}: {path.name}")
    try:
        members = _zip_files(path)
    except ArtifactMismatch as exc:
        return list(exc.errors)
    executables = sorted(name for name in members if name.lower().endswith(".exe"))
    if not executables:
        errors.append("native artifact has no Windows executables")
    for name in executables:
        info = _pe_symbol_info(members[name])
        if not info["pe"]:
            errors.append(f"native artifact member is not a PE executable: {name}")
            continue
        if info["coff_symbols"]:
            errors.append(
                f"native artifact executable has COFF symbols: {name} ({info['coff_symbols']})"
            )
        if info["debug_bytes"]:
            errors.append(
                f"native artifact executable has a PE debug directory: {name} ({info['debug_bytes']} bytes)"
            )
    return errors


def _diff(label: str, expected: Mapping[str, bytes], actual: Mapping[str, bytes]) -> list[str]:
    errors: list[str] = []
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(path for path in set(expected) & set(actual) if expected[path] != actual[path])
    if missing:
        errors.append(f"{label}: missing {', '.join(missing[:12])}" + (" ..." if len(missing) > 12 else ""))
    if extra:
        errors.append(f"{label}: unexpected {', '.join(extra[:12])}" + (" ..." if len(extra) > 12 else ""))
    if changed:
        errors.append(f"{label}: changed {', '.join(changed[:12])}" + (" ..." if len(changed) > 12 else ""))
    return errors


def _metadata_version(data: bytes) -> str | None:
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("Version:"):
            return line.partition(":")[2].strip()
    return None


def _verify_wheel(path: Path, root: Path, version: str) -> list[str]:
    errors: list[str] = []
    if not path.name.startswith(f"cmpath-{version}-"):
        errors.append(f"wheel filename is not for {version}: {path.name}")
    try:
        members = _zip_files(path)
    except ArtifactMismatch as exc:
        return list(exc.errors)
    expected_package = {
        "cmpath/" + name[len("src/cmpath/"):]: data
        for name, data in _source_files(root).items()
        if name.startswith("src/cmpath/")
    }
    actual_package = {name: data for name, data in members.items() if name.startswith("cmpath/")}
    errors.extend(_diff("wheel Python payload", expected_package, actual_package))
    metadata = [name for name in members if name.endswith(".dist-info/METADATA")]
    if len(metadata) != 1:
        errors.append(f"wheel has {len(metadata)} METADATA files (expected one)")
    else:
        value = _metadata_version(members[metadata[0]])
        if value != version:
            errors.append(f"wheel metadata version {value!r} != {version!r}")
    records = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        errors.append(f"wheel has {len(records)} RECORD files (expected one)")
    else:
        try:
            rows = csv.reader(io.StringIO(members[records[0]].decode("utf-8")))
            listed: set[str] = set()
            for row in rows:
                if len(row) != 3:
                    errors.append(f"wheel RECORD row has {len(row)} fields")
                    continue
                name, digest, size = row
                listed.add(name)
                if name not in members:
                    errors.append(f"wheel RECORD names missing member {name}")
                    continue
                if digest:
                    algorithm, encoded = digest.split("=", 1)
                    if algorithm != "sha256":
                        errors.append(f"wheel RECORD uses unsupported hash {algorithm} for {name}")
                    actual = base64.urlsafe_b64encode(hashlib.sha256(members[name]).digest()).rstrip(b"=").decode()
                    if encoded != actual:
                        errors.append(f"wheel RECORD hash mismatch for {name}")
                if size and int(size) != len(members[name]):
                    errors.append(f"wheel RECORD size mismatch for {name}")
            omitted = sorted(set(members) - listed - {records[0]})
            if omitted:
                errors.append(f"wheel RECORD omits {', '.join(omitted[:12])}" + (" ..." if len(omitted) > 12 else ""))
        except (UnicodeDecodeError, ValueError) as exc:
            errors.append(f"invalid wheel RECORD: {exc}")
    return errors


def _verify_sdist(path: Path, root: Path, version: str) -> list[str]:
    errors: list[str] = []
    if path.name != f"cmpath-{version}.tar.gz":
        errors.append(f"source distribution filename is not for {version}: {path.name}")
    try:
        prefix, members = _tar_files(path)
    except ArtifactMismatch as exc:
        return list(exc.errors)
    expected_prefix = f"cmpath-{version}"
    if prefix != expected_prefix:
        errors.append(f"source distribution root {prefix!r} != {expected_prefix!r}")
    actual = {
        name[len(prefix) + 1:]: data
        for name, data in members.items()
        if name.startswith(prefix + "/")
    }
    actual = {
        name: data for name, data in actual.items()
        if name not in _GENERATED_SDIST_FILES
        and not any(name.startswith(item) for item in _GENERATED_SDIST_PREFIXES)
    }
    errors.extend(_diff("source distribution", manifest_files(root), actual))
    return errors


def _portable_expected(root: Path) -> dict[str, bytes]:
    names = list(_PORTABLE_TOP_LEVEL)
    names += [name for name in _source_files(root) if name.startswith("src/cmpath/")]
    names.append("scripts/run_mcp_wsl.sh")
    names += [name for name in _source_files(root) if name.startswith("portable-kimi-hermes/")]
    expected: dict[str, bytes] = {}
    for name in names:
        source = root.joinpath(*name.split("/"))
        if not source.is_file():
            raise ArtifactMismatch([f"portable bundle source is missing: {name}"])
        expected["cmpath/" + name] = source.read_bytes()
    return expected


def _verify_portable(path: Path, root: Path, version: str) -> list[str]:
    errors: list[str] = []
    if not path.name.startswith(f"cmpath-kimi-hermes-{version}"):
        errors.append(f"portable bundle filename is not for {version}: {path.name}")
    try:
        actual = _zip_files(path)
        expected = _portable_expected(root)
    except ArtifactMismatch as exc:
        return list(exc.errors)
    errors.extend(_diff("portable adapter bundle", expected, actual))
    return errors


def _plugin_version(value: str) -> str | None:
    """Normalize 0.4.0-alpha.4+cachebuster to 0.4.0a4."""

    match = re.match(r"^(\d+\.\d+\.\d+)(?:-alpha\.|a)(\d+)(?:\+.*)?$", value)
    return f"{match.group(1)}a{match.group(2)}" if match else None


def _plugin_expected(root: Path) -> dict[str, bytes]:
    return _source_files(root, include_generated=True)


def _plugin_archive_files(path: Path) -> dict[str, bytes]:
    members = _zip_files(path)
    if _PLUGIN_MANIFEST in members:
        return members
    candidates = [name for name in members if name.endswith("/" + _PLUGIN_MANIFEST)]
    if len(candidates) != 1:
        raise ArtifactMismatch(["plugin archive has no unique .codex-plugin/plugin.json"])
    prefix = candidates[0][:-len(_PLUGIN_MANIFEST)].rstrip("/")
    return {
        name[len(prefix) + 1:]: data
        for name, data in members.items()
        if name.startswith(prefix + "/")
    }


def _verify_plugin_manifest(data: bytes, version: str, label: str) -> list[str]:
    errors: list[str] = []
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{label} manifest is invalid JSON: {exc}"]
    if not isinstance(value, dict) or value.get("name") != "cmpath-memory":
        errors.append(f"{label} manifest name is not cmpath-memory")
    plugin_version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(plugin_version, str) or _plugin_version(plugin_version) != version:
        errors.append(f"{label} manifest version {plugin_version!r} does not map to {version!r}")
    return errors


def _verify_plugin_tree(path: Path, expected: Mapping[str, bytes], version: str, label: str) -> list[str]:
    actual = _source_files(path, include_generated=True)
    errors = _diff(label, expected, actual)
    manifest = actual.get(_PLUGIN_MANIFEST)
    if manifest is None:
        errors.append(f"{label} is missing {_PLUGIN_MANIFEST}")
    else:
        errors.extend(_verify_plugin_manifest(manifest, version, label))
    return errors


def _verify_plugin(path: Path, source: Path, version: str, label: str) -> list[str]:
    try:
        expected = _plugin_expected(source)
        if path.is_dir():
            return _verify_plugin_tree(path, expected, version, label)
        actual = _plugin_archive_files(path)
    except ArtifactMismatch as exc:
        return list(exc.errors)
    errors = _diff(label, expected, actual)
    manifest = actual.get(_PLUGIN_MANIFEST)
    if manifest is None:
        errors.append(f"{label} is missing {_PLUGIN_MANIFEST}")
    else:
        errors.extend(_verify_plugin_manifest(manifest, version, label))
    return errors


def _verify_compatibility_skill(path: Path, source: Path, label: str) -> list[str]:
    expected = source / "skills" / "cmpath-memory" / "SKILL.md"
    actual = path / "skills" / "cmpath-memory" / "SKILL.md"
    if not expected.is_file():
        return [f"canonical plugin is missing {expected}"]
    if not actual.is_file():
        return [f"{label} is missing compatibility skill entrypoint"]
    if expected.read_bytes() != actual.read_bytes():
        return [f"{label} compatibility skill differs from canonical source"]
    return []


def verify_artifacts(
    *,
    root: Path = ROOT,
    artifact_dir: Path | None = None,
    wheel: Path | None = None,
    sdist: Path | None = None,
    portable: Path | None = None,
    native_artifact: Path | None = None,
    checksum_manifest: Path | None = None,
    signature: Path | None = None,
    gpg: str = "gpg",
    plugin_root: Path | None = None,
    plugin_artifact: Path | None = None,
    plugin_cache: Path | None = None,
) -> dict[str, object]:
    """Verify selected current-version artifacts and return a JSON report.

    plugin_root is the canonical plugin checkout. plugin_artifact may be a
    plugin directory or ZIP. plugin_cache points at a Codex cache directory;
    complete entries are compared byte-for-byte and legacy compatibility
    entries (which intentionally contain only SKILL.md) are checked for that
    stable entrypoint.
    """

    root = Path(root).resolve()
    artifact_dir = Path(artifact_dir or root / "dist").resolve()
    version = project_version(root)
    wheel = Path(wheel or artifact_dir / f"cmpath-{version}-py3-none-any.whl").resolve()
    sdist = Path(sdist or artifact_dir / f"cmpath-{version}.tar.gz").resolve()
    portable = Path(portable or artifact_dir / f"cmpath-kimi-hermes-{version}.zip").resolve()
    if native_artifact is None:
        candidates = sorted(artifact_dir.glob(f"cmpath-native-{version}-*.zip"))
        native_artifact = candidates[0] if len(candidates) == 1 else None
    elif native_artifact is not None:
        native_artifact = Path(native_artifact).resolve()
    errors: list[str] = []
    checks: list[dict[str, object]] = []
    for kind, path, verifier in (
        ("wheel", wheel, _verify_wheel),
        ("sdist", sdist, _verify_sdist),
        ("portable", portable, _verify_portable),
    ):
        if not path.is_file():
            errors.append(f"missing {kind} artifact: {path}")
            checks.append({"kind": kind, "path": str(path), "ok": False})
            continue
        artifact_errors = verifier(path, root, version)
        errors.extend(artifact_errors)
        checks.append({
            "kind": kind,
            "path": str(path),
            "ok": not artifact_errors,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        })

    if native_artifact is not None:
        native_artifact = Path(native_artifact).resolve()
        if not native_artifact.is_file():
            native_errors = [f"missing native artifact: {native_artifact}"]
        else:
            native_errors = _verify_native(native_artifact, version)
        errors.extend(native_errors)
        native_check: dict[str, object] = {
            "kind": "native",
            "path": str(native_artifact),
            "ok": not native_errors,
        }
        if native_artifact.is_file():
            native_check.update({
                "sha256": hashlib.sha256(native_artifact.read_bytes()).hexdigest(),
                "bytes": native_artifact.stat().st_size,
            })
        checks.append(native_check)

    checksum_report: dict[str, object] | None = None
    if checksum_manifest is None:
        candidate = artifact_dir / _CHECKSUM_NAME
        if candidate.is_file():
            checksum_manifest = candidate
    if checksum_manifest is not None:
        checksum_manifest = Path(checksum_manifest).resolve()
        required = [wheel, sdist, portable]
        if native_artifact is not None and native_artifact.is_file():
            required.append(native_artifact)
        try:
            checksum_report = verify_checksum_manifest(checksum_manifest, required=required)
            if signature is not None:
                checksum_report["signature"] = verify_detached_signature(
                    checksum_manifest, Path(signature), gpg=gpg
                )
        except ArtifactMismatch as exc:
            errors.extend(exc.errors)
            if checksum_report is not None:
                checksum_report["ok"] = False
        if checksum_report is None:
            checksum_report = {"path": str(checksum_manifest), "ok": False}

    plugin_checks: list[dict[str, object]] = []
    if plugin_root is not None:
        plugin_root = Path(plugin_root).resolve()
        if not plugin_root.is_dir():
            errors.append(f"missing canonical plugin root: {plugin_root}")
        else:
            if plugin_artifact is not None:
                plugin_artifact = Path(plugin_artifact).resolve()
                if not plugin_artifact.exists():
                    errors.append(f"missing plugin artifact: {plugin_artifact}")
                else:
                    artifact_errors = _verify_plugin(plugin_artifact, plugin_root, version, "plugin artifact")
                    errors.extend(artifact_errors)
                    plugin_checks.append({
                        "kind": "plugin",
                        "path": str(plugin_artifact),
                        "ok": not artifact_errors,
                    })
            if plugin_cache is not None:
                plugin_cache = Path(plugin_cache).resolve()
                if not plugin_cache.is_dir():
                    errors.append(f"missing plugin cache: {plugin_cache}")
                else:
                    cache_dirs = sorted(path for path in plugin_cache.iterdir() if path.is_dir())
                    complete = False
                    expected_plugin = _plugin_expected(plugin_root)
                    for entry in cache_dirs:
                        manifest = entry / _PLUGIN_MANIFEST
                        if manifest.is_file():
                            complete = True
                            entry_errors = _verify_plugin_tree(
                                entry, expected_plugin, version, f"plugin cache {entry.name}"
                            )
                        else:
                            entry_errors = _verify_compatibility_skill(
                                entry, plugin_root, f"plugin cache {entry.name}"
                            )
                        errors.extend(entry_errors)
                        plugin_checks.append({
                            "kind": "plugin-cache",
                            "path": str(entry),
                            "ok": not entry_errors,
                        })
                    if not cache_dirs:
                        errors.append(f"plugin cache has no entries: {plugin_cache}")
                    elif not complete:
                        errors.append(f"plugin cache has no complete manifest entry: {plugin_cache}")

    report: dict[str, object] = {"ok": not errors, "version": version, "artifacts": checks}
    if checksum_report is not None:
        report["checksums"] = checksum_report
    if plugin_root is not None:
        report["plugins"] = plugin_checks
    if errors:
        report["errors"] = errors
        raise ArtifactMismatch(errors)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--portable", type=Path)
    parser.add_argument("--native-artifact", type=Path,
                        help="optional Windows native ZIP (auto-detected in artifact dir)")
    parser.add_argument("--checksums", "--checksum-manifest", dest="checksum_manifest", type=Path,
                        help=f"SHA-256 manifest (defaults to { _CHECKSUM_NAME } when present)")
    parser.add_argument("--signature", type=Path,
                        help="optional detached OpenPGP signature for the checksum manifest")
    parser.add_argument("--gpg", default="gpg", help="GPG executable used for signature verification")
    parser.add_argument("--plugin-root", type=Path)
    parser.add_argument("--plugin-artifact", type=Path)
    parser.add_argument("--plugin-cache", type=Path)
    parser.add_argument("--json", type=Path, metavar="PATH", help="also write the report to PATH")
    args = parser.parse_args(argv)
    try:
        report = verify_artifacts(
            root=args.root,
            artifact_dir=args.artifact_dir,
            wheel=args.wheel,
            sdist=args.sdist,
            portable=args.portable,
            native_artifact=args.native_artifact,
            checksum_manifest=args.checksum_manifest,
            signature=args.signature,
            gpg=args.gpg,
            plugin_root=args.plugin_root,
            plugin_artifact=args.plugin_artifact,
            plugin_cache=args.plugin_cache,
        )
    except ArtifactMismatch as exc:
        report = {"ok": False, "errors": exc.errors}
        print(json.dumps(report, indent=2), file=sys.stderr)
        if args.json:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 1
    print(json.dumps(report, indent=2))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
