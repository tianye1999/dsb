import { test } from 'node:test';
import assert from 'node:assert/strict';
import { assertDeviceReport } from './device-report.mjs';
const output = 'DSB_TEST_FINISH:current:PASS\n[]';
const pass = () => ({ runId: 'current', phase: 'PASS', results: Array.from({ length: 22 }, () => ({ ok: true })),
  running: false, worker: false, cancelInFlight: false, cancelUnconfirmed: false });
test('current complete report passes', () => assertDeviceReport(output, pass(), 'current'));
test('stale report with the same test count fails', () => {
  assert.throws(() => assertDeviceReport(output, { ...pass(), runId: 'previous' }, 'current'));
});
test('process crash cannot reuse a passing report', () => {
  assert.throws(() => assertDeviceReport('INSTRUMENTATION_RESULT: shortMsg=Process crashed.\nINSTRUMENTATION_CODE: 0', pass(), 'current'));
});
test('missing run identity fails', () => {
  assert.throws(() => assertDeviceReport(output, { ...pass(), runId: undefined }, 'current'));
});
test('wrong count, failed case, or active worker fails', () => {
  for (const report of [{ ...pass(), results: [] }, { ...pass(), results: [...pass().results.slice(1), { ok: false }] },
    { ...pass(), worker: true }, { ...pass(), phase: 'FAIL' }]) {
    assert.throws(() => assertDeviceReport(output, report, 'current'));
  }
});
