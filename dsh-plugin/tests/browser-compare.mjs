// Opt-in headless acceptance against two isolated, already-running DSH instances.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {indexRows} from '../dsb-ui/index.js';

const {chromium} = await import(pathToFileURL(process.env.DSB_PLAYWRIGHT_MODULE));
const output = process.env.DSB_BROWSER_OUTPUT;
assert.match(output || '', /^\/tmp\/dsb-t19-[^]*$/);
fs.mkdirSync(output, {recursive: true});
const browser = await chromium.launch({headless: true,
  ...(process.env.DSB_BROWSER_EXECUTABLE ? {executablePath: process.env.DSB_BROWSER_EXECUTABLE} : {})});
const results = [];
try {
  if (process.env.DSB_BROWSER_PHONE_SESSION) {
    const {comparePhone} = await import('./browser-phone-compare.mjs');
    await comparePhone(browser, output);
  } else {
  for (const viewport of [{width: 390, height: 844}, {width: 1440, height: 1000}]) {
    for (const saved of [null, 'hidden']) {
      const pair = [];
      const versions = [['old', process.env.DSB_BROWSER_OLD_URL], ['new', process.env.DSB_BROWSER_NEW_URL]];
      if (process.env.DSB_BROWSER_LEGACY_PLUGIN_URL) versions.push(['legacy-plugin', process.env.DSB_BROWSER_LEGACY_PLUGIN_URL]);
      for (const [version, url] of versions) {
        assert.ok(url, 'two isolated DSH URLs are required');
        const context = await browser.newContext({viewport, locale: 'zh-CN', colorScheme: 'dark'});
        try {
          await context.addInitScript(value => {
            if (value) localStorage.setItem('dsh.composer.mode', value);
          }, saved);
          const page = await context.newPage();
          const errors = [], failedAssets = [];
          page.on('pageerror', error => errors.push(error.message));
          page.on('response', response => {
            if (/\/(assets|plugins)\//.test(response.url()) && response.status() >= 400) failedAssets.push(response.status());
          });
          await page.goto(url);
          await page.locator('[data-composer-input]').waitFor({state: 'attached'});
          const html = await page.content();
          assert.equal((html.match(/function openFeedback/g) || []).length, 1);
          const ids = await page.locator('style[id^="dsh-"], script[id^="dsh-"]').evaluateAll(nodes => nodes.map(node => node.id));
          assert.equal(new Set(ids).size, ids.length, 'no duplicate patch IDs');
          await page.waitForTimeout(4000);
          for (let i = 0; i < 12; i++) {
            const accept = page.getByRole('dialog').getByRole('button', {name: /^(继续|Continue)$/});
            const close = page.getByRole('dialog').getByRole('button', {name: /^(关闭|Close)$/});
            if (await accept.count() && await accept.isEnabled()) await accept.click({timeout: 1000});
            else if (await close.count()) await close.click({timeout: 1000});
            else if (!await page.getByRole('dialog').count()) break;
            await page.waitForTimeout(250);
          }
          assert.equal(await page.getByRole('dialog').count(), 0, 'onboarding must be dismissed before UI checks');
          const styles = () => page.evaluate(() => {
            const read = selector => {
              const element = document.querySelector(selector);
              if (!element) return null;
              const s = getComputedStyle(element);
              return Object.fromEntries(['display', 'fontSize', 'lineHeight', 'backgroundColor', 'color',
                'zIndex', 'marginLeft', 'width', 'colorScheme', 'paddingLeft'].map(key => [key, s[key]]));
            };
            return {sidebar: document.documentElement.dataset.dshSidebar,
              composer: document.documentElement.dataset.dshComposer,
              body: read('body'), rail: read('#dsh-rail-toggle'), column: read('.pI_x6G_sidebarCol'),
              center: read('.pI_x6G_centerCol'), input: read('[data-composer-input]'),
              seat: read('[data-composer-seat]'), dot: read('#dsh-composer-dot'), logo: read('.hHd-Xa_logoRow'),
              title: document.title};
          });
          const initial = await styles();
          const sidebar = async mode => {
            if (await page.evaluate(() => document.documentElement.dataset.dshSidebar) !== mode)
              await page.locator('#dsh-rail-toggle').click();
            await page.waitForTimeout(450);
            await page.waitForFunction(value => document.documentElement.dataset.dshSidebar === value, mode);
          };
          await sidebar('full');
          assert.equal(await page.locator('.hHd-Xa_brand').innerText(), '逗沙包');
          assert.ok(await page.locator('.hHd-Xa_brand img').evaluate(el => el.complete && el.naturalWidth > 0));
          const expanded = await styles();
          await sidebar('hidden');
          const states = [];
          for (let i = 0; i < 2; i++) {
            const previous = (await styles()).composer;
            await page.locator('#dsh-tobottom-keep').click();
            await page.waitForFunction(before => document.documentElement.dataset.dshComposer !== before, previous);
            states.push(await styles());
          }
          assert.ok(states.some(state => state.seat.display === 'none'));
          assert.ok(states.some(state => state.seat.display !== 'none'));
          await page.evaluate(() => {
            document.body.setAttribute('data-ds-dark-theme', '');
            document.documentElement.setAttribute('data-ds-dark-theme', '');
          });
          await page.waitForFunction(() => !document.body.hasAttribute('data-ds-dark-theme') &&
            !document.documentElement.hasAttribute('data-ds-dark-theme'));
          assert.equal((await styles()).body.colorScheme, 'light');
          // No model call: representative markdown nodes exercise the actual loaded CSS.
          const typography = await page.evaluate(() => {
            const sample = document.createElement('div');
            sample.className = 'EvIC1a_scroll';
            sample.innerHTML = '<div class="test_markdown_fixture"><p>Sample text</p><blockquote><p>Conclusion</p><code>code</code></blockquote><table><tbody><tr><td>Cell</td></tr></tbody></table></div>';
            document.body.append(sample);
            const result = [...sample.querySelectorAll('p,blockquote,code,td')].map(el => {
              const s = getComputedStyle(el);
              return [el.tagName, s.fontSize, s.lineHeight, s.color, s.backgroundColor];
            });
            sample.remove();
            return result;
          });
          const observer = await page.evaluate(text => {
            let callback, scans = 0, options;
            const frames = [];
            const doc = {title: 'DSB', documentElement: {}, addEventListener() {}, querySelectorAll() { scans++; return []; }};
            class Observer { constructor(fn) { callback = fn; } observe(_root, value) { options = value; } }
            new Function('document', 'window', 'MutationObserver', 'requestAnimationFrame', text)(doc, {}, Observer, fn => frames.push(fn));
            const before = scans;
            for (let i = 0; i < 100; i++) callback([]);
            const deferred = scans === before && frames.length === 1 && !options.characterData;
            frames.shift()();
            return deferred && scans > before && frames.length === 0;
          }, indexRows().find(row => row.html.includes('function openFeedback')).html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1]);
          assert.equal(observer, true);
          await sidebar('full');
          await page.getByRole('button', {name: '账号菜单', exact: true}).click();
          await page.getByRole('menuitem', {name: '意见反馈', exact: true}).waitFor();
          await page.screenshot({path: path.join(output, `${version}-${viewport.width}-${saved || 'fresh'}.png`)});
          const destinations = [];
          await context.route('https://github.com/**', route => {
            destinations.push(route.request().url());
            return route.fulfill({status: 200, contentType: 'text/html', body: '<title>Feedback test</title>'});
          });
          await page.getByRole('menuitem', {name: '意见反馈', exact: true}).click();
          await page.waitForURL('https://github.com/tianye1999/dsb/issues');
          assert.deepEqual(destinations, ['https://github.com/tianye1999/dsb/issues']);
          assert.deepEqual(errors, []);
          assert.deepEqual(failedAssets, []);
          pair.push({initial, expanded, states, typography, observer, destinations});
        } finally { await context.close(); }
      }
      assert.deepEqual(pair[1], pair[0], `old/new styles and behavior differ: ${viewport.width}/${saved}`);
      if (pair[2]) assert.deepEqual(pair[2], pair[0], `legacy + plugin differs: ${viewport.width}/${saved}`);
      results.push({viewport, saved, matched: true, ...pair[0]});
      console.log(`PASS old/new ${viewport.width}x${viewport.height} composer=${saved || 'fresh'}`);
    }
  }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2));
  }
} finally { await browser.close(); }
