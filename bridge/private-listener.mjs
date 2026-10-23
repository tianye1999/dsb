import fs from 'node:fs';
import path from 'node:path';

// No unlink-on-start: an occupied or stale path is a fail-closed operator decision.
export function listenPrivate(server, socketPath, ready = () => {}) {
  if (!socketPath || !path.isAbsolute(socketPath) || Buffer.byteLength(socketPath) > 100)
    throw new Error('explicit short private socket path required');
  const parent = fs.lstatSync(path.dirname(socketPath));
  if (!parent.isDirectory() || parent.isSymbolicLink() || (parent.mode & 0o077) !== 0)
    throw new Error('socket parent must be private (0700)');
  process.umask(0o077);
  server.listen(socketPath, () => { fs.chmodSync(socketPath, 0o600); ready(); });
}
