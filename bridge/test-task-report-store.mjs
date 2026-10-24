import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { TaskReportStore, failureStatus, reportSummary } from './task-report-store.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';

test('unverified stop cannot manufacture a terminal report', () => {
  assert.equal(failureStatus(A, { tasks: { [A]: 'running' } }, A), null);
  assert.equal(failureStatus(A, { tasks: { [A]: 'cancelled' } }, A), 'cancelled');
  assert.equal(failureStatus(A, { tasks: { [A]: 'running' } }, null), 'error');
});

test('summary uses full counts when the refused event falls beyond the timeline limit', () => {
  const summary = reportSummary({
    counts: { dispatched_actions: 1000, gate_refusals_by_reason: { G3_EDGE: 2 },
      command_hook_refusals: 1, command_hook_shadow_would_block: null,
      typesafe_unavailable_warnings: null, handoff_points: 0 },
    timeline: [{ result: 'dispatched' }],
    coverage: { lifecycle_valid: true, attribution_complete: true,
      timeline_truncated: true, command_hook_linked: true },
  });
  assert.equal(summary.refusals, 3);
  assert.equal(summary.primary_reason_code, 'G3_EDGE');
  assert.equal(summary.command_hook_linked, true);
  assert.match(summary.lines[1], /拒绝了 3 次/);
  assert.equal(summary.evidence_incomplete, true);
});

test('private report contains only allowlisted content and rejects cross-task token', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const audit = path.join(dir, 'audit.jsonl');
  const ledger = path.join(dir, 'ledger.json');
  const store = new TaskReportStore({ dir: path.join(dir, 'reports'), audit,
    script: path.join(here, '../agent/task_report.py'), python: 'python3' });
  const offset = store.lifecycle(A, 'report_task_start');
  fs.appendFileSync(audit, JSON.stringify({ kind: 'tap', task_id: A, allowed: false,
    reason_code: 'G1_NO_SHOT', reason: 'PRIVATE_CHAT_ORDER_782198',
    note: 'PRIVATE_CHAT_ORDER_782198', ts: Date.now() / 1000 }) + '\n');
  store.lifecycle(A, 'report_task_end', 'reply_received');
  fs.writeFileSync(ledger, JSON.stringify({ version: 1, tasks: { [A]: 'reply_received' } }));
  const saved = await store.buildAndSave(A, ledger, offset);
  assert.equal(saved.summary.actions, 0);
  assert.equal(saved.summary.refusals, 1);
  assert.equal(saved.summary.evidence_incomplete, true);
  assert.equal(saved.summary.lines.length, 3);
  assert.equal(store.get(A, saved.token, 'json')?.counts.gate_refusals_by_reason.G1_NO_SHOT, 1);
  assert.equal(store.get(A, saved.token, 'json')?.task.outcome, 'unknown');
  assert.equal(store.get(A, saved.token, 'markdown').includes('执行报告'), true);
  assert.equal(store.get(B, saved.token, 'json'), null);
  assert.equal(store.get(A, '0'.repeat(64), 'json'), null);
  assert.ok(!fs.readFileSync(path.join(dir, 'reports', A + '.json'), 'utf8').includes('PRIVATE_CHAT_ORDER'));
  await assert.rejects(store.buildAndSave(A, ledger, offset), /EEXIST/);
});

test('retention keeps only newest 50 and no file older than 30 days', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-prune-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const reports = path.join(dir, 'reports');
  const store = new TaskReportStore({ dir: reports, audit: path.join(dir, 'audit'),
    script: path.join(here, '../agent/task_report.py'), python: 'python3' });
  fs.mkdirSync(reports, { mode: 0o700 });
  for (let n = 0; n < 52; n++) {
    const id = `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
    const file = path.join(reports, id + '.json');
    fs.writeFileSync(file, '{}', { mode: 0o600 });
    const age = n === 0 ? 31 * 86400 : 52 - n;
    fs.utimesSync(file, new Date(Date.now() - age * 1000), new Date(Date.now() - age * 1000));
  }
  const leftover = path.join(reports, '.aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.md');
  fs.writeFileSync(leftover, 'PRIVATE_SYNTHETIC_REPORT', { mode: 0o600 });
  fs.utimesSync(leftover, new Date(Date.now() - 2 * 3600 * 1000), new Date(Date.now() - 2 * 3600 * 1000));
  store.prune();
  assert.equal(fs.readdirSync(reports).filter(name => name.endsWith('.json')).length, 50);
  assert.equal(fs.existsSync(path.join(reports, '00000000-0000-4000-8000-000000000000.json')), false);
  assert.equal(fs.existsSync(leftover), false);
});

test('retention tolerates another process removing a file after directory scan', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-prune-race-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const store = new TaskReportStore({ dir, audit: path.join(dir, 'audit'),
    script: path.join(here, '../agent/task_report.py') });
  const file = path.join(dir, A + '.json');
  fs.writeFileSync(file, '{}', { mode: 0o600 });
  const old = new Date(Date.now() - 31 * 86400 * 1000);
  fs.utimesSync(file, old, old);
  const unlink = fs.unlinkSync;
  fs.unlinkSync = value => {
    if (value === file) {
      unlink(value);
      throw Object.assign(new Error('gone'), { code: 'ENOENT' });
    }
    return unlink(value);
  };
  try { store.prune(); }
  finally { fs.unlinkSync = unlink; }
  assert.equal(fs.existsSync(file), false);
});

test('get is read-only; retention occurs only in prune or generation', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-readonly-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const audit = path.join(dir, 'audit.jsonl');
  const ledger = path.join(dir, 'ledger.json');
  const store = new TaskReportStore({ dir: path.join(dir, 'reports'), audit,
    script: path.join(here, '../agent/task_report.py') });
  const offset = store.lifecycle(A, 'report_task_start');
  store.lifecycle(A, 'report_task_end', 'reply_received');
  fs.writeFileSync(ledger, JSON.stringify({ tasks: { [A]: 'reply_received' } }));
  const saved = await store.buildAndSave(A, ledger, offset);
  const file = path.join(store.dir, A + '.json');
  const old = new Date(Date.now() - 31 * 86400 * 1000);
  fs.utimesSync(file, old, old);
  assert.ok(store.get(A, saved.token, 'json'));
  assert.equal(fs.existsSync(file), true);
  store.prune();
  assert.equal(fs.existsSync(file), false);
});

test('missing audit evidence cannot create a deceptively complete report', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-missing-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const ledger = path.join(dir, 'ledger.json');
  fs.writeFileSync(ledger, JSON.stringify({ version: 1, tasks: { [A]: 'reply_received' } }));
  const store = new TaskReportStore({ dir: path.join(dir, 'reports'), audit: path.join(dir, 'missing.jsonl'),
    script: path.join(here, '../agent/task_report.py'), python: 'python3' });
  await assert.rejects(store.buildAndSave(A, ledger, 0), /report_generation_unavailable/);
  assert.equal(store.get(A, '0'.repeat(64), 'json'), null);
});

test('report subprocess is asynchronous and cannot block the bridge event loop', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-async-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const script = path.join(dir, 'slow.py');
  fs.writeFileSync(script, 'import time\ntime.sleep(0.25)\nraise SystemExit(3)\n');
  const audit = path.join(dir, 'audit.jsonl');
  const ledger = path.join(dir, 'ledger.json');
  fs.writeFileSync(audit, '');
  fs.writeFileSync(ledger, '{}');
  const store = new TaskReportStore({ dir: path.join(dir, 'reports'), audit, script, python: 'python3' });
  let ticked = false;
  const timer = setTimeout(() => { ticked = true; }, 30);
  const building = store.buildAndSave(A, ledger, 0);
  await new Promise(resolve => setTimeout(resolve, 70));
  assert.equal(ticked, true);
  clearTimeout(timer);
  await assert.rejects(building, /report_generation_unavailable/);
});
