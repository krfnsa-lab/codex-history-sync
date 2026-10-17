"""Opt-in Windows history viewing, using isolated synthetic native databases."""
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import test_workspace_paths as fixtures

ep, sync, ID = fixtures.ep, fixtures.sync, fixtures.ID


class HistoryViewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorkspaceImportTests('test_import_maps_restore_fields_and_preserves_message_bytes')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        for index in (0, 1):
            record = json.loads(self.f.lines[index])
            record['payload'].update(cwd='/home/example-user/project',
                                     workspace_roots=['/home/example-user/project'],
                                     runtime_workspace_roots=['/home/example-user/project'],
                                     sandbox_policy={'type':'workspace-write','writable_roots':['/home/example-user/project']})
            self.f.lines[index] = fixtures.line(record)
        self.f.set_transcript(self.f.source, self.f.lines)
        with fixtures.database(self.f.source / 'state_5.sqlite') as db:
            db.execute('UPDATE threads SET cwd=?', ('/home/example-user/project',))
        self.source = self.f.export_from(platform='linux')

    def args(self, **kwargs):
        return self.f.args(self.f.target, self.f.package, history_view_only=True, expected='absent', **kwargs)

    def test_preview_is_read_only_and_default_still_rejects_unmappable_cwd(self):
        args = self.args()
        with mock.patch.object(ep, 'native_platform', return_value='windows'):
            result = ep.import_preview(args)
            self.assertTrue(result['compatible'])
            self.assertTrue(result['history_view_only'])
            self.assertFalse((self.f.target / '.history-sync-view').exists())
            args.history_view_only = False
            self.assertEqual(ep.import_preview(args)['reason'], 'workspace_cwd_unmappable')

    def test_full_import_preserves_messages_cache_original_package_and_safe_environment(self):
        with mock.patch.object(ep, 'native_platform', return_value='windows'):
            result = ep.import_chat(self.args())
        lines = self.f.target_lines()
        workspace = self.f.target / '.history-sync-view' / ID
        self.assertTrue(workspace.is_dir())
        self.assertEqual(list(workspace.iterdir()), [])
        for data in lines[:2]:
            payload = json.loads(data)['payload']
            self.assertEqual(payload['cwd'], str(workspace))
            self.assertEqual(payload['workspace_roots'], [])
            self.assertEqual(payload['runtime_workspace_roots'], [])
            self.assertEqual(payload['sandbox_policy'], {'type':'read-only'})
            self.assertEqual(payload['approval_policy'], 'never')
        self.assertEqual(lines[2:], self.f.lines[2:])
        self.assertEqual(ep.read_revision(self.f.source, ID)['revision'], self.source['revision'])
        self.assertTrue(result['record']['history_view_only'])
        self.assertTrue(ep.inspect_home(self.f.args(self.f.target,self.f.package))['threads'][0]['history_view_only'])
        self.assertEqual(result['record']['item_count'], self.source['item_count'])
        self.assertEqual(result['record']['turn_count'], self.source['turn_count'])
        preserved = Path(result['backup']) / 'source-package'
        for name in ('meta.json','transcript.jsonl','history.sqlite'):
            self.assertEqual((preserved/name).read_bytes(), (self.f.package/name).read_bytes())
        offsets = fixtures.boundaries(lines)
        with fixtures.database(self.f.target / 'thread_history_1.sqlite') as db:
            self.assertEqual(db.execute('SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns').fetchone(), (offsets[1],offsets[4]))
            self.assertEqual(db.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state').fetchone()[0], offsets[-1])
        with fixtures.database(self.f.target / 'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT cwd,sandbox_policy,approval_mode FROM threads').fetchone(), (str(workspace),'{"type":"read-only"}','never'))

    def test_viewing_replica_cannot_be_imported_back_as_a_project(self):
        with mock.patch.object(ep, 'native_platform', return_value='windows'):
            imported = ep.import_chat(self.args())
            package = self.f.root / 'view-export'
            ep.export_chat(self.f.args(self.f.target, package, expected=imported['revision']))
        args = self.f.args(self.f.source, package, expected=self.source['revision'], allow_existing=True)
        for operation in (ep.import_preview, ep.import_chat):
            with self.subTest(operation=operation.__name__), self.assertRaises(ep.EndpointError) as error:
                operation(args)
            self.assertEqual(error.exception.code, 'history_view_writeback_forbidden')
        self.assertEqual(ep.read_revision(self.f.source, ID)['revision'], self.source['revision'])

    def test_nonempty_or_symlink_workspace_is_rejected(self):
        workspace = self.f.target / '.history-sync-view' / ID
        workspace.mkdir(parents=True)
        (workspace/'unexpected.txt').write_text('preserve')
        with mock.patch.object(ep, 'native_platform', return_value='windows'), self.assertRaises(ep.EndpointError) as error:
            ep.import_preview(self.args())
        self.assertEqual(error.exception.code, 'history_view_workspace_unsafe')
        self.assertEqual((workspace/'unexpected.txt').read_text(),'preserve')

    def test_wrong_platform_and_environment_repair_do_not_enable_viewing(self):
        for platform, environment_only in (('linux',False), ('windows',True)):
            args = self.args()
            args.environment_only = environment_only
            with mock.patch.object(ep, 'native_platform', return_value=platform), self.assertRaises(ep.EndpointError) as error:
                ep.import_preview(args)
            self.assertEqual(error.exception.code,'history_view_invalid')

    def test_continuation_updates_same_id_and_backs_up_existing_view(self):
        with mock.patch.object(ep, 'native_platform', return_value='windows'):
            first = ep.import_chat(self.args())
        original_view = self.f.target_lines()
        self.f.lines.append(fixtures.line({'type':'response_item','payload':{'role':'assistant','text':'next'}}))
        self.f.set_transcript(self.f.source,self.f.lines)
        package = self.f.root / 'next-package'
        self.f.export_from(package=package,platform='linux')
        args = self.f.args(self.f.target, package, history_view_only=True, expected=first['revision'],allow_existing=True)
        with mock.patch.object(ep,'native_platform',return_value='windows'):
            second = ep.import_chat(args)
        self.assertEqual(second['id'],first['id'])
        self.assertEqual(self.f.target_lines()[-1],self.f.lines[-1])
        self.assertEqual((Path(second['backup'])/'transcript.jsonl').read_bytes(),b''.join(original_view))
        with fixtures.database(self.f.target/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM threads').fetchone()[0],1)

    def test_symlink_view_root_is_not_followed(self):
        link=self.f.target/'.history-sync-view'
        other=self.f.root/'other'
        other.mkdir()
        try:
            link.symlink_to(other,target_is_directory=True)
        except OSError:
            self.skipTest('Creating directory symlinks is unavailable')
        with mock.patch.object(ep,'native_platform',return_value='windows'), self.assertRaises(ep.EndpointError) as error:
            ep.import_preview(self.args())
        self.assertEqual(error.exception.code,'history_view_workspace_unsafe')
        self.assertEqual(list(other.iterdir()),[])

    def test_existing_native_target_drops_project_binding_and_keeps_original_backup(self):
        target=fixtures.fixture(self.f.root/'existing-native',extra_column=True)
        old=ep.read_revision(target,ID)
        args=self.f.args(target,self.f.package,history_view_only=True,expected=old['revision'],allow_existing=True)
        with mock.patch.object(ep,'native_platform',return_value='windows'):
            result=ep.import_chat(args)
        with fixtures.database(target/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT project_id,sandbox_policy,approval_mode FROM threads').fetchone(),
                             (None,'{"type":"read-only"}','never'))
        saved=json.loads((Path(result['backup'])/'backup.json').read_text())
        self.assertEqual(saved['thread']['project_id'],'local-project')

    def test_view_only_full_edits_are_blocked_but_metadata_and_wsl_updates_remain_possible(self):
        def row(body,meta='m',view=False):
            return {'id':ID,'revision':body+meta,'content_revision':body,'metadata_revision':meta,
                    'active':False,'quiet_seconds':120,'item_count':1,'turn_count':1,'history_view_only':view}
        win,lin=row('view',view=True),row('original')
        views=lambda r:{'threads':[r],'backend_running':False}
        ledger=sync.baseline(views(win),views(lin))
        ledger['threads'][ID]['body_diverged']=False
        for marker in (True,False):
            plan=sync.build_plan(views(row('edited',view=marker)),views(lin),ledger)
            self.assertEqual(plan['actions'],[])
            self.assertEqual(plan['blocked'][0]['reason'],'history_view_writeback_forbidden')
        plan=sync.build_plan(views(row('view','renamed',view=True)),views(lin),ledger)
        self.assertEqual(plan['actions'][0]['mode'],'metadata')
        plan=sync.build_plan(views(win),views(row('continued')),ledger)
        self.assertEqual(plan['actions'][0]['source'],'wsl')

    def test_controller_passes_view_flag_only_to_full_windows_import_operations(self):
        config={'python_windows':'python.exe','windows_home':'C:\\Example', 'windows_history_view_only':True,
                'wsl_distro':'Ubuntu','wsl_user':'example-user','wsl_home':'/home/example-user/.codex'}
        endpoint=sync.Endpoints(config,self.f.root)
        with mock.patch.object(sync.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'{"ok":true}')) as run:
            for operation,options,wanted in [('import-preview',(),True),('import',(),True),
                    ('import',('--metadata-only',),False),('import',('--environment-only',),False),('inspect',(),False)]:
                endpoint.call('windows',operation,*options)
                self.assertEqual('--history-view-only' in run.call_args.args[0],wanted)

    def test_configuration_flag_is_strict_boolean(self):
        config = {'windows_home':'C:\\Example','wsl_distro':'Ubuntu','wsl_user':'example-user',
                  'wsl_home':'/home/example-user/.codex','python_windows':'python.exe'}
        for value in ('true',1,None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.validate_config({**config,'windows_history_view_only':value})
        sync.validate_config({**config,'windows_history_view_only':True})


if __name__ == '__main__':
    unittest.main()
