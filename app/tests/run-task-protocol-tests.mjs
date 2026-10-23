import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source = fs.readFileSync(path.join(root, 'src/com/dsb/assistant/DshClient.java'), 'utf8');
const start = source.indexOf('    static String taskReceiptError(');
const end = source.indexOf('    public static Result post(', start);
if (start < 0 || end < start) throw new Error('actual task receipt validator missing');
const fixture = fs.readFileSync(path.join(root, 'tests/TaskProtocolTest.java'), 'utf8')
  .replace('/* PRODUCTION_VALIDATOR */', source.slice(start, end));
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-task-protocol-'));
try {
  const file = path.join(dir, 'TaskProtocolTest.java');
  fs.writeFileSync(file, fixture);
  for (const [cmd, args] of [['javac', ['--release', '11', '-Xlint:all', '-Werror', '-encoding', 'UTF-8', '-d', dir, file]],
    ['java', ['-cp', dir, 'TaskProtocolTest']]]) {
    const result = spawnSync(cmd, args, { stdio: 'inherit', timeout: 30000 });
    if (result.status !== 0) { process.exitCode = result.status || 1; break; }
  }
} finally { fs.rmSync(dir, { recursive: true, force: true }); }
