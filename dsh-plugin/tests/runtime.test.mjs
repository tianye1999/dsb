import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import test from 'node:test';

const repo = new URL('../../', import.meta.url);
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-t19-runtime-test-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  fs.cpSync(new URL('dsh-plugin/', repo), path.join(root, 'dsh-plugin'), {recursive: true});
  const env = {...process.env, HOME: path.join(root, 'home')};
  for (const name of ['BRIDGE_AGENT_DIR', 'DSB_AUDIT_FILE', 'DSB_ACTIVE_TASK_FILE']) delete env[name];
  return {root, env};
}
function run(root, env, code) {
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', code],
    {cwd: root, env, encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr || result.stdout);
}

test('bad report environment fails only that component and warns, brand and ui still load', t => {
  const {root, env} = fixture(t);
  for (const state of ['missing-directory', 'missing-vocabulary']) {
    const agent = path.join(root, state);
    if (state === 'missing-vocabulary') fs.mkdirSync(agent);
    run(root, {...env, BRIDGE_AGENT_DIR: agent}, `
      import assert from 'node:assert/strict';
      const {apply} = await import('./dsh-plugin/index.js');
      const warnings = [], events = [], taps = [];
      const ctx = {on(name) { events.push(name); }, effect(fn) { fn(); },
        webServer: {tapIndex(fn) { taps.push(fn); }}, logger: {warn(...args) { warnings.push(args); }}};
      await apply(ctx, {component:'dsb-brand'});
      await apply(ctx, {component:'dsb-report'});
      await apply(ctx, {component:'dsb-ui'});
      assert.equal(events.length, 2);
      assert.equal(taps.length, 1);
      assert.equal(warnings.length, 1);
      assert.match(warnings[0][0], /dsb-report/);
      assert.ok(warnings[0][1] instanceof Error);
      assert.match(warnings[0][1].message, /ENOENT/);
    `);
  }
});

test('report apply failure logs the original registration error rather than blaming environment', t => {
  const {root,env} = fixture(t);
  fs.writeFileSync(path.join(root,'dsh-plugin/dsb-report/index.js'),
    'export function apply() { throw new Error("synthetic route registration failure"); }');
  run(root,env, `
    import assert from 'node:assert/strict';
    const {apply} = await import('./dsh-plugin/index.js');
    const warnings=[];
    await apply({logger:{warn(...args){warnings.push(args);}}}, {component:'dsb-report'});
    assert.equal(warnings.length,1);
    assert.equal(warnings[0][1].message,'synthetic route registration failure');
  `);
});

test('standalone report writes audit and ledgers only beneath writable home', t => {
  const {root, env} = fixture(t);
  const agent = path.join(root, 'dsh-plugin/runtime/agent');
  const before = fs.readdirSync(agent);
  run(root, env, `
    import assert from 'node:assert/strict';
    import fs from 'node:fs';
    import path from 'node:path';
    const {createWebReport} = await import('./dsh-plugin/dsb-report/index.js');
    const events = {}, cleanup = [];
    const result = createWebReport({on(name, fn) {events[name] = fn;},
      effect(fn) {cleanup.push(fn());}, webServer:{register() {return () => {};}}, logger:{warn() {}}});
    assert.equal(result.store.audit, path.join(process.env.HOME,'.dsh-phone/skills/safe_tap_audit.jsonl'));
    assert.ok(result.store.script.endsWith('/runtime/agent/task_report.py'));
    events['session/event']({id:'synthetic'}, {type:'turn/start',data:{turn:0}});
    assert.ok(fs.existsSync(result.store.audit));
    assert.ok(fs.existsSync(path.join(process.env.HOME,'.dsh-phone/reports/web-turns.json')));
    for (const fn of cleanup) fn();
  `);
  assert.deepEqual(fs.readdirSync(agent), before);
});

test('build rejects a non-PNG illustration even when bytes could be encoded', t => {
  const {root, env} = fixture(t);
  for (const dir of ['agent', 'bridge', 'dsh-web-tweaks'])
    fs.cpSync(new URL(dir, repo), path.join(root, dir), {recursive: true});
  fs.copyFileSync(new URL('LICENSE', repo), path.join(root, 'LICENSE'));
  const image = path.join(root, 'app/res/mipmap-xxxhdpi/ic_launcher_foreground.png');
  fs.mkdirSync(path.dirname(image), {recursive: true});
  fs.writeFileSync(image, 'not a PNG');
  const result = spawnSync(process.execPath, ['dsh-plugin/build.mjs', '--check'], {cwd: root, env, encoding: 'utf8'});
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Brand illustration must be PNG/);
});

test('published runtime includes pinned restoration shell and standalone helper', t => {
  const {root, env} = fixture(t);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'dsh-plugin/package.json')));
  assert.ok(manifest.files.includes('runtime/'));
  const shell = fs.readFileSync(path.join(root, 'dsh-plugin/runtime/pristine-0.2.1-rc.2.html'));
  assert.deepEqual(shell, fs.readFileSync(new URL('dsh-plugin/tests/fixtures/pristine-0.2.1-rc.2.html', repo)));
  const target = path.join(root, 'index.html');
  fs.writeFileSync(target, shell);
  const result = spawnSync('python3', ['dsh-plugin/runtime/restore-index.py', '--index', target],
    {cwd: root, env, encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), 'already pristine');
});
