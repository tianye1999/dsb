import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const tests = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(process.argv[2] || path.join(tests, '../app/OverlayService.java'), 'utf8');
const start = source.indexOf('    void handleCommand(String cmd) {');
const end = source.indexOf('        // 2026-09-20 改：', start);
if (start < 0 || end < 0) throw new Error('Command admission source boundary missing');
// Replace downstream dispatch with a counter; the admission code is the real source.
const admission = source.slice(start, end) + '        fallthrough++;\n    }\n';
const fixture = fs.readFileSync(path.join(tests, 'LegacyEntryTest.java'), 'utf8')
  .replace('/* COMMAND_ADMISSION */', admission);
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-legacy-entry-'));
try {
  fs.writeFileSync(path.join(dir, 'LegacyEntryTest.java'), fixture);
  for (const [cmd, args] of [['javac', ['--release', '11', '-Xlint:all', '-Werror', '-encoding', 'UTF-8', '-d', dir, path.join(dir, 'LegacyEntryTest.java')]],
      ['java', ['-cp', dir, 'LegacyEntryTest']]]) {
    const r = spawnSync(cmd, args, { stdio: 'inherit', timeout: 45000 });
    if (r.error) console.error(r.error);
    if (r.status !== 0) { process.exitCode = r.status || 1; break; }
  }
} finally { fs.rmSync(dir, { recursive: true, force: true }); }
