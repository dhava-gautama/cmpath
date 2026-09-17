"""Focused tests for release payload/source reconciliation."""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
from io import BytesIO, StringIO
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_artifacts", ROOT / "scripts" / "verify_artifacts.py"
)
assert SPEC and SPEC.loader
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class ReleaseIntegrityTests(unittest.TestCase):
    def write_fixture(self, root: Path) -> None:
        (root / "src/cmpath").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "portable-kimi-hermes").mkdir()
        (root / "README.md").write_text("# fixture\n", encoding="utf-8")
        (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (root / "NOTICE.md").write_text("notice\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[project]\nname = 'cmpath'\nversion = '1.2.3a1'\n",
            encoding="utf-8",
        )
        (root / "MANIFEST.in").write_text(
            "include README.md LICENSE NOTICE.md\n"
            "recursive-include scripts *.sh\n"
            "graft portable-kimi-hermes\n",
            encoding="utf-8",
        )
        (root / "src/cmpath/__init__.py").write_text(
            '__version__ = "1.2.3a1"\n', encoding="utf-8"
        )
        (root / "scripts/run_mcp_wsl.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "portable-kimi-hermes/README.md").write_text(
            "portable\n", encoding="utf-8"
        )

    @staticmethod
    def write_wheel(root: Path, path: Path) -> None:
        files = {
            "cmpath/__init__.py": (root / "src/cmpath/__init__.py").read_bytes(),
            "cmpath-1.2.3a1.dist-info/METADATA": (
                b"Metadata-Version: 2.4\nName: cmpath\nVersion: 1.2.3a1\n"
            ),
            "cmpath-1.2.3a1.dist-info/WHEEL": (
                b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                b"Tag: py3-none-any\n"
            ),
        }
        rows = []
        for name, data in files.items():
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rows.append([name, "sha256=" + digest, str(len(data))])
        rows.append(["cmpath-1.2.3a1.dist-info/RECORD", "", ""])
        record = StringIO()
        writer = csv.writer(record, lineterminator="\n")
        writer.writerows(rows)
        files["cmpath-1.2.3a1.dist-info/RECORD"] = record.getvalue().encode()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)

    @staticmethod
    def write_sdist(root: Path, path: Path) -> None:
        files = verify.manifest_files(root)
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo("cmpath-1.2.3a1/" + name)
                info.size = len(data)
                archive.addfile(info, BytesIO(data))

    @staticmethod
    def write_portable(root: Path, path: Path) -> None:
        files = verify._portable_expected(root)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)

    def test_current_manifest_includes_package_and_portable_sources(self):
        files = verify.manifest_files(ROOT)
        self.assertIn("src/cmpath/doctor.py", files)
        self.assertIn("src/cmpath/mcp_server.py", files)
        self.assertIn("portable-kimi-hermes/install.py", files)
        self.assertNotIn("results/release_validation.json", files)
        self.assertFalse(any(name.startswith("native/native/") for name in files))

    def test_old_a4_artifacts_are_reported_stale_instead_of_accepted(self):
        old_dist = ROOT / "dist"
        wheel = old_dist / "cmpath-0.4.0a4-py3-none-any.whl"
        sdist = old_dist / "cmpath-0.4.0a4.tar.gz"
        portable = old_dist / "cmpath-kimi-hermes-0.4.0a4.zip"
        if not wheel.is_file():
            self.skipTest("historical distribution is not present")
        with self.assertRaises(verify.ArtifactMismatch) as raised:
            verify.verify_artifacts(root=ROOT, wheel=wheel, sdist=sdist, portable=portable)
        self.assertTrue(any("0.4.0a4" in error for error in raised.exception.errors))

    def test_matching_fixture_passes_and_payload_tamper_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            dist = Path(temporary) / "dist"
            root.mkdir()
            dist.mkdir()
            self.write_fixture(root)
            self.write_wheel(root, dist / "cmpath-1.2.3a1-py3-none-any.whl")
            self.write_sdist(root, dist / "cmpath-1.2.3a1.tar.gz")
            self.write_portable(root, dist / "cmpath-kimi-hermes-1.2.3a1.zip")
            report = verify.verify_artifacts(root=root, artifact_dir=dist)
            self.assertTrue(report["ok"])
            wheel = dist / "cmpath-1.2.3a1-py3-none-any.whl"
            tampered = dist / "tampered.whl"
            with zipfile.ZipFile(wheel) as original, zipfile.ZipFile(
                tampered, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for info in original.infolist():
                    data = original.read(info)
                    if info.filename == "cmpath/__init__.py":
                        data = b"stale\n"
                    archive.writestr(info.filename, data)
            errors = verify._verify_wheel(tampered, root, "1.2.3a1")
            self.assertTrue(any("RECORD hash mismatch" in error for error in errors))
            self.assertTrue(any("changed cmpath/__init__.py" in error for error in errors))

    def test_plugin_cache_accepts_compatibility_skill_but_checks_complete_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "plugin"
            cache = Path(temporary) / "cache"
            source.mkdir()
            cache.mkdir()
            (source / ".codex-plugin").mkdir()
            (source / "skills/cmpath-memory").mkdir(parents=True)
            (source / ".codex-plugin/plugin.json").write_text(
                '{"name":"cmpath-memory","version":"1.2.3-alpha.1"}\n',
                encoding="utf-8",
            )
            skill = "---\nname: cmpath-memory\n---\n"
            (source / "skills/cmpath-memory/SKILL.md").write_text(skill, encoding="utf-8")
            complete = cache / "1.2.3-alpha.1+test"
            shutil.copytree(source, complete)
            compatibility = cache / "1.2.3-alpha.1+old"
            (compatibility / "skills/cmpath-memory").mkdir(parents=True)
            (compatibility / "skills/cmpath-memory/SKILL.md").write_text(skill, encoding="utf-8")
            errors = []
            errors.extend(verify._verify_plugin_tree(complete, verify._plugin_expected(source), "1.2.3a1", "cache"))
            errors.extend(verify._verify_compatibility_skill(compatibility, source, "compat"))
            self.assertEqual(errors, [])

    def test_checksum_manifest_is_deterministic_and_detects_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            dist = Path(temporary) / "dist"
            root.mkdir()
            dist.mkdir()
            self.write_fixture(root)
            wheel = dist / "cmpath-1.2.3a1-py3-none-any.whl"
            source = dist / "cmpath-1.2.3a1.tar.gz"
            portable = dist / "cmpath-kimi-hermes-1.2.3a1.zip"
            self.write_wheel(root, wheel)
            self.write_sdist(root, source)
            self.write_portable(root, portable)
            manifest = verify.write_checksum_manifest([portable, wheel, source], dist / "SHA256SUMS")
            first = manifest.read_bytes()
            verify.write_checksum_manifest([source, portable, wheel], manifest)
            self.assertEqual(first, manifest.read_bytes())
            report = verify.verify_checksum_manifest(manifest, required=[wheel, source, portable])
            self.assertEqual(report["entries"], 3)
            wheel.write_bytes(wheel.read_bytes() + b"tampered")
            with self.assertRaises(verify.ArtifactMismatch) as raised:
                verify.verify_checksum_manifest(manifest, required=[wheel, source, portable])
            self.assertTrue(any("checksum mismatch" in error for error in raised.exception.errors))

    def test_checksum_manifest_rejects_absolute_and_traversal_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "SHA256SUMS"
            digest = "0" * 64
            for target in ("/outside", "../../outside", "C:\\outside"):
                manifest.write_text(f"{digest}  {target}\n", encoding="utf-8")
                with self.assertRaises(verify.ArtifactMismatch):
                    verify._read_checksum_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
