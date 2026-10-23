#!/usr/bin/env node
// 在「逗沙包」App 的 WebView 里执行一段 JS（CDP Runtime.evaluate），把结果打成 JSON。
// 用法：node cdp_eval.mjs <expr.js>
import { execSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const ADB = 'bash /data/data/com.termux/files/home/dsb/adbq.sh';
const PORT = 9334;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const sh = (c) => { try { return execSync(c, { encoding: 'utf8', timeout: 30000 }).trim(); } catch { return ''; } };

const procs = sh(`${ADB} shell /system/bin/ps -A -o PID,ARGS`).split('\n').filter(l => /^\s*\d+\s+com\.dsb\.assistant(:\w+)?\s*$/.test(l));
if (!procs.length) { console.error('✗ 逗沙包 App 没在跑'); process.exit(1); }
const webProc = procs.find(l => l.includes(':web'));
let pid = webProc ? webProc.trim().split(/\s+/)[0] : '';
if (!pid) {
  const sock = sh(`${ADB} shell cat /proc/net/unix`).split('\n').map(l => (l.match(/@webview_devtools_remote_(\d+)/) || [])[1]).filter(Boolean).pop();
  pid = sock || procs[0].trim().split(/\s+/)[0];
}
sh(`${ADB} forward --remove tcp:${PORT}`);
sh(`${ADB} forward tcp:${PORT} localabstract:webview_devtools_remote_${pid}`);

async function fetchJson(url, tries = 12) {
  for (let i = 0; i < tries; i++) {
    try { const r = await fetch(url, { signal: AbortSignal.timeout(3000) }); if (r.ok) return await r.json(); } catch {}
    await sleep(500);
  }
  return null;
}
const list = await fetchJson(`http://127.0.0.1:${PORT}/json`);
if (!list) { console.error('✗ 连不上 WebView 调试口'); process.exit(1); }
const page = list.find(t => t.type === 'page');
if (!page) { console.error('✗ 没找到 WebView 页面'); process.exit(1); }
console.error('页面: ' + page.url.replace(/token=[^&]+/, 'token=***'));

const expr = readFileSync(process.argv[2], 'utf8');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); setTimeout(() => rej(new Error('WS 超时')), 8000); });
const msg = await new Promise((res, rej) => {
  ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id === 1) res(m); });
  ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: expr, returnByValue: true, awaitPromise: true } }));
  setTimeout(() => rej(new Error('evaluate 超时')), 25000);
});
ws.close();
const r = msg.result || {};
if (r.exceptionDetails) { console.error('JS 异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 1200)); process.exit(2); }
const v = r.result && r.result.value;
console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 1));
