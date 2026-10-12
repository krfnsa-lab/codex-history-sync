"""All fixtures live in temporary homes; no real history is modified."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('native_local_probe_test', ROOT / 'native_local_probe.py')
fixtures = load('native_local_fixture', ROOT.parent / 'tests/test_endpoint.py')
ep = fixtures.ep
ID = fixtures.ID


class NativeLocalProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-local-proof-')
        self.addCleanup(self.temp.cleanup)
        self.home = fixtures.fixture(Path(self.temp.name) / 'home')
        self.baseline = ep.read_revision(self.home, ID)
        state = ep.readonly(self.home / 'state_5.sqlite')
        try:
            self.path = ep.safe_rollout(self.home, ep.thread_row(state, ID))
        finally:
            state.close()
        # This deliberately tiny fixture has one physical record. Bring its
        # projection cursor into a valid physical-line convention first.
        c = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        c.execute('UPDATE thread_history_projection_state SET next_rollout_ordinal=1 WHERE thread_id=?', (ID,))
        c.commit(); c.close()
        self.baseline = ep.read_revision(self.home, ID)

    def add(self, raw=None, projection=True):
        if raw is None:
            raw = json.dumps({'type': 'event_msg', 'payload': {'type': 'thread_settings_applied'}}).encode() + b'\n'
        with self.path.open('ab') as stream:
            stream.write(raw)
        past = time.time() - 7200
        os.utime(self.path, (past, past))
        if projection:
            c = sqlite3.connect(self.home / 'thread_history_1.sqlite')
            c.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=next_rollout_ordinal+1 WHERE thread_id=?', (self.path.stat().st_size, ID))
            c.commit(); c.close()

    def verify(self):
        return probe.verify_native(ep, self.home, ID, self.baseline)

    def test_local_settings_append_proven_and_no_files_written(self):
        self.add(); self.add()
        files = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = self.verify()
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['settings_count'], 2)
        self.assertEqual(result['record']['item_count'], 1)
        self.assertEqual(files, {p: p.read_bytes() for p in files})

    def test_no_suffix_is_not_a_reason_to_rebase(self):
        self.assertEqual(self.verify()['reason'], 'no_local_settings_append')

    def test_reject_user_tool_unknown_or_environment_events(self):
        prefix = self.path.read_bytes()
        for record in (
                {'type': 'response_item', 'payload': {'role': 'user', 'text': 'private'}},
                {'type': 'response_item', 'payload': {'type': 'function_call'}},
                {'type': 'event_msg', 'payload': {'type': 'unknown_event'}},
                {'type': 'turn_context', 'payload': {}},
                {'type': 'session_meta', 'payload': {}},
                {'type': 'event_msg', 'payload': {'type': 'task_complete'}},
                {'type': 'event_msg', 'payload': {'type': 'user_message'}},
                None):
            with self.subTest(record=record):
                raw = json.dumps(record).encode() + b'\n'
                with self.assertRaises(probe.ProofRejected):
                    probe.verify_transcript(io.BytesIO(prefix + raw), self.baseline, ep.digest)

    def test_reject_invalid_incomplete_or_empty_lines(self):
        prefix = self.path.read_bytes()
        for raw in (b'\n', b'bad\n', b'{}\n', b'{"type":"event_msg","payload":{"type":"thread_settings_applied"}}', b'\xff\n'):
            with self.subTest(raw=raw), self.assertRaises(probe.ProofRejected):
                probe.verify_transcript(io.BytesIO(prefix + raw), self.baseline, ep.digest)

    def test_reject_duplicate_keys_and_nonstandard_json_numbers(self):
        prefix = self.path.read_bytes()
        for raw in (b'{"type":"response_item","type":"event_msg","payload":{"type":"thread_settings_applied"}}\n',
                    b'{"type":"event_msg","payload":{"type":"thread_settings_applied","value":NaN}}\n'):
            with self.subTest(raw=raw), self.assertRaises(probe.ProofRejected):
                probe.verify_transcript(io.BytesIO(prefix + raw), self.baseline, ep.digest)

    def test_reject_mutated_prefix(self):
        self.add()
        raw = bytearray(self.path.read_bytes()); raw[20] ^= 1
        self.path.write_bytes(raw)
        self.assertFalse(self.verify()['verified'])

    def test_reject_changed_metadata(self):
        self.add()
        c = sqlite3.connect(self.home / 'state_5.sqlite')
        c.execute('UPDATE threads SET title=? WHERE id=?', ('different', ID))
        c.commit(); c.close()
        self.assertEqual(self.verify()['reason'], 'target_metadata_changed')

    def test_reject_body_or_cache_status_changes(self):
        self.add()
        for query in ('UPDATE thread_items SET item_json=\'{}\'', 'UPDATE thread_turns SET status=\'interrupted\''):
            with self.subTest(query=query):
                db = self.home / 'thread_history_1.sqlite'
                backup = db.read_bytes()
                c = sqlite3.connect(db); c.execute(query); c.commit(); c.close()
                self.assertEqual(self.verify()['reason'], 'target_cache_changed')
                db.write_bytes(backup)

    def test_reject_projection_that_did_not_follow_settings(self):
        self.add(projection=False)
        self.assertEqual(self.verify()['reason'], 'projection_not_only_settings')

    def test_native_cursor_may_normalize_next_to_last_processed_ordinal(self):
        self.add(); self.add()
        c = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        c.execute('UPDATE thread_history_projection_state SET next_rollout_ordinal=2 WHERE thread_id=?', (ID,))
        c.commit(); c.close()
        self.assertTrue(self.verify()['verified'])

    def test_reject_arbitrary_changed_projection_cursor(self):
        self.add()
        c = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        c.execute('UPDATE thread_history_projection_state SET next_rollout_ordinal=99 WHERE thread_id=?', (ID,))
        c.commit(); c.close()
        self.assertEqual(self.verify()['reason'], 'projection_not_only_settings')

    def test_reject_projection_unknown_schema(self):
        self.add()
        c = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        c.execute('ALTER TABLE thread_history_projection_state ADD COLUMN unknown TEXT')
        c.commit(); c.close()
        self.assertEqual(self.verify()['reason'], 'projection_schema_changed')

    def test_reject_forged_baseline_projection_hash(self):
        self.add()
        self.baseline['cache'][probe.PROJECTION]['rows_sha256'] = 'invalid'
        self.assertEqual(self.verify()['reason'], 'baseline_projection_unproven')

    def test_reject_bad_baseline_or_thread_id(self):
        self.add()
        self.assertFalse(probe.verify_native(ep, self.home, ID, None)['verified'])
        self.baseline['id'] = 'other'
        self.assertEqual(self.verify()['reason'], 'baseline_thread_mismatch')

    def test_reject_target_busy(self):
        self.add(); os.utime(self.path, None)
        self.assertEqual(self.verify()['reason'], 'target_busy')

    def test_reject_revision_changed_between_read_snapshots(self):
        self.add()
        original = ep.read_revision
        count = 0
        def racing(*args, **kwargs):
            nonlocal count
            count += 1
            value = original(*args, **kwargs)
            if count == 2:
                value['revision'] = 'raced'
            return value
        with mock.patch.object(ep, 'read_revision', side_effect=racing):
            self.assertEqual(self.verify()['reason'], 'target_changed_during_probe')

    def test_cli_prints_only_json_no_fixture_message_text(self):
        self.add()
        encoded = base64.b64encode(json.dumps(self.baseline).encode()).decode()
        result = subprocess.run([sys.executable, str(ROOT / 'native_local_probe.py'), '--endpoint', str(fixtures.ENDPOINT),
                                 '--home', str(self.home), '--id', ID, '--baseline-base64', encoded],
                                check=True, text=True, capture_output=True)
        self.assertTrue(json.loads(result.stdout)['verified'])
        self.assertNotIn('source text', result.stdout)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
