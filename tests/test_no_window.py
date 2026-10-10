"""Subprocess window, protocol and WSL lifecycle regressions using only mocks."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('no_window_controller', ROOT / 'history_sync.py')
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

NO_WINDOW = 0x08000000
LIST_COMMAND = ['wsl.exe', '--list', '--running', '--quiet']


def response(value, returncode=0):
    return subprocess.CompletedProcess(
        [], returncode, json.dumps(value, ensure_ascii=False).encode('utf-8'), b'')


def distributions(*names, returncode=0):
    return subprocess.CompletedProcess(
        [], returncode, ('\r\n'.join(names) + '\r\n').encode('utf-16'), b'')


@contextmanager
def mocked_platform(name, **run_options):
    """Replace module references, avoiding changes to the host's os/pathlib."""
    runner = Mock(**run_options)
    process_api = SimpleNamespace(run=runner)
    if name == 'nt':
        process_api.CREATE_NO_WINDOW = NO_WINDOW
    # POSIX deliberately has no CREATE_NO_WINDOW attribute.
    with patch.object(sync, 'os', SimpleNamespace(name=name)), \
         patch.object(sync, 'subprocess', process_api):
        yield runner


class NoWindowTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            'python_windows': 'synthetic-python.exe',
            'windows_home': 'synthetic-windows-home',
            'wsl_distro': 'Synthetic-Ubuntu',
            'wsl_user': 'your-linux-user',
            'wsl_home': '/srv/codex-fixture/.codex',
            'python_wsl': '/usr/bin/python3',
            'quiet_seconds': 90,
            'operation_timeout_seconds': 47,
        }
        self.endpoint = sync.Endpoints(self.config, ROOT)

    def expected_windows_command(self, operation, *args):
        return [self.config['python_windows'], str(ROOT / 'history_endpoint.py'),
                operation, '--home', self.config['windows_home'],
                '--quiet-seconds', '90', *map(str, args)]

    def expected_wsl_command(self, operation, *args):
        return ['wsl.exe', '--distribution', 'Synthetic-Ubuntu', '--user',
                'your-linux-user', '--exec', '/usr/bin/python3',
                '/mnt/synthetic/history_endpoint.py', operation,
                '--home', '/srv/codex-fixture/.codex',
                '--quiet-seconds', '90', *map(str, args)]

    def test_windows_inventory_uses_no_window_and_preserves_utf16_output(self):
        with mocked_platform('nt', return_value=distributions('Other', 'Synthetic-Ubuntu')) as run:
            self.assertTrue(sync.wsl_running('Synthetic-Ubuntu'))
        run.assert_called_once_with(LIST_COMMAND, capture_output=True, timeout=15,
                                    creationflags=NO_WINDOW)

    def test_inventory_returns_false_without_launching_a_missing_distribution(self):
        with mocked_platform('nt', return_value=distributions('Other')) as run:
            self.assertFalse(sync.wsl_running('Synthetic-Ubuntu'))
        run.assert_called_once_with(LIST_COMMAND, capture_output=True, timeout=15,
                                    creationflags=NO_WINDOW)

    def test_inventory_nonzero_returncode_remains_an_error(self):
        with mocked_platform('nt', return_value=distributions(returncode=1)) as run:
            with self.assertRaisesRegex(RuntimeError, 'Cannot query running WSL distributions'):
                sync.wsl_running('Synthetic-Ubuntu')
        self.assertEqual(run.call_count, 1)

    def test_inventory_timeout_is_not_swallowed(self):
        failure = subprocess.TimeoutExpired(LIST_COMMAND, 15)
        with mocked_platform('nt', side_effect=failure) as run:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                sync.wsl_running('Synthetic-Ubuntu')
        self.assertIs(caught.exception, failure)
        self.assertEqual(run.call_count, 1)

    def test_posix_inventory_does_not_require_windows_creation_constant(self):
        with mocked_platform('posix', return_value=distributions('Synthetic-Ubuntu')) as run:
            self.assertTrue(sync.wsl_running('Synthetic-Ubuntu'))
        run.assert_called_once_with(LIST_COMMAND, capture_output=True, timeout=15,
                                    creationflags=0)

    def test_windows_endpoint_preserves_json_arguments_and_single_process(self):
        value = {'ok': True, 'title': '\u5408\u6210\u6d4b\u8bd5', 'count': 3}
        with mocked_platform('nt', return_value=response(value)) as run, \
             patch.object(sync, 'wsl_running') as inventory:
            self.assertEqual(self.endpoint.call('windows', 'export', '--id', 42), value)
        run.assert_called_once_with(self.expected_windows_command('export', '--id', 42),
                                    capture_output=True, timeout=47, creationflags=NO_WINDOW)
        inventory.assert_not_called()

    def test_wsl_endpoint_hides_both_inventory_and_endpoint_processes(self):
        value = {'ok': True, 'backend_detection_ok': True}
        with mocked_platform('nt', side_effect=[distributions('Synthetic-Ubuntu'), response(value)]) as run, \
             patch.object(sync, 'linux_mount_path', return_value='/mnt/synthetic/history_endpoint.py') as mount:
            self.assertEqual(self.endpoint.call('wsl', 'inspect'), value)
        self.assertEqual(run.call_args_list, [
            call(LIST_COMMAND, capture_output=True, timeout=15, creationflags=NO_WINDOW),
            call(self.expected_wsl_command('inspect'), capture_output=True, timeout=47,
                 creationflags=NO_WINDOW),
        ])
        mount.assert_called_once_with(ROOT / 'history_endpoint.py')

    def test_wsl_endpoint_does_not_run_or_map_a_stopped_distribution(self):
        with mocked_platform('nt', return_value=distributions('Other')) as run, \
             patch.object(sync, 'linux_mount_path') as mount:
            with self.assertRaisesRegex(RuntimeError, 'WSL stopped during this run'):
                self.endpoint.call('wsl', 'inspect')
        run.assert_called_once_with(LIST_COMMAND, capture_output=True, timeout=15,
                                    creationflags=NO_WINDOW)
        mount.assert_not_called()

    def test_wsl_running_is_rechecked_before_every_endpoint_call(self):
        value = {'ok': True}
        with mocked_platform('nt', side_effect=[distributions('Synthetic-Ubuntu'),
                                              response(value), distributions('Other')]) as run, \
             patch.object(sync, 'linux_mount_path', return_value='/mnt/synthetic/history_endpoint.py') as mount:
            self.assertEqual(self.endpoint.call('wsl', 'inspect'), value)
            with self.assertRaisesRegex(RuntimeError, 'WSL stopped during this run'):
                self.endpoint.call('wsl', 'backend-status')
        self.assertEqual(run.call_args_list, [
            call(LIST_COMMAND, capture_output=True, timeout=15, creationflags=NO_WINDOW),
            call(self.expected_wsl_command('inspect'), capture_output=True, timeout=47,
                 creationflags=NO_WINDOW),
            call(LIST_COMMAND, capture_output=True, timeout=15, creationflags=NO_WINDOW),
        ])
        mount.assert_called_once()

    def test_posix_endpoint_paths_need_no_windows_constant(self):
        value = {'ok': True}
        for side in ('windows', 'wsl'):
            with self.subTest(side=side), \
                 mocked_platform('posix', return_value=response(value)) as run, \
                 patch.object(sync, 'wsl_running', return_value=True) as inventory, \
                 patch.object(sync, 'linux_mount_path', return_value='/mnt/synthetic/history_endpoint.py'):
                self.assertEqual(self.endpoint.call(side, 'inspect'), value)
                expected = (self.expected_windows_command('inspect') if side == 'windows'
                            else self.expected_wsl_command('inspect'))
                run.assert_called_once_with(expected, capture_output=True, timeout=47,
                                            creationflags=0)
                if side == 'windows':
                    inventory.assert_not_called()
                else:
                    inventory.assert_called_once_with('Synthetic-Ubuntu')

    def test_endpoint_invalid_json_remains_an_error_without_retry(self):
        result = subprocess.CompletedProcess([], 0, b'not-json', b'')
        with mocked_platform('nt', return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, 'windows inspect: invalid endpoint response'):
                self.endpoint.call('windows', 'inspect')
        self.assertEqual(run.call_count, 1)

    def test_endpoint_nonzero_returncode_preserves_json_error_message(self):
        with mocked_platform('nt', return_value=response({'message': 'synthetic failure'}, 7)) as run:
            with self.assertRaisesRegex(RuntimeError, 'windows inspect: synthetic failure'):
                self.endpoint.call('windows', 'inspect')
        self.assertEqual(run.call_count, 1)

    def test_endpoint_json_error_is_honored_with_zero_returncode(self):
        with mocked_platform('nt', return_value=response({'error': 'synthetic error'})) as run:
            with self.assertRaisesRegex(RuntimeError, 'windows inspect: synthetic error'):
                self.endpoint.call('windows', 'inspect')
        self.assertEqual(run.call_count, 1)

    def test_windows_endpoint_timeout_is_not_swallowed_or_retried(self):
        failure = subprocess.TimeoutExpired(self.expected_windows_command('inspect'), 47)
        with mocked_platform('nt', side_effect=failure) as run:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.endpoint.call('windows', 'inspect')
        self.assertIs(caught.exception, failure)
        run.assert_called_once_with(self.expected_windows_command('inspect'), capture_output=True,
                                    timeout=47, creationflags=NO_WINDOW)

    def test_wsl_inventory_timeout_prevents_endpoint_execution(self):
        failure = subprocess.TimeoutExpired(LIST_COMMAND, 15)
        with mocked_platform('nt', side_effect=failure) as run, \
             patch.object(sync, 'linux_mount_path') as mount:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.endpoint.call('wsl', 'inspect')
        self.assertIs(caught.exception, failure)
        run.assert_called_once_with(LIST_COMMAND, capture_output=True, timeout=15,
                                    creationflags=NO_WINDOW)
        mount.assert_not_called()

    def test_wsl_endpoint_timeout_is_not_swallowed_or_retried(self):
        failure = subprocess.TimeoutExpired(self.expected_wsl_command('inspect'), 47)
        with mocked_platform('nt', side_effect=[distributions('Synthetic-Ubuntu'), failure]) as run, \
             patch.object(sync, 'linux_mount_path', return_value='/mnt/synthetic/history_endpoint.py'):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.endpoint.call('wsl', 'inspect')
        self.assertIs(caught.exception, failure)
        self.assertEqual(run.call_args_list, [
            call(LIST_COMMAND, capture_output=True, timeout=15, creationflags=NO_WINDOW),
            call(self.expected_wsl_command('inspect'), capture_output=True, timeout=47,
                 creationflags=NO_WINDOW),
        ])


if __name__ == '__main__':
    unittest.main()
