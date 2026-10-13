"""Opt-in conflict selection against synthetic records and mocked endpoints."""
import copy
import importlib.util
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location('prefer_longer_sync', Path(__file__).resolve().parents[1] / 'history_sync.py')
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def row(body, turns=2, items=10, **changes):
    value = {'id': 'fixture-chat', 'revision': body + '/metadata',
             'content_revision': body, 'metadata_revision': 'metadata',
             'active': False, 'quiet_seconds': 120, 'transcript_complete': True,
             'transcript_size': 1000, 'turn_count': turns, 'item_count': items,
             'history_mode': 'paginated'}
    value.update(changes)
    return value


def view(record=None, backend=False):
    return {'threads': [record] if record is not None else [], 'backend_running': backend}


class PreferLongerPlanner(unittest.TestCase):
    def setUp(self):
        self.win = row('windows-new', 3, 14)
        self.lin = row('linux-new', 4, 11)
        self.ledger = sync.baseline(view(row('old')), view(row('old')))

    def plan(self, win=None, lin=None, **options):
        return sync.build_plan(view(self.win if win is None else win),
                               view(self.lin if lin is None else lin), self.ledger,
                               conflict_resolution='prefer_longer', **options)

    def test_default_manual_shape_and_conflict_are_unchanged(self):
        plan = sync.build_plan(view(self.win), view(self.lin), self.ledger)
        self.assertEqual(set(plan), {'actions', 'blocked', 'baseline_pairs'})
        self.assertEqual(plan['blocked'], [{'id': 'fixture-chat', 'reason': 'both_sides_changed'}])

    def test_more_turns_wins_even_with_fewer_items(self):
        original = copy.deepcopy((self.win, self.lin, self.ledger))
        plan = self.plan()
        action = plan['actions'][0]
        self.assertEqual((action['source'], action['target'], action['mode']), ('wsl', 'windows', 'full'))
        self.assertEqual(action['conflict_resolution'], 'prefer_longer')
        self.assertEqual(action['source_revision'], self.lin['revision'])
        self.assertEqual(action['target_revision'], self.win['revision'])
        self.assertEqual(plan['conflict_decisions'], [{
            'id': 'fixture-chat', 'source': 'wsl', 'target': 'windows',
            'source_counts': {'turns': 4, 'items': 11},
            'target_counts': {'turns': 3, 'items': 14},
            'reason': 'more_turns', 'status': 'ready'}])
        self.assertEqual((self.win, self.lin, self.ledger), original)

    def test_more_items_breaks_equal_turn_count(self):
        plan = self.plan(lin=row('linux-new', 3, 11))
        self.assertEqual(plan['actions'][0]['source'], 'windows')
        self.assertEqual(plan['conflict_decisions'][0]['reason'], 'more_items')

    def test_equal_counts_preserve_conflict(self):
        plan = self.plan(lin=row('linux-new', 3, 14))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'conflict_counts_equal')
        self.assertEqual(plan['conflict_decisions'], [])

    def test_counts_must_be_explicit_nonnegative_integers_on_both_sides(self):
        for side in ('windows', 'wsl'):
            for key in ('turn_count', 'item_count'):
                for invalid in (None, -1, 2.5, True, '4', [], {}):
                    with self.subTest(side=side, key=key, invalid=invalid):
                        win, lin = copy.deepcopy(self.win), copy.deepcopy(self.lin)
                        (win if side == 'windows' else lin)[key] = invalid
                        plan = self.plan(win, lin)
                        self.assertEqual(plan['actions'], [])
                        self.assertEqual(plan['blocked'][0]['reason'], 'conflict_counts_unavailable')

    def test_cache_or_nested_counts_are_not_used_as_a_fallback(self):
        lin = copy.deepcopy(self.lin)
        del lin['turn_count']
        del lin['item_count']
        lin.update(counts={'turns': 900, 'items': 900}, cache={'thread_turns': {'row_count': 900}, 'thread_items': {'row_count': 900}})
        self.assertEqual(self.plan(lin=lin)['blocked'][0]['reason'], 'conflict_counts_unavailable')

    def test_complete_positive_transcript_is_required(self):
        for changes in ({'transcript_complete': None}, {'transcript_complete': 1},
                        {'transcript_size': 0}, {'transcript_size': -1},
                        {'transcript_size': True}, {'transcript_size': '100'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.plan(lin=row('new', 4, 20, **changes))['blocked'][0]['reason'], 'conflict_history_incomplete')
        lin = copy.deepcopy(self.lin)
        del lin['transcript_complete']
        self.assertEqual(self.plan(lin=lin)['blocked'][0]['reason'], 'conflict_history_incomplete')

    def test_known_incomplete_transcript_keeps_earlier_protection(self):
        self.assertEqual(self.plan(lin=row('new', 4, 20, transcript_complete=False))['blocked'][0]['reason'], 'unreadable_transcript')

    def test_empty_or_unindexed_loser_is_not_overwritten(self):
        for turns, items in ((0, 0), (1, 0), (0, 1)):
            with self.subTest(turns=turns, items=items):
                self.assertEqual(self.plan(win=row('win', turns, items))['blocked'][0]['reason'], 'conflict_history_empty')
        self.assertEqual(self.plan(win=row('win', 0, 0, transcript_message_hint=True))['blocked'][0]['reason'], 'conflict_history_unindexed')

    def test_unchanged_tracked_original_divergence_uses_actual_longer(self):
        self.ledger = sync.baseline(view(self.win), view(self.lin))
        self.assertTrue(self.ledger['threads']['fixture-chat']['body_diverged'])
        self.assertEqual(self.plan()['actions'][0]['source'], 'wsl')

    def test_original_divergence_with_one_changed_side_can_choose_other_side(self):
        self.ledger = sync.baseline(view(row('old-win', 3, 13)), view(self.lin))
        self.assertEqual(self.plan()['actions'][0]['source'], 'wsl')

    def test_untracked_initial_divergence_is_protected(self):
        self.ledger = {'version': 1, 'threads': {}}
        plan = self.plan()
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'initial_body_difference')

    def test_missing_replica_is_not_recreated(self):
        plan = sync.build_plan(view(self.win), view(), self.ledger, conflict_resolution='prefer_longer')
        self.assertEqual(plan['blocked'], [{'id': 'fixture-chat', 'reason': 'replica_missing'}])

    def test_both_sides_need_to_be_quiet(self):
        for side, reason in (('windows', 'target_busy'), ('wsl', 'source_busy')):
            for changes in ({'active': True}, {'quiet_seconds': 59}, {'quiet_seconds': None}):
                with self.subTest(side=side, changes=changes):
                    win, lin = copy.deepcopy(self.win), copy.deepcopy(self.lin)
                    (win if side == 'windows' else lin).update(changes)
                    plan = self.plan(win, lin)
                    self.assertEqual(plan['actions'], [])
                    self.assertEqual(plan['blocked'][0]['reason'], reason)
                    self.assertEqual(plan['blocked'][0]['source'], 'wsl')
                    self.assertEqual(plan['conflict_decisions'][0]['waiting_reason'], reason)

    def test_selected_target_backend_guard_remains(self):
        plan = sync.build_plan(view(self.win, backend=True), view(self.lin), self.ledger,
                               conflict_resolution='prefer_longer')
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'target_backend_running')
        self.assertEqual(plan['blocked'][0]['target'], 'windows')
        self.assertEqual(plan['conflict_decisions'][0]['status'], 'waiting')

    def test_source_backend_does_not_change_native_existing_guard(self):
        plan = sync.build_plan(view(self.win), view(self.lin, backend=True), self.ledger,
                               conflict_resolution='prefer_longer')
        self.assertEqual(len(plan['actions']), 1)

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            sync.build_plan(view(self.win), view(self.lin), self.ledger, conflict_resolution='force')


class PreferLongerExecute(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='prefer-longer-test-')
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.win, self.lin = row('windows-new', 5, 30), row('linux-new', 3, 17)
        self.ledger = sync.baseline(view(row('old')), view(row('old')))
        (self.runtime / 'ledger.json').write_text(json.dumps(self.ledger), encoding='utf-8')
        self.endpoint = MagicMock()
        self.endpoint.inspect.side_effect = lambda side: copy.deepcopy(view(self.win if side == 'windows' else self.lin))
        self.endpoint.backend_status.return_value = {'backend_running': False}
        self.imported = row('committed-target', 5, 30)
        self.endpoint.call.side_effect = lambda side, operation, *args: {'record': self.imported, 'revision': self.imported['revision'], 'backup': '/synthetic/backup'} if operation == 'import' else {'compatible': True}
        for name, replacement in (('wsl_running', lambda _: True), ('Endpoints', lambda *_: self.endpoint), ('linux_mount_path', str)):
            patcher = patch.object(sync, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {'wsl_distro': 'Synthetic', 'conflict_resolution': 'prefer_longer'}

    def execute(self, dry=False):
        return sync.execute(self.config, SimpleNamespace(initialize=False, dry_run=dry), self.runtime, logging.getLogger('prefer-longer-test'))

    def test_existing_export_preview_import_flow_preserves_exact_expected_revisions(self):
        summary = self.execute()
        calls = self.endpoint.call.call_args_list
        self.assertEqual([call.args[1] for call in calls], ['export', 'import-preview', 'import'])
        self.assertIn(self.win['revision'], calls[0].args)
        self.assertIn(self.lin['revision'], calls[2].args)
        self.assertIn('--allow-existing', calls[2].args)
        self.assertEqual(summary['transferred'], 1)
        self.assertEqual(summary['resolved_conflicts'], 1)
        self.assertEqual(summary['conflict_results'], [{
            'id': 'fixture-chat', 'source': 'windows', 'target': 'wsl',
            'source_counts': {'turns': 5, 'items': 30},
            'target_counts': {'turns': 3, 'items': 17},
            'reason': 'more_turns', 'backup': '/synthetic/backup'}])
        saved = json.loads((self.runtime / 'ledger.json').read_text(encoding='utf-8'))['threads']['fixture-chat']
        self.assertEqual(saved['windows'], self.win)
        self.assertEqual(saved['wsl'], self.imported)
        self.assertFalse(saved['body_diverged'])
        self.assertFalse((self.runtime / 'pending.json').exists())

    def test_backend_start_blocks_existing_import_after_export(self):
        self.endpoint.backend_status.return_value = {'backend_running': True}
        summary = self.execute()
        self.assertEqual(summary['transferred'], 0)
        self.assertEqual(summary['resolved_conflicts'], 0)
        self.assertEqual(summary['conflict_results'], [])
        self.assertEqual(summary['deferred'][0]['reason'], 'target_backend_started')
        self.assertEqual([call.args[1] for call in self.endpoint.call.call_args_list], ['export', 'import-preview'])
        self.assertEqual(json.loads((self.runtime / 'ledger.json').read_text(encoding='utf-8')), self.ledger)

    def test_failed_import_keeps_conflict_pending_and_old_baseline(self):
        def transfer(side, operation, *args):
            if operation == 'import':
                raise RuntimeError('fixture failure')
            return {'compatible': True}
        self.endpoint.call.side_effect = transfer
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            self.execute()
        journal = json.loads((self.runtime / 'pending.json').read_text(encoding='utf-8'))
        self.assertEqual(journal['conflict_resolution'], 'prefer_longer')
        self.assertEqual(journal['target_revision'], self.lin['revision'])
        self.assertEqual(json.loads((self.runtime / 'ledger.json').read_text(encoding='utf-8')), self.ledger)

    def test_dry_run_returns_selection_without_mutations(self):
        previous = (self.runtime / 'ledger.json').read_bytes()
        summary = self.execute(dry=True)
        self.assertEqual(summary['actions'][0]['conflict_resolution'], 'prefer_longer')
        self.assertEqual(summary['conflict_decisions'][0]['status'], 'ready')
        self.assertEqual(summary['resolved_conflicts'], 0)
        self.assertEqual(summary['conflict_results'], [])
        self.endpoint.call.assert_not_called()
        self.assertEqual((self.runtime / 'ledger.json').read_bytes(), previous)

    def test_omitted_config_keeps_manual_no_transfer(self):
        self.config.pop('conflict_resolution')
        summary = self.execute()
        self.assertEqual(summary['transferred'], 0)
        self.assertNotIn('conflict_decisions', summary)
        self.endpoint.call.assert_not_called()


class PreferLongerConfiguration(unittest.TestCase):
    def configuration(self, **changes):
        value = {'windows_home': str(Path.cwd()), 'wsl_distro': 'Synthetic',
                 'wsl_user': 'synthetic', 'wsl_home': '/tmp/synthetic',
                 'python_windows': 'auto'}
        value.update(changes)
        return value

    def test_valid_policies_and_safe_normalized_default(self):
        for policy in ('manual', 'prefer_longer'):
            self.assertEqual(sync.validate_config(self.configuration(conflict_resolution=policy))['conflict_resolution'], policy)
        supplied = self.configuration()
        normalized = sync.normalize_config(supplied, Path.cwd() / 'config.json')
        self.assertEqual(normalized['conflict_resolution'], 'manual')
        self.assertNotIn('conflict_resolution', supplied)

    def test_invalid_policy_rejected(self):
        for value in (None, False, 1, [], {}, '', 'force', 'Prefer_Longer'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync.validate_config(self.configuration(conflict_resolution=value))


if __name__ == '__main__':
    unittest.main()
