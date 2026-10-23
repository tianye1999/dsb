import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { assertRecoveryReport, assertRestart } from './recovery-report.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const bridgeDir = process.env.DSB_BRIDGE_DIR || path.resolve(here, '../../bridge');
const runId = randomUUID();
const contractOnly = process.env.DSB_CONTRACT_ONLY === '1';
const archive = path.join(here, 'cross-runs', runId);
fs.mkdirSync(archive, { recursive: true });
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-cross-'));
fs.mkdirSync(path.join(home, '.dsh-phone'));
fs.writeFileSync(path.join(home, '.dsh-phone/bridge-tasks.json'), JSON.stringify({
  version: 1, tasks: {}, active: null, latest: null, runtimeOpen: false }));
const eventsFile = path.join(home, 'events');
const events = () => fs.existsSync(eventsFile) ? fs.readFileSync(eventsFile, 'utf8')
  .split('\n').filter(Boolean).map(JSON.parse) : [];
const pids = () => [...new Set(events().flatMap(e => [e.pid, e.child]).filter(Boolean))];
const bash = '/data/data/com.termux/files/usr/bin/bash';
const adbq = '/data/data/com.termux/files/home/dsb/adbq.sh';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function alive(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    return !['Z', 'X'].includes(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[0]);
  } catch (e) { if (e.code === 'ENOENT' || e.code === 'ESRCH') return false; throw e; }
}
async function until(check) {
  const end = Date.now() + 15000;
  while (Date.now() < end) { if (await check()) return; await delay(30); }
  throw new Error('owned fixture deadline');
}
function run(args, timeout = 90000) {
  return new Promise((resolve, reject) => {
    const child = spawn(bash, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '', errors = '', timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, timeout);
    child.stdout.on('data', d => output += d); child.stderr.on('data', d => errors += d);
    child.on('error', e => { clearTimeout(timer); reject(e); });
    child.on('close', code => { clearTimeout(timer);
      if (timedOut || code !== 0) reject(new Error(`exit=${code} timeout=${timedOut}\n${output}\n${errors}`));
      else resolve(output.trim());
    });
  });
}
let bridge, bridgeBase, fixture, keeper, safety, prepared = false, cleanupVerified = false;
const counts = { runId, doCount: 0, cancelCount: 0, taskId: '' };
const responses = [];
const proxy = http.createServer(async (req, res) => {
  res.setHeader('content-type', 'application/json');
  if (req.method === 'GET' && req.url === '/state') {
    return res.end(JSON.stringify({ ...counts, cleanupVerified, workersAlive: pids().some(alive),
      runnerStarted: events().some(e=>e.event==='runner-start' && e.scenario==='stopped') }));
  }
  try {
    if (req.method !== 'POST' || !['/do', '/cancel'].includes(req.url)) throw new Error('unsupported fixture route');
    let body = '';
    for await (const data of req) { body += data; if (body.length > 1024 * 1024) throw new Error('oversized fixture request'); }
    const parsed = JSON.parse(body);
    if (req.url === '/do') { counts.doCount++; if (!counts.taskId) counts.taskId = parsed.task_id; }
    else counts.cancelCount++;
    const upstream = await fetch(bridgeBase + req.url, { method: 'POST', body, signal: AbortSignal.timeout(90000) });
    const result = await upstream.text();
    responses.push({route:req.url,taskId:parsed.task_id,status:upstream.status,result:JSON.parse(result)});
    res.statusCode = upstream.status; res.end(result);
  } catch (e) { res.statusCode = 502; res.end(JSON.stringify({ ok: false, error: String(e) })); }
});
async function startBridge(port) {
  bridgeBase = `http://127.0.0.1:${port}`;
  bridge = spawn(process.execPath, [path.join(bridgeDir, 'bridge.mjs')], {
    env: { PATH: process.env.PATH, HOME: home, BRIDGE_PORT: String(port), BRIDGE_HOST: '127.0.0.1',
      BRIDGE_DSH: contractOnly ? path.join(here,'contract-brain.mjs') : path.join(bridgeDir, 'fake-dsh-test.mjs'), BRIDGE_DIR: path.join(home, 'shots'),
      BRIDGE_MEM_DIR: path.join(home, 'memory'), BRIDGE_COMPACT_EVERY: '0', TEST_EVENTS: eventsFile },
    stdio: ['ignore', 'pipe', 'pipe'] });
  for (const stream of [bridge.stdout, bridge.stderr]) stream.on('data', data => fs.appendFileSync(path.join(archive, 'bridge.log'), data));
  await until(async () => {
    if (bridge.exitCode !== null || bridge.signalCode !== null) throw new Error('isolated bridge exited');
    try { return (await fetch(bridgeBase + '/health')).ok; } catch { return false; }
  });
}
async function stopBridge() {
  if (bridge && bridge.exitCode === null && bridge.signalCode === null) {
    const exited = once(bridge, 'exit'); bridge.kill('SIGKILL'); await exited;
  }
}
async function phase(name) {
  const output = await run([adbq, 'shell', 'am', 'instrument', '-w', '-e', 'runId', runId,
    '-e', 'phase', name, '-e', 'fixture', fixture,
    'com.dsb.assistant.stoptests/com.dsb.assistant.stoptests.RecoveryInstrumentation']);
  const raw = await run([adbq, 'shell', 'cat', '/sdcard/Android/data/com.dsb.assistant/files/recovery-device-test.json'], 10000);
  fs.writeFileSync(path.join(archive, name + '.txt'), output);
  fs.writeFileSync(path.join(archive, name + '.json'), raw);
  const report = JSON.parse(raw);
  assertRecoveryReport(output, report, runId, name);
  console.log(JSON.stringify({ phase: name, report }));
  return report;
}
try {
  proxy.listen(0, '127.0.0.1'); await once(proxy, 'listening');
  fixture = `http://127.0.0.1:${proxy.address().port}`;
  keeper = net.connect(3098, '127.0.0.1');
  keeper.setTimeout(5000, () => keeper.destroy(new Error('keepalive timeout')));
  await once(keeper, 'connect'); keeper.setTimeout(0); keeper.on('error', () => {});
  safety = setTimeout(() => keeper.destroy(), 360000);
  const reserve = net.createServer(); reserve.listen(0, '127.0.0.1'); await once(reserve, 'listening');
  const port = reserve.address().port; await new Promise(resolve => reserve.close(resolve));
  await phase('preflight');
  if (process.env.DSB_PREFLIGHT_ONLY === '1') console.log('PREFLIGHT_ONLY_PASS ' + runId);
  else {
    await startBridge(port);
    if(contractOnly) {
      const final=await phase('contract');
      for(const name of ['completed','failed','refused']) {
        const event=events().find(e=>e.event==='runner-end' && e.scenario===name);
        assert.ok(event, 'missing real runner result '+name);
        assert.equal(event.result.terminal_status,name);
        assert.equal(event.result.goal_verified,false);
        assert.equal(event.code,{completed:0,failed:3,refused:2}[name]);
      }
      assert.equal(counts.doCount,5);
      for(const response of responses.filter(r=>r.route==='/do')) assert.equal(response.result.goal_verified,false);
      assert.equal(new Set(final.cases.map(c=>c.taskId)).size,5);
      console.log('MAIN_ENTRY_REAL_BRIDGE_RUNNER_CONTRACT_PASS '+runId);
    } else {
    prepared = true;
    const before = await phase('prepare_bridge');
    await until(() => events().some(e => e.text === 'hang'));
    const oldPid = events().find(e => e.event === 'start').pid;
    await stopBridge(); await startBridge(port);
    assert.equal(alive(oldPid), true, 'old synthetic runtime must still be alive');
    const after = await phase('verify_unknown');
    assertRestart(before, after);
    assert.equal(counts.taskId, after.taskId);
    assert.equal(counts.doCount, 1);
    fs.writeFileSync(path.join(archive, 'evidence.json'), JSON.stringify({ ...counts, oldPid, oldWorkerAlive: alive(oldPid) }));
    console.log('CROSS_LAYER_ASSERTIONS_PASS ' + runId);
    }
  }
} catch (e) { console.error(e); process.exitCode = 1; }
finally {
  try {
    await stopBridge();
    for (const pid of pids()) {
      if (!alive(pid)) continue;
      const env = fs.readFileSync(`/proc/${pid}/environ`, 'utf8').split('\0');
      if (!env.includes('TEST_EVENTS=' + eventsFile)) throw new Error('owned PID identity no longer matches');
      process.kill(pid, 'SIGKILL');
    }
    await until(() => pids().every(pid => !alive(pid)));
    cleanupVerified = true;
    if (prepared) await phase('cleanup_unknown');
    console.log(await run([path.join(here, 'restore-a11y.sh')], 45000));
    console.log('OWNED_CROSS_LAYER_CLEANUP_VERIFIED');
  } catch (e) { console.error('CLEANUP_FAILED', e); process.exitCode = 1; }
  fs.writeFileSync(path.join(archive, 'events.json'), JSON.stringify(events()));
  fs.writeFileSync(path.join(archive, 'responses.json'), JSON.stringify(responses));
  if (cleanupVerified) fs.rmSync(home, { recursive: true, force: true });
  clearTimeout(safety); keeper?.destroy();
  proxy.closeAllConnections(); await new Promise(resolve => proxy.close(resolve));
  console.log('ARCHIVE=' + archive);
}
