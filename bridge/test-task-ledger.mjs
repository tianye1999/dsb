import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { TaskLedger } from './task-ledger.mjs';

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
function fixture(t, capacity) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-ledger-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'tasks.json');
  fs.writeFileSync(file, JSON.stringify({ version: 1, tasks: {}, active: null, latest: null, runtimeOpen: false }));
  return { file, ledger: new TaskLedger(file, capacity) };
}
test('admission is durable before execution and duplicate IDs never replay', t => {
  const { file, ledger } = fixture(t);
  ledger.admit(A);
  assert.equal(JSON.parse(fs.readFileSync(file)).tasks[A], 'running');
  assert.throws(() => ledger.admit(A), /duplicate/);
  assert.throws(() => ledger.admit(B), /busy/);
});
test('bridge restart cannot confirm a still-owned old worker stopped', t => {
  const { file, ledger } = fixture(t);
  ledger.admit(A);
  const restarted = new TaskLedger(file);
  assert.equal(restarted.cancellation(A), 'unknown');
  assert.throws(() => restarted.confirmStopped(A), /unknown/);
  assert.throws(() => restarted.admit(B), /unknown/);
});
test('reply does not prove runtime death, including after restart', t => {
  const { file, ledger } = fixture(t);
  ledger.admit(A);
  ledger.reply(A);
  assert.equal(ledger.cancellation(A), 'owned');
  assert.throws(() => ledger.admit(A), /duplicate/);
  assert.equal(new TaskLedger(file).cancellation(A), 'unknown');
});
test('late cancellation cannot target a newer task', t => {
  const { ledger } = fixture(t);
  ledger.admit(A);
  ledger.reply(A);
  ledger.admit(B);
  assert.equal(ledger.cancellation(A), 'mismatch');
  assert.throws(() => ledger.confirmStopped(A), /mismatch/);
  assert.throws(() => ledger.reply(A), /mismatch/);
  assert.equal(ledger.cancellation(B), 'owned');
});
test('confirmed stops persist and old cancelled IDs do not affect new tasks', t => {
  const { file, ledger } = fixture(t);
  ledger.admit(A);
  ledger.confirmStopped(A);
  const restarted = new TaskLedger(file);
  assert.equal(restarted.cancellation(A), 'confirmed');
  restarted.admit(B);
  assert.equal(restarted.cancellation(A), 'confirmed');
  assert.equal(restarted.cancellation(B), 'owned');
});
test('missing task and malformed IDs never provide cancellation proof', t => {
  const { ledger } = fixture(t);
  assert.equal(ledger.cancellation(A), 'unknown');
  for (const value of ['', null, {}, '__proto__', '../x']) {
    assert.throws(() => ledger.admit(value), /task_id/);
    assert.equal(ledger.cancellation(value), 'unknown');
  }
});
test('bad persisted state prevents admission rather than resetting', t => {
  const { file } = fixture(t);
  for (const state of ['{', '{}', '{"version":1,"tasks":{},"runtimeOpen":false,"latest":null,"active":"bad"}']) {
    fs.writeFileSync(file, state);
    const broken = new TaskLedger(file);
    assert.throws(() => broken.admit(A), /unavailable/);
    assert.equal(broken.cancellation(A), 'unknown');
  }
});
test('capacity exhaustion fails closed instead of forgetting old IDs', t => {
  const { file, ledger } = fixture(t, 1);
  ledger.admit(A);
  ledger.confirmStopped(A);
  assert.throws(() => ledger.admit(B), /capacity/);
  assert.equal(JSON.parse(fs.readFileSync(file)).tasks[A], 'cancelled');
});
test('write failure poisons admission and cancellation proof', t => {
  const { file, ledger } = fixture(t);
  fs.unlinkSync(file);
  fs.mkdirSync(file);
  assert.throws(() => ledger.admit(A), /unavailable/);
  assert.throws(() => ledger.admit(B), /unavailable/);
  assert.equal(ledger.cancellation(A), 'unknown');
});
test('missing ledger never bootstraps over possibly surviving legacy workers', t => {
  const { file } = fixture(t);
  fs.unlinkSync(file);
  const ledger = new TaskLedger(file);
  assert.throws(() => ledger.admit(A), /unavailable/);
  assert.equal(ledger.cancellation(A), 'unknown');
  assert.equal(fs.existsSync(file), false);
});
test('stop before admission durably prevents the delayed request', t => {
  const { file, ledger } = fixture(t);
  ledger.cancelUnadmitted(A);
  assert.equal(ledger.cancellation(A), 'confirmed');
  assert.throws(() => ledger.admit(A), /duplicate/);
  assert.throws(() => new TaskLedger(file).admit(A), /duplicate/);
  ledger.admit(B);
  assert.equal(ledger.cancellation(A), 'confirmed');
  assert.equal(ledger.cancellation(B), 'owned');
});
test('stop-before-admission is forbidden when restart ownership is unknown', t => {
  const { file, ledger } = fixture(t);
  ledger.admit(A);
  const restarted = new TaskLedger(file);
  assert.throws(() => restarted.cancelUnadmitted(B), /unknown/);
  assert.equal(restarted.cancellation(B), 'unknown');
});
test('removing or replacing live state never resets admission or cancellation proof', t => {
  const { file, ledger } = fixture(t);
  ledger.admit(A);
  ledger.confirmStopped(A);
  fs.unlinkSync(file);
  assert.equal(ledger.cancellation(A), 'unknown');
  assert.throws(() => ledger.admit(B), /unavailable/);
  fs.writeFileSync(file, '{}');
  assert.throws(() => ledger.admit(B), /unavailable/);
});
test('file sync, rename and directory sync failures poison state even after rename', t => {
  for (const [method, failAt] of [['fsyncSync', 1], ['renameSync', 1], ['fsyncSync', 2]]) {
    const { file, ledger } = fixture(t);
    const original = fs[method];
    let calls = 0;
    fs[method] = (...args) => {
      if (++calls === failAt) throw new Error('injected persistence failure');
      return original(...args);
    };
    try { assert.throws(() => ledger.admit(A), /unavailable/); }
    finally { fs[method] = original; }
    assert.equal(ledger.cancellation(A), 'unknown');
    assert.throws(() => ledger.admit(B), /unavailable/);
    if (method === 'fsyncSync' && failAt === 2)
      assert.throws(() => new TaskLedger(file).admit(B), /unknown/);
  }
});
