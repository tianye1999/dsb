#!/usr/bin/env node
import { spawn } from 'node:child_process';
import readline from 'node:readline';
const SID = 'phone-' + Date.now(); console.log('SID', SID);
const child = spawn('dsh', ['--profile', 'phone'], { cwd: '/root', env: { ...process.env, DSH_PERMISSION_MODE: 'danger-full-access' }, stdio: ['pipe','pipe','pipe'] });
let nextId = 1; const pending = new Map(); let assistant = []; let onEnd = null;
readline.createInterface({ input: child.stdout }).on('line', (line) => {
  let m; try { m = JSON.parse(line); } catch { return; }
  if (m.id !== undefined && m.method === undefined) { const p = pending.get(m.id); if (p) { pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); } return; }
  if (m.method === 'session.event' && m.params.sessionId === SID) {
    const ev = m.params.event;
    if (ev.type === 'assistant/message') { const t = (ev.data.message.content||[]).filter(b=>b.type==='text').map(b=>b.text).join(''); if (t) assistant.push(t); }
    if (ev.type === 'turn/end') { const cb = onEnd; onEnd = null; if (cb) cb(); }
  }
});
child.stderr.on('data', ()=>{});
const send = (method, params) => { const id = nextId++; child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n'); return new Promise((res,rej)=>pending.set(id,{resolve:res,reject:rej})); };
async function ask(text) { assistant = []; await send('session/prompt', { sessionId: SID, contentBlocks: [{ type:'text', text }] }); await new Promise(r => { onEnd = r; setTimeout(r, 240000); }); return assistant.join(' | '); }
(async () => {
  await send('initialize', { cwd: '/root', provider: 'deepseek-official', model: 'deepseek-flash' });
  console.log('A:', await ask('记住一个事实：我最喜欢的数字是 42。只回复 OK'));
  console.log('B:', await ask('我最喜欢的数字是多少？只回数字'));
  console.log('C:', await ask('把当前屏幕上的主要文字念出来（可以自己截屏看）'));
  child.kill('SIGKILL'); process.exit(0);
})().catch(e => { console.log('ERR', e.message); process.exit(1); });
