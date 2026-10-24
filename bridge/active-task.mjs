import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const validId = value => typeof value === 'string' &&
  /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value);

export class ActiveTask {
  constructor(file) {
    this.file = file;
    this.state = null;
    this.errorCode = null;
    try { this.state = this.read(); }
    catch { this.errorCode = 'active_task_unavailable'; }
  }

  read() {
    let stat;
    try { stat = fs.lstatSync(this.file); }
    catch (error) {
      if (error.code === 'ENOENT' && fs.existsSync(path.dirname(this.file))) return null;
      throw new Error('active_task_unavailable');
    }
    if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o600)
      throw new Error('active_task_unavailable');
    try {
      const value = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (value.version !== 1 || !validId(value.task_id) || !validId(value.generation) ||
          !['active', 'closed'].includes(value.state) ||
          (value.owner !== undefined && value.owner !== 'web')) throw new Error();
      return value;
    } catch { throw new Error('active_task_unavailable'); }
  }

  snapshot() { return this.state; }

  locked(action) {
    const lock = this.file + '.lock';
    try { fs.mkdirSync(lock, { mode: 0o700 }); }
    catch { throw new Error('active_task_unavailable'); }
    try {
      const current = this.read();
      this.state = current;
      this.errorCode = null;
      return action(current);
    } catch (error) {
      if (error.message === 'active_task_unavailable') this.errorCode = error.message;
      throw error;
    } finally {
      try { fs.rmdirSync(lock); } catch { this.errorCode = 'active_task_unavailable'; }
    }
  }

  write(next) {
    const tmp = this.file + '.' + crypto.randomUUID() + '.tmp';
    let fd;
    let dirFd;
    try {
      fd = fs.openSync(tmp, 'wx', 0o600);
      fs.writeFileSync(fd, JSON.stringify(next));
      fs.fsyncSync(fd);
      fs.closeSync(fd); fd = undefined;
      fs.renameSync(tmp, this.file);
      dirFd = fs.openSync(path.dirname(this.file), 'r');
      fs.fsyncSync(dirFd);
      fs.closeSync(dirFd); dirFd = undefined;
      this.state = next;
      this.errorCode = null;
    } catch { throw new Error('active_task_unavailable'); }
    finally {
      if (fd !== undefined) try { fs.closeSync(fd); } catch {}
      if (dirFd !== undefined) try { fs.closeSync(dirFd); } catch {}
      try { fs.unlinkSync(tmp); } catch {}
    }
  }

  // A restarted bridge may only retire a confirmed, bridge-owned cancellation.
  recover(ledger) {
    this.locked(current => {
      if (current?.state === 'active' && current.owner !== 'web' && !ledger.runtimeOpen &&
          ledger.active === null && ledger.tasks?.[current.task_id] === 'cancelled')
        this.write({ ...current, state: 'closed' });
    });
  }

  activate(taskId, ledger, owner) {
    if (!validId(taskId)) throw new Error('valid task_id required');
    return this.locked(current => {
      if (current?.state === 'active' && current.owner !== 'web' &&
          current.task_id !== taskId && ledger?.active === taskId &&
          ['reply_received', 'cancelled'].includes(ledger.tasks?.[current.task_id])) {
        this.write({ ...current, state: 'closed' });
        current = this.state;
      }
      if (current?.state === 'active')
        throw new Error(current.owner === 'web' ? 'active_task_busy_web' : 'active_task_busy');
      const next = { version: 1, task_id: taskId, generation: crypto.randomUUID(),
        state: 'active', activated_at: Date.now() };
      if (owner === 'web') next.owner = 'web';
      this.write(next);
      return next;
    });
  }

  close(taskId, generation, owner) {
    return this.locked(current => {
      if (current?.task_id !== taskId || current.generation !== generation ||
          (owner === 'web' ? current.owner !== 'web' : current.owner === 'web'))
        throw new Error('active_task_mismatch');
      if (current.state === 'active') this.write({ ...current, state: 'closed' });
    });
  }

  closeConfirmed(taskId) {
    return this.locked(current => {
      if (current?.task_id === taskId && current.state === 'active' && current.owner !== 'web')
        this.write({ ...current, state: 'closed' });
    });
  }
}
