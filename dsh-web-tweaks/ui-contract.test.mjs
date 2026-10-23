import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('brand uses declared DSH slots and the App illustration, not official artwork', async () => {
  const built = spawnSync(process.execPath, [new URL('./dsb-brand/build.mjs', import.meta.url).pathname]);
  assert.equal(built.status, 0, built.stderr?.toString());
  const {apply} = await import('./dsb-brand/index.js');
  const injections = [];
  apply({on: (name, fn) => { assert.equal(name, 'webserver/index-inject'); fn(injections); }});
  const icon = injections[0].value;
  assert.deepEqual(Buffer.from(icon.split(',')[1], 'base64'),
    readFileSync(new URL('../app/res/mipmap-xxxhdpi/ic_launcher_foreground.png', import.meta.url)));
  let plugin;
  vm.runInNewContext(readFileSync(new URL('./dsb-brand/client.js', import.meta.url), 'utf8'), {
    window: {__DSB_BRAND_ICON__: icon, __ModuleLoader__: {load: entry => {
      plugin = entry.factory(name => { assert.equal(name, 'react/jsx-runtime'); return {jsx: (type, props) => ({type, props})}; });
    }}}
  });
  const rows = new Map();
  const slots = {
    inject: (_name, fn) => { const result = fn(); if (result?.next) for (const _ of result) {} },
    register: ({name}, render) => { rows.set(name, render); }
  };
  plugin.apply({slots});
  assert.equal(rows.get('sidebar.brand.name')().props.children, '逗沙包');
  const mark = rows.get('sidebar.brand.mark')({size: 24});
  assert.equal(mark.props.children.type, 'img');
  assert.equal(mark.props.children.props.src, icon);
});

test('product observer coalesces streaming mutations into one animation frame', () => {
  const result = spawnSync('python3', ['-c', 'from patch_privacy_ui import SCRIPT; print(SCRIPT)'],
    {cwd: new URL('.', import.meta.url), encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  const body = result.stdout.match(/<script[^>]*>([\s\S]*)<\/script>/)[1];
  let callback, options, scans = 0;
  const frames = [];
  vm.runInNewContext(body, {
    document: {title: 'DSB', documentElement: {}, addEventListener() {}, querySelectorAll() { scans++; return []; }},
    window: {}, requestAnimationFrame: fn => { frames.push(fn); },
    MutationObserver: class {constructor(fn) { callback = fn; } observe(_root, value) { options = value; }}
  });
  assert.notEqual(options.characterData, true);
  const initial = scans;
  for (let i = 0; i < 100; i++) callback([]);
  assert.equal(scans, initial);
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.ok(scans > initial);
  assert.equal(frames.length, 0);
});

test('feedback capture uses a clean same-view GitHub URL and leaves login alone', () => {
  const result = spawnSync('python3', ['-c', 'from patch_privacy_ui import SCRIPT; print(SCRIPT)'],
    {cwd: new URL('.', import.meta.url), encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  const body = result.stdout.match(/<script[^>]*>([\s\S]*)<\/script>/)[1];
  const listeners = {};
  let destination, blocked = 0;
  class Element {
    constructor(text) { this.textContent = text; }
    closest(selector) { assert.equal(selector, '[role="menuitem"]'); return this; }
  }
  const heading = {textContent: 'DeepSeek Harness'};
  const document = {
    title: 'DeepSeek Harness', documentElement: {},
    addEventListener: (name, fn, capture) => { assert.equal(capture, true); listeners[name] = fn; },
    querySelectorAll: selector => selector.includes(' p') ? [] : [heading]
  };
  vm.runInNewContext(body, {document, Element,
    MutationObserver: class {observe() {}}, window: {location: {assign: url => {destination = url; }}}});
  function event(text) { return {target: new Element(text), preventDefault: () => {blocked++;}, stopImmediatePropagation: () => {blocked++;}}; }
  listeners.click(event('登录'));
  assert.equal(destination, undefined);
  for (const label of ['意见反馈', 'Feedback']) {
    listeners.click(event(label));
    assert.equal(destination, 'https://github.com/tianye1999/dsb/issues');
  }
  assert.equal(blocked, 4);
  listeners.keydown({...event('意见反馈'), type: 'keydown', key: 'Escape'});
  assert.equal(blocked, 4);
  listeners.keydown({...event('意见反馈'), type: 'keydown', key: 'Enter'});
  assert.equal(blocked, 6);
  assert.equal(destination, 'https://github.com/tianye1999/dsb/issues');
  assert.equal(heading.textContent, '逗沙包 · 基于 DeepSeek Harness');
  assert.equal(document.title, heading.textContent);
});
