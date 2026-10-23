// 固定版本的上游鉴权契约；不启动模型、不建立用户会话。
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const root = process.env.DSB_DSH_TEST_ROOT;
assert.ok(root, 'DSB_DSH_TEST_ROOT must name the CI installation');
const require = createRequire(resolve(root, 'package.json'));
const release = JSON.parse(readFileSync(new URL('../release.json', import.meta.url)));
assert.equal(require('@deepseek-ai/dsh/package.json').version, release.dsh_version);
const { HostConnectionService } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-client-connection')));

function connection(authenticated = false) {
  return Object.assign(Object.create(HostConnectionService.prototype), {
    owner: { get: () => ({ host: '127.0.0.1', protocol: 'http:' }) },
    trustedHosts: [], disposed: false, peers: new Map(), browserPeer: {},
    browserAuth: { isAuthenticated: () => authenticated, authenticateApi: () => undefined },
  });
}
const request = (host = '127.0.0.1:3080', origin = undefined) => ({
  headers: { host, ...(origin ? { origin } : {}) }, url: '/recap/api',
});
test('unauthenticated local route is rejected', () => {
  assert.equal(connection().requestRejection(request()), 401);
});
test('untrusted Host is rejected even with browser authentication', () => {
  assert.equal(connection(true).requestRejection(request('untrusted.invalid')), 403);
});
test('cross-origin request is rejected', () => {
  assert.equal(connection(true).requestRejection(request('127.0.0.1:3080', 'http://untrusted.invalid')), 403);
});
test('authenticated same-origin request is admitted', () => {
  assert.equal(connection(true).requestRejection(request('127.0.0.1:3080', 'http://127.0.0.1:3080')), undefined);
});
