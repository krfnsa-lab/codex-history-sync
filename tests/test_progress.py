import copy
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

import history_progress as telemetry
import history_sync as sync
from test_history_sync_review import row, view


class ProgressSnapshots(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / 'progress.json'

    def tearDown(self):
        self.directory.cleanup()

    def value(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_allowlist_drops_messages_titles_paths_and_invalid_counts(self):
        writer = telemetry.ProgressWriter(self.path)
        writer.update('exporting', title='private chat', error='secret',
                      processed_chats=True, total_chats=-1, side='private',
                      password='credential', reason='unknown secret')
        value = self.value()
        self.assertEqual(value['phase'], 'exporting')
        self.assertEqual(value['owner'], 'codex-history-sync')
        self.assertFalse({'title', 'error', 'processed_chats', 'total_chats',
                          'side', 'password', 'reason'} & value.keys())

    def test_rate_limit_keeps_latest_real_counts_for_next_phase(self):
        with patch.object(telemetry.time, 'monotonic', return_value=10):
            writer = telemetry.ProgressWriter(self.path)
            writer.update('scanning', processed_records=1)
            writer.update('scanning', processed_records=2)
            self.assertEqual(self.value()['processed_records'], 1)
            writer.update('verifying')
            self.assertEqual(self.value()['processed_records'], 2)
            writer.finish('completed', processed_records=3)
            self.assertEqual(self.value()['processed_records'], 3)
            writer.update('writing')
            self.assertEqual(self.value()['status'], 'completed')

    def test_operation_and_request_identity_correlation(self):
        request_id = str(uuid.uuid4())
        writer = telemetry.ProgressWriter(self.path, request_id=request_id)
        operation_id = writer.begin_operation('exporting', side='wsl')
        value = self.value()
        self.assertEqual(value['operation_id'], operation_id)
        self.assertEqual(value['request_id'], request_id)
        detail = telemetry.ProgressWriter(self.root / 'progress-detail.json',
            run_id=writer.run_id, operation_id=operation_id, side='wsl')
        detail.update('reading', processed_records=2)
        actual = json.loads(detail.path.read_text(encoding='utf-8'))
        self.assertEqual((actual['run_id'], actual['operation_id']),
                         (value['run_id'], value['operation_id']))

    def test_bad_identity_and_path_are_diagnostic_noops(self):
        writer = telemetry.ProgressWriter(self.path, run_id='not-an-id')
        writer.update('writing')
        writer.finish('error')
        self.assertFalse(self.path.exists())
        writer = telemetry.ProgressWriter(self.root / 'missing' / 'progress.json')
        writer.finish('error')
        self.assertFalse(writer.path.exists())

    def test_corrupt_existing_snapshot_is_replaced(self):
        self.path.write_text('{incomplete', encoding='utf-8')
        writer = telemetry.ProgressWriter(self.path)
        writer.finish('waiting', reason='wsl_stopped')
        self.assertEqual(self.value()['status'], 'waiting')

    def test_failed_replace_leaves_existing_snapshot_and_cleans_own_temp(self):
        self.path.write_text('{"old":true}', encoding='utf-8')
        with patch.object(telemetry.os, 'replace', side_effect=PermissionError('readonly')):
            writer = telemetry.ProgressWriter(self.path)
            writer.update('writing')
            writer.finish('error')
        self.assertEqual(self.value(), {'old': True})
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_terminal_replace_recovers_from_one_or_two_short_sharing_failures(self):
        original = telemetry.os.replace
        for failures in (1, 2):
            with self.subTest(failures=failures):
                writer = telemetry.ProgressWriter(self.path)
                writer.update('writing')
                attempts = []
                def briefly_shared(source, target):
                    attempts.append((source, target))
                    if len(attempts) <= failures:
                        raise PermissionError('short-lived sharing conflict')
                    return original(source, target)
                with patch.object(telemetry.os, 'replace', side_effect=briefly_shared), \
                     patch.object(telemetry.time, 'sleep') as sleep:
                    writer.finish('completed', transferred_chats=1)
                self.assertEqual(self.value()['status'], 'completed')
                self.assertEqual(len(attempts), failures + 1)
                self.assertEqual(sleep.call_count, failures)
                self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 0.03)
                self.assertEqual(len({source for source, _ in attempts}), 1)
                self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_terminal_retry_is_bounded_and_persistent_failure_cleans_stage(self):
        writer = telemetry.ProgressWriter(self.path)
        writer.update('writing')
        previous = self.path.read_bytes()
        with patch.object(telemetry.os, 'replace', side_effect=PermissionError('persistent')) as replace, \
             patch.object(telemetry.time, 'sleep') as sleep:
            writer.finish('completed', transferred_chats=1)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02])
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_unrelated_replace_error_is_not_retried(self):
        with patch.object(telemetry.os, 'replace', side_effect=OSError('not a sharing failure')) as replace, \
             patch.object(telemetry.time, 'sleep') as sleep:
            telemetry.ProgressWriter(self.path).finish('error')
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_concurrent_detail_snapshots_are_whole_and_identifiable(self):
        writers = [telemetry.ProgressWriter(self.path) for _ in range(12)]
        workers = [threading.Thread(target=w.finish, args=('completed',),
                   kwargs={'processed_records': index}) for index, w in enumerate(writers)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join()
        value = self.value()
        self.assertIn(value['run_id'], {w.run_id for w in writers})
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(list(self.root.glob('*.tmp')), [])


class CoordinatorProgress(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.args = SimpleNamespace(initialize=False, dry_run=False)
        self.config = {'wsl_distro': 'Ubuntu', 'quiet_seconds': 60}
        self.before = sync.baseline(view(row()), view(row()))
        (self.root / 'ledger.json').write_text(json.dumps(self.before), encoding='utf-8')
        self.endpoint = MagicMock()
        self.endpoint.inspect.side_effect = lambda side: copy.deepcopy(
            view(row(body='new-source')) if side == 'windows' else view(row()))
        self.endpoint.backend_status.return_value = {'backend_running': False}
        self.endpoint.call.side_effect = lambda side, operation, *args: (
            {'ok': True, 'record': row(body='imported-native')} if operation == 'import'
            else {'ok': True, 'compatible': True})

    def tearDown(self):
        self.directory.cleanup()

    def execute(self):
        with patch.object(sync, 'wsl_running', return_value=True), \
             patch.object(sync, 'Endpoints', return_value=self.endpoint), \
             patch.object(sync, 'linux_mount_path', side_effect=str):
            return sync.execute(self.config, self.args, self.root, MagicMock())

    def test_success_counts_only_committed_chats(self):
        result = self.execute()
        value = json.loads((self.root / 'progress.json').read_text(encoding='utf-8'))
        self.assertEqual((value['processed_chats'], value['transferred_chats'], value['total_chats']), (1, 1, 1))
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(result['transferred'], 1)
        self.assertFalse((self.root / 'pending.json').exists())

    def test_failed_import_reports_error_without_changing_ledger(self):
        def fail(side, operation, *args):
            if operation == 'import':
                raise RuntimeError('private chat body must not leak')
            return {'ok': True, 'compatible': True}
        self.endpoint.call.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'private chat body'):
            self.execute()
        value = json.loads((self.root / 'progress.json').read_text(encoding='utf-8'))
        self.assertEqual(value['status'], 'error')
        self.assertEqual(value['transferred_chats'], 0)
        self.assertNotIn('private', json.dumps(value))
        self.assertEqual(json.loads((self.root / 'ledger.json').read_text(encoding='utf-8')), self.before)
        self.assertTrue((self.root / 'pending.json').exists())

    def test_unwritable_progress_never_blocks_successful_checkpoint(self):
        original = telemetry.os.replace
        def selective(source, target):
            if Path(target).name.startswith('progress'):
                raise PermissionError('readonly progress')
            return original(source, target)
        with patch.object(telemetry.os, 'replace', side_effect=selective):
            result = self.execute()
        self.assertEqual(result['transferred'], 1)
        ledger = json.loads((self.root / 'ledger.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['threads']['chat-1']['wsl']['revision'], 'imported-native/meta-0')

    def test_dry_run_and_initialize_never_write_progress(self):
        self.args.dry_run = True
        self.execute()
        self.assertFalse((self.root / 'progress.json').exists())
        self.args.dry_run, self.args.initialize = False, True
        (self.root / 'ledger.json').unlink()
        self.execute()
        self.assertFalse((self.root / 'progress.json').exists())

    def test_supplied_writer_reused_with_request_identity(self):
        request_id = str(uuid.uuid4())
        writer = telemetry.ProgressWriter(self.root / 'progress.json', request_id=request_id)
        self.args.progress_writer = writer
        self.execute()
        value = json.loads(writer.path.read_text(encoding='utf-8'))
        self.assertEqual(value['run_id'], writer.run_id)
        self.assertEqual(value['request_id'], request_id)

    def test_source_becomes_active_reports_waiting_with_real_processed_count(self):
        def active(side, operation, *args):
            if operation == 'export':
                raise sync.EndpointCallError(side, operation, 'source_active', 'private reply')
            raise AssertionError('deferred export must not write target')
        self.endpoint.call.side_effect = active
        self.execute()
        value = json.loads((self.root / 'progress.json').read_text(encoding='utf-8'))
        self.assertEqual(value['status'], 'waiting')
        self.assertEqual((value['processed_chats'], value['transferred_chats'], value['deferred_chats']), (1, 0, 1))
        self.assertEqual(json.loads((self.root / 'ledger.json').read_text(encoding='utf-8')), self.before)
        self.assertFalse((self.root / 'pending.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows coordinator entry point')
    def test_busy_run_lock_preserves_other_runs_progress(self):
        @contextmanager
        def busy_lock(path):
            yield False
        path = self.root / 'progress.json'
        original = b'{"active-other-run":true}'
        path.write_bytes(original)
        config = self.root / 'config.json'
        config.write_text(json.dumps({'runtime_dir': str(self.root)}), encoding='utf-8')
        with patch.object(sync, 'run_lock', busy_lock), \
             patch.object(sync, 'normalize_config', return_value=self.config), \
             patch.object(sync, 'require_local_drive', side_effect=lambda path, label: path), \
             patch.object(sync, 'Endpoints', side_effect=AssertionError('must not start endpoints')), \
             redirect_stdout(io.StringIO()):
            code = sync.main(['--config', str(config), '--runtime-dir', str(self.root), '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_endpoint_telemetry_optional_and_exact_operation_correlated(self):
        endpoint = sync.Endpoints({'python_windows': 'python', 'windows_home': 'home'}, self.root)
        response = SimpleNamespace(returncode=0, stdout=b'{"ok":true}')
        with patch.object(sync.subprocess, 'run', return_value=response) as run:
            endpoint.call('windows', 'inspect')
            self.assertNotIn('--progress-file', run.call_args.args[0])
            endpoint.reporter = telemetry.ProgressWriter(self.root / 'progress.json')
            endpoint.call('windows', 'inspect')
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[cmd.index('--progress-run-id') + 1], endpoint.reporter.run_id)
            self.assertEqual(cmd[cmd.index('--progress-operation-id') + 1], endpoint.reporter.operation_id)


if __name__ == '__main__':
    unittest.main()
