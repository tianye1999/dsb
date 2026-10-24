import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { ActiveTask } from '../../bridge/active-task.mjs';
import { TaskReportStore } from '../../bridge/task-report-store.mjs';
import { WebTurns } from './web-turns.mjs';
import { createWebReport, publicReport } from './index.js';

const here = path.dirname(fileURLToPath(import.meta.url));
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-web-report-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const audit = path.join(dir, 'audit.jsonl');
  const activeFile = path.join(dir, 'active-task.json');
  const ledger = path.join(dir, 'reports/web-turns.json');
  const store = new TaskReportStore({ dir: path.join(dir, 'reports'), audit,
    script: path.resolve(here, '../../agent/task_report.py') });
  const turns = new WebTurns({ file: ledger, activeFile, reportStore: store });
  const auditRow = row => fs.appendFileSync(audit, JSON.stringify({ ts: Date.now() / 1000, ...row }) + '\n');
  return { dir, audit, activeFile, ledger, store, turns, auditRow };
}

test('public report keeps operation failures separate from gate refusals', () => {
  const result = publicReport({counts: {dispatched_actions: 1,
    gate_refusals_by_reason: {OP_APP_NOT_FOUND: 1, G1_NO_SHOT: 1}, handoff_points: 0},
  coverage: {lifecycle_valid: true, attribution_complete: true, command_hook_linked: false},
  timeline: [{action_type: 'open', result: 'failed', reason_code: 'OP_APP_NOT_FOUND',
    note: 'PRIVATE_CHAT_ORDER'}, {action_type: 'open', result: 'opened'}]});
  assert.deepEqual(result.counts.gate_refusals_by_reason, {G1_NO_SHOT: 1});
  assert.equal(result.coverage.command_hook_linked, false);
  assert.equal(result.timeline[0].reason_code, 'OP_APP_NOT_FOUND');
  assert.equal(result.timeline[1].result, 'opened');
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE_CHAT_ORDER/);
});

test('normal web turn creates generation, seals it, and builds an unverified-goal report', async t => {
  const f = fixture(t);
  const row = f.turns.start('session-a', 1);
  assert.equal(new ActiveTask(f.activeFile).snapshot().task_id, row.task_id);
  f.auditRow({ kind: 'tap', task_id: row.task_id, task_generation: row.generation,
    allowed: false, reason_code: 'G2B_ACK', note: 'PRIVATE_CHAT_ORDER_123' });
  f.turns.end('session-a', 1, 'completed');
  assert.equal(new ActiveTask(f.activeFile).snapshot().state, 'closed');
  await f.turns.build(row);
  const report = f.store.readSaved(row.task_id).report;
  assert.equal(report.task.outcome, 'unknown');
  assert.equal(report.coverage.lifecycle_valid, true);
  assert.equal(report.coverage.attribution_complete, true);
  assert.equal(report.counts.gate_refusals_by_reason.G2B_ACK, 1);
  assert.doesNotMatch(JSON.stringify(publicReport(report)), /PRIVATE_CHAT_ORDER/);
});

test('overlapping sessions seal the slot and both reports disclose attribution conflict', async t => {
  const f = fixture(t);
  const first = f.turns.start('session-a', 1);
  const second = f.turns.start('session-b', 1);
  assert.equal(f.turns.find('session-a', 1).conflict, true);
  assert.equal(second.conflict, true);
  assert.equal(new ActiveTask(f.activeFile).snapshot().state, 'closed');
  f.auditRow({ kind: 'tap', task_context: 'unknown', allowed: true,
    dispatch_status: 'submitted', note: 'PRIVATE_SECOND_SESSION' });
  f.turns.end('session-b', 1, 'completed');
  f.turns.end('session-a', 1, 'completed');
  await Promise.all([f.turns.build(first), f.turns.build(second)]);
  for (const row of [first, second]) {
    const report = f.store.readSaved(row.task_id).report;
    assert.equal(report.coverage.attribution_complete, false);
    assert.equal(report.coverage.concurrent_turns, true);
    assert.equal(report.counts.dispatched_actions, 0);
  }
});

test('interrupted turn and restart close a stranded generation without claiming completion', async t => {
  const f = fixture(t);
  const interrupted = f.turns.start('session-a', 1);
  f.turns.end('session-a', 1, 'aborted');
  await f.turns.build(interrupted);
  assert.equal(f.store.readSaved(interrupted.task_id).report.task.outcome, 'unknown');
  assert.equal(f.store.readSaved(interrupted.task_id).report.task.transport_state, 'web_interrupted');
  const crashed = f.turns.start('session-b', 2);
  const restarted = new WebTurns({ file: f.ledger, activeFile: f.activeFile, reportStore: f.store });
  assert.equal(restarted.recover().length, 1);
  assert.equal(new ActiveTask(f.activeFile).snapshot().state, 'closed');
  await restarted.build(crashed);
  const report = f.store.readSaved(crashed.task_id).report;
  assert.equal(report.coverage.concurrent_turns, false);
  assert.equal(report.coverage.interrupted, true);
  assert.equal(report.task.outcome, 'unknown');
});

test('restart does not seal an orphaned web generation without ledger ownership', t => {
  const f = fixture(t);
  const owner = new ActiveTask(f.activeFile);
  const orphan = owner.activate('11111111-1111-4111-8111-111111111111', undefined, 'web');
  assert.equal(orphan.state, 'active');
  f.turns.recover();
  assert.equal(new ActiveTask(f.activeFile).snapshot().state, 'active');
});

test('missing turn/end is superseded and does not poison later activation', async t => {
  const f = fixture(t);
  const first = f.turns.start('session-a', 1);
  const second = f.turns.start('session-a', 2);
  assert.equal(f.turns.find('session-a', 1).status, 'web_interrupted');
  assert.equal(new ActiveTask(f.activeFile).snapshot().task_id, second.task_id);
  f.turns.end('session-a', 2, 'completed');
  await Promise.all([f.turns.build(first), f.turns.build(second)]);
  assert.equal(f.store.readSaved(first.task_id).report.coverage.interrupted, true);
  assert.equal(f.store.readSaved(second.task_id).report.coverage.attribution_complete, false);
});

test('watchdog retires a lost turn after configured maximum and permits fresh ownership', async t => {
  const f = fixture(t);
  let now = 1000;
  const turns = new WebTurns({ file: f.ledger, activeFile: f.activeFile, reportStore: f.store,
    maxTurnMs: 1000, now: () => now });
  const first = turns.start('session-a', 1);
  now = 2001;
  const second = turns.start('session-b', 1);
  assert.equal(turns.find('session-a', 1).status, 'web_interrupted');
  assert.equal(turns.find('session-b', 1).conflict, false);
  assert.equal(new ActiveTask(f.activeFile).snapshot().task_id, second.task_id);
  turns.end('session-b', 1, 'completed');
  await Promise.all([turns.build(first), turns.build(second)]);
  assert.equal(f.store.readSaved(first.task_id).report.coverage.concurrent_turns, false);
  assert.equal(f.store.readSaved(second.task_id).report.coverage.attribution_complete, true);
});

test('web never seals a native owner or overwrites damaged active state', t => {
  const f = fixture(t);
  const native = new ActiveTask(f.activeFile).activate('33333333-3333-4333-8333-333333333333');
  const web = f.turns.start('session-a', 1);
  assert.equal(web.conflict, true);
  assert.equal(new ActiveTask(f.activeFile).snapshot().generation, native.generation);
  f.turns.end('session-a', 1, 'completed');
  assert.equal(new ActiveTask(f.activeFile).snapshot().state, 'active');
  new ActiveTask(f.activeFile).close(native.task_id, native.generation);
  fs.writeFileSync(f.activeFile, '{bad', { mode: 0o600 });
  const bad = f.turns.start('session-b', 1);
  assert.equal(bad.conflict, true);
  assert.equal(fs.readFileSync(f.activeFile, 'utf8'), '{bad');
});

test('web overlap also marks the active native task attribution incomplete', async t => {
  const f = fixture(t);
  const nativeId = '33333333-3333-4333-8333-333333333333';
  const offset = f.store.lifecycle(nativeId, 'report_task_start');
  const native = new ActiveTask(f.activeFile).activate(nativeId);
  const web = f.turns.start('session-a', 1);
  assert.equal(web.conflict, true);
  f.turns.end('session-a', 1, 'completed');
  new ActiveTask(f.activeFile).close(nativeId, native.generation);
  f.store.lifecycle(nativeId, 'report_task_end', 'reply_received', { attributionComplete: true });
  const saved = await f.store.buildAndSave(nativeId, f.ledger, offset);
  assert.equal(saved.summary.evidence_incomplete, true);
  assert.equal(f.store.readSaved(nativeId).report.coverage.attribution_complete, false);
});

test('generation retries transient failures then records final failure', async t => {
  const f = fixture(t);
  const row = f.turns.start('session-a', 1);
  f.turns.end('session-a', 1, 'completed');
  const build = f.store.buildAndSave.bind(f.store);
  let attempts = 0;
  f.store.buildAndSave = (...args) => ++attempts < 3 ? Promise.reject(new Error('synthetic')) : build(...args);
  assert.equal(await f.turns.build(row), true);
  assert.equal(attempts, 3);
  assert.equal(row.report_state, 'ready');
  const failed = f.turns.start('session-b', 1);
  f.turns.end('session-b', 1, 'completed');
  f.store.buildAndSave = () => Promise.reject(new Error('synthetic'));
  assert.equal(await f.turns.build(failed), false);
  assert.equal(failed.report_state, 'failed');
});

test('authenticated route returns only fixed JSON and rejects mismatched session lookup', async t => {
  const f = fixture(t);
  const handlers = new Map();
  let route;
  const ctx = { on: (name, fn) => handlers.set(name, fn),
    effect: fn => fn(), webServer: { register: value => { route = value; return () => {}; } },
    connection: { requestRejection: req => req.headers.cookie === 'valid' ? undefined : 401 },
    logger: { warn: () => {} } };
  const { turns } = createWebReport(ctx, { store: f.store, ledger: f.ledger, activeFile: f.activeFile });
  const session = { id: 'session-a', header: { origin: 'user' } };
  handlers.get('session/event')(session, { type: 'turn/start', data: { turn: 1 } });
  const row = new WebTurns({ file: f.ledger, activeFile: f.activeFile, reportStore: f.store }).find('session-a', 1);
  f.auditRow({ kind: 'command_gate', task_id: row.task_id, decision: 'allow', mode: 'shadow',
    would_decision: 'deny', warning: 'typesafe_unavailable', command: 'PRIVATE_COMMAND' });
  handlers.get('session/event')(session, { type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } });
  for (let i = 0; i < 100 && !f.store.readSaved(row.task_id); i++)
    await new Promise(resolve => setTimeout(resolve, 20));
  assert.ok(f.store.readSaved(row.task_id));
  const call = (url, cookie = 'valid', method = 'GET') => {
    const response = { status: null, headers: null, body: null,
      writeHead(status, headers) { this.status = status; this.headers = headers; },
      end(body) { this.body = body; } };
    route.handler({ method, url, headers: { cookie } }, response);
    return response;
  };
  assert.equal(call('/dsb-report/api?session=session-a&turn=1', '').status, 401);
  assert.equal(call('/dsb-report/api?session=session-b&turn=1').status, 404);
  assert.equal(call('/dsb-report/api?session=session-a&turn=1', 'valid', 'POST').status, 405);
  const ok = call('/dsb-report/api?session=session-a&turn=1');
  assert.equal(ok.status, 200);
  assert.equal(ok.headers['Cache-Control'], 'no-store');
  assert.equal(JSON.parse(ok.body).report.counts.command_hook_shadow_would_block, 1);
  assert.equal(JSON.parse(ok.body).report.counts.typesafe_unavailable_warnings, 1);
  assert.doesNotMatch(ok.body, /PRIVATE_COMMAND|task_id|session-a|token|note|expect|vision|coordinates/);
  const failed = turns.start('session-a', 2);
  turns.end('session-a', 2, 'completed');
  failed.report_state = 'failed';
  failed.failed_at = Date.now();
  turns.flush();
  assert.equal(call('/dsb-report/api?session=session-a&turn=2').status, 503);
  failed.failed_at = Date.now() - 31000;
  assert.equal(call('/dsb-report/api?session=session-a&turn=2').status, 202);
  await turns.build(failed);
  ctx.connection = undefined;
  assert.equal(call('/dsb-report/api?session=session-a&turn=1').status, 401);
});

test('session disposal interrupts an open turn and seals its generation', async t => {
  const f = fixture(t);
  const handlers = new Map();
  const ctx = { on: (name, fn) => handlers.set(name, fn), effect: fn => fn(),
    webServer: { register: () => () => {} }, connection: { requestRejection: () => 401 },
    logger: { warn: () => {} } };
  createWebReport(ctx, { store: f.store, ledger: f.ledger, activeFile: f.activeFile });
  const session = { id: 'session-a', header: { origin: 'user' } };
  handlers.get('session/event')(session, { type: 'turn/start', data: {turn: 1} });
  handlers.get('session/disposed')(session);
  assert.equal(new ActiveTask(f.activeFile).snapshot().state, 'closed');
  assert.equal(new WebTurns({ file: f.ledger, activeFile: f.activeFile,
    reportStore: f.store }).find('session-a', 1).status, 'web_interrupted');
});
