'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { validateConfig } = require('../bridge');

function load({ platform = 'linux', overrides = {}, pause = false, accept = true } = {}) {
  const commands = new Map(); const calls = []; const state = new Map();
  const values = new Map(Object.entries({ 'codexHistoryLiveSync.enabled': false, ...overrides }));
  const proposed = { stateDir: '/home/example-user/.local/share/chsl', windowsPython: '/mnt/c/python.exe', windowsController: 'C:\\Sync\\history_sync.py', windowsConfig: 'C:\\Sync\\config.json', previewHelper: 'C:\\Sync\\preview_sync.py', intervalSeconds: 60, controllerTimeoutSeconds: 600 };
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
  const sandbox = { module: { exports: {} }, process: { platform, arch: 'x64', env: {} }, setInterval: () => ({ unref() {} }), clearInterval() {}, require: name => {
    if (name === 'vscode') return vscode;
    if (name === 'node:fs') return { existsSync: value => value.endsWith('pause.json') ? pause : true };
    if (name === 'node:os') return { homedir: () => '/home/example-user' };
    if (name === 'node:path') return platform === 'linux' ? path.posix : path.win32;
    if (name === './bridge') return { validateConfig, readPrivateJson: async file => { calls.push(['read-installation', file]); return { wrappers_installed: true, setupSettings: proposed }; }, runWindows: async () => ({ safe_to_reload: true }), Coordinator: class { async check() { return { kind: 'current' }; } } };
    return require(name);
  } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../extension.js'), 'utf8'), sandbox);
  sandbox.module.exports.activate(context);
  return { commands, calls, state, values, proposed, vscode };
}

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
