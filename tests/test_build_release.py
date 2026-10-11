"""Release packaging tests use synthetic sources and temporary output only."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "source"
        for relative in builder.REQUIRED_FILES:
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# public source\n", encoding="utf-8")
        (self.project / "history_sync.py").write_text("SOFTWARE_VERSION = '1.1.0'\n", encoding="utf-8")
        self.output = self.root / "release" / "package.zip"

    def test_allowlist_checksums_and_deterministic_archive(self):
        (self.project / "tests" / "test_additional.ps1").write_text("# public test\n")
        (self.project / ".github" / "ISSUE_TEMPLATE.md").write_text("# public issue\n")
        for relative in ("runtime/auth.json", "runtime/session.sqlite", "logs/private.log", "__pycache__/private.pyc", ".git/config", "tests/run-old/auth.json"):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must never be read or packaged")
        ignored = {"auth.json", "session.sqlite", "private.log", "private.pyc", "config"}
        original = Path.read_bytes
        def no_private_reads(path):
            if path.name in ignored:
                raise AssertionError("Private artifact was read")
            return original(path)
        with mock.patch.object(Path, "read_bytes", no_private_reads):
            result = builder.build_release(self.project, self.output)
        self.assertEqual(result["file_count"], len(builder.REQUIRED_FILES) + 2)
        with zipfile.ZipFile(self.output) as archive:
            entries = archive.namelist()
            self.assertTrue(all(name.startswith("codex-history-sync/") for name in entries))
            self.assertFalse(any("runtime" in name or ".git/" in name or "run-old" in name for name in entries))
            manifest = archive.read("codex-history-sync/SHA256SUMS").decode()
            rows = [line.split("  ", 1) for line in manifest.splitlines()]
            self.assertEqual(len(rows), result["file_count"])
            for checksum, relative in rows:
                self.assertEqual(checksum, builder.sha256(archive.read("codex-history-sync/" + relative)))
        self.assertEqual(result["sha256"], builder.sha256(self.output.read_bytes()))
        self.assertEqual(Path(result["sha256_file"]).read_text(), result["sha256"] + "  package.zip\n")
        second = self.root / "second.zip"
        builder.build_release(self.project, second)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())

    def test_missing_expected_file_and_unlisted_source_fail_before_output(self):
        (self.project / "docs" / "SAFETY.md").unlink()
        with self.assertRaises(builder.ReleaseError) as error:
            builder.build_release(self.project, self.output)
        self.assertEqual(error.exception.code, "required_files_missing")
        self.assertFalse(self.output.parent.exists())
        (self.project / "docs" / "SAFETY.md").write_text("# public\n")
        (self.project / "forgotten_helper.py").write_text("# cannot silently omit new source\n")
        with self.assertRaises(builder.ReleaseError) as error:
            builder.build_release(self.project, self.output)
        self.assertEqual(error.exception.code, "file_not_allowlisted")

    def test_private_root_configuration_is_rejected_by_filename_only(self):
        (self.project / "config.json").write_text("not-a-real-config")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("No private configuration should be read")):
            with self.assertRaises(builder.ReleaseError) as error:
                builder.collect_release_files(self.project)
        self.assertEqual(error.exception.code, "private_config_present")

    def test_personal_references_and_non_utf8_are_rejected(self):
        account = "local-person"
        windows_users = "C:/" + "Users/"
        other_windows_users = "D:/" + "Users/"
        linux_home = "/" + "home/"
        cases = (
            "C:" + "\\" + "Users" + "\\" + account + "\\.codex",
            other_windows_users + account + "/project",
            linux_home + account + "/.codex",
            windows_users + "local person" + "/.codex",
            str(self.project),
        )
        readme = self.project / "README.md"
        for value in cases:
            readme.write_text(value, encoding="utf-8")
            with self.assertRaises(builder.ReleaseError) as error:
                builder.collect_release_files(self.project)
            self.assertEqual(error.exception.code, "personal_reference")
            self.assertNotIn(value, str(error.exception))
        readme.write_bytes(b"invalid\xffencoding")
        with self.assertRaises(builder.ReleaseError) as error:
            builder.collect_release_files(self.project)
        self.assertEqual(error.exception.code, "text_encoding")

    def test_portable_user_placeholders_and_python_directories_are_allowed(self):
        cases = (
            "C:/Users/<user>/.codex", r"C:\Users\<your-windows-user>\.codex",
            "C:/Users/user/.codex", "C:/Users/%USERNAME%/.codex",
            "%USERPROFILE%/.codex", "/home/your-linux-user/.codex",
            "/home/<user>/.codex", "/home/user/.codex",
            "/home/your_username/.codex", "/home/${USER}/.codex",
        )
        cases += tuple("C:/Tools/Python" + version + "/python.exe" for version in ("311", "312", "314"))
        for value in cases:
            with self.subTest(value=value):
                builder.check_personal_references(value.encode(), self.project)

    def test_current_machine_home_is_detected_without_fixed_account_rules(self):
        home = Path("/srv/machine-home").resolve()
        with mock.patch.object(builder.Path, "home", return_value=home):
            with self.assertRaises(builder.ReleaseError) as error:
                builder.check_personal_references(str(home).encode(), self.project)
        self.assertEqual(error.exception.code, "personal_reference")

    def test_absolute_project_path_is_rejected_without_a_user_directory(self):
        project = Path("/srv/public-project").resolve()
        with self.assertRaises(builder.ReleaseError) as error:
            builder.check_personal_references(str(project).encode(), project)
        self.assertEqual(error.exception.code, "personal_reference")

    def test_check_only_and_source_change_do_not_publish_an_archive(self):
        result = builder.build_release(self.project, self.output, check_only=True)
        self.assertEqual(result["status"], "checked")
        self.assertFalse(self.output.parent.exists())
        original = builder.verify_archive
        def change_after_verify(path, payload):
            original(path, payload)
            (self.project / "README.md").write_text("# changed while building\n")
        with mock.patch.object(builder, "verify_archive", side_effect=change_after_verify):
            with self.assertRaises(builder.ReleaseError) as error:
                builder.build_release(self.project, self.output)
        self.assertEqual(error.exception.code, "source_changed")
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_zip_tampering_is_rejected_and_existing_outputs_are_preserved(self):
        builder.build_release(self.project, self.output)
        original = self.output.read_bytes()
        with self.assertRaises(builder.ReleaseError) as error:
            builder.build_release(self.project, self.output)
        self.assertEqual(error.exception.code, "output_exists")
        self.assertEqual(self.output.read_bytes(), original)
        _, payload, _ = builder.collect_release_files(self.project)
        with zipfile.ZipFile(self.output, "a") as archive:
            archive.writestr("codex-history-sync/unexpected.txt", "unverified")
        with self.assertRaises(builder.ReleaseError) as error:
            builder.verify_archive(self.output, payload)
        self.assertEqual(error.exception.code, "archive_mismatch")

    def test_output_inside_project_is_rejected(self):
        with self.assertRaises(builder.ReleaseError) as error:
            builder.build_release(self.project, self.project / "release.zip")
        self.assertEqual(error.exception.code, "output_invalid")

    def test_concurrent_output_is_not_overwritten(self):
        original = builder.verify_archive
        def create_concurrent_output(path, payload):
            original(path, payload)
            self.output.write_bytes(b"another-build")
        with mock.patch.object(builder, "verify_archive", side_effect=create_concurrent_output):
            with self.assertRaises(FileExistsError):
                builder.build_release(self.project, self.output)
        self.assertEqual(self.output.read_bytes(), b"another-build")
        self.assertFalse(self.output.with_name(self.output.name + ".sha256").exists())

    def test_symlink_source_is_not_packaged(self):
        outside = self.root / "outside.md"
        outside.write_text("not part of the project")
        try:
            (self.project / "docs" / "linked.md").symlink_to(outside)
        except OSError:
            self.skipTest("Creating symbolic links is not permitted on this host")
        with self.assertRaises(builder.ReleaseError) as error:
            builder.collect_release_files(self.project)
        self.assertEqual(error.exception.code, "source_symlink")

    def test_default_output_is_versioned_outside_project(self):
        result = builder.build_release(self.project)
        path = Path(result["archive"])
        self.assertEqual(path, self.root / "releases" / "codex-history-sync-1.1.0.zip")
        self.assertFalse(path.is_relative_to(self.project))

    def test_extracted_release_can_be_checked_and_rebuilt_without_self_hashing(self):
        # Package the actual tool so this also checks its project-relative CLI.
        (self.project / "tools" / "build_release.py").write_bytes((ROOT / "tools" / "build_release.py").read_bytes())
        original = builder.build_release(self.project, self.output)
        unpacked_parent = self.root / "extracted"
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(unpacked_parent)
        unpacked = unpacked_parent / builder.ARCHIVE_ROOT
        self.assertTrue((unpacked / "SHA256SUMS").is_file())
        (unpacked / "SHA256SUMS.txt").write_text("generated manifest is never source\n")
        command = [sys.executable, str(unpacked / "tools" / "build_release.py")]
        checked_process = subprocess.run(command + ["--check-only"], capture_output=True, text=True, timeout=15, cwd=self.root)
        self.assertEqual(checked_process.returncode, 0, checked_process.stderr)
        checked = json.loads(checked_process.stdout)
        self.assertEqual(checked["file_count"], original["file_count"])
        self.assertNotIn("SHA256SUMS", checked["files"])
        self.assertNotIn("SHA256SUMS.txt", checked["files"])
        second = self.root / "release" / "rebuilt.zip"
        rebuilt_process = subprocess.run(command + ["--output", str(second)], capture_output=True, text=True, timeout=15, cwd=self.root)
        self.assertEqual(rebuilt_process.returncode, 0, rebuilt_process.stderr)
        result = json.loads(rebuilt_process.stdout)
        self.assertEqual(result["sha256"], original["sha256"])
        self.assertEqual(second.read_bytes(), self.output.read_bytes())
        with zipfile.ZipFile(second) as archive:
            manifest = archive.read(builder.ARCHIVE_ROOT + "/SHA256SUMS").decode()
        self.assertFalse(any(line.endswith("  SHA256SUMS") or line.endswith("  SHA256SUMS.txt") for line in manifest.splitlines()))


if __name__ == "__main__":
    unittest.main()
