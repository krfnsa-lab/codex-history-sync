"""New-copy transaction/recovery guards; no live homes or model requests."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

import history_sync as core
import merge_chat as service


class Sync:
    VERSION = 1
    records = staticmethod(core.records)
    is_busy = staticmethod(core.is_busy)
    build_plan = staticmethod(core.build_plan)
    atomic_json = staticmethod(core.atomic_json)
    choice_token = staticmethod(core.choice_token)
    linux_mount_path = staticmethod(str)


class Endpoint:
    def __init__(self, old, new):
        self.rows = {s: {'old': old[s]} for s in ('windows', 'wsl')}
        self.new = new
        self.calls = []
        self.fail = None

    def inspect(self, side):
        return {'threads': list(self.rows[side].values())}

    def call(self, side, operation, *args):
        self.calls.append((side, operation, args))
        if self.fail == side:
            raise RuntimeError('Synthetic import failure')
        if operation != 'import':
            raise AssertionError('Unexpected mutation')
        row = dict(self.new[side])
        self.rows[side][row['id']] = row
        return {'ok': True, 'record': row, 'revision': row['revision']}


def record(tid, rev):
    return {'id': tid, 'revision': rev, 'content_revision': rev, 'metadata_revision': 'm',
            'turn_count': 5, 'item_count': 7, 'active': False, 'quiet_seconds': 120,
            'transcript_complete': True, 'name': 'Synthetic', 'source': 'vscode'}


class MergeTransaction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='merge-chat-tests-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.job = self.runtime / 'merges' / 'job'
        self.package = self.job / 'copy' / 'merged'
        self.package.mkdir(parents=True)
        self.old = {s: record('old', 'old-' + s) for s in ('windows', 'wsl')}
        self.new = {s: record('new', 'new-' + s) for s in ('windows', 'wsl')}
        self.ledger = {'version': 1, 'threads': {'old': {'windows': record('old', 'prior-win'), 'wsl': record('old', 'prior-wsl')},
                                               'unrelated': {'keep': True}}}
        self.prepared = {'status': 'validated', 'job_id': 'job', 'original_id': 'old', 'new_id': 'new',
                         'title': 'Synthetic merged', 'source_records': self.old, 'merged_turns': 5, 'merged_items': 7}
        self.validation = {'validation_passed': True, 'new_thread_id': 'new', 'package_hashes': {}}
        for n in ('meta.json', 'history.sqlite', 'transcript.jsonl'):
            (self.package / n).write_bytes(b'synthetic')
            self.validation['package_hashes'][n] = service.sha(self.package / n)
        Sync.atomic_json(self.job / 'native-validation.json', self.validation)
        self.endpoint = Endpoint(self.old, self.new)

    def finish(self):
        return service.finish_job(Sync, self.endpoint, self.runtime, self.ledger, self.job, self.prepared)

    def test_creates_only_new_id_and_keeps_exact_source_baselines(self):
        result = self.finish()
        self.assertEqual(result['status'], 'merged')
        self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertEqual(len(self.endpoint.calls), 2)
        for side, operation, args in self.endpoint.calls:
            self.assertEqual(operation, 'import')
            self.assertEqual(args[-2:], ('--expected', 'absent'))
            self.assertNotIn('--allow-existing', args)
            self.assertEqual(self.endpoint.rows[side]['old'], self.old[side])
        saved = json.loads((self.runtime / 'ledger.json').read_text())
        self.assertEqual(saved['threads']['unrelated'], {'keep': True})
        self.assertEqual(saved['threads']['old']['windows'], self.old['windows'])
        self.assertEqual(saved['threads']['old']['superseded_by'], 'new')
        self.assertFalse(saved['threads']['new']['body_diverged'])

    def test_second_side_failure_keeps_pending_and_resume_does_not_reimport_first(self):
        self.endpoint.fail = 'wsl'
        with self.assertRaises(RuntimeError): self.finish()
        self.assertTrue((self.runtime / 'pending.json').exists())
        self.assertNotIn('new', self.ledger['threads'])
        self.endpoint.fail = None
        self.endpoint.calls.clear()
        self.assertEqual(self.finish()['status'], 'merged')
        self.assertEqual([c[0] for c in self.endpoint.calls], ['wsl'])

    def test_changed_partial_copy_is_never_overwritten(self):
        self.endpoint.fail = 'wsl'
        with self.assertRaises(RuntimeError): self.finish()
        self.endpoint.rows['windows']['new']['revision'] = 'user-continued'
        self.endpoint.fail = None
        self.endpoint.calls.clear()
        with self.assertRaises(RuntimeError): self.finish()
        self.assertEqual(self.endpoint.calls, [])
        self.assertTrue((self.runtime / 'pending.json').exists())

    def test_foreign_pending_is_not_touched(self):
        Sync.atomic_json(self.runtime / 'pending.json', {'kind': 'ordinary_import'})
        with self.assertRaises(RuntimeError): self.finish()
        self.assertEqual(self.endpoint.calls, [])

    def test_collision_without_receipt_is_not_overwritten(self):
        self.endpoint.rows['windows']['new'] = record('new', 'foreign')
        with self.assertRaises(RuntimeError): self.finish()
        self.assertEqual(self.endpoint.calls, [])

    def test_changed_or_missing_validation_hashes_abort_before_pending(self):
        for value in ({}, {'meta.json': 'changed'}):
            self.validation['package_hashes'] = value
            Sync.atomic_json(self.job / 'native-validation.json', self.validation)
            with self.assertRaises(RuntimeError): self.finish()
            self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertEqual(self.endpoint.calls, [])

    def test_declined_native_validation_aborts(self):
        self.validation['validation_passed'] = False
        Sync.atomic_json(self.job / 'native-validation.json', self.validation)
        with self.assertRaises(RuntimeError): self.finish()
        self.assertEqual(self.endpoint.calls, [])

    def test_conflict_candidates_are_bounded_and_busy_is_disabled(self):
        win, wsl = self.old['windows'], self.old['wsl']
        win['name'] = 'Long label ' * 100
        wsl['active'] = True
        views = {s: {'threads': [r], 'backend_running': True} for s, r in self.old.items()}
        candidates = service.candidates(Sync, views, self.ledger, 60)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]['ready'])
        self.assertLessEqual(len(candidates[0]['label']), 90)


@contextmanager
def acquired_lock(_path):
    yield True


class ChoiceEndpoint:
    """Synthetic packages; native SQLite imports are covered separately."""
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.backends = {'windows': True, 'wsl': True}
        self.calls = []
        self.after_preview = None
        self.after_export = None
        self.fail_import = False
        self.bad_receipt = False
        self.inspect_hook = None
        self.compatible = True

    def inspect(self, side):
        if self.inspect_hook:
            self.inspect_hook(side)
        return {'threads': [copy.deepcopy(self.rows[side])], 'backend_running': self.backends[side]}

    def backend_status(self, side):
        return {'backend_running': self.backends[side], 'backend_detection_ok': True}

    def call(self, side, operation, *args):
        self.calls.append((side, operation, args))
        if operation == 'export':
            package = Path(args[args.index('--package') + 1])
            package.mkdir()
            core.atomic_json(package / 'meta.json', {'thread': {'id': 'old'}, 'source_revision': self.rows[side]})
            (package / 'history.sqlite').write_bytes(('synthetic cache ' + side).encode())
            (package / 'transcript.jsonl').write_bytes(('full original ' + side + '\n').encode())
            if self.after_export:
                self.after_export(side)
            return {'ok': True}
        if operation == 'import-preview':
            if self.after_preview:
                self.after_preview()
            return {'ok': True, 'compatible': self.compatible, 'reason': 'workspace_cwd_unmappable'}
        if operation == 'import':
            if self.fail_import:
                raise RuntimeError('Synthetic interrupted import')
            source = 'wsl' if side == 'windows' else 'windows'
            row = copy.deepcopy(self.rows[source])
            row['revision'] = row['content_revision'] = 'native-imported-' + side
            if self.bad_receipt:
                row['item_count'] -= 1
            self.rows[side] = row
            return {'ok': True, 'record': row, 'revision': row['revision'], 'backup': 'synthetic-native-backup'}
        raise AssertionError(operation)


class ManualChoiceTransaction(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='manual-choice-test-')
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name).resolve()
        self.rows = {side: record('old', 'current-' + side) for side in ('windows', 'wsl')}
        self.rows['windows'].update(turn_count=6, item_count=9)
        self.ledger = {'version': 1, 'threads': {'old': {
            'windows': record('old', 'old-windows'), 'wsl': record('old', 'old-wsl'), 'body_diverged': False}}}
        core.atomic_json(self.runtime / 'ledger.json', self.ledger)
        self.endpoint = ChoiceEndpoint(self.rows)
        self.config = {'wsl_distro': 'Synthetic', 'quiet_seconds': 60}
        self.token = core.choice_token('old', self.rows)
        self.args = SimpleNamespace(id='old', list=False, resume=None, keep_side='windows', expected_token=self.token)
        self.sync_args = SimpleNamespace(initialize=False, dry_run=False)

    def select(self):
        with patch.object(core, 'wsl_running', return_value=True), patch.object(core, 'run_lock', acquired_lock), \
             patch.object(core, 'linux_mount_path', side_effect=str):
            return service.execute(core, self.config, self.runtime, self.endpoint, self.args)

    def consume(self):
        with patch.object(core, 'wsl_running', return_value=True), \
             patch.object(core, 'Endpoints', return_value=self.endpoint), \
             patch.object(core, 'linux_mount_path', side_effect=str):
            return core.execute(self.config, self.sync_args, self.runtime, MagicMock())

    def saved_ledger(self):
        return json.loads((self.runtime / 'ledger.json').read_text())

    def choice(self):
        return core.read_conflict_choices(self.runtime)['choices']['old']

    def test_list_token_binds_both_exact_versions(self):
        self.args.list = True
        original = self.select()['chats'][0]['choice_token']
        self.endpoint.rows['wsl']['revision'] = 'new-wsl'
        changed = self.select()['chats'][0]['choice_token']
        self.assertNotEqual(original, changed)
        self.assertEqual(self.endpoint.calls, [])

    def test_unchanged_but_diverged_initial_baseline_still_offers_a_choice(self):
        self.ledger['threads']['old'] = {**self.rows, 'body_diverged': True}
        core.atomic_json(self.runtime / 'ledger.json', self.ledger)
        self.args.list = True
        result = self.select()
        self.assertEqual([chat['id'] for chat in result['chats']], ['old'])
        self.assertTrue(result['chats'][0]['ready'])

    def test_confirmation_token_change_stops_before_backups_or_queue(self):
        self.endpoint.rows['windows']['revision'] = 'new-version'
        result = self.select()
        self.assertEqual(result['reason'], 'source_changed')
        self.assertEqual(self.endpoint.calls, [])
        self.assertFalse((self.runtime / 'conflict-choices.json').exists())
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_selection_preserves_both_full_versions_without_import_or_checkpoint(self):
        result = self.select()
        self.assertEqual(result['status'], 'choice_saved')
        choice = self.choice()
        self.assertEqual(choice['expected'], {side: row['revision'] for side, row in self.rows.items()})
        for side in ('windows', 'wsl'):
            package = core.conflict_choice_package(self.runtime, choice, side)
            self.assertEqual((package / 'transcript.jsonl').read_text(), 'full original ' + side + '\n')
        self.assertEqual([call[1] for call in self.endpoint.calls], ['export', 'export'])
        self.assertEqual(self.endpoint.rows, self.rows)
        self.assertEqual(self.saved_ledger(), self.ledger)
        self.assertFalse((self.runtime / 'pending.json').exists())

    def test_saved_choice_progress_is_waiting_and_never_claims_transferred(self):
        self.args.progress_request_id = '40000000-0000-4000-8000-000000000001'
        result = self.select()
        progress = json.loads((self.runtime / 'progress.json').read_text())
        self.assertEqual(result['status'], 'choice_saved')
        self.assertEqual((progress['kind'], progress['request_id'], progress['status']),
                         ('choice', self.args.progress_request_id, 'waiting'))
        self.assertEqual((progress['processed_chats'], progress['total_chats']), (1, 1))
        self.assertEqual(progress.get('transferred_chats', 0), 0)
        self.assertEqual([call[1] for call in self.endpoint.calls], ['export', 'export'])
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_list_progress_is_read_only_check_not_choice_or_transfer(self):
        self.args.list = True
        self.args.progress_request_id = '40000000-0000-4000-8000-000000000002'
        result = self.select()
        progress = json.loads((self.runtime / 'progress.json').read_text())
        self.assertEqual(result['status'], 'candidates')
        self.assertEqual((progress['kind'], progress['request_id'], progress['status']),
                         ('check', self.args.progress_request_id, 'completed'))
        self.assertEqual(progress.get('transferred_chats', 0), 0)
        self.assertFalse(self.endpoint.calls)
        self.assertFalse((self.runtime / 'conflict-choices.json').exists())

    def test_export_race_preserves_partial_backup_and_never_saves_decision(self):
        self.endpoint.after_export = lambda side: self.endpoint.rows[side].update(revision='changed-' + side)
        self.assertEqual(self.select()['reason'], 'source_changed')
        self.assertFalse((self.runtime / 'conflict-choices.json').exists())
        self.assertEqual(self.saved_ledger(), self.ledger)
        self.assertTrue(any((self.runtime / 'conflict-choices').glob('*/windows/transcript.jsonl')))

    def test_existing_pending_and_busy_lock_are_never_bypassed(self):
        core.atomic_json(self.runtime / 'pending.json', {'kind': 'ordinary_import'})
        self.assertEqual(self.select()['reason'], 'pending_import')
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(json.loads((self.runtime / 'pending.json').read_text()), {'kind': 'ordinary_import'})

    def test_locked_controller_and_stopped_wsl_make_no_backups(self):
        @contextmanager
        def busy_lock(_path):
            yield False
        with patch.object(core, 'wsl_running', return_value=True), patch.object(core, 'run_lock', busy_lock):
            self.assertEqual(service.execute(core, self.config, self.runtime, self.endpoint, self.args)['reason'], 'controller_running')
        with patch.object(core, 'wsl_running', return_value=False):
            self.assertEqual(service.execute(core, self.config, self.runtime, self.endpoint, self.args)['reason'], 'wsl_stopped')
        self.assertEqual(self.endpoint.calls, [])
        self.assertFalse((self.runtime / 'conflict-choices.json').exists())

    def test_live_target_keeps_choice_pending_without_import(self):
        self.select()
        self.endpoint.calls.clear()
        result = self.consume()
        self.assertEqual(result['transferred'], 0)
        self.assertEqual(result['manual_choices'][0]['reason'], 'target_backend_running')
        self.assertEqual(self.choice()['status'], 'pending')
        self.assertEqual(self.saved_ledger(), self.ledger)
        self.assertEqual(self.endpoint.calls, [])

    def test_each_direction_imports_confirmed_side_only_after_target_stops(self):
        for side in ('windows', 'wsl'):
            with self.subTest(side=side):
                self.endpoint = ChoiceEndpoint(self.rows)
                core.atomic_json(self.runtime / 'ledger.json', self.ledger)
                self.args.keep_side = side
                self.select()
                self.endpoint.calls.clear()
                target = 'wsl' if side == 'windows' else 'windows'
                self.endpoint.backends[target] = False
                result = self.consume()
                self.assertEqual(result['transferred'], 1)
                self.assertEqual(result['resolved_conflicts'], 1)
                self.assertEqual(self.choice()['status'], 'resolved')
                self.assertEqual(result['manual_choices'][0]['status'], 'resolved')
                imports = [call for call in self.endpoint.calls if call[1] == 'import']
                self.assertEqual(len(imports), 1)
                self.assertEqual(imports[0][0], target)
                self.assertIn('--allow-existing', imports[0][2])
                args = imports[0][2]
                self.assertEqual(args[args.index('--expected') + 1], self.rows[target]['revision'])
                saved = self.saved_ledger()['threads']['old']
                self.assertEqual(saved[side]['revision'], self.rows[side]['revision'])
                self.assertEqual(saved[target]['revision'], 'native-imported-' + target)
                self.assertFalse(saved['body_diverged'])
                self.assertEqual(self.endpoint.rows[side], self.rows[side])
                self.assertFalse((self.runtime / 'pending.json').exists())
                self.assertNotIn('export', [call[1] for call in self.endpoint.calls])

    def test_any_original_change_requires_new_confirmation_even_if_reverted_later(self):
        self.select()
        self.endpoint.backends['wsl'] = False
        self.endpoint.rows['windows']['revision'] = 'new-windows'
        result = self.consume()
        self.assertEqual(result['manual_choices'][0]['reason'], 'manual_choice_stale')
        self.assertEqual(self.choice()['status'], 'needs_confirmation')
        self.endpoint.rows = copy.deepcopy(self.rows)
        self.endpoint.calls.clear()
        self.assertEqual(self.consume()['transferred'], 0)
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_edit_during_preview_invalidates_choice_before_pending_import(self):
        self.select()
        self.endpoint.backends['wsl'] = False
        self.endpoint.calls.clear()
        self.endpoint.after_preview = lambda: self.endpoint.rows['wsl'].update(revision='updated-during-preview')
        result = self.consume()
        self.assertEqual(result['transferred'], 0)
        self.assertEqual(result['manual_choices'][0]['status'], 'waiting')
        self.assertEqual(self.choice()['status'], 'needs_confirmation')
        self.assertNotIn('import', [call[1] for call in self.endpoint.calls])
        self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_target_restart_during_final_inspection_defers_without_import(self):
        self.select()
        self.endpoint.backends['wsl'] = False
        self.endpoint.calls.clear()
        inspections = {'count': 0}
        def restart_after_initial_probe(_side):
            inspections['count'] += 1
            if inspections['count'] == 3:
                self.endpoint.backends['wsl'] = True
        self.endpoint.inspect_hook = restart_after_initial_probe
        result = self.consume()
        self.assertEqual(result['transferred'], 0)
        self.assertEqual(result['manual_choices'][0]['reason'], 'target_backend_started')
        self.assertEqual(self.choice()['status'], 'pending')
        self.assertNotIn('import', [call[1] for call in self.endpoint.calls])
        self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_manual_shorter_side_overrides_count_policy_without_reporting_wrong_decision(self):
        self.args.keep_side = 'wsl'
        self.select()
        self.config['conflict_resolution'] = 'prefer_longer'
        self.endpoint.backends['windows'] = False
        result = self.consume()
        self.assertEqual(result['transferred'], 1)
        self.assertEqual(result['conflict_decisions'], [])
        self.assertEqual(result['conflict_results'][0]['source'], 'wsl')
        self.assertEqual(result['conflict_results'][0]['reason'], 'manual_keep')

    def test_reselection_preserves_old_backups_and_binds_new_versions(self):
        self.select()
        old = self.choice()
        self.endpoint.rows['windows']['revision'] = 'new-windows'
        self.args.expected_token = core.choice_token('old', self.endpoint.rows)
        self.args.keep_side = 'wsl'
        self.select()
        new = self.choice()
        self.assertNotEqual(old['choice_id'], new['choice_id'])
        self.assertEqual(new['expected']['windows'], 'new-windows')
        self.assertEqual(new['keep_side'], 'wsl')
        for side in ('windows', 'wsl'):
            core.conflict_choice_package(self.runtime, old, side)
            core.conflict_choice_package(self.runtime, new, side)

    def test_corrupted_private_backup_blocks_before_any_import(self):
        self.select()
        self.endpoint.backends['wsl'] = False
        choice = self.choice()
        path = self.runtime / 'conflict-choices' / choice['choice_id'] / 'wsl' / 'transcript.jsonl'
        path.write_bytes(b'changed')
        self.endpoint.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'backup changed'):
            self.consume()
        self.assertEqual(self.endpoint.calls, [])
        self.assertEqual(self.saved_ledger(), self.ledger)
        self.assertFalse((self.runtime / 'pending.json').exists())

    def test_unmappable_workspace_keeps_originals_and_waiting_choice(self):
        self.select()
        self.endpoint.backends['wsl'] = False
        self.endpoint.compatible = False
        self.endpoint.calls.clear()
        result = self.consume()
        self.assertEqual(result['manual_choices'][0]['status'], 'waiting')
        self.assertEqual(result['manual_choices'][0]['reason'], 'workspace_cwd_unmappable')
        self.assertEqual(result['transferred'], 0)
        self.assertEqual(self.choice()['status'], 'pending')
        self.assertEqual(self.endpoint.rows, self.rows)
        self.assertEqual(self.saved_ledger(), self.ledger)
        self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertEqual([call[1] for call in self.endpoint.calls], ['import-preview'])

    def test_failed_import_keeps_pending_queue_and_old_baseline(self):
        self.select()
        self.endpoint.backends['wsl'] = False
        self.endpoint.fail_import = True
        with self.assertRaisesRegex(RuntimeError, 'interrupted import'):
            self.consume()
        self.assertTrue((self.runtime / 'pending.json').exists())
        self.assertEqual(self.choice()['status'], 'pending')
        self.assertEqual(self.saved_ledger(), self.ledger)
        self.endpoint.fail_import = False
        self.endpoint.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'unfinished prior import'):
            self.consume()
        self.assertEqual(self.endpoint.calls, [])

    def test_incomplete_committed_receipt_keeps_pending_and_unresolved_choice(self):
        self.select()
        self.endpoint.backends['wsl'] = False
        self.endpoint.bad_receipt = True
        with self.assertRaisesRegex(RuntimeError, 'complete selected history'):
            self.consume()
        self.assertTrue((self.runtime / 'pending.json').exists())
        self.assertEqual(self.choice()['status'], 'pending')
        self.assertEqual(self.saved_ledger(), self.ledger)

    def test_dry_run_does_not_persist_stale_choice_or_baseline(self):
        self.select()
        self.endpoint.rows['windows']['revision'] = 'new-windows'
        before = (self.runtime / 'conflict-choices.json').read_bytes()
        self.sync_args.dry_run = True
        result = self.consume()
        self.assertEqual(result['manual_choices'][0]['reason'], 'manual_choice_stale')
        self.assertEqual((self.runtime / 'conflict-choices.json').read_bytes(), before)
        self.assertEqual(self.saved_ledger(), self.ledger)


if __name__ == '__main__':
    unittest.main()
