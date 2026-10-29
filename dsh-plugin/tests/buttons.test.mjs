import assert from 'node:assert/strict';
import vm from 'node:vm';
import test from 'node:test';
import {indexRows} from '../dsb-ui/index.js';

function fixture() {
  let mode='hidden', now=10000, value='';
  const nodes={}, intervals=new Map(), timers=[], events={};
  const svg={style:{}}, scroll={scrollTop:100,clientHeight:800,scrollHeight:2000};
  const dot={click(){mode=mode==='hidden'?'shown':'hidden';}};
  nodes['dsh-composer-dot']=dot;
  const document={readyState:'complete',documentElement:{getAttribute(){return mode;}},
    getElementById(id){return nodes[id];},
    querySelector(selector){return selector.includes('scrollBody')?scroll:{
      getBoundingClientRect(){return {height:148};},querySelector(){return {value};}};},
    createElement(){return {style:{},setAttribute(k,v){this[k]=v;},
      querySelector(){return svg;},addEventListener(k,fn){this[k]=fn;}};},
    body:{appendChild(node){nodes[node.id]=node;}},addEventListener(k,fn){events[k]=fn;}};
  const context={document,window:{},Date:{now:()=>now},
    MutationObserver:class{observe(){}},setInterval(fn,ms){intervals.set(ms,fn);},
    setTimeout(fn,ms){timers.push({fn,ms});}};
  for(const id of ['dsh-tobottom-keep-script','dsh-composer-autohide']){
    const source=indexRows().find(r=>r.html.includes(`id="${id}"`)).html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
    vm.runInNewContext(source,context);
  }
  return {nodes,scroll,svg,intervals,events,get mode(){return mode;},
    setValue(v){value=v;},advance(ms){now+=ms;},
    flush(ms){for(const timer of timers.splice(0))if(timer.ms===ms)timer.fn();else timers.push(timer);}};
}

test('single phone control scrolls without revealing, then toggles at bottom and updates arrow',()=>{
  const f=fixture(),button=f.nodes['dsh-tobottom-keep'];
  assert.equal(button['aria-label'],'输入框与滚动开关');
  assert.equal(button.title,'回到底部');
  button.click();f.flush(80);
  assert.equal(f.scroll.scrollTop,2000);
  assert.equal(f.mode,'hidden');
  assert.equal(f.svg.style.transform,'rotate(180deg)');
  button.click();f.flush(80);
  assert.equal(f.mode,'shown');
  assert.equal(button.style.bottom,'158px');
  assert.equal(f.svg.style.transform,'none');
  button.click();f.flush(80);
  assert.equal(f.mode,'hidden');
  assert.equal(button.style.bottom,'10px');
});

test('queued auto-hide respects the 1500ms manual protection but ordinary clearing still hides',()=>{
  const f=fixture(),button=f.nodes['dsh-tobottom-keep'];
  f.scroll.scrollTop=1200;button.click();
  const clear=()=>{f.setValue('draft');f.intervals.get(250)();f.setValue('');f.intervals.get(250)();};
  clear();f.advance(350);f.flush(350);assert.equal(f.mode,'shown');
  f.advance(1150);clear();f.flush(350);assert.equal(f.mode,'hidden');
});
