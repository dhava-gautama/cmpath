"""Build the Go engine, benchmark and embedded example from vendored source.

The native package uses cgo for the bundled SQLite amalgamation. This script
only uses toolchains already present on the host: it never installs a compiler
or downloads a Go toolchain/dependency. If the default ``go`` command is older
than the module's minimum, an already cached Go toolchain is selected directly
and all builds run with ``GOTOOLCHAIN=local``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MIN_GO = (1, 26, 0)
COMMANDS = {
    "cmpath-native": "./cmd/cmpath-native",
    "cmpath-bench": "./cmd/cmpath-bench",
    "cmpath-embedded-example": "./examples/embedded",
}

# MSYS2 does not normally add its native UCRT64 prefix to the Windows PATH
# used by PowerShell.  Keep the discovery bounded to the conventional install
# locations and explicit environment variables; a recursive drive scan would
# be both slow and surprising in a build script.
_MSYS2_ROOT_ENV_VARS = (
    "MSYS2_ROOT",
    "MSYS2_HOME",
    "MSYS2_INSTALL_PATH",
    "MSYSTEM_PREFIX",
)


def _is_windows() -> bool:
    """Return whether native Windows toolchain discovery should be enabled.

    Kept behind a helper so platform-specific discovery can be tested on
    non-Windows hosts without mutating ``os.name`` (which also changes how
    ``pathlib.Path`` constructs paths globally).
    """
    return os.name == "nt"


def _msys2_roots(environment: dict[str, str] | None = None) -> list[Path]:
    """Return likely MSYS2 roots in deterministic, duplicate-free order.

    ``MSYS2_ROOT`` is the preferred way for CI and non-standard installs to
    identify the installation.  The conventional ``C:\\msys64`` and
    ``C:\\msys2`` locations are retained as a convenience on Windows.  The
    function accepts an environment mapping so tests and callers can inspect
    discovery without mutating the process environment.
    """
    environment = os.environ if environment is None else environment
    roots: list[Path] = []

    for variable in _MSYS2_ROOT_ENV_VARS:
        configured = str(environment.get(variable, "")).strip().strip('"')
        if configured:
            roots.append(Path(configured).expanduser())

    if _is_windows():
        system_drive = str(environment.get("SystemDrive", "C:")).rstrip("\\/") or "C:"
        roots.extend(
            [
                Path(system_drive) / "msys64",
                Path(system_drive) / "msys2",
                Path(system_drive) / "tools" / "msys64",
            ]
        )
        for variable in ("ProgramFiles", "ProgramData", "LOCALAPPDATA"):
            base = str(environment.get(variable, "")).strip().strip('"')
            if base:
                roots.append(Path(base) / "msys64")

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(os.path.normpath(str(root)))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _msys2_ucrt64_candidates(environment: dict[str, str]) -> list[Path]:
    """Return bounded candidate paths for the MSYS2 UCRT64 GCC executable."""
    candidates: list[Path] = []
    for root in _msys2_roots(environment):
        # MSYS2_ROOT normally names the installation root.  Accept a prefix
        # itself as well because MSYSTEM_PREFIX is often exported by shells
        # that launch PowerShell or Python.
        if root.name.casefold() == "ucrt64":
            prefix = root
        else:
            prefix = root / "ucrt64"
        candidates.append(prefix / "bin" / "gcc.exe")
        candidates.append(prefix / "bin" / "gcc")

    # A CI action may expose the UCRT64 bin directory in PATH without
    # exposing the parent MSYS2 root.  Prefer an executable from a path entry
    # containing the explicit UCRT64 prefix before falling back to any gcc.
    path_value = str(environment.get("PATH", ""))
    for entry in path_value.split(os.pathsep):
        if "ucrt64" not in entry.casefold():
            continue
        directory = Path(entry.strip().strip('"'))
        candidates.extend((directory / "gcc.exe", directory / "gcc"))
    return candidates


def _discover_msys2_ucrt64(environment: dict[str, str]) -> str | None:
    """Discover an installed MSYS2 UCRT64 GCC without changing global PATH.

    A compiler already present on PATH is accepted first when it is clearly
    from an UCRT64 prefix.  Explicit roots are then checked, followed by a
    normal PATH lookup so other installed GCC-compatible toolchains continue
    to work.  The returned absolute path is suitable for both ``CC`` and
    runtime-DLL lookup by child processes.
    """
    if not _is_windows():
        return None

    for candidate in _msys2_ucrt64_candidates(environment):
        if candidate.is_file():
            return str(candidate.resolve())

    path_value = str(environment.get("PATH", ""))
    for command in ("gcc.exe", "gcc"):
        resolved = shutil.which(command, path=path_value)
        if resolved is not None:
            return str(Path(resolved).resolve())
    return None


def _resolve_executable(
    command: str,
    label: str,
    *,
    path: str | None = None,
) -> str:
    """Resolve *command* while retaining a useful platform-specific error.

    ``shutil.which`` handles bare command names and Windows ``.exe`` suffixes,
    while the direct-file check makes an explicit path work even when the
    caller supplies a PATH mapping that intentionally excludes its parent.
    """
    direct = Path(command).expanduser()
    if direct.is_file():
        return str(direct.resolve())
    resolved = shutil.which(command, path=path)
    if resolved is None:
        raise ValueError(f"{label} was not found: {command!r}")
    return resolved


def _command_head(command: str) -> str:
    """Return the executable part of a shell-like compiler command."""
    stripped = command.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        stripped = stripped[1:-1]
    # A Windows executable path commonly contains spaces. Check it before
    # shell-style tokenization so ``C:\\Program Files\\...`` stays intact.
    if Path(stripped).is_file():
        return stripped
    try:
        parts = shlex.split(stripped, posix=os.name != "nt")
    except ValueError as error:
        raise ValueError(f"invalid compiler command {command!r}: {error}") from error
    if not parts:
        raise ValueError("compiler command is empty")
    return parts[0].strip("\"'")


def _parse_go_version(output: str) -> tuple[int, int, int]:
    """Extract a Go semantic version from ``go version`` output."""
    match = re.search(r"\bgo(\d+)\.(\d+)(?:\.(\d+))?\b", output)
    if match is None:
        raise ValueError(f"could not parse Go version from: {output.strip()!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _go_version(go: str, environment: dict[str, str]) -> tuple[str, tuple[int, int, int]]:
    """Run ``go version`` and return both its text and parsed version."""
    try:
        completed = subprocess.run(
            [go, "version"], env=environment, check=True,
            capture_output=True, text=True, cwd=ROOT / "native",
        )
    except (OSError, subprocess.CalledProcessError) as error:
        details = getattr(error, "stderr", None) or getattr(error, "stdout", None)
        suffix = f": {details.strip()}" if details else ""
        raise ValueError(f"could not run Go toolchain {go!r}: {error}{suffix}") from error
    output = (completed.stdout or completed.stderr).strip()
    return output, _parse_go_version(output)


def _select_go(command: str) -> str:
    """Resolve Go, preferring a compatible toolchain already in GOMODCACHE."""
    resolved = _resolve_executable(command, "Go")
    environment = os.environ.copy()
    environment.update({"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off"})
    try:
        _, current = _go_version(resolved, environment)
    except ValueError:
        return resolved
    if current >= MIN_GO or command != "go":
        return resolved
    try:
        cache = subprocess.check_output(
            [resolved, "env", "GOMODCACHE"],
            env=environment, text=True, cwd=ROOT / "native",
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return resolved
    binary_name = "go.exe" if os.name == "nt" else "go"
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for candidate in Path(cache).glob(f"golang.org/toolchain@*/bin/{binary_name}"):
        try:
            _, version = _go_version(str(candidate), environment)
        except (OSError, ValueError, subprocess.CalledProcessError):
            continue
        if version >= MIN_GO:
            candidates.append((version, str(candidate)))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return resolved


def _target(go: str, environment: dict[str, str], goos: str | None, goarch: str | None) -> tuple[str, str]:
    """Resolve and normalize the target platform used for naming binaries."""
    target_os = (goos or environment.get("GOOS") or ("windows" if os.name == "nt" else "linux")).lower()
    target_arch = (goarch or environment.get("GOARCH") or "").lower()
    if not target_arch:
        try:
            target_arch = subprocess.check_output(
                [go, "env", "GOARCH"],
                env=environment, text=True, cwd=ROOT / "native",
            ).strip().lower()
        except (KeyError, OSError, subprocess.CalledProcessError) as error:
            raise ValueError(f"could not determine Go target architecture: {error}") from error
    if not target_arch:
        raise ValueError("Go target architecture is empty; pass --goarch")
    return target_os, target_arch


def _require_compiler(environment: dict[str, str], target_os: str) -> str:
    """Validate the existing cgo compiler without invoking package managers.

    On a native Windows build, an unset ``CC`` is resolved through the
    installed MSYS2 UCRT64 prefix before the regular PATH lookup.  Discovery
    is intentionally local to the child-build environment; it never edits the
    user's machine or shell PATH.
    """
    raw = environment.get("CC", "").strip()
    if not raw and _is_windows() and target_os.casefold() == "windows":
        discovered = _discover_msys2_ucrt64(environment)
        if discovered is not None:
            raw = discovered
    if not raw:
        raw = "gcc"
    head = _command_head(raw)
    try:
        resolved = _resolve_executable(head, "C compiler", path=environment.get("PATH"))
    except ValueError as error:
        if _is_windows() and target_os.casefold() == "windows":
            raise ValueError(
                f"{error}. Install MSYS2 UCRT64 GCC or pass --cc to an existing "
                "GCC-compatible compiler."
            ) from error
        raise
    compiler_name = Path(resolved).name.lower()
    if _is_windows() and compiler_name in {"cl", "cl.exe"}:
        raise ValueError(
            "Go cgo requires a GCC-compatible C compiler; MSVC cl.exe is not "
            f"supported for the {target_os} target. Set --cc to a native "
            "MinGW-w64/LLVM compiler already installed on this machine."
        )
    environment["CC"] = resolved
    _prepend_path(environment, str(Path(resolved).resolve().parent))
    return resolved


def _prepend_path(environment: dict[str, str], directory: str) -> None:
    """Prepend *directory* to a child environment PATH exactly once."""
    directory = str(Path(directory).resolve())
    current = str(environment.get("PATH", ""))
    entries = [entry for entry in current.split(os.pathsep) if entry]
    normalized = os.path.normcase(os.path.normpath(directory))
    if not any(os.path.normcase(os.path.normpath(entry)) == normalized for entry in entries):
        entries.insert(0, directory)
    environment["PATH"] = os.pathsep.join(entries)


def _environment(go: str, cgo_enabled: bool, goos: str | None, goarch: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    # ``local`` plus a disabled module proxy guarantees no toolchain or
    # dependency download. ``_select_go`` handles a compatible cached Go.
    environment.update({"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off"})
    environment["CGO_ENABLED"] = "1" if cgo_enabled else "0"
    if environment.get("GOOS"):
        environment["GOOS"] = environment["GOOS"].lower()
    if environment.get("GOARCH"):
        environment["GOARCH"] = environment["GOARCH"].lower()
    if goos:
        environment["GOOS"] = goos.lower()
    if goarch:
        environment["GOARCH"] = goarch.lower()
    return environment


def _validate(go: str, cgo_enabled: bool, goos: str | None, goarch: str | None) -> dict[str, object]:
    environment = _environment(go, cgo_enabled, goos, goarch)
    version_text, version = _go_version(go, environment)
    if version < MIN_GO:
        required = ".".join(str(part) for part in MIN_GO)
        found = ".".join(str(part) for part in version)
        raise ValueError(
            f"Go {required}+ is required (found {found}); use --go with an existing "
            "compatible executable. No toolchain was downloaded."
        )
    target_os, target_arch = _target(go, environment, goos, goarch)
    compiler = _require_compiler(environment, target_os) if cgo_enabled else None
    return {
        "go": version_text,
        "target": {"goos": target_os, "goarch": target_arch},
        "cgo_enabled": bool(cgo_enabled),
        "cc": compiler,
        "offline": True,
    }


def _build(args: argparse.Namespace) -> dict[str, object]:
    go = _select_go(args.go)
    checked = _validate(go, args.cgo_enabled, args.goos, args.goarch)
    environment = _environment(go, args.cgo_enabled, args.goos, args.goarch)
    compiler = checked.get("cc")
    if compiler:
        # Native Windows compiler distributions (including MSYS2 UCRT64)
        # keep runtime DLLs beside gcc.exe.  cgo launches the compiler as a
        # child process, so make that directory available without requiring a
        # permanent machine PATH modification.
        environment["CC"] = str(compiler)
        _prepend_path(environment, str(Path(str(compiler)).resolve().parent))
    target_os = checked["target"]["goos"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if target_os == "windows" else ""
    hashes: dict[str, str] = {}
    for name, package in COMMANDS.items():
        target = output / (name + suffix)
        command = [
            go, "build", "-buildvcs=false", "-mod=vendor", "-trimpath",
            "-ldflags=-s -w -buildid=", "-o", str(target), package,
        ]
        subprocess.run(command, cwd=ROOT / "native", env=environment, check=True)
        hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    return {**checked, "output": str(output), "sha256": hashes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go", help="Go executable or absolute path")
    parser.add_argument(
        "--cc", help="existing GCC-compatible C compiler (also sets CC; defaults to CC or gcc)"
    )
    parser.add_argument("--goos", help="target GOOS (defaults to GOOS or the host)")
    parser.add_argument("--goarch", help="target GOARCH (defaults to GOARCH or Go's host value)")
    parser.add_argument(
        "--cgo-enabled", type=int, choices=(0, 1), default=1,
        help="whether to enable cgo (default: 1; SQLite requires cgo)",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="validate the local toolchains without building",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "native" / "bin")
    args = parser.parse_args()
    if args.cc:
        os.environ["CC"] = args.cc
    try:
        go = _select_go(args.go)
        result = _validate(go, args.cgo_enabled, args.goos, args.goarch) if args.check_only else _build(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
