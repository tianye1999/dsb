import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Compile the actual lifecycle methods against controlled UI/network stand-ins.
// This is a concurrency regression test, not Android or bridge E2E acceptance.
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source = fs.readFileSync(process.argv[2] || path.join(root, 'src/com/dsb/assistant/OverlayService.java'), 'utf8');
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  if (from < 0 || to < 0) throw new Error(`source boundary missing: ${start}`);
  return source.slice(from, to);
}
// Android recovery helpers have their own actual-source suite. Keep this harness's readiness stand-in.
const goals = source.includes('    static void ensureAccessibility(')
  ? section('    private void runGoal(final String goal)', '    static void ensureAccessibility(')
    + section('    private void runGoalByAccessibility(', '    /** 通用设备上下文')
  : section('    private void runGoal(final String goal)', '    /** 通用设备上下文');
const methods = section('    private void stopTask()', '    private void setStatus(')
  + goals
  + section('    private static boolean integerField(', '    private void askDsh(')
  + section('    private void askDsh(', '    // ---------------- 系统语音识别');
const fields = section('    private final Handler main =', '    private long createdAt;');
const fixture = fs.readFileSync(path.join(root, 'tests/StopLifecycleTest.java'), 'utf8')
  .replace('/* PRODUCTION_FIELDS */', fields).replace('/* PRODUCTION_METHODS */', methods);
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-stop-'));
try {
  fs.writeFileSync(path.join(dir, 'StopLifecycleTest.java'), fixture);
  for (const [cmd, args] of [['javac', ['--release', '11', '-Xlint:all', '-Werror', '-encoding', 'UTF-8', '-d', dir, path.join(root, 'src/com/dsb/assistant/ActionReceipt.java'), path.join(root, 'tests/ActionReceiptTest.java'), path.join(dir, 'StopLifecycleTest.java')]],
      ['java', ['-cp', dir, 'ActionReceiptTest']], ['java', ['-cp', dir, 'StopLifecycleTest']]]) {
    const r = spawnSync(cmd, args, { stdio: 'inherit', timeout: 45000 });
    if (r.status !== 0) process.exitCode = r.status || 1;
    if (process.exitCode) break;
  }
} finally { fs.rmSync(dir, { recursive: true, force: true }); }
