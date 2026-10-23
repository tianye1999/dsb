import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const validId = id => typeof id === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
const terminalStates = new Set(['reply_received', 'cancelled']);

// Single bridge writer. This is recovery bookkeeping, not a same-UID trust boundary.
export class TaskLedger {
  constructor(file, capacity = 4096) {
    this.file = file;
    this.capacity = capacity;
    this.unavailable = false;
    this.state = { version: 1, tasks: {}, active: null, latest: null, runtimeOpen: false };
    try {
      if (fs.statSync(file).size > 1024 * 1024) throw new Error('oversized ledger');
      this.snapshot = fs.readFileSync(file, 'utf8');
      const value = JSON.parse(this.snapshot);
      const ids = Object.keys(value.tasks || {});
      if (value.version !== 1 || !value.tasks || typeof value.tasks !== 'object' ||
          Array.isArray(value.tasks) || typeof value.runtimeOpen !== 'boolean' ||
          ids.length > capacity || ids.some(id => !validId(id) ||
            !['running', ...terminalStates].includes(value.tasks[id])) ||
          (value.latest !== null && (!validId(value.latest) || !ids.includes(value.latest))) ||
          (value.active !== null && (value.active !== value.latest || value.tasks[value.active] !== 'running')) ||
          ids.some(id => value.tasks[id] === 'running' && id !== value.active) ||
          (value.runtimeOpen && value.latest === null) ||
          (!value.runtimeOpen && (value.active !== null || ids.some(id => value.tasks[id] !== 'cancelled')))) {
        throw new Error('invalid ledger');
      }
      this.state = value;
    } catch {
      // First install needs an explicit, audited initialization after legacy workers stop.
      this.unavailable = true;
    }
    this.restartUnknown = this.state.runtimeOpen;
  }

  assertAvailable() {
    if (!this.checkStorage()) throw new Error('task_state_unavailable');
    if (this.restartUnknown) throw new Error('task_state_unknown_after_restart');
  }

  checkStorage() {
    if (this.unavailable) return false;
    try {
      if (fs.statSync(this.file).size > 1024 * 1024 ||
          fs.readFileSync(this.file, 'utf8') !== this.snapshot) throw new Error('ledger changed');
      return true;
    } catch { this.unavailable = true; return false; }
  }

  persist(next) {
    let fd;
    let dir;
    const tmp = this.file + '.' + crypto.randomUUID() + '.tmp';
    try {
      const snapshot = JSON.stringify(next);
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fd = fs.openSync(tmp, 'wx', 0o600);
      fs.writeFileSync(fd, snapshot);
      fs.fsyncSync(fd);
      fs.closeSync(fd); fd = undefined;
      fs.renameSync(tmp, this.file);
      dir = fs.openSync(path.dirname(this.file), 'r');
      fs.fsyncSync(dir);
      fs.closeSync(dir); dir = undefined;
      this.state = next;
      this.snapshot = snapshot;
    } catch {
      this.unavailable = true;
      throw new Error('task_state_unavailable');
    } finally {
      if (fd !== undefined) try { fs.closeSync(fd); } catch {}
      if (dir !== undefined) try { fs.closeSync(dir); } catch {}
      try { fs.unlinkSync(tmp); } catch {}
    }
  }

  admit(id) {
    if (!validId(id)) throw new Error('valid task_id required');
    this.assertAvailable();
    if (Object.hasOwn(this.state.tasks, id)) throw new Error('duplicate task_id');
    if (this.state.active !== null) throw new Error('task_busy');
    if (Object.keys(this.state.tasks).length >= this.capacity) throw new Error('task_capacity_exhausted');
    this.persist({ ...this.state, tasks: { ...this.state.tasks, [id]: 'running' },
      active: id, latest: id, runtimeOpen: true });
  }

  reply(id) {
    this.assertAvailable();
    if (id !== this.state.active) throw new Error('task_mismatch');
    this.persist({ ...this.state, tasks: { ...this.state.tasks, [id]: 'reply_received' }, active: null });
  }

  cancellation(id) {
    if (!validId(id) || !this.checkStorage()) return 'unknown';
    if (this.state.tasks[id] === 'cancelled') return 'confirmed';
    if (this.restartUnknown || !Object.hasOwn(this.state.tasks, id)) return 'unknown';
    if (id !== this.state.latest) return 'mismatch';
    return 'owned';
  }

  cancelUnadmitted(id) {
    if (!validId(id)) throw new Error('valid task_id required');
    this.assertAvailable();
    if (Object.hasOwn(this.state.tasks, id)) throw new Error('duplicate task_id');
    if (Object.keys(this.state.tasks).length >= this.capacity) throw new Error('task_capacity_exhausted');
    this.persist({ ...this.state, tasks: { ...this.state.tasks, [id]: 'cancelled' } });
  }

  // Caller must first verify that all owned execution has exited, not merely send SIGKILL.
  confirmStopped(id) {
    this.assertAvailable();
    const status = this.cancellation(id);
    if (status === 'confirmed') return;
    if (status !== 'owned') throw new Error('task_' + status);
    const tasks = { ...this.state.tasks, [id]: 'cancelled' };
    // Prior replies shared this runtime, whose exit has now been verified as well.
    for (const key of Object.keys(tasks)) if (tasks[key] === 'reply_received') tasks[key] = 'cancelled';
    this.persist({ ...this.state, tasks, active: null, runtimeOpen: false });
  }
}
