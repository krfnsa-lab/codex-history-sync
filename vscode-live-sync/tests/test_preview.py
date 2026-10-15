import importlib.util
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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

    def test_two_sided_edits_report_conflict_without_eligible_import(self):
        a,b,ledger=self.inputs(); b['threads'][0]['revision']='wsl-after'
        result=preview.classify_plan(sync,a,b,ledger,60)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'],'chat_conflict')
        self.assertEqual(result['blocked_reason_counts'],{'both_sides_changed':1})
        self.assertEqual(result['conflict_count'],1)
        self.assertNotIn('synthetic-thread',str(result))

    def test_preserved_root_body_difference_is_not_reported_current(self):
        a,b,ledger=self.inputs(); ledger['threads']['synthetic-thread']['body_diverged']=True
        result=preview.classify_plan(sync,a,b,ledger,60)
        self.assertEqual(result['reason'],'chat_conflict')
        self.assertEqual(result['blocked_reason_counts'],{'existing_body_difference':1})

    def test_source_and_target_activity_report_waiting(self):
        for side in ('source','target'):
            a,b,ledger=self.inputs(); (a if side=='source' else b)['threads'][0]['active']=True
            result=preview.classify_plan(sync,a,b,ledger,60)
            self.assertEqual(result['reason'],'chat_busy')
            self.assertEqual(result['blocked_reason_counts'],{side+'_busy':1})

    def test_old_unreadable_chat_does_not_mask_known_incoming_conflict(self):
        a,b,ledger=self.inputs(); b['threads'][0]['revision']='wsl-after'
        old=record('old-revision','old-body'); old['id']='unrelated-old'; old['transcript_complete']=False
        a['threads'].append(old); b['threads'].append(old.copy())
        ledger['threads']['unrelated-old']={'windows':old.copy(),'wsl':old.copy(),'body_diverged':True}
        result=preview.classify_plan(sync,a,b,ledger,60)
        self.assertEqual(result['blocked_reason_counts'],{'both_sides_changed':1})
        self.assertEqual(result['ignored_blocked_count'],1)

    def test_changed_unreadable_incoming_chat_reports_blocked(self):
        a,b,ledger=self.inputs(); a['threads'][0]['transcript_complete']=False
        result=preview.classify_plan(sync,a,b,ledger,60)
        self.assertEqual(result['reason'],'incoming_update_blocked')
        self.assertEqual(result['blocked_reason_counts'],{'unreadable_transcript':1})

    def test_new_and_opposite_direction_changes_are_not_called_current(self):
        a,b,ledger=self.inputs(); b['threads']=[]; ledger['threads']={}
        result=preview.classify_plan(sync,a,b,ledger,60)
        self.assertEqual(result['reason'],'new_chats_pending')
        self.assertEqual(result['new_chat_count'],1)
        a,b,ledger=self.inputs(); a['threads'][0]=ledger['threads']['synthetic-thread']['windows'].copy()
        b['threads'][0]['revision']='wsl-after'; b['threads'][0]['content_revision']='wsl-body-after'
        a['backend_running']=False
        result=preview.classify_plan(sync,a,b,ledger,60)
        self.assertEqual(result['reason'],'opposite_direction_pending')

    def test_unchanged_pair_has_no_pending_updates(self):
        a,b,ledger=self.inputs(); a['threads'][0]=ledger['threads']['synthetic-thread']['windows'].copy()
        result=preview.classify_plan(sync,a,b,ledger,60)
        self.assertEqual(result['reason'],'no_pending_updates')

    def longer_inputs(self, winning_side):
        a,b,ledger=self.inputs()
        b['threads'][0].update(revision='wsl-after',content_revision='wsl-after-body')
        a['backend_running']=True
        for side,view in [('windows',a),('wsl',b)]:
            view['threads'][0].update(turn_count=5 if side==winning_side else 3,
                                     item_count=8 if side==winning_side else 20)
        return a,b,ledger

    def test_longer_windows_version_can_use_normal_pause_candidate(self):
        a,b,ledger=self.longer_inputs('windows')
        result=preview.classify_plan(sync,a,b,ledger,60,'prefer_longer')
        self.assertTrue(result['eligible'])
        self.assertEqual(result['conflict_count'],0)
        self.assertEqual(result['conflict_decisions'][0]['source'],'windows')
        self.assertTrue(b['backend_running'])

    def test_longer_wsl_version_waits_for_windows_backend_without_reload_candidate(self):
        a,b,ledger=self.longer_inputs('wsl')
        result=preview.classify_plan(sync,a,b,ledger,60,'prefer_longer')
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'],'longer_wsl_pending')
        self.assertEqual(result['conflict_count'],0)
        self.assertEqual(result['opposite_direction_count'],1)
        self.assertEqual(result['conflict_decisions'][0]['source'],'wsl')

    def test_equal_counts_under_prefer_longer_remain_a_conflict(self):
        a,b,ledger=self.longer_inputs('windows')
        b['threads'][0].update(turn_count=5,item_count=8)
        result=preview.classify_plan(sync,a,b,ledger,60,'prefer_longer')
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'],'chat_conflict')
        self.assertEqual(result['blocked_reason_counts'],{'conflict_counts_equal':1})

    def local_inputs(self):
        a,b,ledger=self.inputs()
        b['threads'][0]['revision']='wsl-settings-after'
        b['threads'][0]['content_revision']='wsl-settings-body'
        b.update(backend_running=False,backend_detection_ok=True,backend_count=0)
        return a,b,ledger

    def local_proof(self, b):
        return {'verified':True,'reason':'local_settings_only',
                'record':deepcopy(b['threads'][0]),'settings_count':2}

    def test_virtual_local_settings_ack_preserves_disk_ledger_and_source_baseline(self):
        a,b,ledger=self.local_inputs(); original=deepcopy(ledger)
        virtual,proofs=preview.virtual_local_settings_ledger(sync,a,b,ledger,60,lambda *_: self.local_proof(b))
        self.assertEqual(ledger,original)
        self.assertEqual(virtual['threads']['synthetic-thread']['windows'],original['threads']['synthetic-thread']['windows'])
        self.assertEqual(virtual['threads']['synthetic-thread']['wsl']['revision'],'wsl-settings-after')
        self.assertEqual(list(proofs),['synthetic-thread'])
        planned=preview.eligible_candidates(sync,a,b,virtual,60)
        self.assertEqual(planned[0]['source_revision'],'win-after')
        self.assertEqual(planned[0]['target_revision'],'wsl-settings-after')

    def test_busy_source_divergence_failed_proof_and_metadata_only_never_ack(self):
        for failure in ('busy','diverged','failed_proof','stale_target','metadata_only','invalid_proof'):
            a,b,ledger=self.local_inputs(); proof=self.local_proof(b)
            if failure=='busy': a['threads'][0]['active']=True
            if failure=='diverged': ledger['threads']['synthetic-thread']['body_diverged']=True
            if failure=='failed_proof': proof['verified']=False
            if failure=='stale_target': proof['record']['revision']='other-target'
            if failure=='metadata_only': a['threads'][0]['content_revision']=ledger['threads']['synthetic-thread']['windows']['content_revision']
            if failure=='invalid_proof': proof=None
            virtual,proofs=preview.virtual_local_settings_ledger(sync,a,b,ledger,60,lambda *_: proof)
            self.assertEqual(virtual,ledger,failure)
            self.assertEqual(proofs,{},failure)

    def fake_endpoint(self,a,b,statuses=None):
        calls=[]; statuses=list(statuses or [])
        def inspect(side):
            calls.append('inspect-'+side)
            return deepcopy(a if side=='windows' else b)
        def backend_status(side):
            calls.append('backend-'+side)
            return statuses.pop(0) if statuses else {'backend_running':False,'backend_count':0,'backend_detection_ok':True}
        return SimpleNamespace(inspect=inspect,backend_status=backend_status),calls

    def test_durable_ack_backs_up_ledger_and_receipt_before_target_only_write(self):
        a,b,ledger=self.local_inputs(); endpoint,calls=self.fake_endpoint(a,b)
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder); sync.atomic_json(runtime/'ledger.json',ledger)
            writes=[]; atomic=sync.atomic_json
            def track(file,value):
                writes.append(str(file.relative_to(runtime)))
                atomic(file,value)
            with patch.object(sync,'atomic_json',track):
                result=preview.acknowledge_local_settings(sync,endpoint,{},ROOT.parent/'history_sync.py',runtime,lambda *_: self.local_proof(b))
            self.assertEqual(result,['synthetic-thread'])
            updated=json.loads((runtime/'ledger.json').read_text(encoding='utf-8'))
            old=ledger['threads']['synthetic-thread']
            self.assertEqual(updated['threads']['synthetic-thread']['windows'],old['windows'])
            self.assertEqual(updated['threads']['synthetic-thread']['body_diverged'],old['body_diverged'])
            self.assertEqual(writes[2],'ledger.json')
            self.assertTrue(writes[0].endswith('ledger-before.json'))
            self.assertTrue(writes[1].endswith('receipt.json'))
            backup=next((runtime/'local-settings-ack').iterdir())
            self.assertEqual(json.loads((backup/'ledger-before.json').read_text()),ledger)
            receipt=json.loads((backup/'receipt.json').read_text())
            self.assertEqual(receipt['status'],'acknowledged')
            self.assertEqual(receipt['ledger_backup_sha256'],hashlib.sha256((backup/'ledger-before.json').read_bytes()).hexdigest())
            self.assertEqual(receipt['acknowledgements'][0]['preserved_source_baseline_revision'],'win-before')
            self.assertEqual(calls.count('inspect-wsl'),3)

    def test_double_native_proof_rejects_source_or_target_race_without_writes(self):
        for race in ('source','target'):
            a,b,ledger=self.local_inputs(); endpoint,_=self.fake_endpoint(a,b); count=0
            def verify(*_):
                nonlocal count
                count+=1
                if count==1:
                    result=self.local_proof(b)
                    if race=='source': a['threads'][0]['revision']='source-raced'
                    else: b['threads'][0]['revision']='target-raced'
                    return result
                return self.local_proof(b)
            with tempfile.TemporaryDirectory() as folder:
                runtime=Path(folder); sync.atomic_json(runtime/'ledger.json',ledger)
                original=(runtime/'ledger.json').read_bytes()
                with self.assertRaisesRegex(RuntimeError,'changed during'):
                    preview.acknowledge_local_settings(sync,endpoint,{},ROOT.parent/'history_sync.py',runtime,verify)
                self.assertEqual((runtime/'ledger.json').read_bytes(),original)
                self.assertFalse((runtime/'local-settings-ack').exists())

    def test_live_backend_unknown_detection_restart_and_pending_never_ack(self):
        for condition in ('live','unknown','restart','pending'):
            a,b,ledger=self.local_inputs()
            statuses=None
            if condition=='live': b['backend_running']=True; b['backend_count']=1
            if condition=='unknown': b['backend_detection_ok']=False
            if condition=='restart':
                statuses=[{'backend_running':False,'backend_detection_ok':True,'backend_count':0},
                          {'backend_running':True,'backend_detection_ok':True,'backend_count':1}]
            endpoint,_=self.fake_endpoint(a,b,statuses)
            with tempfile.TemporaryDirectory() as folder:
                runtime=Path(folder); sync.atomic_json(runtime/'ledger.json',ledger)
                original=(runtime/'ledger.json').read_bytes()
                if condition=='pending': (runtime/'pending.json').write_text('{}')
                with self.assertRaises(RuntimeError):
                    preview.acknowledge_local_settings(sync,endpoint,{},ROOT.parent/'history_sync.py',runtime,lambda *_: self.local_proof(b))
                self.assertEqual((runtime/'ledger.json').read_bytes(),original)
                self.assertFalse((runtime/'local-settings-ack').exists())

    def test_source_becoming_busy_after_backup_preserves_original_ledger(self):
        a,b,ledger=self.local_inputs(); endpoint,_=self.fake_endpoint(a,b)
        atomic=sync.atomic_json
        def create_backup(file,value):
            atomic(file,value)
            if file.name=='receipt.json': a['threads'][0]['active']=True
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder); atomic(runtime/'ledger.json',ledger)
            before=(runtime/'ledger.json').read_bytes()
            with patch.object(sync,'atomic_json',create_backup), self.assertRaisesRegex(RuntimeError,'became busy'):
                preview.acknowledge_local_settings(sync,endpoint,{},ROOT.parent/'history_sync.py',runtime,lambda *_: self.local_proof(b))
            self.assertEqual((runtime/'ledger.json').read_bytes(),before)
            backup=next((runtime/'local-settings-ack').iterdir())
            self.assertEqual(json.loads((backup/'receipt.json').read_text())['status'],'prepared')

    def test_sync_once_keeps_same_run_lock_through_ack_execute_and_report(self):
        calls=[]; held=False
        @contextmanager
        def lock(_):
            nonlocal held
            held=True; calls.append('lock')
            try: yield True
            finally: held=False; calls.append('unlock')
        def ack(*_):
            self.assertTrue(held); calls.append('ack'); return ['synthetic-thread']
        def execute(config,args,runtime,log):
            self.assertTrue(held)
            self.assertFalse(args.initialize); self.assertFalse(args.dry_run)
            calls.append('execute'); return {'status':'completed','transferred':1}
        def report(*_): self.assertTrue(held); calls.append('report')
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder); (runtime/'ledger.json').write_text('{}')
            config={'wsl_distro':'synthetic'}
            controller=ROOT.parent/'history_sync.py'
            with patch.object(preview.os,'name','nt'), patch.object(preview,'load_controller',return_value=(controller,sync,config,runtime)), patch.object(sync,'wsl_running',return_value=True), patch.object(sync,'run_lock',lock), patch.object(preview,'acknowledge_local_settings',ack), patch.object(sync,'execute',execute), patch.object(sync,'save_run_report',report):
                result=preview.sync_once(controller,controller)
            self.assertEqual(calls,['lock','ack','execute','report','unlock'])
            self.assertEqual(result['local_settings_acknowledged'],1)

    def test_sync_once_lock_collision_never_acknowledges_or_imports(self):
        @contextmanager
        def busy(_): yield False
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder); (runtime/'ledger.json').write_text('{}')
            controller=ROOT.parent/'history_sync.py'
            with patch.object(preview.os,'name','nt'), patch.object(preview,'load_controller',return_value=(controller,sync,{'wsl_distro':'synthetic'},runtime)), patch.object(sync,'wsl_running',return_value=True), patch.object(sync,'run_lock',busy), patch.object(preview,'acknowledge_local_settings') as ack, patch.object(sync,'execute') as execute:
                result=preview.sync_once(controller,controller)
                ack.assert_not_called(); execute.assert_not_called()
            self.assertEqual(result['status'],'skipped_already_running')

class ManualChoicePreview(unittest.TestCase):
    def inputs(self, runtime, keep='windows'):
        win, lin = record('win-selected', 'win-body'), record('wsl-selected', 'wsl-body')
        choice_id = '30000000-0000-4000-8000-000000000001'
        choice = {'id': win['id'], 'choice_id': choice_id, 'keep_side': keep, 'status': 'pending',
                  'expected': {'windows': win['revision'], 'wsl': lin['revision']}, 'package_hashes': {}}
        for side, row in (('windows', win), ('wsl', lin)):
            folder = runtime / 'conflict-choices' / choice_id / side
            folder.mkdir(parents=True)
            sync.atomic_json(folder / 'meta.json', {'thread': {'id': win['id']}, 'source_revision': row})
            (folder / 'history.sqlite').write_bytes(b'synthetic cache')
            (folder / 'transcript.jsonl').write_bytes(b'synthetic transcript')
            choice['package_hashes'][side] = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                             for name in ('meta.json', 'history.sqlite', 'transcript.jsonl')}
        sync.atomic_json(runtime / 'conflict-choices.json', {'version': 1, 'choices': {win['id']: choice}})
        ledger = {'version': 1, 'threads': {win['id']: {'windows': deepcopy(win), 'wsl': deepcopy(lin), 'body_diverged': True}}}
        views = {'windows': {'threads': [win], 'backend_running': True},
                 'wsl': {'threads': [lin], 'backend_running': True}}
        return views, ledger, choice

    def test_windows_choice_admits_pause_without_mutating_selection_or_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            views, ledger, _ = self.inputs(runtime)
            before = (runtime / 'conflict-choices.json').read_bytes(), deepcopy(ledger)
            result = preview.classify_plan(sync, views['windows'], views['wsl'], ledger, 60, runtime=runtime)
            self.assertTrue(result['eligible'])
            self.assertEqual(result['reason'], 'existing_replica_updates')
            self.assertEqual(result['conflict_count'], 0)
            self.assertEqual(before, ((runtime / 'conflict-choices.json').read_bytes(), ledger))
            self.assertTrue(views['wsl']['backend_running'])

    def test_wsl_choice_waits_for_desktop_without_pausing_wsl(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            views, ledger, _ = self.inputs(runtime, 'wsl')
            result = preview.classify_plan(sync, views['windows'], views['wsl'], ledger, 60, runtime=runtime)
            self.assertFalse(result['eligible'])
            self.assertEqual(result['opposite_direction_count'], 1)
            self.assertEqual(result['blocked_reason_counts'], {'target_backend_running': 1})

    def test_either_side_edit_requires_fresh_choice_and_never_admits_pause(self):
        for side in ('windows', 'wsl'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as folder:
                runtime = Path(folder)
                views, ledger, _ = self.inputs(runtime)
                views[side]['threads'][0]['revision'] = 'continued-after-confirmation'
                before = (runtime / 'conflict-choices.json').read_bytes()
                result = preview.classify_plan(sync, views['windows'], views['wsl'], ledger, 60, runtime=runtime)
                self.assertFalse(result['eligible'])
                self.assertEqual(result['reason'], 'chat_conflict')
                self.assertEqual(result['blocked_reason_counts'], {'manual_choice_stale': 1})
                self.assertEqual((runtime / 'conflict-choices.json').read_bytes(), before)

    def test_settings_proof_cannot_advance_a_human_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            views, ledger, _ = self.inputs(runtime)
            views['wsl']['threads'][0]['revision'] = 'later-local-setting'
            with patch.object(preview, 'call_native_probe', side_effect=AssertionError('Must not acknowledge a bound choice')):
                virtual, proofs = preview.virtual_local_settings_ledger(sync, views['windows'], views['wsl'], ledger,
                    60, lambda *_: (_ for _ in ()).throw(AssertionError('Must not prove bound choice')), runtime=runtime)
            self.assertEqual(virtual, ledger)
            self.assertEqual(proofs, {})

    def test_tampered_backup_fails_before_a_reload_is_proposed(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            views, ledger, choice = self.inputs(runtime)
            (runtime / 'conflict-choices' / choice['choice_id'] / 'wsl' / 'transcript.jsonl').write_bytes(b'tampered')
            with self.assertRaisesRegex(RuntimeError, 'backup changed'):
                preview.classify_plan(sync, views['windows'], views['wsl'], ledger, 60, runtime=runtime)

class BusyControllerPreview(unittest.TestCase):
    def test_running_task_prevents_scanning_or_proposing_backend_pause(self):
        fake = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0,
                               locking=lambda *args: (_ for _ in ()).throw(OSError('busy')))
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            (runtime / 'ledger.json').write_text('{}')
            (runtime / 'run.lock').write_bytes(b'0')
            controller = ROOT.parent / 'history_sync.py'
            with patch.object(preview.os, 'name', 'nt'), patch.dict(sys.modules, {'msvcrt': fake}), \
                 patch.object(preview, 'load_controller', return_value=(controller, sync, {'wsl_distro': 'synthetic'}, runtime)), \
                 patch.object(sync, 'wsl_running', return_value=True), \
                 patch.object(sync, 'Endpoints', side_effect=AssertionError('Busy task must not be scanned')):
                result = preview.probe(controller, controller)
            self.assertFalse(result['eligible'])
            self.assertEqual(result['reason'], 'controller_running')


if __name__=='__main__': unittest.main()
