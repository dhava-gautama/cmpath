"""Focused tests for native toolchain discovery and module entry points."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import build_native


class NativeToolchainTests(unittest.TestCase):
    def test_msys2_ucrt64_root_is_discovered_without_path(self):
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root) / "ucrt64" / "bin"
            prefix.mkdir(parents=True)
            compiler = prefix / "gcc.exe"
            compiler.write_bytes(b"test compiler marker")
            environment = {"MSYS2_ROOT": root, "PATH": ""}
            with mock.patch.object(build_native, "_is_windows", return_value=True):
                discovered = build_native._discover_msys2_ucrt64(environment)
            self.assertEqual(Path(discovered), compiler.resolve())

    def test_compiler_check_sets_child_cc_and_runtime_path(self):
        with tempfile.TemporaryDirectory() as root:
            compiler = Path(root) / "ucrt64" / "bin" / "gcc.exe"
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"test compiler marker")
            environment = {"MSYS2_ROOT": root, "PATH": "C:\\Windows\\System32"}
            with mock.patch.object(build_native, "_is_windows", return_value=True):
                resolved = build_native._require_compiler(environment, "windows")
            self.assertEqual(Path(resolved), compiler.resolve())
            self.assertEqual(environment["CC"], str(compiler.resolve()))
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(compiler.parent.resolve()))

    def test_msvc_is_rejected_for_cgo(self):
        with tempfile.TemporaryDirectory() as root:
            compiler = Path(root) / "cl.exe"
            compiler.write_bytes(b"test compiler marker")
            environment = {"CC": str(compiler), "PATH": ""}
            with mock.patch.object(build_native, "_is_windows", return_value=True):
                with self.assertRaisesRegex(ValueError, "GCC-compatible"):
                    build_native._require_compiler(environment, "windows")


class ModuleEntrypointTests(unittest.TestCase):
    def _environment(self):
        environment = os.environ.copy()
        source = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        return environment

    def test_cli_module_dispatches_help(self):
        completed = subprocess.run(
            [sys.executable, "-m", "cmpath.cli", "--help"],
            env=self._environment(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Durable task memory", completed.stdout)

    def test_doctor_module_dispatches_json(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            completed = subprocess.run(
                [
                    sys.executable, "-m", "cmpath.doctor", "--json",
                    "--skip-native", "--skip-mcp", "--db", str(directory / "missing.db"),
                    "--export-dir", str(directory),
                ],
                env=self._environment(), text=True, capture_output=True, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('"package_version"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
