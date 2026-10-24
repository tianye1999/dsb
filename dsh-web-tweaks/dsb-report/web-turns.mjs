import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const base = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const bridge = fs.existsSync(path.join(base, 'bridge/active-task.mjs')) ?
  path.join(base, 'bridge') : path.join(base, 'dsh-bridge');
const { ActiveTask } = await import(pathToFileURL(path.join(bridge, 'active-task.mjs')));

const SESSION = /^[A-Za-z0-9_-]{1,128}$/;
const RETAIN_MS = 30 * 86400 * 1000;

function save(file, value) {
  const tmp = file + '.' + crypto.randomUUID() + '.tmp';
  let fd;
  try {
    fd = fs.openSync(tmp, 'wx', 0o600);
    fs.writeFileSync(fd, JSON.stringify(value));
    fs.fsyncSync(fd);
    fs.closeSync(fd); fd = undefined;
    fs.renameSync(tmp, file);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    try { fs.unlinkSync(tmp); } catch {}
  }
}

export class WebTurns {
  constructor({ file, activeFile, reportStore, onError = () => {},
    maxTurnMs = Number(process.env.DSB_WEB_REPORT_MAX_TURN_MS) || 30 * 60 * 1000,
    now = () => Date.now() }) {
    this.file = file;
    this.activeFile = activeFile;
    this.reportStore = reportStore;
    this.onError = onError;
    this.now = now;
    this.maxTurnMs = Number.isSafeInteger(maxTurnMs) && maxTurnMs >= 1000 ? maxTurnMs : 30 * 60 * 1000;
    this.building = new Map();
    reportStore.ensurePrivate();
    if (fs.existsSync(file)) {
      const stat = fs.lstatSync(file);
      if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 || stat.size > 256 * 1024)
        throw new Error('web_turns_unavailable');
      const data = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (data.version !== 1 || !Array.isArray(data.turns) || data.turns.length > 4096)
        throw new Error('web_turns_unavailable');
      this.turns = data.turns;
    } else this.turns = [];
    this.flush();
  }

  flush() {
    const now = this.now();
    const ended = this.turns.filter(row => row.state !== 'running' && now - row.started_at <= RETAIN_MS)
      .sort((a, b) => b.started_at - a.started_at).slice(0, 50);
    this.turns = [...this.turns.filter(row => row.state === 'running'), ...ended];
    const tasks = Object.fromEntries(this.turns.map(row => [row.task_id,
      row.state === 'running' ? 'running' : row.status]));
    save(this.file, { version: 1, turns: this.turns, tasks });
  }

  find(sessionId, turn) {
    return this.turns.find(row => row.session_id === sessionId && row.turn === turn);
  }

  seal(taskId, generation) {
    const active = new ActiveTask(this.activeFile);
    if (active.errorCode) throw new Error(active.errorCode);
    const state = active.snapshot();
    if (state?.state === 'active' && state.owner === 'web' &&
        state.task_id === taskId && state.generation === generation) {
      active.close(taskId, generation, 'web');
      return true;
    }
    return false;
  }

  start(sessionId, turn) {
    if (!SESSION.test(sessionId) || !Number.isSafeInteger(turn) || turn < 0 || this.find(sessionId, turn))
      return null;
    this.watchdog();
    const taskId = crypto.randomUUID();
    const running = this.turns.filter(row => row.state === 'running');
    const superseded = running.filter(row => row.session_id === sessionId);
    for (const old of superseded) {
      old.interrupted = true;
      const ended = this.end(old.session_id, old.turn, 'aborted');
      if (ended) void this.build(ended);
    }
    const overlapping = running.filter(row => row.state === 'running');
    const offset = this.reportStore.lifecycle(taskId, 'report_task_start', null, { web: true });
    const row = { session_id: sessionId, turn, task_id: taskId, generation: null, audit_offset: offset,
      state: 'running', status: null, conflict: superseded.length > 0 || overlapping.length > 0,
      concurrent: overlapping.length > 0, interrupted: false, slot_owned: false,
      report_state: 'pending', started_at: this.now() };
    if (overlapping.length) {
      for (const old of overlapping) {
        old.conflict = true;
        old.concurrent = true;
        try { if (old.slot_owned && !this.seal(old.task_id, old.generation)) this.onError('active_task_mismatch'); }
        catch { this.onError('active_task_unavailable'); }
      }
    } else {
      try {
        const active = new ActiveTask(this.activeFile);
        if (active.errorCode) throw new Error(active.errorCode);
        const previous = active.snapshot();
        if (previous?.state === 'active' && previous.task_id !== taskId) {
          row.conflict = true;
          this.reportStore.attributionConflict(previous.task_id);
        }
        const activated = active.activate(taskId, undefined, 'web');
        row.generation = activated.generation;
        row.slot_owned = true;
      } catch { row.conflict = true; this.onError('active_task_unavailable'); }
    }
    this.turns.push(row);
    this.flush();
    return row;
  }

  end(sessionId, turn, reason = 'error') {
    const row = this.find(sessionId, turn);
    if (!row || row.state !== 'running') return null;
    if (row.slot_owned) {
      try { if (!this.seal(row.task_id, row.generation)) row.conflict = true; }
      catch { row.conflict = true; this.onError('active_task_unavailable'); }
    }
    row.status = reason === 'completed' ? 'web_completed' :
      reason === 'aborted' || reason === 'blocked' ? 'web_interrupted' : 'web_error';
    if (row.status !== 'web_completed') row.interrupted = true;
    row.state = 'ended';
    row.ended_at = this.now();
    this.reportStore.lifecycle(row.task_id, 'report_task_end', row.status,
      { web: true, attributionConflict: row.conflict, concurrentTurns: row.concurrent,
        interrupted: row.interrupted, attributionComplete: !row.conflict && !row.interrupted && row.slot_owned });
    this.flush();
    return row;
  }

  recover() {
    const running = this.turns.filter(row => row.state === 'running');
    for (const row of running) {
      row.interrupted = true;
      this.end(row.session_id, row.turn, 'aborted');
    }
    try {
      const active = new ActiveTask(this.activeFile);
      const state = active.snapshot();
      if (active.errorCode) throw new Error(active.errorCode);
      if (state?.state === 'active' && state.owner === 'web') {
        const owned = this.turns.some(row => row.task_id === state.task_id &&
          row.generation === state.generation);
        if (owned) active.close(state.task_id, state.generation, 'web');
        else this.onError('active_task_owner_unknown');
      }
    } catch { this.onError('active_task_unavailable'); }
    return this.turns.filter(row => row.state === 'ended' && row.report_state !== 'ready');
  }

  build(row) {
    if (this.building.has(row.task_id)) return this.building.get(row.task_id);
    const work = (async () => {
      if (this.reportStore.readSaved(row.task_id)) { row.report_state = 'ready'; this.flush(); return true; }
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          await this.reportStore.buildAndSave(row.task_id, this.file, row.audit_offset);
          row.report_state = 'ready';
          this.flush();
          return true;
        } catch {
          if (attempt < 2) await new Promise(resolve => setTimeout(resolve, 250 * 2 ** attempt));
        }
      }
      row.report_state = 'failed';
      row.failed_at = this.now();
      this.flush();
      this.onError('report_generation_unavailable');
      return false;
    })();
    this.building.set(row.task_id, work);
    work.finally(() => this.building.delete(row.task_id));
    return work;
  }

  watchdog() {
    const expired = this.turns.filter(row => row.state === 'running' &&
      this.now() - row.started_at >= this.maxTurnMs);
    for (const row of expired) {
      const ended = this.end(row.session_id, row.turn, 'aborted');
      if (ended) void this.build(ended);
    }
    return expired.length;
  }
}
