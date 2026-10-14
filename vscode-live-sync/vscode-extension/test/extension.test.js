'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { validateConfig, describeResult } = require('../bridge');

function load({ platform = 'linux', overrides = {}, pause = false, accept = true, review = false, recoveryResults = [] } = {}) {
  const commands = new Map(); const calls = []; const state = new Map();
  const values = new Map(Object.entries({ 'codexHistoryLiveSync.enabled': false, ...overrides }));
  const proposed = { stateDir: '/home/example-user/.local/share/chsl', windowsPython: '/mnt/c/python.exe', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json', previewHelper: 'C:\\Sync\\preview_sync.py', intervalSeconds: 60, controllerTimeoutSeconds: 600 };
  if (review) {
    state.set('needsReview', true); state.set('reviewError', 'previous failure'); state.set('nextAttemptAt', 12345);
    for (const [key, value] of Object.entries(proposed)) if (!values.has(`codexHistoryLiveSync.${key}`)) values.set(`codexHistoryLiveSync.${key}`, value);
    values.set('chatgpt.cliExecutable', 'codex-history-sync-bridge');
  }
  const timers = []; let coordinatorDependencies;
  const disposable = { dispose() {} };
  const vscode = {
    env: { remoteName: platform === 'linux' ? 'wsl' : undefined },
    ConfigurationTarget: { Global: 1 }, StatusBarAlignment: { Right: 2 },
    extensions: { getExtension: () => ({ extensionPath: '/official' }) },
    debug: {},
    workspace: { textDocuments: [], onDidChangeConfiguration: () => disposable,
      getConfiguration: section => ({
        get: key => values.get(`${section}.${key}`),
        inspect: key => ({ globalValue: values.get(`${section}.${key}`) }),
        update: async (key, value, target) => { calls.push(['update', `${section}.${key}`, value, target]); values.set(`${section}.${key}`, value); }
      })
    },
    commands: { registerCommand: (name, callback) => { commands.set(name, callback); return disposable; }, executeCommand: async name => { calls.push(['command', name]); } },
    window: {
      createStatusBarItem: () => ({ show() {}, hide() {}, dispose() {} }),
      showWarningMessage: async (message, options, action) => { calls.push(['warning', message]); return options?.modal && accept ? action : undefined; },
      showInformationMessage: async message => { calls.push(['info', message]); }
    }
  };
  const context = { extensionPath: '/preview', subscriptions: [], globalState: { get: (key, fallback) => state.has(key) ? state.get(key) : fallback, update: async (key, value) => { state.set(key, value); } } };
  const sandbox = { module: { exports: {} }, process: { platform, arch: 'x64', env: {} }, setInterval: callback => { timers.push(callback); return { unref() {} }; }, clearInterval() {}, require: name => {
    if (name === 'vscode') return vscode;
    if (name === 'node:fs') return { existsSync: value => value.endsWith('pause.json') ? pause : true };
    if (name === 'node:os') return { homedir: () => '/home/example-user' };
    if (name === 'node:path') return platform === 'linux' ? path.posix : path.win32;
    if (name === './bridge') return { validateConfig, describeResult, readPrivateJson: async file => { calls.push(['read-installation', file]); return { wrappers_installed: true, setupSettings: proposed }; }, runWindows: async () => ({ safe_to_reload: true }), Coordinator: class {
      constructor(_, dependencies) { coordinatorDependencies = dependencies; }
      async check() { calls.push(['check']); return { kind: 'current', reason: 'no_pending_updates' }; }
      async recover() { calls.push(['recover']); const result = recoveryResults.shift() || { kind: 'waiting', reason: 'codex_busy' }; if (result instanceof Error) throw result; return result; }
    } };
    return require(name);
  } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../extension.js'), 'utf8'), sandbox);
  sandbox.module.exports.activate(context);
  return { commands, calls, state, values, proposed, vscode, timers, recoveryResults, get dependencies() { return coordinatorDependencies; } };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

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
