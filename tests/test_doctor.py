import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cmpath.doctor import (
    check_atomic_export,
    check_mcp,
    check_native,
    format_report,
    run_doctor,
)


class DoctorTests(unittest.TestCase):
    def test_report_has_named_checks_and_machine_text_renderers(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            report = run_doctor(
                directory / "memory.db",
                skip_native=True,
                skip_mcp=True,
                export_dir=directory,
            )
            self.assertTrue(report["ok"], report)
            self.assertEqual(
                set(report["checks"]),
                {
                    "python",
                    "platform",
                    "sqlite_fts5",
                    "native",
                    "mcp",
                    "filesystem_atomic_export",
                    "database",
                },
            )
            self.assertEqual(report["checks"]["sqlite_fts5"]["status"], "pass")
            self.assertEqual(report["checks"]["filesystem_atomic_export"]["status"], "pass")
            self.assertIn("CMPATH doctor: OK", format_report(report))
            encoded = json.loads(format_report(report, as_json=True))
            self.assertEqual(encoded["package_version"], report["package_version"])

    def test_atomic_export_probe_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            result = check_atomic_export(directory)
            self.assertEqual(result["status"], "pass", result)
            self.assertFalse(list(directory.glob(".cmpath-doctor-*")))
            self.assertTrue(result["details"]["hard_link"])
            self.assertIn(result["details"]["directory_sync"], {"supported", "not_supported"})

    def test_atomic_export_probe_reports_hard_link_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            with mock.patch("cmpath.doctor.os.link", side_effect=OSError("hard links unsupported")):
                result = check_atomic_export(directory)
            expected_status = "pass" if os.name == "nt" else "warn"
            self.assertEqual(result["status"], expected_status, result)
            self.assertFalse(result["details"]["hard_link"])
            self.assertTrue(result["details"]["fallback"])
            self.assertTrue(result["details"]["no_overwrite"])
            self.assertEqual(result["details"]["publication"], "rename_fallback")
            if os.name == "nt":
                self.assertNotIn("reduced_guarantee", result["details"])
            self.assertFalse(list(directory.glob(".cmpath-doctor-*")))

    def test_missing_optional_mcp_is_skipped_without_client_configuration(self):
        with mock.patch("cmpath.doctor.importlib.util.find_spec", return_value=None), mock.patch(
            "cmpath.doctor._config_paths", return_value=[]
        ):
            result = check_mcp()
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(result["details"]["dependency"]["status"], "skip")
        self.assertEqual(result["details"]["configs"], [])

    def test_explicit_missing_native_path_is_actionable_failure(self):
        with tempfile.TemporaryDirectory() as root:
            result = check_native(Path(root) / "missing-native")
            self.assertEqual(result["status"], "fail")
            self.assertTrue(result["required"])
            self.assertIn("Rebuild", result["action"])

    def test_explicit_mcp_config_is_structurally_validated_without_starting_it(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "mcp.json"
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "cmpath-memory": {
                                "command": sys.executable,
                                "args": ["-m", "cmpath.mcp_server"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = check_mcp([config])
            self.assertIn(result["status"], {"pass", "warn"})
            self.assertEqual(len(result["details"]["configs"]), 1)
            entry = result["details"]["configs"][0]
            self.assertTrue(entry["valid"])
            self.assertEqual(entry["command"], sys.executable)

    def test_cli_doctor_json_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            database = directory / "missing.db"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cmpath",
                    "doctor",
                    "--db",
                    str(database),
                    "--json",
                    "--skip-native",
                    "--skip-mcp",
                    "--export-dir",
                    str(directory),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["checks"]["database"]["status"], "skip")
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
