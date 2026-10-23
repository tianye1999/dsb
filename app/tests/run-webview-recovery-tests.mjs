import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Execute the actual recovery methods/callbacks with deterministic UI, worker and clock stand-ins.
// This does not emulate Chromium rendering or Android UI-thread/lifecycle delivery.
const app = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
let source;
if (process.argv[2] === '--baseline') {
  const baseline = spawnSync('git', ['show', 'HEAD:app/src/com/dsb/assistant/MainActivity.java'],
    { cwd: path.dirname(app), encoding: 'utf8', timeout: 10000 });
  assert.equal(baseline.status, 0, baseline.stderr);
  source = baseline.stdout;
} else {
  source = fs.readFileSync(process.argv[2] || path.join(app, 'src/com/dsb/assistant/MainActivity.java'), 'utf8');
}
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to >= 0, `source boundary missing: ${start}`);
  return source.slice(from, to);
}
function blockAt(start) {
  const open = source.indexOf('{', start);
  let depth = 0, quote = null, lineComment = false, blockComment = false;
  for (let i = open; i < source.length; i++) {
    const c = source[i], next = source[i + 1];
    if (lineComment) { if (c === '\n') lineComment = false; continue; }
    if (blockComment) { if (c === '*' && next === '/') { blockComment = false; i++; } continue; }
    if (quote) { if (c === '\\') i++; else if (c === quote) quote = null; continue; }
    if (c === '/' && next === '/') { lineComment = true; i++; continue; }
    if (c === '/' && next === '*') { blockComment = true; i++; continue; }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return { body: source.slice(open + 1, i), end: i + 1 };
  }
  throw new Error('unclosed source block');
}
const callbacksAt = source.indexOf('web.setWebViewClient(new WebViewClient()');
assert.ok(callbacksAt >= 0, 'actual WebView callbacks missing');
const callbacks = blockAt(callbacksAt).body;
const retryAt = source.indexOf('retryBtn.setOnClickListener(');
assert.ok(retryAt >= 0, 'actual retry button listener missing');
const retryClick = blockAt(retryAt).body;
const fetchAt = source.indexOf('    private String fetchUrl()');
const fetchEnd = blockAt(fetchAt).end;
const methods = section('    private void showSplash(', '    private boolean handleUrl(')
  .replace(source.slice(fetchAt, fetchEnd), '');
const optionalMethod = name => {
  const match = new RegExp(`    @Override (?:protected|public) void ${name}\\([^)]*\\) \\{`).exec(source);
  if (!match) return '';
  return source.slice(match.index, blockAt(match.index).end);
};
const fields = section('    private String loadedUrl;', '    /** 网页里的');
const fixture = fs.readFileSync(path.join(app, 'tests/WebViewRecoveryTest.java'), 'utf8')
  .replace('/* PRODUCTION_FIELDS */', fields)
  .replace('/* PRODUCTION_METHODS */', methods)
  .replace('/* PRODUCTION_CALLBACKS */', callbacks)
  .replace('/* PRODUCTION_RETRY_CLICK */', retryClick)
  .replace('/* PRODUCTION_LIFECYCLE */', optionalMethod('onDestroy') + optionalMethod('onResume'))
  .replaceAll('System.currentTimeMillis()', 'Clock.now')
  .replaceAll('SystemClock.elapsedRealtime()', 'Clock.now');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-webview-recovery-'));
try {
  const file = path.join(dir, 'WebViewRecoveryTest.java');
  fs.writeFileSync(file, fixture);
  for (const [cmd, args] of [['javac', ['--release', '11', '-Xlint:all', '-Werror', '-encoding', 'UTF-8', '-d', dir, file]],
    ['java', ['-cp', dir, 'WebViewRecoveryTest']]]) {
    const result = spawnSync(cmd, args, { encoding: 'utf8', timeout: 30000 });
    process.stdout.write(result.stdout);
    process.stderr.write(result.stderr);
    if (result.status !== 0) { process.exitCode = result.status || 1; break; }
  }
} finally { fs.rmSync(dir, { recursive: true, force: true }); }
