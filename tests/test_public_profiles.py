"""Configuration, independent profiles and human-report regressions."""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('public_profile_controller', ROOT / 'history_sync.py')
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def configuration(directory, **overrides):
    value = {'windows_home': str(directory / 'synthetic-windows-home'),
             'wsl_distro': 'Synthetic-Ubuntu', 'wsl_user': 'synthetic-user',
             'wsl_home': '/srv/codex-fixture/linux-home/.codex', 'python_windows': sys.executable}
    value.update(overrides)
    return value


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='public-config-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root / 'profiles' / 'first.json'

    def test_default_runtime_is_relative_to_profile_not_program_directory(self):
        runtime = sync.resolve_runtime({}, self.profile)
        self.assertEqual(runtime, self.profile.parent / 'runtime')
        self.assertFalse(runtime.exists())

    def test_relative_runtime_and_override_are_relative_to_profile(self):
        config = {'runtime_directory': 'state/first'}
        self.assertEqual(sync.resolve_runtime(config, self.profile), self.profile.parent / 'state/first')
        self.assertEqual(sync.resolve_runtime(config, self.profile, Path('state/override')),
                         self.profile.parent / 'state/override')

    def test_absolute_runtime_override_wins_without_mutating_config(self):
        config = {'runtime_directory': 'configured'}
        self.assertEqual(sync.resolve_runtime(config, self.profile, self.root / 'explicit'),
                         self.root / 'explicit')
        self.assertEqual(config, {'runtime_directory': 'configured'})

    def test_positive_integer_fields_reject_boolean_zero_and_noninteger(self):
        keys = ('max_transfers_per_run', 'run_budget_seconds', 'operation_timeout_seconds',
                'quiet_seconds', 'interval_minutes', 'task_time_limit_minutes')
        for key in keys:
            for value in (True, 0, -1, 1.5, '5'):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        sync.validate_config(configuration(self.root, **{key: value}))

    def test_hard_limit_must_cover_budget_three_operations_and_cleanup(self):
        self.assertIsInstance(sync.validate_config(configuration(
            self.root, task_time_limit_minutes=9)), dict)  # Exactly 540 seconds.
        with self.assertRaises(ValueError):
            sync.validate_config(configuration(self.root, run_budget_seconds=121,
                                               task_time_limit_minutes=9))
        self.assertIsInstance(sync.validate_config(configuration(
            self.root, run_budget_seconds=121, task_time_limit_minutes=10)), dict)

    def test_invalid_task_name_characters_are_rejected(self):
        for name in ('name/other', 'name\\other', 'name\nother', 'name\x00other', 'name:other'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                sync.validate_config(configuration(self.root, task_name=name))

    def test_auto_interpreter_and_relative_home_normalization_do_not_mutate_input(self):
        supplied = configuration(self.root, windows_home='codex-home', python_windows='auto')
        original = copy.deepcopy(supplied)
        executable = self.root / 'pythonw.exe'
        with patch.object(sync.sys, 'executable', str(executable)):
            actual = sync.normalize_config(supplied, self.profile)
        self.assertEqual(Path(actual['windows_home']), self.profile.parent / 'codex-home')
        self.assertEqual(Path(actual['python_windows']), self.root / 'python.exe')
        self.assertEqual(supplied, original)

    def test_explicit_interpreter_and_task_overrides_replace_configured_values(self):
        override = self.root / 'different' / 'python.exe'
        actual = sync.normalize_config(configuration(self.root, task_name='First'), self.profile,
                                       str(override), 'Second')
        self.assertEqual(actual['task_name'], 'Second')
        self.assertEqual(Path(actual['python_windows']), override)

    @unittest.skipUnless(os.name == 'nt', 'Native drive types are a Windows API')
    def test_unc_runtime_is_rejected_without_accessing_network_or_drive_api(self):
        with patch.object(sync.ctypes.windll.kernel32, 'GetDriveTypeW') as drive_type:
            with self.assertRaises(ValueError):
                sync.require_local_drive('\\\\synthetic-server\\synthetic-share\\state', '运行目录')
        drive_type.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Native drive types are a Windows API')
    def test_mapped_network_and_unknown_drives_are_rejected_but_local_drive_is_allowed(self):
        for kind in (0, 1, 4):
            with self.subTest(kind=kind), \
                 patch.object(sync.ctypes.windll.kernel32, 'GetDriveTypeW', return_value=kind), \
                 self.assertRaises(ValueError):
                sync.require_local_drive('Z:\\synthetic-state', '运行目录')
        with patch.object(sync.ctypes.windll.kernel32, 'GetDriveTypeW', return_value=3):
            self.assertEqual(sync.require_local_drive('C:\\synthetic-state'), 'C:\\synthetic-state')


class HumanReportTests(unittest.TestCase):
    def test_specific_endpoint_reason_replaces_generic_for_same_chat(self):
        value = {'blocked': [{'id': 'one', 'reason': 'endpoint_record_unavailable'}],
                 'endpoint_skipped': {'windows': [{'id': 'one', 'code': 'rollout_outside_home'}]}}
        self.assertEqual(sync.issue_lines(value), ['  正文路径不在当地数据目录内：1 条'])

    def test_same_reason_on_two_endpoints_counts_chat_once(self):
        details = [{'id': 'one', 'code': 'rollout_outside_home'}]
        value = {'endpoint_skipped': {'windows': details, 'wsl': copy.deepcopy(details)}}
        self.assertEqual(sync.issue_lines(value), ['  正文路径不在当地数据目录内：1 条'])

    def test_generic_unavailable_without_specific_endpoint_reason_remains_visible(self):
        value = {'blocked': [{'id': 'one', 'reason': 'endpoint_record_unavailable'}]}
        self.assertEqual(sync.issue_lines(value), ['  无法安全读取某端记录：1 条'])

    def test_seventeen_duplicate_causes_are_seventeen_chats(self):
        value = {'blocked': [{'id': str(index), 'reason': 'endpoint_record_unavailable'} for index in range(17)],
                 'endpoint_skipped': {'windows': [
                     {'id': str(index), 'code': 'rollout_outside_home'} for index in range(17)]}}
        self.assertEqual(sync.issue_lines(value), ['  正文路径不在当地数据目录内：17 条'])

    def test_cached_status_displays_attention_only_once(self):
        issue = {'id': 'one', 'reason': 'empty_conversation'}
        last = {'status': 'completed', 'planned_transfers': 0, 'transferred': 0,
                'remaining_transfers': 0, 'blocked': [issue]}
        value = {'status': 'cached_status', 'initialized': True, 'tracked_chats': 1,
                 'pending_import': False, 'warnings': [],
                 'last_run': {'summary': last}, 'attention': {'blocked': [issue]}}
        self.assertEqual(sync.human_summary(value).count('聊天为空：1 条'), 1)

    def test_local_timezone_is_applied_without_changing_json_timestamps(self):
        class FixedLocalDateTime(datetime):
            def astimezone(self, tz=None):
                return super().astimezone(timezone(timedelta(hours=8)))
        timestamp = '2026-10-10T02:00:00Z'
        value = {'last_checked_at': timestamp}
        with patch.object(sync, 'datetime', FixedLocalDateTime):
            self.assertEqual(sync.local_timestamp(value['last_checked_at']),
                             '2026-10-10 10:00:00 +0800')
        self.assertEqual(json.loads(json.dumps(value))['last_checked_at'], timestamp)


class ProfileEntryPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='public-main-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def invoke(self, profile, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = sync.main(['--config', str(profile), '--json', *arguments])
        return code, json.loads(output.getvalue())

    def test_status_explicit_runtime_works_without_config_and_writes_nothing(self):
        runtime = self.root / 'selected-state'
        with patch.object(sync, 'Endpoints', side_effect=AssertionError('endpoint started')), \
             patch.object(sync, 'wsl_running', side_effect=AssertionError('WSL queried')):
            code, result = self.invoke(self.root / 'missing.json', '--status',
                                       '--runtime-dir', str(runtime), '--task-name', 'Selected')
        self.assertEqual(code, 0)
        self.assertEqual(Path(result['runtime_directory']), runtime)
        self.assertEqual(result['task_name'], 'Selected')
        self.assertFalse(runtime.exists())

    def test_status_task_override_is_kept_even_for_nonobject_config(self):
        profile = self.root / 'invalid.json'
        profile.write_text('[]', encoding='utf-8')
        code, result = self.invoke(profile, '--status', '--task-name', 'Override')
        self.assertEqual(code, 0)
        self.assertEqual(result['task_name'], 'Override')
        self.assertFalse((self.root / 'runtime').exists())

    def test_status_survives_valid_json_with_malformed_nested_cached_summary(self):
        profile = self.root / 'config.json'
        profile.write_text(json.dumps(configuration(self.root)), encoding='utf-8')
        runtime = self.root / 'runtime'
        runtime.mkdir()
        report = runtime / 'last-run.json'
        report.write_text(json.dumps({'summary': {'endpoint_skipped': []}}), encoding='utf-8')
        before = report.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            code = sync.main(['--config', str(profile), '--status'])
        self.assertEqual(code, 0)
        self.assertIn('同步状态', output.getvalue())
        self.assertEqual(report.read_bytes(), before)

    def test_runtime_pointing_to_existing_file_returns_error_without_endpoint_or_data_changes(self):
        profile = self.root / 'config.json'
        profile.write_text(json.dumps(configuration(self.root, runtime_directory='occupied')), encoding='utf-8')
        occupied = self.root / 'occupied'
        occupied.write_bytes(b'protected synthetic marker')
        before = profile.read_bytes()
        with patch.object(sync, 'Endpoints', side_effect=AssertionError('endpoint started')), \
             patch.object(sync, 'wsl_running', side_effect=AssertionError('WSL queried')):
            code, result = self.invoke(profile, '--initialize')
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'error')
        self.assertIn('运行目录', result['error'])
        self.assertEqual(occupied.read_bytes(), b'protected synthetic marker')
        self.assertEqual(profile.read_bytes(), before)
        self.assertFalse((self.root / 'synthetic-windows-home').exists())

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_report_save_error_remains_visible_without_touching_conversations(self):
        profile = self.root / 'config.json'
        profile.write_text(json.dumps(configuration(self.root)), encoding='utf-8')
        before = profile.read_bytes()
        with patch.object(sync, 'wsl_running', return_value=False), \
             patch.object(sync, 'Endpoints', side_effect=AssertionError('endpoint started')), \
             patch.object(sync, 'save_run_report', side_effect=OSError('synthetic report failure')):
            code, result = self.invoke(profile)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'error')
        self.assertIn('synthetic report failure', result['error'])
        self.assertTrue(any('synthetic report failure' in warning for warning in result['warnings']))
        self.assertEqual(profile.read_bytes(), before)
        self.assertFalse((self.root / 'runtime' / 'ledger.json').exists())
        self.assertFalse((self.root / 'synthetic-windows-home').exists())

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_profiles_initialize_independent_relative_runtimes(self):
        endpoint = MagicMock()
        endpoint.inspect.return_value = {'threads': [], 'backend_running': False}
        for name in ('first', 'second'):
            profile = self.root / name / 'config.json'
            profile.parent.mkdir()
            profile.write_text(json.dumps(configuration(self.root, runtime_directory='state')), encoding='utf-8')
            with patch.object(sync, 'Endpoints', return_value=endpoint), \
                 patch.object(sync, 'wsl_running', return_value=True):
                code, result = self.invoke(profile, '--initialize')
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'initialized')
            self.assertTrue((profile.parent / 'state' / 'ledger.json').is_file())
        self.assertFalse((self.root / 'runtime').exists())
        self.assertNotEqual(self.root / 'first/state', self.root / 'second/state')

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_normal_run_uses_runtime_override_without_touching_configured_runtime(self):
        profile = self.root / 'config.json'
        profile.write_text(json.dumps(configuration(self.root, runtime_directory='original')), encoding='utf-8')
        runtime = self.root / 'override'
        endpoint = MagicMock()
        endpoint.inspect.return_value = {'threads': [], 'backend_running': False}
        with patch.object(sync, 'Endpoints', return_value=endpoint) as factory, \
             patch.object(sync, 'wsl_running', return_value=True):
            code, result = self.invoke(profile, '--initialize', '--runtime-dir', str(runtime),
                                       '--task-name', 'Other', '--python-windows', sys.executable)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'initialized')
        self.assertEqual(factory.call_args.args[0]['task_name'], 'Other')
        self.assertTrue((runtime / 'ledger.json').is_file())
        self.assertFalse((self.root / 'original').exists())


if __name__ == '__main__':
    unittest.main()
