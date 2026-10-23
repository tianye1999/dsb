import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { assertDeviceReport } from './device-report.mjs';

const home = '/data/data/com.termux/files/home';
const bash = '/data/data/com.termux/files/usr/bin/bash';
const adbq = path.join(home, 'dsb/adbq.sh');
const here = path.dirname(fileURLToPath(import.meta.url));
const run = (args, timeout = 120000) => new Promise((resolve, reject) => {
  const child = spawn(bash, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '', err = '';
  const timer = setTimeout(() => child.kill('SIGTERM'), timeout);
  child.stdout.on('data', data => out += data);
  child.stderr.on('data', data => err += data);
  child.on('error', error => { clearTimeout(timer); reject(error); });
  child.on('close', code => { clearTimeout(timer);
    if (code !== 0) reject(new Error(`exit=${code}: ${out}\n${err}`)); else resolve(out.trim()); });
});

// Instrumentation kills the target process on exit. Keep the existing power-saving
// daemon alive only for the bounded test and restoration; do not change its config.
const keepalive = net.connect(3098, '127.0.0.1');
keepalive.on('error', () => {});
await once(keepalive, 'connect');
const safety = setTimeout(() => keepalive.destroy(), 180000);
try {
  if (process.env.DSB_INSTALL_CANDIDATE === '1') {
    const build = process.env.DSB_CANDIDATE_BUILD || '/data/data/com.termux/files/usr/var/lib/proot-distro/containers/ubuntu/rootfs/root/dsb-stop-build';
    console.log(await run([adbq, 'install', '-r', `${build}/candidate.apk`], 30000));
    console.log(await run([adbq, 'install', '-r', '-t', `${build}/tests.apk`], 30000));
    console.log(await run([path.join(here, 'restore-a11y.sh')], 45000));
  }
  const runId = randomUUID();
  const output = await run([adbq, 'shell', 'am', 'instrument', '-w', '-e', 'runId', runId,
    ...(process.env.DSB_REPAIR_TEST_BRIDGE === '1' ? ['-e', 'repairTestBridge', 'true'] : []),
    ...(process.env.DSB_RECOVERY_UI === '1' ? ['-e', 'recovery', 'true'] : []),
    'com.dsb.assistant.stoptests/com.dsb.assistant.stoptests.StopInstrumentation']);
  console.log(output);
  const report = JSON.parse(await run([adbq, 'shell', 'cat',
    '/sdcard/Android/data/com.dsb.assistant/files/stop-device-test.json']));
  assertDeviceReport(output, report, runId);
  console.log('DEVICE_ASSERTIONS_PASS');
} catch (error) {
  console.error(error); process.exitCode = 1;
} finally {
  try {
    console.log(await run([path.join(here, 'restore-a11y.sh')], 45000));
    let restored = false;
    for (let i=0; i<20; i++) {
      const log = fs.readFileSync(path.join(home, 'dsb/alive.log'), 'utf8');
      const matches = [...log.matchAll(/clients=(\d+)/g)];
      if (Number(matches.at(-1)?.[1]) >= 2) { restored = true; break; }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (!restored) throw new Error('App liveness connection not restored');
    console.log(await run([adbq, '蓝']));
    console.log('APP_RESTORED; temporary test keepalive closing');
  } catch (error) { console.error(error); process.exitCode = 1; }
  clearTimeout(safety); keepalive.destroy();
}
