import {readFileSync} from 'node:fs';

export const name = 'dsb-ui';
const blocks = JSON.parse(readFileSync(new URL('./blocks.json', import.meta.url), 'utf8'));

// Body styles follow the application's stylesheets, in the legacy cascade order.
// Bodies match the frozen chain except the documented, captured phone controls.
export function indexRows() {
  return blocks.map(({kind, id, text}) => ({kind: 'html', placement: 'body',
    html: `<!--dsh-dsb:ui:start--><${kind} id="${id}">${text}</${kind}><!--dsh-dsb:ui:end-->`}));
}

export function legacyGuard(warn) {
  let warned = false;
  return html => {
    const original = html.replace(/<!--dsh-dsb:ui:start-->[\s\S]*?<!--dsh-dsb:ui:end-->/g, '');
    const legacy = /\bid\s*=\s*["']dsh-(?:composer|repair|font|content|chat-bg|privacy-ui|rail|hide-tabs|light-enforce|dark-enforce|tobottom-keep)[\w-]*["']|<!--\s*(?:dsh-chat-bg|DSH 侧栏)/i;
    if (!legacy.test(original)) return html;
    if (!warned) {
      warned = true;
      warn('[dsb-ui] Legacy disk patches detected; skipping UI injections. Stop DSH and run restore-index.py.');
    }
    return original;
  };
}

export function apply(ctx) {
  ctx.on('webserver/index-inject', table => table.push(...indexRows()));
  ctx.effect(() => ctx.webServer.tapIndex(legacyGuard(message => ctx.logger.warn(message))),
    'dsb-ui: suppress injections on legacy patched pages');
}
