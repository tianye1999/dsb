// Real chat history and native scrolling controls; never submit a prompt.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

export async function comparePhone(browser, output) {
  const results=[];
  for(const initial of ['shown','hidden']) {
    const pair=[];
    for(const [version,url] of [['A',process.env.DSB_BROWSER_OLD_URL],['B-prime',process.env.DSB_BROWSER_NEW_URL]]) {
      const context=await browser.newContext({viewport:{width:360,height:800},deviceScaleFactor:3,
        isMobile:true,hasTouch:true,locale:'zh-CN',colorScheme:'dark'});
      try {
        await context.addInitScript(value=>{
          localStorage.setItem('dsh.composer.mode',value);
          window.__dshHideQueued=0;
          window.__dshTicks=[];
          const interval=window.setInterval;
          window.setInterval=function(fn,ms,...args){
            if(ms!==250||!String(fn).includes('last !== null'))return interval.call(this,fn,ms,...args);
            return interval.call(this,()=>{
              const seat=document.querySelector('.wSkVaW_composerSeat');
              const input=seat?.querySelector('textarea,[contenteditable="true"]');
              window.__dshTicks.push({mode:document.documentElement.dataset.dshComposer,
                value:input?.value??input?.textContent,queued:window.__dshHideQueued});
              window.__dshTicks=window.__dshTicks.slice(-12);
              fn(...args);
            },ms);
          };
          const original=window.setTimeout;
          window.setTimeout=function(fn,ms,...args){
            if(ms===350&&fn.name==='hide')window.__dshHideQueued++;
            return original.call(this,fn,ms,...args);
          };
        },initial);
        const page=await context.newPage(),errors=[],failedAssets=[];
        page.on('pageerror',e=>errors.push(e.message));
        page.on('response',r=>{if(/\/(assets|plugins)\//.test(r.url())&&r.status()>=400)failedAssets.push(r.status());});
        await context.route('https://**/*',route=>route.abort());
        await page.goto(url);await page.locator('[data-composer-input]').waitFor({state:'attached'});
        await page.waitForTimeout(4000);
        for(let i=0;i<30;i++) {
          const accept=page.getByRole('dialog').getByRole('button',{name:/^(继续|Continue)$/});
          const close=page.getByRole('dialog').getByRole('button',{name:/^(关闭|Close)$/});
          if(await accept.count()&&await accept.isEnabled())await accept.click();
          else if(await close.count())await close.click();
          else if(!await page.getByRole('dialog').count())break;
          await page.waitForTimeout(250);
        }
        assert.equal(await page.getByRole('dialog').count(),0,'dismiss real onboarding before comparing');
        const entry={version,initial,scenes:{}};
        const snapshot=()=>page.evaluate(()=>{
          const read=selector=>{
            const el=document.querySelector(selector);if(!el)return null;
            const s=getComputedStyle(el),r=el.getBoundingClientRect();
            return {x:r.x,y:r.y,width:r.width,height:r.height,display:s.display,
              opacity:s.opacity,color:s.color,background:s.backgroundColor,
              font:s.fontSize,lineHeight:s.lineHeight,bottom:s.bottom,
              transform:s.transform,title:el.title||''};
          };
          const sc=document.querySelector('.wSkVaW_scrollBody');
          return {composer:document.documentElement.dataset.dshComposer,
            sidebar:document.documentElement.dataset.dshSidebar,
            atBottom:sc.scrollHeight-sc.clientHeight-sc.scrollTop<=8,
            keep:read('#dsh-tobottom-keep'),arrow:read('#dsh-tobottom-keep svg'),
            dot:read('#dsh-composer-dot'),rail:read('#dsh-rail-toggle'),
            nativeVisible:[...document.querySelectorAll('[aria-label="回到底部"]')].filter(e=>getComputedStyle(e).display!=='none').length,
            base:getComputedStyle(document.body).backgroundColor,input:read('[data-composer-input]')};
        });
        const shot=async scene=>{
          await page.locator('[data-composer-input]').evaluate(e=>e.blur());await page.waitForTimeout(300);
          entry.scenes[scene]=await snapshot();
          await page.screenshot({path:path.join(output,`${version}-${initial}-${scene}.png`)});
        };
        const bottom=async()=>{await page.evaluate(()=>{const s=document.querySelector('.wSkVaW_scrollBody');s.scrollTop=s.scrollHeight;});await page.waitForTimeout(1000);};
        const mode=async value=>{
          await bottom();
          if((await snapshot()).composer!==value)await page.locator('#dsh-tobottom-keep').tap();
          await page.waitForTimeout(500);assert.equal((await snapshot()).composer,value);
        };
        const swipe=async(from,to)=>{
          const cdp=await context.newCDPSession(page);
          await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:180,y:from}]});
          for(let i=1;i<=12;i++){
            await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:180,y:from+(to-from)*i/12}]});
            await page.waitForTimeout(25);
          }
          await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await cdp.detach();
          await page.waitForTimeout(1000);
        };
        await shot('00-empty-home');
        await page.locator('#dsh-rail-toggle').tap();await page.waitForTimeout(350);
        await page.locator(`[data-row-key="session:${process.env.DSB_BROWSER_PHONE_SESSION}"]`).click();
        await page.locator('[class^="_markdown_"]').first().waitFor();await page.waitForTimeout(1500);
        if((await snapshot()).sidebar==='full')await page.locator('#dsh-rail-toggle').tap();
        await bottom();assert.equal((await snapshot()).composer,initial);
        await shot('01-initial');
        await page.locator('#dsh-rail-toggle').tap();await page.waitForTimeout(350);await shot('02-sidebar-open');
        await page.locator('#dsh-rail-toggle').tap();await page.waitForTimeout(350);
        await mode('hidden');await mode('shown');await bottom();await shot('03-composer-revealed');
        await swipe(180,620);await shot('04-scrolled-up');
        assert.equal((await snapshot()).atBottom,false);assert.equal((await snapshot()).composer,'hidden');
        await page.locator('#dsh-tobottom-keep').tap();await page.waitForTimeout(1800);await shot('05-after-return-bottom');
        assert.equal((await snapshot()).atBottom,true);assert.equal((await snapshot()).composer,'hidden');
        await page.locator('#dsh-tobottom-keep').tap();await bottom();await shot('06-composer-at-bottom');
        assert.equal((await snapshot()).composer,'shown');
        await mode('hidden');await swipe(600,200);assert.equal((await snapshot()).composer,'shown');
        for(const manual of [true,false]){
          await mode('shown');await page.waitForTimeout(1800);
          await page.evaluate(()=>{window.__dshHideQueued=0;window.__dshTicks=[];});
          await page.locator('[data-composer-input]').fill('Offline timer test, never sent');
          await page.waitForFunction(()=>window.__dshTicks.some(t=>t.value==='Offline timer test, never sent'),null,{polling:20,timeout:3000});
          // The controlled rich editor may restore a synthetic fill('') from its
          // draft state. Exercise actual deletion and confirm the DOM is empty.
          await page.locator('[data-composer-input]').press('ControlOrMeta+A');
          await page.locator('[data-composer-input]').press('Backspace');
          await page.waitForFunction(()=>document.querySelector('[data-composer-input]').textContent.trim()==='',null,{timeout:3000});
          try {await page.waitForFunction(()=>window.__dshHideQueued>0,null,{polling:20,timeout:3000});}
          catch(error){console.log(version,initial,manual,await page.evaluate(()=>window.__dshTicks));throw error;}
          if(manual)await page.locator('#dsh-tobottom-keep').evaluate(e=>{e.click();e.click();});
          await page.waitForTimeout(700);
          assert.equal((await snapshot()).composer,manual?'shown':'hidden');
          if(manual){await bottom();await shot('07-manual-toggle-protection');}
        }
        for(const s of Object.values(entry.scenes)){
          assert.equal(s.keep.width,34);assert.equal(s.keep.height,34);assert.equal(s.keep.opacity,'0.85');
          assert.equal(s.dot.display,'none');assert.equal(s.nativeVisible,0);
        }
        const ids=await page.locator('[id^="dsh-"]').evaluateAll(es=>es.map(e=>e.id));
        assert.equal(ids.length,new Set(ids).size);
        assert.deepEqual(errors,[]);assert.deepEqual(failedAssets,[]);
        pair.push(entry);results.push(entry);
      } finally {await context.close();}
    }
    assert.deepEqual(pair[0].scenes,pair[1].scenes,`phone A/B-prime mismatch, initial=${initial}`);
    console.log(`PASS phone A/B-prime 360x800 DPR3 initial=${initial}`);
  }
  fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(results,null,2));
}
