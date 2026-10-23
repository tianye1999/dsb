import {readFileSync} from 'node:fs';

export function apply(ctx) {
  const icon = JSON.parse(readFileSync(new URL('./build/icon.json', import.meta.url), 'utf8'));
  if (!icon.startsWith('data:image/png;base64,')) throw new Error('Build the DSB brand illustration first');
  ctx.on('webserver/index-inject', table => {
    table.push({kind: 'global', name: '__DSB_BRAND_ICON__', value: icon});
  });
}
