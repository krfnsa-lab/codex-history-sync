"""Native, isolated progress tests: telemetry never authorizes a history write."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from tests.test_endpoint import ENDPOINT, ID, ep, fixture


class Recorder:
    def __init__(self, callback=None):
        self.events = []
        self.callback = callback

    def update(self, phase, **fields):
        self.events.append((phase, fields))
        if self.callback:
            self.callback(phase)

    def finish(self, status, **fields):
        self.events.append(("finish:" + status, fields))


class EndpointProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="endpoint-progress-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = fixture(self.root / "source")
        self.target = fixture(self.root / "target", with_chat=False, extra_column=True)
        self.package = self.root / "package"

    def args(self, **values):
        result = dict(home=str(self.source), scope="cli,vscode", quiet_seconds=60,
                      id=ID, package=str(self.package), expected=None,
                      metadata_only=False, allow_existing=False)
        result.update(values)
        return argparse.Namespace(**result)

    def export(self, **values):
        return ep.export_chat(self.args(**values))

    def test_scan_counts_scope_and_failed_records_as_processed(self):
        with contextlib.closing(sqlite3.connect(self.source / "state_5.sqlite")) as state, state:
            state.execute("INSERT INTO threads SELECT 'broken', 'outside-home', created_at, updated_at, source, "
                          "model_provider, cwd, title, sandbox_policy, approval_mode, name, archived, archived_at, "
                          "history_mode, project_id, thread_section_id, is_pinned FROM threads")
            state.execute("INSERT INTO threads SELECT 'excluded', rollout_path, created_at, updated_at, 'excluded-source', "
                          "model_provider, cwd, title, sandbox_policy, approval_mode, name, archived, archived_at, "
                          "history_mode, project_id, thread_section_id, is_pinned FROM threads WHERE id=?", (ID,))
        recorder = Recorder()
        result = ep.inspect_home(self.args(_progress=recorder))
        scans = [fields for phase, fields in recorder.events if phase == "scanning" and fields]
        self.assertEqual([(entry["processed_records"], entry["total_records"]) for entry in scans],
                         [(0, 2), (1, 2), (2, 2)])
        self.assertEqual(len(result["threads"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(recorder.events[-1], ("finish:completed", {"processed_records": 2, "total_records": 2}))
        self.assertNotIn("Original", json.dumps(recorder.events))
        self.assertNotIn(str(self.source), json.dumps(recorder.events))

    def test_export_reports_real_read_copy_verify_order(self):
        recorder = Recorder()
        self.export(_progress=recorder)
        self.assertEqual([phase for phase, _ in recorder.events],
                         ["reading", "exporting", "verifying", "finish:completed"])
        self.assertTrue((self.package / "meta.json").is_file())

    def test_import_commit_event_is_after_visible_commit(self):
        self.export()
        observations = []
        def check_committed(phase):
            if phase == "committed":
                with contextlib.closing(sqlite3.connect(self.target / "state_5.sqlite")) as state:
                    observations.append(state.execute("SELECT COUNT(*) FROM threads WHERE id=?", (ID,)).fetchone()[0])
        recorder = Recorder(check_committed)
        ep.import_chat(self.args(home=str(self.target), expected="absent", _progress=recorder))
        self.assertEqual([phase for phase, _ in recorder.events],
                         ["verifying", "backing_up", "writing", "verifying", "committed", "finish:completed"])
        self.assertEqual(observations, [1])

    def test_backup_failure_never_reports_writing_or_completed(self):
        self.export()
        recorder = Recorder()
        with mock.patch.object(ep, "backup_chat", side_effect=OSError("private failure content")):
            with self.assertRaises(OSError):
                ep.import_chat(self.args(home=str(self.target), expected="absent", _progress=recorder))
        self.assertEqual([phase for phase, _ in recorder.events],
                         ["verifying", "backing_up", "finish:error"])
        with contextlib.closing(sqlite3.connect(self.target / "state_5.sqlite")) as state:
            self.assertEqual(state.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)
        self.assertNotIn("private failure", json.dumps(recorder.events))

    def test_commit_failure_rolls_back_without_committed_progress(self):
        self.export()
        original_connect = sqlite3.connect
        class FailingCommit:
            def __init__(self, connection):
                object.__setattr__(self, "connection", connection)
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def __setattr__(self, name, value):
                setattr(self.connection, name, value)
            def commit(self):
                raise sqlite3.OperationalError("simulated commit failure")
        def connect(path, *values, **kwargs):
            result = original_connect(path, *values, **kwargs)
            return FailingCommit(result) if Path(path) == self.target / "state_5.sqlite" else result
        recorder = Recorder()
        with mock.patch.object(ep.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(sqlite3.OperationalError):
                ep.import_chat(self.args(home=str(self.target), expected="absent", _progress=recorder))
        phases = [phase for phase, _ in recorder.events]
        self.assertIn("writing", phases)
        self.assertNotIn("committed", phases)
        self.assertNotIn("finish:completed", phases)
        self.assertEqual(phases[-1], "finish:error")
        with contextlib.closing(original_connect(self.target / "state_5.sqlite")) as state:
            self.assertEqual(state.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)
        self.assertFalse(any((self.target / "sessions").rglob("*.jsonl")))

    def test_unwritable_or_throwing_progress_does_not_change_export(self):
        recorder = Recorder()
        recorder.update = mock.Mock(side_effect=OSError("telemetry unavailable"))
        recorder.finish = mock.Mock(side_effect=OSError("telemetry unavailable"))
        result = self.export(_progress=recorder)
        self.assertTrue(result["ok"])
        self.assertTrue((self.package / "meta.json").is_file())
        self.assertIsNone(ep._PROGRESS_CONTEXT.get())

    def test_receipt_failure_after_commit_reports_error_without_false_rollback(self):
        self.export()
        write_json = ep.write_json
        def fail_final_receipt(path, value):
            if path.name == "journal.json" and value.get("status") == "committed":
                raise OSError("cannot publish committed receipt")
            return write_json(path, value)
        recorder = Recorder()
        with mock.patch.object(ep, "write_json", side_effect=fail_final_receipt):
            with self.assertRaises(OSError):
                ep.import_chat(self.args(home=str(self.target), expected="absent", _progress=recorder))
        phases = [phase for phase, _ in recorder.events]
        self.assertIn("committed", phases)
        self.assertNotIn("finish:completed", phases)
        self.assertEqual(phases[-1], "finish:error")
        with contextlib.closing(sqlite3.connect(self.target / "state_5.sqlite")) as state:
            self.assertEqual(state.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 1)
        journals = list((self.target / ".history-sync-backups").glob("*/*/journal.json"))
        self.assertEqual(json.loads(journals[0].read_text())["status"], "pending")

    def test_cli_single_json_and_optional_detail_file(self):
        detail = self.root / "detail.json"
        run_id, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
        command = [sys.executable, str(ENDPOINT), "inspect", "--home", str(self.source),
                   "--progress-file", str(detail), "--progress-run-id", run_id,
                   "--progress-operation-id", operation_id, "--progress-side", "windows"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertTrue(json.loads(result.stdout)["ok"])
        snapshot = json.loads(detail.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["run_id"], run_id)
        self.assertEqual(snapshot["operation_id"], operation_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual((snapshot["processed_records"], snapshot["total_records"]), (1, 1))

    def test_partial_or_invalid_progress_identity_is_ignored(self):
        detail = self.root / "detail.json"
        for extra in (dict(progress_file=str(detail)),
                      dict(progress_file=str(detail), progress_run_id="bad", progress_operation_id=str(uuid.uuid4()),
                           progress_side="wsl"),
                      dict(progress_file="relative.json", progress_run_id=str(uuid.uuid4()),
                           progress_operation_id=str(uuid.uuid4()), progress_side="wsl")):
            self.assertIsNone(ep.configured_progress(self.args(**extra)))
        self.assertFalse(detail.exists())
        self.assertTrue(ep.inspect_home(self.args())["ok"])

    def test_heartbeat_keeps_active_phase_and_counts_without_private_data(self):
        recorder = Recorder()
        @ep.endpoint_progress("scanning")
        def operation(args):
            ep.progress_update("scanning", processed_records=2, total_records=5)
            with mock.patch.object(ep.time, "monotonic", return_value=100):
                ep._PROGRESS_CONTEXT.get()["next_heartbeat"] = 99
                ep.progress_heartbeat()
                ep.progress_heartbeat()
            return {"ok": True}
        operation(self.args(_progress=recorder))
        scans = [entry for entry in recorder.events if entry[0] == "scanning"]
        self.assertEqual(len(scans), 3)
        self.assertEqual(scans[-1][1], {"processed_records": 2, "total_records": 5})


if __name__ == "__main__":
    unittest.main()
