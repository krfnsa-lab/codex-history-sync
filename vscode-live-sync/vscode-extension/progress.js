'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const phases = {
  starting: '开始检查', checking_windows: '检查 Windows 记录', checking_wsl: '检查 WSL 记录',
  planning: '比较两端记录', exporting: '导出聊天记录', previewing: '检查导入条件', checking_backend: '检查后端状态',
  backing_up: '备份原记录', writing: '写入聊天记录', verifying: '校验同步结果', checkpointing: '保存同步状态',
  completed: '本轮处理结束', waiting: '等待安全同步条件', error: '本轮未完成', scanning: '扫描记录', reading: '读取记录',
  repairing: '修复记录', committed: '记录已写入', confirming: '等待确认同步并重载', checking: '检查新记录',
  pausing: '暂停 WSL Codex 后端', reloading: '正在重新加载窗口', recovery: '检查安全恢复状态', choosing: '等待选择保留的聊天版本',
  scan: '扫描记录', read: '读取记录', export: '导出记录', backup: '备份原记录', write: '写入记录', verify: '校验记录',
  gateway: '检查 Codex 空闲状态', controller_running: '查看正在执行的定时同步', choice: '备份并保存版本选择'
};
const integer = value => Number.isSafeInteger(value) && value >= 0;
const optionalCount = value => value === null || value === undefined || integer(value);
function validateSnapshot(value, now = Date.now()) {
  if (!value || value.schema_version !== 1 || value.owner !== 'codex-history-sync' || !UUID.test(value.run_id || '') ||
      !UUID.test(value.operation_id || '') || (value.request_id !== undefined && !UUID.test(value.request_id)) || !['sync', 'check', 'repair', 'choice'].includes(value.kind) ||
      !['running', 'waiting', 'completed', 'error'].includes(value.status) || !Object.hasOwn(phases, value.phase) ||
      !Number.isFinite(value.elapsed_seconds) || value.elapsed_seconds < 0 || !integer(value.pid) || value.pid === 0 ||
      ![null, undefined, 'windows', 'wsl'].includes(value.side) ||
      !['processed_chats', 'total_chats', 'transferred_chats', 'deferred_chats', 'current_index'].every(key => optionalCount(value[key]))) return null;
  if (typeof value.started_at !== 'string' || typeof value.updated_at !== 'string') return null;
  const started = Date.parse(value.started_at); const updated = Date.parse(value.updated_at);
  if (!Number.isFinite(started) || !Number.isFinite(updated) || updated < started || updated > now + 10000 || started > now + 10000) return null;
  if (value.total_chats != null && value.processed_chats != null && value.processed_chats > value.total_chats) return null;
  return value;
}
function validateDetail(value, snapshot, now = Date.now()) {
  if (!validateSnapshot(value, now) || value.run_id !== snapshot.run_id ||
      value.operation_id !== snapshot.operation_id || !Object.hasOwn(phases, value.phase) ||
      !optionalCount(value.processed_records) || !optionalCount(value.total_records) ||
      (value.total_records != null && value.processed_records != null && value.processed_records > value.total_records)) return null;
  if (value.updated_at !== undefined && (!Number.isFinite(Date.parse(value.updated_at)) || Date.parse(value.updated_at) > now + 10000)) return null;
  return value;
}
const reasons = { wsl_stopped: 'WSL 未运行', task_failed: '本轮任务未完成', pending_import: '有未完成的导入待检查',
  batch_limit: '达到本轮聊天数量上限', time_budget: '达到本轮时间上限', conflict: '有冲突，需要手动选择保留版本',
  source_busy: '来源聊天正在使用或尚未空闲', target_busy: '目标聊天正在使用或尚未空闲', source_changed: '聊天版本已变化',
  target_backend_running: '等待目标 Codex 后端停止', target_backend_started: '目标后端重新启动，已暂缓',
  environment_paths_unavailable: '工作目录尚无法映射', manual_choice_stale: '版本已变化，需要重新确认',
  controller_running: '另一个同步任务正在执行', updates_available: '发现更新，等待确认应用', deferred: '部分记录暂缓处理' };
function latestUpdate(snapshot, detail) { return Math.max(Date.parse(snapshot.updated_at), Date.parse(detail?.updated_at) || 0); }
async function readProgressJson(file) {
  const handle = await fs.open(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size > 65536 || (process.getuid && stat.uid !== process.getuid())) throw new Error('Unsafe progress file');
    return JSON.parse(await handle.readFile('utf8'));
  } finally { await handle.close(); }
}
function linuxPath(value) { return typeof value === 'string' && path.posix.isAbsolute(value) && !value.includes('\0') ? value : null; }
function render(snapshot, detail, elapsed) {
  let text = phases[(snapshot.status === 'running' && detail?.status === 'running' && detail.phase) || snapshot.phase];
  if (snapshot.status === 'waiting' && Object.hasOwn(reasons, snapshot.reason)) text += '：' + reasons[snapshot.reason];
  if (snapshot.side) text += ` · ${snapshot.side === 'windows' ? 'Windows' : 'WSL'}`;
  const counts = snapshot.total_chats != null ? snapshot.processed_chats != null ? `已处理 ${snapshot.processed_chats}/${snapshot.total_chats} 条` : `共 ${snapshot.total_chats} 条` : '';
  if (counts) text += ` · ${counts}`;
  const outcomes = [];
  if (snapshot.transferred_chats != null) outcomes.push(`已同步 ${snapshot.transferred_chats} 条`);
  if (snapshot.deferred_chats != null) outcomes.push(`暂缓 ${snapshot.deferred_chats} 条`);
  if (outcomes.length) text += ' · ' + outcomes.join('，');
  if (detail?.processed_records != null) text += detail.total_records != null ? ` · 记录 ${detail.processed_records}/${detail.total_records}` : ` · 已处理 ${detail.processed_records} 条记录`;
  return `${text} · 已用 ${Math.max(0, Math.floor(elapsed))} 秒`;
}
class ProgressDisplay {
  constructor(vscode, status, dependencies = {}) {
    this.vscode = vscode; this.status = status; this.read = dependencies.read || readProgressJson;
    this.readPrivate = dependencies.readPrivate; this.runWindows = dependencies.runWindows;
    this.now = dependencies.now || Date.now; this.setInterval = dependencies.setInterval || setInterval;
    this.clearInterval = dependencies.clearInterval || clearInterval; this.paths = new Map(); this.job = null; this.lines = [];
    try { this.output = vscode.window.createOutputChannel('Codex History Sync'); } catch { this.output = null; }
  }
  async resolveFile(config) {
    const key = JSON.stringify([config.progressFile, config.windowsController, config.windowsConfig, config.previewHelper]);
    if (!this.paths.has(key)) this.paths.set(key, (async () => {
      if (config.progressFile) return linuxPath(config.progressFile);
      try {
        const installed = await this.readPrivate(path.join(os.homedir(), '.config', 'codex-history-sync-live', 'launcher.json'));
        const setup = installed?.setupSettings;
        if (installed?.owner === 'krfnsa-lab.codex-history-live-sync' && installed.wrappers_installed === true && setup?.windowsController === config.windowsController && setup?.windowsConfig === config.windowsConfig && linuxPath(setup.progressFile)) return setup.progressFile;
      } catch { /* Optional older-installation fallback below. */ }
      try { return linuxPath((await this.runWindows(config, 'progress-path'))?.progress_file); } catch { return null; }
    })());
    return this.paths.get(key);
  }
  begin(config, manual = false, phase = 'checking') {
    this.stop();
    let resolve;
    const job = { started: this.now(), stageStarted: this.now(), phase, config, done: new Promise(done => { resolve = done; }), resolve: () => resolve(),
      bound: null, snapshot: null, detail: null, externalOnly: false, closed: false, reading: false, fingerprint: '', notification: null };
    this.job = job;
    this.append('开始本轮检查'); this.paint(job);
    // Resolve a path once per configuration, never launch a process on each tick.
    void this.resolveFile(config).then(file => { if (this.job === job) { job.file = file; void this.tick(job); } }).catch(() => {});
    job.timer = this.setInterval(() => { void this.tick(job); }, 1000); job.timer?.unref?.();
    if (manual) this.show(false);
  }
  local(event) {
    const job = this.job; if (!job || !Object.hasOwn(phases, event?.phase)) return;
    job.phase = event.phase; job.expectedRequest = event.requestId || null; job.snapshot = null; job.detail = null;
    if (event.newOperation) { job.stageStarted = this.now(); job.bound = null; job.snapshot = null; job.detail = null; }
    this.paint(job);
  }
  append(line) {
    this.lines.push(line);
    const trim = this.lines.length > 500;
    if (trim) this.lines.splice(0, 100);
    try {
      if (trim) {
        if (typeof this.output?.replace === 'function') this.output.replace(this.lines.join('\n') + '\n');
        else { this.output?.clear?.(); for (const kept of this.lines) this.output?.appendLine(kept); }
      } else this.output?.appendLine(line);
    } catch { /* Display failures do not affect synchronization. */ }
  }
  paint(job) {
    if (this.job !== job || job.closed) return;
    let elapsed = Math.max(0, (this.now() - job.started) / 1000);
    let message = `${phases[job.phase]} · 已用 ${Math.floor(elapsed)} 秒`;
    if (job.snapshot) {
      elapsed = job.snapshot.elapsed_seconds + Math.max(0, (this.now() - Date.parse(job.snapshot.updated_at)) / 1000);
      const stale = this.now() - latestUpdate(job.snapshot, job.detail) > 30000;
      message = stale && job.snapshot.status === 'running' ? `等待进度更新，尚未确认同步结果 · 已用 ${Math.floor(elapsed)} 秒` : render(job.snapshot, job.detail, elapsed);
    }
    let compact = message;
    if (job.snapshot && this.now() - latestUpdate(job.snapshot, job.detail) <= 30000) {
      compact = phases[(job.snapshot.status === 'running' && job.detail?.status === 'running' && job.detail.phase) || job.snapshot.phase];
      if (job.snapshot.status === 'waiting' && Object.hasOwn(reasons, job.snapshot.reason)) compact += '：' + reasons[job.snapshot.reason];
      if (job.snapshot.current_index > 0) compact += ` · 第 ${job.snapshot.current_index} 条`;
      else if (job.snapshot.total_chats != null && job.snapshot.processed_chats != null) compact += ` · ${job.snapshot.processed_chats}/${job.snapshot.total_chats} 条`;
      compact += ` · ${Math.floor(elapsed)} 秒`;
    }
    try { this.status.text = '$(sync~spin) Codex：' + compact; this.status.tooltip = message + '\n点击查看实时进度和详情'; } catch { /* Optional UI. */ }
    try { job.reporter?.report({ message }); } catch { /* A failed notification does not block other progress sinks. */ }
    const fingerprint = message.replace(/ · 已用 \d+ 秒$/, '');
    if (fingerprint !== job.fingerprint) { job.fingerprint = fingerprint; this.append(fingerprint); }
  }
  async tick(job) {
    if (this.job !== job || job.closed || job.reading) return;
    job.reading = true;
    try {
      if (job.file) {
        const snapshot = validateSnapshot(await this.read(job.file), this.now());
        if (this.job !== job || job.closed) return;
        if (snapshot) {
          let nativeDetail = null;
          try { nativeDetail = validateDetail(await this.read(path.join(path.dirname(job.file), 'progress-detail.json')), snapshot, this.now()); } catch { /* A root snapshot can stand alone. */ }
          if (this.job !== job || job.closed) return;
          if (!job.bound) {
            // A terminal left by yesterday's run cannot complete today's operation.
            const recent = this.now() - latestUpdate(snapshot, nativeDetail) <= 30000;
            const startedHere = job.expectedRequest && snapshot.request_id === job.expectedRequest;
            if (snapshot.status === 'running' && recent && (job.externalOnly || startedHere)) job.bound = snapshot.run_id;
          }
          if (job.bound === snapshot.run_id && (job.externalOnly || snapshot.request_id === job.expectedRequest)) {
            job.snapshot = snapshot; job.detail = nativeDetail;
            if (this.job !== job || job.closed) return;
            if (job.externalOnly && snapshot.status !== 'running') {
              this.paint(job); this.append(snapshot.status === 'completed' ? '定时任务本轮处理结束；已同步和暂缓数量见上方。' : snapshot.status === 'waiting' ? '定时任务等待安全同步条件，尚未确认全部同步。' : '定时任务未完成，请检查恢复记录。');
              try { this.status.text = `${snapshot.status === 'error' ? '$(warning)' : snapshot.status === 'waiting' ? '$(clock)' : '$(history)'} Codex：${render(snapshot, job.detail, snapshot.elapsed_seconds)}`; } catch {}
              this.stop(); return;
            }
          }
        }
      }
    } catch { /* Missing, replaced, or invalid progress never changes the real operation. */ }
    finally { job.reading = false; }
    if (this.job !== job) return;
    if (job.externalOnly && (this.now() > job.externalDeadline || (!job.bound && this.now() > job.discoveryDeadline))) {
      this.append('无法继续确认定时任务进度；未报告同步成功。'); this.stop();
      try { this.status.text = '$(clock) Codex：等待下次检查'; this.status.tooltip = '暂时无法确认后台同步结果，请稍后重新检查。'; } catch {}
      return;
    }
    this.paint(job);
  }
  show(details = true) {
    if (details) { try { this.output?.show(true); } catch {} }
    const job = this.job; if (!job || job.notification) return;
    try {
      const promise = this.vscode.window.withProgress({ location: this.vscode.ProgressLocation.Notification, title: 'Codex 聊天记录同步（点击状态栏查看详情）', cancellable: false }, reporter => {
        job.reporter = reporter; this.paint(job); return job.done;
      });
      job.notification = Promise.resolve(promise).catch(() => {}).finally(() => { job.reporter = null; });
    } catch { /* The underlying operation remains independent of its notification. */ }
  }
  finish(result, failed = false) {
    const job = this.job; if (!job) return;
    if (!failed && result?.reason === 'controller_running') {
      job.externalOnly = true; job.bound = null; job.snapshot = null; job.detail = null; job.phase = 'controller_running';
      job.discoveryDeadline = this.now() + 15000; job.externalDeadline = this.now() + Math.max(180, job.config.controllerTimeoutSeconds || 600) * 1000;
      void this.tick(job); return;
    }
    const label = failed ? '本轮未完成，请检查同步状态。' : result?.kind === 'synced' ? '本轮同步完成。' : '本轮检查结束；检查完成不代表所有记录已同步。';
    this.append(label); this.stop();
  }
  stop() {
    const job = this.job; if (!job) return;
    job.closed = true; if (job.timer) this.clearInterval(job.timer); job.resolve(); this.job = null;
  }
  dispose() { this.stop(); try { this.output?.dispose(); } catch {} }
}
module.exports = { ProgressDisplay, validateSnapshot, validateDetail, readProgressJson, render, phases };
