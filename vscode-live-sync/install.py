"""Install the optional bridge; the VS Code setup command enables it separately."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import struct
import uuid
import zipfile
from build_vsix import package
from preview_sync import load_controller

ROOT = Path(__file__).resolve().parent
OWNER = 'krfnsa-lab.codex-history-live-sync'


def equivalent_launcher(source, target):
    """Ignore ZIP timestamps when the native launcher and script are identical."""
    if not target.is_file() or target.is_symlink():
        return False
    try:
        with zipfile.ZipFile(source) as a, zipfile.ZipFile(target) as b:
            if a.namelist() != b.namelist() or not a.namelist():
                return False
            offset_a = min(i.header_offset for i in a.infolist())
            offset_b = min(i.header_offset for i in b.infolist())
            with source.open('rb') as x, target.open('rb') as y:
                if x.read(offset_a) != y.read(offset_b):
                    return False
            return all(a.read(n) == b.read(n) for n in a.namelist())
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def atomic_copy(source, target, launcher=False):
    if target.is_symlink():
        raise RuntimeError('Installation target must not be a symbolic link')
    if launcher and equivalent_launcher(source, target):
        return
    temporary = target.with_name(target.name + '.install-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()

def owned_manifest(path):
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('owner') == OWNER
    except (OSError, ValueError, AttributeError):
        return False

def windows_destination():
    # MSIX apps can virtualize LocalAppData; the user profile is shared with
    # ordinary VS Code processes and the native WSL interop launcher.
    return Path.home() / '.codex-history-sync-live'

def mount(path):
    path = Path(path).resolve()
    if len(path.drive) != 2 or not path.drive[0].isalpha():
        raise ValueError('Use a local Windows drive for the installation')
    return '/mnt/' + path.drive[0].lower() + '/' + '/'.join(path.parts[1:])

def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
    if result.returncode:
        raise RuntimeError('Installation step failed: ' + result.stderr.decode('utf-8', 'replace')[-2000:])
    return result.stdout.decode('utf-8', 'replace').strip()

def install(config_path, dry_run=False, controller_path=None):
    if sys.platform != 'win32':
        raise RuntimeError('Run with native Windows Python')
    if struct.calcsize('P') != 8 or sys.version_info < (3, 11):
        raise RuntimeError('Windows x64 Python 3.11 or newer is required')
    config_path = config_path.resolve(strict=True)
    controller = (controller_path or ROOT.parent / 'history_sync.py').resolve()
    if not controller.is_file() and controller_path is None:
        controller = config_path.parent / 'history_sync.py'
    if not controller.is_file():
        raise RuntimeError('Use the existing configured history synchronizer config')
    controller, _, config, _ = load_controller(controller, config_path)
    python = Path(config['python_windows']).resolve(strict=True)
    if python != Path(sys.executable).resolve():
        raise RuntimeError('Run the installer using the synchronizer Windows Python')
    code = shutil.which('code.cmd')
    if not code:
        raise RuntimeError('VS Code command-line launcher is missing')
    distro, user = config['wsl_distro'], config['wsl_user']
    if any(any(c in str(value) for c in '%&|<>^!\r\n') for value in (code, ROOT, distro)):
        raise RuntimeError('VS Code CLI installation requires paths without shell metacharacters')
    destination = windows_destination()
    windows_apps = Path(os.environ['LOCALAPPDATA']) / 'Microsoft/WindowsApps'
    if str(windows_apps).casefold() not in [p.rstrip('\\/').casefold() for p in os.environ.get('PATH', '').split(';')]:
        raise RuntimeError('WindowsApps is not on PATH; installation cannot prove startup resolution')
    wrapper = windows_apps / 'codex-history-sync-bridge.exe'
    manifest = destination / 'launcher.json'
    legacy_manifest = Path(os.environ['LOCALAPPDATA']) / 'CodexHistorySyncLive/launcher.json'
    owned = owned_manifest(manifest) or (not manifest.exists() and owned_manifest(legacy_manifest))
    if wrapper.exists() and not owned:
        raise RuntimeError('An unrelated Windows launcher already uses this name')
    native = []
    for candidate in (Path.home() / '.vscode/extensions').glob('openai.chatgpt-*/bin/windows-x86_64/codex.exe'):
        native.append((candidate.stat().st_mtime_ns, str(candidate)))
    if not native:
        raise RuntimeError('Install the official Windows Codex extension first')
    plan = {'windows_install': str(destination), 'windows_launcher': str(wrapper), 'wsl_distro': distro,
            'wsl_user': user, 'extension': OWNER, 'automatic_sync_enabled': False}
    if dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    destination.mkdir(parents=True, exist_ok=True)
    atomic_copy(ROOT / 'preview_sync.py', destination / 'preview_sync.py')
    atomic_copy(ROOT / 'native_local_probe.py', destination / 'native_local_probe.py')
    atomic_copy(ROOT / 'native_backend_probe.py', destination / 'native_backend_probe.py')
    for name in ('merge_chat.py', 'history_merge.py', 'merge_validate.py', 'history_endpoint.py'):
        atomic_copy(ROOT.parent / name, destination / name)
    version = json.loads((ROOT / 'vscode-extension/package.json').read_text(encoding='utf-8'))['version']
    vsix = package(ROOT / ('dist/codex-history-live-sync-' + version + '.vsix'))
    atomic_copy(ROOT / 'vscode-extension/bin/windows/codex-history-sync-bridge.exe', wrapper, launcher=True)
    manifest.write_text(json.dumps({'owner': OWNER, 'binary': max(native)[1]}, indent=2), encoding='utf-8')
    setup = {'windowsPython': mount(python), 'windowsController': str(controller), 'windowsConfig': str(config_path),
             'previewHelper': str(destination / 'preview_sync.py'), 'mergeHelper': str(destination / 'merge_chat.py'),
             'intervalSeconds': 60, 'controllerTimeoutSeconds': 600}
    # Native Linux owns Linux configuration and gateway files.
    script = '''import json,os,pathlib,shutil,sys
p=json.load(sys.stdin)
home=pathlib.Path.home()
base=home/'.local/share/codex-history-sync-live'
cfg=home/'.config/codex-history-sync-live'
state=home/'.local/state/cdx-live'
for d in (base,cfg,state,state/'instances'): d.mkdir(parents=True,exist_ok=True,mode=0o700); d.chmod(0o700)
for name in ('gateway.py','linux_launcher.py'): shutil.copy2(pathlib.Path(p['source'])/name,base/name)
native=list((home/'.vscode-server/extensions').glob('openai.chatgpt-*/bin/linux-x86_64/codex'))
if not native: raise RuntimeError('Install official WSL Codex extension first')
binary=str(max(native,key=lambda x:x.stat().st_mtime_ns))
p['settings']['stateDir']=str(state)
v={'owner':p['owner'],'binary':binary,'gateway':str(base/'gateway.py'),'state_dir':str(state),'setupSettings':p['settings'],'wrappers_installed':False}
file=cfg/'launcher.json'
file.write_text(json.dumps(v,indent=2));file.chmod(0o600)
print(json.dumps({'config':str(file),'state_dir':str(state)}))
'''
    response = json.loads(run(['wsl.exe', '-d', distro, '-u', user, '--exec', '/usr/bin/python3', '-c', script],
                              input=json.dumps({'source': mount(ROOT), 'owner': OWNER, 'settings': setup}).encode()))
    root_script = '''import pathlib,shutil,sys
src=pathlib.Path(sys.argv[1]);dst=pathlib.Path('/usr/local/bin/codex-history-sync-bridge')
if dst.exists() and b'Persistent launcher with a per-user fallback' not in dst.read_bytes(): raise RuntimeError('Unrelated Linux launcher exists')
shutil.copyfile(src,dst);dst.chmod(0o755)
'''
    run(['wsl.exe', '-d', distro, '-u', 'root', '--exec', '/usr/bin/python3', '-c', root_script, mount(ROOT / 'linux_launcher.py')])
    run(['cmd.exe', '/d', '/c', code, '--install-extension', str(vsix), '--force'])
    # Use the installed native server CLI; desktop --remote does not reliably
    # select a remote extension-management target in every VS Code release.
    remote_install = '''import pathlib,subprocess,sys
root=pathlib.Path.home()/'.vscode-server'
candidates=list(root.glob('bin/*/bin/code-server'))
if not candidates: raise RuntimeError('Open a Remote WSL workspace once to install VS Code Server')
cli=max(candidates,key=lambda x:x.stat().st_mtime_ns)
r=subprocess.run([str(cli),'--extensions-dir',str(root/'extensions'),'--install-extension',sys.argv[1],'--force'],capture_output=True,timeout=120)
if r.returncode: raise RuntimeError('Native WSL extension install failed: '+r.stderr.decode('utf-8','replace')[-1000:])
print('remote_extension_installed')
'''
    run(['wsl.exe', '-d', distro, '-u', user, '--exec', '/usr/bin/python3', '-c', remote_install, mount(vsix)])
    # Verify both persistent entry points work without companion activation env.
    clean_env = {k:v for k,v in os.environ.items() if not k.startswith('CODEX_HISTORY_SYNC_')}
    windows_version = run([str(wrapper), '--version'], env=clean_env)
    linux_version = run(['wsl.exe', '-d', distro, '-u', user, '--exec', '/usr/local/bin/codex-history-sync-bridge', '--version'])
    verify = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); v=json.loads(p.read_text());v['wrappers_installed']=True;p.write_text(json.dumps(v,indent=2));p.chmod(0o600)
'''
    check_remote = '''import pathlib,sys
root=pathlib.Path.home()/'.vscode-server/extensions'
if not list(root.glob('krfnsa-lab.codex-history-live-sync-*/package.json')): raise RuntimeError('Remote VS Code extension installation was not confirmed')
print('remote_extension_confirmed')
'''
    run(['wsl.exe', '-d', distro, '-u', user, '--exec', '/usr/bin/python3', '-c', check_remote])
    run(['wsl.exe', '-d', distro, '-u', user, '--exec', '/usr/bin/python3', '-c', verify, response['config']])
    plan.update({'windows_native_version': windows_version, 'wsl_native_version': linux_version, 'setup_command': 'Codex History: Complete Setup and Enable'})
    (ROOT / 'installation-result.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(plan, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--controller', type=Path, help='Optional path to history_sync.py; defaults to this repository')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    install(args.config, args.dry_run, args.controller)
