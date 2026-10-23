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
const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const C = '33333333-3333-4333-8333-333333333333';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await check()) return; await delay(20); }
  assert.fail('condition not met before deadline');
}
async function bounded(promise, ms = 5000) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('request remained pending')), ms);
  })]); } finally { clearTimeout(timer); }
}
function alive(pid) {
  try {
    process.kill(pid, 0);
    // Android can retain dead orphan processes until init reaps them.
    if (process.platform === 'linux' && fs.readFileSync(`/proc/${pid}/stat`, 'utf8').split(') ')[1][0] === 'Z') return false;
    return true;
  } catch { return false; }
}
async function fixture(t, extra = {}) {
  const dir = fs.mkdtempSync('/tmp/dsb-cancel-');
  const socketPath = path.join(dir, 'b.sock');
  const fetch = (url, opts) => unixFetch(socketPath, url, opts);
  fs.mkdirSync(path.join(dir, '.dsh-phone'));
  fs.writeFileSync(path.join(dir, '.dsh-phone/bridge-tasks.json'), JSON.stringify({
    version: 1, tasks: {}, active: null, latest: null, runtimeOpen: false }));
  const eventsFile = path.join(dir, 'events');
  const events = () => fs.existsSync(eventsFile) ? fs.readFileSync(eventsFile, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
  fs.chmodSync(path.join(here, 'fake-dsh-test.mjs'), 0o755);
  const child = spawn(process.execPath, [path.join(here, 'bridge.mjs')], {
    env: { PATH: process.env.PATH, HOME: dir, BRIDGE_SOCKET: socketPath,
      BRIDGE_DSH: path.join(here, 'fake-dsh-test.mjs'), BRIDGE_BRAIN: 'dsh',
      BRIDGE_DIR: path.join(dir, 'shots'), BRIDGE_MEM_DIR: path.join(dir, 'memory'),
      BRIDGE_COMPACT_EVERY: '0', TEST_EVENTS: eventsFile, ...extra },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = ''; child.stdout.on('data', d => logs += d); child.stderr.on('data', d => logs += d);
  const base = 'http://localhost';
  const post = (route, body = {}) => fetch(base + route, { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ task_id: A, ...body }),
    signal: AbortSignal.timeout(15000) }).then(r => r.json());
  t.after(async () => {
    const exited = child.exitCode === null ? once(child, 'exit') : Promise.resolve();
    child.kill('SIGKILL'); await exited;
    for (const e of events()) {
      const pid = e.child || e.pid;
      try { process.kill(pid, 'SIGKILL'); } catch {}
    }
    fs.rmSync(dir, { recursive: true, force: true });
  });
  await until(async () => {
    if (child.exitCode !== null) assert.fail(logs);
    try { return (await fetch(base + '/health')).ok; } catch { return false; }
  });
  return { post, events, logs: () => logs };
}

for (const text of ['任务已完成', '执行失败，需要人工处理']) {
  test('reply is not goal evidence: ' + text, async t => {
    const f = await fixture(t, { TEST_REPLY_TEXT: text });
    const result = await f.post('/do', { goal: 'synthetic-reply-only' });
    assert.equal(result.ok, true);
    assert.equal(result.text, text);
    assert.equal(result.run_status, 'reply_received');
    assert.equal(result.goal_status, 'unverified');
    assert.equal(result.goal_verified, false);
  });
}

test('benchmark marker never reaches a production runtime or task ledger admission', async t => {
  const f=await fixture(t);
  const rejected=await f.post('/ask',{transport_benchmark:'synthetic',prompt:'must-not-run'});
  assert.equal(rejected.ok,false);assert.match(rejected.error,/benchmark_requires_disposable_fixture/);
  assert.equal(f.events().length,0);
  assert.equal((await f.post('/do',{goal:'fresh'})).ok,true);
});
for (const text of ['', ' \n\t ']) {
  test('empty or whitespace reply is not success: ' + JSON.stringify(text), async t => {
    const f = await fixture(t, { TEST_REPLY_TEXT: text });
    const result = await f.post('/do', { goal: 'synthetic-empty-reply' });
    assert.equal(result.ok, false);
    assert.equal(result.run_status, 'empty_reply');
    assert.equal(result.goal_status, 'unverified');
    assert.equal(result.goal_verified, false);
    assert.equal(result.error, 'Empty assistant reply');
  });
}

test('cancel during initialization settles HTTP and terminates descendants', async t => {
  const f = await fixture(t, { TEST_INIT_HANG: '1' });
  const request = f.post('/do', { goal: 'hang' });
  await until(() => f.events().some(e => e.event === 'initialize'));
  const cancelled = await f.post('/cancel');
  assert.equal(cancelled.stop_confirmed, true);
  const result = await bounded(request);
  assert.equal(result.ok, false);
  assert.equal(result.run_status, 'error');
  assert.equal(result.goal_status, 'unverified');
  assert.equal(result.goal_verified, false);
  await until(() => f.events().every(e => !alive(e.child || e.pid)));
});

test('cancel settles active turn, discards queued work, and allows a new task', async t => {
  const f = await fixture(t);
  const first = f.post('/do', { goal: 'hang' });
  await until(() => f.events().some(e => e.text === 'hang'));
  const queued = f.post('/do', { task_id: B, goal: 'must-not-run' });
  await delay(150);
  await f.post('/cancel');
  const old = await bounded(Promise.all([first, queued]));
  assert.ok(old.every(r => r.ok === false));
  assert.ok(!f.events().some(e => e.text === 'must-not-run'));
  assert.equal((await bounded(f.post('/do', { task_id: C, goal: 'fresh' }))).ok, true);
  assert.equal(f.events().filter(e => e.text === 'fresh').length, 1);
});

test('cancel stops headless step and its subprocess', async t => {
  const f = await fixture(t);
  const request = f.post('/step', { goal: 'test-only' });
  await until(() => f.events().some(e => e.event === 'headless'));
  assert.equal((await f.post('/cancel')).stop_confirmed, true);
  assert.equal((await bounded(request)).ok, false);
  await until(() => f.events().every(e => !alive(e.child || e.pid)));
});

test('runtime exit during initialize reports failure and retains unknown if its child survives', async t => {
  const f = await fixture(t, { TEST_INIT_EXIT: '1' });
  const result = await bounded(f.post('/do', { goal: 'fresh' }));
  assert.equal(result.ok, false);
  await until(() => !alive(f.events().find(e => e.event === 'start').pid));
  assert.equal(alive(f.events().find(e => e.child).child), true);
  assert.equal((await f.post('/cancel')).ok, false);
  assert.equal((await f.post('/do', { task_id: B, goal: 'must-not-run' })).ok, false);
});

test('turn deadline fails, stays blocked, and needs explicit stop before new work', async t => {
  const f = await fixture(t, { BRIDGE_TURN_TIMEOUT_MS: '100' });
  const failed = await bounded(f.post('/do', { goal: 'hang' }));
  assert.equal(failed.ok, false);
  await until(() => f.events().every(e => !alive(e.child || e.pid)));
  assert.equal((await f.post('/do', { task_id: B, goal: 'must-not-run' })).ok, false);
  assert.equal((await f.post('/cancel')).stop_confirmed, true);
  assert.equal((await bounded(f.post('/do', { task_id: B, goal: 'fresh' }))).ok, true);
});

test('concurrent requests refuse overlap instead of silently queueing work', async t => {
  const f = await fixture(t);
  const results = await bounded(Promise.all(['first', 'second'].map((goal, i) => f.post('/do', { task_id: i ? B : A, goal }))));
  assert.equal(results.filter(r => r.ok).length, 1);
  assert.match(results.find(r => !r.ok).error, /busy/);
  assert.equal(f.events().filter(e => e.event === 'initialize').length, 1);
});

test('initialization deadline terminates its worker and reports failure', async t => {
  const f = await fixture(t, { TEST_INIT_HANG: '1', BRIDGE_TIMEOUT_MS: '1000' });
  assert.equal((await bounded(f.post('/do', { goal: 'hang' }))).ok, false);
  await until(() => f.events().every(e => !alive(e.child || e.pid)));
});

test('cancel stops headless ask and its subprocess', async t => {
  const f = await fixture(t);
  const request = f.post('/ask', { prompt: 'test-only' });
  await until(() => f.events().some(e => e.event === 'headless'));
  assert.equal((await f.post('/cancel')).stop_confirmed, true);
  assert.equal((await bounded(request)).ok, false);
  await until(() => f.events().every(e => !alive(e.child || e.pid)));
});

test('stop before admission tombstones its ID and allows a different new request', async t => {
  const f = await fixture(t);
  assert.equal((await f.post('/cancel')).stop_confirmed, true);
  assert.equal((await f.post('/cancel')).stop_confirmed, true);
  assert.equal((await f.post('/do', { goal: 'must-not-run' })).ok, false);
  assert.equal((await bounded(f.post('/do', { task_id: B, goal: 'fresh-after-noop' }))).ok, true);
});

test('an idle runtime exit with surviving descendants cannot admit a new task', async t => {
  const f = await fixture(t);
  assert.equal((await f.post('/do', { goal: 'reply-then-exit' })).ok, true);
  await until(() => !alive(f.events().find(e => e.event === 'start').pid));
  assert.equal(alive(f.events().find(e => e.child).child), true);
  const next = await f.post('/do', { task_id: B, goal: 'must-not-run' });
  assert.equal(next.ok, false);
  assert.match(next.error, /unknown/);
  assert.equal(f.events().some(e => e.text === 'must-not-run'), false);
});
