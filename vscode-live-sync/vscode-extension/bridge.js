'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execFileAsync = promisify(execFile);

function validateConfig(config) {
  if (config.reloadMode !== undefined && !['confirm', 'automatic'].includes(config.reloadMode)) throw new Error('Choose confirm or automatic for the window reload mode.');
  for (const [key, minimum] of [['intervalSeconds', 30], ['controllerTimeoutSeconds', 180]]) {
    if (config[key] !== undefined && (!Number.isInteger(config[key]) || config[key] < minimum)) throw new Error('Configure valid sync intervals and timeouts.');
  }
  for (const key of ['stateDir', 'windowsPython']) {
    if (typeof config[key] !== 'string' || !path.posix.isAbsolute(config[key]) || config[key].includes('\0')) {
      throw new Error('Configure absolute Linux paths for the sync bridge.');
    }
  }
  for (const key of ['windowsController', 'windowsConfig', 'previewHelper']) {
    if (typeof config[key] !== 'string' || !/^[A-Za-z]:[\\/]/.test(config[key]) || config[key].includes('\0')) {
      throw new Error('Configure absolute Windows paths for the synchronizer.');
    }
  }
  if (config.mergeHelper !== undefined && config.mergeHelper !== '' &&
      (typeof config.mergeHelper !== 'string' || !/^[A-Za-z]:[\\/]/.test(config.mergeHelper) || config.mergeHelper.includes('\0'))) {
    throw new Error('Configure an absolute Windows path for the merge helper.');
  }
}

function mergeArguments(config, action) {
  validateConfig(config);
  const helper = config.mergeHelper || path.win32.join(path.win32.dirname(config.previewHelper), 'merge_chat.py');
  const args = [helper, '--controller', config.windowsController, '--config', config.windowsConfig];
  if (action?.type === 'list') return [...args, '--list'];
  if (action?.type !== 'id' || typeof action.value !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(action.value) ||
      !['windows', 'wsl'].includes(action.keepSide) || typeof action.expectedToken !== 'string' ||
      !/^[0-9a-f]{64}$/i.test(action.expectedToken)) throw new Error('Choose a valid chat version from the current conflict list.');
  return [...args, '--id', action.value, '--keep-side', action.keepSide, '--expected-token', action.expectedToken];
}

async function runMergeWindows(config, action, requestId) {
  const args = mergeArguments(config, action);
  if (requestId !== undefined) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(requestId)) throw new Error("Invalid progress request.");
    args.push("--progress-request-id", requestId);
  }
  try {
    const { stdout } = await execFileAsync(config.windowsPython, args, {
      timeout: Math.max(1200, config.controllerTimeoutSeconds || 600) * 1000,
      maxBuffer: 2 * 1024 * 1024, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
    });
    const result = JSON.parse(stdout.trim());
    if (!result || typeof result.ok !== 'boolean' || !['candidates', 'waiting', 'choice_saved', 'error'].includes(result.status)) throw new Error('Invalid conflict result.');
    return result;
  } catch {
    throw new Error('冲突处理未完成。请查看恢复记录或重新安装冲突处理工具后重试。');
  }
}

async function secureDirectory(dir) {
  const stat = await fs.lstat(dir);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 ||
      (process.getuid && stat.uid !== process.getuid())) throw new Error('The gateway state directory must be private to your user.');
}

async function readPrivateJson(file) {
  const handle = await fs.open(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size > 32768 || (stat.mode & 0o077) !== 0 ||
        (process.getuid && stat.uid !== process.getuid())) throw new Error('Unsafe gateway state file.');
    return JSON.parse(await handle.readFile('utf8'));
  } finally { await handle.close(); }
}

async function discoverInstances(stateDir) {
  await secureDirectory(stateDir);
  const dir = path.join(stateDir, 'instances');
  await secureDirectory(dir);
  const result = [];
  for (const name of (await fs.readdir(dir)).sort()) {
    if (!name.endsWith('.json')) continue;
    const record = await readPrivateJson(path.join(dir, name));
    if (record.protocol_version !== 1 || typeof record.instance_id !== 'string' ||
        typeof record.nonce !== 'string' || record.nonce.length < 16 ||
        typeof record.socket_path !== 'string' || !path.isAbsolute(record.socket_path) ||
        path.relative(stateDir, record.socket_path).startsWith('..') ||
        path.isAbsolute(path.relative(stateDir, record.socket_path))) throw new Error('Invalid gateway instance.');
    result.push(record);
  }
  if (new Set(result.map(item => item.instance_id)).size !== result.length) throw new Error('Duplicate gateway instance.');
  return result;
}

function request(instance, action) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(instance.socket_path);
    let buffer = ''; let done = false;
    const finish = (error, result) => {
      if (done) return; done = true; socket.destroy();
      error ? reject(error) : resolve(result);
    };
    socket.setTimeout(10000, () => finish(new Error('The Codex bridge did not respond.')));
    socket.on('error', () => finish(new Error('The Codex bridge is unavailable.')));
    socket.on('connect', () => socket.write(JSON.stringify({ action, instance_id: instance.instance_id, nonce: instance.nonce }) + '\n'));
    socket.on('data', chunk => {
      buffer += chunk.toString('utf8');
      if (buffer.length > 65536) return finish(new Error('Invalid bridge response.'));
      const newline = buffer.indexOf('\n');
      if (newline >= 0) {
        try { finish(null, JSON.parse(buffer.slice(0, newline))); }
        catch { finish(new Error('Invalid bridge response.')); }
      }
    });
    socket.on('end', () => finish(new Error('The bridge connection ended early.')));
  });
}

async function runWindows(config, mode, requestId) {
  const args = mode === 'progress-path'
    ? [config.previewHelper, '--controller', config.windowsController, '--config', config.windowsConfig, '--json', '--progress-path']
    : mode === 'recovery'
    ? [config.previewHelper, '--controller', config.windowsController, '--config', config.windowsConfig, '--json', '--recovery-state']
    : mode === true
    ? [config.previewHelper, '--controller', config.windowsController, '--config', config.windowsConfig, '--json']
    : [config.previewHelper, '--controller', config.windowsController, '--config', config.windowsConfig, '--json', '--sync-once'];
  if (requestId !== undefined) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(requestId)) throw new Error('Invalid progress request.');
    args.push('--progress-request-id', requestId);
  }
  try {
    const { stdout } = await execFileAsync(config.windowsPython, args, {
      timeout: Math.max(180, Number(config.controllerTimeoutSeconds) || 600) * 1000, maxBuffer: 2 * 1024 * 1024, windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
    });
    return JSON.parse(stdout.trim());
  } catch (error) {
    // Do not expose arbitrary stderr, arguments, or user paths in notifications.
    if (error.code === 2 && /can't open file/i.test(String(error.stderr || ''))) {
      throw new Error('无法读取 Windows 同步脚本，请重新安装同步桥以修复路径。');
    }
    if (error.code === 'ENOENT') throw new Error('找不到 Windows Python，请检查同步桥的解释器配置。');
    if (error.killed) throw new Error('同步检查超时，已保留原记录；请稍后重试。');
    throw new Error(mode === 'recovery' ? '无法确认同步恢复状态，暂停保护已保留。' : mode === true ? '同步检查未完成，请检查安装路径及配置。' : '同步未完成，请检查恢复记录。');
  }
}

function sameInstances(left, right) {
  const shape = rows => rows.map(row => [row.instance_id, row.pid, row.parent_pid, row.nonce, row.socket_path]);
  return JSON.stringify(shape(left)) === JSON.stringify(shape(right));
}

function previewResult(preview) {
  const reason = preview?.reason || 'unknown_preview_state';
  const kind = reason === 'chat_conflict' ? 'conflict'
    : reason === 'no_pending_updates' ? 'current'
    : ['chat_busy', 'other_native_backends', 'native_sync_can_run', 'new_chats_pending', 'opposite_direction_pending', 'longer_wsl_pending', 'wsl_stopped'].includes(reason) ? 'waiting'
    : 'blocked';
  return { kind, reason, preview };
}

// Diagnostic metadata is emitted only from a fully known running backend.
// It never changes the existing admission checks below.
function gatewayWaitDetails(reply) {
  const state = reply?.state;
  const counts = ['pending_request_count', 'server_request_count', 'active_turn_count', 'known_activity_count'];
  if (reply?.ok !== true || !state || state.child_running !== true || state.paused !== false ||
      state.shutdown_failed !== false || state.starting !== false ||
      !Number.isInteger(state.child_pid) || state.child_pid <= 0 || typeof state.busy !== 'boolean' ||
      counts.some(key => !Number.isSafeInteger(state[key]) || state[key] < 0)) return {};
  if (state.busy || counts.some(key => state[key] > 0)) return { busy_activity: true };
  return Number.isFinite(state.idle_seconds) && state.idle_seconds >= 0 && state.idle_seconds < 60
    ? { idle_seconds: Math.floor(state.idle_seconds) } : {};
}

function describeResult(result) {
  const reasons = {
    chat_conflict: '同一聊天两端均有修改，或原有正文不同。已保留两端记录，未自动覆盖；需要检查冲突。',
    manual_choice_stale: '之前手动选择的聊天版本已经变化，未按旧选择覆盖聊天。请重新检查并确认保留 Windows 还是 WSL。',
    chat_busy: '聊天正在进行或尚未静默 60 秒。此次未同步，空闲后会再次检查。',
    dirty_editor: '存在未保存的文件。请先保存，之后会自动重试同步。',
    debugging: '正在调试。调试结束后会自动重试同步。',
    editor_not_idle: '编辑器有未保存内容或正在调试，此次未同步。',
    check_running: '已有同步检查正在进行，请等待本轮结束。',
    reload_confirmation: '有新记录待同步。请点击此状态，确认聊天输入框中的草稿已发送或保存后同步并重载窗口。',
    sync_disabled: '同步设置已变更或自动同步已停用，本轮未继续导入。',
    retry_backoff: '上次未传输记录，正在等待重试时间。此次没有同步。',
    other_native_backends: '检测到其他独立或无法确认归属的 Codex 进程，同步暂缓。这不一定表示打开了多个 VS Code 窗口。',
    multiple_windows: '检测到多个同步桥窗口，此次未同步。请只保留一个 WSL Codex 窗口。',
    other_window: '同步桥属于另一个 VS Code 窗口，此次未同步。请在该窗口触发检查。',
    codex_busy: 'Codex 正在处理请求或尚未空闲 60 秒，此次未同步。',
    existing_pause: '已有暂停保护，此次未同步。请先检查上一轮同步是否需要恢复。',
    recovery_pending: '恢复检查尚未通过，自动同步仍暂停；空闲后会自动重试，也可再次点击检查。',
    gateway_unhealthy: '同步桥尚未恢复正常运行，自动同步仍暂停。请检查 Codex 连接状态。',
    instances_changed: 'Codex 窗口发生变化，此次未同步，稍后会重试。',
    controller_running: '定时同步正在执行，此次未重复传输。',
    zero_transfers: '同步器本轮没有传输记录，已恢复 Codex，稍后会重新检查。',
    native_sync_can_run: '当前未连接 Codex 后端，请等待定时同步或运行基础同步脚本。',
    new_chats_pending: '有新聊天待导入；当前窗口自动补齐只处理已有聊天，请通过基础同步导入。',
    opposite_direction_pending: '有 WSL → Windows 更新待同步；请等待 Windows Codex 空闲并停止后端后使用基础同步。',
    longer_wsl_pending: '已选择 WSL 中聊天更多的一端。等待桌面端 Codex 后端停止后，定时任务会先备份旧版本，再同步到 Windows。',
    wsl_stopped: 'WSL 未运行，此次未同步。',
    baseline_missing: '缺少同步基线，此次未同步。请先初始化基础同步器。',
    pending_import: '存在未完成的导入，此次未同步。请先检查恢复记录。',
    backend_detection_failed: '无法确认 Codex 后端状态，为保护聊天记录，此次未同步。',
    incoming_update_blocked: '检测到待更新聊天，但正文读取或索引检查未通过。未修改记录，请检查基础同步器的问题报告。',
    gateway_unavailable: '当前没有可用的同步桥，此次未同步。请确认已完成设置并重载窗口。',
    no_pending_updates: '未发现可补齐到 WSL 的新更新。检查完成不代表执行了同步。'
  };
  if (result.kind === 'synced' && Number.isInteger(result.result?.transferred) && result.result.transferred > 0) {
    const conflicts = Number.isInteger(result.preview?.conflict_count) && result.preview.conflict_count > 0 ? result.preview.conflict_count : 0;
    const resolved = Number.isInteger(result.result.resolved_conflicts) && result.result.resolved_conflicts > 0 ? result.result.resolved_conflicts : 0;
    const manualResolved = Array.isArray(result.result.conflict_results) ? result.result.conflict_results.filter(choice => choice.reason === 'manual_keep').length : 0;
    const resolutionDetail = resolved ? `其中 ${resolved} 条冲突已处理${manualResolved ? `，${manualResolved} 条按你的选择保留版本` : ''}，被替换的版本已备份。` : '';
    return { text: conflicts ? '$(warning) Codex：部分已同步，仍有冲突' : '$(check) Codex：新记录已同步',
      detail: `本轮实际同步 ${result.result.transferred} 条聊天。` + resolutionDetail + (conflicts ? `另有 ${conflicts} 条聊天存在冲突，已保留两端记录。` : ''), warning: conflicts > 0 };
  }
  const text = result.kind === 'conflict' ? '$(warning) Codex：未同步，聊天冲突'
    : result.reason === 'reload_confirmation' ? '$(history) Codex：有更新，点击确认同步'
    : result.kind === 'current' ? '$(history) Codex：没有待同步更新'
    : result.kind === 'blocked' ? '$(warning) Codex：未同步，需要检查'
    : result.kind === 'unavailable' ? '$(history) Codex：尚未连接同步桥'
    : ['dirty_editor', 'debugging', 'editor_not_idle', 'chat_busy', 'codex_busy'].includes(result.reason) ? '$(history) Codex：等待空闲，未同步'
    : '$(history) Codex：等待同步';
  const selections = result.preview?.conflict_resolution === 'prefer_longer' && Array.isArray(result.preview?.conflict_decisions)
    ? result.preview.conflict_decisions : [];
  const selectedText = selections.length && result.kind === 'waiting'
    ? result.reason === 'longer_wsl_pending' ? '$(history) Codex：已选 WSL 记录，等待桌面端退出'
      : '$(history) Codex：已选较多记录，等待同步'
    : text;
  const choiceCounts = ['windows', 'wsl'].map(side => [side, selections.filter(choice => choice.source === side).length]);
  const selectionDetail = selections.length ? '\n已按数量选择：' + choiceCounts.filter(([, count]) => count > 0)
    .map(([side, count]) => `${side === 'windows' ? 'Windows' : 'WSL'} ${count} 条`).join('；') + '。替换前会完整备份旧版本。'
    + (selections.some(choice => choice.source === 'wsl' && choice.waiting_reason === 'target_backend_running')
      ? 'WSL 较多的记录需等待桌面端后端停止。' : '') : '';
  const manual = Array.isArray(result.preview?.manual_choices) ? result.preview.manual_choices.filter(choice => ['windows', 'wsl'].includes(choice.keep_side) && choice.reason !== 'manual_choice_stale') : [];
  const manualDetail = manual.length ? '\n已手动选择：' + ['windows', 'wsl'].map(side => [side, manual.filter(choice => choice.keep_side === side).length])
    .filter(([, count]) => count > 0).map(([side, count]) => `${side === 'windows' ? 'Windows' : 'WSL'} ${count} 条`).join('；') + '。选择已保存，尚未同步；等待目标后端停止且双方版本保持不变。' : '';
  const stale = result.preview?.blocked_reason_counts?.manual_choice_stale > 0;
  const incompatible = result.preview?.blocked_reason_counts?.workspace_cwd_unmappable > 0;
  const diagnostics = Object.entries(result.preview?.blocked_reason_counts || {})
    .filter(([key, count]) => /^[a-z_]+$/.test(key) && Number.isInteger(count) && count > 0)
    .map(([key, count]) => `${key}：${count}`).join('；');
  const codexWait = result.reason === 'codex_busy'
    ? result.busy_activity === true ? 'Codex 仍有请求或任务正在运行，此次未同步。'
      : Number.isFinite(result.idle_seconds) && result.idle_seconds >= 0 && result.idle_seconds < 60
      ? `后端已空闲 ${Math.floor(result.idle_seconds)}/60 秒，等待安全同步。此次未同步。` : reasons.codex_busy
    : undefined;
  const explanation = stale ? reasons.manual_choice_stale
    : incompatible ? '聊天工作目录无法映射到目标系统中存在的目录。未替换聊天，请检查工作目录兼容性。'
    : result.reason === 'chat_conflict' && result.preview?.conflict_resolution === 'prefer_longer'
    ? '两端聊天数量相同、无法可靠比较，或尚未建立可用基线。已保留两端，未自动选择版本。'
    : codexWait || reasons[result.reason] || '无法确认同步结果，此次未报告同步成功。';
  return { text: selectedText, detail: explanation + selectionDetail + manualDetail + (diagnostics ? `\n检查原因：${diagnostics}` : ''),
    warning: ['conflict', 'blocked'].includes(result.kind) };
}

class Coordinator {
  constructor(config, dependencies = {}) {
    validateConfig(config); this.config = config; this.running = false;
    this.discover = dependencies.discover || discoverInstances;
    this.request = dependencies.request || request;
    this.runWindows = dependencies.runWindows || runWindows;
    this.reload = dependencies.reload || (() => Promise.resolve());
    this.canReload = dependencies.canReload || (() => true);
    this.shouldContinue = dependencies.shouldContinue || (() => true);
    this.confirmReload = dependencies.confirmReload || (() => Promise.resolve(true));
    this.reloadBlockReason = dependencies.reloadBlockReason || (() => 'editor_not_idle');
    this.onFailure = dependencies.onFailure || (() => Promise.resolve());
    this.onDeferred = dependencies.onDeferred || (() => Promise.resolve());
    this.nextAttemptAt = dependencies.nextAttemptAt || (() => 0);
    this.onProgress = dependencies.onProgress || (() => {});
  }

  progress(phase, extra = {}) {
    try { const pending = this.onProgress({ phase, ...extra }); pending?.catch?.(() => {}); } catch { /* Progress is optional and cannot change safety decisions. */ }
  }

  async callWindows(mode) {
    const requestId = crypto.randomUUID();
    this.progress(mode === true ? 'checking' : mode === 'recovery' ? 'recovery' : 'starting', { requestId, newOperation: true });
    return this.runWindows(this.config, mode, requestId);
  }

  async recover() {
    if (this.running) return { kind: 'waiting', reason: 'check_running' };
    this.running = true;
    const pauseFile = path.join(this.config.stateDir, 'pause.json');
    let recoveryChildPid; let recoveryWaitDetails = {};
    const hasPause = async () => {
      try { await fs.lstat(pauseFile); return true; }
      catch (error) { if (error.code === 'ENOENT') return false; throw error; }
    };
    const health = async instance => {
      const reply = await this.request(instance, 'status');
      recoveryWaitDetails = gatewayWaitDetails(reply);
      const state = reply?.state;
      if (!reply?.ok || !state || state.child_running !== true || state.paused !== false ||
          state.shutdown_failed !== false || state.starting !== false ||
          !Number.isInteger(state.child_pid) || state.child_pid <= 0) return 'gateway_unhealthy';
      if (state.busy !== false || ['pending_request_count', 'server_request_count', 'active_turn_count', 'known_activity_count'].some(key => state[key] !== 0) ||
          !Number.isFinite(state.idle_seconds) || state.idle_seconds < 60) return 'codex_busy';
      if (recoveryChildPid !== undefined && recoveryChildPid !== state.child_pid) return 'instances_changed';
      recoveryChildPid = state.child_pid;
      return null;
    };
    try {
      await secureDirectory(this.config.stateDir);
      if (await hasPause()) return { kind: 'blocked', reason: 'existing_pause' };
      this.progress('gateway');
      const instances = await this.discover(this.config.stateDir);
      if (instances.length === 0) return { kind: 'unavailable', reason: 'gateway_unavailable' };
      if (instances.length !== 1) return { kind: 'waiting', reason: 'multiple_windows' };
      if (instances[0].parent_pid !== process.pid) return { kind: 'waiting', reason: 'other_window' };
      const first = await health(instances[0]);
      if (first) return { kind: 'waiting', reason: first, ...recoveryWaitDetails };
      if ((await this.callWindows('recovery'))?.safe_to_reload !== true) {
        return { kind: 'blocked', reason: 'recovery_pending' };
      }
      if (await hasPause()) return { kind: 'blocked', reason: 'existing_pause' };
      if (!sameInstances(instances, await this.discover(this.config.stateDir))) return { kind: 'waiting', reason: 'instances_changed' };
      const final = await health(instances[0]);
      if (final) return { kind: 'waiting', reason: final, ...recoveryWaitDetails };
      if (await hasPause()) return { kind: 'blocked', reason: 'existing_pause' };
      return { kind: 'recovered', reason: 'recovery_verified' };
    } finally { this.running = false; }
  }

  async check(options = {}) {
    if (this.running) return { kind: 'waiting', reason: 'check_running' };
    this.running = true;
    let lease = null; let pauseStarted = false; let operationFailed = false; let operationError;
    const pauseFile = path.join(this.config.stateDir, 'pause.json');
    try {
      if (!this.shouldContinue()) return { kind: 'waiting', reason: 'sync_disabled' };
      if (!this.canReload()) return { kind: 'waiting', reason: this.reloadBlockReason() };
      if (Date.now() < this.nextAttemptAt()) return { kind: 'waiting', reason: 'retry_backoff' };
      const preview = await this.callWindows(true);
      if (!this.shouldContinue()) return { kind: 'waiting', reason: 'sync_disabled', preview };
      if (preview.eligible !== true || !Array.isArray(preview.candidate_ids) || preview.candidate_ids.length === 0) return previewResult(preview);
      if (Number.isFinite(preview.controller_timeout_seconds)) this.config.controllerTimeoutSeconds = Math.max(this.config.controllerTimeoutSeconds || 600, preview.controller_timeout_seconds);
      this.progress('gateway');
      const instances = await this.discover(this.config.stateDir);
      if (instances.length === 0) return { kind: 'unavailable', reason: 'gateway_unavailable', preview };
      if (instances.length !== 1) return { kind: 'waiting', reason: 'multiple_windows', preview };
      if (!Number.isInteger(instances[0].parent_pid) || instances[0].parent_pid !== process.pid) return { kind: 'waiting', reason: 'other_window', preview };
      for (const instance of instances) {
        const status = await this.request(instance, 'status');
        if (preview.native_backend_pid !== undefined &&
            (!Number.isInteger(preview.native_backend_pid) || status.state?.child_pid !== preview.native_backend_pid)) {
          return { kind: 'waiting', reason: 'instances_changed', preview };
        }
        const state = status.state;
        if (status.ok !== true || !state || state.busy !== false || state.starting !== false || state.paused !== false ||
            state.child_running !== true || state.shutdown_failed !== false || !Number.isInteger(state.child_pid) || state.child_pid <= 0 ||
            ['pending_request_count', 'server_request_count', 'active_turn_count', 'known_activity_count'].some(key => state[key] !== 0) ||
            !Number.isFinite(state.idle_seconds) || state.idle_seconds < 60) return { kind: 'waiting', reason: 'codex_busy', preview, ...gatewayWaitDetails(status) };
      }
      if (!this.shouldContinue()) return { kind: 'waiting', reason: 'sync_disabled', preview };
      this.progress('confirming');
      if (!await this.confirmReload(options.manual === true)) return { kind: 'waiting', reason: 'reload_confirmation', preview };
      if (!this.shouldContinue()) return { kind: 'waiting', reason: 'sync_disabled', preview };
      if (!this.canReload()) return { kind: 'waiting', reason: this.reloadBlockReason(), preview };
      await secureDirectory(this.config.stateDir);
      const proposedLease = { protocol_version: 1, owner: 'vscode-companion', nonce: crypto.randomBytes(24).toString('hex'), pid: process.pid, created_at: new Date().toISOString() };
      let handle;
      try { handle = await fs.open(pauseFile, 'wx', 0o600); }
      catch (error) { if (error.code === 'EEXIST') return { kind: 'waiting', reason: 'existing_pause', preview }; throw error; }
      lease = proposedLease;
      try { await handle.writeFile(JSON.stringify(lease)); await handle.sync(); } finally { await handle.close(); }
      if (!this.shouldContinue()) return { kind: 'waiting', reason: 'sync_disabled', preview };
      if (!sameInstances(instances, await this.discover(this.config.stateDir))) return { kind: 'waiting', reason: 'instances_changed', preview };
      this.progress('pausing');
      for (const instance of instances) {
        let paused;
        try { paused = await this.request(instance, 'prepare_pause'); }
        catch (error) { pauseStarted = true; throw error; }
        if (!paused.ok && paused.error === 'busy') return { kind: 'waiting', reason: 'codex_busy', preview };
        if (!paused.ok && (paused.error === 'stop_failed' || paused.state?.shutdown_failed)) pauseStarted = true;
        if (!paused.ok || paused.paused !== true) throw new Error('Codex became busy; the update was postponed.');
        pauseStarted = true;
      }
      if (!this.shouldContinue()) return { kind: 'waiting', reason: 'sync_disabled', preview };
      // New extension windows are refused rather than left running across an import.
      if (!sameInstances(instances, await this.discover(this.config.stateDir))) throw new Error('A new Codex window opened; the update was postponed.');
      for (const instance of instances) {
        const paused = await this.request(instance, 'status');
        if (paused.ok !== true || !paused.state || paused.state.paused !== true || paused.state.child_running !== false ||
            paused.state.busy !== false || paused.state.shutdown_failed !== false || paused.state.starting !== false ||
            ['pending_request_count', 'server_request_count', 'active_turn_count', 'known_activity_count'].some(key => paused.state[key] !== 0)) throw new Error('The Codex bridge has not paused.');
      }
      if (!this.shouldContinue()) return { kind: 'waiting', reason: 'sync_disabled', preview };
      if (!this.canReload()) throw new Error('Unsaved work appeared; the update was postponed.');
      const result = await this.callWindows(false);
      if (result.status !== 'completed' && result.status !== 'skipped_already_running') throw new Error('The synchronizer needs review before another update.');
      if (result.status === 'skipped_already_running' || !Number.isInteger(result.transferred) || result.transferred < 1) {
        await this.onDeferred(Date.now() + 300000);
        return { kind: 'waiting', reason: result.status === 'skipped_already_running' ? 'controller_running' : 'zero_transfers', result, preview };
      }
      return { kind: 'synced', result, preview };
    } catch (error) {
      operationFailed = true;
      operationError = error;
      throw error;
    } finally {
      // Never remove the synchronizer's pending marker. It belongs to its own recovery protocol.
      try {
        if (pauseStarted) {
          let recovery;
          try { recovery = await this.callWindows('recovery'); }
          catch { recovery = { safe_to_reload: false }; }
          const safe = recovery?.safe_to_reload === true;
          if (operationFailed || !safe) await this.onFailure(operationError || new Error('无法确认同步恢复状态，暂停保护已保留。'));
          if (!safe) throw new Error('同步需要检查：已保留暂停保护。请确认没有未完成导入后再恢复 Codex。');
          if (!this.canReload()) {
            if (!operationFailed) await this.onFailure(new Error('编辑器有未保存内容或正在调试，已保留暂停保护。'));
            throw new Error('编辑器有未保存内容或正在调试，已保留暂停保护。请保存并完成恢复检查后重载。');
          }
          try { if (lease) await this.removeLease(pauseFile, lease); }
          catch (error) { if (!operationFailed) await this.onFailure(error); throw error; }
          lease = null;
          // Verify that no controller/importer or unfinished journal exists before permitting a fresh backend.
          this.progress('reloading');
          try { await this.reload(); }
          catch (error) { if (!operationFailed) await this.onFailure(error); throw error; }
        } else if (lease) {
          await this.removeLease(pauseFile, lease);
        }
      } finally { this.running = false; }
    }
  }

  async removeLease(file, lease) {
    const actual = await readPrivateJson(file);
    if (actual.nonce !== lease.nonce || actual.pid !== process.pid) throw new Error('The bridge pause lease changed; manual recovery is required.');
    await fs.unlink(file);
  }
}

module.exports = { Coordinator, validateConfig, discoverInstances, readPrivateJson, runWindows, runMergeWindows, mergeArguments, sameInstances, previewResult, describeResult };
