import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preview_recovery', ROOT / 'preview_sync.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)

@unittest.skipUnless(os.name == 'nt', 'Requires native Windows locking')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runtime = Path(self.folder.name)
        (self.runtime / 'run.lock').write_bytes(b'0')
        self.sync = SimpleNamespace(wsl_running=lambda _: True, decode_output=lambda b: b.decode())
        self.config = {'wsl_distro': 'Synthetic', 'wsl_user': 'test'}
        self.load = patch.object(preview, 'load_controller', return_value=(ROOT, self.sync, self.config, self.runtime))
        self.load.start(); self.addCleanup(self.load.stop)

    def test_unfinished_import_blocks_without_endpoint_scan(self):
        (self.runtime / 'pending.json').write_text('{}')
        with patch.object(preview.subprocess, 'run') as execute:
            self.assertFalse(preview.recovery_state(ROOT, ROOT)['safe_to_reload'])
        execute.assert_not_called()

    def test_native_writer_lock_blocks_reload(self):
        import msvcrt
        with (self.runtime / 'run.lock').open('r+b') as stream:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                self.assertEqual(preview.recovery_state(ROOT, ROOT)['reason'], 'controller_running')
            finally:
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def test_orphan_endpoint_blocks_reload(self):
        result = subprocess.CompletedProcess([], 0, b'{"safe":false}', b'')
        with patch.object(preview.subprocess, 'run', return_value=result):
            self.assertFalse(preview.recovery_state(ROOT, ROOT)['safe_to_reload'])

    def test_no_writer_allows_reload(self):
        result = subprocess.CompletedProcess([], 0, b'{"safe":true}', b'')
        with patch.object(preview.subprocess, 'run', return_value=result):
            self.assertTrue(preview.recovery_state(ROOT, ROOT)['safe_to_reload'])

    def test_scan_failure_and_unknown_output_never_allow_reload(self):
        with patch.object(preview.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'')):
            self.assertFalse(preview.recovery_state(ROOT, ROOT)['safe_to_reload'])
        with patch.object(preview.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'unknown', b'')):
            with self.assertRaises(ValueError): preview.recovery_state(ROOT, ROOT)

if __name__ == '__main__': unittest.main()
