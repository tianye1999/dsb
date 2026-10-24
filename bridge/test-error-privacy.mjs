import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { unixFetch } from './unix-fetch-test.mjs';

const source = fs.readFileSync(new URL('./bridge.mjs', import.meta.url), 'utf8');

test('unexpected request errors return a stable code, not exception details', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-errors-'));
  const socket = path.join(dir, 'b.sock');
  fs.mkdirSync(path.join(dir, '.dsh-phone'));
  fs.writeFileSync(path.join(dir, '.dsh-phone/bridge-tasks.json'), JSON.stringify({
    version: 1, tasks: {}, active: null, latest: null, runtimeOpen: false,
  }));
  const child = spawn(process.execPath, [new URL('./bridge.mjs', import.meta.url).pathname], {
    env: { PATH: process.env.PATH, HOME: dir, BRIDGE_SOCKET: socket, BRIDGE_DIR: path.join(dir, 'shots'),
      BRIDGE_AUDIT_PATH: path.join(dir, 'audit.jsonl'),
      BRIDGE_DSH: path.join(dir, 'DO_NOT_LOG_TASK-executable') },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '';
  child.stdout.on('data', d => { logs += d; });
  child.stderr.on('data', d => { logs += d; });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) {
      const done = once(child, 'exit'); child.kill('SIGKILL'); await done;
    }
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const fetch = (url, opts) => unixFetch(socket, url, opts);
  const deadline = Date.now() + 5000;
  while (true) {
    try { if ((await fetch('http://localhost/health')).ok) break; } catch {}
    assert.ok(Date.now() < deadline && child.exitCode === null, logs);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  // A wrong-typed value causes a real handler exception without starting a brain or calling a device.
  const response = await fetch('http://localhost/ask', { method: 'POST', body: JSON.stringify({
    task_id: '11111111-1111-4111-8111-111111111111', prompt: { secretTask: 'DO_NOT_LOG_TASK' },
  }) });
  assert.equal(response.status, 500);
  const body = await response.json();
  assert.equal(body.error, 'internal_error');
  assert.equal(body.transport_settled, false);
  assert.doesNotMatch(JSON.stringify(body), /TypeError|bridge\.mjs|\.trim|DO_NOT_LOG_TASK/);
  const cancel = await fetch('http://localhost/cancel', { method: 'POST', body: JSON.stringify({
    task_id: '11111111-1111-4111-8111-111111111111',
  }) });
  assert.equal((await cancel.json()).stop_confirmed, true);
  const runtime = await fetch('http://localhost/do', { method: 'POST', body: JSON.stringify({
    task_id: '22222222-2222-4222-8222-222222222222', goal: 'DO_NOT_LOG_TASK',
  }) });
  const runtimeBody = await runtime.json();
  assert.equal(runtimeBody.error, 'runtime_error');
  assert.equal(runtimeBody.run_status, 'error');
  assert.equal(runtimeBody.goal_verified, false);
  assert.doesNotMatch(JSON.stringify(runtimeBody), /ENOENT|DO_NOT_LOG_TASK|bridge\.mjs/);
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.match(logs, /TypeError/);
  assert.doesNotMatch(logs, /DO_NOT_LOG_TASK/);
});

test('error logger retains stack frames without exception messages or task text', () => {
  const start = source.indexOf('function logError(');
  assert.ok(start >= 0, 'privacy-safe local error logger required');
  const end = source.indexOf('\n}\n', start) + 2;
  const logs = [];
  const context = { console: { error: (...args) => logs.push(args.join(' ')) } };
  vm.runInNewContext(source.slice(start, end) + `
    const e = new Error('DO_NOT_LOG_TASK\\n    at injected-task-text');
    logError('request', e);
    const formatted = new Error('DO_NOT_LOG_TASK');
    String(formatted.stack);
    logError('request', formatted);
    logError('request', 'DO_NOT_LOG_TASK');
  `, context, { filename: 'privacy-test.mjs' });
  assert.match(logs.join('\n'), /privacy-test\.mjs:\d+:\d+/);
  assert.doesNotMatch(logs.join('\n'), /DO_NOT_LOG_TASK|injected-task-text/);
});
