"""Installation and runtime diagnostics for :mod:`cmpath`.

The doctor command deliberately performs small, local probes only.  It never
installs packages, starts an MCP server, opens a native database, or replaces a
user file.  The filesystem probe creates uniquely named temporary files and
removes them again after exercising the journal export hard-link path and its
same-directory reservation/rename fallback.  Native Windows can use its
no-replace rename primitive when hard links are unavailable; that path is
reported as a passing fallback after an explicit overwrite probe.
"""
from __future__ import annotations

from collections.abc import Sequence
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from . import __version__


_CHECK_ORDER = (
    "python",
    "platform",
    "sqlite_fts5",
    "native",
    "mcp",
    "filesystem_atomic_export",
    "database",
)
_STATUS_OK = frozenset(("pass", "skip"))
_STATUS_LABEL = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
_NATIVE_ENV = "CMP_NATIVE_BINARY"
_MCP_CONFIG_ENV = "CMPATH_MCP_CONFIG"


def _check(
    status: str,
    message: str,
    *,
    action: str | None = None,
    required: bool = False,
    **details,
) -> dict:
    """Build one stable check record.

    ``ok`` intentionally means "this individual check passed (or was not
    applicable)".  Optional warnings therefore remain visible to callers
    without making the core installation unusable; the report-level ``ok``
    applies the required/strict policy.
    """
    if status not in _STATUS_LABEL:
        raise ValueError(f"unknown doctor status: {status}")
    result = {
        "status": status,
        "ok": status in _STATUS_OK,
        "required": bool(required),
        "message": message,
    }
    if action:
        result["action"] = action
    if details:
        result["details"] = details
    return result


def _version_tuple(value: str) -> tuple[int, ...] | None:
    pieces: list[int] = []
    for piece in value.split("."):
        digits = ""
        for character in piece:
            if character.isdigit():
                digits += character
            else:
                break
        if not digits:
            break
        pieces.append(int(digits))
    return tuple(pieces) if pieces else None


def check_python() -> dict:
    """Check the interpreter contract and return safe runtime metadata."""
    info = {
        "executable": str(Path(sys.executable).resolve()) if sys.executable else "",
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "build": platform.python_build(),
        "prefix": sys.prefix,
    }
    supported = sys.version_info >= (3, 10)
    if supported:
        return _check(
            "pass",
            f"Python {info['version']} ({info['implementation']}) meets the 3.10+ requirement",
            required=True,
            **info,
        )
    return _check(
        "fail",
        f"Python {info['version']} is older than the required 3.10",
        action="Install Python 3.10 or newer and rerun cmpath doctor.",
        required=True,
        **info,
    )


def check_platform() -> dict:
    """Report the host platform and the target of bundled native artifacts."""
    system = platform.system() or "unknown"
    machine = platform.machine() or "unknown"
    libc = platform.libc_ver()
    details = {
        "system": system,
        "release": platform.release(),
        "machine": machine,
        "architecture": platform.architecture()[0],
        "platform": platform.platform(),
        "libc": {"name": libc[0], "version": libc[1]},
        "validated_native_targets": ["Linux x86-64 (glibc)", "Windows AMD64 (UCRT64)"],
    }
    normalized_system = system.casefold()
    normalized_machine = machine.casefold().replace("-", "_")
    native_target = (
        normalized_system in {"linux", "windows"}
        and normalized_machine in {"x86_64", "amd64"}
    )
    if native_target:
        return _check(
            "pass",
            f"{system} {machine} is a validated native target family",
            **details,
        )
    return _check(
        "warn",
        f"{system} {machine} is outside the validated native target families",
        action="Use Python storage on this host, or rebuild native/cmpath-native for this platform before enabling the native harness.",
        **details,
    )


def check_sqlite_fts5() -> dict:
    """Exercise the SQLite FTS5 module rather than relying on a compile flag."""
    details = {
        "sqlite_version": sqlite3.sqlite_version,
    }
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(":memory:")
        try:
            details["compile_options"] = sorted(
                row[0]
                for row in connection.execute("PRAGMA compile_options")
                if isinstance(row[0], str)
            )
        except sqlite3.Error:
            details["compile_options"] = []
        connection.execute("CREATE VIRTUAL TABLE cmpath_doctor_fts USING fts5(content)")
        connection.execute(
            "INSERT INTO cmpath_doctor_fts(content) VALUES (?)",
            ("cmpath fts5 probe",),
        )
        found = connection.execute(
            "SELECT count(*) FROM cmpath_doctor_fts WHERE cmpath_doctor_fts MATCH ?",
            ("fts5",),
        ).fetchone()[0]
        details["probe_matches"] = int(found)
        if found != 1:
            raise sqlite3.OperationalError("FTS5 probe returned no matching row")
    except (sqlite3.Error, OSError) as exc:
        details["error"] = str(exc)
        return _check(
            "fail",
            "SQLite is available but the FTS5 probe failed",
            action="Install a Python build with SQLite FTS5 enabled, then rerun cmpath doctor.",
            required=True,
            **details,
        )
    finally:
        if connection is not None:
            connection.close()
    return _check(
        "pass",
        f"SQLite {sqlite3.sqlite_version} can create and query an FTS5 table",
        required=True,
        **details,
    )


def _native_candidates() -> list[Path]:
    package_root = Path(__file__).resolve().parents[2]
    bases = []
    for native_bin in (
        Path.cwd() / "native" / "bin",
        package_root / "native" / "bin",
    ):
        bases.append(native_bin / "cmpath-native")
        bases.append(native_bin / "windows" / "cmpath-native")
    candidates: list[Path] = []
    for base in bases:
        if os.name == "nt":
            candidates.append(base.with_suffix(".exe"))
        candidates.append(base)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def check_native(binary: str | Path | None = None) -> dict:
    """Locate a native executable and run its side-effect-free version probe."""
    source = "discovered"
    explicit = binary is not None
    if binary is not None:
        path = Path(binary).expanduser()
        source = "argument"
    else:
        configured = os.environ.get(_NATIVE_ENV, "").strip()
        if configured:
            path = Path(configured).expanduser()
            source = _NATIVE_ENV
            explicit = True
        else:
            path = next((candidate for candidate in _native_candidates() if candidate.is_file()), None)
            if path is None:
                return _check(
                    "warn",
                    "No cmpath-native executable was found (native harness is optional)",
                    action="Build native/cmpath-native with python scripts/build_native.py, or pass --native PATH when you need the native harness.",
                    path=None,
                    source="not_found",
                )
    details = {"path": str(path.resolve()), "source": source}
    try:
        if not path.exists():
            raise FileNotFoundError(f"file does not exist: {path}")
        if not path.is_file():
            raise OSError(f"path is not a regular file: {path}")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise PermissionError(f"file is not executable: {path}")
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        details["returncode"] = completed.returncode
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout:
            details["reported_version"] = stdout.splitlines()[-1].strip()
        if stderr:
            details["stderr"] = stderr[-1000:]
        if completed.returncode != 0:
            message = stderr or f"version probe exited with status {completed.returncode}"
            raise OSError(message)
        reported = details.get("reported_version")
        if not reported:
            raise OSError("version probe returned no version")
        details["compatible_version"] = reported == __version__
        if reported != __version__:
            message = f"native executable reports {reported}, Python package is {__version__}"
            action = "Rebuild or select a cmpath-native executable that matches the installed Python package."
            return _check(
                "fail" if explicit else "warn",
                message,
                action=action,
                required=explicit,
                **details,
            )
    except subprocess.TimeoutExpired as exc:
        details["error"] = "version probe timed out"
        status = "fail" if explicit else "warn"
        return _check(
            status,
            "Native executable did not answer its version probe within 3 seconds",
            action="Rebuild the native executable for this host and rerun cmpath doctor.",
            required=explicit,
            **details,
        )
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
        details["error"] = str(exc)
        status = "fail" if explicit else "warn"
        return _check(
            status,
            "Native executable was found but could not be run on this host",
            action="Rebuild native/cmpath-native for this platform, or omit native integration when using the Python API.",
            required=explicit,
            **details,
        )
    return _check(
        "pass",
        f"Native executable {Path(details['path']).name} reports compatible version {__version__}",
        **details,
    )


def _config_paths(
    configs: Sequence[str | Path] | str | Path | None,
) -> list[tuple[Path, str, bool]]:
    """Return ``(path, source, explicit)`` config candidates without duplicates."""
    candidates: list[tuple[Path, str, bool]] = []
    if isinstance(configs, (str, Path)):
        configs = (configs,)
    if configs:
        candidates.extend((Path(item).expanduser(), "argument", True) for item in configs)
    else:
        configured = os.environ.get(_MCP_CONFIG_ENV, "").strip()
        if configured:
            candidates.extend(
                (Path(item).expanduser(), _MCP_CONFIG_ENV, True)
                for item in configured.split(os.pathsep)
                if item.strip()
            )
        roots = [Path.cwd(), Path(__file__).resolve().parents[2], Path.home()]
        for root in roots:
            candidates.extend(
                (root / relative, "discovered", False)
                for relative in (Path(".cursor/mcp.json"), Path(".kimi-code/mcp.json"))
            )
    result: list[tuple[Path, str, bool]] = []
    seen: set[str] = set()
    for path, source, explicit in candidates:
        key = os.path.normcase(str(path.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        result.append((path, source, explicit))
    return result


def _mcp_server_record(path: Path, source: str, explicit: bool) -> tuple[dict, str, str | None]:
    """Validate one JSON MCP config without executing its command."""
    record: dict = {"path": str(path.resolve(strict=False)), "source": source, "explicit": explicit}
    if not path.exists():
        record.update({"exists": False, "valid": False})
        return record, "fail" if explicit else "skip", f"Create {path} or remove it from --mcp-config."
    record["exists"] = True
    if not path.is_file():
        record.update({"valid": False, "error": "path is not a regular file"})
        return record, "fail", f"Point --mcp-config at a JSON file, not {path}."
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        record.update({"valid": False, "error": str(exc)})
        return record, "fail", f"Fix the JSON in {path} and rerun cmpath doctor."
    if not isinstance(value, dict):
        record.update({"valid": False, "error": "top-level JSON value must be an object"})
        return record, "fail", f"Make {path} contain an object with an mcpServers mapping."
    servers = value.get("mcpServers", value.get("mcp_servers"))
    if not isinstance(servers, dict):
        record.update({"valid": False, "error": "mcpServers mapping is missing"})
        return record, "fail", f"Add an mcpServers.cmpath-memory entry to {path}."
    server = servers.get("cmpath-memory")
    if not isinstance(server, dict):
        record.update({"valid": False, "error": "cmpath-memory server entry is missing"})
        return record, "fail", f"Add a cmpath-memory server entry that launches cmpath-mcp to {path}."
    command = server.get("command")
    args = server.get("args", [])
    if not isinstance(command, str) or not command.strip():
        record.update({"valid": False, "error": "server command must be a nonempty string"})
        return record, "fail", f"Set mcpServers.cmpath-memory.command in {path}."
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        record.update({"valid": False, "error": "server args must be an array of strings"})
        return record, "fail", f"Set mcpServers.cmpath-memory.args to an array of strings in {path}."
    rendered = " ".join([command, *args]).casefold()
    if "cmpath" not in rendered:
        record.update({"valid": False, "error": "server entry does not reference cmpath"})
        return record, "fail", f"Point the cmpath-memory entry in {path} at cmpath-mcp or the CMP launcher."
    available = bool(Path(command).exists()) if Path(command).is_absolute() else shutil.which(command) is not None
    record.update(
        {
            "valid": True,
            "server": "cmpath-memory",
            "command": command,
            "args_count": len(args),
            "command_available": available,
        }
    )
    if not available:
        return (
            record,
            "warn",
            f"Install or expose the configured MCP command {command!r} before starting the client that owns {path}.",
        )
    return record, "pass", None


def check_mcp(configs: Sequence[str | Path] | str | Path | None = None) -> dict:
    """Check optional MCP support and known client config files."""
    dependency: dict = {"installed": False, "package": "mcp", "required": ">=2,<3"}
    dependency_status = "warn"
    dependency_action: str | None = None
    try:
        found = importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        found = False
    if found:
        dependency["installed"] = True
        try:
            dependency["version"] = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            dependency["version"] = "unknown"
            dependency_action = "Install a supported MCP SDK with: python -m pip install 'cmpath[integrations]'."
        parsed = _version_tuple(str(dependency["version"]))
        if parsed and parsed[0] == 2:
            dependency_status = "pass"
        elif parsed:
            dependency_status = "warn"
            dependency_action = "Install a supported MCP SDK with: python -m pip install 'cmpath[integrations]'."
        else:
            dependency_status = "warn"
    else:
        # MCP is intentionally optional.  With no explicit client config the
        # absence of the SDK is not a defect in the core installation and
        # should not make a default doctor run noisy or strict-failing.
        dependency_status = "skip"
    config_records: list[dict] = []
    config_statuses: list[str] = []
    config_actions: list[str] = []
    for path, source, explicit in _config_paths(configs):
        if not path.exists() and not explicit:
            continue
        record, status, action = _mcp_server_record(path, source, explicit)
        config_records.append(record)
        config_statuses.append(status)
        if action:
            config_actions.append(action)
    if not config_records:
        config_status = "skip"
        config_message = "No MCP client config was found (MCP integration is optional)"
        if dependency_status != "skip":
            config_actions.append("Configure an MCP client with cmpath-memory, or pass --mcp-config PATH to validate one.")
    elif "fail" in config_statuses:
        config_status = "fail" if any(record.get("explicit") for record in config_records if not record.get("valid", True)) else "warn"
        config_message = "At least one MCP config is malformed or missing cmpath-memory"
    elif "warn" in config_statuses:
        config_status = "warn"
        config_message = "MCP config structure is valid but its launch command is not available here"
    else:
        config_status = "pass"
        config_message = f"Validated {len(config_records)} MCP client config file(s)"
    statuses = [dependency_status, config_status]
    if "fail" in statuses:
        status = "fail"
    elif "warn" in statuses:
        status = "warn"
    elif config_status == "skip":
        # An absent SDK and absent config are both expected for a core-only
        # install.  An installed SDK is useful on its own, but remains a
        # warning until a client config has been checked.
        status = "skip" if dependency_status == "skip" else "warn"
    elif all(item == "skip" for item in statuses):
        status = "skip"
    else:
        status = "pass"
    actions: list[str] = []
    if dependency_action:
        actions.append(dependency_action)
    actions.extend(config_actions)
    details = {
        "dependency": {**dependency, "status": dependency_status},
        "configs": config_records,
    }
    required = config_status == "fail" and any(
        record.get("explicit") and not record.get("valid", True)
        for record in config_records
    )
    return _check(
        status,
        f"{config_message}; optional dependency is {dependency_status}",
        action="; ".join(dict.fromkeys(actions)) if actions else None,
        required=required,
        **details,
    )


def check_atomic_export(directory: str | Path) -> dict:
    """Exercise the journal export publication paths and cleanup.

    Hard-link publication is the strongest no-overwrite path.  A few mounted
    filesystems reject hard links while still providing same-directory rename;
    probe the export marker/reservation fallback there and report its reduced
    interruption guarantee instead of treating the mount as unusable.
    """
    path = Path(directory).expanduser()
    details = {
        "directory": str(path.resolve(strict=False)),
        "directory_sync": "not_attempted",
        "hard_link": False,
        "fallback": False,
        "no_overwrite": False,
        "publication": "not_attempted",
    }
    if not path.exists():
        return _check(
            "fail",
            f"Export directory does not exist: {path}",
            action="Create the export/database directory or pass --export-dir PATH for a writable local directory.",
            required=True,
            **details,
        )
    if not path.is_dir():
        return _check(
            "fail",
            f"Export path is not a directory: {path}",
            action="Pass --export-dir to an existing directory on a writable local filesystem.",
            required=True,
            **details,
        )
    temporary: Path | None = None
    linked: Path | None = None
    published: Path | None = None
    fallback_temporary: Path | None = None
    descriptor: int | None = None
    marker = ("cmpath-doctor-" + uuid.uuid4().hex).encode("ascii")
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".cmpath-doctor-", suffix=".tmp", dir=str(path))
        temporary = Path(temporary_name)
        published = path / (temporary.name + ".published")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        details["file_fsync"] = True
        # Probe hard-link publication first. Some mounted/network filesystems
        # permit rename but reject hard links, so continue with the same
        # reservation fallback used by the native journal exporter.
        linked = path / (temporary.name + ".linked")
        try:
            os.link(temporary, linked)
        except OSError as hard_link_error:
            details["hard_link_error"] = str(hard_link_error)
            details["hard_link"] = False
            # Native Windows' MoveFile (the implementation behind Go's
            # os.Rename) rejects an existing destination, so no marker is
            # needed there. POSIX rename replaces, requiring an O_EXCL marker
            # reservation before replacing that marker with the archive.
            if os.name == "nt":
                os.rename(temporary, published)
                temporary = None
                details["reservation"] = "windows_rename_no_replace"
            else:
                reservation = b"cmpath-export-reservation-v1\n" + temporary.name.encode("utf-8") + b"\n"
                reservation_fd = os.open(
                    published,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    written = os.write(reservation_fd, reservation)
                    if written != len(reservation):
                        raise OSError("short reservation marker write")
                    os.fsync(reservation_fd)
                    details["reservation"] = "exclusive_marker"
                finally:
                    os.close(reservation_fd)
                try:
                    os.replace(temporary, published)
                except OSError:
                    # Remove only the exact marker created above. If another
                    # writer replaced it, leave that writer's file intact.
                    try:
                        if published.read_bytes() == reservation:
                            published.unlink()
                    except OSError:
                        pass
                    raise
                temporary = None
            details["fallback"] = True
            details["publication"] = "rename_fallback"
            if os.name != "nt":
                details["reduced_guarantee"] = (
                    "a crash after reservation and before rename may leave a recognizable marker"
                )
            # Exercise the no-overwrite side of the fallback. The probe is
            # intentionally separate from the published archive so cleanup can
            # still remove all probe artifacts.
            fallback_temporary = path / (published.name + ".second")
            fallback_temporary.write_bytes(b"second archive")
            if os.name == "nt":
                try:
                    os.rename(fallback_temporary, published)
                except OSError:
                    details["no_overwrite"] = True
                else:
                    raise OSError("fallback rename replaced an existing destination")
            else:
                try:
                    fd = os.open(published, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    details["no_overwrite"] = True
                else:
                    os.close(fd)
                    raise OSError("fallback reservation accepted an existing destination")
        else:
            details["hard_link"] = True
            if linked.read_bytes() != marker:
                raise OSError("hard-linked probe contents did not match")
            linked.unlink()
            linked = None
            details["publication"] = "hard_link"
            details["no_overwrite"] = True
            os.replace(temporary, published)
            temporary = None
        temporary = None
        details["atomic_replace"] = True
        if published.read_bytes() != marker:
            raise OSError("published probe contents did not match")
        details["readback"] = True
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(str(path), directory_flags)
        except (OSError, ValueError):
            details["directory_sync"] = "not_supported"
        else:
            try:
                os.fsync(directory_fd)
                details["directory_sync"] = "supported"
            except OSError:
                details["directory_sync"] = "not_supported"
            finally:
                os.close(directory_fd)
        if details["fallback"] and os.name == "nt" and details["no_overwrite"]:
            return _check(
                "pass",
                "Filesystem rejects hard links but supports a no-replace Windows rename fallback",
                required=True,
                **details,
            )
        if details["fallback"]:
            return _check(
                "warn",
                "Filesystem rejects hard links; export fallback can publish without replacing an existing destination",
                action="Prefer a filesystem with hard-link support for the strongest crash/concurrency guarantee; the fallback may leave a reservation marker after interruption.",
                required=True,
                **details,
            )
        return _check(
            "pass",
            "Filesystem can durably write and atomically publish an export probe",
            required=True,
            **details,
        )
    except (OSError, ValueError) as exc:
        details["error"] = str(exc)
        return _check(
            "fail",
            "Filesystem could not complete the atomic export probe",
            action="Choose a writable local filesystem with reliable hard-link, rename and locking semantics; avoid read-only or uncertain network mounts.",
            required=True,
            **details,
        )
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for candidate in (temporary, linked, published, fallback_temporary):
            if candidate is not None:
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # A cleanup failure is included in diagnostics only when
                    # the probe itself has already failed; never mask its result.
                    pass


def check_database(database: str | Path | None) -> dict:
    """Read-only sanity check for an existing CMP database, if one is given."""
    if database is None or str(database) == ":memory:":
        return _check("skip", "No on-disk database was selected for checking")
    path = Path(database).expanduser()
    details = {"path": str(path.resolve(strict=False))}
    if not path.exists():
        return _check(
            "skip",
            f"Database does not exist yet: {path}",
            action=f"Initialize it with: cmpath --db {path} init",
            **details,
        )
    if not path.is_file():
        return _check(
            "fail",
            f"Database path is not a regular file: {path}",
            action="Choose a file path for --db, not a directory or device.",
            required=True,
            **details,
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(path), timeout=2)
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        required_tables = {
            "tasks",
            "messages",
            "facts",
            "dependencies",
            "aliases",
            "runtime_state",
            "events",
            "evidence_index",
        }
        missing = sorted(required_tables - tables)
        connection.execute(
            "SELECT count(*) FROM evidence_index WHERE evidence_index MATCH ?",
            ("doctor",),
        ).fetchone()
        details.update(
            {
                "integrity": integrity,
                "foreign_keys": foreign_keys,
                "schema_version": user_version,
                "missing_tables": missing,
                "sqlite_version": sqlite3.sqlite_version,
            }
        )
        if integrity != ["ok"] or foreign_keys or missing or user_version != 1:
            return _check(
                "fail",
                "CMP database opened but its integrity/schema check failed",
                action="Restore a known-good SQLite backup and rerun cmpath doctor; do not continue writes to a corrupt database.",
                required=True,
                **details,
            )
    except (sqlite3.Error, OSError) as exc:
        details["error"] = str(exc)
        return _check(
            "fail",
            "CMP database could not be opened for a read-only sanity check",
            action="Verify the database path and restore a compatible CMP SQLite backup if the file is corrupt.",
            required=True,
            **details,
        )
    finally:
        if connection is not None:
            connection.close()
    return _check(
        "pass",
        f"CMP database {path.name} passed integrity, foreign-key, schema and FTS checks",
        required=True,
        **details,
    )


def run_doctor(
    database: str | Path | None = "cmpath.db",
    *,
    db: str | Path | None = None,
    native_binary: str | Path | None = None,
    mcp_configs: Sequence[str | Path] | str | Path | None = None,
    export_dir: str | Path | None = None,
    skip_native: bool = False,
    skip_mcp: bool = False,
    strict: bool = False,
) -> dict:
    """Run all local diagnostics and return a JSON-serializable report.

    ``db`` is accepted as a convenience alias for callers that mirror the CLI
    option name.  The default database is inspected only when it already exists;
    doctor never creates it.
    """
    if db is not None:
        database = db
    selected_database = database
    checks = {
        "python": check_python(),
        "platform": check_platform(),
        "sqlite_fts5": check_sqlite_fts5(),
        "native": (
            _check("skip", "Native executable check was disabled with --skip-native")
            if skip_native
            else check_native(native_binary)
        ),
        "mcp": (
            _check("skip", "MCP check was disabled with --skip-mcp")
            if skip_mcp
            else check_mcp(mcp_configs)
        ),
    }
    if export_dir is None:
        if selected_database is not None and str(selected_database) != ":memory:":
            export_directory = Path(selected_database).expanduser().parent
        else:
            export_directory = Path.cwd()
    else:
        export_directory = Path(export_dir).expanduser()
    checks["filesystem_atomic_export"] = check_atomic_export(export_directory)
    checks["database"] = check_database(selected_database)
    # Preserve a deterministic order even if a caller later extends the map.
    checks = {name: checks[name] for name in _CHECK_ORDER}
    actions: list[str] = []
    counts = {status: 0 for status in _STATUS_LABEL}
    blocking: list[str] = []
    for name, result in checks.items():
        status = result["status"]
        counts[status] += 1
        if result.get("action"):
            actions.append(result["action"])
        if status == "fail" or (strict and status == "warn"):
            blocking.append(name)
    actions = list(dict.fromkeys(actions))
    return {
        "ok": not blocking,
        "strict": bool(strict),
        "package_version": __version__,
        "checks": checks,
        "summary": {
            "passed": counts["pass"],
            "warnings": counts["warn"],
            "failed": counts["fail"],
            "skipped": counts["skip"],
            "blocking": blocking,
        },
        "actions": actions,
    }


def render_text(report: dict) -> str:
    """Render a concise actionable report for terminals."""
    summary = report.get("summary", {})
    if report.get("ok"):
        heading = "CMPATH doctor: OK"
    else:
        heading = "CMPATH doctor: ACTION REQUIRED"
    lines = [
        heading
        + f" (pass={summary.get('passed', 0)}, warn={summary.get('warnings', 0)}, fail={summary.get('failed', 0)}, skip={summary.get('skipped', 0)})"
    ]
    for name in _CHECK_ORDER:
        result = report.get("checks", {}).get(name, {})
        status = str(result.get("status", "fail"))
        label = _STATUS_LABEL.get(status, status.upper())
        lines.append(f"[{label}] {name}: {result.get('message', '')}")
        action = result.get("action")
        if action:
            lines.append(f"       -> {action}")
    if report.get("actions"):
        lines.append("Next actions:")
        for action in report["actions"]:
            lines.append(f"  - {action}")
    return "\n".join(lines)


def format_report(report: dict, *, as_json: bool = False) -> str:
    """Return either pretty JSON or terminal text."""
    if as_json:
        return json.dumps(report, ensure_ascii=False, indent=2)
    return render_text(report)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the doctor as a standalone module entry point.

    The canonical CLI keeps ``doctor`` as a subcommand.  Importing the CLI
    lazily here avoids an import cycle while making ``python -m cmpath.doctor``
    behave like the installed ``cmpath doctor`` command.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"--version", "-V"}:
        print(__version__)
        return 0
    from .cli import main as cli_main

    return cli_main(["doctor", *arguments])


__all__ = [
    "check_atomic_export",
    "check_database",
    "check_mcp",
    "check_native",
    "check_platform",
    "check_python",
    "check_sqlite_fts5",
    "format_report",
    "main",
    "render_text",
    "run_doctor",
]


if __name__ == "__main__":
    raise SystemExit(main())
