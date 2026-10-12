import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

preview = module('preview_test', ROOT / 'preview_sync.py')
sync = module('sync_test', ROOT.parent / 'history_sync.py')

def record(revision, body):
    return {'id':'synthetic-thread', 'revision':revision, 'content_revision':body,
            'metadata_revision':'metadata', 'active':False, 'quiet_seconds':120,
            'item_count':3, 'turn_count':1, 'transcript_complete':True, 'transcript_size':100}

class PreviewTests(unittest.TestCase):
    def inputs(self):
        a, b = record('win-before', 'body-before'), record('wsl-before', 'native-body-before')
        ledger = {'version':1, 'threads': {'synthetic-thread': {'windows':a.copy(), 'wsl':b.copy(), 'body_diverged':False}}}
        a['revision'], a['content_revision'] = 'win-after', 'body-after'
        return {'threads':[a]}, {'threads':[b], 'backend_running':True}, ledger

    def test_ready_single_side_update_retains_existing_target(self):
        a,b,ledger = self.inputs()
        candidates = preview.eligible_candidates(sync,a,b,ledger,60)
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['target_revision'],'wsl-before')
        self.assertTrue(b['backend_running'])

    def test_source_active_never_requests_reload(self):
        a,b,ledger = self.inputs(); a['threads'][0]['active']=True
        self.assertEqual(preview.eligible_candidates(sync,a,b,ledger,60),[])

    def test_target_active_never_requests_reload(self):
        a,b,ledger = self.inputs(); b['threads'][0]['active']=True
        self.assertEqual(preview.eligible_candidates(sync,a,b,ledger,60),[])

    def test_divergence_and_two_sided_edits_are_protected(self):
        a,b,ledger = self.inputs(); ledger['threads']['synthetic-thread']['body_diverged']=True
        self.assertEqual(preview.eligible_candidates(sync,a,b,ledger,60),[])
        ledger['threads']['synthetic-thread']['body_diverged']=False
        b['threads'][0]['revision']='wsl-after'
        self.assertEqual(preview.eligible_candidates(sync,a,b,ledger,60),[])

    def test_new_chat_does_not_request_window_reload(self):
        a,b,ledger=self.inputs(); b['threads']=[]; ledger['threads']={}
        self.assertEqual(preview.eligible_candidates(sync,a,b,ledger,60),[])

if __name__=='__main__': unittest.main()
