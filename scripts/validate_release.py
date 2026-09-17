"""Validate the built distributions in a temporary, isolated Python environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
import tomllib

from verify_artifacts import ArtifactMismatch, verify_artifacts


ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go", help="Go executable used for the vendored source rebuild")
    parser.add_argument(
        "--with-mcp",
        "--mcp",
        dest="with_mcp",
        action="store_true",
        help="Smoke-test the optional MCP extra in a separate isolated environment (offline only)",
    )
    parser.add_argument(
        "--mcp-wheel",
        type=Path,
        help="Local mcp wheel to use for --with-mcp; no package-index access is attempted",
    )
    parser.add_argument(
        "--mcp-wheelhouse",
        type=Path,
        help="Local wheelhouse containing mcp and its dependencies for --with-mcp",
    )
    parser.add_argument(
        "--mcp-required",
        action="store_true",
        help="Fail instead of recording a skipped optional MCP check when its local source is unavailable",
    )
    args = parser.parse_args()
    if args.mcp_wheel or args.mcp_wheelhouse:
        args.with_mcp = True
    if args.mcp_required:
        args.with_mcp = True
    wheel = ROOT / "dist" / f"cmpath-{VERSION}-py3-none-any.whl"
    source = ROOT / "dist" / f"cmpath-{VERSION}.tar.gz"
    portable = ROOT / "dist" / f"cmpath-kimi-hermes-{VERSION}.zip"
    native_dir = ROOT / "native" / "bin"
    if os.name == "nt" and (native_dir / "windows").is_dir():
        native_dir = native_dir / "windows"
    native = native_dir / "cmpath-native"
    embedded = native_dir / "cmpath-embedded-example"
    if os.name == "nt":
        native = native.with_suffix(".exe")
        embedded = embedded.with_suffix(".exe")
    for artifact in (wheel, source, portable, native, embedded):
        if not artifact.is_file():
            parser.error(f"Build the required artifact first: {artifact}")
    try:
        verify_artifacts(root=ROOT, wheel=wheel, sdist=source, portable=portable)
    except ArtifactMismatch as exc:
        parser.error("Artifact/source integrity check failed: " + " | ".join(exc.errors))
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    checks = {}
    with tempfile.TemporaryDirectory(prefix="cmpath-release-") as temporary:
        work = Path(temporary)
        target = work / "venv"
        venv.EnvBuilder(with_pip=True).create(target)
        binaries = target / ("Scripts" if os.name == "nt" else "bin")
        python = binaries / ("python.exe" if os.name == "nt" else "python")
        cli = binaries / ("cmpath.exe" if os.name == "nt" else "cmpath")

        def run(*args, cwd=work, env=None):
            result = subprocess.run([str(a) for a in args], cwd=cwd,
                                    env=environment if env is None else env,
                                    text=True, capture_output=True, check=False)
            if result.returncode:
                raise RuntimeError(f"Command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}")
            return result.stdout

        run(python, "-m", "pip", "install", "--no-index", "--no-deps", wheel)
        info = json.loads(run(python, "-c", "import cmpath,json; print(json.dumps({'version':cmpath.__version__,'path':cmpath.__file__}))"))
        assert info["version"] == VERSION
        assert Path(info["path"]).is_relative_to(target)
        assert run(cli, "--version").strip() == VERSION
        checks["artifact_source_integrity"] = True
        checks["isolated_wheel_install"] = True
        checks["installed_import_outside_source_tree"] = True
        checks["console_entrypoint"] = True

        if args.with_mcp:
            # Keep the optional integration out of the core wheel environment.
            # A wheelhouse or wheel must be supplied explicitly so a release
            # validation run never turns into an implicit network install.
            mcp_wheel = args.mcp_wheel.expanduser().resolve() if args.mcp_wheel else None
            wheelhouse = args.mcp_wheelhouse.expanduser().resolve() if args.mcp_wheelhouse else None
            source_error = None
            if mcp_wheel is not None and not mcp_wheel.is_file():
                source_error = f"MCP wheel does not exist: {mcp_wheel}"
            elif wheelhouse is not None and not wheelhouse.is_dir():
                source_error = f"MCP wheelhouse does not exist: {wheelhouse}"
            elif mcp_wheel is None and wheelhouse is None:
                source_error = (
                    "no local MCP wheel or wheelhouse was supplied; "
                    "rerun with --mcp-wheel or --mcp-wheelhouse"
                )
            if source_error:
                if args.mcp_required:
                    raise RuntimeError(source_error)
                checks["optional_mcp_validation"] = {
                    "status": "skipped",
                    "reason": source_error,
                }
            else:
                mcp_target = work / "mcp-venv"
                venv.EnvBuilder(with_pip=True).create(mcp_target)
                mcp_binaries = mcp_target / ("Scripts" if os.name == "nt" else "bin")
                mcp_python = mcp_binaries / ("python.exe" if os.name == "nt" else "python")
                try:
                    run(mcp_python, "-m", "pip", "install", "--no-index", "--no-deps", wheel)
                    if mcp_wheel is not None:
                        run(mcp_python, "-m", "pip", "install", "--no-index", "--no-deps", mcp_wheel)
                    else:
                        run(
                            mcp_python,
                            "-m",
                            "pip",
                            "install",
                            "--no-index",
                            "--find-links",
                            wheelhouse,
                            "mcp>=2,<3",
                        )
                    mcp_probe = """
import asyncio
import tempfile
from pathlib import Path
from mcp import Client
from cmpath.mcp_server import create_server


async def probe():
    with tempfile.TemporaryDirectory() as root:
        server = create_server(Path(root) / "mcp.db")
        try:
            async with Client(server) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert {"cmp_context", "cmp_codex_event"} <= names, names
        finally:
            server._cmp_memory.close()


asyncio.run(probe())
"""
                    run(mcp_python, "-c", mcp_probe)
                except RuntimeError as exc:
                    if args.mcp_required:
                        raise
                    checks["optional_mcp_validation"] = {
                        "status": "skipped",
                        "reason": str(exc).splitlines()[-1][-500:],
                    }
                else:
                    checks["optional_mcp_validation"] = {
                        "status": "pass",
                        "environment": str(mcp_target),
                    }

        database = work / "workflow.db"

        def memory(*args, db=database):
            return json.loads(run(cli, "--db", db, *args))

        memory("init")
        task = memory("create", "Release verification", "--alias", "release probe",
                      "--snapshot", '{"next_action":"check invoice"}')
        task_id = str(task["id"])
        evidence = memory("append", task_id, "user", "--text", "Approved release budget is 3400 USD.")
        evidence_id = evidence["citation"].split(":M")[1]
        memory("set-fact", task_id, "budget", "3400", "--evidence", evidence_id)
        resolved = memory("resolve", "Return to the release probe")
        assert resolved["status"] == "resolved" and resolved["task_id"] == task["id"]
        resumed = memory("resume", task_id)
        assert resumed["snapshot"] == {"next_action": "check invoice"}
        assert resumed["active_task"] == resumed["task_id"] == task["id"]
        payload = memory("context", task_id, "What budget is approved?", "--budget", "1200", "--reserve", "200")
        assert payload["used_units"] <= payload["input_allowance"] == 1000
        assert evidence["citation"] in payload["citations"]
        assert "3400" in json.dumps(payload["messages"])
        backup = work / "backup.db"
        memory("backup", backup)
        assert memory("check", db=backup)["ok"]
        assert memory("resolve", "release probe", db=backup)["status"] == "resolved"
        checks["cli_persistent_state_fact_context_and_backup"] = True
        example = run(python, ROOT / "examples" / "task_workflow.py")
        assert "Restored state:" in example and "3400" in example
        run(python, ROOT / "scripts" / "run_reader.py", "--help")
        checks["installed_example"] = True
        checks["reader_help_without_network"] = True

        def native_workflows(binary, example_binary, prefix):
            input_file = work / (prefix + "-input.txt")
            original = b"Release verification: preserved source, native execution.\n"
            input_file.write_bytes(original)
            expected = hashlib.sha256(original).hexdigest()
            bridge_db = work / (prefix + "-bridge.db")
            go_db = work / (prefix + "-go.db")
            python_command = [python, ROOT / "examples" / "native_workflow.py", "--binary", binary,
                              "--file", input_file, "--db", bridge_db, "--request-id", "release-checksum"]
            go_command = [example_binary, "--input", input_file, "--db", go_db,
                          "--request-id", "release-checksum"]
            bridge_first = json.loads(run(*python_command))
            go_first = json.loads(run(*go_command))
            assert json.loads(bridge_first["text"])["sha256"] == expected
            assert json.loads(go_first["reply"]["text"])["sha256"] == expected
            # Alter the real input: replay must retain the recorded outcome, not rerun the tool.
            input_file.write_bytes(b"This is a later file revision.\n")
            assert json.loads(run(*python_command)) == bridge_first
            go_replay = json.loads(run(*go_command))
            assert go_replay["reply"] == go_first["reply"] and go_replay["replayed"]
            inspect_code = (
                "import json,sqlite3,sys; from cmpath import TaskMemory; "
                "db=sqlite3.connect(sys.argv[1]); "
                "counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] "
                "for t in ['messages','cmp_turns','cmp_tool_calls','cmp_model_calls']}; "
                "assert counts=={'messages':3,'cmp_turns':1,'cmp_tool_calls':1,'cmp_model_calls':0},counts; "
                "db.close(); memory=TaskMemory(sys.argv[1]); memory.backup(sys.argv[2]); memory.close(); "
                "backup=sqlite3.connect(sys.argv[2]); "
                "assert backup.execute('SELECT count(*) FROM cmp_tool_calls').fetchone()[0]==1; "
                "assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
                "print(json.dumps(counts))"
            )
            for index, db in enumerate((bridge_db, go_db)):
                run(python, "-c", inspect_code, db, work / f"{prefix}-backup-{index}.db")

        native_workflows(native, embedded, "supplied")
        run(python, ROOT / "examples" / "native_chat.py", "--help")
        agent_cli = binaries / ("cmpath-agent.exe" if os.name == "nt" else "cmpath-agent")
        maintenance_cli = binaries / ("cmpath-maintain.exe" if os.name == "nt" else "cmpath-maintain")
        run(agent_cli, "--help")
        run(maintenance_cli, "--help")
        probe_db = work / "supplied-bridge.db"
        inspection = json.loads(run(agent_cli, "--binary", native, "--db", probe_db,
                                    "--request-id", "release-checksum", "--inspect"))
        assert inspection["turn"]["status"] == "committed"
        maintenance = [maintenance_cli, "--binary", native, "--db", probe_db]
        info = json.loads(run(*maintenance, "info"))
        assert info["version"] == VERSION and info["harness_schema"] == 4
        exported = work / "installed-journal.jsonl"
        assert json.loads(run(*maintenance, "export", exported))["rows"]["cmp_turns"] == 1
        plan = json.loads(run(*maintenance, "plan", "--before", "2100-01-01T00:00:00Z"))
        applied = json.loads(run(*maintenance, "apply", "--before", plan["cutoff"],
                                  "--plan-hash", plan["plan_hash"]))
        assert applied["applied"] and applied["rows"]["cmp_retired_turns"] == 1
        assert json.loads(run(*maintenance, "info"))["cmp_retired_turns"] == 1
        checks["installed_native_bridge_and_embedded_real_tool"] = True
        checks["committed_replay_does_not_reread_changed_input"] = True
        checks["native_journal_survives_legacy_sqlite_backup"] = True
        checks["native_chat_help_without_network"] = True
        checks["installed_agent_inspect_without_provider"] = True
        checks["installed_maintenance_export_plan_and_retire"] = True

        with tarfile.open(source) as archive:
            archive.extractall(work / "source", filter="data")
        extracted = work / "source" / f"cmpath-{VERSION}"
        required = ["docs/RELEASE.md", "tests/test_memory.py", "tests/test_migration.py",
                    "tests/test_evaluation.py", "scripts/benchmark_public.py", "PAPER.md",
                    "research/PROTOCOL.md", "figures/public_recall.png", "docs/HARNESS.md",
                    "tests/test_harness.py", "NATIVE_ARCHITECTURE.md", "research/NATIVE_SOURCES.md",
                    "native/engine/engine.go", "native/engine/base.sql", "native/engine/journal.sql",
                    "native/internal/sqlite/sqlite3.c", "native/internal/sqlite/sqlite3.h",
                    "native/vendor/golang.org/x/text/LICENSE", "native/third_party/GO_LICENSE",
                    "native/go.mod", "native/go.sum", "scripts/build_native.py",
                    "scripts/benchmark_native.py", "results/native/benchmark.json",
                    "src/cmpath/agent.py", "src/cmpath/request_counter.py", "src/cmpath/maintenance_cli.py",
                    "native/engine/maintenance.go", "native/engine/model_response.go",
                    "tests/test_agent.py", "tests/test_native_maintenance.py",
                    "research/PILOT_PROTOCOL.md", "docs/AGENT_PILOT.md", "PILOT_RESULTS.md"]
        for name in required:
            assert (extracted / name).is_file(), name
        run(sys.executable, extracted / "scripts" / "build_release.py", cwd=extracted)
        rebuilt = extracted / "dist" / wheel.name
        with zipfile.ZipFile(wheel) as first, zipfile.ZipFile(rebuilt) as second:
            package_files = [name for name in first.namelist() if name.startswith("cmpath/")]
            for name in package_files:
                assert first.read(name) == second.read(name), name
        checks["source_distribution_rebuild"] = True
        checks["rebuilt_runtime_identical"] = True
        native_output = work / "rebuilt-native"
        run(sys.executable, extracted / "scripts" / "build_native.py", "--go", args.go,
            "--output", native_output, cwd=extracted)
        suffix = ".exe" if os.name == "nt" else ""
        rebuilt_native = native_output / ("cmpath-native" + suffix)
        rebuilt_embedded = native_output / ("cmpath-embedded-example" + suffix)
        assert run(rebuilt_native, "--version").strip() == VERSION
        native_workflows(rebuilt_native, rebuilt_embedded, "rebuilt")
        checks["vendored_native_source_rebuild_without_dependency_downloads"] = True
        checks["rebuilt_native_real_tool_replay_and_backup"] = True

    result = {"release": VERSION, "python": platform.python_version(),
              "platform": platform.platform(), "checks": checks,
              "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "native_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
              "embedded_example_sha256": hashlib.sha256(embedded.read_bytes()).hexdigest(),
              "model_calls": 0}
    output = ROOT / "results" / "release_validation.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
