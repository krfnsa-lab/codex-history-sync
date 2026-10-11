"""Bounded-memory history fingerprint regressions using synthetic data only."""
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    'review_history_endpoint', ROOT / 'history_endpoint.py'
)
endpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(endpoint)


class FingerprintReview(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='endpoint-review-')
        self.home = Path(self.directory.name).resolve()
        self.transcript = self.home / 'sessions' / 'chat-1.jsonl'
        self.transcript.parent.mkdir()
        self.transcript.write_bytes(b'original\n')
        self.row = {'id': 'chat-1', 'source': 'vscode', 'rollout_path': str(self.transcript),
                    'archived': 0, 'title': 'Synthetic', 'name': None}
        self.cache = sqlite3.connect(':memory:')
        self.cache.row_factory = sqlite3.Row

    def tearDown(self):
        self.cache.close()
        self.directory.cleanup()

    def revision(self):
        return endpoint.revision_from_connections(self.home, self.row, self.cache, quiet_seconds=0)

    def create_items(self):
        self.cache.execute('CREATE TABLE thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT,'
                           'rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT,'
                           'item_type TEXT, updated_at_ordinal INTEGER,'
                           'PRIMARY KEY(thread_id,turn_id,item_id))')

    def test_same_length_middle_transcript_edit_changes_content_revision(self):
        # The edit is outside both 64 KiB edge samples and keeps file length.
        content = b'a' * (256 * 1024) + b'\n'
        self.transcript.write_bytes(content)
        before = self.revision()
        self.transcript.write_bytes(content[:128 * 1024] + b'b' + content[128 * 1024 + 1:])
        after = self.revision()
        self.assertNotEqual(before['content_revision'], after['content_revision'])

    def test_same_length_cached_message_edit_changes_content_revision(self):
        self.create_items()
        self.cache.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)',
                           ('chat-1', 'turn-1', 'item-1', 10, 1000, '{"text":"alpha"}', 'agentMessage', 10))
        before = self.revision()
        self.cache.execute('UPDATE thread_items SET item_json=?', ('{"text":"bravo"}',))
        after = self.revision()
        self.assertNotEqual(before['content_revision'], after['content_revision'])

    def test_nonprogress_turn_status_edit_changes_content_revision(self):
        self.cache.execute('CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT,'
                           'rollout_ordinal INTEGER, status TEXT, started_at INTEGER,'
                           'PRIMARY KEY(thread_id,turn_id))')
        self.cache.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?)',
                           ('chat-1', 'turn-1', 10, 'completed', 1000))
        before = self.revision()
        self.cache.execute("UPDATE thread_turns SET status='interrupted'")
        after = self.revision()
        self.assertNotEqual(before['content_revision'], after['content_revision'])

    def test_cache_physical_insertion_order_does_not_change_revision(self):
        self.create_items()
        rows = [('chat-1', 'turn-1', 'item-' + str(index), index, 1000,
                 '{"text":"value-' + str(index) + '"}', 'agentMessage', index)
                for index in range(3)]
        self.cache.executemany('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)', rows)
        before = self.revision()
        self.cache.execute('DELETE FROM thread_items')
        self.cache.executemany('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)', reversed(rows))
        after = self.revision()
        self.assertEqual(before['content_revision'], after['content_revision'])

    def test_populated_unknown_future_column_is_blocked(self):
        self.create_items()
        self.cache.execute('ALTER TABLE thread_items ADD COLUMN future_semantics TEXT')
        self.cache.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?,?)',
                           ('chat-1', 'turn-1', 'item-1', 1, 1000, '{"text":"value"}',
                            'agentMessage', 1, 'unsupported'))
        with self.assertRaises(endpoint.EndpointError) as captured:
            self.revision()
        self.assertEqual(captured.exception.code, 'schema_unsupported')

    def test_null_future_column_normalizes_without_changing_content(self):
        self.create_items()
        self.cache.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)',
                           ('chat-1', 'turn-1', 'item-1', 1, 1000,
                            '{"text":"value"}', 'agentMessage', 1))
        before = self.revision()
        self.cache.execute('ALTER TABLE thread_items ADD COLUMN future_nullable TEXT')
        after = self.revision()
        self.assertEqual(before['content_revision'], after['content_revision'])

    def test_native_derived_root_turn_id_normalizes_across_schemas(self):
        self.create_items()
        self.cache.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)',
                           ('chat-1', 'turn-1', 'item-1', 1, 1000,
                            '{"text":"value"}', 'agentMessage', 1))
        before = self.revision()
        self.cache.execute('ALTER TABLE thread_items ADD COLUMN root_turn_id TEXT')
        self.cache.execute('UPDATE thread_items SET root_turn_id=?', ('native-derived-root',))
        after = self.revision()
        self.assertEqual(before['content_revision'], after['content_revision'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
