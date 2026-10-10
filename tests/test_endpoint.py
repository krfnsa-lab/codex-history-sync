"""Isolated tests: no real Codex home is modified."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

ENDPOINT = Path(__file__).resolve().parents[1] / "history_endpoint.py"
spec = importlib.util.spec_from_file_location("history_endpoint", ENDPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)
ID = "00000000-0000-4000-8000-000000000001"


def fixture(root, with_chat=True, extra_column=False, item_text="source text"):
    root.mkdir(parents=True)
    (root / "config.toml").write_text("protected-config\n")
    (root / "auth.json").write_text("protected-auth\n")
    c = sqlite3.connect(root / "state_5.sqlite")
    c.execute("CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY,checksum BLOB)")
    c.execute("INSERT INTO _sqlx_migrations VALUES(5,?)", (b"native-state-checksum",))
    c.execute("""CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT NOT NULL,
      created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,source TEXT NOT NULL,
      model_provider TEXT NOT NULL,cwd TEXT NOT NULL,title TEXT NOT NULL,
      sandbox_policy TEXT NOT NULL,approval_mode TEXT NOT NULL,name TEXT,
      archived INTEGER NOT NULL DEFAULT 0,archived_at INTEGER,history_mode TEXT NOT NULL DEFAULT 'paginated',
      project_id TEXT,thread_section_id TEXT,is_pinned INTEGER DEFAULT 0)""")
    relative = Path("sessions") / "2020" / "01" / "01" / ("rollout-2020-01-01-" + ID + ".jsonl")
    transcript = root / relative
    if with_chat:
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes((json.dumps({"type": "response_item", "payload": {"role": "user", "text": item_text}}) + "\n").encode())
        quiet = time.time() - 7200
        os.utime(transcript, (quiet, quiet))
        c.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ID, str(transcript), 100, 200, "vscode", "openai", r"C:\repo", "Original", "local-policy", "local-approval", "Original name", 0, None, "paginated", "local-project", "local-section", 1))
    c.commit(); c.close()
    c = sqlite3.connect(root / "thread_history_1.sqlite")
    c.execute("CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY,checksum BLOB)")
    c.execute("INSERT INTO _sqlx_migrations VALUES(?,?)", (7 if extra_column else 6, b"linux-native" if extra_column else b"windows-native"))
    c.execute("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,error_json TEXT,started_at INTEGER,completed_at INTEGER,duration_ms INTEGER,first_user_item_id TEXT,final_agent_item_id TEXT,rollout_byte_offset INTEGER,rollout_end_ordinal INTEGER,rollout_end_byte_offset INTEGER," + ("root_turn_id TEXT," if extra_column else "") + "PRIMARY KEY(thread_id,turn_id))")
    c.execute("CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,created_at_ms INTEGER,item_json TEXT,item_type TEXT DEFAULT '',updated_at_ordinal INTEGER DEFAULT 0,started_at_ms INTEGER,completed_at_ms INTEGER,PRIMARY KEY(thread_id,turn_id,item_id))")
    c.execute("CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,rollout_ordinal INTEGER,created_at_ms INTEGER,item_type TEXT,item_json TEXT,PRIMARY KEY(thread_id,item_id))")
    c.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)")
    if with_chat:
        values = [ID, "turn-one", 1, "completed", None, 100, 200, 100, "user-one", None, 0, 2, transcript.stat().st_size]
        if extra_column: values.append(None)
        c.execute("INSERT INTO thread_turns VALUES(" + ",".join("?" for _ in values) + ")", values)
        c.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?,?,?)", (ID, "turn-one", "user-one", 2, 100000, json.dumps({"type": "userMessage", "id": "user-one", "content": [{"type": "text", "text": item_text}]}), "userMessage", 2, None, None))
        c.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (ID, transcript.stat().st_size, 3))
    c.commit(); c.close()
    return root


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='history-endpoint-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = fixture(self.root / "source", extra_column=False)
        self.target = fixture(self.root / "target", with_chat=False, extra_column=True)
        self.package = self.root / "package"

    def args(self, **values):
        defaults = dict(home=str(self.source), scope="cli,vscode", quiet_seconds=60, id=ID,
                        package=str(self.package), expected=None, metadata_only=False, allow_existing=False)
        defaults.update(values)
        return argparse.Namespace(**defaults)

    def export(self, metadata_only=False):
        rev = ep.read_revision(self.source, ID)
        return ep.export_chat(self.args(expected=rev["revision"], metadata_only=metadata_only))

    def imported(self):
        self.export()
        return ep.import_chat(self.args(home=str(self.target), expected="absent"))

    def test_new_chat_cross_schema_and_native_settings(self):
        source_revision = ep.read_revision(self.source, ID)
        result = self.imported()
        self.assertEqual(result["record"]["content_revision"], source_revision["content_revision"])
        self.assertEqual(result["record"]["metadata_revision"], source_revision["metadata_revision"])
        c = sqlite3.connect(self.target / "thread_history_1.sqlite")
        self.assertEqual(c.execute("SELECT root_turn_id FROM thread_turns").fetchone()[0], None)
        self.assertEqual(c.execute("SELECT checksum FROM _sqlx_migrations").fetchone()[0], b"linux-native")
        c.close()
        c = sqlite3.connect(self.target / "state_5.sqlite")
        row = c.execute("SELECT sandbox_policy,approval_mode,project_id FROM threads").fetchone()
        self.assertEqual(row, ('{"type":"read-only"}', 'on-request', None))
        c.close()
        self.assertEqual((self.target / "auth.json").read_text(), "protected-auth\n")
        self.assertEqual((self.target / "config.toml").read_text(), "protected-config\n")

    def test_same_size_cache_edit_changes_revision(self):
        before = ep.read_revision(self.source, ID)
        c = sqlite3.connect(self.source / "thread_history_1.sqlite")
        c.execute("UPDATE thread_items SET item_json=REPLACE(item_json,'source text','altered tex')")
        c.commit(); c.close()
        after = ep.read_revision(self.source, ID)
        self.assertEqual(before["cache"]["thread_items"]["json_bytes"], after["cache"]["thread_items"]["json_bytes"])
        self.assertNotEqual(before["revision"], after["revision"])

    def test_existing_requires_flag_and_expected_revision(self):
        result = self.imported()
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_chat(self.args(home=str(self.target), expected=result["revision"]))
        self.assertEqual(error.exception.code, "existing_not_allowed")
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_chat(self.args(home=str(self.target), expected="wrong", allow_existing=True))
        self.assertEqual(error.exception.code, "target_changed")

    def test_metadata_only_preserves_recovered_cache_and_local_fields(self):
        self.imported()
        c = sqlite3.connect(self.target / "state_5.sqlite")
        c.execute("UPDATE threads SET project_id='target-project',thread_section_id='target-section'")
        c.commit(); c.close()
        c = sqlite3.connect(self.target / "thread_history_1.sqlite")
        c.execute("INSERT INTO thread_items SELECT thread_id,turn_id,'recovered-extra',4,created_at_ms,item_json,item_type,4,started_at_ms,completed_at_ms FROM thread_items")
        c.commit(); c.close()
        before = ep.read_revision(self.target, ID)
        self.package = self.root / "meta-package"
        c = sqlite3.connect(self.source / "state_5.sqlite")
        c.execute("UPDATE threads SET title='Renamed',name='New name',archived=1,archived_at=300")
        c.commit(); c.close()
        self.export(metadata_only=True)
        result = ep.import_chat(self.args(home=str(self.target), expected=before["revision"], allow_existing=True, metadata_only=True))
        self.assertEqual(result["record"]["content_revision"], before["content_revision"])
        self.assertTrue(result["record"]["archived"])
        c = sqlite3.connect(self.target / "state_5.sqlite")
        row = c.execute("SELECT project_id,thread_section_id,rollout_path FROM threads").fetchone(); c.close()
        self.assertEqual(row[:2], ("target-project", "target-section"))
        self.assertIn("archived_sessions", row[2])
        self.assertTrue(Path(row[2]).is_file())

    def test_transaction_failure_restores_file_and_cache(self):
        self.imported()
        before = ep.read_revision(self.target, ID)
        old_path = next((self.target / "sessions").rglob("*.jsonl"))
        old_bytes = old_path.read_bytes()
        self.package = self.root / "replacement"
        source_path = next((self.source / "sessions").rglob("*.jsonl"))
        source_path.write_bytes(b'{"type":"replacement","text":"different source body"}\n')
        os.utime(source_path, (time.time() - 7200, time.time() - 7200))
        c = sqlite3.connect(self.source / "state_5.sqlite")
        c.execute("UPDATE threads SET title='new title'"); c.commit(); c.close()
        c = sqlite3.connect(self.source / "thread_history_1.sqlite")
        c.execute("UPDATE thread_items SET item_json=REPLACE(item_json,'source text','different body')")
        c.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?", (source_path.stat().st_size,))
        c.execute("UPDATE thread_turns SET rollout_end_byte_offset=?", (source_path.stat().st_size,))
        c.commit(); c.close()
        self.export()
        c = sqlite3.connect(self.target / "state_5.sqlite")
        c.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON threads BEGIN SELECT RAISE(ABORT,'test failure'); END")
        c.commit(); c.close()
        with self.assertRaises(sqlite3.IntegrityError):
            ep.import_chat(self.args(home=str(self.target), expected=before["revision"], allow_existing=True))
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(ep.read_revision(self.target, ID)["revision"], before["revision"])

    def test_projection_past_eof_rejected(self):
        self.export()
        c = sqlite3.connect(self.package / "history.sqlite")
        c.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=999999")
        c.commit(); c.close()
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_chat(self.args(home=str(self.target), expected="absent"))
        self.assertEqual(error.exception.code, "projection_past_eof")
        self.assertIsNone(ep.read_revision(self.target, ID))

    def test_receipt_failure_does_not_restore_file_after_commit(self):
        self.export()
        original = ep.write_json
        def fail_receipt(path, value):
            if path.name == "journal.json" and value.get("status") == "committed":
                raise OSError("simulated receipt failure")
            return original(path, value)
        with mock.patch.object(ep, "write_json", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                ep.import_chat(self.args(home=str(self.target), expected="absent"))
        self.assertIsNotNone(ep.read_revision(self.target, ID))
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_chat(self.args(home=str(self.target), expected=ep.read_revision(self.target, ID)["revision"], allow_existing=True))
        self.assertEqual(error.exception.code, "pending_recovery")

    def test_source_revision_changes_before_export(self):
        with self.assertRaises(ep.EndpointError) as error:
            ep.export_chat(self.args(expected="stale-revision"))
        self.assertEqual(error.exception.code, "source_changed")

    def test_recent_in_progress_target_rejected(self):
        self.imported()
        c = sqlite3.connect(self.target / "thread_history_1.sqlite")
        c.execute("UPDATE thread_turns SET status='inProgress',started_at=?", (int(time.time() * 1000),))
        c.commit(); c.close()
        revision = ep.read_revision(self.target, ID)
        self.assertTrue(revision["active"])
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_chat(self.args(home=str(self.target), expected=revision["revision"], allow_existing=True))
        self.assertEqual(error.exception.code, "target_active")

    def test_populated_unknown_field_fails_closed_before_commit(self):
        self.export()
        c = sqlite3.connect(self.target / "thread_history_1.sqlite")
        c.execute("ALTER TABLE thread_items ADD COLUMN future_semantics INTEGER DEFAULT 7")
        c.commit(); c.close()
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_chat(self.args(home=str(self.target), expected="absent"))
        self.assertEqual(error.exception.code, "schema_unsupported")
        self.assertIsNone(ep.read_revision(self.target, ID))

    def test_nullable_unknown_target_addition_is_compatible(self):
        c = sqlite3.connect(self.target / "thread_history_1.sqlite")
        c.execute("ALTER TABLE thread_items ADD COLUMN future_nullable TEXT")
        c.commit(); c.close()
        self.assertTrue(self.imported()["ok"])

    def test_unprojected_messages_are_not_misrepresented_as_empty(self):
        c = sqlite3.connect(self.source / "thread_history_1.sqlite")
        c.execute("DELETE FROM thread_items"); c.commit(); c.close()
        record = ep.read_revision(self.source, ID)
        self.assertEqual(record["item_count"], 0)
        self.assertTrue(record["transcript_message_hint"])
        with self.assertRaises(ep.EndpointError) as error:
            self.export()
        self.assertEqual(error.exception.code, "unindexed_history")
        self.package = self.root / "unprojected-metadata"
        self.assertTrue(self.export(metadata_only=True)["ok"])

    def test_cwd_mapping(self):
        self.assertEqual(ep.translate_cwd(r"D:\project\dir", "linux"), "/mnt/d/project/dir")
        self.assertEqual(ep.translate_cwd("/mnt/d/project/dir", "windows"), "D:\\project\\dir")


if __name__ == "__main__":
    unittest.main()
