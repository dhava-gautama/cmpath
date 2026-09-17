"""Test-only native executable discovery and host-format diagnostics."""
from __future__ import annotations

import os
from pathlib import Path
import sys


_MACH_O_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
}


def native_binary(root: Path) -> Path:
    """Resolve the explicitly requested binary or the host's build name."""
    configured = os.environ.get("CMP_NATIVE_BINARY")
    if configured:
        return Path(configured)
    base = root / "native" / "bin" / "cmpath-native"
    if os.name == "nt" and base.with_suffix(".exe").is_file():
        return base.with_suffix(".exe")
    # Keep the extensionless path when only the archive's Linux binary is
    # present. The format check below then reports why the tests are skipped.
    return base


def native_binary_status(path: Path) -> tuple[bool, str]:
    """Return whether *path* has a format suitable for this test host.

    A file existing is not sufficient: the source archive includes a Linux
    ELF executable, which Windows reports as WinError 193 when launched.
    Unknown formats remain eligible so caller-provided wrappers are not
    rejected before subprocess can interpret them.
    """
    if not path.is_file():
        return False, f"native binary not found: {path}"
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as error:
        return False, f"cannot inspect native binary {path}: {error}"

    if os.name == "nt":
        if magic == b"\x7fELF":
            return False, (
                f"native binary is Linux ELF, not Windows PE: {path}; "
                "build it on Windows with `python scripts/build_native.py`"
            )
        if magic in _MACH_O_MAGICS:
            return False, f"native binary is macOS Mach-O, not Windows PE: {path}"
    elif sys.platform == "darwin":
        if magic == b"\x7fELF":
            return False, f"native binary is Linux ELF, not macOS Mach-O: {path}"
        if magic[:2] == b"MZ":
            return False, f"native binary is Windows PE, not macOS Mach-O: {path}"
    else:
        if magic[:2] == b"MZ":
            return False, f"native binary is Windows PE, not this POSIX host: {path}"
        if magic in _MACH_O_MAGICS:
            return False, f"native binary is macOS Mach-O, not this POSIX host: {path}"
    return True, "native binary format is compatible"

