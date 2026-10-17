"""Process shutdown tests use fake identities; no real Codex is stopped."""
import importlib.util
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import history_windows as shutdown

spec = importlib.util.spec_from_file_location('choice_test_fixtures', ROOT / 'tests/test_merge_chat.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
core = fixtures.core


class FakeProcesses:
    def __init__(self, graceful=False):
        self.rows = [{'pid': 10, 'parent': 0, 'live': True}, {'pid': 11, 'parent': 10, 'live': True}]
        self.graceful = graceful
        self.closed, self.killed, self.released = [], [], []

    def open_candidates(self):
        return self.rows

    def request_close(self, rows):
        self.closed = [row['pid'] for row in rows]
        if self.graceful:
            for row in rows:
                row['live'] = False

    def alive(self, row):
        return row['live']

    def terminate(self, row):
        self.killed.append(row['pid'])
        row['live'] = False

    def release(self, row):
        self.released.append(row['pid'])


class ShutdownTests(unittest.TestCase):
    def run_shutdown(self, api):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        return shutdown.stop_windows_codex(api=api, clock=lambda: now[0], sleep=sleep)

    def test_graceful_exit_does_not_terminate(self):
        api = FakeProcesses(graceful=True)
        self.assertEqual(self.run_shutdown(api)['forced_count'], 0)
        self.assertEqual(api.closed, [10, 11])
        self.assertFalse(api.killed)
        self.assertEqual(api.released, [10, 11])

    def test_lingering_children_stop_before_parent_after_grace_period(self):
        api = FakeProcesses()
        result = self.run_shutdown(api)
        self.assertEqual((result['status'], result['forced_count']), ('stopped', 2))
        self.assertEqual(api.killed, [11, 10])
        self.assertEqual(api.released, [10, 11])

    def test_permission_failure_releases_handles_and_does_not_claim_success(self):
        api = FakeProcesses()
        def denied(row):
            raise PermissionError()
        api.terminate = denied
        self.assertEqual(self.run_shutdown(api)['reason'], 'windows_exit_failed')
        self.assertEqual(api.released, [10, 11])

    def test_managed_desktop_requires_separate_opt_in_before_any_shutdown(self):
        api = FakeProcesses()
        def managed():
            raise shutdown.DesktopHostRequired()
        api.open_candidates = managed
        result = self.run_shutdown(api)
        self.assertEqual(result['reason'], 'windows_exit_desktop_required')
        self.assertEqual(result['forced_count'], 0)
        self.assertFalse(api.closed)
        self.assertFalse(api.killed)

    def test_explicit_desktop_host_exits_before_children_to_prevent_respawn(self):
        api = FakeProcesses()
        api.rows.append({'pid': 20, 'parent': 0, 'name': 'ChatGPT.exe', 'live': True})
        api.rows[0]['parent'] = 20
        self.assertEqual(self.run_shutdown(api)['status'], 'stopped')
        self.assertEqual(api.killed, [20, 11, 10])

    def test_native_read_only_snapshot_can_verify_own_user_and_session(self):
        if os.name != 'nt':
            self.skipTest('Native Windows API')
        api = shutdown.WindowsProcesses()
        self.assertEqual(api.session_id(os.getpid()), api.session)
        self.assertTrue(any(row['pid'] == os.getpid() for row in api.snapshot()))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows process lifecycle')
    def test_native_termination_only_affects_owned_temporary_fixture(self):
        with tempfile.TemporaryDirectory(prefix='codex-exit-fixture-') as folder:
            executable = Path(folder) / 'codex-sync-test-fixture.exe'
            shutil.copy2(sys.executable, executable)
            env = {**os.environ, 'PYTHONHOME': sys.base_prefix,
                   'PATH': sys.base_prefix + os.pathsep + os.environ.get('PATH', '')}
            child = subprocess.Popen([str(executable), '-B', '-S', '-c', 'import time; time.sleep(60)'],
                                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                api = shutdown.WindowsProcesses()
                snapshot = api.snapshot
                def only_owned_fixture():
                    rows = [row for row in snapshot() if row['pid'] == child.pid]
                    self.assertEqual(len(rows), 1)
                    return rows
                # Filter the native snapshot BEFORE opening handles. Otherwise
                # an unrelated real Codex/desktop process changing or denying
                # access can fail an isolated lifecycle test before filtering.
                # Identity, owner, session and termination still use real APIs.
                api.snapshot = only_owned_fixture
                result = shutdown.stop_windows_codex(api=api)
                self.assertEqual((result['status'], result['forced_count']), ('stopped', 1), result)
                self.assertEqual(child.wait(timeout=5), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


class AutomaticChoiceTests(unittest.TestCase):
    # Reuse the existing transaction fixture, including complete private backups.
    select = fixtures.ManualChoiceTransaction.select
    consume = fixtures.ManualChoiceTransaction.consume
    saved_ledger = fixtures.ManualChoiceTransaction.saved_ledger
    choice = fixtures.ManualChoiceTransaction.choice

    def setUp(self):
        fixtures.ManualChoiceTransaction.setUp(self)
        original = self.endpoint.inspect
        self.endpoint.inspect = lambda side: {**original(side), 'backend_detection_ok': True}
        self.config['auto_exit_windows_for_wsl_choices'] = True
        self.args.keep_side = 'wsl'

    def stop(self, **_):
        self.endpoint.backends['windows'] = False
        return {'status': 'stopped', 'reason': 'windows_exit_completed'}

    def test_auto_exit_then_same_id_import_without_copy(self):
        self.select()
        with patch.object(shutdown, 'stop_windows_codex', side_effect=self.stop) as stop:
            result = self.consume()
        stop.assert_called_once()
        self.assertEqual(result['transferred'], 1)
        self.assertEqual(result['windows_exit']['status'], 'stopped')
        self.assertEqual(self.choice()['status'], 'resolved')
        self.assertEqual(set(self.saved_ledger()['threads']), {'old'})
        self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertEqual(self.consume()['transferred'], 0)

    def test_feature_disabled_dry_run_and_opposite_choice_never_stop(self):
        self.select()
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            self.config['auto_exit_windows_for_wsl_choices'] = False
            self.consume()
            self.config['auto_exit_windows_for_wsl_choices'] = True
            self.sync_args.dry_run = True
            self.consume()
        stop.assert_not_called()

    def test_windows_winner_never_exits_windows(self):
        self.args.keep_side = 'windows'
        self.select()
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            self.consume()
        stop.assert_not_called()

    def test_pending_import_does_not_close_application(self):
        self.select()
        (self.runtime / 'pending.json').write_text('{}')
        with patch.object(shutdown, 'stop_windows_codex') as stop, self.assertRaises(RuntimeError):
            self.consume()
        stop.assert_not_called()

    def test_version_flushed_on_exit_requires_reconfirmation(self):
        self.select()
        def flushed(**kwargs):
            self.endpoint.rows['windows']['revision'] = 'newly-flushed-message'
            return self.stop(**kwargs)
        with patch.object(shutdown, 'stop_windows_codex', side_effect=flushed):
            result = self.consume()
        self.assertEqual(result['transferred'], 0)
        self.assertEqual(self.choice()['status'], 'needs_confirmation')
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_other_windows_chat_activity_prevents_automatic_exit(self):
        self.select()
        original = self.endpoint.inspect
        def inspect(side):
            value = original(side)
            if side == 'windows':
                value['threads'].append({**value['threads'][0], 'id': 'other-chat', 'active': True})
            return value
        self.endpoint.inspect = inspect
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            result = self.consume()
        stop.assert_not_called()
        self.assertEqual(result['windows_exit']['reason'], 'windows_exit_busy')

    def test_incompatible_workspace_does_not_close_application(self):
        self.select()
        self.endpoint.compatible = False
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            result = self.consume()
        stop.assert_not_called()
        self.assertEqual(result['windows_exit']['reason'], 'windows_exit_incompatible')

    def test_corrupt_preserved_backup_prevents_process_shutdown(self):
        self.select()
        package = core.conflict_choice_package(self.runtime, self.choice(), 'windows')
        (package / 'transcript.jsonl').write_text('changed backup')
        with patch.object(shutdown, 'stop_windows_codex') as stop, self.assertRaises(RuntimeError):
            self.consume()
        stop.assert_not_called()
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_change_during_preflight_prevents_process_shutdown(self):
        self.select()
        self.endpoint.after_preview = lambda: self.endpoint.rows['windows'].update(revision='concurrent-message')
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            result = self.consume()
        stop.assert_not_called()
        self.assertEqual(result['transferred'], 0)
        self.assertEqual(self.choice()['status'], 'needs_confirmation')

    def test_restarted_backend_is_not_bypassed(self):
        self.select()
        with patch.object(shutdown, 'stop_windows_codex', return_value={'status': 'stopped'}):
            result = self.consume()
        self.assertEqual(result['transferred'], 0)
        self.assertEqual(result['windows_exit']['reason'], 'windows_exit_incomplete')
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_invalid_opt_in_values_are_rejected(self):
        from tests.test_public_profiles import configuration
        for value in (1, 'true', None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.validate_config(configuration(self.runtime, auto_exit_windows_for_wsl_choices=value))


class OrdinaryUpdateTests(unittest.TestCase):
    consume = fixtures.ManualChoiceTransaction.consume
    saved_ledger = fixtures.ManualChoiceTransaction.saved_ledger

    def setUp(self):
        fixtures.ManualChoiceTransaction.setUp(self)
        original = self.endpoint.inspect
        self.endpoint.inspect = lambda side: {**original(side), 'backend_detection_ok': True}
        original_call = self.endpoint.call
        def call(side, operation, *args):
            if operation == 'export':
                package = Path(args[args.index('--package') + 1])
                if package.is_dir() and not list(package.iterdir()):
                    package.rmdir()  # Synthetic exporter creates its directory.
            return original_call(side, operation, *args)
        self.endpoint.call = call
        self.ledger['threads']['old']['windows'] = self.endpoint.rows['windows'].copy()
        core.atomic_json(self.runtime / 'ledger.json', self.ledger)
        self.config['auto_exit_windows_for_wsl_updates'] = True

    def stop(self, **_):
        self.endpoint.backends['windows'] = False
        return {'status': 'stopped', 'reason': 'windows_exit_completed', 'process_count': 1}

    def test_ordinary_wsl_update_closes_windows_and_transfers_without_choice(self):
        with patch.object(shutdown, 'stop_windows_codex', side_effect=self.stop) as stop:
            result = self.consume()
        stop.assert_called_once()
        self.assertEqual(result['transferred'], 1)
        self.assertFalse((self.runtime / 'conflict-choices.json').exists())
        self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertEqual(self.consume()['transferred'], 0)

    def test_opposite_direction_conflicts_disabled_and_dry_run_never_stop(self):
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            self.config['auto_exit_windows_for_wsl_updates'] = False
            self.consume()
            self.config['auto_exit_windows_for_wsl_updates'] = True
            self.sync_args.dry_run = True
            self.consume()
            self.sync_args.dry_run = False
            # Both sides changed: automatic shutdown cannot choose a winner.
            self.endpoint.rows['windows']['revision'] = 'concurrent-windows'
            self.consume()
        stop.assert_not_called()

    def test_incompatible_ordinary_source_does_not_close_windows(self):
        self.endpoint.compatible = False
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            result = self.consume()
        stop.assert_not_called()
        self.assertEqual(result['windows_exit']['reason'], 'windows_exit_incompatible')

    def test_source_change_during_preflight_does_not_close_windows(self):
        self.endpoint.after_preview = lambda: self.endpoint.rows['wsl'].update(revision='new-source')
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            result = self.consume()
        stop.assert_not_called()
        self.assertEqual(result['windows_exit']['reason'], 'windows_exit_choice_changed')

    def test_windows_flush_after_stop_becomes_conflict_instead_of_overwrite(self):
        def flushed(**kwargs):
            self.endpoint.rows['windows']['revision'] = 'flushed-message'
            return self.stop(**kwargs)
        with patch.object(shutdown, 'stop_windows_codex', side_effect=flushed):
            result = self.consume()
        self.assertEqual(result['transferred'], 0)
        self.assertTrue(any(x['reason'] == 'both_sides_changed' for x in result['blocked']))
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_busy_windows_or_unfinished_import_prevents_automatic_exit(self):
        self.endpoint.rows['windows']['active'] = True
        with patch.object(shutdown, 'stop_windows_codex') as stop:
            self.consume()
        stop.assert_not_called()
        self.endpoint.rows['windows']['active'] = False
        (self.runtime / 'pending.json').write_text('{}')
        with patch.object(shutdown, 'stop_windows_codex') as stop, self.assertRaises(RuntimeError):
            self.consume()
        stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
