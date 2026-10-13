"""Native replacement safety for an explicitly selected longer conversation.

All homes and databases are disposable fixtures; no real history is accessed.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from tests.test_endpoint import ID, ep, fixture


class SelectedConversationBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="prefer-longer-backup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = fixture(self.root / "winner", item_text="winner message")
        self.target = fixture(self.root / "loser", item_text="losing unique message")
        self.package = self.root / "package"
        self.source_path = next((self.source / "sessions").rglob("*.jsonl"))
        self.target_path = next((self.target / "sessions").rglob("*.jsonl"))
        # The source wins on turns, although the target has more tool items.
        with self.source_path.open("ab") as stream:
            stream.write(b'{"type":"response_item","payload":{"role":"user","text":"winner second turn"}}\n')
        quiet = time.time() - 7200
        os.utime(self.source_path, (quiet, quiet))
        with closing(sqlite3.connect(self.source / "thread_history_1.sqlite")) as connection, connection:
            connection.execute("INSERT INTO thread_turns SELECT thread_id,'turn-two',3,status,error_json,started_at,completed_at,duration_ms,'user-two',NULL,0,4,? FROM thread_turns", (self.source_path.stat().st_size,))
            connection.execute("INSERT INTO thread_items SELECT thread_id,'turn-two','user-two',4,created_at_ms,?,item_type,4,started_at_ms,completed_at_ms FROM thread_items", (json.dumps({"type": "userMessage", "id": "user-two", "content": [{"type": "text", "text": "winner second turn"}]}),))
            connection.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=5", (self.source_path.stat().st_size,))
        with closing(sqlite3.connect(self.target / "thread_history_1.sqlite")) as connection, connection:
            for index in range(5):
                connection.execute("INSERT INTO thread_items SELECT thread_id,turn_id,?,rollout_ordinal,created_at_ms,?,'commandExecution',updated_at_ordinal,started_at_ms,completed_at_ms FROM thread_items WHERE item_id='user-one'", ("losing-tool-" + str(index), json.dumps({"type": "commandExecution", "id": "losing-tool-" + str(index), "command": "saved unique tool output"})))
            connection.execute("INSERT INTO thread_realtime_items VALUES(?,?,?,?,?,?)", (ID, "losing-realtime", 2, 100000, "agentMessage", '{"type":"agentMessage","text":"losing realtime detail"}'))
        with closing(sqlite3.connect(self.target / "state_5.sqlite")) as connection, connection:
            connection.execute("UPDATE threads SET title='Losing title',name='Losing name',project_id='preserved local project'")
        self.old_row = self.thread_row(self.target)
        self.old_bytes = self.target_path.read_bytes()
        self.old_cache = self.cache_rows(self.target / "thread_history_1.sqlite")
        self.old_revision = ep.read_revision(self.target, ID)

    @staticmethod
    def thread_row(home):
        with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
            connection.row_factory = sqlite3.Row
            return dict(connection.execute("SELECT * FROM threads WHERE id=?", (ID,)).fetchone())

    @staticmethod
    def cache_rows(path):
        with closing(sqlite3.connect(path)) as connection:
            return {table: sorted(connection.execute('SELECT * FROM "' + table + '" WHERE thread_id=?', (ID,)).fetchall(), key=repr)
                    for table in ep.CACHE_TABLES}

    def args(self, **overrides):
        values = dict(home=str(self.source), scope="cli,vscode", quiet_seconds=60,
                      id=ID, package=str(self.package), expected=None,
                      metadata_only=False, allow_existing=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def export(self):
        revision = ep.read_revision(self.source, ID)
        ep.export_chat(self.args(expected=revision["revision"]))

    def replace(self):
        return ep.import_chat(self.args(home=str(self.target),
                                       expected=self.old_revision["revision"],
                                       allow_existing=True))

    def assert_saved_branch(self, backup):
        self.assertEqual((backup / "transcript.jsonl").read_bytes(), self.old_bytes)
        metadata = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["thread"], self.old_row)
        self.assertEqual(self.cache_rows(backup / "history.sqlite"), self.old_cache)

    def test_more_turns_fewer_items_replaces_exactly_and_keeps_losing_branch(self):
        winner = ep.read_revision(self.source, ID)
        self.assertGreater(winner["turn_count"], self.old_revision["turn_count"])
        self.assertLess(winner["item_count"], self.old_revision["item_count"])
        self.export()
        result = self.replace()
        backup = Path(result["backup"])
        self.assert_saved_branch(backup)
        self.assertEqual(result["record"]["turn_count"], winner["turn_count"])
        self.assertEqual(result["record"]["item_count"], winner["item_count"])
        self.assertEqual(result["record"]["content_revision"], winner["content_revision"])
        self.assertEqual(self.thread_row(self.target)["project_id"], "preserved local project")
        journal = json.loads((backup / "journal.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "committed")
        self.assertEqual(journal["expected"], self.old_revision["revision"])
        self.assertEqual(journal["result_revision"], result["revision"])

    def test_target_changed_after_selection_is_rejected_without_replacement(self):
        self.export()
        with closing(sqlite3.connect(self.target / "state_5.sqlite")) as connection, connection:
            connection.execute("UPDATE threads SET title='New concurrent title'")
        with self.assertRaises(ep.EndpointError) as error:
            self.replace()
        self.assertEqual(error.exception.code, "target_changed")
        self.assertEqual(self.target_path.read_bytes(), self.old_bytes)
        self.assertEqual(self.cache_rows(self.target / "thread_history_1.sqlite"), self.old_cache)
        self.assertFalse((self.target / ".history-sync-backups").exists())

    def test_target_transcript_changed_during_backup_is_rechecked(self):
        self.export()
        native_backup = ep.backup_chat
        changed_bytes = self.old_bytes + b'{"type":"response_item","payload":{"role":"user","text":"late target message"}}\n'

        def changing_backup(*args):
            saved = native_backup(*args)
            self.target_path.write_bytes(changed_bytes)
            return saved

        with mock.patch.object(ep, "backup_chat", side_effect=changing_backup):
            with self.assertRaises(ep.EndpointError) as error:
                self.replace()
        self.assertEqual(error.exception.code, "target_changed")
        self.assertEqual(self.target_path.read_bytes(), changed_bytes)
        self.assertEqual(self.cache_rows(self.target / "thread_history_1.sqlite"), self.old_cache)
        journal_path = next((self.target / ".history-sync-backups").rglob("journal.json"))
        self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["status"], "rolled_back")
        self.assert_saved_branch(journal_path.parent)

    def test_committed_receipt_failure_keeps_backup_and_blocks_retry(self):
        self.export()
        write_json = ep.write_json

        def fail_receipt(path, value):
            if path.name == "journal.json" and value.get("status") == "committed":
                raise OSError("simulated committed receipt failure")
            return write_json(path, value)

        with mock.patch.object(ep, "write_json", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.replace()
        journal_path = next((self.target / ".history-sync-backups").rglob("journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "pending")
        self.assert_saved_branch(journal_path.parent)
        current = ep.read_revision(self.target, ID)
        self.assertEqual(current["revision"], journal["result_revision"])
        self.assertEqual(current["turn_count"], 2)
        self.assertEqual(current["item_count"], 2)
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_chat(self.args(home=str(self.target), expected=current["revision"], allow_existing=True))
        self.assertEqual(error.exception.code, "pending_recovery")


if __name__ == "__main__":
    unittest.main()
