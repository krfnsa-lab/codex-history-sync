'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { Coordinator, validateConfig, runWindows, describeResult } = require('../bridge');

test('missing Windows helper reports an actionable error without revealing paths', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'codex-missing-helper-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const executable = path.join(directory, 'fake-python');
  await fs.writeFile(executable, '#!/bin/sh\nprintf "%s\\n" "python.exe: can\'t open file synthetic-private-path: [Errno 2]" >&2\nexit 2\n', { mode: 0o700 });
  const config = { windowsPython: executable, previewHelper: 'C:\\Sync\\missing.py', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json' };
  await assert.rejects(runWindows(config, true), error => error.message.includes('重新安装同步桥') && !error.message.includes('synthetic-private-path'));
});

async function fixture(t, changes = {}) {
  const stateDir = await fs.mkdtemp(path.join(os.tmpdir(), 'codex-live-test-'));
  await fs.chmod(stateDir, 0o700);
  t.after(() => fs.rm(stateDir, { recursive: true, force: true }));
  const config = { stateDir, windowsPython: '/mnt/c/Python/python.exe', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json', previewHelper: 'C:\\Sync\\preview.py' };
  const instances = [{ instance_id: 'one', pid: 101, parent_pid: process.pid, nonce: 'test-secret-nonce-not-logged', socket_path: path.join(stateDir, 'one.sock') }];
  const calls = [];
  const dependencies = {
    discover: async () => instances,
    request: async (instance, action) => {
      calls.push(action);
      return action === 'prepare_pause' ? { ok: true, paused: true } : { ok: true, state: { busy: false, idle_seconds: 60, paused: calls.includes('prepare_pause'), child_running: !calls.includes('prepare_pause') } };
    },
    runWindows: async (_, mode) => {
      calls.push(mode === 'recovery' ? 'recovery' : mode ? 'preview' : 'sync');
      return mode === 'recovery' ? { safe_to_reload: true } : mode ? { eligible: true, candidate_ids: ['fake-test-thread'] } : { status: 'completed', transferred: 1 };
    },
    reload: async () => { calls.push('reload'); },
    onFailure: async () => { calls.push('disable-review'); },
    ...changes
  };
  return { config, instances, calls, coordinator: new Coordinator(config, dependencies) };
}

test('reject malformed paths before any process starts', () => {
  assert.throws(() => validateConfig({ stateDir: '../relative' }));
  assert.throws(() => validateConfig({ stateDir: '/tmp/state', windowsPython: '/mnt/c/python.exe', windowsController: 'relative.py' }));
});

test('family proof belonging to another native backend cannot pause this window', async t => {
  const f = await fixture(t, {
    runWindows: async () => ({ eligible: true, candidate_ids: ['synthetic'], native_backend_pid: 300 }),
    request: async () => ({ ok: true, state: { busy: false, child_pid: 400, idle_seconds: 120 } })
  });
  const result = await f.coordinator.check();
  assert.equal(result.reason, 'instances_changed');
  assert.equal(f.calls.includes('sync'), false);
  assert.equal(f.calls.includes('reload'), false);
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
});

test('a verified family still waits while its own backend is busy', async t => {
  const f = await fixture(t, {
    runWindows: async () => ({ eligible: true, candidate_ids: ['synthetic'], native_backend_pid: 300 }),
    request: async () => ({ ok: true, state: { busy: true, child_pid: 300, idle_seconds: 120 } })
  });
  assert.equal((await f.coordinator.check()).reason, 'codex_busy');
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
});

test('a verified idle family belonging to this window uses normal guarded import', async t => {
  const f = await fixture(t);
  const originalRequest = f.coordinator.request;
  const originalRun = f.coordinator.runWindows;
  f.coordinator.request = async (...args) => {
    const value = await originalRequest(...args);
    if (value.state) value.state.child_pid = 300;
    return value;
  };
  f.coordinator.runWindows = async (...args) => {
    const value = await originalRun(...args);
    if (args[1] === true) value.native_backend_pid = 300;
    return value;
  };
  assert.equal((await f.coordinator.check()).kind, 'synced');
  assert.ok(f.calls.includes('prepare_pause'));
  assert.ok(f.calls.includes('sync'));
  assert.ok(f.calls.includes('reload'));
});

test('no eligible updates never pauses or reloads', async t => {
  const f = await fixture(t, { runWindows: async () => ({ eligible: false, candidate_ids: [], reason: 'no_pending_updates' }) });
  assert.equal((await f.coordinator.check()).kind, 'current');
  assert.deepEqual(f.calls, []);
});

test('conflict and blocked previews preserve their reason without pausing', async t => {
  for (const [reason, kind] of [['chat_conflict', 'conflict'], ['pending_import', 'blocked'], ['chat_busy', 'waiting'], ['other_native_backends', 'waiting'], ['baseline_missing', 'blocked']]) {
    const preview = { eligible: false, candidate_ids: [], reason, blocked_reason_counts: { both_sides_changed: 1 } };
    const f = await fixture(t, { runWindows: async () => preview });
    const result = await f.coordinator.check();
    assert.equal(result.kind, kind);
    assert.equal(result.reason, reason);
    assert.equal(result.preview, preview);
    assert.deepEqual(f.calls, []);
  }
});

test('an old or malformed preview cannot claim that records are current', async t => {
  for (const preview of [{ eligible: false }, { eligible: false, reason: 'nothing_ready' }, { eligible: true, candidate_ids: [] }]) {
    const f = await fixture(t, { runWindows: async () => preview });
    assert.equal((await f.coordinator.check()).kind, 'blocked');
    assert.deepEqual(f.calls, []);
  }
});

test('status explains conflict and waiting without a success check mark', () => {
  const conflict = describeResult({ kind: 'conflict', reason: 'chat_conflict', preview: { blocked_reason_counts: { both_sides_changed: 1 } } });
  assert.match(conflict.text, /未同步.*聊天冲突/);
  assert.equal(conflict.warning, true);
  assert.match(conflict.detail, /保留两端/);
  for (const reason of ['dirty_editor', 'debugging', 'codex_busy']) {
    const waiting = describeResult({ kind: 'waiting', reason });
    assert.match(waiting.text, /等待空闲.*未同步/);
    assert.ok(!waiting.text.includes('$(check)'));
    assert.ok(waiting.detail);
  }
  const current = describeResult({ kind: 'current', reason: 'no_pending_updates' });
  assert.match(current.text, /没有待同步更新/);
  assert.ok(!current.text.includes('$(check)'));
});

test('only a positive actual transfer count may display synchronized', () => {
  for (const transferred of [0, undefined, -1, 1.5, '1']) {
    const value = describeResult({ kind: 'synced', result: { transferred } });
    assert.ok(!value.text.includes('已同步'));
    assert.ok(!value.text.includes('$(check)'));
  }
  const value = describeResult({ kind: 'synced', result: { transferred: 2 }, preview: { conflict_count: 1 } });
  assert.match(value.text, /部分已同步.*仍有冲突/);
  assert.match(value.detail, /实际同步 2 条/);
  assert.equal(value.warning, true);
});

test('dirty, debugging and retry delays retain distinct waiting explanations', async t => {
  for (const reason of ['dirty_editor', 'debugging']) {
    const f = await fixture(t, { canReload: () => false, reloadBlockReason: () => reason });
    assert.equal((await f.coordinator.check()).reason, reason);
    assert.deepEqual(f.calls, []);
  }
  const f = await fixture(t, { nextAttemptAt: () => Date.now() + 10000 });
  assert.equal((await f.coordinator.check()).reason, 'retry_backoff');
  assert.deepEqual(f.calls, []);
});

test('more than one instance waits without touching them', async t => {
  const f = await fixture(t);
  f.instances.push({ instance_id: 'two' });
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.deepEqual(f.calls, ['preview']);
});

test('busy status cannot import or reload', async t => {
  const f = await fixture(t, { request: async () => ({ ok: true, state: { busy: true } }) });
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.deepEqual(f.calls, ['preview']);
});

test('a sole gateway belonging to another window is left running', async t => {
  const f = await fixture(t);
  f.instances[0].parent_pid = process.pid + 1;
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.deepEqual(f.calls, ['preview']);
});

test('missing parent identity fails closed', async t => {
  const f = await fixture(t);
  delete f.instances[0].parent_pid;
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.deepEqual(f.calls, ['preview']);
});

test('another existing pause lease remains untouched', async t => {
  const f = await fixture(t);
  const other = path.join(f.config.stateDir, 'pause.json');
  await fs.writeFile(other, '{"nonce":"another-owner"}', { mode: 0o600 });
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.equal(await fs.readFile(other, 'utf8'), '{"nonce":"another-owner"}');
  assert.ok(!f.calls.includes('prepare_pause'));
});

test('completed sync removes own lease before reload', async t => {
  const f = await fixture(t);
  assert.equal((await f.coordinator.check()).kind, 'synced');
  assert.deepEqual(f.calls, ['preview', 'status', 'prepare_pause', 'status', 'sync', 'recovery', 'reload']);
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
});

test('new RPC between idle status and pause aborts without interrupting it', async t => {
  const f = await fixture(t, { request: async (_, action) => action === 'prepare_pause' ? { ok: false, error: 'busy' } : { ok: true, state: { busy: false, idle_seconds: 60 } } });
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.deepEqual(f.calls, ['preview']);
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
});

test('failed import with pending journal preserves the gate and never reloads the backend', async t => {
  const f = await fixture(t);
  const pending = path.join(f.config.stateDir, 'pending.json');
  await fs.writeFile(pending, '{"unfinished":true}', { mode: 0o600 });
  f.coordinator.runWindows = async (_, mode) => {
    if (mode === 'recovery') { f.calls.push('recovery'); return { safe_to_reload: false, reason: 'pending_import' }; }
    return mode ? { eligible: true, candidate_ids: ['fake'] } : Promise.reject(new Error('failed import'));
  };
  await assert.rejects(f.coordinator.check(), /已保留暂停保护/);
  assert.deepEqual(f.calls, ['status', 'prepare_pause', 'status', 'recovery', 'disable-review']);
  assert.equal(await fs.readFile(pending, 'utf8'), '{"unfinished":true}');
  await fs.access(path.join(f.config.stateDir, 'pause.json'));
});

test('failed controller without outstanding work reloads only after recovery verification', async t => {
  const f = await fixture(t);
  f.coordinator.runWindows = async (_, mode) => {
    f.calls.push(mode === 'recovery' ? 'recovery' : mode ? 'preview' : 'sync');
    if (mode === 'recovery') return { safe_to_reload: true };
    if (mode) return { eligible: true, candidate_ids: ['fake'] };
    throw new Error('failed controller');
  };
  await assert.rejects(f.coordinator.check(), /failed controller/);
  assert.deepEqual(f.calls.slice(-3), ['recovery', 'disable-review', 'reload']);
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
});

test('recovery helper exception fails closed and retains the gate', async t => {
  const f = await fixture(t);
  const original = f.coordinator.runWindows;
  f.coordinator.runWindows = async (config, mode) => {
    if (mode === 'recovery') throw new Error('helper unavailable');
    return original(config, mode);
  };
  await assert.rejects(f.coordinator.check(), /已保留暂停保护/);
  await fs.access(path.join(f.config.stateDir, 'pause.json'));
  assert.ok(!f.calls.includes('reload'));
  assert.ok(f.calls.includes('disable-review'));
});

test('a completed controller does not unlock a still-running importer', async t => {
  const f = await fixture(t);
  const original = f.coordinator.runWindows;
  f.coordinator.runWindows = async (config, mode) => mode === 'recovery' ? { safe_to_reload: false, reason: 'endpoint_importer_running' } : original(config, mode);
  await assert.rejects(f.coordinator.check(), /已保留暂停保护/);
  await fs.access(path.join(f.config.stateDir, 'pause.json'));
  assert.ok(!f.calls.includes('reload'));
  assert.ok(f.calls.includes('disable-review'));
});

test('a completed controller does not unlock an active controller run lock', async t => {
  const f = await fixture(t);
  const original = f.coordinator.runWindows;
  f.coordinator.runWindows = async (config, mode) => mode === 'recovery' ? { safe_to_reload: false, reason: 'controller_running' } : original(config, mode);
  await assert.rejects(f.coordinator.check(), /已保留暂停保护/);
  await fs.access(path.join(f.config.stateDir, 'pause.json'));
  assert.ok(!f.calls.includes('reload'));
});

test('partial backend stop failure blocks import and requests review before restoring the window', async t => {
  const f = await fixture(t, { request: async (_, action) => action === 'prepare_pause' ? { ok: false, error: 'stop_failed', state: { shutdown_failed: true } } : { ok: true, state: { busy: false, idle_seconds: 60 } } });
  await assert.rejects(f.coordinator.check(), /postponed/);
  assert.ok(!f.calls.includes('sync'));
  assert.deepEqual(f.calls, ['preview', 'recovery', 'disable-review', 'reload']);
});

test('new instance after global gate aborts before import', async t => {
  const f = await fixture(t);
  let count = 0;
  f.coordinator.discover = async () => ++count === 3 ? [...f.instances, { instance_id: 'two' }] : f.instances;
  await assert.rejects(f.coordinator.check(), /new Codex window/);
  assert.ok(!f.calls.includes('sync'));
  assert.deepEqual(f.calls.slice(-2), ['disable-review', 'reload']);
});

test('dirty editor/debug guard prevents all checks', async t => {
  const f = await fixture(t, { canReload: () => false });
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.deepEqual(f.calls, []);
});

test('missing idle duration or a recent native request cannot pause', async t => {
  for (const idle_seconds of [undefined, 59, NaN]) {
    const f = await fixture(t, { request: async () => ({ ok: true, state: { busy: false, idle_seconds } }) });
    assert.equal((await f.coordinator.check()).kind, 'waiting');
    assert.deepEqual(f.calls, ['preview']);
  }
});

test('zero transfers restores the backend but reports waiting and saves a reload-resistant delay', async t => {
  const f = await fixture(t);
  let deferredUntil = 0;
  const run = f.coordinator.runWindows;
  f.coordinator.runWindows = (config, mode) => mode === false ? Promise.resolve({ status: 'completed', transferred: 0 }) : run(config, mode);
  f.coordinator.onDeferred = async value => { deferredUntil = value; };
  f.coordinator.nextAttemptAt = () => deferredUntil;
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.ok(deferredUntil > Date.now());
  assert.equal(f.calls.filter(call => call === 'reload').length, 1);
  const before = f.calls.length;
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.equal(f.calls.length, before);
});

test('an already-running scheduled check is never reported as a successful transfer', async t => {
  const f = await fixture(t);
  const run = f.coordinator.runWindows;
  f.coordinator.runWindows = (config, mode) => mode === false ? Promise.resolve({ status: 'skipped_already_running' }) : run(config, mode);
  assert.equal((await f.coordinator.check()).kind, 'waiting');
  assert.equal(f.calls.filter(call => call === 'reload').length, 1);
});

test('a dirty document appearing during import prevents the final reload and retains protection', async t => {
  let checks = 0;
  const f = await fixture(t, { canReload: () => ++checks < 4 });
  await assert.rejects(f.coordinator.check(), /未保存内容/);
  await fs.access(path.join(f.config.stateDir, 'pause.json'));
  assert.ok(!f.calls.includes('reload'));
  assert.ok(f.calls.includes('disable-review'));
});

