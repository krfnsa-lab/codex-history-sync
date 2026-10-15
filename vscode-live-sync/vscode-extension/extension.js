'use strict';
const vscode = require('vscode');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { Coordinator, validateConfig, readPrivateJson, runWindows, runMergeWindows, describeResult } = require('./bridge');
let timer; let coordinator; let status; let lastError = '';
let extensionContext;
let setupInProgress = false;
let recoveryInProgress = false;
let disableGeneration = 0;
let checkInProgress = false;
let mergeInProgress = false;
let configurationGeneration = 0;
let configurePending = false;
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
  if (!quiet && (mergeInProgress || checkInProgress || recoveryInProgress || setupInProgress)) {
    await vscode.window.showWarningMessage('同步、冲突处理或设置正在进行，请等待本轮结束。'); return false;
  }
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
  if (mergeInProgress || checkInProgress || coordinator?.running || setupInProgress) { await vscode.window.showWarningMessage('同步、冲突处理或设置正在进行，请稍后再完成设置。'); return; }
  if (context.globalState.get('needsReview')) { await vscode.window.showWarningMessage('同步需要检查。请先处理未完成的导入和暂停保护，再重新设置。'); return; }
  let reloadRequested = false;
  try {
    const file = path.join(os.homedir(), '.config', 'codex-history-sync-live', 'launcher.json');
    const installation = await readPrivateJson(file);
    if (installation.wrappers_installed !== true || !installation.setupSettings || typeof installation.setupSettings !== 'object') throw new Error('安装配置不完整，请先运行安装程序。');
    const keys = ['intervalSeconds', 'controllerTimeoutSeconds', 'stateDir', 'windowsPython', 'windowsController', 'windowsConfig', 'previewHelper', 'mergeHelper'];
    const proposed = Object.fromEntries(keys.map(key => [key, installation.setupSettings[key]]));
    validateConfig(proposed);
    if (!canReload()) throw new Error('请先保存编辑器中的文件并停止调试，再完成设置。');
    const currentCli = vscode.workspace.getConfiguration('chatgpt').get('cliExecutable');
    if (currentCli != null && String(currentCli).trim() && currentCli !== gatewayName) throw new Error('已有自定义 Codex 设置，安装程序不会覆盖它。');
    const automatic = settings().reloadMode === 'automatic';
    const accepted = await vscode.window.showWarningMessage(automatic
      ? '启用后，空闲时会自动重载这个 VS Code 窗口。请先发送或保存 Codex 输入框里尚未发送的草稿；插件无法检查这些草稿。'
      : '启用后会自动检查新记录，同步前由你确认并重载窗口。现在请先发送或保存 Codex 输入框里尚未发送的草稿，再重载完成设置。', { modal: true }, '启用并重载');
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
    await context.globalState.update('reviewRecoverySuppressed', false);
    await context.globalState.update('automaticReloadAccepted', automatic);
    await config.update('enabled', true, vscode.ConfigurationTarget.Global);
    reloadRequested = true;
    await vscode.commands.executeCommand('workbench.action.reloadWindow');
  } catch (error) { await vscode.window.showWarningMessage(error.message); }
  finally { setupInProgress = false; if (!reloadRequested) configure(); }
}

async function disableRestore(context) {
  const wasRunning = mergeInProgress || checkInProgress || coordinator?.running;
  await disable();
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
  return Object.fromEntries(['enabled', 'reloadMode', 'intervalSeconds', 'controllerTimeoutSeconds', 'stateDir', 'windowsPython', 'windowsController', 'windowsConfig', 'previewHelper', 'mergeHelper'].map(key => [key, config.get(key)]));
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

function failureMessage(error) {
  const message = String(error?.message || '');
  if (/未保存|Unsaved work/.test(message)) return '编辑器有未保存内容或正在调试，已保留暂停保护。';
  if (/暂停保护|恢复状态|pause lease/.test(message)) return '无法确认同步恢复状态，暂停保护已保留。';
  if (/无法读取 Windows 同步脚本/.test(message)) return '无法读取 Windows 同步脚本，请重新安装同步桥以修复路径。';
  if (/找不到 Windows Python/.test(message)) return '找不到 Windows Python，请检查同步桥的解释器配置。';
  if (/同步检查超时/.test(message)) return '同步检查超时，已保留原记录；请稍后重试。';
  return '同步未完成，需要恢复检查。';
}

async function disable() {
  disableGeneration += 1;
  await extensionContext.globalState.update('reviewRecoverySuppressed', true);
  await vscode.workspace.getConfiguration(section).update('enabled', false, vscode.ConfigurationTarget.Global);
}

function mergeWaitingMessage(result) {
  const reasons = {
    wsl_stopped: 'WSL 未运行，请先连接 WSL 后再处理冲突。',
    controller_running: '同步正在进行，请等待本轮结束再选择版本。',
    pending_import: '仍有未完成的导入，请先检查恢复记录；当前没有保存新的版本选择。',
    chat_busy_or_not_conflicting: '聊天仍在写入、尚未静默 60 秒，或冲突已变化。请等两端这条聊天空闲后重新检查。',
    source_changed: '确认期间聊天版本发生变化，没有保存这次选择。请重新检查并确认要保留的版本。',
    manual_choice_stale: '之前选择的版本已经变化，未按旧选择覆盖聊天。请重新检查并确认。',
    workspace_incompatible: '无法安全转换这条聊天的工作目录。请检查恢复记录。'
  };
  return reasons[result?.reason] || '冲突处理未完成。请查看恢复记录后再重试。';
}

async function mergeChats() {
  if (!isWsl()) { await vscode.window.showWarningMessage('请在连接 WSL 的 VS Code 窗口中选择聊天版本。'); return; }
  if (mergeInProgress || checkInProgress || recoveryInProgress || coordinator?.running || setupInProgress) {
    await vscode.window.showInformationMessage('同步、恢复或冲突处理正在进行，请等待本轮结束。'); return;
  }
  const config = settings();
  try { validateConfig(config); }
  catch { await vscode.window.showWarningMessage('同步桥配置不完整，请先完成设置再处理聊天冲突。'); return; }
  if (fs.existsSync(path.join(config.stateDir, 'pause.json'))) {
    await vscode.window.showWarningMessage('已有暂停保护，请先完成同步恢复检查，再选择聊天版本。'); return;
  }
  mergeInProgress = true;
  const generation = configurationGeneration;
  const disableAtStart = disableGeneration;
  const current = () => generation === configurationGeneration && disableAtStart === disableGeneration;
  try {
    const listing = await runMergeWindows(config, { type: 'list' });
    if (!current()) return;
    if (listing.ok !== true || listing.status !== 'candidates' || !Array.isArray(listing.chats)) {
      await vscode.window.showWarningMessage(mergeWaitingMessage(listing)); return;
    }
    const choices = listing.chats.filter(chat => chat.ready === true && typeof chat.id === 'string' &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(chat.id) &&
      typeof chat.choice_token === 'string' && /^[0-9a-f]{64}$/i.test(chat.choice_token) &&
      Number.isInteger(chat.windows_turns) && chat.windows_turns >= 0 && Number.isInteger(chat.wsl_turns) && chat.wsl_turns >= 0)
      .map(chat => ({ label: String(chat.label || '未命名聊天').replace(/[\r\n\x00-\x1f]/g, ' ').slice(0, 160),
        description: `Windows ${chat.windows_turns} 轮 · WSL ${chat.wsl_turns} 轮`, id: chat.id,
        choiceToken: chat.choice_token, windowsTurns: chat.windows_turns, wslTurns: chat.wsl_turns }));
    if (!choices.length) {
      await vscode.window.showInformationMessage(listing.chats.length ? '冲突聊天尚未空闲或无法可靠比较。请静置两端这条聊天至少 60 秒后重新检查。' : '没有待处理的聊天冲突。'); return;
    }
    const chosen = await vscode.window.showQuickPick(choices, { title: '选择有冲突的聊天', placeHolder: '手动决定保留 Windows 还是 WSL 的版本', canPickMany: false });
    if (!chosen) return;
    const methods = [
      { label: '以 Windows 为准', description: `保留 Windows ${chosen.windowsTurns} 轮，替换 WSL 的版本`, keepSide: 'windows' },
      { label: '以 WSL 为准', description: `保留 WSL ${chosen.wslTurns} 轮，替换 Windows 的版本`, keepSide: 'wsl' }
    ];
    const method = await vscode.window.showQuickPick(methods, { title: '选择保留哪一端', placeHolder: '对端独有的消息将从聊天中移除；替换前会完整备份', canPickMany: false });
    if (!method) return;
    const source = method.keepSide === 'windows' ? 'Windows' : 'WSL';
    const target = method.keepSide === 'windows' ? 'WSL' : 'Windows';
    const accepted = await vscode.window.showWarningMessage(`确定以 ${source} 为准吗？安全同步时将先完整备份两端，再用当前 ${source} 版本替换 ${target} 的聊天。${target} 独有的消息会从聊天中移除，只保留在备份中。两端这条聊天在完成同步前请停止发送消息；版本变化后需要重新选择。`, { modal: true }, `保留 ${source} 版本`);
    if (accepted !== `保留 ${source} 版本` || !current()) return;
    const action = { type: 'id', value: chosen.id, keepSide: method.keepSide, expectedToken: chosen.choiceToken };
    const result = await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: '正在备份并保存聊天版本选择', cancellable: false },
      () => runMergeWindows(config, action));
    if (result.ok === true && result.status === 'choice_saved' && result.reason === 'waiting_for_safe_sync' &&
        result.id === chosen.id && result.keep_side === method.keepSide) {
      await vscode.window.showInformationMessage(`已保存以 ${source} 为准的选择，尚未同步。等待 ${target} Codex 后端停止、两端聊天保持空闲后，定时任务会更新对端；若版本发生变化，需重新确认。不会创建聊天副本。`);
    } else await vscode.window.showWarningMessage(mergeWaitingMessage(result));
  } catch {
    await vscode.window.showWarningMessage('冲突处理未完成。请检查恢复记录或重新安装冲突处理工具后重试。');
  } finally {
    mergeInProgress = false;
    if (configurePending) { configurePending = false; configure(false); }
  }
}

async function check(manual = false) {
  if (mergeInProgress || checkInProgress) {
    if (manual) await vscode.window.showInformationMessage('已有同步检查正在进行，请等待本轮结束。');
    return;
  }
  if (!coordinator) {
    if (manual) await vscode.window.showWarningMessage('同步尚未启用或配置不完整，请先检查同步桥设置。');
    return;
  }
  checkInProgress = true;
  let mergeRequested = false;
  try {
    if (extensionContext.globalState.get('needsReview')) {
      if (!manual && extensionContext.globalState.get('reviewRecoverySuppressed')) return;
      if (recoveryInProgress) {
        if (manual) await vscode.window.showInformationMessage('恢复检查正在进行，请等待本轮结束。');
        return;
      }
      recoveryInProgress = true;
      const generation = disableGeneration;
      try {
        const recovered = await coordinator.recover();
        if (recovered.kind === 'recovered') {
          if (generation !== disableGeneration) return;
          await extensionContext.globalState.update('reviewError', undefined);
          await extensionContext.globalState.update('nextAttemptAt', 0);
          if (generation !== disableGeneration) return;
          await vscode.workspace.getConfiguration(section).update('enabled', true, vscode.ConfigurationTarget.Global);
          if (generation !== disableGeneration) {
            await vscode.workspace.getConfiguration(section).update('enabled', false, vscode.ConfigurationTarget.Global);
            return;
          }
          await extensionContext.globalState.update('reviewRecoverySuppressed', false);
          await extensionContext.globalState.update('needsReview', false);
          if (generation !== disableGeneration) {
            await extensionContext.globalState.update('reviewRecoverySuppressed', true);
            await extensionContext.globalState.update('needsReview', true);
            await vscode.workspace.getConfiguration(section).update('enabled', false, vscode.ConfigurationTarget.Global);
            return;
          }
          status.text = '$(history) Codex：已恢复自动同步';
          status.tooltip = '恢复检查已通过，后续检查会继续同步；本次恢复检查未传输记录。';
          if (manual) await vscode.window.showInformationMessage(status.tooltip);
          return;
        }
        const presentation = describeResult(recovered);
        status.text = '$(warning) Codex：同步需要检查';
        status.tooltip = '自动同步仍暂停。' + presentation.detail;
        if (manual) await vscode.window.showWarningMessage(status.tooltip);
        return;
      } finally {
        recoveryInProgress = false;
        configurePending = true;
      }
    }
    const result = await coordinator.check({ manual });
    const presentation = describeResult(result);
    status.text = presentation.text;
    status.tooltip = presentation.detail; lastError = '';
    if (manual) {
      const hasConflicts = ['chat_conflict', 'manual_choice_stale'].includes(result.reason) || (Number.isInteger(result.preview?.conflict_count) && result.preview.conflict_count > 0);
      if (hasConflicts) mergeRequested = await vscode.window.showWarningMessage(presentation.detail, '处理聊天冲突') === '处理聊天冲突';
      else if (presentation.warning) await vscode.window.showWarningMessage(presentation.detail);
      else await vscode.window.showInformationMessage(presentation.detail);
    }
  } catch (error) {
    const message = failureMessage(error);
    status.text = '$(warning) Codex：同步待处理';
    status.tooltip = message;
    if (manual && lastError !== message) await vscode.window.showWarningMessage(message);
    lastError = message;
  } finally {
    checkInProgress = false;
    if (configurePending) { configurePending = false; configure(false); }
  }
  if (mergeRequested) await mergeChats();
}

function configure(initialCheck = true) {
  if (mergeInProgress || checkInProgress || recoveryInProgress || coordinator?.running) { configurePending = true; return; }
  if (timer) clearInterval(timer); timer = null; coordinator = null;
  const config = settings();
  if (setupInProgress) return;
  process.env.CODEX_HISTORY_SYNC_STATE_DIR = config.stateDir || '';
  const needsReview = extensionContext.globalState.get('needsReview');
  const recoverySuppressed = extensionContext.globalState.get('reviewRecoverySuppressed');
  if (!config.enabled && !needsReview) { status.hide(); return; }
  status.show();
  if (!isWsl()) { status.text = '$(history) Codex：请在 WSL 中启用'; return; }
  if (vscode.workspace.getConfiguration('chatgpt').get('cliExecutable') !== gatewayName) {
    status.text = '$(warning) Codex：需要配置同步桥';
    status.tooltip = 'Codex 启动设置已变更；请重新完成同步桥设置后同步。';
    return;
  }
  try {
    validateConfig(config);
    const generation = configurationGeneration;
    const disableAtStart = disableGeneration;
    coordinator = new Coordinator(config, {
      shouldContinue: () => generation === configurationGeneration && disableAtStart === disableGeneration && settings().enabled === true,
      confirmReload: async manual => {
        if (config.reloadMode === 'automatic' && extensionContext.globalState.get('automaticReloadAccepted') === true) return true;
        if (!manual) return false;
        const automatic = config.reloadMode === 'automatic';
        const accepted = await vscode.window.showWarningMessage(automatic
          ? '允许空闲时自动重载 VS Code 窗口吗？插件无法检查未发送的聊天草稿，请先发送或保存。以后自动重载前不会再次询问。'
          : '同步会重载此 VS Code 窗口。请确认 Codex 输入框中的草稿已发送或保存；插件无法检查这些草稿。', { modal: true }, automatic ? '允许自动重载并同步' : '同步并重载');
        if (accepted !== (automatic ? '允许自动重载并同步' : '同步并重载')) return false;
        if (automatic) await extensionContext.globalState.update('automaticReloadAccepted', true);
        return true;
      },
      canReload,
      reloadBlockReason,
      nextAttemptAt: () => extensionContext.globalState.get('nextAttemptAt', 0),
      onDeferred: value => extensionContext.globalState.update('nextAttemptAt', value),
      reload: () => vscode.commands.executeCommand('workbench.action.reloadWindow'),
      onFailure: async error => {
        await extensionContext.globalState.update('reviewError', failureMessage(error));
        await extensionContext.globalState.update('needsReview', true);
        await vscode.workspace.getConfiguration(section).update('enabled', false, vscode.ConfigurationTarget.Global);
      }
    });
  } catch (error) { status.text = '$(warning) Codex：需要配置'; status.tooltip = error.message; return; }
  status.text = needsReview ? '$(warning) Codex：同步需要检查' : '$(history) Codex：自动同步已启用';
  status.tooltip = needsReview ? recoverySuppressed ? '自动同步已手动停用；点击检查可验证是否能够恢复。' : (extensionContext.globalState.get('reviewError') || '自动同步暂停，正在等待安全恢复检查。') : '';
  if (!needsReview || !recoverySuppressed) {
    timer = setInterval(() => void check(), Math.max(30, config.intervalSeconds || 60) * 1000);
    timer.unref?.();
    if (initialCheck) void check();
  }
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
      if (mergeInProgress || setupInProgress) { await vscode.window.showWarningMessage('冲突处理或设置正在进行，请等待本轮结束。'); return; }
      if (!isWsl()) { await vscode.window.showWarningMessage('请在连接 WSL 的 VS Code 窗口中启用。'); return; }
      try { validateConfig(settings()); }
      catch (error) { await vscode.window.showWarningMessage(error.message); return; }
      if (context.globalState.get('needsReview')) { await check(true); return; }
      if (vscode.workspace.getConfiguration('chatgpt').get('cliExecutable') !== gatewayName) { await vscode.window.showWarningMessage('请先配置同步桥，再启用自动同步。'); return; }
      await context.globalState.update('reviewRecoverySuppressed', false);
      await vscode.workspace.getConfiguration(section).update('enabled', true, vscode.ConfigurationTarget.Global);
    }),
    vscode.commands.registerCommand('codexHistoryLiveSync.disable', disable),
    vscode.commands.registerCommand('codexHistoryLiveSync.checkNow', () => check(true)),
    vscode.commands.registerCommand('codexHistoryLiveSync.resolveConflicts', mergeChats),
    vscode.commands.registerCommand('codexHistoryLiveSync.mergeChats', mergeChats),
    vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration(section) || event.affectsConfiguration('chatgpt.cliExecutable')) {
        configurationGeneration += 1;
        configure();
      }
    }),
    { dispose: () => { if (timer) clearInterval(timer); } }
  );
  configure();
}
function deactivate() { if (timer) clearInterval(timer); }
module.exports = { activate, deactivate };
