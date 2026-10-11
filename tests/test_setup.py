"""The configuration wizard only touches synthetic local profiles."""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('public_profile_setup', ROOT / 'setup.py')
setup = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(ROOT), *sys.path]):
    SPEC.loader.exec_module(setup)


def config(directory, **overrides):
    value = {'windows_home': str(directory / 'synthetic-windows-home'),
             'wsl_distro': 'Synthetic-Ubuntu', 'wsl_user': 'synthetic-user',
             'wsl_home': '/srv/codex-fixture/linux-home/.codex', 'python_windows': sys.executable,
             'runtime_directory': 'state'}
    value.update(overrides)
    return value


def response(value, code=0):
    return SimpleNamespace(returncode=code, stdout=json.dumps(value).encode('utf-8'))


class DiscoveryTests(unittest.TestCase):
    def test_distribution_inventory_decodes_utf16_without_starting_distro(self):
        result = SimpleNamespace(returncode=0, stdout='Synthetic-Ubuntu\r\nOther\r\n'.encode('utf-16'))
        with patch.object(setup.subprocess, 'run', return_value=result) as run:
            self.assertEqual(setup.distribution_names(running=True), ['Synthetic-Ubuntu', 'Other'])
        self.assertEqual(run.call_args.args[0], ['wsl.exe', '--list', '--running', '--quiet'])

    def test_stopped_wsl_probe_does_not_execute_any_wsl_command(self):
        with patch.object(setup, 'distribution_names', return_value=[]), \
             patch.object(setup.subprocess, 'run') as process:
            with self.assertRaisesRegex(RuntimeError, '未运行'):
                setup.probe_wsl('Synthetic-Ubuntu')
        process.assert_not_called()

    def test_running_wsl_probe_only_collects_environment_metadata(self):
        value = {'user': 'synthetic-user', 'home': '/srv/codex-fixture/linux-home',
                 'codex_home': '/srv/codex-fixture/linux-home/.codex',
                 'python': '/usr/bin/python3', 'version': [3, 12, 0]}
        with patch.object(setup, 'distribution_names', return_value=['Synthetic-Ubuntu']), \
             patch.object(setup.subprocess, 'run', return_value=response(value)) as process:
            self.assertEqual(setup.probe_wsl('Synthetic-Ubuntu', 'synthetic-user'), value)
        command = process.call_args.args[0]
        self.assertEqual(command[:5], ['wsl.exe', '--distribution', 'Synthetic-Ubuntu', '--user', 'synthetic-user'])
        self.assertNotIn('read_text', command[-1])
        self.assertNotIn('auth.json', command[-1])
        self.assertNotIn('sqlite3', command[-1])

    def test_old_wsl_python_is_rejected(self):
        value = {'user': 'synthetic-user', 'home': '/srv/codex-fixture/linux-home',
                 'codex_home': '/srv/codex-fixture/linux-home/.codex', 'python': '/usr/bin/python3',
                 'version': [3, 10, 9]}
        with patch.object(setup, 'distribution_names', return_value=['Synthetic-Ubuntu']), \
             patch.object(setup.subprocess, 'run', return_value=response(value)):
            with self.assertRaisesRegex(RuntimeError, '3.11'):
                setup.probe_wsl('Synthetic-Ubuntu')

    def test_malformed_native_home_metadata_is_reported_as_invalid(self):
        value = {'user': 'synthetic-user', 'home': '/srv/codex-fixture/linux-home',
                 'codex_home': 42, 'python': '/usr/bin/python3', 'version': [3, 12, 0]}
        with patch.object(setup, 'distribution_names', return_value=['Synthetic-Ubuntu']), \
             patch.object(setup.subprocess, 'run', return_value=response(value)):
            with self.assertRaises(RuntimeError):
                setup.probe_wsl('Synthetic-Ubuntu')

    def test_windows_probe_rejects_old_python_or_non_cpython(self):
        for implementation, version in (('CPython', [3, 10, 9]), ('PyPy', [3, 12, 0])):
            with self.subTest(implementation=implementation, version=version), \
                 patch.object(setup.subprocess, 'run', return_value=response({
                     'implementation': implementation, 'version': version, 'path': sys.executable})), \
                 self.assertRaises(RuntimeError):
                setup.probe_windows_python(sys.executable)

    def test_windows_probe_accepts_cpython_and_returns_resolved_executable(self):
        value = {'implementation': 'CPython', 'version': [3, 12, 0], 'path': sys.executable}
        with patch.object(setup.subprocess, 'run', return_value=response(value)):
            self.assertEqual(setup.probe_windows_python(sys.executable), str(Path(sys.executable).resolve()))

    def test_multiple_distributions_need_explicit_noninteractive_choice(self):
        with self.assertRaises(ValueError):
            setup.choose_distro(['First', 'Second'], None, False)
        self.assertEqual(setup.choose_distro(['First', 'Second'], 'Second', False), 'Second')

    def test_interactive_distribution_selection_uses_number_and_rejects_bad_number(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(setup.choose_distro(['First', 'Second'], None, True,
                                                input_fn=lambda prompt: '2'), 'Second')
            with self.assertRaises(ValueError):
                setup.choose_distro(['First', 'Second'], None, True, input_fn=lambda prompt: '0')

    def test_positive_integer_rejects_fraction_boolean_and_negative(self):
        for value in (True, 0, -1, '1.5', '3.0', 'text'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                setup.positive_integer(value, 'test')
        self.assertEqual(setup.positive_integer('5', 'test'), 5)


class ProfileProtectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='setup-profile-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root / 'profile' / 'config.json'
        self.original = config(self.root)

    def create_profile(self, marker=None):
        setup.write_profile(self.profile, self.original)
        runtime = setup.resolve_runtime(self.original, self.profile)
        if marker:
            runtime.mkdir(parents=True)
            (runtime / marker).write_text('{}', encoding='utf-8')
        return runtime

    def test_new_profile_only_writes_config_without_creating_runtime(self):
        setup.write_profile(self.profile, self.original)
        self.assertEqual(json.loads(self.profile.read_text(encoding='utf-8')), self.original)
        self.assertEqual([path.name for path in self.root.rglob('*') if path.is_file()], ['config.json'])
        self.assertFalse(setup.resolve_runtime(self.original, self.profile).exists())

    def test_overwrite_requires_explicit_force_and_preserves_existing_bytes(self):
        self.create_profile()
        before = self.profile.read_bytes()
        with self.assertRaises(ValueError):
            setup.write_profile(self.profile, config(self.root, quiet_seconds=90))
        self.assertEqual(self.profile.read_bytes(), before)

    def test_force_cannot_change_initialized_profile_identity(self):
        self.create_profile('ledger.json')
        before = self.profile.read_bytes()
        changes = {'windows_home': str(self.root / 'different-home'), 'wsl_home': '/srv/codex-fixture/other-home/.codex',
                   'wsl_user': 'other-user', 'wsl_distro': 'Other-Ubuntu', 'runtime_directory': 'other-state'}
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                setup.write_profile(self.profile, config(self.root, **{key: value}), force=True)
            self.assertEqual(self.profile.read_bytes(), before)

    def test_force_allows_limits_change_with_same_initialized_identity(self):
        runtime = self.create_profile('ledger.json')
        before = (runtime / 'ledger.json').read_bytes()
        changed = config(self.root, quiet_seconds=90, max_transfers_per_run=3)
        setup.write_profile(self.profile, changed, force=True)
        self.assertEqual(json.loads(self.profile.read_text(encoding='utf-8')), changed)
        self.assertEqual((runtime / 'ledger.json').read_bytes(), before)

    def test_pending_current_import_blocks_force_reconfiguration(self):
        self.create_profile('pending.json')
        before = self.profile.read_bytes()
        with self.assertRaises(ValueError):
            setup.write_profile(self.profile, self.original, force=True)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_new_profile_cannot_adopt_an_existing_used_runtime(self):
        used = self.root / 'used'
        used.mkdir()
        (used / 'ledger.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            setup.write_profile(self.profile, config(self.root, runtime_directory=str(used)))
        self.assertFalse(self.profile.exists())

    def test_force_from_uninitialized_profile_cannot_adopt_another_ledger_or_pending(self):
        self.create_profile()
        before = self.profile.read_bytes()
        for marker in ('ledger.json', 'pending.json'):
            used = self.root / marker.replace('.', '-')
            used.mkdir()
            (used / marker).write_text('{}', encoding='utf-8')
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                setup.write_profile(self.profile, config(self.root, runtime_directory=str(used)), force=True)
            self.assertEqual(self.profile.read_bytes(), before)

    def test_force_preserves_invalid_old_nonobject_config(self):
        self.profile.parent.mkdir(parents=True)
        self.profile.write_bytes(b'[]')
        with self.assertRaises(ValueError):
            setup.write_profile(self.profile, self.original, force=True)
        self.assertEqual(self.profile.read_bytes(), b'[]')


@unittest.skipUnless(os.name == 'nt', 'The setup wizard main entry point requires Windows')
class WizardEntryPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='setup-entry-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root / 'config.json'
        self.native = {'user': 'synthetic-user', 'home': '/srv/codex-fixture/linux-home',
                       'codex_home': '/srv/codex-fixture/linux-home/.codex',
                       'python': '/usr/bin/python3', 'version': [3, 12, 0]}

    def invoke(self, *arguments):
        with redirect_stdout(io.StringIO()):
            return setup.main(['--config', str(self.profile), '--non-interactive', *arguments])

    def test_noninteractive_wizard_writes_only_personal_config(self):
        with patch.object(setup, 'distribution_names', return_value=['Synthetic-Ubuntu']), \
             patch.object(setup, 'probe_wsl', return_value=copy.deepcopy(self.native)), \
             patch.object(setup, 'probe_windows_python', return_value=sys.executable), \
             patch.dict(setup.os.environ, {'CODEX_HOME': str(self.root / 'synthetic-home')}):
            self.assertEqual(self.invoke('--task-name', 'SyntheticTask'), 0)
        actual = json.loads(self.profile.read_text(encoding='utf-8'))
        self.assertEqual(actual['wsl_user'], 'synthetic-user')
        self.assertEqual(actual['runtime_directory'], 'runtime/SyntheticTask')
        self.assertEqual(actual['task_time_limit_minutes'], 10)
        self.assertEqual([path.name for path in self.root.rglob('*') if path.is_file()], ['config.json'])

    def test_stopped_wsl_aborts_before_python_probe_or_config_write(self):
        with patch.object(setup, 'distribution_names', side_effect=[['Synthetic-Ubuntu'], []]), \
             patch.object(setup.subprocess, 'run') as process, \
             patch.object(setup, 'probe_windows_python') as windows_probe:
            self.assertEqual(self.invoke(), 1)
        process.assert_not_called()
        windows_probe.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_extended_soft_budget_derives_sufficient_hard_limit(self):
        with patch.object(setup, 'distribution_names', return_value=['Synthetic-Ubuntu']), \
             patch.object(setup, 'probe_wsl', return_value=copy.deepcopy(self.native)), \
             patch.object(setup, 'probe_windows_python', return_value=sys.executable), \
             patch.dict(setup.os.environ, {'CODEX_HOME': str(self.root / 'synthetic-home')}):
            self.assertEqual(self.invoke('--run-budget-seconds', '601'), 0)
        actual = json.loads(self.profile.read_text(encoding='utf-8'))
        self.assertEqual(actual['task_time_limit_minutes'], 18)
        self.assertFalse((self.root / 'runtime').exists())


if __name__ == '__main__':
    unittest.main()
