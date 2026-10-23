import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { assertRecoveryReport, assertRestart } from './recovery-report.mjs';

const id = 'e6318aa2-a950-4baf-af65-f9f51a90e29e';
const output = `RECOVERY_TEST_FINISH:${id}:prepare:PASS\n`;
const report = { runId: id, phase: 'prepare', ok: true, pid: 123,
  taskId: id, checks: ['a', 'b', 'c'] };
test('accept current phase with exact completion marker', () => {
  assert.doesNotThrow(() => assertRecoveryReport(output, report, id, 'prepare'));
});
test('reject stale, incomplete, failed and malformed reports', () => {
  for (const delta of [{runId:'old'}, {phase:'verify'}, {ok:false}, {pid:0},
    {pid:'123'}, {checks:[]}, {checks:['a','a','a']}, {taskId:''}]) {
    assert.throws(() => assertRecoveryReport(output, {...report,...delta}, id, 'prepare'));
  }
  for (const text of ['', output.replace(':PASS', ':FAIL'), 'prefix'+output])
    assert.throws(() => assertRecoveryReport(text, report, id, 'prepare'));
});
test('restart evidence requires different processes and same task identity', () => {
  const next={...report,pid:124};
  assert.doesNotThrow(() => assertRestart(report,next));
  assert.throws(() => assertRestart(report,report));
  assert.throws(() => assertRestart(report,{...next,taskId:'different'}));
});
test('cross-layer phases require complete counts and original identity', () => {
  for (const [phase, size] of [['preflight',1],['prepare_bridge',3],['verify_unknown',7],['cleanup_unknown',4]]) {
    const current={...report,phase,checks:Array.from({length:size},(_,i)=>'check-'+i)};
    const marker=`RECOVERY_TEST_FINISH:${id}:${phase}:PASS\n`;
    assert.doesNotThrow(() => assertRecoveryReport(marker,current,id,phase));
    assert.throws(() => assertRecoveryReport(marker,{...current,ok:false},id,phase));
    if (phase!=='cleanup_unknown')
      assert.throws(() => assertRecoveryReport(marker,{...current,checks:[]},id,phase));
    if (['prepare_bridge','verify_unknown'].includes(phase))
      assert.throws(() => assertRecoveryReport(marker,{...current,taskId:''},id,phase));
  }
});
test('contract requires all five unique task cases and eight checks', () => {
  const phase='contract', marker=`RECOVERY_TEST_FINISH:${id}:${phase}:PASS\n`;
  const current={runId:id,phase,ok:true,pid:123,checks:Array.from({length:8},(_,i)=>'check-'+i),
    cases:['completed','failed','refused','stopped','unknown'].map(scenario=>({scenario,taskId:randomUUID(),status:'observed'}))};
  assert.doesNotThrow(()=>assertRecoveryReport(marker,current,id,phase));
  for(const delta of [{checks:current.checks.slice(1)},{cases:current.cases.slice(1)},
    {cases:current.cases.map(c=>({...c,taskId:id}))},{cases:current.cases.map(c=>({...c,scenario:'completed'}))}])
    assert.throws(()=>assertRecoveryReport(marker,{...current,...delta},id,phase));
});
