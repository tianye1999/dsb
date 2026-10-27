import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { TaskReportStore } from '../../bridge/task-report-store.mjs';
import { createWebReport, EXPORT_ROUTE } from './index.js';

const here = path.dirname(fileURLToPath(import.meta.url));
function python(code, input) {
  const result = spawnSync('python3', ['-c', code], { input, maxBuffer: 24 * 1024 * 1024 });
  assert.equal(result.status, 0, result.stderr.toString());
  return result.stdout;
}
function response() {
  return { status: 0, headers: {}, body: null,
    writeHead(status, headers = {}) { this.status = status; this.headers = headers; },
    end(body) { this.body = body; } };
}

test('export route is authenticated, exact-turn, bounded, and delivers a private ZIP', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-feedback-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const shotsDir = path.join(dir, 'shots');
  fs.mkdirSync(shotsDir);
  const store = new TaskReportStore({ dir: path.join(dir, 'reports'), audit: path.join(dir, 'audit.jsonl'),
    script: path.resolve(here, '../../agent/task_report.py') });
  const routes = new Map();
  const ctx = { connection: { requestRejection(req) { return req.headers.auth === 'yes' ? undefined : 401; } },
    logger: { warn() {} }, on() {}, effect(fn) { return fn(); },
    webServer: { register(route) { routes.set(route.path, route); return () => {}; } } };
  const { turns } = createWebReport(ctx, { store, ledger: path.join(dir, 'reports/web-turns.json'),
    activeFile: path.join(dir, 'active-task.json'), shotsDir });
  const row = turns.start('session-a', 2);
  const stamp = Date.now();
  const shot = path.join(shotsDir, `before-${stamp}.jpg`);
  const created = spawnSync('python3', ['-c',
    'from PIL import Image\nimport sys\nim=Image.new("RGB",(100,100),"white")\ne=Image.Exif(); e[270]="SECRET_EXIF"\nim.save(sys.argv[1],exif=e,icc_profile=b"SECRET_ICC")', shot]);
  assert.equal(created.status, 0, created.stderr.toString());
  const secrets = ['PRIVATE_CHAT_ORDER', 'PRIVATE_EXPECT', 'PRIVATE_COMMAND',
    'PRIVATE_VISION', 'PRIVATE_UNKNOWN', 'PRIVATE_PREVIOUS_TURN'];
  fs.appendFileSync(store.audit, JSON.stringify({ task_id: row.task_id, kind: 'tap', ts: Date.now() / 1000,
    allowed: false, reason_code: 'G2B_ACK', note: secrets[0], expect: secrets[1],
    command: secrets[2], vision: {explanation: secrets[3]}, next_unknown: secrets[4],
    shot_before: shot }) + '\n');
  fs.appendFileSync(store.audit, JSON.stringify({task_id: '22222222-2222-4222-8222-222222222222',
    kind: 'tap', note: secrets[5], shot_before: shot}) + '\n');
  await new Promise(resolve => setTimeout(resolve, 100));
  turns.end('session-a', 2, 'completed');
  await turns.build(row);
  const route = routes.get(EXPORT_ROUTE);
  const url = '/dsb-report/api/export?session=session-a&turn=2';
  const call = async (suffix = '', auth = 'yes', method = 'GET', accept) => {
    const res = response();
    await route.handler({ method, url: url + suffix, headers: { auth, accept } }, res);
    return res;
  };
  assert.equal((await call('', 'no')).status, 401);
  assert.equal((await call('', 'yes', 'POST')).status, 405);
  for (const query of ['&extra=1', '&shots=0', '&shots=1&shots=1', '&turn=2'])
    assert.equal((await call(query)).status, 400);
  assert.equal((await call('&session=wrong')).status, 400);
  const wrong = response();
  await route.handler({method: 'GET', url: '/dsb-report/api/export?session=wrong&turn=2',
    headers: {auth: 'yes'}}, wrong);
  assert.equal(wrong.status, 404);
  const wrongTurn = response();
  await route.handler({method: 'GET', url: '/dsb-report/api/export?session=session-a&turn=3',
    headers: {auth: 'yes'}}, wrongTurn);
  assert.equal(wrongTurn.status, 404);
  const htmlNotFound = response();
  await route.handler({method: 'GET', url: '/dsb-report/api/export?session=wrong&turn=2',
    headers: {auth: 'yes', accept: 'text/html'}}, htmlNotFound);
  assert.equal(htmlNotFound.status, 404);
  assert.match(htmlNotFound.body, /找不到这一轮执行报告/);
  const htmlUnauth = await call('', 'no', 'GET', 'text/html');
  assert.equal(htmlUnauth.status, 401);
  assert.match(htmlUnauth.headers['Content-Type'], /text\/html; charset=utf-8/);
  assert.match(htmlUnauth.body, /未登录.*返回对话|返回对话/s);
  assert.match(htmlUnauth.body, /history\.back\(\)/);
  const htmlMissing = await call('&turn=3', 'yes', 'GET', 'text/html');
  assert.equal(htmlMissing.status, 400);
  assert.match(htmlMissing.body, /下载链接无效/);
  const plain = await call();
  assert.equal(plain.status, 200);
  assert.equal(plain.headers['Content-Type'], 'application/zip');
  assert.equal(plain.headers['Cache-Control'], 'no-store');
  assert.match(plain.headers['Content-Disposition'], /^attachment; filename="dsb-feedback-\d{8}-\d{4}\.zip"$/);
  const inspect = data => JSON.parse(python('import io,json,sys,zipfile\nfrom PIL import Image\n'
    + 'z=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read()))\n'
    + 'names=z.namelist(); shots=[n for n in names if n.startswith("screenshots/")]\n'
    + 'im=Image.open(io.BytesIO(z.read(shots[0]))) if shots else None\n'
    + 'print(json.dumps({"names":names,"files":{n:z.read(n).decode("utf-8","ignore") for n in names},'
    + '"top":im.getpixel((50,0)) if im else None,'
    + '"below":im.getpixel((50,90)) if im else None,'
    + '"exif":dict(im.getexif()) if im else {},"icc":bool(im.info.get("icc_profile")) if im else False}))', data));
  const noShots = inspect(plain.body);
  assert.equal(noShots.names.some(name => name.startsWith('screenshots/')), false);
  for (const text of Object.values(noShots.files))
    for (const secret of secrets) assert.doesNotMatch(text, new RegExp(secret));
  const running = call('&shots=1');
  const busy = await call('', 'yes', 'GET', 'text/html');
  assert.equal(busy.status, 429);
  assert.match(busy.body, /正在导出.*返回对话|返回对话/s);
  const withShots = await running;
  assert.equal(withShots.status, 200);
  const check = inspect(withShots.body);
  assert.equal(check.names.filter(name => name.startsWith('screenshots/')).length, 1);
  assert.deepEqual(check.top, [0, 0, 0]);
  assert.ok(check.below.every(channel => channel >= 250));
  assert.deepEqual(check.exif, {});
  assert.equal(check.icc, false);
  for (const text of Object.values(check.files))
    for (const secret of secrets) assert.doesNotMatch(text, new RegExp(secret));
  for (let index = 0; index < 41; index++) {
    const file = path.join(shotsDir, `after-${stamp + index + 1}.jpg`);
    fs.copyFileSync(shot, file);
    fs.appendFileSync(store.audit, JSON.stringify({ task_id: row.task_id, shot_before: file }) + '\n');
  }
  const limited = await call('&shots=1');
  assert.equal(limited.status, 413);
  assert.equal(JSON.parse(limited.body).error, 'screenshot_limit_exceeded');
  const htmlLimited = await call('&shots=1', 'yes', 'GET', 'text/html');
  assert.equal(htmlLimited.status, 413);
  assert.match(htmlLimited.body, /截图过多/);
  const lines = fs.readFileSync(store.audit, 'utf8').trim().split('\n').map(JSON.parse);
  const lifecycle = lines.filter(value => value.kind === 'report_task_start' || value.kind === 'report_task_end');
  const large = path.join(shotsDir, `shot-${stamp}.png`);
  const big = spawnSync('python3', ['-c',
    'from PIL import Image\nimport sys\nImage.new("1",(4500,4500)).save(sys.argv[1])', large]);
  assert.equal(big.status, 0, big.stderr.toString());
  fs.writeFileSync(store.audit, [...lifecycle, {task_id: row.task_id, shot_before: large}]
    .map(value => JSON.stringify(value)).join('\n') + '\n');
  const dimension = await call('&shots=1');
  assert.equal(dimension.status, 413);
  assert.equal(JSON.parse(dimension.body).error, 'screenshot_limit_exceeded');
  fs.writeFileSync(store.audit, 'invalid-json\n');
  const unavailable = await call('', 'yes', 'GET', 'text/html');
  assert.equal(unavailable.status, 503);
  assert.match(unavailable.body, /暂不可用/);
  assert.match(unavailable.body, /history\.back\(\)/);
});
