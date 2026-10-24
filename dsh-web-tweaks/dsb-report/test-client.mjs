import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';

const source = fs.readFileSync(new URL('./client.js', import.meta.url), 'utf8');
function fixture(report, fetchResponse) {
  const state = [];
  let index = 0, effect;
  const React = {
    useState(initial) { const slot = index++; if (!(slot in state)) state[slot] = initial;
      return [state[slot], value => { state[slot] = value; }]; },
    useEffect(fn) { effect = fn; },
  };
  let plugin, card, injection;
  const window = { __ModuleLoader__: { load: entry => {
    plugin = entry.factory(name => name === 'react' ? React :
      { jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props}) });
  } }, fetch: fetchResponse || (async () => ({ status: 200, json: async () => ({ok: true, report}) })),
  setTimeout, clearTimeout };
  vm.runInNewContext(source, { window });
  plugin.apply({ slots: { inject: (_name, fn) => fn(), register: (spec, component) => {
    injection = spec.inject;
    card = component;
  } } });
  const render = (end = {seq: 4}) => { index = 0; return card({ ...injection('session-a'),
    turn: {turn: 1, status: 'closed', end}, seq: 4 }); };
  return { render, load: async (end = {seq: 4}) => { render(end); effect(); await new Promise(resolve => setImmediate(resolve)); },
    card, injection };
}
function textOf(node) {
  if (node == null) return '';
  if (typeof node === 'string') return node;
  if (Array.isArray(node)) return node.map(textOf).join(' ');
  return textOf(node.props?.children);
}

test('turn-tail card renders fixed fields and warnings but never free text', async () => {
  const SECRET = 'PRIVATE_CHAT_ORDER_COMMAND_COORDINATE';
  const f = fixture({counts: {dispatched_actions: 2, gate_refusals_by_reason: {G2B_ACK: 1},
    command_hook_refusals: 0, command_hook_shadow_would_block: 1,
    typesafe_unavailable_warnings: null, handoff_points: 1},
  coverage: {attribution_complete: false, lifecycle_valid: true, concurrent_turns: true,
    command_hook_linked: true},
  timeline: [{action_type: 'tap', result: 'refused', reason_code: 'G2B_ACK',
    note: SECRET, command: SECRET, coordinates: SECRET, explanation: SECRET}],
  chat: SECRET, screenshot: SECRET});
  await f.load();
  const card = f.render();
  assert.match(textOf(card), /做了 2 个动作/);
  assert.match(textOf(card), /拒绝了 1 次/);
  assert.match(textOf(card), /同时有其他会话运行/);
  assert.match(textOf(card), /观察模式本应拦截.*1 次/);
  assert.match(textOf(card), /TypeSafe 不可用.*未核实/);
  assert.doesNotMatch(textOf(card), /PRIVATE_CHAT_ORDER_COMMAND_COORDINATE/);
  const button = card.props.children.find(child => child?.type === 'button');
  button.props.onClick();
  assert.match(textOf(f.render()), /点击 · 已拒绝 · 无法确认危险按钮/);
});

test('pure question with no action has no execution report card', async () => {
  const f = fixture({counts: {dispatched_actions: 0, handoff_points: 0}, timeline: [], coverage: {}});
  await f.load();
  assert.equal(f.render(), null);
});

test('phone open failure and success show one action, no refusal, and hook gap only in details', async () => {
  const f = fixture({counts: {dispatched_actions: 1, gate_refusals_by_reason: {},
    command_hook_refusals: null, command_hook_shadow_would_block: null,
    typesafe_unavailable_warnings: null, handoff_points: 0},
  coverage: {lifecycle_valid: true, attribution_complete: true, command_hook_linked: false},
  timeline: [{action_type: 'open', result: 'failed', reason_code: 'OP_APP_NOT_FOUND'},
    {action_type: 'open', result: 'opened', reason_code: null}]});
  await f.load();
  const card = f.render();
  assert.match(textOf(card), /做了 1 个动作/);
  assert.match(textOf(card), /未见已关联的拒绝/);
  assert.doesNotMatch(textOf(card), /未核实|命令层审查/);
  card.props.children.find(child => child?.type === 'button').props.onClick();
  const detail = textOf(f.render());
  assert.match(detail, /打开应用 · 操作未成功 · 没找到这个应用/);
  assert.match(detail, /打开应用 · 已打开/);
  assert.match(detail, /命令层审查未覆盖本轮/);
  assert.doesNotMatch(detail, /TypeSafe 不可用|观察模式本应拦截/);
});

test('normal attribution has no generic warning; interruption is not concurrent', async () => {
  const report = { counts: { dispatched_actions: 1, gate_refusals_by_reason: {},
    command_hook_refusals: 0, handoff_points: 0 },
  coverage: { attribution_complete: true, lifecycle_valid: true, concurrent_turns: false,
    interrupted: false }, timeline: [{ action_type: 'tap', result: 'dispatched' }] };
  const f = fixture(report);
  await f.load();
  assert.doesNotMatch(textOf(f.render()), /归属不完整/);
  report.coverage.attribution_complete = false;
  report.coverage.interrupted = true;
  const interrupted = fixture(report);
  await interrupted.load();
  assert.match(textOf(interrupted.render()), /本轮异常中断/);
  assert.doesNotMatch(textOf(interrupted.render()), /同时有其他会话/);
});

test('query failure degrades to a retryable notice', async () => {
  const f = fixture(null, async () => ({ status: 503 }));
  await f.load();
  assert.match(textOf(f.render()), /执行报告生成失败/);
  assert.match(textOf(f.render()), /重试/);
});

test('client vocabulary is generated from the canonical allowlist', () => {
  execFileSync(process.execPath, [new URL('./generate-vocabulary.mjs', import.meta.url).pathname, '--check']);
});

test('turn-tail fallback seq loads when the closing event is absent', async () => {
  const f = fixture({counts: {dispatched_actions: 1, gate_refusals_by_reason: {},
    command_hook_refusals: 0, handoff_points: 0}, coverage: {lifecycle_valid: true,
    attribution_complete: true}, timeline: [{action_type: 'tap', result: 'dispatched'}]});
  await f.load(null);
  assert.match(textOf(f.render(null)), /做了 1 个动作/);
});
