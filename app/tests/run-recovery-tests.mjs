import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { assertRecoveryReport, assertRestart } from './recovery-report.mjs';

const home = '/data/data/com.termux/files/home';
const bash = '/data/data/com.termux/files/usr/bin/bash';
const adbq = path.join(home, 'dsb/adbq.sh');
const here = path.dirname(fileURLToPath(import.meta.url));
const runId = randomUUID();
const counts = {doCount:0, cancelCount:0};
let held;
let heldId;
const cancelled = new Set();
const server = http.createServer(async (req,res) => {
  res.setHeader('Content-Type','application/json');
  const reply = value => res.end(JSON.stringify(value));
  if (req.url === '/state' && req.method === 'GET') return reply(counts);
  let body;
  try {
    let raw='';
    for await (const chunk of req) {
      raw+=chunk;
      if (raw.length>1024*1024) throw new Error('oversized fixture request');
    }
    body=JSON.parse(raw);
    if (typeof body.task_id!=='string' || !body.task_id) throw new Error('task ID required');
  } catch { res.statusCode=400; return reply({ok:false}); }
  const receipt={protocol:2,task_id:body.task_id};
  if (req.url === '/do' && req.method === 'POST') {
    if (cancelled.has(body.task_id)) return reply({...receipt,ok:false});
    counts.doCount++;
    if (counts.doCount === 1) { held=res; heldId=body.task_id; return; }
    return reply({...receipt,ok:true,transport_settled:true,text:'owned fixture reply'});
  }
  if (req.url === '/cancel' && req.method === 'POST') {
    counts.cancelCount++;
    if (body.task_id!==heldId && !cancelled.has(body.task_id))
      return reply({...receipt,ok:false,stop_confirmed:false});
    cancelled.add(body.task_id);
    if (held && !held.destroyed) held.end(JSON.stringify({...receipt,ok:false,text:'owned cancelled reply'}));
    return reply({...receipt,ok:true,stop_confirmed:true});
  }
  res.statusCode=404; reply({ok:false});
});
const run = (args, timeout=60000) => new Promise((resolve,reject) => {
  const child=spawn(bash,args,{stdio:['ignore','pipe','pipe']});
  let output='', errors='', timedOut=false;
  const timer=setTimeout(() => { timedOut=true; child.kill('SIGKILL'); },timeout);
  child.stdout.on('data',data => output+=data);
  child.stderr.on('data',data => errors+=data);
  child.on('error',error => { clearTimeout(timer); reject(error); });
  child.on('close',code => {
    clearTimeout(timer);
    if (timedOut || code!==0) reject(new Error(`command exit=${code} timeout=${timedOut}\n${output}\n${errors}`));
    else resolve(output.trim());
  });
});
let fixture, keepalive, safety, prepared=false;
async function phase(name) {
  const output=await run([adbq,'shell','am','instrument','-w','-e','runId',runId,
    '-e','phase',name,'-e','fixture',fixture,
    'com.dsb.assistant.stoptests/com.dsb.assistant.stoptests.RecoveryInstrumentation']);
  console.log(output);
  const report=JSON.parse(await run([adbq,'shell','cat',
    '/sdcard/Android/data/com.dsb.assistant/files/recovery-device-test.json'],10000));
  console.log(JSON.stringify(report));
  assertRecoveryReport(output,report,runId,name);
  return report;
}
try {
  server.listen(0,'127.0.0.1'); await once(server,'listening');
  fixture=`http://127.0.0.1:${server.address().port}`;
  keepalive=net.connect(3098,'127.0.0.1');
  await Promise.race([once(keepalive,'connect'),new Promise((_,reject) => {
    keepalive.setTimeout(5000,() => { keepalive.destroy(); reject(new Error('keepalive connect timeout')); });
  })]);
  keepalive.setTimeout(0); keepalive.on('error',() => {});
  // Bounded guard only; no edits to the existing power policy.
  safety=setTimeout(() => keepalive.destroy(),420000);
  await phase('store');
  prepared=true;
  const before=await phase('prepare');
  const after=await phase('verify');
  assertRestart(before,after);
  if (counts.doCount!==2 || counts.cancelCount<2) throw new Error('unexpected owned bridge requests');
  console.log(`RECOVERY_DEVICE_ASSERTIONS_PASS ${JSON.stringify({runId,...counts})}`);
} catch (error) { console.error(error); process.exitCode=1; }
finally {
  try {
    // Cleanup must run while the owned bridge is still alive, including on failure.
    if (prepared) await phase('cleanup');
  } catch (error) { console.error('OWNED_RECOVERY_CLEANUP_FAILED',error); process.exitCode=1; }
  try {
    if (keepalive && !keepalive.destroyed) {
      console.log(await run([path.join(here,'restore-a11y.sh')],45000));
      let restored=false;
      for(let i=0;i<20;i++) {
        const matches=[...fs.readFileSync(path.join(home,'dsb/alive.log'),'utf8').matchAll(/clients=(\d+)/g)];
        if(Number(matches.at(-1)?.[1])>=2) { restored=true; break; }
        await new Promise(resolve => setTimeout(resolve,1000));
      }
      if(!restored) throw new Error('App liveness connection not restored');
      console.log(await run([adbq,'蓝'],10000));
      console.log('APP_RESTORED; temporary test keepalive closing');
    }
  } catch(error) { console.error(error); process.exitCode=1; }
  clearTimeout(safety); keepalive?.destroy();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
