import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import test from 'node:test';
import {apply, indexRows} from '../dsb-ui/index.js';

const read = name => readFileSync(new URL('./fixtures/' + name, import.meta.url), 'utf8');
const legacy = read('legacy-0.2.1-rc.2.html');
const pristine = read('pristine-0.2.1-rc.2.html');
const blocks = [...legacy.matchAll(/<(style|script) id="([^"]+)">([\s\S]*?)<\/\1>/g)];

test('reference fixture bytes stay pinned to the captured old chain', () => {
  const hash = text => createHash('sha256').update(text).digest('hex');
  assert.equal(hash(pristine), '3a402b94efa1253d80666438e60ed89d0b5cf1fd07d24a2f59731623e9bd8a49');
  assert.equal(hash(legacy), '6b7ccf50a7939edb1ad1274774ffebd5b4aaa51100456f69a49d06cd6cd283a7');
  assert.equal(hash(read('phone-buttons.json')), '958eaf87997b0dbac0b8373b30acf3057654a5fe265c4e73aeb573a83e1f5d2f');
});

test('legacy blocks stay exact except the explicitly adopted phone button behavior', () => {
  const rows = indexRows();
  const phone = JSON.parse(read('phone-buttons.json'));
  assert.equal(rows.length, 15);
  assert.equal(blocks.length, 14);
  const expected = blocks.map(([,kind,id,text]) => ({kind,id,text}));
  expected.find(b => b.id === 'dsh-rail-style').text += '\n' + phone.rules.join('\n') + '\n';
  expected.find(b => b.id === 'dsh-composer-autohide').text = phone.autohide;
  const gesture = expected.splice(expected.findIndex(b => b.id === 'dsh-composer-gesture'), 1)[0];
  expected.splice(expected.findIndex(b => b.id === 'dsh-composer-autohide'), 0, gesture);
  expected.splice(expected.findIndex(b => b.id === 'dsh-light-enforce'), 0,
    {kind:'script', id:'dsh-tobottom-keep-script', text:phone.keep});
  assert.deepEqual(rows, expected.map(({kind,id,text}) => ({kind:'html',placement:'body',
    html:`<!--dsh-dsb:ui:start--><${kind} id="${id}">${text}</${kind}><!--dsh-dsb:ui:end-->`})));
  assert.equal(phone.rules.length, 5);
  assert.doesNotMatch(phone.rules.join('\n'), /zoom|Bao|font-size/);
  for (const {kind,text} of expected) {
    assert.doesNotMatch(text, /<\/(?:script|style)/i);
    if (kind === 'script') {
      const result = spawnSync(process.execPath, ['--check'], {input: text, encoding: 'utf8'});
      assert.equal(result.status, 0, result.stderr);
    }
  }
});

test('every response collects fresh, stable rows without mutating the disk shell', () => {
  let inject;
  apply({on(event, fn) { assert.equal(event, 'webserver/index-inject'); inject = fn; },
    effect(fn) { fn(); }, webServer: {tapIndex() {}}});
  const before = createHash('sha256').update(pristine).digest('hex');
  const first = [], second = [];
  inject(first); inject(second);
  assert.deepEqual(first, second);
  first[0].html = 'not shared';
  assert.notEqual(first[0].html, second[0].html);
  assert.equal(createHash('sha256').update(read('pristine-0.2.1-rc.2.html')).digest('hex'), before);
});

function originalMarkup(html) {
  const parts = [];
  let start = 0;
  // Extract untouched spans from the fixture, without reinterpreting joined text as HTML.
  for (const match of html.matchAll(/<(style|script) id="[^"]+">[\s\S]*?<\/\1\b[^>]*>|<!--[\s\S]*?--!?>/gi)) {
    parts.push(html.slice(start, match.index));
    start = match.index + match[0].length;
  }
  parts.push(html.slice(start));
  return parts.join('').replace(/>\s+</g, '><').trim();
}

test('original markup extraction handles mixed-case tags and permissive end tags', () => {
  for (const end of ['>', ' >', '\t\n bar>', ' foo="bar">']) {
    assert.equal(originalMarkup(`<main>before</main><ScRiPt id="patch">code</sCrIpT${end}<footer>after</footer>`),
      '<main>before</main><footer>after</footer>');
  }
});

test('original markup extraction preserves every span outside injected blocks and comments', () => {
  const shell = '<head><title>Original</title><script src="app.js"></script></head><body>text<div id="root"></div></body>';
  assert.equal(originalMarkup(shell), shell);
  assert.equal(originalMarkup('before<!-- note --!>after'), 'beforeafter');
  assert.equal(originalMarkup('<!<script id="patch">code</script>--original-->'), '<!--original-->');
  assert.equal(originalMarkup('<scr<script id="patch">code</script>ipt>original</script>'), '<script>original</script>');
});

test('legacy chain made no original-markup changes requiring tapIndex', () => {
  assert.equal(originalMarkup(legacy), originalMarkup(pristine));
});

test('narrow-screen effort label, rail layers and original privacy script remain intact', () => {
  assert.match(legacy, /white-space:nowrap !important; overflow:visible !important/);
  assert.match(legacy, /flex:0 0 auto !important/);
  assert.match(legacy, /z-index:120;/);
  assert.match(legacy, /z-index:100;/);
  assert.doesNotMatch(blocks.find(([, , id]) => id === 'dsh-rail-style')[3], /2147483000/);
  assert.doesNotMatch(legacy, /content:"Bao"/);
  assert.match(legacy, /https:\/\/github.com\/tianye1999\/dsb\/issues'/);
});
