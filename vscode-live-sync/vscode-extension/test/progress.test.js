'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { ProgressDisplay, validateSnapshot, validateDetail, readProgressJson, render } = require('../progress');
const run = '11111111-1111-4111-8111-111111111111';
const operation = '22222222-2222-4222-8222-222222222222';
const requestId = '33333333-3333-4333-8333-333333333333';
const other = '44444444-4444-4444-8444-444444444444';
const baseTime = Date.parse('2026-10-15T00:00:00Z');
const config = { progressFile: '/mnt/c/sync/runtime/progress.json', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json' };
const make = changes => ({ schema_version: 1, owner: 'codex-history-sync', run_id: run, operation_id: operation,
  request_id: requestId, kind: 'sync', status: 'running', phase: 'writing', started_at: new Date(baseTime).toISOString(),
  updated_at: new Date(baseTime).toISOString(), pid: 10, elapsed_seconds: 0, ...changes });
const detail = changes => ({ ...make(), request_id: undefined, phase: 'backing_up', processed_records: 12, total_records: 50, ...changes });
const settle = () => new Promise(resolve => setImmediate(resolve));
function fixture(changes = {}) {
  const logs = []; const notifications = []; const reports = []; const intervals = new Map(); const files = new Map(); let now = baseTime;
  let fallbackCalls = 0; let privateCalls = 0;
  const status = {};
  const vscode = { ProgressLocation: { Notification: 15 }, window: {
    createOutputChannel: () => ({ appendLine: line => logs.push(line), show: () => logs.push('SHOW'), dispose() {} }),
    withProgress: (options, callback) => { notifications.push(options); return callback({ report: event => reports.push(event) }); }
  } };
  const dependencies = {
    now: () => now, read: async file => { if (!files.has(file)) throw Error('missing-private-path'); return files.get(file); },
    readPrivate: async () => { privateCalls++; return {}; }, runWindows: async () => { fallbackCalls++; return { progress_file: config.progressFile }; },
    setInterval: (callback, delay) => { const id = {}; assert.equal(delay, 1000); intervals.set(id, callback); return id; },
    clearInterval: id => intervals.delete(id), ...changes
  };
  const view = new ProgressDisplay(vscode, status, dependencies);
  return { view, vscode, status, logs, reports, notifications, intervals, files,
    setNow: value => { now = value; }, get fallbackCalls() { return fallbackCalls; }, get privateCalls() { return privateCalls; },
    async begin(manual = false, override = config) { view.begin(override, manual); view.local({ phase: 'writing', requestId, newOperation: true }); await settle(); },
    async tick() { await view.tick(view.job); }, root: value => files.set(config.progressFile, value),
    detail: value => files.set(path.join(path.dirname(config.progressFile), 'progress-detail.json'), value) };
}

test('only strict progress metadata and bounded nonnegative counters are accepted', () => {
  assert.ok(validateSnapshot(make(), baseTime));
  for (const changed of [{ schema_version: 2 }, { owner: 'someone' }, { run_id: 'bad' }, { operation_id: 'bad' },
    { request_id: 'bad' }, { status: 'done' }, { kind: 'private-path' }, { phase: 'private-path' }, { elapsed_seconds: -1 },
    { total_chats: 1, processed_chats: 2 }, { pid: 0 }, { side: 'private-path' }, { transferred_chats: -1 },
    { total_chats: '10' }, { updated_at: 'tomorrow' }, { updated_at: '2026-10-16T00:00:00Z' }]) {
    assert.equal(validateSnapshot(make(changed), baseTime), null, JSON.stringify(changed));
  }
});

test('detail must belong to the root current run and operation; unknown detail is ignored', () => {
  assert.ok(validateDetail(detail(), make(), baseTime));
  for (const changed of [{ owner: 'wrong' }, { schema_version: 2 }, { run_id: other }, { operation_id: other },
    { phase: 'private-path' }, { status: 'done' }, { elapsed_seconds: -1 }, { pid: 0 }, { updated_at: 'tomorrow' }, { processed_records: 99, total_records: 2 }]) assert.equal(validateDetail(detail(changed), make(), baseTime), null);
});

test('formatted counts distinguish processed, actually synchronized and deferred without invented percentages', () => {
  const text = render(make({ processed_chats: 2, total_chats: 5, transferred_chats: 1, deferred_chats: 1 }), detail(), 12);
  assert.match(text, /已处理 2\/5 条/); assert.match(text, /已同步 1 条，暂缓 1 条/); assert.match(text, /记录 12\/50/);
  assert.doesNotMatch(text, /%/);
  assert.doesNotMatch(render(make(), null, 2), /0\/0|100%/);
});

test('old terminals and another request cannot impersonate current work', async () => {
  const f = fixture(); f.root(make({ status: 'completed' })); await f.begin();
  assert.equal(f.view.job.snapshot, null);
  f.root(make({ request_id: other })); await f.tick(); assert.equal(f.view.job.snapshot, null);
  f.root(make()); await f.tick(); assert.equal(f.view.job.snapshot.run_id, run);
  f.view.local({ phase: 'recovery', requestId: other, newOperation: true }); await f.tick(); assert.equal(f.view.job.snapshot, null);
  f.view.finish({ kind: 'synced' }); assert.equal(f.intervals.size, 0);
});

test('a matching request shows real record progress but ignores an old operation detail', async () => {
  const f = fixture(); f.root(make({ processed_chats: 1, total_chats: 3, transferred_chats: 1 })); f.detail(detail({ operation_id: other }));
  await f.begin(); assert.match(f.status.text, /1\/3/); assert.doesNotMatch(f.status.tooltip, /12\/50/);
  f.detail(detail()); await f.tick(); assert.match(f.status.tooltip, /12\/50/);
  f.root(make({ operation_id: other })); await f.tick(); assert.doesNotMatch(f.status.tooltip, /12\/50/); f.view.dispose();
});

test('notifications share the actual operation lifetime and repeated clicks open one current progress', async () => {
  const f = fixture(); await f.begin(true); f.view.show(); f.view.show();
  assert.equal(f.notifications.length, 1); assert.equal(f.notifications[0].cancellable, false); assert.ok(f.logs.includes('SHOW'));
  let done = false; f.view.job.done.then(() => { done = true; }); await settle(); assert.equal(done, false);
  f.view.finish({ kind: 'current' }); await settle(); assert.equal(done, true); assert.equal(f.intervals.size, 0);
  assert.doesNotMatch(f.logs.join('\n'), /100%/);
});

test('automatic checks never create a foreground notification; local phases show confirmation and reload', async () => {
  const f = fixture(); await f.begin();
  for (const phase of ['confirming', 'pausing', 'reloading']) { f.view.local({ phase }); assert.match(f.status.text, new RegExp({ confirming: '等待确认', pausing: '暂停 WSL', reloading: '重新加载' }[phase])); }
  assert.equal(f.notifications.length, 0); f.view.finish();
});

test('phase/count changes append once, elapsed-only ticks do not flood details', async () => {
  const f = fixture(); f.root(make()); await f.begin(); const size = f.logs.length;
  for (let seconds = 1; seconds <= 3; seconds++) { f.setNow(baseTime + seconds * 1000); await f.tick(); }
  assert.equal(f.logs.length, size); assert.match(f.status.text, /3 秒/); f.view.dispose();
});

test('a stale in-flight snapshot reports unknown progress instead of success', async () => {
  const f = fixture(); f.root(make()); await f.begin(); f.setNow(baseTime + 31000); await f.tick();
  assert.match(f.status.text, /等待进度更新/); assert.doesNotMatch(f.status.text, /同步完成/); f.view.dispose();
});

test('a scheduled run is observed through its matching terminal and then polling stops', async () => {
  const f = fixture(); await f.begin(); f.root(make({ started_at: '2026-10-14T23:59:00Z', request_id: other }));
  f.view.finish({ reason: 'controller_running' }); await settle(); assert.equal(f.view.job.bound, run);
  f.root(make({ request_id: other, status: 'completed', phase: 'completed', processed_chats: 3, total_chats: 3, transferred_chats: 1, deferred_chats: 2 }));
  await f.tick(); assert.equal(f.view.job, null); assert.equal(f.intervals.size, 0);
  assert.match(f.status.text, /已同步 1 条，暂缓 2 条/); assert.doesNotMatch(f.status.text, /sync~spin/);
});

test('a stale scheduled terminal is never adopted and observation expires without leaving a timer', async () => {
  const f = fixture(); f.root(make({ status: 'completed' })); await f.begin(); f.view.finish({ reason: 'controller_running' }); await settle();
  assert.equal(f.view.job.bound, null); f.setNow(baseTime + 16000); await f.tick();
  assert.equal(f.view.job, null); assert.equal(f.intervals.size, 0); assert.match(f.logs.join('\n'), /未报告同步成功/);
});

test('legacy configuration resolves the mounted path once and refuses an unrelated launcher installation', async () => {
  let calls = 0;
  const f = fixture({ readPrivate: async () => ({ setupSettings: { windowsController: 'C:\\Other', windowsConfig: config.windowsConfig, progressFile: '/private-wrong' } }),
    runWindows: async () => { calls++; return { progress_file: config.progressFile }; } });
  await f.begin(false, { ...config, progressFile: '' }); f.view.finish(); await f.begin(false, { ...config, progressFile: '' });
  assert.equal(calls, 1); assert.equal(f.view.job.file, config.progressFile); f.view.dispose();
});

test('matching owner-checked installation avoids starting Windows Python to discover its progress path', async () => {
  const f = fixture({ readPrivate: async () => ({ owner: 'krfnsa-lab.codex-history-live-sync', wrappers_installed: true, setupSettings: { ...config } }) });
  await f.begin(false, { ...config, progressFile: '' }); assert.equal(f.fallbackCalls, 0); f.view.dispose();
});

test('a slow read completing after disposal cannot overwrite the final status', async () => {
  let resolve; const waiting = new Promise(done => { resolve = done; });
  const f = fixture({ read: async () => waiting }); f.view.begin(config); f.view.local({ phase: 'writing', requestId, newOperation: true }); await settle();
  f.view.dispose(); f.status.text = 'final actual result'; resolve(make()); await settle();
  assert.equal(f.status.text, 'final actual result'); assert.equal(f.intervals.size, 0);
});

test('invalid or failing optional progress sources do not throw or reveal source exception text', async () => {
  const f = fixture({ read: async () => { throw Error('secret/path/chat-body'); } }); await f.begin(true); await f.tick(); f.view.finish({ kind: 'synced' });
  assert.doesNotMatch(f.logs.join('\n') + f.status.text, /secret|chat-body/); assert.equal(f.intervals.size, 0);
});

test('native file reader rejects oversized and symbolic files without following them', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'history-progress-reader-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const file = path.join(directory, 'progress.json'); await fs.writeFile(file, JSON.stringify(make())); assert.equal((await readProgressJson(file)).run_id, run);
  await fs.writeFile(file, ' '.repeat(65537)); await assert.rejects(readProgressJson(file));
  if (process.platform !== 'win32') {
    const link = path.join(directory, 'linked.json'); await fs.symlink(file, link); await assert.rejects(readProgressJson(link));
  }
});

test('fresh matching endpoint heartbeats keep a long root operation live', async () => {
  const f = fixture(); f.root(make()); await f.begin(); f.setNow(baseTime + 45000);
  f.detail(detail({ updated_at: new Date(baseTime + 45000).toISOString(), elapsed_seconds: 45 })); await f.tick();
  assert.doesNotMatch(f.status.text, /等待进度更新/); assert.match(f.status.tooltip, /12\/50/);
  f.detail(detail({ operation_id: other, updated_at: new Date(baseTime + 45000).toISOString() })); await f.tick();
  assert.match(f.status.text, /等待进度更新/); f.view.dispose();
});

test('launcher fallback requires the expected owner and installed-wrapper marker', async () => {
  for (const changed of [{ owner: 'wrong' }, { wrappers_installed: false }, { owner: undefined }, { wrappers_installed: undefined }]) {
    let fallback = 0;
    const f = fixture({ readPrivate: async () => ({ owner: 'krfnsa-lab.codex-history-live-sync', wrappers_installed: true, setupSettings: { ...config, progressFile: '/wrong-file' }, ...changed }),
      runWindows: async () => { fallback++; return { progress_file: config.progressFile }; } });
    await f.begin(false, { ...config, progressFile: '' }); assert.equal(fallback, 1); assert.equal(f.view.job.file, config.progressFile); f.view.dispose();
  }
});

test('detail history is bounded and truncates the real output channel', () => {
  const f = fixture(); let replacements = 0; let actual = '';
  f.view.output = { appendLine: line => { actual += line + '\n'; }, replace: text => { replacements++; actual = text; }, dispose() {} };
  for (let index = 0; index < 1600; index++) f.view.append('safe stage ' + index);
  assert.ok(f.view.lines.length <= 500); assert.ok(actual.trim().split('\n').length <= 500); assert.ok(replacements > 0);
  assert.doesNotMatch(actual, /safe stage 0\n/); assert.match(actual, /safe stage 1599/); f.view.dispose();
});


test('an unusable optional progress path never triggers a read or affects the actual operation lifecycle', async () => {
  let reads = 0;
  const f = fixture({ read: async () => { reads++; throw Error('must not read an unsafe path'); } });
  await f.begin(true, { ...config, progressFile: 'C:\\Private\\wrong.json' });
  assert.equal(f.view.job.file, null); assert.equal(reads, 0);
  f.view.finish({ kind: 'synced' }); assert.equal(f.intervals.size, 0);
});


test('a finished or errored child never reports the still-running root operation as finished', async () => {
  for (const terminal of [{ status: 'completed', phase: 'completed' }, { status: 'error', phase: 'error' }]) {
    const f = fixture(); f.root(make({ phase: 'writing' })); f.detail(detail(terminal)); await f.begin();
    assert.match(f.status.text, /写入聊天记录/);
    assert.doesNotMatch(f.status.text + f.status.tooltip, /本轮处理结束|本轮未完成/);
    assert.match(f.status.tooltip, /记录 12\/50/);
    assert.match(render(make({ phase: 'writing' }), detail(terminal), 2), /写入聊天记录/);
    assert.notEqual(f.view.job, null, 'A child terminal cannot finish the operation notification or timer');
    f.view.dispose();
  }
});
