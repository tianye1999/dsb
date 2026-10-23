#!/usr/bin/env node
// 测：SDK runtime 正常 shutdown 之后，会话有没有落进共享 session 存储（决定"刷新网页能不能看到"）
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import zlib from 'node:zlib';

const root = path.join(os.homedir(), '.dsh/sessions/--root--');
const before = new Set(fs.readdirSync(root));

const child = spawn('dsh', ['--profile', 'phone'], { cwd: '/root', env: { ...process.env, DSH_PERMISSION_MODE: 'danger-full-access' }, stdio: ['pipe','pipe','pipe'] });
let nextId = 1; const pending = new Map(); let assistant = []; let onEnd = null;
readline.createInterface({ input: child.stdout }).on('line', (line) => {
  let m; try { m = JSON.parse(line); } catch { return; }
  if (m.id !== undefined && m.method === undefined) { const p = pending.get(m.id); if (p) { pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); } return; }
  if (m.method === 'session.event') {
    const ev = m.params.event;
    if (ev.type === 'assistant/message') { const x=(ev.data.message.content||[]).filter(b=>b.type==='text').map(b=>b.text).join(''); if(x) assistant.push(x); }
    if (ev.type === 'turn/end' && onEnd) { const cb=onEnd; onEnd=null; cb(); }
  }
});
child.stderr.on('data', ()=>{});
const send = (method, params) => { const id = nextId++; child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n'); return new Promise((res,rej)=>pending.set(id,{resolve:res,reject:rej})); };
const sid = 'persist-test-' + Date.now();
(async () => {
  await send('initialize', { cwd: '/root', provider: 'deepseek-official', model: 'deepseek-flash' });
  await send('session/prompt', { sessionId: sid, contentBlocks: [{ type:'text', text:'只回复两个字：收到' }] });
  await new Promise(r => { onEnd = r; setTimeout(r, 120000); });
  console.log('答复:', assistant.join(' | '));
  console.log('shutdown 前，会话文件状态：');
  const dir = path.join(root, sid);
  if (fs.existsSync(path.join(dir, 'session.v3.jsonl.zstd'))) {
    const n = zlib.zstdDecompressSync(fs.readFileSync(path.join(dir,'session.v3.jsonl.zstd'))).toString('utf8').split('\n').filter(Boolean).length;
    console.log('  事件数 =', n);
  } else console.log('  文件还不存在');
  await send('shutdown', {});
  await new Promise(r => setTimeout(r, 3000));
  console.log('shutdown 后：');
  try {
    const n = zlib.zstdDecompressSync(fs.readFileSync(path.join(dir,'session.v3.jsonl.zstd'))).toString('utf8').split('\n').filter(Boolean).length;
    console.log('  事件数 =', n);
    const types = zlib.zstdDecompressSync(fs.readFileSync(path.join(dir,'session.v3.jsonl.zstd'))).toString('utf8').split('\n').filter(Boolean).map(l=>{try{return JSON.parse(l).type}catch{return '?'}});
    console.log('  类型:', [...new Set(types)].join(','));
  } catch (e) { console.log('  读不到:', e.message); }
  try { child.kill('SIGKILL'); } catch {}
  process.exit(0);
})().catch(e => { console.log('ERR', e.message); process.exit(1); });
