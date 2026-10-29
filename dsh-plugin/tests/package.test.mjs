import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
import test from 'node:test';
import {apply, indexRows} from '../dsb-ui/index.js';

const dir = new URL('../', import.meta.url);
const manifest = JSON.parse(fs.readFileSync(new URL('package.json', dir)));
test('installable manifest and single lazy client entry', async () => {
  assert.equal(manifest.name, 'dsh-dsb');
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
  assert.notEqual(manifest.private, true);
  assert.equal(manifest.license, 'Apache-2.0');
  assert.equal(manifest.repository.directory, 'dsh-plugin');
  assert.deepEqual(manifest.peerDependencies, {'@deepseek-ai/dsh': '0.2.1-rc.2'});
  assert.match(manifest.description, /^[A-Za-z]/);
  assert.ok(fs.existsSync(new URL(manifest.dsh.bundle.patch, dir)));
  for (const id of ['dsb-brand', 'dsb-report', 'dsb-ui']) {
    assert.equal(typeof (await import(new URL(manifest.exports['./' + id], dir))).apply, 'function');
  }
  const entries = [];
  const source = fs.readFileSync(new URL(manifest.exports['./client'], dir), 'utf8');
  vm.runInNewContext(source, {window: {__ModuleLoader__: {load: row => entries.push(row)}}});
  assert.equal(entries.length, 1);
  assert.equal(entries[0].id, 'dsh-dsb');
  const client = entries[0].factory(name => {
    assert.ok(['react', 'react/jsx-runtime'].includes(name));
    return {};
  });
  const children = [];
  client.apply({plugin: child => children.push(child)});
  assert.equal(children.length, 2);
  assert.ok(children.every(child => typeof child.apply === 'function'));
});

const modules = process.env.DSH_TEST_MODULES;
test('actual DSH parses three cordis rows and skips the whole incompatible bundle', {skip: !modules}, async t => {
  const boot = await import(pathToFileURL(path.join(modules, '@deepseek-ai/dsh-app-boot/lib/index.js')));
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-t19-package-'));
  t.after(() => fs.rmSync(temp, {recursive: true, force: true}));
  fs.mkdirSync(path.join(temp, 'node_modules'), {recursive: true});
  const target = path.join(temp, 'node_modules/dsh-dsb');
  fs.cpSync(dir, target, {recursive: true});
  fs.writeFileSync(path.join(temp, 'package.json'), JSON.stringify({name: 'test-profile',
    dsh: {profile: {bundles: ['dsh-dsb']}}}));
  const anchor = path.join(modules, '@deepseek-ai/dsh/package.json');
  const profile = boot.loadProfileDirectory('dsh', temp, anchor);
  assert.equal(profile.skippedBundles.length, 0);
  const entries = boot.composeEntries(profile.layers.map(layer => layer.patches));
  assert.deepEqual(entries.map(({id, name, config}) => ({id, name, config})),
    ['dsb-brand', 'dsb-report', 'dsb-ui'].map(id => ({id, name: 'dsh-dsb', config: {component: id}})));
  const changed = {...manifest, peerDependencies: {'@deepseek-ai/dsh': '0.0.0'}};
  fs.writeFileSync(path.join(target, 'package.json'), JSON.stringify(changed));
  const denied = boot.loadProfileDirectory('dsh', temp, anchor);
  assert.equal(denied.skippedBundles.length, 1);
  assert.equal(denied.layers.length, 0);
});

test('actual DSH renderer is stable per response and preserves body style precedence', {skip: !modules}, async () => {
  const {renderIndexInjections} = await import(pathToFileURL(path.join(modules, '@deepseek-ai/dsh-host-webserver/lib/index.js')));
  const shell = fs.readFileSync(new URL('./fixtures/pristine-0.2.1-rc.2.html', import.meta.url), 'utf8');
  const first = renderIndexInjections(shell, indexRows());
  assert.equal(first, renderIndexInjections(shell, indexRows()));
  assert.ok(first.indexOf('dsh-composer-style') > first.indexOf('index-BxpfOGf3.css'));
  assert.equal((first.match(/<style id="dsh-/g) || []).length, 7);
  assert.equal((first.match(/function openFeedback/g) || []).length, 1);
});

test('actual renderer plus registered tap suppresses legacy duplicates and warns once', {skip: !modules}, async () => {
  const {renderIndexInjections} = await import(pathToFileURL(path.join(modules, '@deepseek-ai/dsh-host-webserver/lib/index.js')));
  let collect, tap;
  const warnings = [];
  apply({on(_event, fn) { collect = fn; }, effect(fn) { fn(); },
    webServer: {tapIndex(fn) { tap = fn; }}, logger: {warn(message) { warnings.push(message); }}});
  const rows = []; collect(rows);
  const legacy = fs.readFileSync(new URL('./fixtures/legacy-0.2.1-rc.2.html', import.meta.url), 'utf8');
  for (let i = 0; i < 2; i++) {
    const rendered = tap(renderIndexInjections(legacy, rows));
    assert.equal(rendered, renderIndexInjections(legacy, []));
    const ids = [...rendered.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
    assert.equal(new Set(ids).size, ids.length);
    assert.equal((rendered.match(/function openFeedback/g) || []).length, 1);
  }
  assert.equal(warnings.length, 1);
  const clean = fs.readFileSync(new URL('./fixtures/pristine-0.2.1-rc.2.html', import.meta.url), 'utf8');
  const rendered = renderIndexInjections(clean, rows);
  assert.equal(tap(rendered), rendered);
  for (const marker of ['<style id="dsh-rail-style"></style>', '<!-- DSH 侧栏 -->',
    '<script id="dsh-tobottom-keep-script"></script>', '<button id="dsh-tobottom-keep"></button>',
    '<script id="dsh-dark-enforce"></script>']) {
    const partial = clean.replace('</body>', marker + '</body>');
    assert.equal(tap(renderIndexInjections(partial, rows)), renderIndexInjections(partial, []));
  }
});
