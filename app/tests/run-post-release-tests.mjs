import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Compile the production file-I/O and speech-cleaning methods, without Android or devices.
const app = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const overlay = fs.readFileSync(path.join(app, 'src/com/dsb/assistant/OverlayService.java'), 'utf8');
const tts = fs.readFileSync(path.join(app, 'src/com/dsb/assistant/TtsSpeaker.java'), 'utf8');
for (const [method, nonce] of [['observeGraph', 'nonceI'], ['dispatchDisplayGesture', 'nonce']]) {
  assert.match(overlay, new RegExp(`private void ${method}\\([^]*?\\{\\s*if \\(!validNonce\\(${nonce}\\)\\)`));
}
assert.match(overlay, /try \(java\.io\.OutputStream fo = openOutput\(new java\.io\.File\(dir, jpgName\)\)\)/);
const io = overlay.slice(overlay.indexOf('    private void writeAtomically('),
  overlay.indexOf('    /** 只保留最近 OBS_KEEP_FILES'));
const nonce = overlay.slice(overlay.indexOf('    private static boolean validNonce('),
  overlay.indexOf('    private static String nonceForLog('));
const clean = tts.slice(tts.indexOf('    static String clean('), tts.lastIndexOf('}'));
assert.ok(io.includes('private void copyFile(') && clean.includes('return s.trim();'));
const fixture = fs.readFileSync(path.join(app, 'tests/PostReleaseHardeningTest.java'), 'utf8')
  .replace('/* PRODUCTION_IO */', io + nonce).replace('/* PRODUCTION_CLEAN */', clean);
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-post-release-'));
try {
  const file = path.join(dir, 'PostReleaseHardeningTest.java');
  fs.writeFileSync(file, fixture);
  for (const [cmd, args] of [
    ['javac', ['-encoding', 'UTF-8', '-d', dir, file]],
    ['java', ['-cp', dir, 'PostReleaseHardeningTest', ...process.argv.slice(2)]],
  ]) {
    const result = spawnSync(cmd, args, { encoding: 'utf8', timeout: 15000 });
    assert.equal(result.status, 0, result.stdout + result.stderr + (result.error || ''));
    process.stdout.write(result.stdout);
  }
} finally {
  fs.rmSync(dir, { recursive: true, force: true });
}
