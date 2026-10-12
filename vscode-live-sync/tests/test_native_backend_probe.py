"""A known worker is one backend; independent or uncertain processes still block."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

native = load('backend_native_test', ROOT / 'native_backend_probe.py')
preview = load('backend_preview_test', ROOT / 'preview_sync.py')

def records():
    server = dict(pid=100,ppid=99,group=100,session=100,start=1000,uid=7,
                  exe='/opt/synthetic/codex',app_server=True)
    helper = dict(pid=102,ppid=100,group=102,session=100,start=1010,uid=7,
                  exe='/opt/synthetic/codex-code-mode-host',app_server=False)
    return [server,helper]

class FamilyTests(unittest.TestCase):
    def test_known_helper_in_its_own_group_is_not_a_second_backend(self):
        result = native.classify(records(),uid=7)
        self.assertTrue(result['verified_family'])
        self.assertEqual(result['app_server_count'],1)
        self.assertEqual(result['helper_count'],1)
        self.assertEqual(result['app_server_pid'],100)
        self.assertNotIn('/opt',str(result))

    def test_second_server_and_terminal_are_independent(self):
        for app_server in (True,False):
            rows=records(); extra=deepcopy(rows[0]);extra.update(pid=200,group=200,session=200,app_server=app_server)
            self.assertFalse(native.classify(rows+[extra],uid=7)['verified_family'])

    def test_unrelated_worker_executable_session_parent_or_user_is_rejected(self):
        for change in ({'exe':'/opt/other/codex-code-mode-host'}, {'exe':'/opt/synthetic/codex-unknown'},
                       {'ppid':2}, {'session':200}, {'uid':9}, {'app_server':True}):
            rows=records();rows[1].update(change)
            self.assertFalse(native.classify(rows,uid=7)['verified_family'],change)

    def test_unowned_server_session_or_user_is_rejected(self):
        for change in ({'session':50},{'group':50},{'uid':9}):
            rows=records();rows[0].update(change)
            self.assertFalse(native.classify(rows,uid=7)['verified_family'])

    def test_process_change_and_scan_error_do_not_relax_guards(self):
        with patch.object(native,'snapshot',side_effect=[records(),records()[:1]]):
            self.assertEqual(native.probe()['reason'],'processes_changed')
        with patch.object(native,'snapshot',side_effect=PermissionError('synthetic')):
            self.assertEqual(native.probe()['reason'],'process_scan_failed')

    def test_native_count_must_match_two_stable_family_snapshots(self):
        family=native.classify(records(),uid=7)
        self.assertTrue(preview.accepted_backend_family({'backend_count':2},family))
        for count in (None,True,1,3):
            self.assertFalse(preview.accepted_backend_family({'backend_count':count},family))
        self.assertFalse(preview.accepted_backend_family({'backend_count':2},{'verified_family':False}))

if __name__ == '__main__': unittest.main()
