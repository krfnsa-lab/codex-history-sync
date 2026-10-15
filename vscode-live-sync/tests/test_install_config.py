"""Portable installer previews must not register extensions or write profiles."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    spec = importlib.util.spec_from_file_location('bridge_install_test', ROOT / 'install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
finally:
    sys.path.remove(str(ROOT))

@unittest.skipUnless(sys.platform == 'win32', 'Native Windows profile resolver')
class InstallerConfiguration(unittest.TestCase):
    def test_external_profile_auto_and_relative_interpreters_are_preview_only(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder).resolve()
            fake_home = temporary / 'example-user'
            binary = fake_home / '.vscode/extensions/openai.chatgpt-synthetic/bin/windows-x86_64/codex.exe'
            binary.parent.mkdir(parents=True); binary.write_bytes(b'synthetic')
            appdata = temporary / 'appdata'
            windows_apps = appdata / 'Microsoft/WindowsApps'
            windows_apps.mkdir(parents=True)
            profile_dir = temporary / 'external-profile'
            profile_dir.mkdir()
            config = profile_dir / 'config.json'
            for python in ('auto', os.path.relpath(sys.executable, profile_dir)):
                config.write_text(json.dumps({'windows_home': '%BRIDGE_TEST_HOME%', 'wsl_distro':'Synthetic',
                    'wsl_user':'example-user', 'wsl_home':'/home/example-user/.codex',
                    'python_windows':python, 'python_wsl':'/usr/bin/python3'}))
                env = {'LOCALAPPDATA':str(appdata), 'PATH':str(windows_apps), 'BRIDGE_TEST_HOME':str(temporary)}
                with patch.dict(os.environ, env), patch.object(Path, 'home', return_value=fake_home), \
                     patch.object(installer.shutil, 'which', side_effect=lambda name: 'C:\\Tools\\code.cmd' if name == 'code.cmd' else None), \
                     patch.object(installer, 'run', side_effect=AssertionError('Preview launched a process')), \
                     contextlib.redirect_stdout(io.StringIO()) as captured:
                    installer.install(config, dry_run=True)
                plan = json.loads(captured.getvalue())
                self.assertFalse(plan['automatic_sync_enabled'])
                self.assertEqual(plan['windows_install'], str(fake_home / '.codex-history-sync-live'))
                self.assertFalse((fake_home / '.codex-history-sync-live').exists())
                self.assertFalse((appdata / 'CodexHistorySyncLive').exists())
                self.assertFalse((windows_apps / 'codex-history-sync-bridge.exe').exists())
                self.assertFalse((profile_dir / 'history_sync.py').exists())

    def test_legacy_owner_allows_preview_upgrade_but_unrelated_new_manifest_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder).resolve()
            fake_home = temporary / 'example-user'
            binary = fake_home / '.vscode/extensions/openai.chatgpt-synthetic/bin/windows-x86_64/codex.exe'
            binary.parent.mkdir(parents=True); binary.write_bytes(b'synthetic')
            appdata = temporary / 'appdata'
            windows_apps = appdata / 'Microsoft/WindowsApps'
            windows_apps.mkdir(parents=True)
            (windows_apps / 'codex-history-sync-bridge.exe').write_bytes(b'owned-legacy-wrapper')
            legacy = appdata / 'CodexHistorySyncLive/launcher.json'
            legacy.parent.mkdir(); legacy.write_text(json.dumps({'owner': installer.OWNER}))
            config = temporary / 'config.json'
            config.write_text(json.dumps({'windows_home':str(temporary), 'wsl_distro':'Synthetic',
                'wsl_user':'example-user', 'wsl_home':'/home/example-user/.codex',
                'python_windows':sys.executable, 'python_wsl':'/usr/bin/python3'}))
            env = {'LOCALAPPDATA':str(appdata), 'PATH':str(windows_apps)}
            with patch.dict(os.environ, env), patch.object(Path, 'home', return_value=fake_home), \
                 patch.object(installer.shutil, 'which', return_value='C:\\Tools\\code.cmd'), \
                 patch.object(installer, 'run', side_effect=AssertionError('Preview launched a process')), \
                 contextlib.redirect_stdout(io.StringIO()) as captured:
                installer.install(config, dry_run=True)
                new_manifest = fake_home / '.codex-history-sync-live/launcher.json'
                new_manifest.parent.mkdir(); new_manifest.write_text(json.dumps({'owner': 'someone-else'}))
                with self.assertRaisesRegex(RuntimeError, 'unrelated Windows launcher'):
                    installer.install(config, dry_run=True)
            self.assertEqual(json.loads(captured.getvalue())['windows_install'], str(new_manifest.parent))
            self.assertEqual(json.loads(legacy.read_text())['owner'], installer.OWNER)

    def test_malformed_manifest_is_not_owned(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / 'launcher.json'
            for contents in ('invalid-json', 'null', '[]', '{"owner":"unrelated"}'):
                manifest.write_text(contents)
                self.assertFalse(installer.owned_manifest(manifest))

class AtomicUpgrade(unittest.TestCase):
    def test_failed_replace_preserves_previous_helper_and_cleans_owned_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'new.py', root / 'installed.py'
            source.write_text('new')
            target.write_text('old')
            with patch.object(installer.os, 'replace', side_effect=PermissionError('synthetic busy file')):
                with self.assertRaises(PermissionError): installer.atomic_copy(source, target)
            self.assertEqual(target.read_text(), 'old')
            self.assertEqual(list(root.glob('*.install-*')), [])

    def test_equivalent_launcher_keeps_running_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'new.exe', root / 'installed.exe'
            for path, date in ((source, (2026, 1, 1, 0, 0, 0)), (target, (2025, 1, 1, 0, 0, 0))):
                path.write_bytes(b'launcher/interpreter-prefix')
                with zipfile.ZipFile(path, 'a') as archive:
                    archive.writestr(zipfile.ZipInfo('__main__.py', date), 'same script')
            before = target.read_bytes()
            with patch.object(installer.os, 'replace', side_effect=AssertionError('Running equivalent executable was replaced')):
                installer.atomic_copy(source, target, launcher=True)
            self.assertEqual(target.read_bytes(), before)

    def test_changed_launcher_script_is_not_equivalent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'new.exe', root / 'installed.exe'
            for path, script in ((source, 'new script'), (target, 'old script')):
                path.write_bytes(b'same prefix')
                with zipfile.ZipFile(path, 'a') as archive: archive.writestr('__main__.py', script)
            self.assertFalse(installer.equivalent_launcher(source, target))
            installer.atomic_copy(source, target, launcher=True)
            self.assertEqual(target.read_bytes(), source.read_bytes())


if __name__ == '__main__': unittest.main()
