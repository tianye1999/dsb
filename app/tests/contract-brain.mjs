#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
const record=(event,extra={})=>fs.appendFileSync(process.env.TEST_EVENTS,JSON.stringify({event,pid:process.pid,...extra})+'\n');
const reply=value=>process.stdout.write(JSON.stringify(value)+'\n');
const scripts={completed:'print("owned offline calculation", 2 + 2)\n',failed:'raise RuntimeError("owned deliberate failure")\n',
  refused:'tap(630, 1400, expect="owned missing credential", ack=True)\n',stopped:'time.sleep(60)\n'};
record('start');
readline.createInterface({input:process.stdin}).on('line',async line=>{
  const m=JSON.parse(line);
  reply({jsonrpc:'2.0',id:m.id,result:{}});
  if(m.method!=='session/prompt') return;
  const prompt=m.params.contentBlocks[0].text;
  if(prompt.startsWith('DSH 手机代理 · 启动（加载长期记忆）')) {
    reply({method:'session.event',params:{sessionId:m.params.sessionId,event:{type:'turn/end'}}});
    return;
  }
  const scenario=prompt.replace(/^CONTRACT_/,'');
  if(!['completed','failed','refused','stopped','unknown'].includes(scenario)) throw new Error('unexpected fixture prompt');
  record('prompt',{scenario});
  let text='';
  if(scenario!=='unknown') {
    const work=path.join(process.env.HOME,'runner-'+scenario); fs.mkdirSync(work);
    // Copy code only into empty isolated state; never copy real audits, helpers or observations.
    const hashes={};
    for(const name of ['dsb_run.py','safe_tap.py','task_status.py','recovery.py']) {
      const bytes=fs.readFileSync('/root/.dsh-phone/skills/'+name);
      hashes[name]=createHash('sha256').update(bytes).digest('hex');fs.writeFileSync(path.join(work,name),bytes);
    }
    const script=path.join(work,'owned-task.py');fs.writeFileSync(script,scripts[scenario]);
    const child=spawn('/usr/bin/python3',['-B',path.join(work,'dsb_run.py'),script,'--json','--timeout','70'],
      {env:{...process.env,HOME:work},stdio:['ignore','pipe','pipe']});
    record('runner-start',{scenario,child:child.pid,hashes});
    let stdout='',stderr=''; child.stdout.on('data',d=>stdout+=d); child.stderr.on('data',d=>stderr+=d);
    const code=await new Promise((resolve,reject)=>{child.on('error',reject);child.on('close',resolve);});
    const marker='=== RUN_SUMMARY ===';
    const start=stdout.lastIndexOf(marker);
    if(start<0) { record('runner-invalid',{scenario,code,stdout,stderr}); throw new Error('missing runner summary'); }
    const result=JSON.parse(stdout.slice(start+marker.length));
    record('runner-end',{scenario,code,result,stderr});text=JSON.stringify(result);
  }
  reply({method:'session.event',params:{sessionId:m.params.sessionId,event:{type:'assistant/message',data:{message:{content:[{type:'text',text}]}}}}});
  reply({method:'session.event',params:{sessionId:m.params.sessionId,event:{type:'turn/end'}}});
});
