"""Real stdio/socket tests, using only disposable fake native processes."""
import json
import asyncio
import importlib.util
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

GATEWAY = Path(__file__).resolve().parents[1] / "gateway.py"
FAKE = r'''#!/usr/bin/python3
import json,sys,os,subprocess,signal
if 'app-server' not in sys.argv:
 print(json.dumps({'args':sys.argv[1:]}));sys.exit()
held=[]
def send(x):print(json.dumps(x),flush=True)
for raw in sys.stdin:
 m=json.loads(raw);method=m.get('method');identifier=m.get('id')
 if not method:continue
 if method=='hold':held.append(identifier);continue
 if method=='getAuthStatus' and m.get('params',{}).get('hold'):
  held.append(identifier);continue
 if method=='thread/read' and m.get('params',{}).get('releaseAuth'):
  for i in held:send({'id':i,'result':{'released':True}})
  held.clear()
 if method=='leave-descendant-and-exit':
  descendant=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  send({'id':identifier,'result':{'descendant_pid':descendant.pid}});sys.exit()
 if method in ('leave-separated-group-and-exit','spawn-separated-group'):
  descendant=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],process_group=0,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  send({'id':identifier,'result':{'descendant_pid':descendant.pid}})
  if method=='leave-separated-group-and-exit':sys.exit()
  continue
 if method=='exit':sys.exit()
 if method=='ignore-term':signal.signal(signal.SIGTERM,signal.SIG_IGN)
 if method=='release':
  for i in held:send({'id':i,'result':{'released':True}})
  held.clear()
 if method=='turn/start':
  send({'method':'turn/started','params':{'threadId':'synthetic','turn':{'id':'turn-a','status':'inProgress'}}})
  send({'id':identifier,'result':{'turn':{'id':'turn-a','status':'inProgress'}}});continue
 if method=='thread/resume':
  send({'id':identifier,'result':{'thread':{'id':'synthetic','status':{'type':'active','activeFlags':[]},'turns':[{'id':'returned-turn','status':'inProgress'}]}}});continue
 if method=='returned-turn-only':
  send({'id':identifier,'result':{'thread':{'id':'synthetic','status':{'type':'idle'},'turns':[{'id':'returned-turn','status':'inProgress'}]}}});continue
 if method=='stored-idle-thread':send({'id':identifier,'result':{'thread':{'id':'synthetic','status':{'type':'idle'},'turns':[]}}});continue
 if method=='process/spawn':
  send({'id':identifier,'error':{'code':-1,'message':'synthetic spawn error'}} if m.get('params',{}).get('fail') else {'id':identifier,'result':{}});continue
 if method=='process-failed':send({'id':identifier,'error':{'code':-1,'message':'synthetic process error'}});continue
 if method=='process-finish':send({'method':'process/exited','params':{'processHandle':'synthetic-handle','exitCode':0}})
 if method=='hook-start':send({'method':'hook/started','params':{'threadId':'synthetic','run':{'id':'synthetic-hook'}}})
 if method=='hook-finish':send({'method':'hook/completed','params':{'threadId':'synthetic','run':{'id':'synthetic-hook'}}})
 if method=='queue-changed':send({'method':'thread/queue/changed','params':{'threadId':'synthetic'}})
 if method=='thread/queue/list':send({'id':identifier,'result':{'data':m.get('params',{}).get('entries',[]),'nextCursor':m.get('params',{}).get('nextCursor')}});continue
 if method=='thread/backgroundTerminals/list':send({'id':identifier,'result':{'data':m.get('params',{}).get('terminals',[]),'nextCursor':m.get('params',{}).get('nextCursor')}});continue
 if method=='background-item-finish':send({'method':'item/completed','params':{'threadId':'synthetic','item':{'id':'background-item','type':'commandExecution','processId':'pty-a','exitCode':None,'status':'completed'}}})
 if method=='finish':send({'method':'turn/completed','params':{'threadId':'synthetic','turn':{'id':'turn-a','status':'completed'}}})
 if method=='tool-start':send({'method':'item/started','params':{'threadId':'synthetic','item':{'id':'tool-a','type':'commandExecution'}}})
 if method=='unknown-tool-start':send({'method':'item/started','params':{'threadId':'synthetic','item':{'id':'tool-a','type':'futureToolKind'}}})
 if method=='tool-finish':send({'method':'item/completed','params':{'threadId':'synthetic','item':{'id':'tool-a','type':'commandExecution'}}})
 if method=='unknown-status':send({'method':'thread/status/changed','params':{'threadId':'synthetic','status':{'type':'futureBusyState'}}})
 if method=='idle-status':send({'method':'thread/status/changed','params':{'threadId':'synthetic','status':{'type':'idle'}}})
 if method=='server-question':send({'id':'server-a','method':'item/approval','params':{'synthetic':True}})
 if method=='fail':send({'id':identifier,'error':{'code':-123,'message':'synthetic failure'}})
 elif method=='scalar':send({'id':identifier,'result':['synthetic',123]})
 else:send({'id':identifier,'result':{'echo':m.get('params'), 'args':sys.argv[1:]}})
'''


@unittest.skipUnless(sys.platform == "linux", "WSL/Linux Unix sockets required")
class GatewayIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cxlive-", dir="/tmp")
        self.root = Path(self.temp.name).resolve()
        self.state_dir = self.root / "s"
        self.fake = self.root / "native"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.fake.chmod(0o700)
        self.process = None
        self.messages = queue.Queue()
        self.stderr = []
        self.identifier = 0

    def tearDown(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
            for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
                if stream:stream.close()
        self.temp.cleanup()

    def launch(self, native_args=None, pause_timeout=2, shutdown_timeout=5):
        args = [sys.executable,str(GATEWAY),'--binary',str(self.fake),'--state-dir',str(self.state_dir),'--pause-timeout',str(pause_timeout),'--shutdown-timeout',str(shutdown_timeout),'--',*(native_args or ['-c','features.code_mode_host=true','app-server','--analytics-default-enabled'])]
        self.process = subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        def errors():
            for line in self.process.stderr:self.stderr.append(line)
        threading.Thread(target=read,daemon=True).start()
        threading.Thread(target=errors,daemon=True).start()

    def wait(self, fn):
        end = time.monotonic()+4
        while time.monotonic()<end:
            result = fn()
            if result:return result
            time.sleep(.02)
        self.fail('isolated gateway condition timed out')

    def instance(self):
        def load():
            files=list((self.state_dir/'instances').glob('*.json'))
            if not files:return None
            try:return json.loads(files[0].read_text())
            except (OSError,ValueError):return None
        return self.wait(load)

    def control(self, action, instance=None, minimum_idle_seconds=0):
        instance=instance or self.instance()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(3);client.connect(instance['socket_path'])
            payload=dict(action=action,instance_id=instance['instance_id'],nonce=instance['nonce'])
            if minimum_idle_seconds is not None:payload['minimum_idle_seconds']=minimum_idle_seconds
            client.sendall((json.dumps(payload)+'\n').encode())
            data=b''
            while not data.endswith(b'\n'):data+=client.recv(4096)
        return json.loads(data)

    def status(self):return self.control('status')['state']

    def ready(self):self.wait(lambda:self.status()['child_running'] and not self.status()['starting'])

    def send(self, method, params=None):
        self.identifier+=1
        self.process.stdin.write(json.dumps(dict(id=self.identifier,method=method,params=params or {}))+'\n');self.process.stdin.flush()
        return self.identifier

    def response(self, identifier):
        end=time.monotonic()+3
        while time.monotonic()<end:
            message=self.messages.get(timeout=end-time.monotonic())
            if message.get('id')==identifier:return message
        self.fail('synthetic response timeout')

    def global_pause(self):
        self.state_dir.mkdir(parents=True,exist_ok=True)
        (self.state_dir/'pause.json').write_text(json.dumps({'nonce':'synthetic-pause'}))

    def assert_request_restarted_idle(self, method, params=None):
        # Request delivery and reading its response/status can be arbitrarily
        # delayed by a busy runner. A reset must still fall after this boundary;
        # comparing with a previous idle duration assumes a fast round trip.
        sent=time.monotonic()
        self.response(self.send(method,params))
        state=self.status()
        self.assertLessEqual(state['idle_seconds'],time.monotonic()-sent+.02)
        return state

    def test_passthrough_error_and_private_state(self):
        self.launch();self.ready()
        marker='synthetic-body-not-in-state'
        reply=self.response(self.send('echo',{'marker':marker}))
        self.assertEqual(reply['result']['echo'],{'marker':marker})
        self.assertEqual(reply['result']['args'],['-c','features.code_mode_host=true','app-server','--analytics-default-enabled'])
        self.assertEqual(self.response(self.send('fail'))['error']['code'],-123)
        self.assertEqual(self.response(self.send('scalar'))['result'],['synthetic',123])
        self.assertEqual(self.response(self.send('echo',['synthetic-list-params']))['result']['echo'],['synthetic-list-params'])
        state=self.instance();self.assertNotIn(marker,json.dumps(state))
        self.assertEqual(state['parent_pid'],os.getpid())
        self.assertNotIn('nonce',self.control('status')['state'])
        self.assertEqual(self.state_dir.stat().st_mode & 0o777,0o700)
        self.assertEqual(Path(state['socket_path']).stat().st_mode & 0o777,0o600)
        self.assertEqual(next((self.state_dir/'instances').glob('*.json')).stat().st_mode & 0o777,0o600)

    def test_pending_refuses_pause_without_losing_replies(self):
        self.launch();self.ready();hold=self.send('hold')
        self.wait(lambda:self.status()['pending_request_count']==1)
        self.global_pause();reply=self.control('prepare_pause')
        self.assertEqual(reply['error'],'busy');self.assertTrue(self.status()['child_running'])
        release=self.send('release');self.assertTrue(self.response(hold)['result']['released'])
        self.response(release);self.wait(lambda:not self.status()['busy'])

    def test_turn_and_tool_activity_refuse_pause(self):
        self.launch();self.ready()
        self.response(self.send('turn/start',{'threadId':'synthetic'}))
        self.wait(lambda:self.status()['active_turn_count']==1)
        self.global_pause();self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('finish'));self.wait(lambda:self.status()['active_turn_count']==0)
        self.response(self.send('tool-start'));self.wait(lambda:self.status()['known_activity_count']==1)
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('tool-finish'));self.wait(lambda:not self.status()['busy'])
        self.assertTrue(self.control('prepare_pause')['paused'])

    def test_duplicate_request_ids_do_not_hide_pending_requests(self):
        self.launch();self.ready()
        for _ in range(2):
            self.process.stdin.write(json.dumps({'id':'duplicate','method':'hold','params':{}})+'\n')
        self.process.stdin.flush()
        self.wait(lambda:self.status()['pending_request_count']==2)
        self.global_pause();self.assertEqual(self.control('prepare_pause')['error'],'busy')
        release=self.send('release')
        self.response('duplicate');self.response('duplicate');self.response(release)
        self.wait(lambda:self.status()['pending_request_count']==0)

    def test_idle_pause_kills_only_owned_child_and_rejects_late_turn(self):
        self.launch();self.ready();child=self.status()['child_pid']
        self.assertEqual(self.control('prepare_pause')['error'],'global_pause_required')
        self.global_pause();reply=self.control('prepare_pause')
        self.assertTrue(reply['ok']);self.assertTrue(reply['state']['paused'])
        self.assertFalse(reply['state']['child_running'])
        with self.assertRaises(ProcessLookupError):os.kill(child,0)
        self.assertIsNone(self.process.poll())
        late=self.response(self.send('turn/start',{'threadId':'synthetic'}))
        self.assertEqual(late['error']['code'],-32098)
        self.assertTrue((self.state_dir/'pause.json').exists())

    def test_startup_pause_blocks_child_until_owner_clears(self):
        self.global_pause();self.launch();state=self.instance()
        self.assertTrue(state['starting']);self.assertFalse(state['child_running'])
        time.sleep(.12);self.assertFalse(self.status()['child_running'])
        (self.state_dir/'pause.json').unlink();self.ready()
        self.assertIn('result',self.response(self.send('echo')))

    def test_startup_pause_timeout_never_unlocks(self):
        self.global_pause();self.launch(pause_timeout=.15)
        self.process.wait(timeout=3)
        self.assertEqual(self.process.returncode,2)
        self.assertTrue((self.state_dir/'pause.json').exists())
        self.assertFalse(list((self.state_dir/'instances').glob('*.json')))

    def test_native_server_request_is_busy_until_client_reply(self):
        self.launch();self.ready();identifier=self.send('server-question')
        self.response(identifier);self.wait(lambda:self.status()['server_request_count']==1)
        self.global_pause();self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.process.stdin.write(json.dumps({'id':'server-a','result':{'approved':False}})+'\n');self.process.stdin.flush()
        self.wait(lambda:not self.status()['busy'])
        self.assertTrue(self.control('prepare_pause')['ok'])

    def test_non_app_server_exec_preserves_arguments(self):
        self.launch(['--version','synthetic-option'])
        self.process.wait(timeout=3)
        reply=self.messages.get(timeout=2)
        self.assertEqual(reply['args'],['--version','synthetic-option'])
        self.assertFalse(self.state_dir.exists())

    def test_native_fatal_eof_cleans_owned_process_group(self):
        self.launch();self.ready()
        descendant=self.response(self.send('leave-descendant-and-exit'))['result']['descendant_pid']
        self.process.wait(timeout=3)
        def stopped():
            stat=Path('/proc')/str(descendant)/'stat'
            try:return stat.read_text().rsplit(')',1)[1].split()[0]=='Z'
            except FileNotFoundError:return True
        self.wait(stopped)
        self.assertFalse(list((self.state_dir/'instances').glob('*.json')))

    def test_native_fatal_eof_closes_pending_client_connection(self):
        self.launch();self.ready()
        self.send('exit');self.process.wait(timeout=3)
        self.assertFalse(list((self.state_dir/'instances').glob('*.json')))

    def test_owned_separate_group_is_cleaned_after_native_eof(self):
        self.launch();self.ready()
        descendant=self.response(self.send('leave-separated-group-and-exit'))['result']['descendant_pid']
        self.process.wait(timeout=3)
        def stopped():
            try:return (Path('/proc')/str(descendant)/'stat').read_text().rsplit(')',1)[1].split()[0]=='Z'
            except FileNotFoundError:return True
        self.wait(stopped)

    def test_pause_stops_owned_worker_group_and_preserves_unrelated_session(self):
        unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
        try:
            self.launch();self.ready()
            descendant=self.response(self.send('spawn-separated-group'))['result']['descendant_pid']
            self.global_pause();self.assertTrue(self.control('prepare_pause')['ok'])
            def stopped():
                try:return (Path('/proc')/str(descendant)/'stat').read_text().rsplit(')',1)[1].split()[0]=='Z'
                except FileNotFoundError:return True
            self.wait(stopped)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate();unrelated.wait(timeout=3)

    def test_unknown_sdk_status_and_tool_are_conservatively_busy(self):
        self.launch();self.ready();self.global_pause()
        self.response(self.send('unknown-status'));self.wait(lambda:self.status()['busy'])
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('idle-status'));self.wait(lambda:not self.status()['busy'])
        self.response(self.send('unknown-tool-start'));self.wait(lambda:self.status()['busy'])
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('tool-finish'));self.wait(lambda:not self.status()['busy'])

    def test_stubborn_owned_child_is_killed_after_grace_period(self):
        self.launch(shutdown_timeout=.1);self.ready()
        self.response(self.send('ignore-term'));child=self.status()['child_pid']
        self.global_pause();self.assertTrue(self.control('prepare_pause')['ok'])
        with self.assertRaises(ProcessLookupError):os.kill(child,0)

    def test_process_group_validation_failure_stays_busy(self):
        spec=importlib.util.spec_from_file_location('isolated_gateway_test',GATEWAY)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        gateway=module.Gateway(str(self.fake),['app-server'],self.state_dir)
        gateway.starting=False
        gateway.child=mock.Mock(pid=999999999,returncode=None)
        with mock.patch.object(gateway,'owned_group_live',side_effect=PermissionError('synthetic ownership mismatch')):
            self.assertFalse(asyncio.run(gateway.terminate_child()))
        self.assertTrue(gateway.state()['shutdown_failed'])
        self.assertTrue(gateway.state()['busy'])
        self.assertFalse(gateway.child_group_stopped)

    def test_process_spawn_empty_result_tracks_requested_handle_until_exit(self):
        self.launch();self.ready();self.global_pause()
        self.assertEqual(self.response(self.send('process/spawn',{'processHandle':'synthetic-handle','marker':'not-persisted'}))['result'],{})
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.assertEqual(self.status()['known_activity_count'],1)
        self.assertNotIn('not-persisted',json.dumps(self.instance()))
        self.response(self.send('process-finish'))
        self.wait(lambda:not self.status()['busy'])
        self.assertTrue(self.control('prepare_pause')['ok'])

    def test_resumed_active_thread_and_returned_inprogress_turn_refuse_pause(self):
        for method in ('thread/resume','returned-turn-only'):
            with self.subTest(method=method):
                if self.process:self.tearDown();self.setUp()
                self.launch();self.ready();self.global_pause()
                self.response(self.send(method,{'threadId':'synthetic'}))
                self.assertTrue(self.status()['busy'])
                self.assertEqual(self.control('prepare_pause')['error'],'busy')
                self.response(self.send('idle-status'))
                self.wait(lambda:not self.status()['busy'])

    def test_hooks_queue_and_background_terminals_need_explicit_clearance(self):
        self.launch();self.ready();self.global_pause()
        self.response(self.send('hook-start'))
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('idle-status'))
        self.assertTrue(self.status()['busy'])
        self.response(self.send('hook-finish'));self.wait(lambda:not self.status()['busy'])
        self.response(self.send('queue-changed'))
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('thread/queue/list',{'threadId':'synthetic','entries':[{'id':'queued'}]}))
        self.assertTrue(self.status()['busy'])
        self.response(self.send('thread/queue/list',{'threadId':'synthetic','entries':[]}))
        self.wait(lambda:not self.status()['busy'])
        self.response(self.send('thread/backgroundTerminals/list',{'threadId':'synthetic','terminals':[{'id':'running'}]}))
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('idle-status'));self.assertTrue(self.status()['busy'])
        self.response(self.send('thread/backgroundTerminals/list',{'threadId':'synthetic','terminals':[]}))
        self.wait(lambda:not self.status()['busy'])

    def test_recent_activity_guard_is_atomic_and_readonly_polling_does_not_extend_it(self):
        self.launch();self.ready();self.global_pause()
        self.assertEqual(self.control('prepare_pause',minimum_idle_seconds=60)['error'],'recent_activity')
        self.assertEqual(self.control('prepare_pause',minimum_idle_seconds=None)['error'],'recent_activity')
        time.sleep(.12);before=self.status()['idle_seconds']
        self.response(self.send('thread/read',{'threadId':'synthetic'}))
        self.assertGreaterEqual(self.status()['idle_seconds'],before)
        self.assert_request_restarted_idle('echo')
        self.assertEqual(self.control('prepare_pause',minimum_idle_seconds=True)['error'],'recent_activity')

    def test_failed_duplicate_spawn_does_not_hide_existing_process(self):
        self.launch();self.ready();self.global_pause()
        self.response(self.send('process/spawn',{'processHandle':'synthetic-handle'}))
        self.assertIn('error',self.response(self.send('process/spawn',{'processHandle':'synthetic-handle','fail':True})))
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('process-finish'));self.wait(lambda:not self.status()['busy'])
        self.assertIn('error',self.response(self.send('process/spawn',{'processHandle':'failed-handle','fail':True})))
        self.wait(lambda:not self.status()['busy'])

    def test_repeated_legacy_auth_status_queries_do_not_restart_idle_timer(self):
        self.launch();self.ready()
        self.wait(lambda:self.status()['idle_seconds']>=.12)
        before=self.status()['idle_seconds']
        for _ in range(5):
            time.sleep(.04)
            self.assertIn('result',self.response(self.send('getAuthStatus')))
            self.assertFalse(self.status()['busy'])
        self.assertGreaterEqual(self.status()['idle_seconds'],before+.15)
        self.global_pause()
        self.assertTrue(self.control('prepare_pause',minimum_idle_seconds=before+.15)['ok'])

    def test_inflight_legacy_auth_query_remains_busy_until_its_response(self):
        self.launch();self.ready()
        self.wait(lambda:self.status()['idle_seconds']>=.12)
        before=self.status()['idle_seconds']
        auth=self.send('getAuthStatus',{'hold':True})
        self.wait(lambda:self.status()['pending_request_count']==1)
        self.global_pause()
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.assertTrue(self.status()['child_running'])
        released=self.send('thread/read',{'releaseAuth':True})
        self.assertTrue(self.response(auth)['result']['released'])
        self.response(released)
        self.wait(lambda:not self.status()['busy'])
        self.assertGreaterEqual(self.status()['idle_seconds'],before)
        self.assertTrue(self.control('prepare_pause',minimum_idle_seconds=before)['ok'])

    def test_legacy_auth_poll_cannot_clear_turn_or_unknown_write_activity(self):
        self.launch();self.ready();self.global_pause()
        self.response(self.send('turn/start',{'threadId':'synthetic'}))
        self.response(self.send('getAuthStatus'))
        self.assertGreater(self.status()['active_turn_count'],0)
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('finish'))
        self.wait(lambda:not self.status()['busy'] and self.status()['idle_seconds']>=.12)
        self.assert_request_restarted_idle('synthetic/write')
        self.response(self.send('getAuthStatus'))
        self.assertEqual(self.control('prepare_pause',minimum_idle_seconds=60)['error'],'recent_activity')
        self.assertTrue(self.status()['child_running'])

    def test_idle_reset_assertion_tolerates_delayed_reads_but_rejects_no_reset(self):
        self.launch();self.ready()
        self.wait(lambda:self.status()['idle_seconds']>=.12)
        response,status=self.response,self.status
        def delayed_response(identifier):
            result=response(identifier)
            time.sleep(.2)
            return result
        def delayed_status():
            time.sleep(.2)
            return status()
        with mock.patch.object(self,'response',side_effect=delayed_response), \
                mock.patch.object(self,'status',side_effect=delayed_status):
            state=self.assert_request_restarted_idle('echo')
            self.assertGreaterEqual(state['idle_seconds'],.2)
            # An actual read-only round trip leaves the earlier timestamp in
            # place, so the same boundary assertion must fail despite delays.
            with self.assertRaises(AssertionError):
                self.assert_request_restarted_idle('thread/read',{'threadId':'synthetic'})

    def test_stored_thread_idle_response_never_overrides_observed_live_turn(self):
        self.launch();self.ready();self.global_pause()
        self.response(self.send('turn/start',{'threadId':'synthetic'}))
        self.response(self.send('stored-idle-thread',{'threadId':'synthetic'}))
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('finish'));self.wait(lambda:not self.status()['busy'])

    def test_background_item_survives_completion_and_empty_pagination_is_not_exit_proof(self):
        self.launch();self.ready();self.global_pause()
        self.response(self.send('background-item-finish'))
        self.assertEqual(self.control('prepare_pause')['error'],'busy')
        self.response(self.send('thread/backgroundTerminals/list',{'threadId':'synthetic','cursor':'second-page'}))
        self.assertTrue(self.status()['busy'])
        self.response(self.send('thread/backgroundTerminals/list',{'threadId':'synthetic','nextCursor':'more'}))
        self.assertTrue(self.status()['busy'])
        self.response(self.send('thread/backgroundTerminals/list',{'threadId':'synthetic'}))
        self.wait(lambda:not self.status()['busy'])
        self.response(self.send('queue-changed'))
        self.response(self.send('thread/queue/list',{'threadId':'synthetic','cursor':'second-page'}))
        self.assertTrue(self.status()['busy'])
        self.response(self.send('thread/queue/list',{'threadId':'synthetic'}))
        self.wait(lambda:not self.status()['busy'])


if __name__=='__main__':unittest.main()
