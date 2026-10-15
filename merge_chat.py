#!/usr/bin/env python3
"""Explicit Windows/WSL conflict choices and historical merge recovery."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from history_merge import merge_packages


def load(controller, config_path):
    controller, config_path = Path(controller).resolve(strict=True), Path(config_path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location('merge_chat_controller', controller)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    raw = json.loads(config_path.read_text(encoding='utf-8-sig'))
    config = sync.normalize_config(raw, config_path) if hasattr(sync, 'normalize_config') else sync.validate_config(raw)
    runtime = sync.resolve_runtime(config, config_path) if hasattr(sync, 'resolve_runtime') else controller.parent / 'runtime'
    return sync, config, runtime, sync.Endpoints(config, controller.parent)


def candidates(sync, views, ledger, quiet):
    # The explicit merge command never adopts the count-only overwrite policy.
    plan = sync.build_plan(views['windows'], views['wsl'], ledger, quiet)
    ids = {a['id'] for a in plan['blocked'] if a['reason'] in ('both_sides_changed', 'existing_body_difference', 'initial_body_difference')}
    # An initial baseline can already contain two different bodies without a
    # later edit. They still need an explicit way to choose one version.
    ids.update(tid for tid, old in ledger.get('threads', {}).items() if old.get('body_diverged'))
    states = {s: sync.records(v) for s, v in views.items()}
    output = []
    for tid in sorted(ids):
        win, lin = states['windows'].get(tid), states['wsl'].get(tid)
        if not win or not lin:
            continue
        title = win.get('name') or lin.get('name') or win.get('title') or lin.get('title') or tid
        label = ' '.join(str(title).split())[:90]
        ready = not sync.is_busy(win, quiet) and not sync.is_busy(lin, quiet)
        ready = ready and all(r.get('transcript_complete') and r.get('item_count', 0) > 0 for r in (win, lin))
        output.append({'id': tid, 'label': label, 'ready': bool(ready), 'reason': 'ready' if ready else 'chat_busy_or_incomplete',
                       'windows_turns': win.get('turn_count', 0), 'wsl_turns': lin.get('turn_count', 0),
                       'choice_token': sync.choice_token(tid, {'windows': win, 'wsl': lin})})
    return output


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(512 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate_native(sync, config, job, package):
    """Only native Linux opens its live schema; model requests use local mock."""
    if not sync.wsl_running(config['wsl_distro']):
        raise RuntimeError('WSL stopped before validation')
    helper = Path(__file__).resolve().with_name('merge_validate.py')
    report = job / 'native-validation.json'
    script = '''import json,pathlib,os,sys
p=pathlib.Path.home()/'.config/codex-history-sync-live/launcher.json'
binary=None
if p.is_file() and not p.is_symlink():
 st=p.stat()
 if st.st_uid==os.getuid() and not st.st_mode&0o077:
  v=json.loads(p.read_text())
  if v.get('owner')=='krfnsa-lab.codex-history-live-sync': binary=v.get('binary')
if not binary or not pathlib.Path(binary).is_file():
 candidates=[]
 for meta in (pathlib.Path.home()/'.vscode-server/extensions').glob('openai.chatgpt-*/package.json'):
  v=json.loads(meta.read_text());b=meta.parent/'bin/linux-x86_64/codex'
  if v.get('publisher')=='openai' and v.get('name')=='chatgpt' and b.is_file(): candidates.append((meta.stat().st_mtime_ns,str(b)))
 if not candidates: raise SystemExit(78)
 binary=max(candidates)[1]
os.execv(sys.executable,[sys.executable,sys.argv[1],'--home',sys.argv[2],'--codex',binary,'--package',sys.argv[3],'--report',sys.argv[4]])
'''
    command = ['wsl.exe', '-d', config['wsl_distro'], '-u', config['wsl_user'], '--exec', config.get('python_wsl', '/usr/bin/python3'),
               '-c', script, sync.linux_mount_path(helper), config['wsl_home'], sync.linux_mount_path(package), sync.linux_mount_path(report)]
    result = subprocess.run(command, capture_output=True, timeout=360, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode or not report.is_file():
        raise RuntimeError('Native validation did not finish; originals have been preserved')
    value = json.loads(report.read_text(encoding='utf-8'))
    if value.get('validation_passed') is not True:
        raise RuntimeError('Native validation rejected the merged copy; originals have been preserved')
    hashes = value.get('package_hashes', {})
    if set(hashes) != {'meta.json', 'history.sqlite', 'transcript.jsonl'} or any(sha(package / n) != digest for n, digest in hashes.items()):
        raise RuntimeError('Validated package changed')
    return value


def finish_job(sync, endpoint, runtime, ledger, job, prepared):
    """Never replace an existing ID, including when recovering a partial copy."""
    package = job / 'copy' / 'merged'
    validation = json.loads((job / 'native-validation.json').read_text(encoding='utf-8'))
    if validation.get('validation_passed') is not True or validation.get('new_thread_id') != prepared['new_id']:
        raise RuntimeError('The native validation receipt does not match this job')
    if set(validation.get('package_hashes', {})) != {'meta.json', 'history.sqlite', 'transcript.jsonl'} or any(sha(package / n) != digest for n, digest in validation['package_hashes'].items()):
        raise RuntimeError('The validated package changed; recovery stopped')
    pending = runtime / 'pending.json'
    if pending.exists():
        marker = json.loads(pending.read_text(encoding='utf-8'))
        if marker.get('kind') != 'merge_copy' or marker.get('job_id') != prepared['job_id']:
            raise RuntimeError('Another unfinished import must be reviewed first')
    else:
        sync.atomic_json(pending, {'kind': 'merge_copy', 'job_id': prepared['job_id'], 'id': prepared['new_id']})
    receipts_path = job / 'receipts.json'
    receipts = json.loads(receipts_path.read_text(encoding='utf-8')) if receipts_path.exists() else {}
    for side in ('windows', 'wsl'):
        rows = sync.records(endpoint.inspect(side))
        current = rows.get(prepared['new_id'])
        if side in receipts:
            if not current or current['revision'] != receipts[side]['record']['revision']:
                raise RuntimeError('The partially imported new copy changed; originals and recovery files are preserved')
            continue
        if current is not None:
            raise RuntimeError('New ID already exists without a matching receipt; never overwrite it')
        target = str(package) if side == 'windows' else sync.linux_mount_path(package)
        result = endpoint.call(side, 'import', '--package', target, '--expected', 'absent')
        record = result.get('record', {})
        if result.get('ok') is not True or record.get('id') != prepared['new_id'] or record.get('turn_count') != prepared['merged_turns'] or record.get('item_count') != prepared['merged_items']:
            raise RuntimeError('Importer did not return the expected committed copy')
        receipts[side] = result
        sync.atomic_json(receipts_path, receipts)
    ledger['threads'][prepared['new_id']] = {'windows': receipts['windows']['record'], 'wsl': receipts['wsl']['record'],
                                             'body_diverged': False, 'merged_from': prepared['original_id']}
    # Acknowledge only the exact exported original versions now preserved in the
    # new copy. A later edit of an original remains different from this baseline.
    ledger['threads'][prepared['original_id']] = {**prepared['source_records'], 'body_diverged': True, 'superseded_by': prepared['new_id']}
    sync.atomic_json(runtime / 'ledger.json', ledger)
    prepared['status'] = 'completed'
    sync.atomic_json(job / 'job.json', prepared)
    pending.unlink()
    return {'ok': True, 'status': 'merged', 'id': prepared['new_id'], 'title': prepared['title'],
            'turn_count': prepared['merged_turns'], 'item_count': prepared['merged_items'], 'job_id': prepared['job_id']}


def execute(sync, config, runtime, endpoint, args):
    if not sync.wsl_running(config['wsl_distro']):
        return {'ok': True, 'status': 'waiting', 'reason': 'wsl_stopped'}
    with sync.run_lock(runtime / 'run.lock') as acquired:
        if not acquired:
            return {'ok': True, 'status': 'waiting', 'reason': 'controller_running'}
        ledger = json.loads((runtime / 'ledger.json').read_text(encoding='utf-8'))
        if type(ledger.get('version')) is not int or ledger.get('version') != sync.VERSION or not isinstance(ledger.get('threads'), dict):
            raise RuntimeError('Initialize a compatible baseline before merging')
        jobs = runtime / 'merges'
        if args.resume:
            job_id = str(uuid.UUID(args.resume))
            job = jobs / job_id
            if job.is_symlink() or not job.resolve().is_relative_to(jobs.resolve()):
                raise RuntimeError('Unexpected recovery path')
            prepared = json.loads((job / 'job.json').read_text(encoding='utf-8'))
            if prepared['job_id'] != job_id:
                raise RuntimeError('Recovery job identity changed')
            if prepared['status'] == 'completed':
                return {'ok': True, 'status': 'already_merged', 'id': prepared['new_id'], 'title': prepared['title']}
            return finish_job(sync, endpoint, runtime, ledger, job, prepared)
        if (runtime / 'pending.json').exists():
            marker = json.loads((runtime / 'pending.json').read_text(encoding='utf-8'))
            return {'ok': True, 'status': 'waiting', 'reason': 'pending_import', 'resume_job_id': marker.get('job_id') if marker.get('kind') == 'merge_copy' else None}
        views = {s: endpoint.inspect(s) for s in ('windows', 'wsl')}
        choices = candidates(sync, views, ledger, config.get('quiet_seconds', 60))
        if args.list:
            return {'ok': True, 'status': 'candidates', 'chats': choices}
        selected = next((c for c in choices if c['id'] == args.id), None)
        if not selected or not selected['ready']:
            return {'ok': True, 'status': 'waiting', 'reason': 'chat_busy_or_not_conflicting'}
        if getattr(args, 'keep_side', None):
            if getattr(args, 'expected_token', None) != selected['choice_token']:
                return {'ok': True, 'status': 'waiting', 'reason': 'source_changed'}
            jid = str(uuid.uuid4())
            job = runtime / 'conflict-choices' / jid
            job.mkdir(parents=True, mode=0o700)
            sources, hashes = {}, {}
            for side in ('windows', 'wsl'):
                record = sync.records(views[side])[args.id]
                destination = job / side
                native = str(destination) if side == 'windows' else sync.linux_mount_path(destination)
                try:
                    endpoint.call(side, 'export', '--id', args.id, '--package', native, '--expected', record['revision'])
                except sync.EndpointCallError as error:
                    if error.side == side and error.operation == 'export' and error.code in ('source_changed', 'source_active', 'thread_missing'):
                        return {'ok': True, 'status': 'waiting', 'reason': 'source_changed'}
                    raise
                sources[side] = record
                hashes[side] = {name: sha(destination / name) for name in ('meta.json', 'history.sqlite', 'transcript.jsonl')}
            for side in ('windows', 'wsl'):
                current = sync.records(endpoint.inspect(side)).get(args.id)
                if not current or current['revision'] != sources[side]['revision'] or sync.is_busy(current, config.get('quiet_seconds', 60)):
                    return {'ok': True, 'status': 'waiting', 'reason': 'source_changed'}
            choice = {'choice_id': jid, 'id': args.id, 'keep_side': args.keep_side, 'status': 'pending',
                      'expected': {side: record['revision'] for side, record in sources.items()},
                      'package_hashes': hashes, 'created_at': sync.utc_now()}
            for side in ('windows', 'wsl'):
                sync.conflict_choice_package(runtime, choice, side)
            sync.atomic_json(job / 'ledger-before.json', ledger)
            sync.atomic_json(job / 'choice.json', choice)
            store = sync.read_conflict_choices(runtime)
            store['choices'][args.id] = choice
            sync.atomic_json(runtime / 'conflict-choices.json', store)
            return {'ok': True, 'status': 'choice_saved', 'reason': 'waiting_for_safe_sync',
                    'choice_id': jid, 'id': args.id, 'keep_side': args.keep_side}
        jid = str(uuid.uuid4())
        job = jobs / jid
        job.mkdir(parents=True, mode=0o700)
        sources = {}
        for side in ('windows', 'wsl'):
            record = sync.records(views[side])[args.id]
            destination = job / side
            native = str(destination) if side == 'windows' else sync.linux_mount_path(destination)
            endpoint.call(side, 'export', '--id', args.id, '--package', native, '--expected', record['revision'])
            sources[side] = record
        title = selected['label'][:60] + '（Win与WSL合并副本）'
        report = merge_packages(job / 'windows', job / 'wsl', job / 'copy', title)
        validate_native(sync, config, job, job / 'copy' / 'merged')
        for side in ('windows', 'wsl'):
            rows = sync.records(endpoint.inspect(side))
            current = rows.get(args.id)
            if not current or current['revision'] != sources[side]['revision'] or current.get('active'):
                return {'ok': True, 'status': 'waiting', 'reason': 'source_changed', 'job_id': jid}
            target = str(job / 'copy' / 'merged') if side == 'windows' else sync.linux_mount_path(job / 'copy' / 'merged')
            if endpoint.call(side, 'import-preview', '--package', target).get('compatible') is not True:
                return {'ok': True, 'status': 'waiting', 'reason': 'workspace_incompatible', 'job_id': jid}
        prepared = {'job_id': jid, 'status': 'validated', 'original_id': args.id, 'new_id': report['new_thread_id'],
                    'source_records': sources, 'title': title, 'merged_turns': report['merged_turns'], 'merged_items': report['merged_items']}
        sync.atomic_json(job / 'job.json', prepared)
        sync.atomic_json(job / 'ledger-before.json', ledger)
        return finish_job(sync, endpoint, runtime, ledger, job, prepared)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller', required=True)
    parser.add_argument('--config', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--list', action='store_true')
    group.add_argument('--id')
    group.add_argument('--resume')
    parser.add_argument('--keep-side', choices=('windows', 'wsl'))
    parser.add_argument('--expected-token')
    args = parser.parse_args()
    if args.id and not args.keep_side:
        parser.error('--id requires --keep-side; only historical merge jobs may use --resume')
    if args.keep_side and (not args.id or not args.expected_token):
        parser.error('--keep-side requires --id and --expected-token from --list')
    if args.expected_token and not args.keep_side:
        parser.error('--expected-token requires --keep-side')
    try:
        if os.name != 'nt':
            raise RuntimeError('Use native Windows Python for the merge coordinator')
        sync, config, runtime, endpoint = load(args.controller, args.config)
        result = execute(sync, config, runtime, endpoint, args)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        # Keep private paths, bodies, tool output and traceback out of the UI.
        message = ('选择未完成。原记录和备份已保留；请检查同步恢复状态。' if args.keep_side
                   else '历史任务恢复未完成。原记录和备份已保留；请检查同步恢复状态。')
        print(json.dumps({'ok': False, 'status': 'error', 'reason': 'choice_requires_review' if args.keep_side else 'merge_requires_review',
                          'message': message}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    sys.exit(main())
