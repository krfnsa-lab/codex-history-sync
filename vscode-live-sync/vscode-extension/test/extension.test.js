'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { validateConfig, describeResult } = require('../bridge');

function load({ platform = 'linux', overrides = {}, pause = false, accept = true, mergeFromWarning = false, review = false, configured = false, pick = true, resolution = 'windows', recoveryResults = [], checkResults = [], mergeResults = [] } = {}) {
  const commands = new Map(); const calls = []; const state = new Map();
  const values = new Map(Object.entries({ 'codexHistoryLiveSync.enabled': false, ...overrides }));
  const proposed = { stateDir: '/home/example-user/.local/share/chsl', windowsPython: '/mnt/c/python.exe', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json', previewHelper: 'C:\\Sync\\preview_sync.py', intervalSeconds: 60, controllerTimeoutSeconds: 600 };
  if (configured) for (const [key, value] of Object.entries(proposed)) if (!values.has(`codexHistoryLiveSync.${key}`)) values.set(`codexHistoryLiveSync.${key}`, value);
  if (review) {
    state.set('needsReview', true); state.set('reviewError', 'previous failure'); state.set('nextAttemptAt', 12345);
    for (const [key, value] of Object.entries(proposed)) if (!values.has(`codexHistoryLiveSync.${key}`)) values.set(`codexHistoryLiveSync.${key}`, value);
    values.set('chatgpt.cliExecutable', 'codex-history-sync-bridge');
  }
  const timers = []; let coordinatorDependencies; let configurationListener; let constructors = 0;
  const disposable = { dispose() {} };
  const vscode = {
    env: { remoteName: platform === 'linux' ? 'wsl' : undefined },
    ConfigurationTarget: { Global: 1 }, StatusBarAlignment: { Right: 2 }, ProgressLocation: { Notification: 15 },
    extensions: { getExtension: () => ({ extensionPath: '/official' }) },
    debug: {},
    workspace: { textDocuments: [], onDidChangeConfiguration: callback => { configurationListener = callback; return disposable; },
      getConfiguration: section => ({
        get: key => values.get(`${section}.${key}`),
        inspect: key => ({ globalValue: values.get(`${section}.${key}`) }),
        update: async (key, value, target) => { calls.push(['update', `${section}.${key}`, value, target]); values.set(`${section}.${key}`, value); }
      })
    },
    commands: { registerCommand: (name, callback) => { commands.set(name, callback); return disposable; }, executeCommand: async name => { calls.push(['command', name]); } },
    window: {
      createStatusBarItem: () => ({ show() {}, hide() {}, dispose() {} }),
      createOutputChannel: () => ({ appendLine: message => calls.push(['output', message]), show: () => calls.push(['output-show']), dispose() {} }),
      showWarningMessage: async (message, options, action) => { calls.push(['warning', message, options, action]); return options?.modal && accept ? action : options === '处理聊天冲突' && mergeFromWarning ? options : undefined; },
      showInformationMessage: async message => { calls.push(['info', message]); },
      showQuickPick: async (items, options) => { calls.push(['pick', items, options]); return options.title === '选择保留哪一端' ? items.find(item => item.keepSide === resolution) : pick ? items[0] : undefined; },
      withProgress: async (options, callback) => { calls.push(['progress', options]); return callback({ report: event => calls.push(['progress-report', event]) }); }
    }
  };
  const context = { extensionPath: '/preview', subscriptions: [], globalState: { get: (key, fallback) => state.has(key) ? state.get(key) : fallback, update: async (key, value) => { state.set(key, value); } } };
  const sandbox = { module: { exports: {} }, process: { platform, arch: 'x64', env: {} }, setInterval: callback => { timers.push(callback); return { unref() {} }; }, clearInterval() {}, require: name => {
    if (name === 'vscode') return vscode;
    if (name === 'node:fs') return { existsSync: value => value.endsWith('pause.json') ? pause : true };
    if (name === 'node:os') return { homedir: () => '/home/example-user' };
    if (name === 'node:path') return platform === 'linux' ? path.posix : path.win32;
    if (name === './progress') return require('../progress');
    if (name === './bridge') return { validateConfig, describeResult, readPrivateJson: async file => { calls.push(['read-installation', file]); return { wrappers_installed: true, setupSettings: proposed }; }, runWindows: async () => ({ safe_to_reload: true }),
      runMergeWindows: async (_, action) => { calls.push(['merge', action]); const result = mergeResults.shift() || { ok: true, status: 'candidates', chats: [] }; if (result instanceof Error) throw result; return result; }, Coordinator: class {
      constructor(_, dependencies) { coordinatorDependencies = dependencies; constructors += 1; }
      async check(options) { this.running = true; calls.push(['check', options]); try { return await (checkResults.shift() || { kind: 'current', reason: 'no_pending_updates' }); } finally { this.running = false; } }
      async recover() { calls.push(['recover']); const result = recoveryResults.shift() || { kind: 'waiting', reason: 'codex_busy' }; if (result instanceof Error) throw result; return result; }
    } };
    return require(name);
  } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../extension.js'), 'utf8'), sandbox);
  sandbox.module.exports.activate(context);
  return { commands, calls, state, values, proposed, vscode, timers, recoveryResults,
    notifyConfiguration: () => configurationListener({ affectsConfiguration: name => name === 'codexHistoryLiveSync' }),
    get constructors() { return constructors; }, get dependencies() { return coordinatorDependencies; } };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

function leaveNoticesOpen(f) {
  const pending = new Promise(() => {});
  for (const method of ['showInformationMessage', 'showWarningMessage']) {
    const original = f.vscode.window[method];
    f.vscode.window[method] = (...args) => {
      const result = original(...args);
      return args[1]?.modal ? result : pending;
    };
  }
}

async function finishesWithoutDismissal(operation) {
  let finished = false;
  const result = operation();
  result.then(() => { finished = true; });
  await settle();
  assert.equal(finished, true, 'Completed work must not wait for a result notification to close');
  await result;
}

test('open information and warning notices do not retain check guards or defer configuration', async () => {
  for (const result of [{ kind: 'current', reason: 'no_pending_updates' }, { kind: 'blocked', reason: 'incoming_update_blocked' }]) {
    const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }], checkResults: [result, result] });
    await settle(); leaveNoticesOpen(f);
    await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
    const constructors = f.constructors;
    f.notifyConfiguration(); await settle();
    assert.equal(f.constructors, constructors + 1);
    await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
    assert.ok(f.calls.filter(call => call[0] === 'check').length >= 2);
    assert.ok(!f.calls.some(call => call[0] === 'info' && call[1].includes('已有同步检查')));
  }
});

test('open recovery result notices release recovery and check guards', async () => {
  for (const outcome of [{ kind: 'recovered' }, { kind: 'waiting', reason: 'codex_busy' }]) {
    const f = load({ review: true }); await settle(); leaveNoticesOpen(f);
    f.recoveryResults.push(outcome);
    await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
    await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
    assert.ok(!f.calls.some(call => call[0] === 'info' && call[1].includes('已有同步检查')));
  }
});

test('an unclosed conflict action notice does not block checks and its later action uses a fresh list', async () => {
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }],
    checkResults: [{ kind: 'conflict', reason: 'chat_conflict' }, { kind: 'current', reason: 'no_pending_updates' }],
    mergeResults: [candidateListing, savedChoice('windows')] });
  await settle();
  let choose;
  const pending = new Promise(resolve => { choose = resolve; });
  const original = f.vscode.window.showWarningMessage;
  f.vscode.window.showWarningMessage = (...args) => {
    const result = original(...args);
    return args[1] === '处理聊天冲突' ? pending : result;
  };
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
  assert.equal(f.calls.filter(call => call[0] === 'check').length, 2);
  assert.equal(f.calls.filter(call => call[0] === 'merge').length, 0);
  choose('处理聊天冲突'); await settle();
  assert.equal(f.calls.filter(call => call[0] === 'merge').length, 2);
});

test('an open saved-choice notice does not keep conflict handling busy', async () => {
  const f = load({ configured: true, mergeResults: [candidateListing, savedChoice('windows')] });
  leaveNoticesOpen(f);
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.resolveConflicts')());
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.resolveConflicts')());
  assert.equal(f.calls.filter(call => call[0] === 'merge').length, 3);
  assert.ok(!f.calls.some(call => call[0] === 'info' && call[1].includes('正在进行，请等待')));
});

test('an open gateway setup error notice does not keep setup busy', async () => {
  const f = load();
  leaveNoticesOpen(f);
  f.vscode.extensions.getExtension = () => undefined;
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.setup')());
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.setup')());
  assert.equal(f.calls.filter(call => call[0] === 'read-installation').length, 2);
  assert.ok(!f.calls.some(call => call[0] === 'warning' && call[1].includes('正在进行，请稍后')));
});

test('notification promise rejections do not retain operation guards', async () => {
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }] }); await settle();
  f.vscode.window.showInformationMessage = () => Promise.reject(new Error('Notification service unavailable'));
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
  await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
  assert.equal(f.calls.filter(call => call[0] === 'check').length, 2);
});

test('a reviewed installation automatically retries recovery and resumes without importing or reloading', async () => {
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered', reason: 'recovery_verified' }] });
  await settle();
  assert.equal(f.state.get('needsReview'), false);
  assert.equal(f.state.get('reviewError'), undefined);
  assert.equal(f.state.get('nextAttemptAt'), 0);
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), true);
  assert.ok(!f.calls.some(call => call[0] === 'check' || call[0] === 'command'));
  f.timers.at(-1)(); await settle();
  assert.ok(f.calls.some(call => call[0] === 'check'));
});

test('failed recovery keeps the review flag and manual check explains the waiting condition', async () => {
  const f = load({ review: true }); await settle();
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  assert.equal(f.state.get('needsReview'), true);
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), false);
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[1].includes('空闲 60 秒')));
  assert.ok(!f.calls.some(call => call[0] === 'check' || call[0] === 'command'));
});

test('enable while reviewed runs recovery instead of silently refusing', async () => {
  const f = load({ review: true }); await settle();
  f.recoveryResults.push({ kind: 'recovered', reason: 'recovery_verified' });
  await f.commands.get('codexHistoryLiveSync.enable')();
  assert.equal(f.state.get('needsReview'), false);
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), true);
  assert.ok(f.calls.some(call => call[0] === 'info' && call[1].includes('未传输记录')));
});

test('periodic review recovery retries safely after an exception', async () => {
  const f = load({ review: true, recoveryResults: [new Error('helper unavailable')] }); await settle();
  assert.equal(f.state.get('needsReview'), true);
  f.recoveryResults.push({ kind: 'recovered', reason: 'recovery_verified' });
  f.timers.at(-1)(); await settle();
  assert.equal(f.state.get('needsReview'), false);
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), true);
});

test('explicitly disabled installations without review stay disabled and never recover', async () => {
  const f = load(); await settle();
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), false);
  assert.ok(!f.calls.some(call => call[0] === 'recover' || call[0] === 'check'));
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[1].includes('尚未启用')));
});

test('explicit disable during review suppresses automatic recovery until manual enable', async () => {
  const f = load({ review: true }); await settle();
  await f.commands.get('codexHistoryLiveSync.disable')();
  const before = f.calls.filter(call => call[0] === 'recover').length;
  f.timers.at(-1)(); await settle();
  assert.equal(f.calls.filter(call => call[0] === 'recover').length, before);
  assert.equal(f.state.get('needsReview'), true);
  assert.equal(f.state.get('reviewRecoverySuppressed'), true);
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), false);
  f.recoveryResults.push({ kind: 'recovered', reason: 'recovery_verified' });
  await f.commands.get('codexHistoryLiveSync.enable')();
  assert.equal(f.state.get('needsReview'), false);
  assert.equal(f.state.get('reviewRecoverySuppressed'), false);
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), true);
});

test('disable wins when a startup recovery proof finishes later', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const f = load({ review: true, recoveryResults: [pending] });
  await f.commands.get('codexHistoryLiveSync.disable')();
  resolve({ kind: 'recovered', reason: 'recovery_verified' }); await settle();
  assert.equal(f.state.get('needsReview'), true);
  assert.equal(f.state.get('reviewRecoverySuppressed'), true);
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), false);
  assert.ok(!f.calls.some(call => call[0] === 'check' || call[0] === 'command'));
});

test('failure stores a safe review explanation without persisting private paths', async () => {
  const f = load({ review: true }); await settle();
  await f.dependencies.onFailure(new Error('无法确认同步恢复状态，暂停保护已保留。'));
  assert.match(f.state.get('reviewError'), /暂停保护/);
  await f.dependencies.onFailure(new Error('ENOENT private/path/file'));
  assert.equal(f.state.get('reviewError'), '同步未完成，需要恢复检查。');
});

test('explicit setup imports installed values through VS Code configuration API and reloads once', async () => {
  const f = load({ overrides: { 'codexHistoryLiveSync.intervalSeconds': 90 } });
  await f.commands.get('codexHistoryLiveSync.setup')();
  assert.ok(f.calls.some(call => call[0] === 'read-installation' && call[1] === '/home/example-user/.config/codex-history-sync-live/launcher.json'));
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[1].includes('草稿')));
  assert.equal(f.values.get('chatgpt.cliExecutable'), 'codex-history-sync-bridge');
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), true);
  assert.equal(f.state.get('setupOriginalSettings').intervalSeconds.value, 90);
  assert.equal(f.calls.filter(call => call[0] === 'command').length, 1);
  assert.ok(f.calls.filter(call => call[0] === 'update').every(call => call[3] === 1));
});

test('cancelled draft warning makes no settings changes', async () => {
  const f = load({ accept: false });
  await f.commands.get('codexHistoryLiveSync.setup')();
  assert.ok(!f.calls.some(call => call[0] === 'update'));
  assert.ok(!f.calls.some(call => call[0] === 'command'));
});

test('setup preserves an existing custom CLI', async () => {
  const f = load({ overrides: { 'chatgpt.cliExecutable': '/home/example-user/custom-cli' } });
  await f.commands.get('codexHistoryLiveSync.setup')();
  assert.equal(f.values.get('chatgpt.cliExecutable'), '/home/example-user/custom-cli');
  assert.ok(!f.calls.some(call => call[0] === 'update'));
});

test('local Windows with run-in-WSL mode refuses gateway configuration', async () => {
  const f = load({ platform: 'win32', overrides: { 'chatgpt.runCodexInWindowsSubsystemForLinux': true } });
  await f.commands.get('codexHistoryLiveSync.configureGateway')();
  assert.ok(!f.calls.some(call => call[0] === 'update'));
});

test('restore puts back original settings while keeping later user choices', async () => {
  const f = load({ overrides: { 'codexHistoryLiveSync.intervalSeconds': 90 } });
  await f.commands.get('codexHistoryLiveSync.setup')();
  f.values.set('codexHistoryLiveSync.controllerTimeoutSeconds', 800);
  await f.commands.get('codexHistoryLiveSync.disableRestore')();
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), false);
  assert.equal(f.values.get('chatgpt.cliExecutable'), undefined);
  assert.equal(f.values.get('codexHistoryLiveSync.intervalSeconds'), 90);
  assert.equal(f.values.get('codexHistoryLiveSync.controllerTimeoutSeconds'), 800);
});

test('a remaining pause gate prevents restoration of raw CLI settings', async () => {
  const f = load({ pause: true, overrides: { 'chatgpt.cliExecutable': 'codex-history-sync-bridge', 'codexHistoryLiveSync.stateDir': '/home/example-user/.local/share/chsl' } });
  f.state.set('needsReview', true);
  await f.commands.get('codexHistoryLiveSync.disableRestore')();
  assert.equal(f.values.get('chatgpt.cliExecutable'), 'codex-history-sync-bridge');
  assert.equal(f.state.get('needsReview'), true);
});

test('settings changes and repeated clicks do not replace or overlap an active coordinator', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }], checkResults: [pending] });
  await settle();
  const operation = f.commands.get('codexHistoryLiveSync.checkNow')();
  const before = f.constructors;
  f.notifyConfiguration();
  f.timers.at(-1)();
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  assert.equal(f.constructors, before);
  assert.equal(f.calls.filter(call => call[0] === 'check').length, 1);
  assert.equal(f.dependencies.shouldContinue(), false);
  resolve({ kind: 'waiting', reason: 'sync_disabled' }); await operation;
  assert.equal(f.constructors, before + 1);
  assert.equal(f.dependencies.shouldContinue(), true);
});

test('disable during an ordinary check cancels admission and never re-enables after completion', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }], checkResults: [pending] });
  await settle();
  const operation = f.commands.get('codexHistoryLiveSync.checkNow')();
  await f.commands.get('codexHistoryLiveSync.disable')();
  f.notifyConfiguration();
  assert.equal(f.dependencies.shouldContinue(), false);
  resolve({ kind: 'waiting', reason: 'sync_disabled' }); await operation;
  assert.equal(f.values.get('codexHistoryLiveSync.enabled'), false);
  assert.equal(f.state.get('reviewRecoverySuppressed'), true);
});

test('default confirmation does not prompt during periodic checks and manual confirmation protects drafts', async () => {
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }] }); await settle();
  const before = f.calls.length;
  assert.equal(await f.dependencies.confirmReload(false), false);
  assert.equal(f.calls.length, before);
  assert.equal(await f.dependencies.confirmReload(true), true);
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[1].includes('草稿')));
  assert.equal(f.state.get('automaticReloadAccepted'), undefined);
});

test('automatic reload requires explicit acceptance, then avoids repeated prompts', async () => {
  const f = load({ review: true, overrides: { 'codexHistoryLiveSync.reloadMode': 'automatic' }, recoveryResults: [{ kind: 'recovered' }] }); await settle();
  assert.equal(await f.dependencies.confirmReload(false), false);
  assert.equal(await f.dependencies.confirmReload(true), true);
  assert.equal(f.state.get('automaticReloadAccepted'), true);
  const before = f.calls.length;
  assert.equal(await f.dependencies.confirmReload(false), true);
  assert.equal(f.calls.length, before);
});

test('cancelling a sync confirmation never authorizes automatic reload', async () => {
  const f = load({ review: true, accept: false, overrides: { 'codexHistoryLiveSync.reloadMode': 'automatic' }, recoveryResults: [{ kind: 'recovered' }] }); await settle();
  assert.equal(await f.dependencies.confirmReload(true), false);
  assert.equal(f.state.get('automaticReloadAccepted'), undefined);
});

const chatId = '11111111-1111-7111-8111-111111111111';
const jobId = '22222222-2222-4222-8222-222222222222';
const choiceToken = 'a'.repeat(64);
const candidateListing = { ok: true, status: 'candidates', chats: [{ id: chatId, label: 'Synthetic learning', ready: true, windows_turns: 2, wsl_turns: 3, choice_token: choiceToken }] };
const savedChoice = side => ({ ok: true, status: 'choice_saved', reason: 'waiting_for_safe_sync', choice_id: jobId, id: chatId, keep_side: side });

test('manual conflict handling offers only Windows or WSL and saves the exact choice without creating a copy', async () => {
  for (const side of ['windows', 'wsl']) {
    const f = load({ configured: true, resolution: side, mergeResults: [candidateListing, savedChoice(side)] });
    await f.commands.get('codexHistoryLiveSync.resolveConflicts')();
    const actions = f.calls.filter(call => call[0] === 'merge').map(call => call[1]);
    assert.deepEqual(JSON.parse(JSON.stringify(actions)), [{ type: 'list' }, { type: 'id', value: chatId, keepSide: side, expectedToken: choiceToken }]);
    const methods = f.calls.filter(call => call[0] === 'pick')[1][1];
    assert.deepEqual(Array.from(methods, item => item.keepSide), ['windows', 'wsl']);
    assert.ok(f.calls.some(call => call[0] === 'warning' && call[1].includes('独有的消息') && call[1].includes('完整备份')));
    assert.equal(f.calls.find(call => call[0] === 'progress')[1].cancellable, false);
    assert.ok(f.calls.some(call => call[0] === 'info' && call[1].includes('尚未同步') && call[1].includes('不会创建聊天副本')));
    assert.ok(!f.calls.some(call => call[0] === 'command' || call[0] === 'check'));
  }
});

test('cancelled chat, side or risk confirmation never saves a version choice', async () => {
  for (const options of [{ pick: false }, { resolution: null }, { accept: false }]) {
    const f = load({ configured: true, ...options, mergeResults: [candidateListing] });
    await f.commands.get('codexHistoryLiveSync.resolveConflicts')();
    assert.equal(f.calls.filter(call => call[0] === 'merge').length, 1);
    assert.ok(!f.calls.some(call => call[0] === 'merge' && call[1].type === 'id'), 'Read-only progress may show, but cancellation never saves a choice');
  }
});

test('busy, incomplete or unbound candidates are never offered for version selection', async () => {
  for (const changes of [{ ready: false }, { choice_token: undefined }, { choice_token: 'invalid' }]) {
    const f = load({ configured: true, mergeResults: [{ ...candidateListing, chats: [{ ...candidateListing.chats[0], ...changes }] }] });
    await f.commands.get('codexHistoryLiveSync.resolveConflicts')();
    assert.equal(f.calls.filter(call => call[0] === 'merge').length, 1);
    assert.ok(!f.calls.some(call => call[0] === 'pick' || (call[0] === 'merge' && call[1].type === 'id')));
    assert.ok(f.calls.some(call => call[0] === 'info' && call[1].includes('60 秒')));
  }
});

test('historical pending import blocks new choices and never offers automatic merge recovery', async () => {
  const f = load({ configured: true, mergeResults: [{ ok: true, status: 'waiting', reason: 'pending_import', resume_job_id: jobId }] });
  await f.commands.get('codexHistoryLiveSync.resolveConflicts')();
  assert.equal(f.calls.filter(call => call[0] === 'merge').length, 1);
  assert.ok(!f.calls.some(call => call[0] === 'pick' || (call[0] === 'merge' && call[1].type === 'id')));
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[1].includes('恢复记录')));
});

test('changed, stale, malformed and errored choices never report selection saved or expose private errors', async () => {
  for (const result of [{ ok: true, status: 'waiting', reason: 'source_changed' },
    { ok: true, status: 'waiting', reason: 'manual_choice_stale' },
    { ...savedChoice('windows'), id: jobId }, { ...savedChoice('windows'), keep_side: 'wsl' },
    { ok: false, status: 'error', reason: 'merge_requires_review' }, new Error('private/path secret')]) {
    const f = load({ configured: true, mergeResults: [candidateListing, result] });
    await f.commands.get('codexHistoryLiveSync.resolveConflicts')();
    assert.ok(f.calls.filter(call => call[0] === 'warning').length >= 2);
    assert.ok(!f.calls.some(call => call[0] === 'info' && call[1].includes('已保存以')));
    assert.ok(!f.calls.some(call => String(call[1]).includes('private/path')));
    if (result.reason === 'source_changed' || result.reason === 'manual_choice_stale') assert.ok(f.calls.some(call => call[0] === 'warning' && /重新检查.*确认/.test(call[1])));
  }
});

test('active conflict handling excludes sync checks, repeated choices and configuration recreation', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }], mergeResults: [candidateListing, pending] }); await settle();
  const operation = f.commands.get('codexHistoryLiveSync.resolveConflicts')(); await settle();
  const before = f.constructors;
  f.notifyConfiguration();
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  await f.commands.get('codexHistoryLiveSync.resolveConflicts')();
  assert.equal(f.constructors, before);
  assert.ok(!f.calls.some(call => call[0] === 'check'));
  assert.equal(f.calls.filter(call => call[0] === 'merge').length, 2);
  resolve(savedChoice('windows')); await operation;
  assert.equal(f.constructors, before + 1);
});

test('pause protection and local Windows reject conflict handling before launching helpers', async () => {
  for (const options of [{ pause: true }, { platform: 'win32' }]) {
    const f = load({ configured: true, ...options });
    await f.commands.get('codexHistoryLiveSync.resolveConflicts')();
    assert.ok(!f.calls.some(call => call[0] === 'merge'));
    assert.ok(f.calls.some(call => call[0] === 'warning'));
  }
});

test('manual status check offers conflict handling and enters selection UI after the check completes', async () => {
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }], mergeFromWarning: true,
    checkResults: [{ kind: 'conflict', reason: 'chat_conflict', preview: { conflict_count: 1 } }],
    mergeResults: [candidateListing, savedChoice('windows')]
  }); await settle();
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  await settle(); // The action toast opens conflict handling after check cleanup.
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[2] === '处理聊天冲突'));
  assert.equal(f.calls.filter(call => call[0] === 'merge').length, 2);
  assert.equal(f.calls.filter(call => call[0] === 'pick').length, 2);
  assert.ok(f.calls.some(call => call[0] === 'info' && call[1].includes('尚未同步')));
});

test('manual checks offer remaining preview conflicts even when another update is waiting', async () => {
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }],
    checkResults: [{ kind: 'waiting', reason: 'codex_busy', preview: { conflict_count: 2 } }]
  }); await settle();
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[2] === '处理聊天冲突'));
  assert.ok(!f.calls.some(call => call[0] === 'merge'));
});

test('cancelled conflict action, ordinary checks and automatic conflict checks never save a choice', async () => {
  const conflict = { kind: 'conflict', reason: 'chat_conflict', preview: { conflict_count: 1 } };
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }], checkResults: [conflict, { kind: 'current', reason: 'no_pending_updates' }, conflict] }); await settle();
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  const before = f.calls.filter(call => call[0] === 'warning' && call[2] === '处理聊天冲突').length;
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  f.timers.at(-1)(); await settle();
  assert.equal(f.calls.filter(call => call[0] === 'warning' && call[2] === '处理聊天冲突').length, before);
  assert.ok(!f.calls.some(call => call[0] === 'merge' || call[0] === 'pick' || (call[0] === 'merge' && call[1].type === 'id')));
});

test('legacy merge command id remains an alias for explicit side selection, without creating a copy', async () => {
  const f = load({ configured: true, resolution: 'wsl', mergeResults: [candidateListing, savedChoice('wsl')] });
  await f.commands.get('codexHistoryLiveSync.mergeChats')();
  assert.equal(f.calls.filter(call => call[0] === 'merge')[1][1].keepSide, 'wsl');
  assert.ok(f.calls.some(call => call[0] === 'info' && call[1].includes('不会创建聊天副本')));
});

test('a stale saved choice offers a fresh explicit version selection from the status bar', async () => {
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }],
    checkResults: [{ kind: 'blocked', reason: 'manual_choice_stale' }]
  }); await settle();
  await f.commands.get('codexHistoryLiveSync.checkNow')();
  assert.ok(f.calls.some(call => call[0] === 'warning' && call[2] === '处理聊天冲突' && call[1].includes('版本已经变化')));
  assert.ok(!f.calls.some(call => call[0] === 'merge'));
});

test('a stalled or failed progress notification cannot retain the actual check guard', async () => {
  for (const mode of ['stalled', 'throw', 'reject']) {
    const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }] }); await settle();
    f.vscode.window.withProgress = () => {
      if (mode === 'throw') throw Error('UI failed');
      return mode === 'reject' ? Promise.reject(Error('UI failed')) : new Promise(() => {});
    };
    await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
    await finishesWithoutDismissal(() => f.commands.get('codexHistoryLiveSync.checkNow')());
    assert.equal(f.calls.filter(call => call[0] === 'check').length, 2);
  }
});

test('repeated manual check opens the existing operation details instead of launching a check or busy notice', async () => {
  let resolve; const pending = new Promise(done => { resolve = done; });
  const f = load({ review: true, recoveryResults: [{ kind: 'recovered' }], checkResults: [pending] }); await settle();
  const running = f.commands.get('codexHistoryLiveSync.checkNow')();
  const notifications = f.calls.filter(call => call[0] === 'progress').length;
  await f.commands.get('codexHistoryLiveSync.checkNow')(); await f.commands.get('codexHistoryLiveSync.showProgress')();
  assert.equal(f.calls.filter(call => call[0] === 'check').length, 1);
  assert.equal(f.calls.filter(call => call[0] === 'progress').length, notifications);
  assert.ok(f.calls.some(call => call[0] === 'output-show'));
  assert.ok(!f.calls.some(call => call[0] === 'info' && call[1].includes('已有同步检查')));
  resolve({ kind: 'current', reason: 'no_pending_updates' }); await running;
});
