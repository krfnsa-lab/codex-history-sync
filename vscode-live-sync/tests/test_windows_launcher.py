import importlib.util
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
