#!/usr/bin/env node
// 让「逗沙包」App 的 WebView 重新加载页面
//
// 为什么需要：DSH 是单页应用，App 的 WebView 不会自己重新拉 index.html。
//   · Chrome → 刷新页面就生效
//   · App    → **不会生效**（实测：App 里 dsh-composer-gesture 还是旧版 1685 字节，
//              而 Chrome 里已是新版 3248 字节，导致同一个手势在两边表现不同）
//
// 仅供可调试构建：MainActivity 按 FLAG_DEBUGGABLE 开启 WebView 调试，
//   才存在 @webview_devtools_remote_<pid> socket，可通过 CDP 发 Page.reload。
//   公开发布构建默认关闭调试口，此脚本不可用于该构建。
import { execSync } from 'node:child_process';

// 2026-09-23：改走 adbq.sh（自动脉冲橙球），别再用裸 adb
const ADB = `bash /data/data/com.termux/files/home/dsb/adbq.sh`;
const PORT = 9334;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const sh = (c) => { try { return execSync(c, { encoding: 'utf8', timeout: 30000 }).trim(); } catch { return ''; } };
async function fetchJson(url, tries = 12) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(3000) });
      if (r.ok) return await r.json();
    } catch {}
    await sleep(500);                     // ← forward 建立后 socket 需要时间就绪，必须重试
  }
  return null;
}

// ⚠️ 不能用 pidof 的第一个 pid：App 有两个进程，
//   主进程 com.dsb.assistant 和 :web 进程（WebView 在后者）。
//   WebView 的调试 socket 名字里带的是 **:web 那个进程**的 pid。
//   实测踩到：pidof 返回 25175（主），而 socket 是 remote_20733（:web）。
const procs = sh(`${ADB} shell /system/bin/ps -A -o PID,ARGS`)
  .split('\n').filter(l => /^\s*\d+\s+com\.dsb\.assistant(:\w+)?\s*$/.test(l));
if (!procs.length) { console.error('✗ 逗沙包 App 没在跑'); process.exit(1); }
console.log('  App 进程:\n' + procs.map(l => '    ' + l.trim()).join('\n'));

// 优先用 :web 进程的 pid；拿不到就从 socket 名里反解
const webProc = procs.find(l => l.includes(':web'));
let pid = webProc ? webProc.trim().split(/\s+/)[0] : '';
if (!pid) {
  const sock = sh(`${ADB} shell cat /proc/net/unix`)
    .split('\n').map(l => (l.match(/@webview_devtools_remote_(\d+)/) || [])[1]).filter(Boolean).pop();
  pid = sock || procs[0].trim().split(/\s+/)[0];
}
console.log(`  用 pid = ${pid} 建 forward`);

sh(`${ADB} forward --remove tcp:${PORT}`);       // 清掉旧转发，避免指向已死 socket
sh(`${ADB} forward tcp:${PORT} localabstract:webview_devtools_remote_${pid}`);

const list = await fetchJson(`http://127.0.0.1:${PORT}/json`);
if (!list) { console.error('✗ 连不上 WebView 调试口（forward 或 socket 不可用）'); process.exit(1); }

const page = list.find(t => t.type === 'page');
if (!page) { console.error('✗ 没找到 WebView 页面'); process.exit(1); }
console.log(`  页面: ${page.url}`);

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener('open', res);
  ws.addEventListener('error', rej);
  setTimeout(() => rej(new Error('WS 连接超时')), 8000);
});
ws.send(JSON.stringify({ id: 1, method: 'Page.reload', params: { ignoreCache: true } }));
await sleep(1500);
ws.close();
console.log('✓ 已让 App 的 WebView 重新加载页面（等 5~8 秒生效）');
