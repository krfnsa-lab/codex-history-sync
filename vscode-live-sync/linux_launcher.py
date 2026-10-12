#!/usr/bin/python3
"""Persistent launcher with a per-user fallback independent of activation order."""
import json
import os
from pathlib import Path
import sys

def configuration():
    file = Path.home() / '.config/codex-history-sync-live/launcher.json'
    stat = file.stat()
    if file.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError('Launcher configuration must be private')
    return json.loads(file.read_text())

def native_binary(value):
    if value and Path(value).is_absolute() and Path(value).is_file():
        return value
    candidates = []
    for package in (Path.home() / '.vscode-server/extensions').glob('openai.chatgpt-*/package.json'):
        try:
            meta = json.loads(package.read_text())
            binary = package.parent / 'bin/linux-x86_64/codex'
            if meta.get('publisher') == 'openai' and meta.get('name') == 'chatgpt' and binary.is_file():
                candidates.append((package.stat().st_mtime_ns, str(binary)))
        except (OSError, ValueError):
            pass
    if not candidates:
        raise RuntimeError('Official WSL Codex extension is unavailable')
    return max(candidates)[1]

def main():
    try:
        config = configuration()
        binary = native_binary(os.environ.get('CODEX_HISTORY_SYNC_REAL_CLI') or config.get('binary'))
        if Path(binary).resolve() == Path(__file__).resolve():
            raise RuntimeError('Recursive launcher')
        if 'app-server' not in sys.argv[1:]:
            os.execv(binary, [binary, *sys.argv[1:]])
        gateway = config['gateway']
        state = os.environ.get('CODEX_HISTORY_SYNC_STATE_DIR') or config['state_dir']
        if not Path(gateway).is_absolute() or not Path(gateway).is_file() or not Path(state).is_absolute():
            raise RuntimeError('Incomplete bridge installation')
        os.execv('/usr/bin/python3', ['/usr/bin/python3', gateway, '--binary', binary, '--state-dir', state, '--', *sys.argv[1:]])
    except (OSError, ValueError, KeyError, RuntimeError):
        print('Codex history bridge needs a complete per-user installation.', file=sys.stderr)
        return 78

if __name__ == '__main__':
    raise SystemExit(main())
