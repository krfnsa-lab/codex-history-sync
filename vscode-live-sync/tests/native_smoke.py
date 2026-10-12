"""Isolated official CLI transport smoke test; no authentication or model turn."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

async def main():
    with tempfile.TemporaryDirectory(prefix='cdx-live-smoke-') as folder:
        root = Path(folder)
        state = root / 'state'; state.mkdir(mode=0o700)
        home = root / 'codex'; home.mkdir(mode=0o700)
        env = dict(os.environ, CODEX_HOME=str(home), CODEX_HISTORY_SYNC_STATE_DIR=str(state))
        child = await asyncio.create_subprocess_exec('/usr/local/bin/codex-history-sync-bridge', 'app-server', env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        async def rpc(identifier, method, params):
            child.stdin.write((json.dumps({'id':identifier,'method':method,'params':params})+'\n').encode())
            await child.stdin.drain()
            while True:
                raw = await asyncio.wait_for(child.stdout.readline(), 15)
                if not raw: raise RuntimeError('Native backend exited early')
                value = json.loads(raw)
                if value.get('id') == identifier:
                    if 'error' in value: raise RuntimeError('Native backend rejected smoke RPC')
                    return value['result']
        try:
            await rpc(1, 'initialize', {'clientInfo':{'name':'history_live_smoke','version':'0.1.0'}, 'capabilities':{'experimentalApi':True}})
            child.stdin.write(b'{"method":"initialized","params":{}}\n'); await child.stdin.drain()
            result = await rpc(2, 'thread/list', {'limit':1})
            assert result.get('data') == [], 'Isolated store should be empty'
            paths = list((state / 'instances').glob('*.json')); assert len(paths)==1
            instance = json.loads(paths[0].read_text())
            assert instance['parent_pid'] == os.getpid()
            async def ipc(action, **extra):
                reader, writer = await asyncio.open_unix_connection(instance['socket_path'])
                writer.write((json.dumps({'action':action,'instance_id':instance['instance_id'],'nonce':instance['nonce'], **extra})+'\n').encode())
                await writer.drain()
                value = json.loads(await asyncio.wait_for(reader.readline(), 12))
                writer.close(); await writer.wait_closed()
                return value
            status = await ipc('status'); assert status['ok'] and not status['state']['busy']
            (state / 'pause.json').write_text('{}'); (state / 'pause.json').chmod(0o600)
            recent = await ipc('prepare_pause'); assert recent.get('error')=='recent_activity'
            paused = await ipc('prepare_pause', minimum_idle_seconds=0)
            assert paused['ok'] and paused['paused'] and not paused['state']['child_running']
            child.stdin.close(); await child.stdin.wait_closed()
            assert await asyncio.wait_for(child.wait(), 10)==0
            assert not list((state / 'instances').glob('*.json'))
            print(json.dumps({'official_native_transport':'passed','store':'isolated_empty','parent_verified':True,
                              'default_idle_guard':'passed','owned_backend_pause':'passed','clean_exit':'passed','model_turns':0}))
        finally:
            if child.returncode is None:
                child.terminate(); await child.wait()

if __name__ == '__main__': asyncio.run(main())
