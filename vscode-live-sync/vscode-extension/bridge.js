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
    : [config.windowsController, '--config', config.windowsConfig, '--once', '--json'];
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

class Coordinator {
  constructor(config, dependencies = {}) {
    validateConfig(config); this.config = config; this.running = false;
    this.discover = dependencies.discover || discoverInstances;
    this.request = dependencies.request || request;
    this.runWindows = dependencies.runWindows || runWindows;
    this.reload = dependencies.reload || (() => Promise.resolve());
    this.canReload = dependencies.canReload || (() => true);
    this.onFailure = dependencies.onFailure || (() => Promise.resolve());
    this.onDeferred = dependencies.onDeferred || (() => Promise.resolve());
    this.nextAttemptAt = dependencies.nextAttemptAt || (() => 0);
  }

  async check() {
    if (this.running) return { kind: 'waiting' };
    this.running = true;
    let lease = null; let pauseStarted = false; let operationFailed = false;
    const pauseFile = path.join(this.config.stateDir, 'pause.json');
    try {
      if (!this.canReload() || Date.now() < this.nextAttemptAt()) return { kind: 'waiting' };
      const preview = await this.runWindows(this.config, true);
      if (preview.eligible !== true || !Array.isArray(preview.candidate_ids) || preview.candidate_ids.length === 0) return { kind: 'current' };
      if (Number.isFinite(preview.controller_timeout_seconds)) this.config.controllerTimeoutSeconds = Math.max(this.config.controllerTimeoutSeconds || 600, preview.controller_timeout_seconds);
      const instances = await this.discover(this.config.stateDir);
      if (instances.length === 0) return { kind: 'unavailable' };
      if (instances.length !== 1) return { kind: 'waiting' };
      if (!Number.isInteger(instances[0].parent_pid) || instances[0].parent_pid !== process.pid) return { kind: 'waiting' };
      for (const instance of instances) {
        const status = await this.request(instance, 'status');
        if (!status.ok || !status.state || status.state.busy || status.state.starting || status.state.paused ||
            status.state.pending_request_count || status.state.server_request_count || status.state.active_turn_count || status.state.known_activity_count ||
            !Number.isFinite(status.state.idle_seconds) || status.state.idle_seconds < 60) return { kind: 'waiting' };
      }
      if (!this.canReload()) return { kind: 'waiting' };
      await secureDirectory(this.config.stateDir);
      const proposedLease = { protocol_version: 1, owner: 'vscode-companion', nonce: crypto.randomBytes(24).toString('hex'), pid: process.pid, created_at: new Date().toISOString() };
      let handle;
      try { handle = await fs.open(pauseFile, 'wx', 0o600); }
      catch (error) { if (error.code === 'EEXIST') return { kind: 'waiting' }; throw error; }
      lease = proposedLease;
      try { await handle.writeFile(JSON.stringify(lease)); await handle.sync(); } finally { await handle.close(); }
      if (!sameInstances(instances, await this.discover(this.config.stateDir))) return { kind: 'waiting' };
      for (const instance of instances) {
        let paused;
        try { paused = await this.request(instance, 'prepare_pause'); }
        catch (error) { pauseStarted = true; throw error; }
        if (!paused.ok && paused.error === 'busy') return { kind: 'waiting' };
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
        return { kind: 'waiting', result };
      }
      return { kind: 'synced', result };
    } catch (error) {
      operationFailed = true;
      throw error;
    } finally {
      // Never remove the synchronizer's pending marker. It belongs to its own recovery protocol.
      try {
        if (pauseStarted) {
          let recovery;
          try { recovery = await this.runWindows(this.config, 'recovery'); }
          catch { recovery = { safe_to_reload: false }; }
          const safe = recovery?.safe_to_reload === true;
          if (operationFailed || !safe) await this.onFailure();
          if (!safe) throw new Error('同步需要检查：已保留暂停保护。请确认没有未完成导入后再恢复 Codex。');
          if (!this.canReload()) {
            if (!operationFailed) await this.onFailure();
            throw new Error('编辑器有未保存内容或正在调试，已保留暂停保护。请保存并完成恢复检查后重载。');
          }
          try { if (lease) await this.removeLease(pauseFile, lease); }
          catch (error) { if (!operationFailed) await this.onFailure(); throw error; }
          lease = null;
          // Verify that no controller/importer or unfinished journal exists before permitting a fresh backend.
          try { await this.reload(); }
          catch (error) { if (!operationFailed) await this.onFailure(); throw error; }
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

module.exports = { Coordinator, validateConfig, discoverInstances, readPrivateJson, runWindows, sameInstances };
