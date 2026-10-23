const uuid = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const counts = {store:9, prepare:3, verify:8, preflight:1, prepare_bridge:3, verify_unknown:7, contract:8};

export function assertRecoveryReport(output, report, runId, phase) {
  if (!uuid.test(runId) || !output.split(/\r?\n/).includes(`RECOVERY_TEST_FINISH:${runId}:${phase}:PASS`))
    throw new Error('Missing exact successful recovery finish marker');
  if (report.runId !== runId || report.phase !== phase || report.ok !== true
      || !Number.isInteger(report.pid) || report.pid <= 0 || !Array.isArray(report.checks)
      || report.checks.some(x => typeof x !== 'string' || !x)
      || new Set(report.checks).size !== report.checks.length)
    throw new Error('Invalid or stale recovery report');
  if (phase === 'cleanup' ? ![0,2,3].includes(report.checks.length)
      : phase === 'cleanup_unknown' ? ![0,3,4].includes(report.checks.length)
      : !(phase in counts) || report.checks.length !== counts[phase])
    throw new Error('Incomplete recovery assertions');
  if (['prepare','verify','prepare_bridge','verify_unknown'].includes(phase) && !uuid.test(report.taskId))
    throw new Error('Missing persisted task identity');
  if (phase === 'contract' && (!Array.isArray(report.cases) || report.cases.length !== 5
      || report.cases.some((c,i) => c.scenario !== ['completed','failed','refused','stopped','unknown'][i]
          || !uuid.test(c.taskId) || typeof c.status !== 'string')
      || new Set(report.cases.map(c=>c.taskId)).size !== 5))
    throw new Error('Missing contract cases or unique persisted identities');
}

export function assertRestart(before, after) {
  if (!Number.isInteger(before.pid) || !Number.isInteger(after.pid) || before.pid === after.pid
      || !uuid.test(before.taskId) || before.taskId !== after.taskId)
    throw new Error('No evidence of a fresh process recovering the same task');
}
