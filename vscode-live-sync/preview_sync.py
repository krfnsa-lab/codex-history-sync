"""Read-only Windows probe for the optional VS Code reload companion."""
import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess


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


def eligible_candidates(sync, windows, wsl, ledger, quiet_seconds):
    # Simulate only the process guard being lifted. All thread, source, baseline,
    # conflict and quiet checks remain the production planner's responsibility.
    relaxed = deepcopy(wsl)
    relaxed['backend_running'] = False
    relaxed['backend_detection_ok'] = True
    plan = sync.build_plan(windows, relaxed, ledger, quiet_seconds)
    return [a for a in plan['actions'] if a['source'] == 'windows' and a['target'] == 'wsl'
            and a['target_revision'] != 'absent']


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
    if wsl.get('backend_count') != 1:
        return {'eligible': False, 'candidate_ids': [], 'reason': 'other_native_backends'}
    ledger = json.loads((runtime / 'ledger.json').read_text(encoding='utf-8'))
    if ledger.get('version') != sync.VERSION or not isinstance(ledger.get('threads'), dict):
        raise RuntimeError('Unsupported ledger')
    candidates = eligible_candidates(sync, windows, wsl, ledger, config.get('quiet_seconds', 60))
    return {'eligible': bool(candidates), 'candidate_ids': [a['id'] for a in candidates],
            'candidate_count': len(candidates), 'reason': 'existing_replica_updates' if candidates else 'nothing_ready',
            'wsl_distro': config['wsl_distro'], 'runtime_directory': str(runtime),
            'operation_timeout_seconds': config.get('operation_timeout_seconds', 120),
            'run_budget_seconds': config.get('run_budget_seconds', 120),
            'controller_timeout_seconds': config.get('run_budget_seconds', 120) + 3 * config.get('operation_timeout_seconds', 120) + 60}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--recovery-state', action='store_true')
    args = parser.parse_args()
    try:
        value = recovery_state(args.controller, args.config) if args.recovery_state else probe(args.controller, args.config)
        code = 0
    except Exception as error:
        # Paths and endpoint errors are local diagnostics; never include messages.
        value = {'eligible': False, 'candidate_ids': [], 'reason': type(error).__name__}
        code = 1
    print(json.dumps(value, ensure_ascii=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
