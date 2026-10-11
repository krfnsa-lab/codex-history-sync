"""Isolated process/diagnostic tests; never use a real Codex home."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

try:
    from .test_endpoint import ep, fixture, ID
except ImportError:
    from test_endpoint import ep, fixture, ID


class BackendStatusTests(unittest.TestCase):
    def test_name_guard_covers_native_backends_without_matching_helpers(self):
        for name in ("codex.exe", "Codex.exe", "codex-x86_64-pc-windows-msvc.exe"):
            self.assertTrue(ep.is_native_codex_process(name, "windows"))
        for name in ("codex", "codex-x64", "codex-app-server"):
            self.assertTrue(ep.is_native_codex_process(name, "linux"))
        for name in ("python.exe", "python3", "history_endpoint.py", "history_sync.py", "not-codex", "codex.js"):
            self.assertFalse(ep.is_native_codex_process(name, "windows"))
            self.assertFalse(ep.is_native_codex_process(name, "linux"))

    def test_native_windows_scan_is_sanitized(self):
        with mock.patch.object(ep.os, "name", "nt"), mock.patch.object(ep, "windows_process_names", return_value=iter(("python.exe", "codex.exe", "Codex.exe"))):
            value = ep.backend_status()
        self.assertTrue(value["backend_running"])
        self.assertTrue(value["backend_detection_ok"])
        self.assertEqual(value["backend_count"], 2)
        self.assertEqual(value["backend_kinds"], {"native_codex": 2})
        self.assertNotIn("python.exe", json.dumps(value))

    def test_process_scan_failure_and_partial_results_fail_closed(self):
        def denied_scan():
            yield "codex"
            raise PermissionError("secret commandline must not be disclosed")
        with mock.patch.object(ep.os, "name", "posix"), mock.patch.object(ep, "linux_process_names", side_effect=denied_scan):
            value = ep.backend_status()
        self.assertTrue(value["backend_running"])
        self.assertFalse(value["backend_detection_ok"])
        self.assertIsNone(value["backend_count"])
        self.assertNotIn("secret", json.dumps(value))

    def test_successful_empty_linux_snapshot_is_stopped(self):
        with mock.patch.object(ep.os, "name", "posix"), mock.patch.object(ep, "linux_process_names", return_value=iter(("python3", "systemd"))):
            value = ep.backend_status()
        self.assertFalse(value["backend_running"])
        self.assertTrue(value["backend_detection_ok"])
        self.assertEqual(value["backend_count"], 0)

    def test_linux_only_reads_comm_and_tolerates_process_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for pid, name in (("900001", "codex"), ("900002", "python3")):
                directory = root / pid
                directory.mkdir()
                (directory / "comm").write_text(name + "\n")
                (directory / "cmdline").write_text("secret-token-and-prompt")
            (root / "900003").mkdir()  # Process vanished before comm was read.
            (root / "uptime").write_text("unrelated")
            original = Path.open
            def no_commandlines(path, *args, **kwargs):
                if path.name == "cmdline":
                    raise AssertionError("Process arguments were accessed")
                return original(path, *args, **kwargs)
            with mock.patch.object(Path, "open", no_commandlines):
                names = list(ep.linux_process_names(root))
            self.assertEqual(sorted(names), ["codex", "python3"])

    def test_backend_command_accepts_existing_interface_flags(self):
        args = ep.build_parser().parse_args(("backend-status", "--home", "dummy", "--scope", "interactive", "--quiet-seconds", "60"))
        self.assertEqual(args.command, "backend-status")
        self.assertEqual(args.quiet_seconds, 60)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = fixture(self.root / "synthetic-home", extra_column=True)
        self.backend_patch = mock.patch.object(ep, "backend_status", return_value={
            "backend_running": False, "backend_detection_ok": True,
            "backend_detection_code": "stopped", "backend_count": 0,
            "backend_kinds": {"native_codex": 0}, "backend_guard_scope": "native_os_codex_processes"})
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)

    def args(self, **values):
        defaults = {"home": str(self.home), "scope": "interactive", "quiet_seconds": 60, "quick_check": False}
        defaults.update(values)
        return argparse.Namespace(**defaults)

    def fingerprint_files(self):
        return {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}

    def test_doctor_reports_counts_without_hashing_transcripts_or_changing_files(self):
        before = self.fingerprint_files()
        with mock.patch.object(ep, "file_signature", side_effect=AssertionError("Doctor must not hash or parse rollouts")):
            result = ep.doctor_home(self.args())
        self.assertTrue(result["healthy"])
        self.assertEqual(result["record_counts"]["thread_count"], 1)
        self.assertEqual(result["record_counts"]["history_mode_counts"], {"paginated": 1})
        self.assertEqual(result["record_counts"]["cache_row_counts"]["thread_items"], 1)
        self.assertEqual(before, self.fingerprint_files())
        serialized = json.dumps(result)
        for secret in ("source text", "protected-auth", "protected-config", "Original name"):
            self.assertNotIn(secret, serialized)

    def test_doctor_summarizes_missing_outside_and_incomplete_rollouts(self):
        state = sqlite3.connect(self.home / "state_5.sqlite")
        original = state.execute("SELECT * FROM threads").fetchone()
        for new_id, path in (("missing", self.home / "sessions" / "missing.jsonl"), ("outside", self.root / "elsewhere.jsonl")):
            values = list(original)
            values[0], values[1] = new_id, str(path)
            state.execute("INSERT INTO threads VALUES(" + ",".join("?" for _ in values) + ")", values)
        state.commit(); state.close()
        rollout = next((self.home / "sessions").rglob("*.jsonl"))
        rollout.write_bytes(b'{"not_complete":true}')
        result = ep.doctor_home(self.args())
        counts = result["record_counts"]
        self.assertEqual(counts["thread_count"], 3)
        self.assertEqual(counts["rollout_missing"], 1)
        self.assertEqual(counts["rollout_outside_home"], 1)
        self.assertEqual(counts["rollout_incomplete"], 1)

    def test_pending_and_malformed_receipts_only_expose_status_counts(self):
        for name, content in (("pending", '{"status":"pending","id":"secret-id","auth":"do-not-print"}'), ("broken", "malformed-secret")):
            directory = self.home / ".history-sync-backups" / ID / name
            directory.mkdir(parents=True)
            (directory / "journal.json").write_text(content)
        result = ep.doctor_home(self.args())
        self.assertFalse(result["healthy"])
        self.assertEqual(result["journals"]["counts"]["pending"], 1)
        self.assertEqual(result["journals"]["counts"]["invalid"], 1)
        self.assertIn("pending_recovery", [check["code"] for check in result["checks"]])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("do-not-print", json.dumps(result))

    def test_symlink_receipt_root_is_not_followed(self):
        original = Path.is_symlink
        def pretend_link(path):
            return path.name == ".history-sync-backups" or original(path)
        with mock.patch.object(Path, "is_symlink", pretend_link):
            value = ep.journal_summary(self.home)
        self.assertEqual(value["examined"], 0)
        self.assertEqual(value["counts"]["invalid"], 1)

    def test_unknown_populated_schema_and_history_modes_are_errors(self):
        connection = sqlite3.connect(self.home / "thread_history_1.sqlite")
        connection.execute("ALTER TABLE thread_items ADD COLUMN unknown_semantics TEXT DEFAULT 'private' ")
        connection.commit(); connection.close()
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute("UPDATE threads SET history_mode='futureMode'")
        connection.commit(); connection.close()
        result = ep.doctor_home(self.args())
        self.assertFalse(result["healthy"])
        codes = {check["code"] for check in result["checks"]}
        self.assertIn("history_schema_unsupported", codes)
        self.assertIn("history_mode_unsupported", codes)
        self.assertNotIn("private", json.dumps(result))

    def test_optional_quick_check_and_missing_home_are_read_only(self):
        before = self.fingerprint_files()
        result = ep.doctor_home(self.args(quick_check=True))
        self.assertTrue(result["healthy"])
        self.assertEqual(result["databases"]["state"]["quick_check"], "ok")
        self.assertEqual(result["databases"]["history"]["quick_check"], "ok")
        self.assertEqual(before, self.fingerprint_files())
        result = ep.doctor_home(self.args(home=str(self.root / "absent")))
        self.assertFalse(result["healthy"])
        self.assertFalse((self.root / "absent").exists())

    def test_inspect_keeps_records_and_adds_backend_guard(self):
        result = ep.inspect_home(self.args())
        self.assertEqual(result["threads"][0]["id"], ID)
        self.assertFalse(result["backend_running"])
        self.assertTrue(result["backend_detection_ok"])


if __name__ == "__main__":
    unittest.main()
