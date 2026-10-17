"""Read-only progress integration using isolated runtime and native-call fakes."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preview_progress_tests', ROOT / 'preview_sync.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class SourceEntryPoints(unittest.TestCase):
    def test_installer_and_preview_start_without_repository_pythonpath(self):
        import os
        environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        with tempfile.TemporaryDirectory() as directory:
            for name in ('install.py', 'preview_sync.py'):
                with self.subTest(entry_point=name):
                    result = subprocess.run([sys.executable, '-B', str(ROOT / name), '--help'],
                                            cwd=directory, env=environment, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
                    self.assertIn(b'--config', result.stdout)


class PreviewProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='preview-progress-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name).resolve() / 'custom-runtime'
        self.runtime.mkdir()
        (self.runtime / 'ledger.json').write_text(json.dumps({'version': 1, 'threads': {}}))
        (self.runtime / 'run.lock').write_bytes(b'0')
        self.request_id = str(uuid.uuid4())
        self.controller = ROOT.parent / 'history_sync.py'
        self.config = {'wsl_distro': 'synthetic', 'quiet_seconds': 60}
        self.held = False
        self.acquired = True
        self.events = []
        self.endpoints = []
        self.inspection_failure = None
        self.inspection_hook = None
        self.summary = {'eligible': False, 'candidate_ids': [], 'reason': 'no_pending_updates'}

        @contextmanager
        def run_lock(path):
            self.assertEqual(path, self.runtime / 'run.lock')
            self.held = self.acquired
            self.events.append('lock')
            try:
                yield self.acquired
            finally:
                self.events.append('unlock')
                self.held = False
        owner = self
        class Endpoint:
            def __init__(self, config, source):
                owner.assertTrue(owner.held)
                self.reporter = None
                owner.endpoints.append(self)
            def inspect(self, side):
                owner.assertTrue(owner.held)
                snapshot = owner.snapshot()
                owner.events.append(('inspect', side, snapshot['phase']))
                owner.assertEqual(snapshot['phase'], 'checking_' + side)
                if owner.inspection_hook:
                    owner.inspection_hook(side)
                if owner.inspection_failure:
                    raise owner.inspection_failure
                return {'threads': [], 'backend_detection_ok': True,
                        'backend_running': True, 'backend_count': 1}
        self.sync = SimpleNamespace(VERSION=1, wsl_running=Mock(return_value=True),
                                    run_lock=run_lock, Endpoints=Endpoint)

    def snapshot(self):
        return json.loads((self.runtime / 'progress.json').read_text(encoding='utf-8'))

    def harness(self):
        stack = ExitStack()
        stack.enter_context(patch.object(preview, 'os', SimpleNamespace(name='nt')))
        stack.enter_context(patch.object(preview, 'load_controller', return_value=(
            self.controller, self.sync, self.config, self.runtime)))
        stack.enter_context(patch.object(preview, 'virtual_local_settings_ledger',
                                         return_value=({'version': 1, 'threads': {}}, {})))
        def classify(*args):
            self.assertTrue(self.held)
            self.assertEqual(self.snapshot()['phase'], 'planning')
            self.events.append('plan')
            return deepcopy(self.summary)
        stack.enter_context(patch.object(preview, 'classify_plan', side_effect=classify))
        self.addCleanup(stack.close)
        return stack

    def test_probe_holds_lock_through_both_native_scans_and_terminal_progress(self):
        with self.harness():
            result = preview.probe(self.controller, self.controller, self.request_id)
        self.assertEqual(result['reason'], 'no_pending_updates')
        self.assertEqual(self.events, ['lock', ('inspect', 'windows', 'checking_windows'),
                                      ('inspect', 'wsl', 'checking_wsl'), 'plan', 'unlock'])
        snapshot = self.snapshot()
        self.assertEqual((snapshot['kind'], snapshot['request_id'], snapshot['status']),
                         ('check', self.request_id, 'completed'))
        self.assertEqual(self.endpoints[0].reporter.run_id, snapshot['run_id'])
        self.assertFalse(self.held)

    def test_busy_probe_preserves_other_run_snapshot_exactly(self):
        self.acquired = False
        original = b'{"owner":"other-active-run","progress":3}'
        (self.runtime / 'progress.json').write_bytes(original)
        with self.harness():
            result = preview.probe(self.controller, self.controller, self.request_id)
        self.assertEqual(result['reason'], 'controller_running')
        self.assertEqual((self.runtime / 'progress.json').read_bytes(), original)
        self.assertEqual(self.endpoints, [])

    def test_pending_race_after_lock_never_replaces_progress(self):
        @contextmanager
        def pending_lock(_):
            (self.runtime / 'pending.json').write_text('{}')
            yield True
        original = b'{"owner":"pending-import"}'
        (self.runtime / 'progress.json').write_bytes(original)
        self.sync.run_lock = pending_lock
        with self.harness():
            result = preview.probe(self.controller, self.controller, self.request_id)
        self.assertEqual(result['reason'], 'pending_import')
        self.assertEqual((self.runtime / 'progress.json').read_bytes(), original)
        self.assertFalse(self.endpoints)

    def test_active_probe_cannot_be_replaced_by_second_probe(self):
        def nested(side):
            if side == 'windows':
                original = (self.runtime / 'progress.json').read_bytes()
                self.acquired = False
                result = preview.probe(self.controller, self.controller, str(uuid.uuid4()))
                self.assertEqual(result['reason'], 'controller_running')
                self.assertEqual((self.runtime / 'progress.json').read_bytes(), original)
                self.acquired = True
                # This fake lock uses a bool; restore the outer lock's ownership.
                self.held = True
        self.inspection_hook = nested
        with self.harness():
            preview.probe(self.controller, self.controller, self.request_id)
        self.assertEqual(self.snapshot()['request_id'], self.request_id)
        self.assertEqual(len(self.endpoints), 1)

    def test_native_scan_failure_publishes_terminal_error_without_private_text(self):
        self.inspection_failure = RuntimeError('private chat data in native exception')
        ledger_before = (self.runtime / 'ledger.json').read_bytes()
        with self.harness(), self.assertRaises(RuntimeError):
            preview.probe(self.controller, self.controller, self.request_id)
        snapshot = self.snapshot()
        self.assertEqual((snapshot['status'], snapshot['phase'], snapshot['reason']),
                         ('error', 'error', 'task_failed'))
        self.assertNotIn('private chat', json.dumps(snapshot))
        self.assertEqual((self.runtime / 'ledger.json').read_bytes(), ledger_before)
        self.assertFalse(self.held)

    def test_ready_updates_are_waiting_not_claimed_transferred(self):
        self.summary = {'eligible': True, 'candidate_ids': ['private-id'], 'reason': 'updates_available'}
        with self.harness():
            preview.probe(self.controller, self.controller, self.request_id)
        snapshot = self.snapshot()
        self.assertEqual((snapshot['status'], snapshot['reason']), ('waiting', 'updates_available'))
        self.assertEqual(snapshot.get('transferred_chats', 0), 0)
        self.assertNotIn('private-id', json.dumps(snapshot))

    def test_sync_once_reuses_same_reporter_for_ack_execute_and_report(self):
        reporters = []
        def acknowledge(sync, endpoint, config, controller, runtime):
            self.assertTrue(self.held)
            reporters.append(endpoint.reporter)
            endpoint.reporter.update('planning')
            return ['private-id']
        def execute(config, args, runtime, log):
            self.assertTrue(self.held)
            self.assertIs(args.progress_writer, reporters[0])
            self.assertEqual(args.progress_request_id, self.request_id)
            args.progress_writer.update('writing', current_index=1, total_chats=1)
            args.progress_writer.finish('completed', transferred_chats=1, total_chats=1)
            return {'status': 'completed', 'transferred': 1, 'planned_transfers': 1}
        def report(runtime, result):
            self.assertTrue(self.held)
            self.assertEqual(self.snapshot()['status'], 'completed')
        self.sync.execute = execute
        self.sync.save_run_report = report
        with self.harness(), patch.object(preview, 'acknowledge_local_settings', side_effect=acknowledge):
            result = preview.sync_once(self.controller, self.controller, self.request_id)
        snapshot = self.snapshot()
        self.assertEqual((snapshot['kind'], snapshot['request_id'], snapshot['transferred_chats']),
                         ('sync', self.request_id, 1))
        self.assertEqual(result['local_settings_acknowledged'], 1)
        self.assertIs(self.endpoints[0].reporter, reporters[0])

    def test_sync_once_busy_preserves_progress_and_never_acknowledges(self):
        self.acquired = False
        original = b'{"owner":"existing-controller"}'
        (self.runtime / 'progress.json').write_bytes(original)
        with self.harness(), patch.object(preview, 'acknowledge_local_settings') as ack:
            result = preview.sync_once(self.controller, self.controller, self.request_id)
        self.assertEqual(result['status'], 'skipped_already_running')
        ack.assert_not_called()
        self.assertEqual((self.runtime / 'progress.json').read_bytes(), original)

    def test_outgoing_sync_skips_wsl_acknowledgement_and_passes_strict_scope(self):
        self.sync.restrict_outgoing_plan = Mock()
        def execute(config,args,runtime,log):
            self.assertTrue(self.held)
            self.assertIs(args.outgoing_only,True)
            return {'status':'completed','transferred':1,'planned_transfers':1,'outgoing_only':True}
        self.sync.execute=execute
        self.sync.save_run_report=Mock()
        with self.harness(), patch.object(preview,'acknowledge_local_settings',side_effect=AssertionError('WSL must stay open')) as ack:
            result=preview.sync_once(self.controller,self.controller,self.request_id,outgoing_only=True)
        ack.assert_not_called()
        self.assertEqual(result['local_settings_acknowledged'],0)
        self.assertTrue(result['outgoing_only'])
        self.assertFalse(self.held)

    def test_outgoing_sync_rejects_controller_without_direction_scope(self):
        with self.harness(), self.assertRaises(RuntimeError):
            preview.sync_once(self.controller,self.controller,self.request_id,outgoing_only=True)

    def test_sync_once_ack_failure_reports_error_and_never_executes(self):
        self.sync.execute = Mock(side_effect=AssertionError('must not import'))
        with self.harness(), patch.object(preview, 'acknowledge_local_settings', side_effect=OSError('private-error')):
            with self.assertRaises(OSError):
                preview.sync_once(self.controller, self.controller, self.request_id)
        self.sync.execute.assert_not_called()
        self.assertEqual(self.snapshot()['status'], 'error')
        self.assertEqual(self.snapshot()['request_id'], self.request_id)
        self.assertNotIn('private-error', json.dumps(self.snapshot()))

    def test_sync_once_endpoint_initialization_failure_terminates_progress(self):
        self.sync.Endpoints = Mock(side_effect=OSError('private endpoint initialization failure'))
        with self.harness(), patch.object(preview, 'acknowledge_local_settings') as acknowledge:
            with self.assertRaises(OSError):
                preview.sync_once(self.controller, self.controller, self.request_id)
        acknowledge.assert_not_called()
        snapshot = self.snapshot()
        self.assertEqual((snapshot['status'], snapshot['reason']), ('error', 'task_failed'))
        self.assertEqual(snapshot['request_id'], self.request_id)
        self.assertFalse(self.held)
        self.assertNotIn('private endpoint', json.dumps(snapshot))

    def test_progress_path_resolves_custom_runtime_without_wsl_or_databases(self):
        config_path = self.runtime.parent / 'config.json'
        config_path.write_text(json.dumps({'windows_home': str(self.runtime.parent / 'unused-home'),
            'wsl_distro': 'synthetic', 'wsl_user': 'synthetic', 'wsl_home': '/tmp/unused-home',
            'python_windows': 'auto', 'runtime_directory': 'nested/custom-runtime'}))
        original_loader = preview.load_controller
        def load(*args):
            controller, sync, config, runtime = original_loader(*args)
            sync.wsl_running = Mock(side_effect=AssertionError('must not query WSL'))
            sync.Endpoints = Mock(side_effect=AssertionError('must not read databases'))
            # Native drive mapping is tested elsewhere; keep this test portable.
            sync.linux_mount_path = str
            return controller, sync, config, runtime
        with patch.object(preview, 'load_controller', side_effect=load), \
                patch.object(preview.subprocess, 'run', side_effect=AssertionError('must not start WSL')):
            result = preview.progress_path(self.controller, config_path)
        self.assertEqual(result, {'ok': True, 'progress_file': str(
            (config_path.parent / 'nested/custom-runtime/progress.json').resolve())})
        self.assertFalse((config_path.parent / 'nested').exists())


if __name__ == '__main__':
    unittest.main()
