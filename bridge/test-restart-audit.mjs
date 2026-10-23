// Audit reproduction only: disposable bridge, fake runtime, no production port.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { unixFetch } from './unix-fetch-test.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function alive(pid) {
  try {
    process.kill(pid, 0);
    if (process.platform === 'linux' && fs.readFileSync(`/proc/${pid}/stat`, 'utf8').split(') ')[1][0] === 'Z') return false;
    return true;
  } catch { return false; }
}
async function until(check) {
  const end = Date.now() + 8000;
  while (Date.now() < end) { if (await check()) return; await delay(30); }
  assert.fail('audit fixture deadline');
}
async function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = once(child, 'exit');
  child.kill('SIGKILL');
  await exited;
}

test('restarted bridge must not confirm cancellation while old worker survives', async t => {
  const dir = fs.mkdtempSync('/tmp/dsb-restart-audit-');
  const socketPath = path.join(dir, 'b.sock');
  const fetch = (url, opts) => unixFetch(socketPath, url, opts);
  fs.mkdirSync(path.join(dir, '.dsh-phone'));
  fs.writeFileSync(path.join(dir, '.dsh-phone', 'bridge-tasks.json'), JSON.stringify({
    version: 1, tasks: {}, active: null, latest: null, runtimeOpen: false }));
  const eventsFile = path.join(dir, 'events');
  const events = () => fs.existsSync(eventsFile)
    ? fs.readFileSync(eventsFile, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
  const base = 'http://localhost';
  const bridges = [];
  const abort = new AbortController();
  t.after(async () => {
    abort.abort();
    for (const child of bridges) await stop(child);
    const pids = [...new Set(events().flatMap(e => [e.pid, e.child]).filter(Boolean))];
    for (const pid of pids) {
      try { process.kill(-pid, 'SIGKILL'); } catch {}
      try { process.kill(pid, 'SIGKILL'); } catch {}
    }
    await until(() => pids.every(pid => !alive(pid)));
    fs.rmSync(dir, { recursive: true, force: true });
    console.log('OWNED_PROCESSES_CLEANED');
  });
  fs.chmodSync(path.join(here, 'fake-dsh-test.mjs'), 0o755);
  async function start() {
    // The fixture owns every PID and this path; production never unlinks blindly.
    if (fs.existsSync(socketPath)) fs.unlinkSync(socketPath);
    const child = spawn(process.execPath, [path.join(here, 'bridge.mjs')], {
      env: { PATH: process.env.PATH, HOME: dir, BRIDGE_HOST: '127.0.0.1',
        BRIDGE_SOCKET: socketPath, BRIDGE_DSH: path.join(here, 'fake-dsh-test.mjs'),
        BRIDGE_DIR: path.join(dir, 'shots'), BRIDGE_MEM_DIR: path.join(dir, 'memory'),
        BRIDGE_COMPACT_EVERY: '0', TEST_EVENTS: eventsFile }, stdio: 'ignore'
    });
    bridges.push(child);
    await until(async () => {
      assert.equal(child.exitCode, null);
      try { return (await fetch(base + '/health')).ok; } catch { return false; }
    });
    return child;
  }
  const first = await start();
  let pending = fetch(base + '/do', { method: 'POST', signal: abort.signal,
    body: JSON.stringify({ goal: 'hang', task_id: '11111111-1111-4111-8111-111111111111' }) }).catch(() => null);
  await until(() => events().some(e => e.text === 'hang'));
  const stoppedPid = events().find(e => e.event === 'start').pid;
  const stopped = await (await fetch(base + '/cancel', { method: 'POST', body: JSON.stringify({
    task_id: '11111111-1111-4111-8111-111111111111' }) })).json();
  assert.equal(stopped.ok, true, JSON.stringify(stopped));
  assert.equal(stopped.stop_confirmed, true);
  assert.equal(alive(stoppedPid), false);
  await pending;
  const duplicate = await (await fetch(base + '/do', { method: 'POST', body: JSON.stringify({
    task_id: '11111111-1111-4111-8111-111111111111', goal: 'must-not-run' }) })).json();
  assert.equal(duplicate.ok, false);
  assert.match(duplicate.error, /duplicate/);
  pending = fetch(base + '/do', { method: 'POST', signal: abort.signal,
    body: JSON.stringify({ goal: 'hang', task_id: '22222222-2222-4222-8222-222222222222' }) }).catch(() => null);
  await until(() => events().filter(e => e.text === 'hang').length === 2);
  const oldPid = events().filter(e => e.event === 'start').at(-1).pid;
  const stale = await (await fetch(base + '/cancel', { method: 'POST', body: JSON.stringify({
    task_id: '11111111-1111-4111-8111-111111111111' }) })).json();
  assert.equal(stale.ok, true);
  assert.equal(alive(oldPid), true, 'old cancellation must not kill the new runtime');
  await stop(first);
  await pending;
  const second = await start();
  const health = await (await fetch(base + '/health')).json();
  const cancel = await (await fetch(base + '/cancel', { method: 'POST',
    body: JSON.stringify({ task_id: '22222222-2222-4222-8222-222222222222' }) })).json();
  const orphanAlive = alive(oldPid);
  console.log(JSON.stringify({ bridgeRestarted: first.pid !== second.pid,
    runtimeAlive: health.runtimeAlive, cancel, oldWorkerAlive: orphanAlive }));
  assert.equal(cancel.ok && orphanAlive, false,
    'cancel success must not clear the App recovery barrier while an old runtime survives');
  assert.equal(cancel.ok, false);
  assert.equal(cancel.stop_confirmed, false);
  const replay = await (await fetch(base + '/do', { method: 'POST', body: JSON.stringify({
    task_id: '33333333-3333-4333-8333-333333333333', goal: 'must-not-run' }) })).json();
  assert.equal(replay.ok, false);
  assert.match(replay.error, /unknown/);
  assert.equal(events().some(e => e.text === 'must-not-run'), false);
});
