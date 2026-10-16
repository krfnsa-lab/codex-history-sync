'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { Coordinator, validateConfig, runWindows, runMergeWindows, mergeArguments, describeResult } = require('../bridge');

test('a longer WSL winner explains waiting for the desktop backend without pausing', async t => {
  const f = await fixture(t, { runWindows: async () => ({ eligible: false, reason: 'longer_wsl_pending', conflict_resolution: 'prefer_longer' }) });
  const result = await f.coordinator.check();
  assert.equal(result.kind, 'waiting');
  assert.equal(f.calls.includes('prepare_pause'), false);
  assert.equal(f.calls.includes('reload'), false);
  const display = describeResult(result);
  assert.match(display.detail, /已选择 WSL/);
  assert.match(display.detail, /桌面端 Codex 后端停止/);
  assert.doesNotMatch(display.text, /已同步/);
});

test('equal or unverifiable longer choices keep conflicts and successful choices report backups', () => {
  const conflict = describeResult({ kind: 'conflict', reason: 'chat_conflict', preview: { conflict_resolution: 'prefer_longer' } });
  assert.match(conflict.detail, /数量相同/);
  assert.match(conflict.detail, /未自动选择/);
  const success = describeResult({ kind: 'synced', result: { transferred: 1, resolved_conflicts: 1 }, preview: { conflict_count: 0 } });
  assert.match(success.detail, /被替换的版本已备份/);
});

test('busy selected histories report a chosen version while waiting', () => {
  const display = describeResult({ kind: 'waiting', reason: 'chat_busy', preview: {
    conflict_resolution: 'prefer_longer', conflict_decisions: [
      { source: 'windows', waiting_reason: 'source_busy' },
      { source: 'wsl', waiting_reason: 'target_backend_running' }
    ]
  } });
  assert.match(display.text, /已选较多记录/);
  assert.match(display.detail, /Windows 1 条；WSL 1 条/);
  assert.match(display.detail, /等待桌面端后端停止/);
  assert.doesNotMatch(display.text, /已同步/);
});

test('manual conflict success describes the human choice rather than the longer history', () => {
  const display = describeResult({ kind: 'synced', result: { transferred: 1, resolved_conflicts: 1,
    conflict_results: [{ reason: 'manual_keep', source: 'wsl', target: 'windows' }] } });
  assert.match(display.detail, /按你的选择保留版本/);
  assert.match(display.detail, /被替换的版本已备份/);
  assert.doesNotMatch(display.detail, /数量|较多/);
});

test('saved manual choices distinguish waiting from stale confirmation', () => {
  const waiting = describeResult({ kind: 'waiting', reason: 'opposite_direction_pending', preview: {
    manual_choices: [{ keep_side: 'wsl', status: 'waiting', reason: 'target_backend_running' }]
  } });
  assert.match(waiting.detail, /已手动选择：WSL 1 条/);
  assert.match(waiting.detail, /尚未同步/);
  const stale = describeResult({ kind: 'conflict', reason: 'chat_conflict', preview: {
    manual_choices: [{ keep_side: 'wsl', reason: 'manual_choice_stale' }], blocked_reason_counts: { manual_choice_stale: 1 }
  } });
  assert.match(stale.detail, /重新检查并确认/);
  assert.doesNotMatch(stale.detail, /已手动选择：/);
});

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
      return action === 'prepare_pause' ? { ok: true, paused: true } : { ok: true, state: { ...healthyRecoveryState, paused: calls.includes('prepare_pause'), child_running: !calls.includes('prepare_pause') } };
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

const healthyRecoveryState = { busy: false, paused: false, child_running: true, child_pid: 202,
  starting: false, shutdown_failed: false, idle_seconds: 60, pending_request_count: 0,
  server_request_count: 0, active_turn_count: 0, known_activity_count: 0 };

test('review recovery proves health twice and never pauses, imports or reloads', async t => {
  const f = await fixture(t);
  f.coordinator.request = async (_, action) => { f.calls.push(action); return { ok: true, state: healthyRecoveryState }; };
  assert.deepEqual(await f.coordinator.recover(), { kind: 'recovered', reason: 'recovery_verified' });
  assert.deepEqual(f.calls, ['status', 'recovery', 'status']);
  assert.equal(f.coordinator.running, false);
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
});

test('review recovery preserves an existing pause, even a symlink', async t => {
  const f = await fixture(t);
  const pause = path.join(f.config.stateDir, 'pause.json');
  await fs.symlink(path.join(f.config.stateDir, 'missing'), pause);
  assert.equal((await f.coordinator.recover()).reason, 'existing_pause');
  assert.deepEqual(f.calls, []);
  assert.equal((await fs.lstat(pause)).isSymbolicLink(), true);
});

test('review recovery keeps busy, unhealthy and incomplete gateway states disabled', async t => {
  for (const changes of [{ busy: true }, { idle_seconds: 59 }, { child_running: false }, { paused: true },
    { shutdown_failed: true }, { starting: true }, { pending_request_count: 1 }, { active_turn_count: 1 },
    { child_pid: undefined }, { shutdown_failed: undefined }, { known_activity_count: undefined }]) {
    const f = await fixture(t, { request: async () => ({ ok: true, state: { ...healthyRecoveryState, ...changes } }) });
    assert.notEqual((await f.coordinator.recover()).kind, 'recovered');
    assert.deepEqual(f.calls, []);
  }
});

test('review recovery refuses another window and multiple instances', async t => {
  const f = await fixture(t);
  f.instances[0].parent_pid = process.pid + 1;
  assert.equal((await f.coordinator.recover()).reason, 'other_window');
  f.instances.push({ instance_id: 'two' });
  assert.equal((await f.coordinator.recover()).reason, 'multiple_windows');
  assert.deepEqual(f.calls, []);
});

test('review recovery requires a safe Windows proof and unchanged instance and lease state', async t => {
  for (const mutation of ['pending', 'new_instance', 'pause', 'busy', 'child_changed']) {
    const f = await fixture(t);
    let busy = false;
    let child_pid = healthyRecoveryState.child_pid;
    f.coordinator.discover = async () => f.instances.map(instance => ({ ...instance }));
    f.coordinator.request = async () => ({ ok: true, state: { ...healthyRecoveryState, busy, child_pid } });
    f.coordinator.runWindows = async (_, mode) => {
      assert.equal(mode, 'recovery');
      if (mutation === 'new_instance') f.instances[0] = { ...f.instances[0], nonce: 'changed-nonce' };
      if (mutation === 'pause') await fs.writeFile(path.join(f.config.stateDir, 'pause.json'), 'protected', { mode: 0o600 });
      if (mutation === 'busy') busy = true;
      if (mutation === 'child_changed') child_pid += 1;
      return { safe_to_reload: mutation !== 'pending' };
    };
    assert.notEqual((await f.coordinator.recover()).kind, 'recovered');
    assert.equal(f.coordinator.running, false);
  }
});

test('review recovery rejects a public state directory before contacting anything', async t => {
  const f = await fixture(t);
  await fs.chmod(f.config.stateDir, 0o755);
  await assert.rejects(f.coordinator.recover(), /private/);
  assert.deepEqual(f.calls, []);
  assert.equal(f.coordinator.running, false);
});

test('review recovery exceptions release only the local check flag', async t => {
  const f = await fixture(t, { request: async () => { throw new Error('unavailable'); } });
  await assert.rejects(f.coordinator.recover(), /unavailable/);
  assert.equal(f.coordinator.running, false);
  f.coordinator.running = true;
  assert.equal((await f.coordinator.recover()).reason, 'check_running');
  assert.deepEqual(f.calls, []);
});

test('reject malformed paths before any process starts', () => {
  assert.throws(() => validateConfig({ stateDir: '../relative' }));
  assert.throws(() => validateConfig({ stateDir: '/tmp/state', windowsPython: '/mnt/c/python.exe', windowsController: 'relative.py' }));
});

test('invalid reload modes and timer values are rejected before any process starts', () => {
  const config = { stateDir: '/tmp/state', windowsPython: '/mnt/c/python.exe', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json', previewHelper: 'C:\\Sync\\preview.py' };
  for (const changes of [{ reloadMode: 'sometimes' }, { intervalSeconds: NaN }, { intervalSeconds: 29 }, { intervalSeconds: '60' },
    { controllerTimeoutSeconds: Infinity }, { controllerTimeoutSeconds: 179 }, { controllerTimeoutSeconds: 180.5 }]) assert.throws(() => validateConfig({ ...config, ...changes }));
  assert.doesNotThrow(() => validateConfig({ ...config, reloadMode: 'confirm', intervalSeconds: 60, controllerTimeoutSeconds: 600 }));
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

test('manual retry bypasses only the clock delay and retains confirmation and busy guards', async t => {
  const f = await fixture(t, { nextAttemptAt: () => Date.now() + 300000,
    confirmReload: async () => false });
  assert.equal((await f.coordinator.check()).reason, 'retry_backoff');
  assert.deepEqual(f.calls, []);
  assert.equal((await f.coordinator.check({ manual: true })).reason, 'reload_confirmation');
  assert.equal(f.calls.includes('prepare_pause'), false);
  const busy = await fixture(t, { nextAttemptAt: () => Date.now() + 300000,
    request: async () => ({ ok: true, state: { ...healthyRecoveryState, busy: true } }) });
  assert.equal((await busy.coordinator.check({ manual: true })).reason, 'codex_busy');
  assert.equal(busy.calls.includes('sync'), false);
});

test('zero-transfer reason survives automatic retry without retaining arbitrary server text', async t => {
  let until = 0; let saved = {};
  const f = await fixture(t, { nextAttemptAt: () => until, retryDetails: () => saved,
    onDeferred: async (value, counts) => { until = value; saved = counts; } });
  const run = f.coordinator.runWindows;
  f.coordinator.runWindows = (config, mode) => mode === false ? Promise.resolve({ status: 'completed', transferred: 0,
    deferred: [{ reason: 'environment_record_too_large', id: 'private-chat' }, { reason: 'private-server-text' }] }) : run(config, mode);
  const zero = await f.coordinator.check({ manual: true });
  assert.equal(zero.reason, 'zero_transfers');
  assert.deepEqual(saved, { environment_record_too_large: 1, other: 1 });
  const delayed = await f.coordinator.check();
  assert.equal(delayed.reason, 'retry_backoff');
  assert.ok(delayed.retry_after_seconds > 0 && delayed.retry_after_seconds <= 300);
  const text = describeResult(delayed).detail;
  assert.match(text, /格式兼容性/);
  assert.match(text, /手动重新检查/);
  assert.doesNotMatch(text, /private-server-text|private-chat/);
});

test('retry diagnostics cannot turn a committed transfer into failure', async t => {
  const f = await fixture(t, { onDeferred: async () => { throw new Error('storage failure'); } });
  assert.equal((await f.coordinator.check({ manual: true })).kind, 'synced');
  assert.equal(f.calls.includes('disable-review'), false);
  assert.equal(f.calls.includes('reload'), true);
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
  const f = await fixture(t, { request: async (_, action) => action === 'prepare_pause' ? { ok: false, error: 'busy' } : { ok: true, state: { ...healthyRecoveryState } } });
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
  const f = await fixture(t, { request: async (_, action) => action === 'prepare_pause' ? { ok: false, error: 'stop_failed', state: { shutdown_failed: true } } : { ok: true, state: { ...healthyRecoveryState } } });
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

test('confirmation mode previews eligible updates without pausing, importing or reloading', async t => {
  const f = await fixture(t, { confirmReload: async manual => manual });
  assert.equal((await f.coordinator.check()).reason, 'reload_confirmation');
  assert.deepEqual(f.calls, ['preview', 'status']);
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
  assert.equal((await f.coordinator.check({ manual: true })).kind, 'synced');
});

test('disable while preview or confirmation is pending cancels before creating a pause lease', async t => {
  for (const stage of ['preview', 'confirmation']) {
    let enabled = true;
    const f = await fixture(t, { shouldContinue: () => enabled });
    if (stage === 'preview') {
      const original = f.coordinator.runWindows;
      f.coordinator.runWindows = async (...args) => { const value = await original(...args); enabled = false; return value; };
    } else f.coordinator.confirmReload = async () => { enabled = false; return true; };
    assert.equal((await f.coordinator.check({ manual: true })).reason, 'sync_disabled');
    assert.ok(!f.calls.includes('prepare_pause'));
    assert.ok(!f.calls.includes('sync'));
    assert.ok(!f.calls.includes('reload'));
    await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
  }
});

test('disable after pausing skips import and safely restores the already-paused backend', async t => {
  let enabled = true;
  const f = await fixture(t, { shouldContinue: () => enabled });
  const original = f.coordinator.request;
  f.coordinator.request = async (...args) => { const value = await original(...args); if (args[1] === 'prepare_pause') enabled = false; return value; };
  assert.equal((await f.coordinator.check()).reason, 'sync_disabled');
  assert.ok(!f.calls.includes('sync'));
  assert.ok(f.calls.includes('recovery'));
  assert.ok(f.calls.includes('reload'));
  await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
});

test('missing or malformed idle health fields never admit a pause', async t => {
  for (const change of [{ busy: undefined }, { busy: 0 }, { child_running: undefined }, { shutdown_failed: undefined },
    { starting: undefined }, { paused: undefined }, { child_pid: 0 }, { child_pid: '202' },
    { pending_request_count: undefined }, { server_request_count: undefined }, { active_turn_count: undefined }, { known_activity_count: undefined }]) {
    const f = await fixture(t, { request: async () => ({ ok: true, state: { ...healthyRecoveryState, ...change } }) });
    assert.equal((await f.coordinator.check()).reason, 'codex_busy');
    assert.deepEqual(f.calls, ['preview']);
    await assert.rejects(fs.access(path.join(f.config.stateDir, 'pause.json')));
  }
});

test('a malformed paused acknowledgment blocks import and requires verified recovery', async t => {
  const f = await fixture(t);
  const original = f.coordinator.request;
  f.coordinator.request = async (...args) => { const value = await original(...args); if (value.state?.paused) delete value.state.shutdown_failed; return value; };
  await assert.rejects(f.coordinator.check(), /has not paused/);
  assert.ok(!f.calls.includes('sync'));
  assert.ok(f.calls.includes('disable-review'));
  assert.ok(f.calls.includes('recovery'));
});

test('conflict commands pass exact side and expected token without a merge or resume operation', () => {
  const config = { stateDir: '/tmp/state', windowsPython: '/mnt/c/python.exe', windowsController: 'C:\\Sync Folder\\history_sync.py', windowsConfig: 'C:\\Sync Folder\\config.json', previewHelper: 'C:\\Sync Folder\\preview_sync.py' };
  const id = '11111111-1111-7111-8111-111111111111';
  const expectedToken = 'a'.repeat(64);
  assert.deepEqual(mergeArguments(config, { type: 'list' }), ['C:\\Sync Folder\\merge_chat.py', '--controller', config.windowsController, '--config', config.windowsConfig, '--list']);
  for (const keepSide of ['windows', 'wsl']) assert.deepEqual(mergeArguments({ ...config, mergeHelper: 'D:\\Tools\\merge_chat.py' }, { type: 'id', value: id, keepSide, expectedToken }),
    ['D:\\Tools\\merge_chat.py', '--controller', config.windowsController, '--config', config.windowsConfig, '--id', id, '--keep-side', keepSide, '--expected-token', expectedToken]);
  for (const action of [{ type: 'delete', value: id }, { type: 'resume', value: id }, { type: 'id', value: id },
    { type: 'id', value: '--help', keepSide: 'windows', expectedToken }, { type: 'id', value: id, keepSide: 'both', expectedToken },
    { type: 'id', value: id, keepSide: 'wsl', expectedToken: '../private' }]) assert.throws(() => mergeArguments(config, action));
  assert.throws(() => mergeArguments({ ...config, mergeHelper: '/mnt/c/merge.py' }, { type: 'list' }));
});

test('merge runner handles safe JSON and hides process errors, private paths and malformed results', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'codex-merge-helper-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const executable = path.join(directory, 'fake-python');
  const config = { stateDir: directory, windowsPython: executable, windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json', previewHelper: 'C:\\Sync\\preview_sync.py' };
  await fs.writeFile(executable, '#!/bin/sh\nprintf "%s\\n" \'{"ok":true,"status":"candidates","chats":[]}\'\n', { mode: 0o700 });
  assert.deepEqual(await runMergeWindows(config, { type: 'list' }), { ok: true, status: 'candidates', chats: [] });
  for (const script of ['#!/bin/sh\nprintf "%s\\n" "private/path secret" >&2\nexit 1\n', '#!/bin/sh\nprintf "%s\\n" \'{"ok":true,"status":"unknown"}\'\n']) {
    await fs.writeFile(executable, script, { mode: 0o700 });
    await assert.rejects(runMergeWindows(config, { type: 'list' }), error => error.message.includes('恢复记录') && !error.message.includes('private/path'));
  }
});


test('progress callbacks receive distinct request UUIDs and cannot change the guarded synchronization outcome', async t => {
  const events = []; const requests = [];
  const f = await fixture(t, {
    onProgress: event => { events.push(event); throw Error('optional progress renderer failed'); },
    runWindows: async (_, mode, requestId) => {
      requests.push({ mode, requestId });
      return mode === 'recovery' ? { safe_to_reload: true } : mode === true ? { eligible: true, candidate_ids: ['test'] } : { status: 'completed', transferred: 1 };
    }
  });
  assert.equal((await f.coordinator.check()).kind, 'synced');
  assert.equal(requests.length, 3); assert.equal(new Set(requests.map(row => row.requestId)).size, 3);
  for (const row of requests) {
    assert.match(row.requestId, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    assert.ok(events.some(event => event.requestId === row.requestId && event.newOperation === true));
  }
  assert.ok(events.some(event => event.phase === 'confirming')); assert.ok(events.some(event => event.phase === 'pausing'));
  assert.ok(events.some(event => event.phase === 'reloading')); assert.equal(f.coordinator.running, false);
  assert.ok(f.calls.includes('prepare_pause')); assert.ok(f.calls.includes('reload'));
});

test('progress-path mode is read-only and request identities are passed as exact arguments', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'codex-progress-helper-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const executable = path.join(directory, 'fake-python'); const capture = path.join(directory, 'argv');
  await fs.writeFile(executable, `#!/bin/sh\nprintf '%s\\n' "$@" > '${capture.replace(/'/g, "'\\''")}'\nprintf '%s\\n' '{"progress_file":"/mnt/c/Sync/runtime/progress.json"}'\n`, { mode: 0o700 });
  const config = { windowsPython: executable, previewHelper: 'C:\\Sync\\preview_sync.py', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json' };
  assert.equal((await runWindows(config, 'progress-path')).progress_file, '/mnt/c/Sync/runtime/progress.json');
  const first = (await fs.readFile(capture, 'utf8')).trim().split('\n'); assert.ok(first.includes('--progress-path')); assert.ok(!first.includes('--sync-once'));
  const id = '55555555-5555-4555-8555-555555555555'; await runWindows(config, true, id);
  const second = (await fs.readFile(capture, 'utf8')).trim().split('\n'); assert.deepEqual(second.slice(-2), ['--progress-request-id', id]);
  await assert.rejects(runWindows(config, true, '------------------------------------'), /Invalid progress request/);
});


test('verified short idle duration is reported distinctly from a genuinely active request', async t => {
  for (const idle_seconds of [0, 25.9, 59.99]) {
    const f = await fixture(t, { request: async (_, action) => {
      f.calls.push(action); return { ok: true, state: { ...healthyRecoveryState, idle_seconds } };
    } });
    const result = await f.coordinator.check();
    assert.equal(result.reason, 'codex_busy'); assert.equal(result.idle_seconds, Math.floor(idle_seconds));
    assert.equal(result.busy_activity, undefined);
    assert.match(describeResult(result).detail, new RegExp(`后端已空闲 ${Math.floor(idle_seconds)}/60 秒`));
    assert.doesNotMatch(describeResult(result).detail, /仍有请求或任务正在运行/);
    assert.ok(!f.calls.includes('prepare_pause')); assert.ok(!f.calls.includes('reload'));
  }
  for (const active of [{ busy: true }, { pending_request_count: 1 }, { server_request_count: 1 },
    { active_turn_count: 1 }, { known_activity_count: 1 }]) {
    const f = await fixture(t, { request: async () => ({ ok: true, state: { ...healthyRecoveryState, idle_seconds: 25, ...active } }) });
    const result = await f.coordinator.check();
    assert.equal(result.reason, 'codex_busy'); assert.equal(result.idle_seconds, undefined); assert.equal(result.busy_activity, true);
    assert.match(describeResult(result).detail, /仍有请求或任务正在运行/); assert.doesNotMatch(describeResult(result).detail, /已空闲/);
    assert.ok(!f.calls.includes('sync')); assert.ok(!f.calls.includes('reload'));
  }
});

test('unknown or unhealthy gateway fields never claim a verified idle duration', async t => {
  const invalid = [{ ok: false }, { state: null }, ...[
    { busy: undefined }, { starting: true }, { paused: true }, { child_running: false }, { shutdown_failed: true },
    { child_pid: undefined }, { pending_request_count: undefined }, { server_request_count: -1 },
    { active_turn_count: '0' }, { known_activity_count: null }, { idle_seconds: -1 }, { idle_seconds: NaN },
    { idle_seconds: undefined }
  ].map(changed => ({ state: { ...healthyRecoveryState, idle_seconds: 25, ...changed } }))];
  for (const changed of invalid) {
    const f = await fixture(t, { request: async () => ({ ok: true, state: { ...healthyRecoveryState, idle_seconds: 25 }, ...changed }) });
    const result = await f.coordinator.check();
    assert.equal(result.reason, 'codex_busy'); assert.equal(result.idle_seconds, undefined); assert.equal(result.busy_activity, undefined);
    assert.doesNotMatch(describeResult(result).detail, /已空闲 \d+\/60 秒|仍有请求或任务正在运行/);
    assert.ok(!f.calls.includes('sync')); assert.ok(!f.calls.includes('reload'));
  }
});

test('recovery diagnostics retain the same verified-idle and active-task distinction without weakening guards', async t => {
  for (const changes of [{ idle_seconds: 17.8 }, { busy: true, idle_seconds: 17.8 }, { known_activity_count: undefined, idle_seconds: 17.8 }]) {
    const f = await fixture(t, { request: async () => ({ ok: true, state: { ...healthyRecoveryState, ...changes } }) });
    const result = await f.coordinator.recover();
    assert.equal(result.reason, 'codex_busy');
    if (changes.busy) { assert.equal(result.busy_activity, true); assert.equal(result.idle_seconds, undefined); }
    else if (changes.known_activity_count === undefined && Object.hasOwn(changes, 'known_activity_count')) {
      assert.equal(result.idle_seconds, undefined); assert.equal(result.busy_activity, undefined);
    } else assert.equal(result.idle_seconds, 17);
    assert.ok(!f.calls.includes('prepare_pause')); assert.ok(!f.calls.includes('recovery')); assert.ok(!f.calls.includes('reload'));
  }
});

test('idle-wait presentation is bounded and never invents idle time from malformed metadata', () => {
  const idle = describeResult({ kind: 'waiting', reason: 'codex_busy', idle_seconds: 25.8 });
  assert.match(idle.detail, /后端已空闲 25\/60 秒，等待安全同步/); assert.doesNotMatch(idle.text, /25\/60/);
  assert.match(describeResult({ kind: 'waiting', reason: 'codex_busy', busy_activity: true, idle_seconds: 25 }).detail, /仍有请求或任务正在运行/);
  for (const idle_seconds of [undefined, null, -1, 60, Infinity, NaN, 'private/path']) {
    const result = describeResult({ kind: 'waiting', reason: 'codex_busy', idle_seconds });
    assert.doesNotMatch(result.detail, /后端已空闲|private\/path/); assert.doesNotMatch(result.text, /\$\(check\)/);
  }
});
