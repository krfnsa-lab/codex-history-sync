import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import merge_validate as validation


class MergeValidationTests(unittest.TestCase):
    def test_clone_only_schema_and_migrations(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source.db', Path(directory) / 'target.db'
            with closing(sqlite3.connect(source)) as db, db:
                db.executescript('CREATE TABLE chats(id TEXT PRIMARY KEY, secret TEXT); CREATE INDEX chats_secret ON chats(secret); CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY, checksum BLOB);')
                db.execute('INSERT INTO chats VALUES (?,?)', ('original', 'must not be copied'))
                db.execute('INSERT INTO _sqlx_migrations VALUES (?,?)', (1, b'verified'))
            before = source.read_bytes()
            validation.clone_schema(source, target)
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM chats').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT * FROM _sqlx_migrations').fetchall(), [(1, b'verified')])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='chats_secret'").fetchone()[0], 1)
            self.assertEqual(source.read_bytes(), before)

    def test_clone_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.db'
            source.touch()
            with self.assertRaisesRegex(validation.ValidationError, 'already exists'):
                validation.clone_schema(source, source)

    def test_clone_refuses_virtual_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source.db', Path(directory) / 'target.db'
            with closing(sqlite3.connect(source)) as db, db:
                try:
                    db.execute('CREATE VIRTUAL TABLE searchable USING fts5(text)')
                except sqlite3.OperationalError:
                    self.skipTest('SQLite FTS5 unavailable')
            with self.assertRaisesRegex(validation.ValidationError, 'virtual schema'):
                validation.clone_schema(source, target)

    def test_branch_text_uses_dynamic_boundary_and_ignores_analysis(self):
        records = []
        for number in range(3):
            records.append({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': str(number)}})
            for role, channel in (('user', None), ('assistant', 'final'), ('assistant', 'analysis')):
                records.append({'type': 'response_item', 'payload': {'type': 'message', 'role': role, 'channel': channel,
                                'content': [{'type': 'input_text', 'text': str(number) + '-' + role + '-' + str(channel)}]}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'transcript.jsonl'
            path.write_text('\n'.join(json.dumps(record) for record in records) + '\n', encoding='utf-8')
            self.assertEqual(validation.branch_text(path, 2), ['2-user-None', '2-assistant-final'])
            self.assertEqual(len(validation.branch_text(path, 1)), 4)

    def test_invalid_common_boundary_rejected(self):
        for value in (0, -1, True, '1', None):
            with self.subTest(value=value), self.assertRaises(validation.ValidationError):
                validation.branch_text(Path('unused'), value)

    def test_texts_preserve_repeated_parts(self):
        self.assertEqual(list(validation.texts({'content': [{'text': 'same'}, {'text': 'same'}, {'text': 8}]})), ['same', 'same'])

    def test_common_prefix_duplicate_does_not_replace_branch_content(self):
        result = validation.branch_context_check(['duplicate', 'marker', 'new'], ['duplicate', 'new'], 'marker', set())
        self.assertEqual(result['missing_text_parts'], 1)
        self.assertFalse(result['order_preserved'])

    def test_branch_multiplicity_and_order_verified_between_markers(self):
        result = validation.branch_context_check(['markerA', 'same', 'same', 'last', 'markerB', 'other'],
                                                  ['same', 'same', 'last'], 'markerA', {'markerB'})
        self.assertEqual(result['missing_text_parts'], 0)
        self.assertTrue(result['order_preserved'])
        reordered = validation.branch_context_check(['markerA', 'last', 'same', 'same', 'markerB'],
                                                     ['same', 'same', 'last'], 'markerA', {'markerB'})
        self.assertEqual(reordered['missing_text_parts'], 0)
        self.assertFalse(reordered['order_preserved'])

    def test_missing_or_duplicate_context_marker_is_rejected(self):
        for request in (['text'], ['marker', 'text', 'marker']):
            with self.assertRaises(validation.ValidationError):
                validation.branch_context_check(request, ['text'], 'marker', set())

    def test_empty_branch_needs_no_marker(self):
        self.assertTrue(validation.branch_context_check([], [], None, set())['order_preserved'])

    def test_pagination_follows_cursor_and_limits_count(self):
        requests = []
        def rpc(method, params):
            requests.append((method, params))
            return {'data': [1], 'nextCursor': 'next'} if params['cursor'] is None else {'data': [2], 'nextCursor': None}
        self.assertEqual(validation.pages(rpc, 'items', {'threadId': 'id'}, 2), [1, 2])
        self.assertEqual(requests[1][1]['cursor'], 'next')
        self.assertEqual(requests[0][1]['limit'], 100)

    def test_pagination_repeated_cursor_rejected(self):
        with self.assertRaisesRegex(validation.ValidationError, 'advance'):
            validation.pages(lambda *_: {'data': [], 'nextCursor': 'same'}, 'items', {})

    def test_pagination_rejects_malformed_page(self):
        for page in (None, [], {'data': {}}, {'data': [], 'nextCursor': 1}, {'data': [], 'nextCursor': ''}):
            with self.subTest(page=page), self.assertRaises(validation.ValidationError):
                validation.pages(lambda *_, value=page: value, 'items', {})

    def test_pagination_hard_limit(self):
        count = 0
        def rpc(*_):
            nonlocal count
            count += 1
            return {'data': [], 'nextCursor': str(count)}
        with self.assertRaisesRegex(validation.ValidationError, 'exceeded'):
            validation.pages(rpc, 'items', {}, expected_count=1)
        self.assertEqual(count, 2)

    def test_env_does_not_inherit_credentials_or_proxy(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'CODEX_HOME': 'real', 'HOME': 'real',
                                     'HTTP_PROXY': 'remote', 'GITHUB_TOKEN': 'secret', 'PATH': 'needed'}, clear=True):
            root = Path('/tmp/owned')
            environment = validation.isolated_env(root)
        self.assertEqual(environment['HOME'], str(root))
        self.assertEqual(environment['CODEX_HOME'], str(root))
        self.assertEqual(environment['PATH'], 'needed')
        self.assertFalse({'OPENAI_API_KEY', 'HTTP_PROXY', 'GITHUB_TOKEN'} & environment.keys())
        self.assertTrue(all(value.startswith(str(root)) for key, value in environment.items() if key != 'PATH'))

    def test_hashes_cover_all_package_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in validation.FILES:
                (path / name).write_bytes(name.encode())
            original = validation.package_hashes(path)
            (path / 'history.sqlite').write_bytes(b'changed')
            changed = validation.package_hashes(path)
            self.assertNotEqual(original['history.sqlite'], changed['history.sqlite'])
            self.assertEqual(original['meta.json'], changed['meta.json'])

    def test_non_native_platform_refused_before_files_are_opened(self):
        with patch.object(validation.sys, 'platform', 'win32'), self.assertRaisesRegex(validation.ValidationError, 'Linux/WSL'):
            validation.validate('unused', 'unused', 'unused', 'unused')


if __name__ == '__main__':
    unittest.main()
