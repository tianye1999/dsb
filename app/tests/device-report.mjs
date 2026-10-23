export function assertDeviceReport(output, report, runId) {
  if (!output.split(/\r?\n/).includes(`DSB_TEST_FINISH:${runId}:PASS`))
    throw new Error('Instrumentation did not finish successfully');
  if (!runId || report.runId !== runId) throw new Error('Stale or unidentified device report');
  if (report.phase !== 'PASS' || report.results.length !== 22 || report.results.some(r => r.ok !== true)
      || ['running', 'worker', 'cancelInFlight', 'cancelUnconfirmed'].some(k => report[k] !== false)) {
    throw new Error(`Device assertions failed: ${JSON.stringify(report)}`);
  }
}
