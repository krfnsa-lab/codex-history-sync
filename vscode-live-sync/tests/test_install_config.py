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
                self.assertFalse((appdata / 'CodexHistorySyncLive').exists())
                self.assertFalse((windows_apps / 'codex-history-sync-bridge.exe').exists())
                self.assertFalse((profile_dir / 'history_sync.py').exists())

if __name__ == '__main__': unittest.main()
