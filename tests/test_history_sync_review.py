"""Independent planner regression tests. No live Codex directories are read."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    'review_history_sync', ROOT / 'history_sync.py'
)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def row(tid='chat-1', body='body-0', metadata='meta-0', **overrides):
    value = {
        'id': tid, 'revision': body + '/' + metadata,
        'content_revision': body, 'metadata_revision': metadata,
        'active': False, 'quiet_seconds': 120,
        'item_count': 5, 'turn_count': 2,
    }
    value.update(overrides)
    return value


def view(*rows, backend=False):
    return {'threads': list(rows), 'backend_running': backend}


class PlannerReview(unittest.TestCase):
    def setUp(self):
        self.windows = view(row())
        self.wsl = view(row())
        self.ledger = sync.baseline(self.windows, self.wsl)

    def plan(self, windows=None, wsl=None, ledger=None):
        return sync.build_plan(
            self.windows if windows is None else windows,
            self.wsl if wsl is None else wsl,
            self.ledger if ledger is None else ledger,
        )

    def test_unchanged_histories_need_no_action(self):
        self.assertEqual(self.plan()['actions'], [])
        self.assertEqual(self.plan()['blocked'], [])

    def test_windows_new_chat_goes_to_wsl_even_backend_open(self):
        plan = self.plan(view(row(), row('new-win')), view(row(), backend=True))
        self.assertEqual([(a['id'], a['source'], a['mode'], a['target_revision'])
                          for a in plan['actions']],
                         [('new-win', 'windows', 'full', 'absent')])

    def test_wsl_new_chat_goes_to_windows(self):
        plan = self.plan(wsl=view(row(), row('new-linux')))
        self.assertEqual([(a['id'], a['source'], a['target'])
                          for a in plan['actions']],
                         [('new-linux', 'wsl', 'windows')])

    def test_source_only_chat_tracked_at_initialize_can_still_copy(self):
        windows, wsl = view(row()), view()
        plan = self.plan(windows, wsl, sync.baseline(windows, wsl))
        self.assertEqual(len(plan['actions']), 1)

    def test_one_side_continuation_copies_full_history(self):
        plan = self.plan(windows=view(row(body='body-1')))
        self.assertEqual(plan['actions'][0]['source'], 'windows')
        self.assertEqual(plan['actions'][0]['mode'], 'full')
        self.assertEqual(plan['actions'][0]['target_revision'], 'body-0/meta-0')

    def test_rename_or_archive_uses_metadata_only(self):
        plan = self.plan(wsl=view(row(metadata='meta-1')))
        self.assertEqual(plan['actions'][0]['source'], 'wsl')
        self.assertEqual(plan['actions'][0]['mode'], 'metadata')

    def test_both_sides_changed_preserves_both(self):
        plan = self.plan(view(row(body='body-win')), view(row(body='body-linux')))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'both_sides_changed')

    def test_both_sides_changed_to_same_body_is_conservative(self):
        plan = self.plan(view(row(body='same-new')), view(row(body='same-new')))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'both_sides_changed')

    def test_deleted_replica_is_not_recreated(self):
        plan = self.plan(wsl=view())
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'replica_missing')

    def test_deleted_source_is_not_deleted_on_other_side(self):
        plan = self.plan(windows=view())
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'replica_missing')

    def test_known_recovered_different_body_allows_rename(self):
        windows, wsl = view(row()), view(row(body='recovered'))
        ledger = sync.baseline(windows, wsl)
        self.assertTrue(ledger['threads']['chat-1']['body_diverged'])
        plan = self.plan(view(row(metadata='renamed')), wsl, ledger)
        self.assertEqual(plan['actions'][0]['mode'], 'metadata')

    def test_known_recovered_different_body_blocks_continuation(self):
        windows, wsl = view(row()), view(row(body='recovered'))
        ledger = sync.baseline(windows, wsl)
        plan = self.plan(view(row(body='continued')), wsl, ledger)
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'existing_body_difference')

    def test_first_seen_both_existing_histories_only_record_baseline(self):
        plan = self.plan(ledger={'version': 1, 'threads': {}})
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['baseline_pairs'], ['chat-1'])

    def test_first_seen_divergent_histories_are_not_overwritten(self):
        plan = self.plan(wsl=view(row(body='different')),
                         ledger={'version': 1, 'threads': {}})
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'initial_body_difference')

    def test_active_source_is_skipped(self):
        plan = self.plan(view(row(), row('new', active=True)))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'source_busy')

    def test_recent_source_is_skipped(self):
        plan = self.plan(view(row(), row('new', quiet_seconds=30)))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'source_busy')

    def test_empty_source_is_skipped(self):
        plan = self.plan(view(row(), row('new', item_count=0, turn_count=0)))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'empty_conversation')

    def test_real_nested_cache_shape_empty_paginated_chat_is_blocked(self):
        source = row('new')
        source.pop('item_count')
        source.pop('turn_count')
        source.update(cache={'thread_items': {'row_count': 0},
                             'thread_realtime_items': {'row_count': 0},
                             'thread_turns': {'row_count': 0}},
                      history_mode='paginated', transcript_size=0)
        plan = self.plan(view(row(), source))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'empty_conversation')

    def test_real_nested_cache_shape_populated_chat_can_copy(self):
        source = row('new')
        source.pop('item_count')
        source.pop('turn_count')
        source.update(cache={'thread_items': {'row_count': 5},
                             'thread_realtime_items': {'row_count': 0},
                             'thread_turns': {'row_count': 2}},
                      history_mode='paginated', transcript_size=2048)
        plan = self.plan(view(row(), source))
        self.assertEqual(len(plan['actions']), 1)
        self.assertEqual(plan['actions'][0]['id'], 'new')

    def test_unindexed_legacy_messages_need_manual_recovery(self):
        source = row('new')
        source.pop('item_count')
        source.pop('turn_count')
        source.update(cache={'thread_items': {'row_count': 0},
                             'thread_realtime_items': {'row_count': 0},
                             'thread_turns': {'row_count': 0}},
                      history_mode='legacy', transcript_size=2048, has_user_event=True)
        plan = self.plan(view(row(), source))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'unindexed_history')

    def test_paginated_empty_cache_cannot_rely_on_nonempty_transcript(self):
        source = row('new')
        source.pop('item_count')
        source.pop('turn_count')
        source.update(cache={'thread_items': {'row_count': 0},
                             'thread_realtime_items': {'row_count': 0},
                             'thread_turns': {'row_count': 0}},
                      history_mode='paginated', transcript_size=2048)
        plan = self.plan(view(row(), source))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'empty_conversation')

    def test_target_inspection_skip_is_not_treated_as_absent_target(self):
        windows = view(row(), row('unreadable-target'))
        wsl = view(row())
        wsl['skipped'] = [{'id': 'unreadable-target', 'code': 'schema_unsupported'}]
        plan = self.plan(windows, wsl)
        self.assertEqual(plan['actions'], [])
        self.assertTrue(any(item['id'] == 'unreadable-target' for item in plan['blocked']))

    def test_source_inspection_skip_is_not_treated_as_new_reverse_copy(self):
        windows = view(row())
        windows['skipped'] = [{'id': 'unreadable-source', 'code': 'source_changed'}]
        wsl = view(row(), row('unreadable-source'))
        plan = self.plan(windows, wsl)
        self.assertEqual(plan['actions'], [])
        self.assertTrue(any(item['id'] == 'unreadable-source' for item in plan['blocked']))

    def test_unindexed_source_still_allows_safe_metadata_only_update(self):
        source = row(item_count=0, turn_count=0, history_mode='legacy',
                     transcript_size=2048, has_user_event=True,
                     transcript_message_hint=True)
        windows, wsl = view(source), view(row(body='recovered'))
        ledger = sync.baseline(windows, wsl)
        edited = copy.deepcopy(source)
        edited.update(metadata_revision='renamed', revision='body-0/renamed')
        plan = self.plan(view(edited), wsl, ledger)
        self.assertEqual(plan['actions'][0]['mode'], 'metadata')

    def test_actual_worker_payload_empty_history_is_blocked(self):
        endpoint_spec = importlib.util.spec_from_file_location(
            'actual_endpoint_empty_review', ROOT / 'history_endpoint.py'
        )
        endpoint = importlib.util.module_from_spec(endpoint_spec)
        endpoint_spec.loader.exec_module(endpoint)
        with tempfile.TemporaryDirectory(prefix='actual-payload-') as directory:
            home = Path(directory).resolve()
            transcript = home / 'sessions' / 'new.jsonl'
            transcript.parent.mkdir()
            transcript.write_bytes(b'')
            worker_row = {'id': 'new', 'source': 'vscode', 'rollout_path': str(transcript),
                          'history_mode': 'paginated', 'title': '', 'name': None, 'archived': 0}
            record = endpoint.revision_from_connections(home, worker_row, None, quiet_seconds=0)
            # The planner's wall-clock quiescence policy is separate from this
            # payload contract test; make the synthetic new file quiescent.
            record.update(active=False, quiet_seconds=120)
            plan = self.plan(view(row(), record))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'empty_conversation')

    def test_missing_source_rollout_with_nullable_age_is_blocked(self):
        source = row('new', transcript_size=None, transcript_complete=False,
                     quiet_seconds=None)
        plan = self.plan(view(row(), source))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'unreadable_transcript')

    def test_missing_target_rollout_with_nullable_age_is_blocked(self):
        target = row(transcript_size=None, transcript_complete=False,
                     quiet_seconds=None)
        plan = self.plan(view(row(body='continued')), view(target))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'unreadable_transcript')

    def test_incomplete_source_rollout_is_blocked(self):
        source = row('new', transcript_size=2048, transcript_complete=False)
        plan = self.plan(view(row(), source))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'unreadable_transcript')

    def test_incomplete_target_rollout_is_blocked(self):
        target = row(transcript_size=2048, transcript_complete=False)
        plan = self.plan(view(row(body='continued')), view(target))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'unreadable_transcript')

    def test_nullable_source_quiet_age_is_busy(self):
        source = row('new', transcript_size=2048, transcript_complete=True,
                     quiet_seconds=None)
        plan = self.plan(view(row(), source))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'source_busy')

    def test_nullable_target_quiet_age_is_busy(self):
        target = row(transcript_size=2048, transcript_complete=True,
                     quiet_seconds=None)
        plan = self.plan(view(row(body='continued')), view(target))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'target_busy')

    def test_existing_target_backend_blocks_update(self):
        plan = self.plan(view(row(body='new')), view(row(), backend=True))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'target_backend_running')

    def test_existing_busy_target_blocks_update(self):
        plan = self.plan(view(row(body='new')), view(row(active=True)))
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'][0]['reason'], 'target_busy')

    def test_replica_translation_checkpoint_prevents_mirroring_loop(self):
        # Each side has a stable native revision after import; those native
        # revisions may differ because Linux paths and schema defaults differ.
        windows, wsl = view(row(body='new-win')), view(row(body='new-linux'))
        ledger = sync.baseline(windows, wsl)
        plan = self.plan(windows, wsl, ledger)
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['blocked'], [])

    def test_planner_does_not_mutate_inputs(self):
        before = copy.deepcopy((self.windows, self.wsl, self.ledger))
        self.plan()
        self.assertEqual((self.windows, self.wsl, self.ledger), before)


class RunningDistributionReview(unittest.TestCase):
    def test_native_error_code_is_available_without_parsing_message(self):
        endpoint = sync.Endpoints({'python_windows': sys.executable,
                                   'windows_home': 'synthetic-home'}, ROOT)
        result = SimpleNamespace(returncode=2, stdout=json.dumps({
            'ok': False, 'code': 'source_changed', 'message': 'Synthetic text',
        }).encode('utf-8'))
        with patch.object(sync.subprocess, 'run', return_value=result):
            with self.assertRaises(sync.EndpointCallError) as captured:
                endpoint.call('windows', 'export')
        self.assertEqual((captured.exception.side, captured.exception.operation,
                          captured.exception.code), ('windows', 'export', 'source_changed'))

    def test_explicit_native_failure_is_not_success_with_zero_exit_code(self):
        endpoint = sync.Endpoints({'python_windows': sys.executable,
                                   'windows_home': 'synthetic-home'}, ROOT)
        result = SimpleNamespace(returncode=0, stdout=b'{"ok":false,"code":"source_active"}')
        with patch.object(sync.subprocess, 'run', return_value=result):
            with self.assertRaises(sync.EndpointCallError) as captured:
                endpoint.call('windows', 'export')
        self.assertEqual(captured.exception.code, 'source_active')

    def test_nonobject_native_reply_is_rejected(self):
        endpoint = sync.Endpoints({'python_windows': sys.executable,
                                   'windows_home': 'synthetic-home'}, ROOT)
        result = SimpleNamespace(returncode=0, stdout=b'[]')
        with patch.object(sync.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'invalid endpoint response'):
                endpoint.call('windows', 'export')

    def test_utf16_output_and_exact_distribution_match(self):
        from types import SimpleNamespace
        value = '\ufeffUbuntu\r\nUbuntu-Test\r\n'.encode('utf-16-le')
        result = SimpleNamespace(returncode=0, stdout=value)
        with patch.object(sync.subprocess, 'run', return_value=result) as run:
            self.assertTrue(sync.wsl_running('Ubuntu'))
            self.assertFalse(sync.wsl_running('Ubun'))
        for call in run.call_args_list:
            self.assertEqual(call.args[0], ['wsl.exe', '--list', '--running', '--quiet'])

    def test_stopped_wsl_only_queries_inventory(self):
        from types import SimpleNamespace
        result = SimpleNamespace(returncode=0, stdout=b'')
        with patch.object(sync.subprocess, 'run', return_value=result):
            self.assertFalse(sync.wsl_running('Ubuntu'))

    def test_distribution_listing_failure_is_not_assumed_stopped(self):
        from types import SimpleNamespace
        with patch.object(sync.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=b'')):
            with self.assertRaises(RuntimeError):
                sync.wsl_running('Ubuntu')


class CoordinatorCheckpointReview(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='sync-review-')
        self.runtime = Path(self.directory.name).resolve()
        self.windows, self.wsl = view(row(body='new-source')), view(row())
        self.before = sync.baseline(view(row()), view(row()))
        (self.runtime / 'ledger.json').write_text(json.dumps(self.before), encoding='utf-8')
        self.config = {'wsl_distro': 'Ubuntu', 'quiet_seconds': 60}
        self.args = SimpleNamespace(initialize=False, dry_run=False,
                                    status=False, doctor=False, json=False)
        self.endpoint = MagicMock()
        self.endpoint.backend_status.return_value = {
            'backend_running': False, 'backend_detection_ok': True,
        }
        self.endpoint.inspect.side_effect = lambda side: copy.deepcopy(self.windows if side == 'windows' else self.wsl)
        self.imported_record = row(body='imported-native')
        self.endpoint.call.side_effect = lambda side, operation, *args: (
            {'ok': True, 'record': copy.deepcopy(self.imported_record)}
            if operation == 'import' else ({'ok': True, 'compatible': True} if operation == 'import-preview' else {'ok': True})
        )

    def tearDown(self):
        self.directory.cleanup()

    def execute(self):
        with patch.object(sync, 'wsl_running', return_value=True), \
             patch.object(sync, 'Endpoints', return_value=self.endpoint), \
             patch.object(sync, 'linux_mount_path', side_effect=lambda value: str(value)):
            return sync.execute(self.config, self.args, self.runtime, MagicMock())

    def saved(self):
        return json.loads((self.runtime / 'ledger.json').read_text(encoding='utf-8'))

    def use_new_chats(self, count):
        self.windows = view(*(row(f'new-{number:02d}') for number in range(count)))
        self.wsl = view()
        self.before = {'version': 1, 'threads': {}}
        (self.runtime / 'ledger.json').write_text(json.dumps(self.before), encoding='utf-8')

        def transfer(side, operation, *args):
            if operation == 'export':
                self.exported_id = args[args.index('--id') + 1]
            if operation == 'import':
                imported = row(self.exported_id, body='imported-native')
                return {'ok': True, 'revision': imported['revision'], 'record': imported}
            if operation == 'import-preview':
                return {'ok': True, 'compatible': True}
            return {'ok': True}
        self.endpoint.call.side_effect = transfer

    def operations(self):
        return [call.args[1] for call in self.endpoint.call.call_args_list]

    def test_exact_committed_native_revision_is_checkpointed(self):
        summary = self.execute()
        self.assertEqual(summary['transferred'], 1)
        tracked = self.saved()['threads']['chat-1']
        self.assertEqual(tracked['windows']['revision'], 'new-source/meta-0')
        self.assertEqual(tracked['wsl']['revision'], 'imported-native/meta-0')
        self.assertFalse((self.runtime / 'pending.json').exists())

    def test_incomplete_committed_receipt_preserves_pending_and_checkpoint(self):
        for missing in ('revision', 'content_revision', 'metadata_revision'):
            with self.subTest(missing=missing):
                imported = copy.deepcopy(self.imported_record)
                imported.pop(missing)
                self.endpoint.call.side_effect = lambda side, operation, *args: (
                    {'ok': True, 'record': copy.deepcopy(imported)}
                    if operation == 'import' else ({'ok': True, 'compatible': True} if operation == 'import-preview' else {'ok': True})
                )
                with self.assertRaises(RuntimeError):
                    self.execute()
                self.assertTrue((self.runtime / 'pending.json').is_file())
                self.assertEqual(self.saved(), self.before)
                (self.runtime / 'pending.json').unlink()

    def test_inconsistent_committed_receipt_is_not_checkpointed(self):
        self.endpoint.call.side_effect = lambda side, operation, *args: (
            {'ok': True, 'record': copy.deepcopy(self.imported_record),
             'revision': 'another-revision'}
            if operation == 'import' else ({'ok': True, 'compatible': True} if operation == 'import-preview' else {'ok': True})
        )
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertTrue((self.runtime / 'pending.json').is_file())
        self.assertEqual(self.saved(), self.before)

    def test_existing_import_checks_backend_without_rescanning_histories(self):
        self.execute()
        self.assertEqual([call.args[0] for call in self.endpoint.inspect.call_args_list],
                         ['windows', 'wsl'])
        self.endpoint.backend_status.assert_called_once_with('wsl')

    def test_default_batch_limit_caps_twelve_new_chats_at_ten(self):
        self.use_new_chats(12)
        summary = self.execute()
        self.assertEqual(summary['attempted_transfers'], 10)
        self.assertEqual(summary['transferred'], 10)
        self.assertEqual(summary['remaining_transfers'], 2)
        self.assertEqual(summary['batch_stop_reason'], 'batch_limit')
        self.assertEqual(self.operations().count('export'), 10)
        self.assertEqual(self.operations().count('import'), 10)
        self.assertEqual(len(self.saved()['threads']), 10)
        self.endpoint.backend_status.assert_not_called()

    def test_custom_batch_limit_stops_before_preparing_next_package(self):
        self.use_new_chats(3)
        self.config['max_transfers_per_run'] = 2
        summary = self.execute()
        self.assertEqual(summary['transferred'], 2)
        self.assertEqual(summary['remaining_transfers'], 1)
        self.assertEqual(summary['run_limits']['max_transfers_per_run'], 2)
        self.assertEqual(self.operations(), ['export', 'import-preview', 'import', 'export', 'import-preview', 'import'])
        self.assertFalse((self.runtime / 'pending.json').exists())
        self.assertFalse(any(self.runtime.glob('transfer-*')))

    def test_deadline_during_export_finishes_receipt_before_stopping(self):
        self.use_new_chats(2)
        clock = {'seconds': 0}
        normal_transfer = self.endpoint.call.side_effect

        def slow_export(side, operation, *args):
            result = normal_transfer(side, operation, *args)
            if operation == 'export':
                clock['seconds'] = 121
            return result
        self.endpoint.call.side_effect = slow_export
        with patch.object(sync.time, 'monotonic', side_effect=lambda: clock['seconds']):
            summary = self.execute()
        self.assertEqual(summary['attempted_transfers'], 1)
        self.assertEqual(summary['transferred'], 1)
        self.assertEqual(summary['remaining_transfers'], 1)
        self.assertEqual(summary['batch_stop_reason'], 'time_budget')
        self.assertEqual(summary['elapsed_seconds'], 121)
        self.assertEqual(self.operations(), ['export', 'import-preview', 'import'])
        self.assertEqual(self.saved()['threads']['new-00']['wsl']['revision'],
                         'imported-native/meta-0')
        self.assertFalse((self.runtime / 'pending.json').exists())

    def test_scan_can_use_budget_but_no_transfer_starts_after_deadline(self):
        self.use_new_chats(2)
        clock = {'seconds': 0}

        def inspect(side):
            if side == 'wsl':
                clock['seconds'] = 120
            return copy.deepcopy(self.windows if side == 'windows' else self.wsl)
        self.endpoint.inspect.side_effect = inspect
        with patch.object(sync.time, 'monotonic', side_effect=lambda: clock['seconds']):
            summary = self.execute()
        self.assertEqual(summary['transferred'], 0)
        self.assertEqual(summary['attempted_transfers'], 0)
        self.assertEqual(summary['batch_stop_reason'], 'time_budget')
        self.endpoint.call.assert_not_called()
        self.assertEqual(self.saved(), self.before)

    def test_backend_start_defers_without_import_or_checkpoint_change(self):
        self.endpoint.backend_status.return_value = {
            'backend_running': True, 'backend_detection_ok': True,
        }
        summary = self.execute()
        self.assertEqual(summary['transferred'], 0)
        self.assertEqual(summary['attempted_transfers'], 1)
        self.assertEqual(summary['remaining_transfers'], 1)
        self.assertEqual(summary['deferred'], [
            {'id': 'chat-1', 'reason': 'target_backend_started'},
        ])
        self.assertEqual(self.operations(), ['export', 'import-preview'])
        self.assertEqual(self.saved(), self.before)
        self.assertFalse((self.runtime / 'pending.json').exists())

    def test_transient_source_export_defers_only_that_chat_and_keeps_next_copy(self):
        for code, reason in (('source_active', 'source_busy'), ('source_changed', 'source_changed'),
                             ('thread_missing', 'source_disappeared')):
            with self.subTest(code=code):
                self.endpoint.call.reset_mock()
                self.use_new_chats(2)
                normal_transfer = self.endpoint.call.side_effect

                def resumed_source(side, operation, *args):
                    if operation == 'export' and args[args.index('--id') + 1] == 'new-00':
                        # A partially produced package must still be cleaned up.
                        package = Path(args[args.index('--package') + 1])
                        (package / 'partial.jsonl').write_bytes(b'partial')
                        raise sync.EndpointCallError(side, operation, code, 'Synthetic change')
                    return normal_transfer(side, operation, *args)

                self.endpoint.call.side_effect = resumed_source
                summary = self.execute()
                self.assertEqual(summary['status'], 'completed')
                self.assertEqual(summary['transferred'], 1)
                self.assertEqual(summary['remaining_transfers'], 1)
                self.assertEqual(summary['deferred'], [{'id': 'new-00', 'reason': reason}])
                self.assertNotIn('new-00', self.saved()['threads'])
                self.assertIn('new-01', self.saved()['threads'])
                self.assertEqual(self.operations(), ['export', 'export', 'import-preview', 'import'])
                self.assertFalse((self.runtime / 'pending.json').exists())
                self.assertFalse(any(self.runtime.glob('transfer-*')))

    def test_unknown_or_untyped_source_export_failure_stops_before_target_write(self):
        for error in (sync.EndpointCallError('windows', 'export', 'schema_unsupported', 'Synthetic'),
                      RuntimeError('source_changed'),
                      sync.EndpointCallError('wsl', 'export', 'source_changed', 'Synthetic'),
                      sync.EndpointCallError('windows', 'import', 'source_changed', 'Synthetic')):
            with self.subTest(error=type(error).__name__, code=getattr(error, 'code', None)):
                self.endpoint.call.reset_mock()
                self.use_new_chats(2)
                self.endpoint.call.side_effect = error
                with self.assertRaises(RuntimeError):
                    self.execute()
                self.assertEqual(self.operations(), ['export'])
                self.assertEqual(self.saved(), self.before)
                self.assertFalse((self.runtime / 'pending.json').exists())

    def test_source_change_code_from_import_keeps_pending_and_old_checkpoint(self):
        self.use_new_chats(2)
        normal_transfer = self.endpoint.call.side_effect

        def failed_import(side, operation, *args):
            if operation == 'import':
                raise sync.EndpointCallError(side, operation, 'source_changed', 'Synthetic import failure')
            return normal_transfer(side, operation, *args)

        self.endpoint.call.side_effect = failed_import
        with self.assertRaises(sync.EndpointCallError):
            self.execute()
        self.assertTrue((self.runtime / 'pending.json').is_file())
        self.assertEqual(self.saved(), self.before)
        self.assertEqual(self.operations(), ['export', 'import-preview', 'import'])

    def test_resolved_problems_refresh_attention_to_empty_snapshot(self):
        (self.runtime / 'attention.json').write_text(json.dumps({
            'blocked': [{'id': 'stale', 'reason': 'both_sides_changed'}],
        }), encoding='utf-8')
        self.execute()
        attention = json.loads((self.runtime / 'attention.json').read_text(encoding='utf-8'))
        self.assertEqual(attention['blocked'], [])
        self.assertEqual(attention['deferred'], [])
        self.assertEqual(attention['endpoint_skipped'], {'windows': [], 'wsl': []})

    def test_source_continuation_during_transport_stays_pending(self):
        def transfer(side, operation, *args):
            if operation == 'import':
                self.windows = view(row(body='next-source'))
                self.wsl = view(copy.deepcopy(self.imported_record))
                return {'ok': True, 'record': copy.deepcopy(self.imported_record)}
            return {'ok': True}
        self.endpoint.call.side_effect = transfer
        self.execute()
        self.assertEqual(self.saved()['threads']['chat-1']['windows']['revision'], 'new-source/meta-0')
        plan = sync.build_plan(self.windows, self.wsl, self.saved())
        self.assertEqual(plan['actions'][0]['source'], 'windows')
        self.assertEqual(plan['actions'][0]['mode'], 'full')

    def test_target_edit_after_commit_is_not_consumed_by_checkpoint(self):
        def transfer(side, operation, *args):
            if operation == 'import':
                self.wsl = view(row(body='native-target-next'))
                return {'ok': True, 'record': copy.deepcopy(self.imported_record)}
            return {'ok': True}
        self.endpoint.call.side_effect = transfer
        self.execute()
        self.assertEqual(self.saved()['threads']['chat-1']['wsl']['revision'], 'imported-native/meta-0')
        plan = sync.build_plan(self.windows, self.wsl, self.saved())
        self.assertEqual(plan['actions'][0]['source'], 'wsl')

    def test_dry_run_does_not_change_checkpoint_or_call_mutations(self):
        self.args.dry_run = True
        before = (self.runtime / 'ledger.json').read_bytes()
        summary = self.execute()
        self.assertEqual(summary['planned_transfers'], 1)
        self.assertEqual((self.runtime / 'ledger.json').read_bytes(), before)
        self.endpoint.call.assert_not_called()

    def test_pending_prior_import_stops_new_writes(self):
        (self.runtime / 'pending.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'unfinished prior import'):
            self.execute()
        self.endpoint.call.assert_not_called()

    def test_failed_import_preserves_pending_journal_and_old_checkpoint(self):
        def transfer(side, operation, *args):
            if operation == 'import':
                raise RuntimeError('simulated interruption')
            return {'ok': True}
        self.endpoint.call.side_effect = transfer
        with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
            self.execute()
        self.assertTrue((self.runtime / 'pending.json').is_file())
        self.assertEqual(self.saved(), self.before)

    def test_stopped_wsl_skips_without_creating_endpoint(self):
        with patch.object(sync, 'wsl_running', return_value=False), \
             patch.object(sync, 'Endpoints') as endpoint:
            result = sync.execute(self.config, self.args, self.runtime, MagicMock())
        self.assertEqual(result['status'], 'skipped_wsl_stopped')
        endpoint.assert_not_called()

    def test_boolean_ledger_version_is_rejected_before_data_mutations(self):
        malformed = copy.deepcopy(self.before)
        malformed['version'] = True
        (self.runtime / 'ledger.json').write_text(json.dumps(malformed), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'ledger version'):
            self.execute()
        self.endpoint.call.assert_not_called()
        self.assertEqual(self.saved(), malformed)


class ReadOnlyAndReportingReview(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='report-review-')
        self.script_dir = Path(self.directory.name).resolve()
        self.runtime = self.script_dir / 'runtime'
        self.config_path = self.script_dir / 'config.json'
        self.config = {
            'windows_home': str(self.script_dir / 'windows-codex'),
            'wsl_distro': 'Ubuntu', 'wsl_user': 'test-user',
            'wsl_home': '/srv/codex-fixture/test-home/.codex', 'python_windows': sys.executable,
        }
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.endpoint = MagicMock()
        self.endpoint.call.return_value = {
            'checks': [{'code': 'native_database', 'status': 'ok', 'message': '读取正常'}],
        }

    def tearDown(self):
        self.directory.cleanup()

    def populate_runtime(self):
        self.runtime.mkdir()
        (self.runtime / 'ledger.json').write_text(json.dumps(
            sync.baseline(view(row()), view(row()))), encoding='utf-8')
        (self.runtime / 'last-run.json').write_text(json.dumps({
            'last_checked_at': '2026-10-10T01:00:00Z',
            'last_transfer_at': '2026-10-10T00:00:00Z',
            'summary': {'status': 'completed', 'transferred': 1},
        }), encoding='utf-8')
        (self.runtime / 'attention.json').write_text(json.dumps({
            'blocked': [], 'endpoint_skipped': {'windows': [], 'wsl': []},
        }), encoding='utf-8')

    def snapshot(self):
        return {str(path.relative_to(self.script_dir)): path.read_bytes()
                for path in self.script_dir.rglob('*') if path.is_file()}

    def invoke_main(self, *arguments):
        output = io.StringIO()
        with patch.object(sync, 'SCRIPT_DIR', self.script_dir), redirect_stdout(output):
            code = sync.main(['--config', str(self.config_path), *arguments])
        return code, output.getvalue()

    def test_status_reads_cached_files_without_endpoints_processes_or_writes(self):
        self.populate_runtime()
        before = self.snapshot()
        with patch.object(sync, 'Endpoints', side_effect=AssertionError('endpoint started')), \
             patch.object(sync, 'wsl_running', side_effect=AssertionError('WSL queried')), \
             patch.object(sync.subprocess, 'run', side_effect=AssertionError('process started')), \
             patch.object(sync, 'atomic_json', side_effect=AssertionError('file written')):
            summary = sync.read_status(self.runtime)
        self.assertTrue(summary['cached_only'])
        self.assertTrue(summary['initialized'])
        self.assertEqual(summary['tracked_chats'], 1)
        self.assertEqual(summary['last_run']['summary']['transferred'], 1)
        self.assertEqual(self.snapshot(), before)

    def test_status_preserves_corrupt_cached_file_and_reports_warning(self):
        self.populate_runtime()
        (self.runtime / 'attention.json').write_bytes(b'{broken')
        before = self.snapshot()
        summary = sync.read_status(self.runtime)
        self.assertIsNone(summary['attention'])
        self.assertTrue(any('attention.json' in warning for warning in summary['warnings']))
        self.assertEqual(self.snapshot(), before)

    def test_boolean_cached_ledger_version_is_not_initialized(self):
        self.populate_runtime()
        malformed = sync.baseline(view(row()), view(row()))
        malformed['version'] = True
        (self.runtime / 'ledger.json').write_text(json.dumps(malformed), encoding='utf-8')
        summary = sync.read_status(self.runtime)
        self.assertFalse(summary['initialized'])
        self.assertEqual(summary['tracked_chats'], 0)
        self.assertTrue(summary['warnings'])

    def test_corrupt_cached_report_shapes_do_not_break_human_status(self):
        self.populate_runtime()
        (self.runtime / 'last-run.json').write_text(json.dumps({'summary': []}), encoding='utf-8')
        (self.runtime / 'attention.json').write_text(json.dumps({'blocked': []}), encoding='utf-8')
        before = self.snapshot()
        summary = sync.read_status(self.runtime)
        self.assertTrue(summary['warnings'])
        self.assertIsInstance(sync.human_summary(summary), str)
        self.assertEqual(self.snapshot(), before)

    def test_main_status_needs_no_config_runtime_logger_or_lock(self):
        self.config_path.unlink()
        with patch.object(sync, 'Endpoints', side_effect=AssertionError('endpoint started')), \
             patch.object(sync, 'wsl_running', side_effect=AssertionError('WSL queried')), \
             patch.object(sync, 'run_lock', side_effect=AssertionError('lock created')):
            code, output = self.invoke_main('--status', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'cached_status')
        self.assertFalse(self.runtime.exists())

    def test_default_status_output_is_readable_chinese_and_json_is_opt_in(self):
        code, output = self.invoke_main('--status')
        self.assertEqual(code, 0)
        self.assertIn('同步状态', output)
        self.assertFalse(output.lstrip().startswith('{'))
        code, output = self.invoke_main('--status', '--json')
        self.assertEqual(code, 0)
        self.assertIsInstance(json.loads(output), dict)

    def test_doctor_running_sides_only_call_native_read_only_operation(self):
        self.populate_runtime()
        before = self.snapshot()
        with patch.object(sync, 'Endpoints', return_value=self.endpoint), \
             patch.object(sync, 'wsl_running', return_value=True), \
             patch.object(sync, 'atomic_json', side_effect=AssertionError('file written')):
            summary = sync.diagnose(self.config, self.runtime)
        self.assertTrue(summary['read_only'])
        self.assertTrue(summary['healthy'])
        self.assertEqual([call.args for call in self.endpoint.call.call_args_list],
                         [('windows', 'doctor'), ('wsl', 'doctor')])
        self.endpoint.inspect.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_doctor_stopped_wsl_checks_windows_but_never_launches_wsl_endpoint(self):
        before = self.snapshot()
        with patch.object(sync, 'Endpoints', return_value=self.endpoint), \
             patch.object(sync, 'wsl_running', return_value=False), \
             patch.object(sync, 'atomic_json', side_effect=AssertionError('file written')):
            summary = sync.diagnose(self.config, self.runtime)
        self.assertEqual(summary['endpoints']['wsl']['status'], 'skipped_wsl_stopped')
        self.endpoint.call.assert_called_once_with('windows', 'doctor')
        self.assertEqual(self.snapshot(), before)

    def test_doctor_failed_inventory_and_native_error_are_visible_without_writes(self):
        before = self.snapshot()
        self.endpoint.call.side_effect = RuntimeError('database unavailable')
        with patch.object(sync, 'Endpoints', return_value=self.endpoint), \
             patch.object(sync, 'wsl_running', side_effect=RuntimeError('inventory failed')), \
             patch.object(sync, 'atomic_json', side_effect=AssertionError('file written')):
            summary = sync.diagnose(self.config, self.runtime)
        self.assertFalse(summary['healthy'])
        self.assertTrue(any(check['status'] == 'error' for check in summary['checks']))
        self.endpoint.call.assert_called_once_with('windows', 'doctor')
        self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_main_doctor_creates_no_runtime_logs_or_lock(self):
        before = self.snapshot()
        with patch.object(sync, 'Endpoints', return_value=self.endpoint), \
             patch.object(sync, 'wsl_running', return_value=False), \
             patch.object(sync, 'run_lock', side_effect=AssertionError('lock created')):
            code, output = self.invoke_main('--doctor', '--json')
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['read_only'])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.runtime.exists())

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_invalid_config_json_is_logged_without_querying_or_mutating_data(self):
        self.config_path.write_bytes(b'{broken-json')
        with patch.object(sync, 'wsl_running', side_effect=AssertionError('WSL queried')), \
             patch.object(sync, 'Endpoints', side_effect=AssertionError('endpoint started')):
            code, output = self.invoke_main('--json')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['status'], 'error')
        saved = json.loads((self.runtime / 'last-run.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['summary']['status'], 'error')
        self.assertIn('ERROR', (self.runtime / 'sync.log').read_text(encoding='utf-8'))
        self.assertFalse((self.runtime / 'ledger.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_invalid_budget_value_is_logged_before_any_endpoint_execution(self):
        self.config['max_transfers_per_run'] = True
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(sync, 'Endpoints', side_effect=AssertionError('endpoint started')):
            code, output = self.invoke_main('--json')
        self.assertEqual(code, 1)
        self.assertIn('max_transfers_per_run', json.loads(output)['error'])
        self.assertTrue((self.runtime / 'sync.log').is_file())
        self.assertTrue((self.runtime / 'last-run.json').is_file())

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_main_dry_run_preserves_last_run_and_ledger(self):
        self.populate_runtime()
        ledger_before = (self.runtime / 'ledger.json').read_bytes()
        report_before = (self.runtime / 'last-run.json').read_bytes()
        result = {'status': 'dry_run', 'planned_transfers': 0, 'transferred': 0,
                  'remaining_transfers': 0, 'blocked': [], 'endpoint_skipped': {}}
        with patch.object(sync, 'execute', return_value=result), \
             patch.object(sync, 'save_run_report', side_effect=AssertionError('report changed')):
            code, output = self.invoke_main('--dry-run', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'dry_run')
        self.assertEqual((self.runtime / 'ledger.json').read_bytes(), ledger_before)
        self.assertEqual((self.runtime / 'last-run.json').read_bytes(), report_before)

    def test_skipped_run_preserves_last_successful_transfer_time(self):
        self.populate_runtime()
        sync.save_run_report(self.runtime, {'status': 'skipped_wsl_stopped'})
        summary = json.loads((self.runtime / 'last-run.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['last_transfer_at'], '2026-10-10T00:00:00Z')
        self.assertEqual(summary['summary']['status'], 'skipped_wsl_stopped')

    @unittest.skipUnless(os.name == 'nt', 'The controller main entry point requires Windows')
    def test_partial_run_failure_reports_completed_checkpoint_and_current_pending(self):
        self.populate_runtime()
        (self.runtime / 'ledger.json').write_text(json.dumps({'version': 1, 'threads': {}}), encoding='utf-8')
        self.endpoint.inspect.side_effect = lambda side: (
            view(row('new-00'), row('new-01')) if side == 'windows' else view()
        )

        def transfer(side, operation, *args):
            if operation == 'export':
                self.exported_id = args[args.index('--id') + 1]
            if operation == 'import':
                if self.exported_id == 'new-01':
                    raise RuntimeError('second chat interrupted')
                imported = row(self.exported_id, body='native-imported')
                return {'ok': True, 'record': imported, 'revision': imported['revision']}
            return {'ok': True}
        self.endpoint.call.side_effect = transfer
        with patch.object(sync, 'wsl_running', return_value=True), \
             patch.object(sync, 'Endpoints', return_value=self.endpoint), \
             patch.object(sync, 'linux_mount_path', side_effect=lambda value: str(value)):
            code, output = self.invoke_main('--json')
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report['status'], 'error')
        self.assertEqual(report['transferred'], 1)
        self.assertEqual(report['attempted_transfers'], 2)
        self.assertEqual(report['remaining_transfers'], 1)
        ledger = json.loads((self.runtime / 'ledger.json').read_text(encoding='utf-8'))
        self.assertEqual(set(ledger['threads']), {'new-00'})
        pending = json.loads((self.runtime / 'pending.json').read_text(encoding='utf-8'))
        self.assertEqual(pending['id'], 'new-01')
        saved = json.loads((self.runtime / 'last-run.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['summary']['transferred'], 1)
        self.assertNotEqual(saved['last_transfer_at'], '2026-10-10T00:00:00Z')

    def test_unknown_backend_detection_fails_closed_without_history_scan(self):
        endpoint = sync.Endpoints(self.config, self.script_dir)
        with patch.object(endpoint, 'call', return_value={'backend_running': False}) as call:
            report = endpoint.backend_status('wsl')
        self.assertTrue(report['backend_running'])
        call.assert_called_once_with('wsl', 'backend-status')

    def test_wsl_stops_after_initial_scan_no_endpoint_process_is_launched(self):
        endpoint = sync.Endpoints(self.config, self.script_dir)
        with patch.object(sync, 'wsl_running', return_value=False), \
             patch.object(sync.subprocess, 'run') as process:
            with self.assertRaisesRegex(RuntimeError, 'WSL stopped'):
                endpoint.call('wsl', 'backend-status')
        process.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
