'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execFileAsync = promisify(execFile);

function validateConfig(config) {
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

async function runWindows(config, mode) {
  const args = mode === 'recovery'
    ? [config.previewHelper, '--controller', config.windowsController, '--config', config.windowsConfig, '--json', '--recovery-state']
    : mode === true
    ? [config.previewHelper, '--controller', config.windowsController, '--config', config.windowsConfig, '--json']
    : [config.previewHelper, '--controller', config.windowsController, '--config', config.windowsConfig, '--json', '--sync-once'];
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

function describeResult(result) {
  const reasons = {
    chat_conflict: '同一聊天两端均有修改，或原有正文不同。已保留两端记录，未自动覆盖；需要检查冲突。',
    chat_busy: '聊天正在进行或尚未静默 60 秒。此次未同步，空闲后会再次检查。',
    dirty_editor: '存在未保存的文件。请先保存，之后会自动重试同步。',
    debugging: '正在调试。调试结束后会自动重试同步。',
    editor_not_idle: '编辑器有未保存内容或正在调试，此次未同步。',
    check_running: '已有同步检查正在进行，请等待本轮结束。',
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
    return { text: conflicts ? '$(warning) Codex：部分已同步，仍有冲突' : '$(check) Codex：新记录已同步',
      detail: `本轮实际同步 ${result.result.transferred} 条聊天。` + (result.result.resolved_conflicts > 0 ? `其中 ${result.result.resolved_conflicts} 条按聊天数量选择版本，被替换的版本已备份。` : '') + (conflicts ? `另有 ${conflicts} 条聊天存在冲突，已保留两端记录。` : ''), warning: conflicts > 0 };
  }
  const text = result.kind === 'conflict' ? '$(warning) Codex：未同步，聊天冲突'
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
  const diagnostics = Object.entries(result.preview?.blocked_reason_counts || {})
    .filter(([key, count]) => /^[a-z_]+$/.test(key) && Number.isInteger(count) && count > 0)
    .map(([key, count]) => `${key}：${count}`).join('；');
  const explanation = result.reason === 'chat_conflict' && result.preview?.conflict_resolution === 'prefer_longer'
    ? '两端聊天数量相同、无法可靠比较，或尚未建立可用基线。已保留两端，未自动选择版本。'
    : reasons[result.reason] || '无法确认同步结果，此次未报告同步成功。';
  return { text: selectedText, detail: explanation + selectionDetail + (diagnostics ? `\n检查原因：${diagnostics}` : ''),
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
    this.reloadBlockReason = dependencies.reloadBlockReason || (() => 'editor_not_idle');
    this.onFailure = dependencies.onFailure || (() => Promise.resolve());
    this.onDeferred = dependencies.onDeferred || (() => Promise.resolve());
    this.nextAttemptAt = dependencies.nextAttemptAt || (() => 0);
  }

  async recover() {
    if (this.running) return { kind: 'waiting', reason: 'check_running' };
    this.running = true;
    const pauseFile = path.join(this.config.stateDir, 'pause.json');
    let recoveryChildPid;
    const hasPause = async () => {
      try { await fs.lstat(pauseFile); return true; }
      catch (error) { if (error.code === 'ENOENT') return false; throw error; }
    };
    const health = async instance => {
      const reply = await this.request(instance, 'status');
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
      const instances = await this.discover(this.config.stateDir);
      if (instances.length === 0) return { kind: 'unavailable', reason: 'gateway_unavailable' };
      if (instances.length !== 1) return { kind: 'waiting', reason: 'multiple_windows' };
      if (instances[0].parent_pid !== process.pid) return { kind: 'waiting', reason: 'other_window' };
      const first = await health(instances[0]);
      if (first) return { kind: 'waiting', reason: first };
      if ((await this.runWindows(this.config, 'recovery'))?.safe_to_reload !== true) {
        return { kind: 'blocked', reason: 'recovery_pending' };
      }
      if (await hasPause()) return { kind: 'blocked', reason: 'existing_pause' };
      if (!sameInstances(instances, await this.discover(this.config.stateDir))) return { kind: 'waiting', reason: 'instances_changed' };
      const final = await health(instances[0]);
      if (final) return { kind: 'waiting', reason: final };
      if (await hasPause()) return { kind: 'blocked', reason: 'existing_pause' };
      return { kind: 'recovered', reason: 'recovery_verified' };
    } finally { this.running = false; }
  }

  async check() {
    if (this.running) return { kind: 'waiting', reason: 'check_running' };
    this.running = true;
    let lease = null; let pauseStarted = false; let operationFailed = false; let operationError;
    const pauseFile = path.join(this.config.stateDir, 'pause.json');
    try {
      if (!this.canReload()) return { kind: 'waiting', reason: this.reloadBlockReason() };
      if (Date.now() < this.nextAttemptAt()) return { kind: 'waiting', reason: 'retry_backoff' };
      const preview = await this.runWindows(this.config, true);
      if (preview.eligible !== true || !Array.isArray(preview.candidate_ids) || preview.candidate_ids.length === 0) return previewResult(preview);
      if (Number.isFinite(preview.controller_timeout_seconds)) this.config.controllerTimeoutSeconds = Math.max(this.config.controllerTimeoutSeconds || 600, preview.controller_timeout_seconds);
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
        if (!status.ok || !status.state || status.state.busy || status.state.starting || status.state.paused ||
            status.state.pending_request_count || status.state.server_request_count || status.state.active_turn_count || status.state.known_activity_count ||
            !Number.isFinite(status.state.idle_seconds) || status.state.idle_seconds < 60) return { kind: 'waiting', reason: 'codex_busy', preview };
      }
      if (!this.canReload()) return { kind: 'waiting', reason: this.reloadBlockReason(), preview };
      await secureDirectory(this.config.stateDir);
      const proposedLease = { protocol_version: 1, owner: 'vscode-companion', nonce: crypto.randomBytes(24).toString('hex'), pid: process.pid, created_at: new Date().toISOString() };
      let handle;
      try { handle = await fs.open(pauseFile, 'wx', 0o600); }
      catch (error) { if (error.code === 'EEXIST') return { kind: 'waiting', reason: 'existing_pause', preview }; throw error; }
      lease = proposedLease;
      try { await handle.writeFile(JSON.stringify(lease)); await handle.sync(); } finally { await handle.close(); }
      if (!sameInstances(instances, await this.discover(this.config.stateDir))) return { kind: 'waiting', reason: 'instances_changed', preview };
      for (const instance of instances) {
        let paused;
        try { paused = await this.request(instance, 'prepare_pause'); }
        catch (error) { pauseStarted = true; throw error; }
        if (!paused.ok && paused.error === 'busy') return { kind: 'waiting', reason: 'codex_busy', preview };
        if (!paused.ok && (paused.error === 'stop_failed' || paused.state?.shutdown_failed)) pauseStarted = true;
        if (!paused.ok || paused.paused !== true) throw new Error('Codex became busy; the update was postponed.');
        pauseStarted = true;
      }
      // New extension windows are refused rather than left running across an import.
      if (!sameInstances(instances, await this.discover(this.config.stateDir))) throw new Error('A new Codex window opened; the update was postponed.');
      for (const instance of instances) {
        const paused = await this.request(instance, 'status');
        if (!paused.ok || !paused.state || !paused.state.paused || paused.state.child_running || paused.state.busy) throw new Error('The Codex bridge has not paused.');
      }
      if (!this.canReload()) throw new Error('Unsaved work appeared; the update was postponed.');
      const result = await this.runWindows(this.config, false);
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
          try { recovery = await this.runWindows(this.config, 'recovery'); }
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

module.exports = { Coordinator, validateConfig, discoverInstances, readPrivateJson, runWindows, sameInstances, previewResult, describeResult };
