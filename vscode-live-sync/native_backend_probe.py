"""Read-only Linux proof of one app-server and its known code-mode workers."""
import argparse
import json
import os
from pathlib import Path
import re


def snapshot(proc_root=Path('/proc')):
    rows = []
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            name = (entry / 'comm').read_text(encoding='utf-8').strip()
            if not re.fullmatch(r'codex(?:[-_][a-z0-9_.-]+)?', name, re.I):
                continue
            fields = (entry / 'stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                # The original native process guard counts this too; fail closed.
                raise ValueError('zombie process')
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            rows.append({'pid': int(entry.name), 'ppid': int(fields[1]),
                'group': int(fields[2]), 'session': int(fields[3]), 'start': int(fields[19]),
                'uid': entry.stat().st_uid, 'exe': str((entry / 'exe').resolve(strict=True)),
                'app_server': b'app-server' in args})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return sorted(rows, key=lambda row: row['pid'])


def classify(rows, uid=None):
    uid = os.getuid() if uid is None else uid
    servers = [r for r in rows if Path(r['exe']).name == 'codex' and r['app_server']]
    result = {'verified_family': False, 'process_count': len(rows),
              'app_server_count': len(servers), 'helper_count': 0,
              'reason': 'independent_or_unknown_processes'}
    if len(servers) != 1:
        return result
    server = servers[0]
    if server['uid'] != uid or server['session'] != server['pid'] or server['group'] != server['pid']:
        return result
    helpers = [r for r in rows if r['pid'] != server['pid']]
    for helper in helpers:
        if (Path(helper['exe']).name != 'codex-code-mode-host' or helper['app_server']
                or Path(helper['exe']).parent != Path(server['exe']).parent
                or helper['ppid'] != server['pid'] or helper['session'] != server['pid']
                or helper['uid'] != uid):
            return result
    return {**result, 'verified_family': True, 'app_server_pid': server['pid'],
            'helper_count': len(helpers), 'reason': 'one_backend_with_owned_helpers'}


def probe(proc_root=Path('/proc')):
    try:
        first, second = snapshot(proc_root), snapshot(proc_root)
        if first != second:
            return {'verified_family': False, 'reason': 'processes_changed'}
        return classify(second)
    except (OSError, ValueError, IndexError, UnicodeError):
        return {'verified_family': False, 'reason': 'process_scan_failed'}


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(probe(), ensure_ascii=True))
