import http from 'node:http';
export function unixFetch(socketPath, url, opts = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ socketPath, agent: false, path: new URL(url).pathname, method: opts.method || 'GET', signal: opts.signal }, res => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve({ ok: res.statusCode >= 200 && res.statusCode < 300,
        status: res.statusCode, json: async () => JSON.parse(Buffer.concat(chunks).toString()),
        text: async () => Buffer.concat(chunks).toString() }));
      res.on('error', reject);
    });
    req.on('error', reject); req.end(opts.body);
  });
}
