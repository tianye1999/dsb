#!/usr/bin/env node
import fs from 'node:fs';
import readline from 'node:readline';
import { spawn } from 'node:child_process';

function record(event, extra = {}) {
  fs.appendFileSync(process.env.TEST_EVENTS, JSON.stringify({ event, pid: process.pid, ...extra }) + '\n');
}
record('start', { activeFile: process.env.DSB_ACTIVE_TASK_FILE, auditFile: process.env.DSB_AUDIT_FILE });
const descendant = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
record('descendant', { child: descendant.pid });
setInterval(() => {}, 1000);
if (process.argv.length > 4) {
  record('headless');
} else {
  const reply = value => process.stdout.write(JSON.stringify(value) + '\n');
  readline.createInterface({ input: process.stdin }).on('line', line => {
    const m = JSON.parse(line);
    record(m.method, { text: m.params?.contentBlocks?.[0]?.text });
    if (m.method === 'initialize' && process.env.TEST_INIT_HANG === '1') return;
    if (m.method === 'initialize' && process.env.TEST_INIT_EXIT === '1') process.exit(9);
    reply({ jsonrpc: '2.0', id: m.id, result: {} });
    if (m.method === 'session/prompt') {
      const text = m.params.contentBlocks[0].text;
      if (text === 'hang') {
        reply({ method: 'session.event', params: { sessionId: m.params.sessionId,
          event: { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'still working' }] } } } } });
        return;
      }
      reply({ method: 'session.event', params: { sessionId: m.params.sessionId,
        event: { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: process.env.TEST_REPLY_TEXT ?? 'READY' }] } } } } });
      reply({ method: 'session.event', params: { sessionId: m.params.sessionId, event: { type: 'turn/end' } } });
      if (text === 'reply-then-exit') setTimeout(() => process.exit(9), 50);
    }
  });
}
