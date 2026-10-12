import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('builder_test', ROOT / 'build_windows_launcher.py')
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)

@unittest.skipUnless(sys.platform == 'win32', 'Native Windows executable')
class LauncherTests(unittest.TestCase):
    def test_no_activation_env_uses_stable_profile_config_before_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            file = temporary / 'bridge.exe'
            builder.build(file)
            home = temporary / 'synthetic-user'
            config = home / '.codex-history-sync-live/launcher.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'binary': sys.executable}), encoding='utf-8')
            appdata = temporary / 'synthetic-localappdata'
            legacy = appdata / 'CodexHistorySyncLive/launcher.json'
            legacy.parent.mkdir(parents=True)
            # A valid legacy executable would fail if selected instead of the new config.
            legacy.write_text(json.dumps({'binary': str(file)}), encoding='utf-8')
            env = {k:v for k,v in os.environ.items() if not k.startswith('CODEX_HISTORY_SYNC_')}
            env.update(USERPROFILE=str(home), LOCALAPPDATA=str(appdata), PYTHONIOENCODING='utf-8')
            script = 'import pathlib,sys; print(pathlib.Path.home()); print(sys.argv[1]); print(sys.stdin.read()); sys.exit(9)'
            result = subprocess.run([str(file), '-c', script, '测试 stable profile'], input=b'live pipe',
                                    capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 9, result.stderr.decode('utf-8', 'replace'))
            self.assertEqual(result.stdout.decode().splitlines(), [str(home), '测试 stable profile', 'live pipe'])
            self.assertFalse(result.stderr)

    def test_no_activation_env_can_read_legacy_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            file = temporary / 'bridge.exe'
            builder.build(file)
            home = temporary / 'synthetic-user'
            appdata = temporary / 'synthetic-localappdata'
            config = appdata / 'CodexHistorySyncLive/launcher.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'binary': sys.executable}), encoding='utf-8')
            env = {k:v for k,v in os.environ.items() if not k.startswith('CODEX_HISTORY_SYNC_')}
            env.update(USERPROFILE=str(home), LOCALAPPDATA=str(appdata))
            result = subprocess.run([str(file), '-c', 'print("legacy-compatible")'],
                                    capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            self.assertEqual(result.stdout.strip(), b'legacy-compatible')

    def test_stdio_eof_unicode_arguments_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'bridge.exe'
            builder.build(file)
            env = dict(os.environ, CODEX_HISTORY_SYNC_REAL_CLI=sys.executable, PYTHONIOENCODING='utf-8')
            script = 'import sys; print(sys.argv[1]); print(sys.stdin.read()); sys.exit(7)'
            result = subprocess.run([str(file), '-c', script, '测试 argument with spaces'], input=b'pipe data', capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout.decode().splitlines(), ['测试 argument with spaces', 'pipe data'])
            self.assertFalse(result.stderr)

if __name__ == '__main__': unittest.main()
