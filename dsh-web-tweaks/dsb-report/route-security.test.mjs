import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { pathToFileURL } from 'node:url';
import { TaskReportStore } from '../../bridge/task-report-store.mjs';
import { createWebReport } from './index.js';

const modules = process.env.DSH_TEST_MODULES && path.resolve(process.env.DSH_TEST_MODULES);
const options = { skip: modules ? false : 'DSH_TEST_MODULES is unset' };
let Context, connectionPlugin;
if (modules) {
  ({ Context } = await import(pathToFileURL(modules + '/@deepseek-ai/cordis/lib/index.js')));
  connectionPlugin = await import(pathToFileURL(modules + '/@deepseek-ai/dsh-client-connection/lib/index.js'));
}

function response() {
  return { status: 0, headers: {}, body: '',
    writeHead(status, headers = {}) { this.status = status; this.headers = headers; },
    end(body) { this.body = body || ''; } };
}

test('report route uses real DSH Connection cookie, Host and Origin checks', options, async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-auth-'));
  const root = new Context();
  t.after(async () => { await root.fiber.dispose(); fs.rmSync(dir, { recursive: true, force: true }); });
  let record;
  root.reflect.provide('credentials', { modifyRecord: async (_, mutate) => {
    record = await mutate(record) || record;
    return record;
  } });
  await connectionPlugin.apply(root, {});
  const store = new TaskReportStore({ dir: path.join(dir, 'reports'), audit: path.join(dir, 'audit.jsonl'),
    script: path.resolve('agent/task_report.py') });
  let route;
  const ctx = { connection: root.connection, logger: { warn() {} }, on() {},
    effect(fn) { return fn(); }, webServer: { register(value) { route = value; return () => {}; } } };
  const { turns } = createWebReport(ctx, { store, ledger: path.join(dir, 'reports/web-turns.json'),
    activeFile: path.join(dir, 'active-task.json') });
  const row = turns.start('synthetic-session', 1);
  fs.appendFileSync(store.audit, JSON.stringify({ kind: 'tap', task_id: row.task_id,
    allowed: false, reason_code: 'G2B_ACK', ts: Date.now() / 1000, note: 'PRIVATE_ORDER' }) + '\n');
  turns.end('synthetic-session', 1, 'completed');
  await turns.build(row);
  const request = (headers = {}, url = '/dsb-report/api?session=synthetic-session&turn=1') =>
    ({ method: 'GET', url, headers: { host: '127.0.0.1:3080', ...headers } });
  const call = headers => { const res = response(); route.handler(request(headers), res); return res; };
  function cookie(age = 0) {
    const now = Date.now;
    try {
      Date.now = () => now() - age;
      const launch = new URL(root.connection.authenticatedUrl('http://127.0.0.1:3080/'));
      const res = response();
      assert.equal(root.connection.authorizeIndex(request({}, launch.pathname + launch.search), res), false);
      assert.equal(res.status, 303);
      return res.headers['set-cookie'].split(';')[0];
    } finally { Date.now = now; }
  }
  assert.equal(call().status, 401);
  const valid = cookie();
  const ok = call({ cookie: valid });
  assert.equal(ok.status, 200);
  assert.equal(JSON.parse(ok.body).report.coverage.attribution_complete, true);
  assert.doesNotMatch(ok.body, /PRIVATE_ORDER|task_id/);
  assert.equal(call({ cookie: valid, 'sec-fetch-site': 'cross-site' }).status, 403);
  assert.equal(call({ cookie: valid, host: 'evil.invalid' }).status, 403);
  assert.equal(call({ cookie: valid, origin: 'https://evil.invalid' }).status, 403);
  assert.equal(call({ cookie: cookie(31 * 86400000) }).status, 401);
});
