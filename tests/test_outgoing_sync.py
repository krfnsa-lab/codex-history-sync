"""A manual WSL-window request may read WSL history and write only Windows."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from tests.test_history_sync_review import sync, row, view


class OutgoingControllerTests(unittest.TestCase):
    def run_case(self, *, after_exit=False, busy=False, pending=False, dry_run=False):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)
            win,lin=row('outgoing'),row('outgoing')
            ledger=sync.baseline(view(win),view(lin))
            ledger_path=runtime/'ledger.json'
            ledger_path.write_text(json.dumps(ledger))
            if pending:
                (runtime/'pending.json').write_text('{"owner":"earlier-import"}')
            lin=row('outgoing',body='continued',active=busy)
            windows=view(win,row('new-incoming'),row('unknown-pair'))
            windows['backend_detection_ok']=True
            wsl=view(lin,row('unknown-pair'))
            endpoint=MagicMock()
            scans=[0]
            def inspect(side):
                if side=='windows':
                    scans[0]+=1
                    return {**deepcopy(windows),'backend_running':after_exit and scans[0]==1}
                return {**deepcopy(wsl),'backend_running':True}
            endpoint.inspect.side_effect=inspect
            endpoint.backend_status.return_value={'backend_running':False,'backend_detection_ok':True}
            operations=[]
            receipt=row('outgoing',body='native-view')
            def call(side,operation,*args):
                operations.append((side,operation))
                if operation=='import':
                    self.assertEqual(side,'windows')
                    return {'ok':True,'record':receipt,'revision':receipt['revision']}
                if operation=='import-preview':
                    return {'ok':True,'compatible':True}
                return {'ok':True}
            endpoint.call.side_effect=call
            args=SimpleNamespace(initialize=False,dry_run=dry_run,outgoing_only=True)
            before=ledger_path.read_bytes()
            with patch.object(sync,'wsl_running',return_value=True), \
                 patch.object(sync,'Endpoints',return_value=endpoint), \
                 patch.object(sync,'linux_mount_path',side_effect=str), \
                 patch.object(sync,'close_windows_for_choices',return_value=(
                     {'status':'stopped','reason':'windows_exit_completed'} if after_exit else None)):
                if pending:
                    with self.assertRaises(RuntimeError):
                        sync.execute({'wsl_distro':'synthetic'},args,runtime,MagicMock())
                    self.assertEqual(operations,[])
                    self.assertEqual(ledger_path.read_bytes(),before)
                    return
                result=sync.execute({'wsl_distro':'synthetic'},args,runtime,MagicMock())
            self.assertTrue(result['outgoing_only'])
            if busy or dry_run:
                self.assertEqual(operations,[])
                self.assertEqual(ledger_path.read_bytes(),before)
                if dry_run:
                    self.assertEqual([(a['source'],a['target']) for a in result['actions']],[('wsl','windows')])
            else:
                self.assertEqual(result['transferred'],1)
                self.assertEqual(operations,[('wsl','export'),('windows','import-preview'),('windows','import')])
                saved=json.loads(ledger_path.read_text())
                self.assertEqual(set(saved['threads']),{'outgoing'})
                self.assertEqual(saved['threads']['outgoing']['wsl']['revision'],lin['revision'])
            self.assertFalse((runtime/'pending.json').exists())

    def test_only_outgoing_actions_and_checkpoints_run_with_wsl_backend_open(self):
        self.run_case()

    def test_exit_replanning_cannot_introduce_an_incoming_import(self):
        self.run_case(after_exit=True)

    def test_busy_source_is_not_exported_and_incoming_is_still_untouched(self):
        self.run_case(busy=True)

    def test_pending_import_blocks_all_writes(self):
        self.run_case(pending=True)

    def test_dry_run_retains_direction_scope_without_changing_baseline(self):
        self.run_case(dry_run=True)

    def test_unrelated_manual_decisions_and_baseline_pairs_are_not_acknowledged(self):
        plan={'actions':[{'source':'windows','target':'wsl'},{'source':'wsl','target':'windows'}],
              'baseline_pairs':['unrelated'], 'manual_choices':[{'keep_side':'windows'},{'keep_side':'wsl'}],
              'conflict_decisions':[{'source':'windows'},{'source':'wsl'}]}
        updates={'a':{'status':'needs_confirmation'},'b':{'status':'needs_confirmation'}}
        kept=sync.restrict_outgoing_plan(plan,{'choices':{'a':{'keep_side':'windows'},'b':{'keep_side':'wsl'}}},updates)
        self.assertEqual(set(kept),{'b'})
        self.assertEqual(plan['baseline_pairs'],[])
        self.assertEqual(plan['manual_choices'],[{'keep_side':'wsl'}])
        self.assertEqual(plan['conflict_decisions'],[{'source':'wsl'}])


if __name__=='__main__':
    unittest.main()
