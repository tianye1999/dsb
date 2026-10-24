import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { ActiveTask } from './active-task.mjs';

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';

test('generation changes between tasks; closed and restart states never remain active', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-active-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'active-task.json');
  const first = new ActiveTask(file);
  const a = first.activate(A);
  assert.equal(JSON.parse(fs.readFileSync(file)).task_id, A);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  first.close(A, a.generation);
  assert.equal(JSON.parse(fs.readFileSync(file)).state, 'closed');
  const b = first.activate(B);
  assert.notEqual(a.generation, b.generation);
  const restarted = new ActiveTask(file);
  assert.equal(restarted.snapshot().state, 'active');
  restarted.recover({ runtimeOpen: false, active: null, tasks: { [B]: 'cancelled' } });
  assert.equal(restarted.snapshot().state, 'closed');
});

test('restart preserves uncertain owner, but stale terminal generation is recoverable', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-active-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'active-task.json');
  const context = new ActiveTask(file);
  const old = context.activate(A);
  const restarted = new ActiveTask(file);
  restarted.recover({ runtimeOpen: true, active: A, tasks: { [A]: 'running' } });
  assert.equal(restarted.snapshot().state, 'active');
  const next = restarted.activate(B, { active: B, tasks: { [A]: 'reply_received', [B]: 'running' } });
  assert.notEqual(next.generation, old.generation);
});

test('damaged or inaccessible context does not throw at bridge construction', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-active-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'active-task.json');
  fs.writeFileSync(file, '{bad', { mode: 0o600 });
  const damaged = new ActiveTask(file);
  assert.equal(damaged.snapshot(), null);
  assert.equal(damaged.errorCode, 'active_task_unavailable');
  fs.chmodSync(file, 0o644);
  assert.equal(new ActiveTask(file).errorCode, 'active_task_unavailable');
  const missingParent = new ActiveTask(path.join(dir, 'missing', 'active-task.json'));
  assert.equal(missingParent.snapshot(), null);
  assert.equal(missingParent.errorCode, 'active_task_unavailable');
});

test('wrong task or generation cannot close another active task', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-active-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const context = new ActiveTask(path.join(dir, 'active-task.json'));
  const a = context.activate(A);
  assert.throws(() => context.activate(B), /active_task_busy/);
  assert.throws(() => context.close(B, a.generation), /active_task_mismatch/);
  assert.throws(() => context.close(A, 'bad'), /active_task_mismatch/);
  assert.equal(context.snapshot().state, 'active');
});

test('a stale owner cannot seal a newer generation or a different owner', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-active-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'active-task.json');
  const old = new ActiveTask(file);
  const web = old.activate(A, undefined, 'web');
  const stale = new ActiveTask(file);
  old.close(A, web.generation, 'web');
  const native = new ActiveTask(file).activate(B);
  assert.throws(() => stale.close(A, web.generation, 'web'), /active_task_mismatch/);
  stale.closeConfirmed(A);
  assert.equal(new ActiveTask(file).snapshot().generation, native.generation);
});

test('corrupt active state is never overwritten and the atomic lock fails closed', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-active-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'active-task.json');
  fs.writeFileSync(file, '{bad', { mode: 0o600 });
  assert.throws(() => new ActiveTask(file).activate(A), /active_task_unavailable/);
  assert.equal(fs.readFileSync(file, 'utf8'), '{bad');
  fs.unlinkSync(file);
  fs.mkdirSync(file + '.lock', { mode: 0o700 });
  assert.throws(() => new ActiveTask(file).activate(A), /active_task_unavailable/);
  assert.equal(fs.existsSync(file), false);
});
