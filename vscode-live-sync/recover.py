"""Clear only a stopped companion's pause gate after verified import completion."""
import argparse
import json
from pathlib import Path
import subprocess
from preview_sync import load_controller, recovery_state

def recover(controller, config_path):
    if recovery_state(controller, config_path).get('safe_to_reload') is not True:
        raise RuntimeError('An import, controller, or recovery journal still needs review; nothing was removed')
    controller, sync, config, runtime = load_controller(controller, config_path)
    with sync.run_lock(runtime / 'run.lock') as acquired:
        if not acquired or (runtime / 'pending.json').exists():
            raise RuntimeError('Synchronization is running or incomplete; nothing was removed')
        if sync.Endpoints(config, controller.parent).backend_status('wsl').get('backend_running', True):
            raise RuntimeError('Close the WSL VS Code window before recovering a failed pause')
        script = '''import json,os,pathlib,stat
home=pathlib.Path.home()
cfg=home/'.config/codex-history-sync-live/launcher.json'
v=json.loads(cfg.read_text()); root=pathlib.Path(v['state_dir']); gate=root/'pause.json'
def private(file):
 s=file.lstat()
 if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077: raise RuntimeError('Unsafe recovery state')
def alive(pid):
 if not isinstance(pid,int) or pid<1: raise RuntimeError('Invalid PID')
 return (pathlib.Path('/proc')/str(pid)).exists()
if not gate.exists(): print(json.dumps({'recovered':False,'reason':'no_pause_gate'})); raise SystemExit(0)
private(gate); lease=json.loads(gate.read_text())
if lease.get('owner')!='vscode-companion' or not isinstance(lease.get('nonce'),str): raise RuntimeError('Unknown pause owner')
if alive(lease.get('pid')): raise RuntimeError('Close the WSL window; its extension host is still alive')
records=[]
for file in (root/'instances').glob('*.json'):
 private(file); r=json.loads(file.read_text())
 if alive(r.get('pid')): raise RuntimeError('A gateway is still alive')
 records.append((file,r))
# Archive the exact gate before removing it. Never remove an import journal.
archive=root/('recovered-pause-'+lease['nonce']+'.json')
if archive.exists(): raise RuntimeError('Recovery archive already exists')
archive.write_bytes(gate.read_bytes()); archive.chmod(0o600)
for file,r in records:
 sock=pathlib.Path(r['socket_path'])
 if sock.parent!=(root/'instances') or sock.is_symlink(): raise RuntimeError('Unsafe socket path')
 if sock.exists(): sock.unlink()
 file.unlink()
if json.loads(gate.read_text())!=lease: raise RuntimeError('Pause owner changed')
gate.unlink()
print(json.dumps({'recovered':True,'reason':'stopped_owner_and_no_native_backend'}))
'''
        result = subprocess.run(['wsl.exe', '-d', config['wsl_distro'], '-u', config['wsl_user'], '--exec', config.get('python_wsl', '/usr/bin/python3'), '-c', script], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('Pause recovery refused; keep the gate and inspect the stopped WSL window')
        return json.loads(sync.decode_output(result.stdout))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--controller', type=Path, help='Optional path to history_sync.py; defaults to this repository')
    args = parser.parse_args()
    controller = args.controller or Path(__file__).resolve().parent.parent / 'history_sync.py'
    if not controller.is_file() and args.controller is None:
        controller = args.config.resolve().parent / 'history_sync.py'
    print(json.dumps(recover(controller, args.config.resolve())))
