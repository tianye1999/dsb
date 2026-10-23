import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';

// Derived only from the public App illustration, never a screenshot.
const source = process.argv[2] || new URL('../../app/res/mipmap-xxxhdpi/ic_launcher_foreground.png', import.meta.url);
const icon = readFileSync(source);
if (icon.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') throw new Error('PNG illustration required');
mkdirSync(new URL('./build/', import.meta.url), {recursive: true});
writeFileSync(new URL('./build/icon.json', import.meta.url), JSON.stringify('data:image/png;base64,' + icon.toString('base64')));
console.log('DSB brand illustration prepared');
