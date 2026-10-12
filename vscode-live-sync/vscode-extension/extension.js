'use strict';
const vscode = require('vscode');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { Coordinator, validateConfig, readPrivateJson, runWindows, describeResult } = require('./bridge');
let timer; let coordinator; let status; let lastError = '';
let extensionContext;
let setupInProgress = false;
const section = 'codexHistoryLiveSync';
const gatewayName = 'codex-history-sync-bridge';

function bootstrapGateway(context) {
  const official = vscode.extensions.getExtension('openai.chatgpt');
  if (!official || !['win32', 'linux'].includes(process.platform) || process.arch !== 'x64') return false;
  const platformDir = process.platform === 'win32' ? 'windows-x86_64' : 'linux-x86_64';
  const native = path.join(official.extensionPath, 'bin', platformDir, process.platform === 'win32' ? 'codex.exe' : 'codex');
  const launcherDir = path.join(context.extensionPath, 'bin', process.platform === 'win32' ? 'windows' : 'linux');
  const launcher = path.join(launcherDir, process.platform === 'win32' ? gatewayName + '.exe' : gatewayName);
  if (!fs.existsSync(native) || !fs.existsSync(launcher)) return false;
  process.env.CODEX_HISTORY_SYNC_REAL_CLI = native;
  process.env.CODEX_HISTORY_SYNC_STATE_DIR = settings().stateDir || '';
  process.env.PATH = launcherDir + path.delimiter + (process.env.PATH || '');
  return true;
}

async function configureGateway(context, quiet = false) {
  const config = vscode.workspace.getConfiguration('chatgpt');
  if (process.platform === 'win32' && config.get('runCodexInWindowsSubsystemForLinux') === true) {
    await vscode.window.showWarningMessage('本预览只支持通过 Remote WSL 打开的窗口。请先关闭在 Windows 本地窗口中让 Codex 单独运行于 WSL 的选项。'); return false;
  }
  const ready = bootstrapGateway(context);
  if (!ready) { await vscode.window.showWarningMessage('同步桥尚未完整安装；请先按安装说明准备两端启动器。'); return false; }
  const current = config.get('cliExecutable');
  if (current != null && String(current).trim() !== '' && current !== gatewayName) {
    await vscode.window.showWarningMessage('已配置自定义 Codex，请先自行恢复默认设置；同步桥不会覆盖它。'); return false;
  }
  const previous = context.globalState.get('gatewayOriginalSetting');
  if (current !== gatewayName && previous === undefined) {
    const inspected = config.inspect('cliExecutable');
    await context.globalState.update('gatewayOriginalSetting', { hadValue: inspected?.globalValue !== undefined, value: inspected?.globalValue ?? null });
  }
  await config.update('cliExecutable', gatewayName, vscode.ConfigurationTarget.Global);
  if (!quiet) await vscode.window.showInformationMessage('同步桥已配置。请先确认 Windows 和 WSL 两端都已安装，再重载此窗口。');
  return true;
}

async function setup(context) {
  if (!isWsl()) { await vscode.window.showWarningMessage('请在通过 Remote WSL 连接的 VS Code 窗口中完成设置。'); return; }
  if (coordinator?.running || setupInProgress) { await vscode.window.showWarningMessage('同步或设置正在进行，请稍后再完成设置。'); return; }
  if (context.globalState.get('needsReview')) { await vscode.window.showWarningMessage('同步需要检查。请先处理未完成的导入和暂停保护，再重新设置。'); return; }
  let reloadRequested = false;
  try {
    const file = path.join(os.homedir(), '.config', 'codex-history-sync-live', 'launcher.json');
    const installation = await readPrivateJson(file);
    if (installation.wrappers_installed !== true || !installation.setupSettings || typeof installation.setupSettings !== 'object') throw new Error('安装配置不完整，请先运行安装程序。');
    const keys = ['intervalSeconds', 'controllerTimeoutSeconds', 'stateDir', 'windowsPython', 'windowsController', 'windowsConfig', 'previewHelper'];
    const proposed = Object.fromEntries(keys.map(key => [key, installation.setupSettings[key]]));
    validateConfig(proposed);
    if (!canReload()) throw new Error('请先保存编辑器中的文件并停止调试，再完成设置。');
    const currentCli = vscode.workspace.getConfiguration('chatgpt').get('cliExecutable');
    if (currentCli != null && String(currentCli).trim() && currentCli !== gatewayName) throw new Error('已有自定义 Codex 设置，安装程序不会覆盖它。');
    const accepted = await vscode.window.showWarningMessage('启用后，空闲时会自动重载这个 VS Code 窗口。请先发送或保存 Codex 输入框里尚未发送的草稿；插件无法检查这些草稿。', { modal: true }, '启用并重载');
    if (accepted !== '启用并重载') return;
    if (!canReload()) throw new Error('编辑器出现未保存内容，设置已推迟。');
    const config = vscode.workspace.getConfiguration(section);
    setupInProgress = true;
    if (!context.globalState.get('setupOriginalSettings')) {
      const originals = Object.fromEntries(keys.map(key => {
        const value = config.inspect(key)?.globalValue;
        return [key, { hadValue: value !== undefined, value: value ?? null }];
      }));
      await context.globalState.update('setupOriginalSettings', originals);
    }
    await config.update('enabled', false, vscode.ConfigurationTarget.Global);
    for (const key of keys) if (proposed[key] !== undefined) await config.update(key, proposed[key], vscode.ConfigurationTarget.Global);
    await context.globalState.update('setupInstalledSettings', proposed);
    if (!await configureGateway(context, true)) return;
    await context.globalState.update('nextAttemptAt', 0);
    await config.update('enabled', true, vscode.ConfigurationTarget.Global);
    reloadRequested = true;
    await vscode.commands.executeCommand('workbench.action.reloadWindow');
  } catch (error) { await vscode.window.showWarningMessage(error.message); }
  finally { setupInProgress = false; if (!reloadRequested) configure(); }
}

async function disableRestore(context) {
  const wasRunning = coordinator?.running;
  await vscode.workspace.getConfiguration(section).update('enabled', false, vscode.ConfigurationTarget.Global);
  const currentSettings = settings();
  if (wasRunning || (isWsl() && currentSettings.stateDir && fs.existsSync(path.join(currentSettings.stateDir, 'pause.json')))) {
    await vscode.window.showWarningMessage('自动同步已停用。仍有同步或暂停保护，请先通过恢复工具检查；当前不会恢复原 Codex 启动设置。'); return;
  }
  if (context.globalState.get('needsReview') && isWsl()) {
    try {
      validateConfig(currentSettings);
      if ((await runWindows(currentSettings, 'recovery')).safe_to_reload !== true) throw new Error('仍有未完成的导入，请先完成恢复检查。');
    } catch (error) { await vscode.window.showWarningMessage(error.message); return; }
  }
  const config = vscode.workspace.getConfiguration('chatgpt');
  if (config.get('cliExecutable') === gatewayName) {
    const previous = context.globalState.get('gatewayOriginalSetting');
    if (!previous) { await vscode.window.showWarningMessage('缺少原设置备份；已停用自动同步，请在设置中清空 Codex 自定义可执行文件。'); return; }
    await config.update('cliExecutable', previous.hadValue ? previous.value : undefined, vscode.ConfigurationTarget.Global);
    await context.globalState.update('gatewayOriginalSetting', undefined);
  }
  await context.globalState.update('needsReview', false);
  const originals = context.globalState.get('setupOriginalSettings');
  const installed = context.globalState.get('setupInstalledSettings');
  if (originals && installed) {
    const settingsConfig = vscode.workspace.getConfiguration(section);
    for (const [key, old] of Object.entries(originals)) {
      if (settingsConfig.get(key) === installed[key]) await settingsConfig.update(key, old.hadValue ? old.value : undefined, vscode.ConfigurationTarget.Global);
    }
    await context.globalState.update('setupOriginalSettings', undefined);
    await context.globalState.update('setupInstalledSettings', undefined);
  }
  await context.globalState.update('nextAttemptAt', 0);
  await vscode.window.showInformationMessage('自动同步已停用，原 Codex 设置已恢复。重载窗口后生效。');
}

function settings() {
  const config = vscode.workspace.getConfiguration(section);
  return Object.fromEntries(['enabled', 'intervalSeconds', 'controllerTimeoutSeconds', 'stateDir', 'windowsPython', 'windowsController', 'windowsConfig', 'previewHelper'].map(key => [key, config.get(key)]));
}

function isWsl() { return process.platform === 'linux' && vscode.env.remoteName === 'wsl'; }
function reloadBlockReason() {
  if (vscode.debug.activeDebugSession) return 'debugging';
  if (vscode.workspace.textDocuments.some(document => document.isDirty)) return 'dirty_editor';
  return 'editor_not_idle';
}
function canReload() {
  return !vscode.debug.activeDebugSession && !vscode.workspace.textDocuments.some(document => document.isDirty);
}

async function check(manual = false) {
  if (!coordinator) return;
  try {
    const result = await coordinator.check();
    const presentation = describeResult(result);
    status.text = presentation.text;
    status.tooltip = presentation.detail; lastError = '';
    if (manual) {
      if (presentation.warning) await vscode.window.showWarningMessage(presentation.detail);
      else await vscode.window.showInformationMessage(presentation.detail);
    }
  } catch (error) {
    status.text = '$(warning) Codex：同步待处理';
    status.tooltip = error.message;
    if (manual && lastError !== error.message) await vscode.window.showWarningMessage(error.message);
    lastError = error.message;
  }
}

function configure() {
  if (timer) clearInterval(timer); timer = null; coordinator = null;
  const config = settings();
  if (setupInProgress) return;
  process.env.CODEX_HISTORY_SYNC_STATE_DIR = config.stateDir || '';
  if (extensionContext.globalState.get('needsReview')) { status.show(); status.text = '$(warning) Codex：同步需要检查'; return; }
  if (!config.enabled) { status.hide(); return; }
  status.show();
  if (!isWsl()) { status.text = '$(history) Codex：请在 WSL 中启用'; return; }
  try {
    validateConfig(config);
    coordinator = new Coordinator(config, {
      canReload,
      reloadBlockReason,
      nextAttemptAt: () => extensionContext.globalState.get('nextAttemptAt', 0),
      onDeferred: value => extensionContext.globalState.update('nextAttemptAt', value),
      reload: () => vscode.commands.executeCommand('workbench.action.reloadWindow'),
      onFailure: async () => {
        await extensionContext.globalState.update('needsReview', true);
        await vscode.workspace.getConfiguration(section).update('enabled', false, vscode.ConfigurationTarget.Global);
      }
    });
  } catch (error) { status.text = '$(warning) Codex：需要配置'; status.tooltip = error.message; return; }
  status.text = '$(history) Codex：自动同步已启用';
  timer = setInterval(() => void check(), Math.max(30, config.intervalSeconds || 60) * 1000);
  timer.unref?.();
  void check();
}

function activate(context) {
  extensionContext = context;
  bootstrapGateway(context);
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 10);
  status.command = 'codexHistoryLiveSync.checkNow';
  context.subscriptions.push(status,
    vscode.commands.registerCommand('codexHistoryLiveSync.setup', () => setup(context)),
    vscode.commands.registerCommand('codexHistoryLiveSync.configureGateway', () => configureGateway(context)),
    vscode.commands.registerCommand('codexHistoryLiveSync.disableRestore', () => disableRestore(context)),
    vscode.commands.registerCommand('codexHistoryLiveSync.enable', async () => {
      if (!isWsl()) { await vscode.window.showWarningMessage('请在连接 WSL 的 VS Code 窗口中启用。'); return; }
      try { validateConfig(settings()); }
      catch (error) { await vscode.window.showWarningMessage(error.message); return; }
      if (context.globalState.get('needsReview')) { await vscode.window.showWarningMessage('请先检查未完成的同步，再停用并恢复原设置。'); return; }
      if (vscode.workspace.getConfiguration('chatgpt').get('cliExecutable') !== gatewayName) { await vscode.window.showWarningMessage('请先配置同步桥，再启用自动同步。'); return; }
      await vscode.workspace.getConfiguration(section).update('enabled', true, vscode.ConfigurationTarget.Global);
    }),
    vscode.commands.registerCommand('codexHistoryLiveSync.disable', () => vscode.workspace.getConfiguration(section).update('enabled', false, vscode.ConfigurationTarget.Global)),
    vscode.commands.registerCommand('codexHistoryLiveSync.checkNow', () => check(true)),
    vscode.workspace.onDidChangeConfiguration(event => { if (event.affectsConfiguration(section)) configure(); }),
    { dispose: () => { if (timer) clearInterval(timer); } }
  );
  configure();
}
function deactivate() { if (timer) clearInterval(timer); }
module.exports = { activate, deactivate };
