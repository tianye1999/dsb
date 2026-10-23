import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const modules=process.env.DSH_TEST_MODULES && path.resolve(process.env.DSH_TEST_MODULES);
const authTestOptions={skip:modules ? false : 'DSH_TEST_MODULES is unset; install @deepseek-ai/dsh@0.1.7-rc.2 and point it at node_modules to run real authentication tests'};
let Context,connectionPlugin;
if(modules){
  ({Context}=await import(pathToFileURL(modules+'/@deepseek-ai/cordis/lib/index.js')));
  connectionPlugin=await import(pathToFileURL(modules+'/@deepseek-ai/dsh-client-connection/lib/index.js'));
}

function response(){return {status:0,headers:{},body:'',writeHead(status,headers={}){this.status=status;this.headers=headers;},end(body){this.body=body||'';}};}
async function fixture(t){
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recap-http-'));
  fs.cpSync(new URL('./hardened/',import.meta.url),dir+'/lib',{recursive:true});
  fs.symlinkSync(modules,dir+'/node_modules');fs.writeFileSync(dir+'/package.json','{"type":"module"}');
  const plugin=await import(pathToFileURL(dir+'/lib/index.js'));
  const root=new Context();let record;
  root.reflect.provide('credentials',{modifyRecord:async(_,mutate)=>{record=await mutate(record)||record;return record;}});
  await connectionPlugin.apply(root,{});
  const connection=root.connection,config=plugin.Config({enabled:true});
  let route, idle;const events=new Map();
  const session={id:'synthetic-session',header:{origin:'user'},snapshotEvents:()=>[
    {type:'user/message',seq:1,data:{source:{kind:'user'},content:[{type:'text',text:'synthetic input'}]}},
    {type:'assistant/message',seq:2,data:{message:{content:[{type:'text',text:'synthetic response'}]}}},
  ]};
  const ctx={connection,sessions:{get(){return session;}},
    llm:{async *stream(){yield {type:'text-delta',index:0,text:'synthetic-private-summary'};yield {type:'finish',reason:{kind:'stop'}};}},
    timeout(fn){idle=fn;return ()=>{};},
    webServer:{register(value){route=value;return ()=>{};}},
    logger:{info(){},warn(){}},on(name,fn){events.set(name,fn);},inject(){},
    effect(fn,label){if(label==='recap: http route')return fn();}};
  plugin.apply(ctx,config);assert.ok(route);
  const request=(method='GET',url='/recap/api',headers={})=>({method,url,headers:{host:'127.0.0.1:3080',...headers}});
  async function call(method,url,headers){const res=response();await route.handler(request(method,url,headers),res);return res;}
  function cookie(age=0){
    const now=Date.now;try{
      Date.now=()=>now()-age;
      const res=response(),url=new URL(connection.authenticatedUrl('http://127.0.0.1:3080/'));
      assert.equal(connection.authorizeIndex(request('GET',url.pathname+url.search),res),false);
      assert.equal(res.status,303);return res.headers['set-cookie'].split(';')[0];
    }finally{Date.now=now;}
  }
  t.after(async()=>{await root.fiber.dispose();fs.rmSync(dir,{recursive:true,force:true});});
  async function seed(){
    events.get('session/event')(session,{type:'turn/end',data:{reason:{kind:'completed'},turn:1}});idle();
    for(let i=0;i<20;i++){
      await new Promise(resolve=>setImmediate(resolve));
      const res=await call('GET','/recap/api',{cookie:cookie()});
      if(JSON.parse(res.body).count===1)return;
    }
    assert.fail('synthetic summary not generated');
  }
  return {plugin,config,ctx,call,cookie,connection,seed};
}

test('public default is disabled and disabled recap never returns data',authTestOptions,async t=>{
  const f=await fixture(t);assert.equal(f.plugin.Config({}).enabled,false);
  await f.seed();f.config.enabled=false;
  for(const url of ['/recap/api','/recap/api?session=synthetic']){
    const res=await f.call('GET',url,{cookie:f.cookie()});assert.equal(res.status,404);
    assert.doesNotMatch(res.body,/items|recap|session|count/);
  }
});
test('enabled exact route reuses actual dsh cookie/trust checks before reading',authTestOptions,async t=>{
  const f=await fixture(t);
  await f.seed();
  for(const url of ['/recap/api','/recap/api?session=synthetic-session','/recap/api?token=synthetic']){
    const res=await f.call('GET',url);assert.equal(res.status,401);assert.doesNotMatch(res.body,/synthetic-private-summary|synthetic-session|items/);
  }
  assert.equal((await f.call('HEAD','/recap/api')).status,401);
  assert.equal((await f.call('GET','/recap/api',{cookie:'dsh-auth-forged=synthetic'})).status,401);
  const cookie=f.cookie(),allowed=await f.call('GET','/recap/api',{cookie});
  assert.equal(allowed.status,200);assert.equal(JSON.parse(allowed.body).items[0].recap,'synthetic-private-summary');
  assert.equal(allowed.headers['Cache-Control'],'no-store');
  const signature=cookie.lastIndexOf('.')+1;
  const tampered=cookie.slice(0,signature)+(cookie[signature]==='A'?'B':'A')+cookie.slice(signature+1);
  assert.equal((await f.call('GET','/recap/api',{cookie:tampered})).status,401);
  const validLaunchQuery=new URL(f.connection.authenticatedUrl('http://127.0.0.1:3080/')).search;
  assert.equal((await f.call('GET','/recap/api'+validLaunchQuery)).status,401);
  assert.equal((await f.call('GET','/recap/api',{cookie,host:'127.0.0.1:3099'})).status,401);
  assert.equal((await f.call('GET','/recap/api',{cookie,'sec-fetch-site':'cross-site'})).status,403);
  assert.equal((await f.call('GET','/recap/api',{cookie,host:'evil.invalid'})).status,403);
  assert.equal((await f.call('GET','/recap/api',{cookie,origin:'https://evil.invalid'})).status,403);
  assert.equal((await f.call('GET','/recap/api',{cookie:f.cookie(31*86400000)})).status,401);
  f.ctx.connection=undefined;assert.equal((await f.call('GET','/recap/api',{cookie})).status,401);
  for(const requestRejection of [()=>{throw new Error('auth unavailable');},()=>false,()=>200,()=>Promise.resolve(undefined)]){
    f.ctx.connection={requestRejection};assert.equal((await f.call('GET','/recap/api',{cookie})).status,401);
  }
});
test('POST remains closed independently of enabled state and credentials',authTestOptions,async t=>{
  const f=await fixture(t);
  for(const enabled of [false,true])for(const cookie of ['',f.cookie()]){
    f.config.enabled=enabled;assert.equal((await f.call('POST','/recap/api',{cookie})).status,410);
  }
});
test('3080 route inventory contains only recap; no private recap server remains',()=>{
  const tweaks=new URL('../',import.meta.url);
  const walk=dir=>fs.readdirSync(dir,{withFileTypes:true}).flatMap(entry=>{
    const full=path.join(dir,entry.name);
    return entry.isDirectory()?walk(full):[full];
  });
  const root=path.resolve(tweaks.pathname);
  const sources=walk(root).filter(file=>/\.(js|mjs|py|sh)$/.test(file)&&!file.endsWith('.test.mjs'));
  const routes=sources.filter(file=>/webServer\s*\.\s*(?:register|registerUpgrade|registerFallback)\s*\(|\bcreateServer\s*\(/.test(fs.readFileSync(file,'utf8')));
  assert.deepEqual(routes.map(file=>path.relative(root,file)),['recap/hardened/index.js']);
  const host=fs.readFileSync(routes[0],'utf8');
  assert.doesNotMatch(host,/createPrivateRecap|recap\.sock|\/inject|followup\(/);
  assert.match(host,/routePath: z\.string\(\)\.default\('\/recap\/api'\)/);
});
