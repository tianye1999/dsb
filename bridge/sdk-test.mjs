#!/usr/bin/env node
// 一次性测试：起 SDK runtime，走 JSON-RPC，打印流回来的事件形状
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const prompt = process.argv[2] || '只回复两个字：收到';
const child = spawn('dsh', ['--profile', 'phone'], {
  cwd: '/root',
  env: { ...process.env, DSH_PERMISSION_MODE: 'danger-full-access' },
  stdio: ['pipe', 'pipe', 'pipe'],
});
child.stderr.on('data', d => process.stderr.write('[runtime] ' + d.toString().slice(0, 200)));

let nextId = 1;
const pending = new Map();
const rl = readline.createInterface({ input: child.stdout });
rl.on('line', (line) => {
  let msg; try { msg = JSON.parse(line); } catch { return; }
  if (msg.id !== undefined && msg.method === undefined) {
    const p = pending.get(msg.id); if (p) { pending.delete(msg.id); msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result); }
    return;
  }
  if (msg.method) {
    if (msg.method === 'session.status') {
      console.log('STATUS', msg.params.sessionId, msg.params.status);
      if (msg.params.status === 'idle') { finish(); }
      return;
    }
    if (msg.method === 'session.event') {
      const ev = msg.params.event;
      const s = JSON.stringify(ev);
      if (ev && (ev.type || ev.kind)) {
        console.log('EVENT type=' + (ev.type || ev.kind) + ' :: ' + s.slice(0, 260));
      } else {
        console.log('EVENT ? :: ' + s.slice(0, 260));
      }
      return;
    }
    console.log('NOTIFY', msg.method, JSON.stringify(msg.params).slice(0, 160));
  }
});
function send(method, params) {
  const id = nextId++;
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
let done = false;
function finish() {
  if (done) return; done = true;
  setTimeout(() => { try { child.kill('SIGKILL'); } catch {} process.exit(0); }, 500);
}
(async () => {
  const init = await send('initialize', { cwd: '/root', provider: 'deepseek-official', model: 'deepseek-flash' });
  console.log('INIT', JSON.stringify(init));
  const r = await send('session/prompt', { sessionId: 'phone', contentBlocks: [{ type: 'text', text: prompt }] });
  console.log('PROMPT', JSON.stringify(r));
  setTimeout(finish, 240000);
})().catch(e => { console.log('ERR', e.message); process.exit(1); });
