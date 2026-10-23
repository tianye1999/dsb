import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = process.env.DSB_TEST_SOURCE_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-trusted-files-'));
try {
  const source = path.join(root, 'app/src/com/dsb/assistant/TrustedFiles.java');
  const test = fileURLToPath(new URL('./TrustedFilesTest.java', import.meta.url));
  for (const [command, args] of [
    [process.env.JAVAC || 'javac', ['-encoding', 'UTF-8', '-d', dir, source, test]],
    [process.env.JAVA || 'java', ['-cp', dir, 'com.dsb.assistant.TrustedFilesTest']],
  ]) {
    const result = spawnSync(command, args, { encoding: 'utf8', timeout: 60000 });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    process.stdout.write(result.stdout);
  }
} finally {
  fs.rmSync(dir, { recursive: true, force: true });
}
