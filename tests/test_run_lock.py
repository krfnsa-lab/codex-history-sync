"""Windows native lock contention must defer without reading a locked byte."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import history_sync as sync


@unittest.skipUnless(os.name == 'nt', 'Native Windows byte-range locking')
class WindowsRunLock(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'run.lock'

    def tearDown(self):
        self.directory.cleanup()

    def test_two_real_processes_defer_contention_then_reacquire(self):
        code = '''
import json,sys
from pathlib import Path
from history_sync import run_lock
with run_lock(Path(sys.argv[1])) as acquired:
 print(json.dumps({'acquired':acquired}),flush=True)
 if sys.argv[2]=='hold' and acquired:
  Path(sys.argv[3]).write_text('ready',encoding='utf-8')
  sys.stdin.readline()
'''
        ready_path = self.path.with_name('holder-ready.txt')
        holder = subprocess.Popen([sys.executable, '-u', '-c', code, str(self.path), 'hold', str(ready_path)],
            cwd=sync.SCRIPT_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            # The holder writes a separate readiness marker only after taking
            # the lock. Bound startup waiting so a failing child cannot hang CI.
            deadline = time.monotonic() + 15
            while not ready_path.exists() and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready_path.exists(), 'native holder did not acquire its lock')
            contender = subprocess.run([sys.executable, '-u', '-c', code, str(self.path), 'try'],
                cwd=sync.SCRIPT_DIR, capture_output=True, text=True, timeout=15)
            self.assertEqual(contender.returncode, 0, contender.stderr)
            self.assertEqual(json.loads(contender.stdout), {'acquired': False})
            held_output, held_error = holder.communicate('\n', timeout=15)
            self.assertEqual(holder.returncode, 0)
            self.assertEqual(json.loads(held_output), {'acquired': True}, held_error)
            self.assertEqual(self.path.read_bytes(), b'0')
            after = subprocess.run([sys.executable, '-u', '-c', code, str(self.path), 'try'],
                cwd=sync.SCRIPT_DIR, capture_output=True, text=True, timeout=15)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(json.loads(after.stdout), {'acquired': True})
            self.assertEqual(self.path.read_bytes(), b'0')
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.communicate(timeout=15)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                if stream is not None:
                    stream.close()

    def test_unexpected_native_lock_failure_propagates(self):
        import msvcrt
        with patch.object(msvcrt, 'locking', side_effect=OSError(errno.EBADF, 'invalid handle')):
            with self.assertRaises(OSError) as error:
                with sync.run_lock(self.path):
                    self.fail('invalid handle is not lock contention')
        self.assertEqual(error.exception.errno, errno.EBADF)

    def test_open_permission_failure_propagates(self):
        with patch.object(Path, 'open', side_effect=PermissionError(errno.EACCES, 'read-only directory')):
            with self.assertRaises(PermissionError):
                with sync.run_lock(self.path):
                    self.fail('inaccessible file is not lock contention')

    def test_explicit_native_access_denied_is_not_lock_contention(self):
        import msvcrt
        error = PermissionError(errno.EACCES, 'access denied')
        error.winerror = 5
        with patch.object(msvcrt, 'locking', side_effect=error):
            with self.assertRaises(PermissionError):
                with sync.run_lock(self.path):
                    self.fail('access denied is not lock contention')


if __name__ == '__main__':
    unittest.main()
