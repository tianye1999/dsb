import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const TOKEN = /^[0-9a-f]{64}$/i;
const RETAIN_MS = 30 * 86400 * 1000;
const MAX_REPORTS = 50;
const MAX_REPORT_BYTES = 512 * 1024;
const TEMP_RETAIN_MS = 60 * 60 * 1000;

function writeAtomic(file, value) {
  const tmp = file + '.' + crypto.randomUUID() + '.tmp';
  let fd;
  let dirFd;
  try {
    fd = fs.openSync(tmp, 'wx', 0o600);
    fs.writeFileSync(fd, JSON.stringify(value));
    fs.fsyncSync(fd);
    fs.closeSync(fd); fd = undefined;
    fs.linkSync(tmp, file);
    fs.unlinkSync(tmp);
    dirFd = fs.openSync(path.dirname(file), 'r');
    fs.fsyncSync(dirFd);
    fs.closeSync(dirFd); dirFd = undefined;
  } finally {
    if (fd !== undefined) try { fs.closeSync(fd); } catch {}
    if (dirFd !== undefined) try { fs.closeSync(dirFd); } catch {}
    try { fs.unlinkSync(tmp); } catch {}
  }
}

export function reportSummary(report) {
  const counts = report.counts;
  const byReason = Object.entries(counts.gate_refusals_by_reason || {});
  const gateRefusals = byReason.reduce((sum, [, value]) => sum + value, 0);
  const hookRefusals = counts.command_hook_refusals || 0;
  const refusals = gateRefusals + hookRefusals;
  if (hookRefusals) byReason.push(['CHECK_GATE_OTHER', hookRefusals]);
  const [primaryCode] = byReason.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0] || [];
  const primary = report.timeline.find(event => event.reason_code === primaryCode)?.explanation ||
    (primaryCode === 'CHECK_GATE_OTHER' ? '命令钩子已拦下操作' : '安全检查已拦下操作');
  return {
    actions: counts.dispatched_actions, refusals, primary_reason_code: primaryCode || null,
    handoffs: counts.handoff_points,
    command_hook_linked: report.coverage.command_hook_linked === true,
    evidence_incomplete: !report.coverage.lifecycle_valid || !report.coverage.attribution_complete ||
      report.coverage.timeline_truncated,
    shadow_would_block: counts.command_hook_shadow_would_block,
    typesafe_unavailable_warnings: counts.typesafe_unavailable_warnings,
    lines: [
      `做了 ${counts.dispatched_actions} 个动作`,
      refusals ? `拒绝了 ${refusals} 次（主要原因：${primary}）` : '未见已关联的拒绝',
      `${counts.handoff_points} 处需要你接管`,
    ],
  };
}

export function incompleteSummary() {
  return { actions: null, refusals: null, primary_reason_code: null, handoffs: null,
    evidence_incomplete: true, command_hook_linked: false,
    shadow_would_block: null, typesafe_unavailable_warnings: null,
    lines: ['动作记录未核实', '拒绝记录未核实', '接管记录未核实'] };
}

export function failureStatus(taskId, ledger, stopPendingId) {
  if (ledger.tasks?.[taskId] === 'cancelled') return 'cancelled';
  return stopPendingId === taskId ? null : 'error';
}

export class TaskReportStore {
  constructor({ dir, audit, script, python = 'python3' }) {
    this.dir = dir;
    this.audit = audit;
    this.script = script;
    this.python = python;
  }

  ensurePrivate() {
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    const stat = fs.lstatSync(this.dir);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0)
      throw new Error('report_store_unavailable');
  }

  lifecycle(taskId, kind, status, options = {}) {
    if (!ID.test(taskId) || !['report_task_start', 'report_task_end'].includes(kind)) throw new Error('invalid lifecycle');
    let offset = null;
    if (kind === 'report_task_start') {
      try { offset = fs.statSync(this.audit).size; }
      catch (error) { if (error.code !== 'ENOENT') throw error; offset = 0; }
    }
    const row = { kind, task_id: taskId, ts: Date.now() / 1000 };
    if (kind === 'report_task_start') Object.assign(row, {
      evidence_kind: options.web ? 'web_turn_start' : 'bridge_admit', audit_offset: offset,
    });
    if (kind === 'report_task_end') {
      if (!['reply_received', 'empty_reply', 'cancelled', 'error',
        'web_completed', 'web_interrupted', 'web_error'].includes(status))
        throw new Error('invalid lifecycle status');
      Object.assign(row, { terminal_status: status,
        evidence_kind: options.web ? 'web_turn_end' :
          status === 'cancelled' ? 'stop_confirmed' : status === 'error' ? 'runtime_error' : 'bridge_reply',
        goal_verified: false });
      if (options.attributionComplete === true) row.attribution_complete = true;
      if (options.attributionConflict === true) row.attribution_conflict = true;
      if (options.concurrentTurns === true) row.concurrent_turns = true;
      if (options.interrupted === true) row.interrupted = true;
    }
    const fd = fs.openSync(this.audit, 'a', 0o600);
    try { fs.writeSync(fd, JSON.stringify(row) + '\n'); } finally { fs.closeSync(fd); }
    return offset;
  }

  attributionConflict(taskId) {
    if (!ID.test(taskId)) throw new Error('invalid task_id');
    const fd = fs.openSync(this.audit, 'a', 0o600);
    try { fs.writeSync(fd, JSON.stringify({ kind: 'report_attribution_conflict',
      task_id: taskId, ts: Date.now() / 1000 }) + '\n'); }
    finally { fs.closeSync(fd); }
  }

  prune() {
    this.ensurePrivate();
    const now = Date.now();
    const files = fs.readdirSync(this.dir).filter(name =>
      (ID.test(name.slice(0, -5)) && name.endsWith('.json')) ||
      /^\.[0-9a-f-]{36}\.(json|md)$/.test(name))
      .map(name => {
        try { return { name, stat: fs.lstatSync(path.join(this.dir, name)) }; }
        catch (error) { if (error.code === 'ENOENT') return null; throw error; }
      })
      .filter(Boolean)
      .filter(item => item.stat.isFile() && !item.stat.isSymbolicLink())
      .sort((a, b) => b.stat.mtimeMs - a.stat.mtimeMs);
    let reports = 0;
    files.forEach(item => {
      const temporary = item.name.startsWith('.');
      if ((!temporary && ++reports > MAX_REPORTS) ||
          now - item.stat.mtimeMs > (temporary ? TEMP_RETAIN_MS : RETAIN_MS))
        try { fs.unlinkSync(path.join(this.dir, item.name)); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
    });
  }

  async buildAndSave(taskId, ledger, auditOffset) {
    if (!ID.test(taskId) || !Number.isSafeInteger(auditOffset) || auditOffset < 0)
      throw new Error('invalid report input');
    this.ensurePrivate();
    const tmpJson = path.join(this.dir, '.' + crypto.randomUUID() + '.json');
    const tmpMarkdown = path.join(this.dir, '.' + crypto.randomUUID() + '.md');
    try {
      await new Promise((resolve, reject) => {
        const child = spawn(this.python, [this.script, '--audit', this.audit, '--ledger', ledger,
          '--task-id', taskId, '--audit-offset', String(auditOffset), '--json-out', tmpJson,
          '--markdown-out', tmpMarkdown], { stdio: 'ignore' });
        const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
        child.on('error', () => { clearTimeout(timer); reject(new Error('report_generation_unavailable')); });
        child.on('close', code => {
          clearTimeout(timer);
          if (code === 0) resolve();
          else reject(new Error('report_generation_unavailable'));
        });
      });
      if (fs.statSync(tmpJson).size > MAX_REPORT_BYTES || fs.statSync(tmpMarkdown).size > MAX_REPORT_BYTES)
        throw new Error('report_generation_oversized');
      const report = JSON.parse(fs.readFileSync(tmpJson, 'utf8'));
      const markdown = fs.readFileSync(tmpMarkdown, 'utf8');
      if (report.task?.task_id !== taskId || report.task.goal_verified !== false)
        throw new Error('report_generation_invalid');
      const token = crypto.randomBytes(32).toString('hex');
      writeAtomic(path.join(this.dir, taskId + '.json'), {
        created_at: Date.now(), token_hash: crypto.createHash('sha256').update(token).digest('hex'),
        report, markdown,
      });
      this.prune();
      return { summary: reportSummary(report), token };
    } finally {
      for (const file of [tmpJson, tmpMarkdown]) try { fs.unlinkSync(file); } catch {}
    }
  }

  get(taskId, token, format) {
    if (!ID.test(taskId) || !TOKEN.test(token) || !['json', 'markdown'].includes(format)) return null;
    try {
      const saved = this.readSaved(taskId);
      if (!saved || !TOKEN.test(saved.token_hash)) return null;
      const actual = crypto.createHash('sha256').update(token).digest();
      if (!crypto.timingSafeEqual(actual, Buffer.from(saved.token_hash, 'hex'))) return null;
      return format === 'json' ? saved.report : saved.markdown;
    } catch { return null; }
  }

  readSaved(taskId) {
    if (!ID.test(taskId)) return null;
    try {
      const file = path.join(this.dir, taskId + '.json');
      const stat = fs.lstatSync(file);
      if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 ||
          stat.size > 2 * MAX_REPORT_BYTES + 4096) return null;
      const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
      return saved.report?.task?.task_id === taskId ? saved : null;
    } catch { return null; }
  }
}
