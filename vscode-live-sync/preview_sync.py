"""Read-only Windows probe for the optional VS Code reload companion."""
import argparse
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid


def load_controller(controller, config_path):
    controller = Path(controller).resolve(strict=True)
    config_path = Path(config_path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location('vscode_sync_controller', controller)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    raw = json.loads(config_path.read_text(encoding='utf-8-sig'))
    config = sync.normalize_config(raw, config_path) if hasattr(sync, 'normalize_config') else sync.validate_config(raw)
    runtime = sync.resolve_runtime(config, config_path) if hasattr(sync, 'resolve_runtime') else controller.parent / 'runtime'
    return controller, sync, config, runtime


def recovery_state(controller, config_path):
    """Fail closed unless no import or controller can still be writing."""
    if os.name != 'nt':
        raise RuntimeError('Run this probe with native Windows Python')
    controller, sync, config, runtime = load_controller(controller, config_path)
    if not sync.wsl_running(config['wsl_distro']):
        return {'safe_to_reload': False, 'reason': 'wsl_stopped'}
    lock = runtime / 'run.lock'
    if not lock.is_file():
        return {'safe_to_reload': False, 'reason': 'lock_missing'}
    import msvcrt
    with lock.open('r+b') as stream:
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return {'safe_to_reload': False, 'reason': 'controller_running'}
        try:
            if (runtime / 'pending.json').exists():
                return {'safe_to_reload': False, 'reason': 'pending_import'}
            # Inspect native /proc only, never read the WSL database in Windows.
            script = '''import json,os,pathlib
safe=True
for entry in pathlib.Path('/proc').iterdir():
 if not entry.name.isdigit() or int(entry.name)==os.getpid(): continue
 try:
  data=(entry/'cmdline').read_bytes().split(b'\\0')
 except FileNotFoundError: continue
 except OSError: safe=False; break
 if any(pathlib.PurePosixPath(arg.decode('utf-8','replace')).name=='history_endpoint.py' for arg in data if arg):
  safe=False; break
print(json.dumps({'safe':safe}))
'''
            command = ['wsl.exe', '--distribution', config['wsl_distro'], '--user', config['wsl_user'], '--exec', config.get('python_wsl', '/usr/bin/python3'), '-c', script]
            result = subprocess.run(command, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode or json.loads(sync.decode_output(result.stdout)).get('safe') is not True:
                return {'safe_to_reload': False, 'reason': 'endpoint_running_or_unknown'}
            if (runtime / 'pending.json').exists():
                return {'safe_to_reload': False, 'reason': 'pending_import'}
            return {'safe_to_reload': True, 'reason': 'no_writer_or_pending_import'}
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def preview_plan(sync, windows, wsl, ledger, quiet_seconds):
    # Simulate only the process guard being lifted. All thread, source, baseline,
    # conflict and quiet checks remain the production planner's responsibility.
    relaxed = deepcopy(wsl)
    relaxed['backend_running'] = False
    relaxed['backend_detection_ok'] = True
    return sync.build_plan(windows, relaxed, ledger, quiet_seconds)


def eligible_candidates(sync, windows, wsl, ledger, quiet_seconds):
    return [a for a in preview_plan(sync, windows, wsl, ledger, quiet_seconds)['actions']
            if a['source'] == 'windows' and a['target'] == 'wsl' and a['target_revision'] != 'absent']


def classify_plan(sync, windows, wsl, ledger, quiet_seconds):
    """Report pending incoming updates separately from unrelated historical skips.

    Only production-planner actions can become reload candidates. Blocked counts
    contain reason names and totals, never chat titles, bodies or local paths.
    """
    plan = preview_plan(sync, windows, wsl, ledger, quiet_seconds)
    candidates = [a for a in plan['actions'] if a['source'] == 'windows' and
                  a['target'] == 'wsl' and a['target_revision'] != 'absent']
    win = {row['id']: row for row in windows.get('threads', [])}
    lin = {row['id']: row for row in wsl.get('threads', [])}
    relevant = set()
    for tid, row in win.items():
        old = ledger.get('threads', {}).get(tid, {})
        previous = old.get('windows')
        target_exists = tid in lin or old.get('wsl') is not None
        if target_exists and (previous is None or row['revision'] != previous['revision']):
            relevant.add(tid)
    counts = {}
    ignored = 0
    for item in plan['blocked']:
        if item['id'] in relevant:
            reason = item['reason']
            counts[reason] = counts.get(reason, 0) + 1
        else:
            ignored += 1
    new_count = sum(a['source'] == 'windows' and a['target'] == 'wsl' and
                    a['target_revision'] == 'absent' for a in plan['actions'])
    # Outgoing updates can themselves be blocked by the Windows backend. They
    # still must not be presented as an absence of pending records.
    outgoing_ids = {a['id'] for a in plan['actions'] if a['source'] == 'wsl'}
    for tid, row in lin.items():
        old = ledger.get('threads', {}).get(tid, {})
        if old.get('wsl') and old.get('windows') and tid in win and (
                row['revision'] != old['wsl']['revision'] and
                win[tid]['revision'] == old['windows']['revision']):
            outgoing_ids.add(tid)
    outgoing = len(outgoing_ids)
    conflict_reasons = ('both_sides_changed', 'existing_body_difference',
                        'initial_body_difference', 'replica_missing')
    conflict_count = sum(counts.get(reason, 0) for reason in conflict_reasons)
    if candidates:
        reason = 'existing_replica_updates'
    elif conflict_count:
        reason = 'chat_conflict'
    elif counts.get('source_busy') or counts.get('target_busy'):
        reason = 'chat_busy'
    elif counts:
        reason = 'incoming_update_blocked'
    elif new_count:
        reason = 'new_chats_pending'
    elif outgoing:
        reason = 'opposite_direction_pending'
    else:
        reason = 'no_pending_updates'
    return {'eligible': bool(candidates), 'candidate_ids': [a['id'] for a in candidates],
            'candidate_count': len(candidates), 'reason': reason,
            'blocked_reason_counts': counts, 'conflict_count': conflict_count,
            'ignored_blocked_count': ignored, 'new_chat_count': new_count,
            'opposite_direction_count': outgoing}


def call_native_probe(sync, config, controller, thread_id, baseline):
    """Run the proof in native WSL, never open its live SQLite from Windows."""
    if not sync.wsl_running(config['wsl_distro']):
        raise RuntimeError('WSL stopped during local settings verification')
    helper = Path(__file__).resolve().with_name('native_local_probe.py')
    encoded = base64.b64encode(json.dumps(baseline, ensure_ascii=True).encode('utf-8')).decode('ascii')
    if len(encoded) > 65536:
        return {'verified': False, 'reason': 'invalid_baseline'}
    command = ['wsl.exe', '--distribution', config['wsl_distro'], '--user', config['wsl_user'],
               '--exec', config.get('python_wsl', '/usr/bin/python3'), sync.linux_mount_path(helper),
               '--endpoint', sync.linux_mount_path(controller.parent / 'history_endpoint.py'),
               '--home', config['wsl_home'], '--id', thread_id, '--baseline-base64', encoded,
               '--quiet-seconds', str(config.get('quiet_seconds', 60))]
    result = subprocess.run(command, capture_output=True,
                            timeout=config.get('operation_timeout_seconds', 120),
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Native local settings verification failed')
    return json.loads(sync.decode_output(result.stdout))


def virtual_local_settings_ledger(sync, windows, wsl, ledger, quiet_seconds, verify):
    """A verified target-only acknowledgement is virtual until a stopped import.

    Genuine cache/message edits, diverged baselines, source activity and failed
    proofs stay under the ordinary production conflict guards.
    """
    virtual = deepcopy(ledger)
    win = {row['id']: row for row in windows.get('threads', [])}
    lin = {row['id']: row for row in wsl.get('threads', [])}
    proofs = {}
    blocked = preview_plan(sync, windows, wsl, ledger, quiet_seconds)['blocked']
    for item in blocked:
        if item['reason'] != 'both_sides_changed':
            continue
        tid = item['id']
        old = ledger.get('threads', {}).get(tid, {})
        source, target = win.get(tid), lin.get(tid)
        if (old.get('body_diverged') is not False or not old.get('windows') or not old.get('wsl')
                or source is None or target is None or sync.is_busy(source, quiet_seconds)
                or source.get('transcript_complete') is not True
                or source.get('content_revision') == old['windows'].get('content_revision')):
            continue
        proof = verify(tid, old['wsl'])
        if not isinstance(proof, dict):
            continue
        record = proof.get('record')
        if (proof.get('verified') is not True or proof.get('reason') != 'local_settings_only'
                or type(proof.get('settings_count')) is not int or proof['settings_count'] <= 0
                or not isinstance(record, dict) or record.get('id') != tid
                or record.get('revision') != target.get('revision')):
            continue
        virtual['threads'][tid]['wsl'] = deepcopy(record)
        proofs[tid] = proof
    # Even a proof is not sufficient: the ordinary planner must approve the
    # source quietness, available transcript, target quietness and counts.
    approved = {action['id'] for action in eligible_candidates(sync, windows, wsl, virtual, quiet_seconds)}
    proofs = {tid: proof for tid, proof in proofs.items() if tid in approved}
    for tid in set(virtual['threads']) - set(proofs):
        virtual['threads'][tid] = deepcopy(ledger['threads'][tid])
    return virtual, proofs


def stopped_backend(view):
    return (view.get('backend_detection_ok') is True and view.get('backend_running') is False
            and type(view.get('backend_count')) is int and view['backend_count'] == 0)


def acknowledge_local_settings(sync, endpoint, config, controller, runtime, verify=None):
    """Called only with run.lock held. Persist proof before a target-only ack."""
    ledger_path = runtime / 'ledger.json'
    if (runtime / 'pending.json').exists():
        raise RuntimeError('An unfinished import exists')
    original_bytes = ledger_path.read_bytes()
    ledger = json.loads(original_bytes)
    if type(ledger.get('version')) is not int or ledger['version'] != sync.VERSION or not isinstance(ledger.get('threads'), dict):
        raise RuntimeError('Unsupported synchronization ledger')
    windows, wsl = endpoint.inspect('windows'), endpoint.inspect('wsl')
    if not stopped_backend(wsl) or not stopped_backend(endpoint.backend_status('wsl')):
        raise RuntimeError('The WSL backend must be reliably stopped before synchronization')
    verify = verify or (lambda tid, baseline: call_native_probe(sync, config, controller, tid, baseline))
    quiet = config.get('quiet_seconds', 60)
    virtual, first = virtual_local_settings_ledger(sync, windows, wsl, ledger, quiet, verify)
    if not first:
        return []
    # Repeat inspection and the native proof against the same original baseline.
    again_windows, again_wsl = endpoint.inspect('windows'), endpoint.inspect('wsl')
    if not stopped_backend(again_wsl):
        raise RuntimeError('The WSL backend restarted during verification')
    second_virtual, second = virtual_local_settings_ledger(sync, again_windows, again_wsl, ledger, quiet, verify)
    source_before = {row['id']: row for row in windows.get('threads', [])}
    source_after = {row['id']: row for row in again_windows.get('threads', [])}
    for tid, proof in first.items():
        if (tid not in second or proof['record']['revision'] != second[tid]['record']['revision']
                or source_before[tid]['revision'] != source_after[tid]['revision']):
            raise RuntimeError('A chat changed during local settings verification')
    limit = config.get('max_transfers_per_run', 10)
    selected = sorted(first)[:limit]
    updated = deepcopy(ledger)
    for tid in selected:
        # Preserve the complete source baseline and all other ledger fields.
        updated['threads'][tid]['wsl'] = second_virtual['threads'][tid]['wsl']
    if (runtime / 'pending.json').exists() or ledger_path.read_bytes() != original_bytes:
        raise RuntimeError('Synchronization state changed during verification')
    if not stopped_backend(endpoint.backend_status('wsl')):
        raise RuntimeError('The WSL backend restarted before acknowledgement')
    backup_dir = runtime / 'local-settings-ack' / uuid.uuid4().hex
    backup_dir.mkdir(parents=True, exist_ok=False)
    sync.atomic_json(backup_dir / 'ledger-before.json', ledger)
    receipt = {'version': 1, 'status': 'prepared', 'created_at': sync.utc_now(),
               'original_ledger_bytes_sha256': hashlib.sha256(original_bytes).hexdigest(),
               'ledger_backup_sha256': hashlib.sha256((backup_dir / 'ledger-before.json').read_bytes()).hexdigest(),
               'acknowledgements': [{'id': tid, 'reason': 'local_settings_only',
                   'settings_count': second[tid]['settings_count'],
                   'target_before_revision': ledger['threads'][tid]['wsl']['revision'],
                   'target_ack_revision': updated['threads'][tid]['wsl']['revision'],
                   'preserved_source_baseline_revision': ledger['threads'][tid]['windows']['revision']}
                   for tid in selected]}
    sync.atomic_json(backup_dir / 'receipt.json', receipt)
    # Backup creation can take time. Recheck exact revisions and source
    # quietness after it, immediately before changing any acknowledgement.
    final_windows, final_wsl = endpoint.inspect('windows'), endpoint.inspect('wsl')
    final_sources = {row['id']: row for row in final_windows.get('threads', [])}
    final_targets = {row['id']: row for row in final_wsl.get('threads', [])}
    if not stopped_backend(final_wsl) or not stopped_backend(endpoint.backend_status('wsl')):
        raise RuntimeError('The WSL backend restarted before acknowledgement')
    for tid in selected:
        source, target = final_sources.get(tid), final_targets.get(tid)
        if (source is None or target is None or sync.is_busy(source, quiet)
                or source['revision'] != source_after[tid]['revision']
                or target['revision'] != updated['threads'][tid]['wsl']['revision']):
            raise RuntimeError('A chat changed or became busy before acknowledgement')
    if (runtime / 'pending.json').exists() or ledger_path.read_bytes() != original_bytes:
        raise RuntimeError('Synchronization state changed before acknowledgement')
    sync.atomic_json(ledger_path, updated)
    sync.atomic_json(backup_dir / 'receipt.json', {**receipt, 'status': 'acknowledged', 'acknowledged_at': sync.utc_now()})
    return selected


def sync_once(controller, config_path):
    if os.name != 'nt':
        raise RuntimeError('Run this helper with native Windows Python')
    controller, sync, config, runtime = load_controller(controller, config_path)
    if not sync.wsl_running(config['wsl_distro']):
        return {'status': 'skipped_wsl_stopped', 'transferred': 0}
    if not (runtime / 'ledger.json').is_file():
        raise RuntimeError('A synchronization baseline is required')
    with sync.run_lock(runtime / 'run.lock') as acquired:
        if not acquired:
            return {'status': 'skipped_already_running', 'transferred': 0}
        endpoint = sync.Endpoints(config, controller.parent)
        acknowledged = acknowledge_local_settings(sync, endpoint, config, controller, runtime)
        args = SimpleNamespace(initialize=False, dry_run=False)
        result = sync.execute(config, args, runtime, logging.getLogger('codex-history-sync'))
        result['local_settings_acknowledged'] = len(acknowledged)
        sync.save_run_report(runtime, result)
        return result


def call_backend_family_probe(sync, config):
    if not sync.wsl_running(config['wsl_distro']):
        return {'verified_family': False, 'reason': 'wsl_stopped'}
    helper = Path(__file__).resolve().with_name('native_backend_probe.py')
    command = ['wsl.exe', '-d', config['wsl_distro'], '-u', config['wsl_user'], '--exec',
               config.get('python_wsl', '/usr/bin/python3'), sync.linux_mount_path(helper)]
    result = subprocess.run(command, capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        return {'verified_family': False, 'reason': 'process_scan_failed'}
    return json.loads(sync.decode_output(result.stdout))


def accepted_backend_family(wsl, family):
    return (type(wsl.get('backend_count')) is int and wsl['backend_count'] > 1
            and family.get('verified_family') is True and family.get('app_server_count') == 1
            and type(family.get('app_server_pid')) is int and family['app_server_pid'] > 0
            and family.get('process_count') == wsl.get('backend_count')
            and family.get('helper_count') == wsl.get('backend_count') - 1)


def probe(controller, config_path):
    if os.name != 'nt':
        raise RuntimeError('Run this probe with native Windows Python')
    controller, sync, config, runtime = load_controller(controller, config_path)
    if not sync.wsl_running(config['wsl_distro']):
        return {'eligible': False, 'candidate_ids': [], 'reason': 'wsl_stopped'}
    if not (runtime / 'ledger.json').is_file():
        return {'eligible': False, 'candidate_ids': [], 'reason': 'baseline_missing'}
    if (runtime / 'pending.json').exists():
        return {'eligible': False, 'candidate_ids': [], 'reason': 'pending_import'}
    endpoint = sync.Endpoints(config, controller.parent)
    windows, wsl = endpoint.inspect('windows'), endpoint.inspect('wsl')
    if not wsl.get('backend_detection_ok'):
        return {'eligible': False, 'candidate_ids': [], 'reason': 'backend_detection_failed'}
    # A running ordinary task can import directly without a VS Code reload.
    if not wsl.get('backend_running'):
        return {'eligible': False, 'candidate_ids': [], 'reason': 'native_sync_can_run'}
    family = None
    if wsl.get('backend_count') != 1:
        family = call_backend_family_probe(sync, config)
        if not accepted_backend_family(wsl, family):
            return {'eligible': False, 'candidate_ids': [], 'reason': 'other_native_backends',
                    'backend_count': wsl.get('backend_count'), 'process_reason': family.get('reason')}
    ledger = json.loads((runtime / 'ledger.json').read_text(encoding='utf-8'))
    if ledger.get('version') != sync.VERSION or not isinstance(ledger.get('threads'), dict):
        raise RuntimeError('Unsupported ledger')
    virtual, proofs = virtual_local_settings_ledger(sync, windows, wsl, ledger, config.get('quiet_seconds', 60),
        lambda tid, baseline: call_native_probe(sync, config, controller, tid, baseline))
    summary = classify_plan(sync, windows, wsl, virtual, config.get('quiet_seconds', 60))
    summary['needs_reconciliation_ids'] = sorted(proofs)
    summary['local_settings_reconciliation_count'] = len(proofs)
    if family is not None:
        summary['native_backend_pid'] = family['app_server_pid']
        summary['owned_helper_count'] = family['helper_count']
    return {**summary,
            'wsl_distro': config['wsl_distro'], 'runtime_directory': str(runtime),
            'operation_timeout_seconds': config.get('operation_timeout_seconds', 120),
            'run_budget_seconds': config.get('run_budget_seconds', 120),
            'controller_timeout_seconds': config.get('run_budget_seconds', 120) + 3 * config.get('operation_timeout_seconds', 120) + 60}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--json', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--recovery-state', action='store_true')
    mode.add_argument('--sync-once', action='store_true')
    args = parser.parse_args()
    try:
        value = (recovery_state(args.controller, args.config) if args.recovery_state else
                 sync_once(args.controller, args.config) if args.sync_once else probe(args.controller, args.config))
        code = 0
    except Exception as error:
        # Paths and endpoint errors are local diagnostics; never include messages.
        value = {'eligible': False, 'candidate_ids': [], 'reason': type(error).__name__}
        code = 1
    print(json.dumps(value, ensure_ascii=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
