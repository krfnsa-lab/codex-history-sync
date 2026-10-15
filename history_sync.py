"""One-shot Windows/WSL Codex history synchronization coordinator.

Run by the Windows Task Scheduler or manually. This process never sleeps in a
loop and never starts a stopped WSL distribution. Native endpoints own SQLite
reads/writes, so live SQLite is never opened through the opposite OS mount.
"""
import argparse
import ctypes
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from history_progress import ProgressWriter

VERSION = 1
SOFTWARE_VERSION = '2.2.0'
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MAX_TRANSFERS = 10
DEFAULT_RUN_BUDGET = 120


def records(view):
    return {row['id']: row for row in view.get('threads', [])}


def chat_counts(row):
    cache = row.get('cache', {})
    counts = row.get('counts', {})
    items = row.get('item_count', counts.get('items',
        cache.get('thread_items', {}).get('row_count', 0) +
        cache.get('thread_realtime_items', {}).get('row_count', 0)))
    turns = row.get('turn_count', counts.get('turns',
        cache.get('thread_turns', {}).get('row_count', 0)))
    return items, turns


def is_busy(row, quiet_seconds):
    quiet = row.get('quiet_seconds')
    return bool(row.get('active')) or quiet is None or quiet < quiet_seconds


def baseline(windows, wsl):
    win, lin = records(windows), records(wsl)
    return {'version': VERSION, 'threads': {
        tid: {'windows': win.get(tid), 'wsl': lin.get(tid),
              'body_diverged': bool(tid in win and tid in lin and win[tid]['content_revision'] != lin[tid]['content_revision'])}
        for tid in sorted(win.keys() | lin.keys())}}


def longer_conflict_selection(win, lin):
    """Compare indexed, complete histories; never invent counts from a cache."""
    comparison = {'metric': ['turn_count', 'item_count']}
    for side, row in (('windows', win), ('wsl', lin)):
        values = {key: row.get(key) for key in ('turn_count', 'item_count')}
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in values.values()):
            return None, 'conflict_counts_unavailable', comparison
        comparison[side] = values
        size = row.get('transcript_size')
        if row.get('transcript_complete') is not True or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            return None, 'conflict_history_incomplete', comparison
        if values['item_count'] == 0:
            if row.get('transcript_message_hint') or (row.get('history_mode') == 'legacy' and row.get('has_user_event')):
                return None, 'conflict_history_unindexed', comparison
            return None, 'conflict_history_empty', comparison
        if values['turn_count'] == 0:
            return None, 'conflict_history_empty', comparison
    win_count = (comparison['windows']['turn_count'], comparison['windows']['item_count'])
    lin_count = (comparison['wsl']['turn_count'], comparison['wsl']['item_count'])
    if win_count == lin_count:
        return None, 'conflict_counts_equal', comparison
    source = 'windows' if win_count > lin_count else 'wsl'
    comparison['selected_source'] = source
    return source, None, comparison


def build_plan(windows, wsl, ledger, quiet_seconds=60, conflict_resolution='manual'):
    """Pure planning with an explicit opt-in to select the longer conflict."""
    if conflict_resolution not in ('manual', 'prefer_longer'):
        raise ValueError('conflict_resolution must be manual or prefer_longer')
    views = {'windows': windows, 'wsl': wsl}
    states = {side: records(view) for side, view in views.items()}
    unavailable = {entry['id'] for view in views.values()
                   for entry in view.get('skipped', []) if entry.get('id')}
    actions, blocked, pairs, decisions = [], [], [], []
    for tid in sorted(states['windows'].keys() | states['wsl'].keys()):
        resolution_info = {}
        decision = None
        if tid in unavailable:
            blocked.append({'id': tid, 'reason': 'endpoint_record_unavailable'})
            continue
        win, lin = states['windows'].get(tid), states['wsl'].get(tid)
        if any(row is not None and (row.get('transcript_complete') is False or
                ('transcript_size' in row and row['transcript_size'] is None)) for row in (win, lin)):
            blocked.append({'id': tid, 'reason': 'unreadable_transcript'})
            continue
        old = ledger.get('threads', {}).get(tid)
        if win is None or lin is None:
            source = 'windows' if win else 'wsl'
            target = 'wsl' if win else 'windows'
            row = states[source][tid]
            # A replica that disappeared is preserved as a deletion/conflict;
            # never silently recreate or delete it on the other side.
            if old and old.get(target) is not None:
                blocked.append({'id': tid, 'reason': 'replica_missing'})
                continue
            mode = 'full'
        else:
            if not old or old.get('windows') is None or old.get('wsl') is None:
                pairs.append(tid)
                if win['content_revision'] != lin['content_revision']:
                    blocked.append({'id': tid, 'reason': 'initial_body_difference'})
                continue
            changed = [side for side in states if states[side][tid]['revision'] != old[side]['revision']]
            known_divergence = bool(old.get('body_diverged'))
            conflict_reason = 'both_sides_changed' if len(changed) == 2 else (
                'existing_body_difference' if known_divergence else None)
            if conflict_resolution == 'prefer_longer' and conflict_reason:
                source, reason, comparison = longer_conflict_selection(win, lin)
                resolution_info = {'conflict_resolution': 'prefer_longer',
                                   'conflict_reason': conflict_reason, 'comparison': comparison}
                if reason:
                    blocked.append({'id': tid, 'reason': reason, **resolution_info})
                    continue
                target = 'wsl' if source == 'windows' else 'windows'
                resolution_info.update(source=source, target=target)
                source_counts, target_counts = comparison[source], comparison[target]
                decision = {'id': tid, 'source': source, 'target': target,
                            'source_counts': {'turns': source_counts['turn_count'], 'items': source_counts['item_count']},
                            'target_counts': {'turns': target_counts['turn_count'], 'items': target_counts['item_count']},
                            'reason': 'more_turns' if source_counts['turn_count'] > target_counts['turn_count'] else 'more_items',
                            'status': 'waiting'}
                decisions.append(decision)
                row = states[source][tid]
                mode = 'full'
            elif not changed:
                continue
            elif len(changed) == 2:
                blocked.append({'id': tid, 'reason': 'both_sides_changed'})
                continue
            else:
                source = changed[0]
                target = 'wsl' if source == 'windows' else 'windows'
                row = states[source][tid]
                body_changed = row['content_revision'] != old[source]['content_revision']
                if body_changed and old.get('body_diverged'):
                    blocked.append({'id': tid, 'reason': 'existing_body_difference'})
                    continue
                mode = 'full' if body_changed else 'metadata'
            if views[target].get('backend_running', True):
                if decision is not None:
                    decision['waiting_reason'] = 'target_backend_running'
                blocked.append({'id': tid, 'reason': 'target_backend_running', **resolution_info})
                continue
            target_row = states[target][tid]
            if is_busy(target_row, quiet_seconds):
                if decision is not None:
                    decision['waiting_reason'] = 'target_busy'
                blocked.append({'id': tid, 'reason': 'target_busy', **resolution_info})
                continue
        if is_busy(row, quiet_seconds):
            if decision is not None:
                decision['waiting_reason'] = 'source_busy'
            blocked.append({'id': tid, 'reason': 'source_busy', **resolution_info})
            continue
        items, turns = chat_counts(row)
        unindexed = row.get('transcript_message_hint') or (row.get('history_mode') == 'legacy' and row.get('has_user_event') and row.get('transcript_size', 0))
        if mode == 'full' and items == 0 and unindexed:
            blocked.append({'id': tid, 'reason': 'unindexed_history'})
            continue
        if mode == 'full' and items == 0 and turns == 0:
            blocked.append({'id': tid, 'reason': 'empty_conversation'})
            continue
        target_row = states[target].get(tid)
        if decision is not None:
            decision['status'] = 'ready'
        actions.append({'id': tid, 'source': source, 'target': target, 'mode': mode,
                        'source_revision': row['revision'],
                        'target_revision': target_row['revision'] if target_row else 'absent',
                        **resolution_info})
    plan = {'actions': actions, 'blocked': blocked, 'baseline_pairs': pairs}
    if conflict_resolution == 'prefer_longer':
        plan['conflict_decisions'] = decisions
    return plan


def decode_output(raw):
    if raw.startswith(b'\xff\xfe') or b'\x00' in raw[:100]:
        return raw.decode('utf-16', errors='replace') if raw.startswith(b'\xff\xfe') else raw.decode('utf-16-le', errors='replace')
    return raw.decode('utf-8-sig', errors='replace')


def wsl_running(distro):
    result = subprocess.run(['wsl.exe', '--list', '--running', '--quiet'], capture_output=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('Cannot query running WSL distributions')
    return distro in [line.replace('\x00', '').strip() for line in decode_output(result.stdout).splitlines()]


def linux_mount_path(path):
    path = PureWindowsPath(str(Path(path).resolve()))
    if len(path.drive) != 2 or path.drive[1] != ':':
        raise ValueError('Place synchronization files on a Windows drive')
    return '/mnt/' + path.drive[0].lower() + '/' + '/'.join(path.parts[1:])


class EndpointCallError(RuntimeError):
    """Keep native error codes separate from messages for narrow retry rules."""
    def __init__(self, side, operation, code, message):
        super().__init__(f'{side} {operation}: {message}')
        self.side, self.operation = side, operation
        self.code = code if isinstance(code, str) else 'endpoint_error'


class Endpoints:
    def __init__(self, config, script_dir):
        self.config = config
        self.script = script_dir / 'history_endpoint.py'
        self.reporter = None

    def call(self, side, operation, *args):
        progress_args = []
        reporter = self.reporter
        if reporter is not None and reporter.enabled:
            try:
                phases = {'inspect': 'checking_' + side, 'backend-status': 'checking_backend',
                          'export': 'exporting', 'import-preview': 'previewing',
                          'import': 'writing', 'path-preview': 'repairing'}
                operation_id = reporter.begin_operation(phases.get(operation, 'reading'), side=side)
                if operation_id:
                    progress_path = reporter.path.parent / 'progress-detail.json'
                    progress_args = ['--progress-file', str(progress_path) if side == 'windows' else linux_mount_path(progress_path),
                                     '--progress-run-id', reporter.run_id,
                                     '--progress-operation-id', operation_id, '--progress-side', side]
            except Exception:
                # Path conversion or diagnostic failures must never add a new
                # failure condition to the synchronization protocol.
                progress_args = []
        if side == 'windows':
            cmd = [self.config['python_windows'], str(self.script), operation, '--home', self.config['windows_home']]
        else:
            # Recheck without starting WSL if it stopped after the first scan.
            if not wsl_running(self.config['wsl_distro']):
                raise RuntimeError('WSL stopped during this run; deferred until the next run')
            cmd = ['wsl.exe', '--distribution', self.config['wsl_distro'], '--user', self.config['wsl_user'], '--exec', self.config.get('python_wsl', '/usr/bin/python3'), linux_mount_path(self.script), operation, '--home', self.config['wsl_home']]
        cmd += ['--quiet-seconds', str(self.config.get('quiet_seconds', 60)), *map(str, args), *progress_args]
        result = subprocess.run(cmd, capture_output=True, timeout=self.config.get('operation_timeout_seconds', 120),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            value = json.loads(decode_output(result.stdout))
        except (json.JSONDecodeError, UnicodeError):
            raise RuntimeError(f'{side} {operation}: invalid endpoint response') from None
        if not isinstance(value, dict):
            raise RuntimeError(f'{side} {operation}: invalid endpoint response')
        if result.returncode or value.get('error') or value.get('ok') is False:
            reason = value.get('message', value.get('error', value.get('code', 'failed')))
            raise EndpointCallError(side, operation, value.get('code'), reason)
        return value

    def inspect(self, side):
        value = self.call(side, 'inspect')
        if value.get('backend_detection_ok') is not True:
            value['backend_running'] = True
        return value

    def backend_status(self, side):
        value = self.call(side, 'backend-status')
        if value.get('backend_detection_ok') is not True:
            value['backend_running'] = True
        return value


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.new')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def choice_token(thread_id, source_records):
    value = {'id': thread_id, **{side: source_records[side]['revision'] for side in ('windows', 'wsl')}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def read_conflict_choices(runtime):
    path = runtime / 'conflict-choices.json'
    if not path.exists():
        return {'version': 1, 'choices': {}}
    value = json.loads(path.read_text(encoding='utf-8'))
    if type(value.get('version')) is not int or value['version'] != 1 or not isinstance(value.get('choices'), dict):
        raise RuntimeError('Unsupported manual conflict-choice store')
    for tid, choice in value['choices'].items():
        if (not isinstance(choice, dict) or choice.get('id') != tid
                or choice.get('keep_side') not in ('windows', 'wsl')
                or choice.get('status') not in ('pending', 'needs_confirmation', 'resolved')):
            raise RuntimeError('Invalid manual conflict choice')
        if str(uuid.UUID(choice.get('choice_id', ''))) != choice['choice_id']:
            raise RuntimeError('Invalid manual conflict-choice identity')
        expected = choice.get('expected', {})
        if set(expected) != {'windows', 'wsl'} or any(not isinstance(v, str) or not v for v in expected.values()):
            raise RuntimeError('Manual conflict choice has no exact source versions')
    return value


def conflict_choice_package(runtime, choice, side):
    root = runtime / 'conflict-choices'
    folder = root / choice['choice_id'] / side
    if folder.is_symlink() or not folder.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Unexpected manual-choice backup path')
    hashes = choice.get('package_hashes', {}).get(side, {})
    if set(hashes) != {'meta.json', 'history.sqlite', 'transcript.jsonl'}:
        raise RuntimeError('Manual choice is missing a complete private backup')
    for name, expected in hashes.items():
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Manual-choice backup is missing or unsafe')
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(512 * 1024), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != expected:
            raise RuntimeError('Manual-choice backup changed; preserved records require review')
    meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
    if meta.get('thread', {}).get('id') != choice['id'] or meta.get('source_revision', {}).get('revision') != choice['expected'][side]:
        raise RuntimeError('Manual-choice backup does not match the confirmed version')
    return folder


def add_manual_choices(plan, views, runtime, quiet_seconds):
    """Use explicit exact-version decisions without a count-based overwrite."""
    store = read_conflict_choices(runtime)
    states = {side: records(view) for side, view in views.items()}
    decisions, updates = [], {}
    for tid, choice in store['choices'].items():
        if choice['status'] == 'resolved':
            continue
        plan['actions'] = [a for a in plan['actions'] if a['id'] != tid]
        plan['blocked'] = [a for a in plan['blocked'] if a['id'] != tid]
        plan['baseline_pairs'] = [value for value in plan['baseline_pairs'] if value != tid]
        if 'conflict_decisions' in plan:
            plan['conflict_decisions'] = [value for value in plan['conflict_decisions'] if value['id'] != tid]
        rows = {side: states[side].get(tid) for side in states}
        reason = None
        if choice['status'] == 'needs_confirmation' or any(
                row is None or row['revision'] != choice['expected'][side] for side, row in rows.items()):
            reason = 'manual_choice_stale'
            updates[tid] = {**choice, 'status': 'needs_confirmation'}
        elif any(is_busy(row, quiet_seconds) for row in rows.values()):
            reason = 'source_busy'
        elif any(row.get('transcript_complete') is not True or row.get('item_count', 0) <= 0 for row in rows.values()):
            reason = 'unreadable_transcript'
        target = 'wsl' if choice['keep_side'] == 'windows' else 'windows'
        if reason is None and views[target].get('backend_running', True):
            reason = 'target_backend_running'
        decision = {'id': tid, 'choice_id': choice['choice_id'], 'keep_side': choice['keep_side'],
                    'status': 'waiting' if reason else 'ready'}
        decisions.append(decision)
        if reason:
            decision['reason'] = reason
            plan['blocked'].append({'id': tid, 'reason': reason, 'conflict_resolution': 'manual_keep'})
            continue
        for side in ('windows', 'wsl'):
            conflict_choice_package(runtime, choice, side)
        plan['actions'].append({'id': tid, 'source': choice['keep_side'], 'target': target, 'mode': 'full',
                                'source_revision': choice['expected'][choice['keep_side']],
                                'target_revision': choice['expected'][target],
                                'conflict_resolution': 'manual_keep', 'choice_id': choice['choice_id']})
    if decisions:
        plan['manual_choices'] = decisions
    return store, updates


@contextmanager
def run_lock(path):
    import msvcrt
    with path.open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _tracked_operation(function, kind):
    def run(config, args, runtime, log, progress=None):
        # The caller owns run.lock. Dry-run and initialization preserve their
        # existing telemetry-free semantics, including no progress-file writes.
        reporter = None
        if not args.dry_run and not getattr(args, 'initialize', False):
            reporter = getattr(args, 'progress_writer', None)
            if not isinstance(reporter, ProgressWriter):
                reporter = ProgressWriter(runtime / 'progress.json', kind=kind,
                    run_id=getattr(args, 'progress_run_id', None), request_id=getattr(args, 'progress_request_id', None))
        if reporter is not None:
            reporter.update('starting', processed_chats=0, transferred_chats=0, deferred_chats=0)
        try:
            result = function(config, args, runtime, log, progress, reporter=reporter)
            if reporter is not None:
                transferred = result.get('transferred', result.get('repaired', 0))
                deferred = len(result.get('deferred', []))
                remaining = result.get('remaining_transfers', 0)
                blocked = len(result.get('blocked', []))
                unavailable = any(result.get('endpoint_skipped', {}).values())
                waiting = (result.get('status') == 'skipped_wsl_stopped' or remaining or blocked
                           or unavailable or result.get('batch_stop_reason'))
                fields = {'processed_chats': result.get('inspected_repairs', transferred + deferred),
                          'transferred_chats': transferred,
                          'deferred_chats': remaining + blocked}
                if result.get('status') == 'skipped_wsl_stopped':
                    fields['reason'] = 'wsl_stopped'
                elif waiting:
                    fields['reason'] = result.get('batch_stop_reason') or 'deferred'
                reporter.finish('waiting' if waiting else 'completed', **fields)
            return result
        except BaseException:
            if reporter is not None:
                reporter.finish('error', reason='task_failed')
            raise
    return run


def _execute(config, args, runtime, log, progress=None, reporter=None):
    started = time.monotonic()
    maximum = config.get('max_transfers_per_run', DEFAULT_MAX_TRANSFERS)
    budget = config.get('run_budget_seconds', DEFAULT_RUN_BUDGET)
    limits = {'max_transfers_per_run': maximum, 'run_budget_seconds': budget}

    def finish(value):
        value['elapsed_seconds'] = round(time.monotonic() - started, 3)
        value['run_limits'] = limits
        return value

    if not wsl_running(config['wsl_distro']):
        if args.initialize:
            raise RuntimeError('Start the specified WSL distribution before initializing')
        return finish({'status': 'skipped_wsl_stopped'})
    endpoint = Endpoints(config, SCRIPT_DIR)
    endpoint.reporter = reporter
    if reporter is not None:
        reporter.update('checking_windows')
    windows = endpoint.inspect('windows')
    if reporter is not None:
        reporter.update('checking_wsl')
    wsl = endpoint.inspect('wsl')
    states = {'windows': records(windows), 'wsl': records(wsl)}
    ledger_path = runtime / 'ledger.json'
    if args.initialize:
        if ledger_path.exists():
            raise RuntimeError('Baseline already exists; preserve it instead of reinitializing')
        value = baseline(windows, wsl)
        atomic_json(ledger_path, value)
        return finish({'status': 'initialized', 'tracked_chats': len(value['threads']), 'different_existing_bodies': sum(x['body_diverged'] for x in value['threads'].values())})
    initialized = ledger_path.exists()
    if not initialized and not args.dry_run:
        raise RuntimeError('Run --initialize once before enabling synchronization')
    ledger = json.loads(ledger_path.read_text(encoding='utf-8')) if initialized else baseline(windows, wsl)
    if isinstance(ledger.get('version'), bool) or ledger.get('version') != VERSION or not isinstance(ledger.get('threads'), dict):
        raise RuntimeError('Unsupported synchronization ledger version')
    if reporter is not None:
        reporter.update('planning')
    plan = build_plan(windows, wsl, ledger, config.get('quiet_seconds', 60),
                      config.get('conflict_resolution', 'manual'))
    choice_store, choice_updates = add_manual_choices(plan, {'windows': windows, 'wsl': wsl}, runtime,
                                                     config.get('quiet_seconds', 60))
    if reporter is not None:
        reporter.update('planning', total_chats=len(plan['actions']),
                        deferred_chats=len(plan['blocked']))
    summary = progress if progress is not None else {}
    summary.update({'status': 'dry_run' if args.dry_run else 'completed', 'initialization_required': not initialized,
               'planned_transfers': len(plan['actions']), 'blocked': plan['blocked'], 'transferred': 0,
               'attempted_transfers': 0, 'remaining_transfers': len(plan['actions']),
               'deferred': [], 'batch_stop_reason': None,
               'run_limits': limits,
               'inspected_chats': {side: len(rows) for side, rows in states.items()},
               'endpoint_skipped': {'windows': windows.get('skipped', []), 'wsl': wsl.get('skipped', [])}})
    if 'conflict_decisions' in plan:
        summary['conflict_decisions'] = plan['conflict_decisions']
        summary['resolved_conflicts'] = 0
        summary['conflict_results'] = []
    if 'manual_choices' in plan:
        summary['manual_choices'] = plan['manual_choices']
        summary.setdefault('resolved_conflicts', 0)
        summary.setdefault('conflict_results', [])
    if args.dry_run:
        summary['actions'] = plan['actions']
        return finish(summary)
    # A crash after an import but before its ledger checkpoint must block
    # further automatic writes, rather than misidentify the replica as a user edit.
    journal = runtime / 'pending.json'
    if journal.exists():
        raise RuntimeError('An unfinished prior import exists; review runtime/pending.json before continuing')
    if choice_updates:
        choice_store['choices'].update(choice_updates)
        atomic_json(runtime / 'conflict-choices.json', choice_store)
    for tid in plan['baseline_pairs']:
        a, b = states['windows'][tid], states['wsl'][tid]
        ledger['threads'][tid] = {'windows': a, 'wsl': b, 'body_diverged': a['content_revision'] != b['content_revision']}
    if plan['baseline_pairs']:
        atomic_json(ledger_path, ledger)
    for action in plan['actions']:
        # Soft limits apply between transfers. An already started transfer must
        # reach its committed receipt and checkpoint, even after the deadline.
        if summary['attempted_transfers'] >= maximum:
            summary['batch_stop_reason'] = 'batch_limit'
            break
        if time.monotonic() - started >= budget:
            summary['batch_stop_reason'] = 'time_budget'
            break
        summary['attempted_transfers'] += 1
        if reporter is not None:
            reporter.update('exporting', current_index=summary['attempted_transfers'],
                            processed_chats=summary['transferred'] + len(summary['deferred']),
                            transferred_chats=summary['transferred'],
                            deferred_chats=len(plan['blocked']) + len(summary['deferred']), side=action['source'])
        with tempfile.TemporaryDirectory(prefix='transfer-', dir=runtime) as package:
            manual_choice = None
            if action.get('conflict_resolution') == 'manual_keep':
                manual_choice = choice_store['choices'][action['id']]
                package = conflict_choice_package(runtime, manual_choice, action['source'])
                # Validate BOTH preserved versions immediately before using a
                # queued choice. Neither original may be silently reconfirmed.
                conflict_choice_package(runtime, manual_choice, action['target'])
            source_package = package if action['source'] == 'windows' else linux_mount_path(package)
            target_package = package if action['target'] == 'windows' else linux_mount_path(package)
            options = ['--metadata-only'] if action['mode'] == 'metadata' else []
            try:
                if manual_choice is None:
                    endpoint.call(action['source'], 'export', '--id', action['id'], '--package', source_package, '--expected', action['source_revision'], *options)
            except EndpointCallError as error:
                # Export has not written any target or pending import. A chat
                # resumed after inspection must not block unrelated quiet chats.
                # Do not use this rule for unknown errors or any import failure.
                reasons = {'source_active': 'source_busy', 'source_changed': 'source_changed',
                           'thread_missing': 'source_disappeared'}
                if error.side != action['source'] or error.operation != 'export' or error.code not in reasons:
                    raise
                summary['deferred'].append({'id': action['id'], 'reason': reasons[error.code]})
                log.info('Deferred %s: source export %s', action['id'], error.code)
                continue
            if action['mode'] == 'full':
                preview = endpoint.call(action['target'], 'import-preview', '--package', target_package)
                if preview.get('compatible') is False:
                    summary['deferred'].append({'id': action['id'], 'reason': preview.get('reason', 'environment_paths_unavailable')})
                    if manual_choice is not None:
                        next(value for value in summary['manual_choices'] if value['choice_id'] == action['choice_id']).update(
                            status='waiting', reason=preview.get('reason', 'environment_paths_unavailable'))
                    log.info('Deferred %s: environment paths are unavailable on target', action['id'])
                    continue
            # Recheck before any existing record replacement.
            if action['target_revision'] != 'absent' and endpoint.backend_status(action['target']).get('backend_running', True):
                log.info('Deferred existing chat %s: target backend started', action['id'])
                summary['deferred'].append({'id': action['id'], 'reason': 'target_backend_started'})
                if manual_choice is not None:
                    next(value for value in summary['manual_choices'] if value['choice_id'] == action['choice_id']).update(
                        status='waiting', reason='target_backend_started')
                continue
            if manual_choice is not None:
                current = {side: records(endpoint.inspect(side)).get(action['id']) for side in ('windows', 'wsl')}
                if any(row is None or row['revision'] != manual_choice['expected'][side] for side, row in current.items()):
                    manual_choice = {**manual_choice, 'status': 'needs_confirmation'}
                    choice_store['choices'][action['id']] = manual_choice
                    atomic_json(runtime / 'conflict-choices.json', choice_store)
                    summary['deferred'].append({'id': action['id'], 'reason': 'manual_choice_stale'})
                    next(value for value in summary['manual_choices'] if value['choice_id'] == action['choice_id']).update(
                        status='waiting', reason='manual_choice_stale')
                    continue
                if any(is_busy(row, config.get('quiet_seconds', 60)) for row in current.values()):
                    summary['deferred'].append({'id': action['id'], 'reason': 'source_busy'})
                    next(value for value in summary['manual_choices'] if value['choice_id'] == action['choice_id']).update(
                        status='waiting', reason='source_busy')
                    continue
                # Full native inspections can take time. A target started
                # during them must be caught next to the pending/import step.
                if endpoint.backend_status(action['target']).get('backend_running', True):
                    summary['deferred'].append({'id': action['id'], 'reason': 'target_backend_started'})
                    next(value for value in summary['manual_choices'] if value['choice_id'] == action['choice_id']).update(
                        status='waiting', reason='target_backend_started')
                    continue
            atomic_json(journal, action)
            allow = ['--allow-existing'] if action['target_revision'] != 'absent' else []
            imported = endpoint.call(action['target'], 'import', '--package', target_package, '--expected', action['target_revision'], *options, *allow)
            target_record = imported.get('record')
            if not isinstance(target_record, dict) or target_record.get('id') != action['id'] or any(
                    not isinstance(target_record.get(key), str) or not target_record[key]
                    for key in ('revision', 'content_revision', 'metadata_revision')):
                raise RuntimeError('Importer did not return its committed chat revision')
            if imported.get('revision') is not None and imported['revision'] != target_record['revision']:
                raise RuntimeError('Importer receipt revision does not match its committed record')
            if manual_choice is not None and (imported.get('ok') is not True or any(
                    target_record.get(key) != states[action['source']][action['id']].get(key)
                    for key in ('turn_count', 'item_count'))):
                raise RuntimeError('Manual-choice import receipt does not contain the complete selected history')
            # Use the exact exported source revision, not a later source read.
            # Any messages appended during transport remain pending next run.
            source_record = states[action['source']][action['id']]
            rows = {action['source']: source_record, action['target']: target_record}
            previous = ledger['threads'].get(action['id'], {})
            if reporter is not None:
                reporter.update('checkpointing', side=action['target'])
            ledger['threads'][action['id']] = {**rows, 'body_diverged': previous.get('body_diverged', False) if action['mode'] == 'metadata' else False}
            atomic_json(ledger_path, ledger)
            if manual_choice is not None:
                manual_choice = {**manual_choice, 'status': 'resolved', 'resolved_at': utc_now(),
                                 'result_revision': target_record['revision'], 'target_backup': imported.get('backup')}
                choice_store['choices'][action['id']] = manual_choice
                atomic_json(runtime / 'conflict-choices.json', choice_store)
                summary['resolved_conflicts'] += 1
                summary['conflict_results'].append({'id': action['id'], 'source': action['source'], 'target': action['target'],
                                                    'reason': 'manual_keep', 'choice_id': action['choice_id'],
                                                    'backup': imported.get('backup')})
                next(value for value in summary['manual_choices'] if value['choice_id'] == action['choice_id'])['status'] = 'resolved'
            summary['transferred'] += 1
            if action.get('conflict_resolution') == 'prefer_longer':
                summary['resolved_conflicts'] += 1
                decision = next(value for value in plan['conflict_decisions'] if value['id'] == action['id'])
                summary['conflict_results'].append({key: decision[key] for key in (
                    'id', 'source', 'target', 'source_counts', 'target_counts', 'reason')})
                summary['conflict_results'][-1]['backup'] = imported.get('backup')
            summary['remaining_transfers'] = summary['planned_transfers'] - summary['transferred']
            journal.unlink()
            log.info('Synchronized %s to %s (%s)', action['id'], action['target'], action['mode'])
            if reporter is not None:
                reporter.update('checkpointing', processed_chats=summary['transferred'] + len(summary['deferred']),
                                transferred_chats=summary['transferred'])
    summary['remaining_transfers'] = summary['planned_transfers'] - summary['transferred']
    # Refresh even an empty report: resolved problems must not stay on screen.
    atomic_json(runtime / 'attention.json', {'updated_at': utc_now(), 'blocked': plan['blocked'],
        'deferred': summary['deferred'], 'endpoint_skipped': summary['endpoint_skipped']})
    return finish(summary)


def _repair_environment_paths(config, args, runtime, log, progress=None, reporter=None):
    """Repair native environment metadata without copying the other replica.

    Use the normal export/import receipts, backup and stopped-backend checks.
    A pre-existing local edit must remain pending against its old baseline.
    """
    started = time.monotonic()
    side = args.repair_side
    if side == 'wsl' and not wsl_running(config['wsl_distro']):
        return {'status': 'skipped_wsl_stopped', 'repaired': 0}
    ledger_path = runtime / 'ledger.json'
    if not ledger_path.is_file():
        raise RuntimeError('路径修复需要现有同步基线；请先初始化配置')
    ledger = json.loads(ledger_path.read_text(encoding='utf-8'))
    if isinstance(ledger.get('version'), bool) or ledger.get('version') != VERSION or not isinstance(ledger.get('threads'), dict):
        raise RuntimeError('Unsupported synchronization ledger version')
    journal = runtime / 'pending.json'
    if journal.exists():
        raise RuntimeError('An unfinished prior import exists; review runtime/pending.json before continuing')
    endpoint = Endpoints(config, SCRIPT_DIR)
    endpoint.reporter = reporter
    if reporter is not None:
        reporter.update('checking_' + side, side=side)
    view = endpoint.inspect(side)
    native_records = records(view)
    selected = sorted(set(args.thread_ids or [tid for tid, pair in ledger['threads'].items()
                                             if pair.get(side) is not None]))
    if reporter is not None:
        reporter.update('repairing', total_chats=len(selected))
    summary = progress if progress is not None else {}
    summary.update({'status': 'path_repair_preview' if args.dry_run else 'path_repair_completed',
                    'repair_side': side, 'planned_repairs': 0, 'repaired': 0,
                    'unchanged': 0, 'blocked': [], 'repairs': [], 'inspected_repairs': 0})
    maximum = config.get('max_transfers_per_run', DEFAULT_MAX_TRANSFERS)
    budget = config.get('run_budget_seconds', DEFAULT_RUN_BUDGET)
    ledger_backup = None
    for index, tid in enumerate(selected, 1):
        if reporter is not None:
            reporter.update('repairing', current_index=index, processed_chats=index - 1,
                            transferred_chats=summary['repaired'], deferred_chats=len(summary['blocked']))
        if time.monotonic() - started >= budget:
            summary['batch_stop_reason'] = 'time_budget'
            break
        summary['inspected_repairs'] += 1
        pair = ledger['threads'].get(tid)
        row = native_records.get(tid)
        if not pair or not isinstance(pair.get(side), dict) or row is None:
            summary['blocked'].append({'id': tid, 'reason': 'endpoint_record_unavailable'})
            continue
        try:
            preview = endpoint.call(side, 'path-preview', '--id', tid)
        except RuntimeError:
            summary['blocked'].append({'id': tid, 'reason': 'environment_paths_unavailable'})
            continue
        if not preview.get('needs_repair'):
            summary['unchanged'] += 1
            continue
        summary['planned_repairs'] += 1
        entry = {'id': tid, 'environment_paths': preview.get('environment_paths', {}),
                 'database_cwd_changed': bool(preview.get('database_cwd_changed'))}
        summary['repairs'].append(entry)
        if args.dry_run:
            continue
        if is_busy(row, config.get('quiet_seconds', 60)):
            summary['blocked'].append({'id': tid, 'reason': 'target_busy'})
            continue
        if endpoint.backend_status(side).get('backend_running', True):
            summary['blocked'].append({'id': tid, 'reason': 'target_backend_running'})
            continue
        if summary['repaired'] >= maximum:
            summary['batch_stop_reason'] = 'batch_limit'
            break
        if ledger_backup is None:
            backup_root = runtime / 'path-repair-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup_root.mkdir(parents=True, exist_ok=False)
            ledger_backup = backup_root / 'ledger.json'
            shutil.copy2(ledger_path, ledger_backup)
            summary['ledger_backup'] = str(ledger_backup)
        with tempfile.TemporaryDirectory(prefix='path-repair-', dir=runtime) as temporary:
            package = temporary if side == 'windows' else linux_mount_path(temporary)
            endpoint.call(side, 'export', '--id', tid, '--package', package, '--expected', row['revision'])
            if endpoint.backend_status(side).get('backend_running', True):
                summary['blocked'].append({'id': tid, 'reason': 'target_backend_started'})
                continue
            atomic_json(journal, {'operation': 'repair_environment_paths', 'id': tid,
                                 'side': side, 'target_revision': row['revision'],
                                 'ledger_backup': str(ledger_backup)})
            imported = endpoint.call(side, 'import', '--package', package,
                                     '--expected', row['revision'], '--allow-existing', '--environment-only')
            repaired = imported.get('record')
            if not isinstance(repaired, dict) or repaired.get('id') != tid or any(
                    not isinstance(repaired.get(key), str) or not repaired[key]
                    for key in ('revision', 'content_revision', 'metadata_revision')):
                raise RuntimeError('Importer did not return its committed chat revision')
            if imported.get('revision') != repaired['revision']:
                raise RuntimeError('Importer receipt revision does not match its committed record')
            baseline_updated = pair[side].get('revision') == row['revision']
            if baseline_updated:
                pair[side] = repaired
                atomic_json(ledger_path, ledger)
            entry.update({'backup': imported.get('backup'), 'repaired': True,
                          'baseline_updated': baseline_updated,
                          'environment_paths': imported.get('environment_paths', entry['environment_paths'])})
            summary['repaired'] += 1
            journal.unlink()
            log.info('Repaired native environment paths for %s on %s', tid, side)
    summary['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return summary


execute = _tracked_operation(_execute, 'sync')
repair_environment_paths = _tracked_operation(_repair_environment_paths, 'repair')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('配置须为 JSON 对象')
    for key in ('windows_home', 'wsl_distro', 'wsl_user', 'wsl_home', 'python_windows'):
        if not isinstance(config.get(key), str) or not config[key].strip() or '\x00' in config[key]:
            raise ValueError(f'配置项 {key} 缺失或不是有效字符串')
    if 'python_wsl' in config and (not isinstance(config['python_wsl'], str) or not config['python_wsl'].strip()):
        raise ValueError('配置项 python_wsl 不是有效字符串')
    if config.get('conflict_resolution', 'manual') not in ('manual', 'prefer_longer'):
        raise ValueError('配置项 conflict_resolution 须为 manual 或 prefer_longer')
    for key, default in (('max_transfers_per_run', DEFAULT_MAX_TRANSFERS),
                         ('run_budget_seconds', DEFAULT_RUN_BUDGET),
                         ('operation_timeout_seconds', 120), ('quiet_seconds', 60),
                         ('interval_minutes', 5), ('task_time_limit_minutes', 10)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'配置项 {key} 须为正整数')
    for key in ('runtime_directory', 'task_name'):
        if key in config and (not isinstance(config[key], str) or not config[key].strip()):
            raise ValueError(f'配置项 {key} 须为非空字符串')
    task_name = config.get('task_name', 'CodexHistorySync-Public')
    if len(task_name) > 200 or any(character in task_name for character in '\\/:*?"<>|') or any(ord(character) < 32 or ord(character) == 127 for character in task_name):
        raise ValueError('task_name 含有不允许的字符')
    minimum = config.get('run_budget_seconds', DEFAULT_RUN_BUDGET) + 3 * config.get('operation_timeout_seconds', 120) + 60
    if config.get('task_time_limit_minutes', 10) * 60 < minimum:
        raise ValueError(f'task_time_limit_minutes 须至少覆盖 {minimum} 秒，给本条传输收尾留出时间')
    return config


def require_local_drive(path, label='路径'):
    if os.name == 'nt':
        drive = PureWindowsPath(str(path)).drive
        if len(drive) != 2 or not drive.endswith(':'):
            raise ValueError(f'{label}须位于 Windows 本地磁盘，不能使用 UNC 或 WSL 网络路径')
        if ctypes.windll.kernel32.GetDriveTypeW(drive + '\\') in (0, 1, 4):
            raise ValueError(f'{label}不能使用映射网络盘或不存在的磁盘')
    return path


def resolve_runtime(config, config_path, override=None):
    value = override if override is not None else config.get('runtime_directory', 'runtime')
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError('runtime_directory 须为非空路径')
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    resolved = path.resolve()
    return require_local_drive(resolved, '运行目录')


def normalize_config(config, config_path, python_override=None, task_override=None):
    value = dict(config)
    value.setdefault('conflict_resolution', 'manual')
    if python_override:
        value['python_windows'] = str(python_override)
    if task_override:
        value['task_name'] = task_override
    validate_config(value)
    for key in ('windows_home', 'python_windows'):
        supplied = value[key]
        if key == 'python_windows' and supplied.lower() == 'auto':
            supplied = str(Path(sys.executable).with_name('python.exe')) if Path(sys.executable).name.lower() == 'pythonw.exe' else sys.executable
        supplied = os.path.expandvars(supplied)
        path = Path(supplied).expanduser()
        if os.name == 'nt' and PureWindowsPath(str(path)).drive and not path.is_absolute():
            raise ValueError(f'{key} 不能使用盘符相对路径')
        if key == 'python_windows' and not path.is_absolute():
            found = shutil.which(str(path))
            path = Path(found) if found else config_path.parent / path
        elif not path.is_absolute():
            path = config_path.parent / path
        value[key] = str(path.resolve())
        if key == 'windows_home':
            require_local_drive(value[key], 'Windows Codex 目录')
    return value


def read_cached_json(path, warnings):
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except (OSError, ValueError):
        warnings.append(f'{path.name} 无法读取，请保留文件并检查')
        return None


def valid_issue_containers(value):
    if not isinstance(value, dict):
        return False
    groups = []
    for key in ('blocked', 'deferred'):
        group = value.get(key, [])
        if not isinstance(group, list):
            return False
        groups.append(group)
    skipped = value.get('endpoint_skipped', {})
    if not isinstance(skipped, dict) or any(not isinstance(group, list) for group in skipped.values()):
        return False
    groups.extend(skipped.values())
    return all(isinstance(item, dict) and all(key not in item or isinstance(item[key], str)
               for key in ('reason', 'code', 'id')) for group in groups for item in group)


def read_status(runtime):
    """Read cached local summaries only: no processes, WSL or file mutations."""
    warnings = []
    ledger = read_cached_json(runtime / 'ledger.json', warnings)
    last_run = read_cached_json(runtime / 'last-run.json', warnings)
    attention = read_cached_json(runtime / 'attention.json', warnings)
    if last_run and not valid_issue_containers(last_run.get('summary')):
        warnings.append('last-run.json 缺少有效运行摘要')
        last_run = None
    if attention and not valid_issue_containers(attention):
        warnings.append('attention.json 问题报告结构无效，请保留文件并检查')
        attention = None
    pending_exists = (runtime / 'pending.json').exists()
    ledger_valid = bool(ledger and not isinstance(ledger.get('version'), bool) and ledger.get('version') == VERSION and isinstance(ledger.get('threads'), dict))
    if ledger and not ledger_valid:
        warnings.append('ledger.json 版本或结构不兼容，请保留现有基线')
    return {'status': 'cached_status', 'software_version': SOFTWARE_VERSION,
            'cached_only': True, 'initialized': ledger_valid,
            'tracked_chats': len(ledger['threads']) if ledger_valid else 0,
            'pending_import': pending_exists, 'last_run': last_run,
            'attention': attention, 'warnings': warnings}


def diagnose(config, runtime):
    """Native read-only diagnostics; never starts a stopped distribution."""
    status = read_status(runtime)
    checks = [{'code': 'configuration', 'status': 'ok', 'message': '配置格式正确'}]
    checks.append({'code': 'baseline', 'status': 'ok' if status['initialized'] else 'warning',
                   'message': '同步基线已初始化' if status['initialized'] else '尚无有效同步基线；首次启用请初始化'})
    checks.append({'code': 'pending_import', 'status': 'error' if status['pending_import'] else 'ok',
                   'message': '存在未完成导入；请保留记录并检查' if status['pending_import'] else '没有未完成的控制器导入'})
    for warning in status['warnings']:
        checks.append({'code': 'cached_report', 'status': 'warning', 'message': warning})
    endpoint = Endpoints(config, SCRIPT_DIR)
    sides = {}
    try:
        running = wsl_running(config['wsl_distro'])
        checks.append({'code': 'wsl_running', 'status': 'ok' if running else 'warning',
            'message': '指定 WSL 正在运行' if running else '指定 WSL 未运行，已跳过其检查且未启动它'})
    except Exception as exc:
        running = False
        checks.append({'code': 'wsl_running', 'status': 'error', 'message': str(exc)})
    for side in ('windows', 'wsl'):
        if side == 'wsl' and not running:
            sides[side] = {'status': 'skipped_wsl_stopped'}
            continue
        try:
            sides[side] = endpoint.call(side, 'doctor')
        except Exception as exc:
            sides[side] = {'checks': [{'code': 'endpoint', 'status': 'error', 'message': str(exc)}]}
    has_errors = any(item['status'] == 'error' for item in checks) or any(
        item.get('status') == 'error' for value in sides.values() for item in value.get('checks', []))
    return {'status': 'diagnostics', 'software_version': SOFTWARE_VERSION,
            'checked_at': utc_now(), 'read_only': True, 'healthy': not has_errors,
            'checks': checks, 'endpoints': sides}


STATUS_LABELS = {
    'completed': '本轮同步完成', 'dry_run': '预览完成，未修改聊天记录或基线',
    'initialized': '同步基线初始化完成', 'skipped_wsl_stopped': 'WSL 未运行，本轮已跳过',
    'skipped_already_running': '已有同步正在运行，本轮已跳过', 'error': '运行失败',
}
REASON_LABELS = {
    'batch_limit': '达到每轮数量上限', 'time_budget': '达到每轮时间预算',
    'target_backend_running': '目标侧 Codex 正在运行', 'target_backend_started': '目标侧 Codex 在传输前启动',
    'source_busy': '源聊天仍活跃或未静默', 'target_busy': '目标聊天仍活跃或未静默',
    'source_changed': '源聊天在导出前后发生变化，留待下轮',
    'source_disappeared': '源聊天在检查后消失，留待下轮检查',
    'manual_choice_stale': '确认后聊天发生变化，请重新选择保留哪一端',
    'both_sides_changed': '两端都修改了这条聊天', 'existing_body_difference': '两端正文原本不同',
    'conflict_counts_unavailable': '冲突聊天没有可靠的轮数和内容条数',
    'conflict_counts_equal': '两端轮数和内容条数相同，保留冲突',
    'conflict_history_incomplete': '冲突聊天正文缺失或不完整',
    'conflict_history_empty': '冲突聊天为空或没有已索引轮次',
    'conflict_history_unindexed': '冲突聊天正文尚未建立索引',
    'initial_body_difference': '首次发现两端正文不同', 'replica_missing': '曾同步的记录已消失',
    'endpoint_record_unavailable': '无法安全读取某端记录', 'unreadable_transcript': '正文缺失或不完整',
    'empty_conversation': '聊天为空', 'unindexed_history': '旧正文尚未建立索引',
    'rollout_outside_home': '正文路径不在当地数据目录内',
    'environment_paths_unavailable': '会话环境路径无法安全转换',
    'workspace_cwd_unmappable': '原工作目录无法映射或已不存在',
    'environment_record_too_large': '单条记录超过可安全解析的大小',
}
COUNT_LABELS = {'thread_count': '聊天', 'rollout_missing': '正文缺失',
    'rollout_outside_home': '正文路径越界', 'rollout_incomplete': '正文未写完',
    'rollout_unreadable': '正文不可读', 'paginated_threads_without_cached_items': '分页正文索引为空'}


def issue_lines(value):
    endpoint_issues = []
    for side, rows in value.get('endpoint_skipped', {}).items():
        endpoint_issues += [{**item, 'reason': item.get('code', 'unavailable'), 'side': side} for item in rows]
    detailed_ids = {item.get('id') for item in endpoint_issues if item.get('id')}
    issues = [item for item in value.get('blocked', []) if not (
        item.get('reason') == 'endpoint_record_unavailable' and item.get('id') in detailed_ids)]
    issues += list(value.get('deferred', [])) + endpoint_issues
    counts, seen = {}, set()
    for issue in issues:
        reason = issue.get('reason', 'unavailable')
        marker = (issue.get('id'), reason)
        if issue.get('id') and marker in seen:
            continue
        seen.add(marker)
        counts[reason] = counts.get(reason, 0) + 1
    return [f'  {REASON_LABELS.get(reason, reason)}：{count} 条' for reason, count in sorted(counts.items())]


def local_timestamp(value):
    if not value:
        return '尚无记录'
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    except (ValueError, AttributeError):
        return str(value)


def human_summary(value, include_issues=True):
    status = value.get('status')
    if status in ('path_repair_preview', 'path_repair_completed'):
        lines = ['路径修复预览（未修改聊天或基线）' if status == 'path_repair_preview' else '聊天环境路径修复完成',
                 f"待修复：{value['planned_repairs']} 条；已修复：{value['repaired']} 条；无需修复：{value['unchanged']} 条"]
        lines.extend(issue_lines(value))
        if value.get('ledger_backup'):
            lines.append('原基线和每条聊天已分别备份')
        if value.get('elapsed_seconds') is not None:
            lines.append(f"用时：{value['elapsed_seconds']} 秒")
        return '\n'.join(lines)
    if status == 'cached_status':
        lines = ['同步状态（来自本地缓存，不检查进程或启动 WSL）',
                 f"基线：{'已初始化' if value['initialized'] else '未初始化或无效'}；跟踪聊天：{value['tracked_chats']} 条",
                 f"未完成导入：{'有，请先检查' if value['pending_import'] else '无'}"]
        last = value.get('last_run') or {}
        if last:
            lines += [f"上次检查：{local_timestamp(last.get('last_checked_at'))}",
                      f"上次成功传输：{local_timestamp(last.get('last_transfer_at'))}"]
            lines.append(human_summary(last.get('summary', {}), include_issues=False))
        else:
            lines.append('尚无运行结果；可运行预览或诊断')
        attention = value.get('attention') or {}
        if attention:
            lines.append(f"问题报告时间：{local_timestamp(attention.get('updated_at'))}")
            lines.extend(issue_lines(attention))
        elif last:
            lines.extend(issue_lines(last.get('summary', {})))
        lines.extend(value['warnings'])
        return '\n'.join(lines)
    if status == 'diagnostics':
        lines = ['只读诊断完成' if value.get('healthy') else '只读诊断发现问题']
        labels = {'ok': '正常', 'warning': '提示', 'error': '异常', 'skipped': '跳过'}
        for item in value.get('checks', []):
            lines.append(f"[{labels.get(item['status'], item['status'])}] {item['message']}")
        for side, report in value.get('endpoints', {}).items():
            lines.append('Windows：' if side == 'windows' else 'WSL：')
            if report.get('status') == 'skipped_wsl_stopped':
                lines.append('  未运行，已跳过')
            for item in report.get('checks', []):
                lines.append(f"  [{labels.get(item['status'], item['status'])}] {item['message']}")
            counts = report.get('record_counts')
            if counts:
                lines.append('  ' + '；'.join(f'{label}：{counts.get(key, 0)} 条' for key, label in COUNT_LABELS.items()))
        return '\n'.join(lines)
    lines = [STATUS_LABELS.get(status, status or '无运行摘要')]
    if 'planned_transfers' in value:
        lines.append(f"待同步：{value['planned_transfers']} 条；已同步：{value['transferred']} 条；留待下轮：{value['remaining_transfers']} 条")
        if include_issues:
            lines.extend(issue_lines(value))
    if value.get('initialization_required'):
        lines.append('首次启用前请先运行 .\\sync-now.ps1 -Initialize')
    if value.get('batch_stop_reason'):
        lines.append(REASON_LABELS.get(value['batch_stop_reason'], value['batch_stop_reason']))
    if 'tracked_chats' in value:
        lines.append(f"跟踪聊天：{value['tracked_chats']} 条；两端已有正文差异：{value.get('different_existing_bodies', 0)} 条")
    if value.get('error'):
        lines.append(value['error'])
    if 'elapsed_seconds' in value:
        lines.append(f"用时：{value['elapsed_seconds']} 秒")
    return '\n'.join(lines)


def emit_result(summary, as_json):
    if sys.stdout is not None:
        print(json.dumps(summary, ensure_ascii=False) if as_json else human_summary(summary))


def save_run_report(runtime, summary):
    previous = read_cached_json(runtime / 'last-run.json', []) or {}
    now = utc_now()
    atomic_json(runtime / 'last-run.json', {'software_version': SOFTWARE_VERSION,
        'last_checked_at': now,
        'last_transfer_at': now if summary.get('transferred', 0) else previous.get('last_transfer_at'),
        'summary': summary})


def main(argv=None):
    # Native Python may default redirected output to the Windows ANSI code page.
    # Use UTF-8 consistently for the Chinese summary and JSON consumers.
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=SCRIPT_DIR / 'config.json')
    parser.add_argument('--runtime-dir', type=Path, help='本轮运行数据目录，覆盖配置')
    parser.add_argument('--task-name', help='本轮任务名称，覆盖配置')
    parser.add_argument('--python-windows', help='本轮端点解释器，覆盖配置')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--json', action='store_true', help='机器可读的 JSON 输出')
    parser.add_argument('--repair-paths', action='store_true', help='修复已同步聊天的本地环境路径')
    parser.add_argument('--repair-side', choices=('windows', 'wsl'), default='wsl')
    parser.add_argument('--id', dest='thread_ids', action='append', help='仅修复指定聊天，可重复使用')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--initialize', action='store_true')
    mode.add_argument('--status', action='store_true')
    mode.add_argument('--doctor', action='store_true')
    args = parser.parse_args(argv)
    if args.repair_paths and (args.initialize or args.status or args.doctor):
        parser.error('--repair-paths can only be combined with --dry-run or --once')
    if args.thread_ids and not args.repair_paths:
        parser.error('--id requires --repair-paths')
    config_path = args.config.expanduser().resolve()
    raw_config, config_error = {}, None
    try:
        raw_config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        if not isinstance(raw_config, dict):
            raise ValueError('配置须为 JSON 对象')
        runtime = resolve_runtime(raw_config, config_path, args.runtime_dir)
    except Exception as exc:
        config_error = exc
        try:
            runtime = resolve_runtime({}, config_path, args.runtime_dir)
        except Exception as runtime_error:
            emit_result({'status': 'error', 'error': str(runtime_error)}, args.json)
            return 1
    if args.status:
        summary = read_status(runtime)
        summary['runtime_directory'] = str(runtime)
        summary['task_name'] = args.task_name or (raw_config.get('task_name', 'CodexHistorySync-Public') if isinstance(raw_config, dict) else 'CodexHistorySync-Public')
        if config_error:
            summary['warnings'].append('配置尚未生成或无法读取；可运行 .\\configure.ps1，或指定 --runtime-dir 查看现有状态')
        emit_result(summary, args.json)
        return 0
    if args.doctor:
        try:
            if os.name != 'nt':
                raise RuntimeError('请在 Windows 中运行控制器')
            if config_error:
                if isinstance(config_error, FileNotFoundError):
                    raise RuntimeError('配置尚未生成，请运行 .\\configure.ps1，或用 -ConfigPath 指定现有配置') from None
                raise config_error
            config = normalize_config(raw_config, config_path, args.python_windows, args.task_name)
            summary = diagnose(config, runtime)
            emit_result(summary, args.json)
            return 0 if summary['healthy'] else 1
        except Exception as exc:
            emit_result({'status': 'error', 'error': str(exc)}, args.json)
            return 1
    log = logging.getLogger('codex-history-sync')
    log.setLevel(logging.INFO)
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(runtime / 'sync.log', maxBytes=262144, backupCount=2, encoding='utf-8')
    except OSError as exc:
        emit_result({'status': 'error', 'error': f'无法写入运行目录：{exc}'}, args.json)
        return 1
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler)
    progress = {}
    run_started = time.monotonic()
    try:
        if os.name != 'nt':
            raise RuntimeError('Run the coordinator on Windows')
        if sys.version_info < (3, 11):
            raise RuntimeError('请使用 Python 3.11 或更新版本')
        if config_error:
            raise config_error
        config = normalize_config(raw_config, config_path, args.python_windows, args.task_name)
        require_local_drive(SCRIPT_DIR, '程序目录')
        with run_lock(runtime / 'run.lock') as acquired:
            if acquired:
                summary = (repair_environment_paths(config, args, runtime, log, progress)
                           if args.repair_paths else execute(config, args, runtime, log, progress))
                if not args.dry_run:
                    save_run_report(runtime, summary)
            else:
                summary = {'status': 'skipped_already_running'}
        if summary.get('transferred') or summary.get('status') == 'initialized':
            log.info('%s', json.dumps({k: v for k, v in summary.items() if k not in ['blocked', 'actions']}))
        emit_result(summary, args.json)
        return 0
    except Exception as exc:
        log.error('%s', exc)
        summary = {**progress, 'status': 'error', 'error': str(exc),
                   'elapsed_seconds': round(time.monotonic() - run_started, 3)}
        if not args.dry_run:
            try:
                save_run_report(runtime, summary)
            except OSError as report_error:
                summary['warnings'] = [f'无法保存本轮状态：{report_error}']
        emit_result(summary, args.json)
        return 1
    finally:
        log.removeHandler(handler)
        handler.close()


if __name__ == '__main__':
    raise SystemExit(main())
