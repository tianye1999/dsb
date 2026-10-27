import fs from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { WebTurns } from './web-turns.mjs';

export const name = 'dsb-report';
export const inject = ['webServer', 'connection'];
export const ROUTE = '/dsb-report/api';
export const EXPORT_ROUTE = '/dsb-report/api/export';
const MAX_REPORT_BYTES = 512 * 1024;
const MAX_EXPORT_BYTES = 22 * 1024 * 1024;

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const bridge = fs.existsSync(path.join(root, 'bridge/task-report-store.mjs')) ?
  path.join(root, 'bridge') : path.join(root, 'dsh-bridge');
const { TaskReportStore } = await import(pathToFileURL(path.join(bridge, 'task-report-store.mjs')));
const home = process.env.HOME || '/root';
const agent = process.env.BRIDGE_AGENT_DIR ||
  (fs.existsSync(path.join(home, '.dsh-phone/skills/task_report.py')) ?
    path.join(home, '.dsh-phone/skills') : path.join(root, 'agent'));
const vocabulary = JSON.parse(fs.readFileSync(path.join(agent, 'report_vocabulary.json'), 'utf8'));

function json(res, code, body) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff' });
  res.end(JSON.stringify(body));
}

function exportError(req, res, code, error) {
  if (!String(req.headers?.accept || '').includes('text/html'))
    return json(res, code, { ok: false, error });
  const messages = {
    authentication_required: '未登录，请返回对话后重新进入。',
    not_found: '找不到这一轮执行报告。',
    screenshot_limit_exceeded: '截图过多或过大，无法打包。请下载不含截图的材料。',
    export_busy: '正在导出另一份材料，请稍后重试。',
    report_pending: '报告仍在生成，请稍后重试。',
    invalid_query: '下载链接无效。',
    method_not_allowed: '此链接只支持下载。',
  };
  const message = messages[error] || '反馈材料暂不可用，请稍后重试。';
  res.writeHead(code, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline' });
  res.end(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>反馈材料</title>` +
    `<body><main><h1>反馈材料</h1><p>${message}</p>` +
    '<a href="#" onclick="history.back(); return false;">返回对话</a></main></body></html>');
}

function requestRejection(ctx, req) {
  let rejection = 401;
  try {
    if (typeof ctx.connection?.requestRejection === 'function') {
      const status = ctx.connection.requestRejection(req);
      if (status === undefined || status === 403) rejection = status;
    }
  } catch {}
  return rejection;
}

function reportRequest(ctx, req, res, turns, store, allowShots = false, sendError = json) {
  const rejection = requestRejection(ctx, req);
  if (rejection !== undefined) return sendError(res, rejection, { ok: false, error: 'authentication_required' });
  if (req.method !== 'GET') return sendError(res, 405, { ok: false, error: 'method_not_allowed' });
  if (!turns || (req.url || '').length > 512) return sendError(res, 503, { ok: false, error: 'report_unavailable' });
  let url;
  try { url = new URL(req.url, 'http://localhost'); }
  catch { return sendError(res, 400, { ok: false, error: 'invalid_query' }); }
  const sessionId = url.searchParams.get('session');
  const turnText = url.searchParams.get('turn');
  const shots = url.searchParams.get('shots');
  if (!sessionId || !/^(0|[1-9][0-9]{0,8})$/.test(turnText || '') ||
      (allowShots && shots !== null && shots !== '1') ||
      [...url.searchParams.keys()].some(key => !['session', 'turn', ...(allowShots ? ['shots'] : [])].includes(key)) ||
      ['session', 'turn', 'shots'].some(key => url.searchParams.getAll(key).length > 1))
    return sendError(res, 400, { ok: false, error: 'invalid_query' });
  const row = turns.find(sessionId, Number(turnText));
  if (!row || row.state !== 'ended') return sendError(res, 404, { ok: false, error: 'not_found' });
  const saved = store.readSaved(row.task_id);
  if (!saved) {
    if (row.report_state === 'failed') {
      if (Date.now() - (Number.isFinite(row.failed_at) ? row.failed_at : 0) >= 30000)
        void turns.build(row);
      else return sendError(res, 503, { ok: false, error: 'report_generation_failed' });
    }
    return sendError(res, 202, { ok: false, error: 'report_pending' });
  }
  return { row, report: publicReport(saved.report), shots: shots === '1' };
}

function runExport(store, row, report, shots, options = {}) {
  return new Promise((resolve, reject) => {
    const input = Buffer.from(JSON.stringify(report));
    if (input.length > MAX_REPORT_BYTES) return reject(new Error('report_oversized'));
    const args = [options.exportScript || path.join(path.dirname(store.script), 'feedback_export.py'),
      '--audit', store.audit, '--task-id', row.task_id, '--audit-offset', String(row.audit_offset)];
    if (shots) args.push('--shots');
    if (options.shotsDir) args.push('--shots-dir', options.shotsDir);
    const child = spawn(store.python, args, { stdio: ['pipe', 'pipe', 'ignore'] });
    const chunks = [];
    let size = 0;
    let failed = false;
    const timer = setTimeout(() => { failed = true; child.kill('SIGKILL'); }, shots ? 60000 : 10000);
    child.stdout.on('data', chunk => {
      size += chunk.length;
      if (size > MAX_EXPORT_BYTES) { failed = true; child.kill('SIGKILL'); }
      else chunks.push(chunk);
    });
    child.on('error', () => { failed = true; });
    child.on('close', code => {
      clearTimeout(timer);
      if (failed) reject(new Error('export_unavailable'));
      else if (code === 4) reject(new Error('screenshot_limit_exceeded'));
      else if (code !== 0) reject(new Error('export_unavailable'));
      else resolve(Buffer.concat(chunks));
    });
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}

const actionTypes = new Set(Object.keys(vocabulary.actions));
const results = new Set(Object.keys(vocabulary.results));
const codes = new Set(Object.keys(vocabulary.reasons));
const gateCodes = new Set(vocabulary.gate_reason_codes);
const count = value => Number.isSafeInteger(value) && value >= 0 && value <= 1000000 ? value : null;

export function publicReport(report) {
  const raw = report?.counts || {};
  const coverage = report?.coverage || {};
  const timeline = Array.isArray(report?.timeline) ? report.timeline.slice(0, 1000) : [];
  return {
    counts: {
      dispatched_actions: count(raw.dispatched_actions),
      gate_refusals_by_reason: Object.fromEntries(Object.entries(raw.gate_refusals_by_reason || {})
        .filter(([key, value]) => gateCodes.has(key) && count(value) !== null)),
      command_hook_refusals: count(raw.command_hook_refusals),
      command_hook_shadow_would_block: count(raw.command_hook_shadow_would_block),
      typesafe_unavailable_warnings: count(raw.typesafe_unavailable_warnings),
      handoff_points: count(raw.handoff_points),
    },
    coverage: {
      lifecycle_valid: coverage.lifecycle_valid === true,
      attribution_complete: coverage.attribution_complete === true,
      command_hook_linked: coverage.command_hook_linked === true,
      concurrent_turns: coverage.concurrent_turns === true,
      interrupted: coverage.interrupted === true,
      timeline_truncated: coverage.timeline_truncated === true,
    },
    timeline: timeline.filter(row => actionTypes.has(row?.action_type) && results.has(row?.result))
      .map(row => ({ action_type: row.action_type, result: row.result,
        reason_code: codes.has(row.reason_code) ? row.reason_code : null })),
  };
}

export function createWebReport(ctx, options = {}) {
  const store = options.store || new TaskReportStore({
    dir: path.join(home, '.dsh-phone/reports'),
    audit: process.env.DSB_AUDIT_FILE || path.join(agent, 'safe_tap_audit.jsonl'),
    script: path.join(agent, 'task_report.py'), python: process.env.BRIDGE_PYTHON || 'python3',
  });
  const error = code => ctx.logger?.warn?.('[dsb-report] ' + code);
  let exporting = false;
  let turns;
  try {
    turns = new WebTurns({ file: options.ledger || path.join(store.dir, 'web-turns.json'),
      activeFile: options.activeFile || process.env.DSB_ACTIVE_TASK_FILE ||
        path.join(home, '.dsh-phone/active-task.json'), reportStore: store, onError: error });
    try { store.prune(); } catch { error('report_store_unavailable'); }
    for (const row of turns.recover()) void turns.build(row);
  } catch { error('web_turns_unavailable'); }

  const onEvent = (session, event) => {
    if (!turns || session.header?.origin === 'subagent') return;
    const sessionId = String(session.id);
    const turn = event.data?.turn;
    try {
      if (event.type === 'turn/start') turns.start(sessionId, turn);
      else if (event.type === 'turn/end') {
        const row = turns.end(sessionId, turn, event.data?.reason?.kind);
        if (row) void turns.build(row);
      }
    } catch { error('web_turn_event_unavailable'); }
  };
  ctx.on('session/event', onEvent);
  ctx.on('session/disposed', session => {
    if (!turns) return;
    for (const row of turns.turns.filter(value => value.session_id === String(session.id) &&
        value.state === 'running')) {
      try {
        const ended = turns.end(row.session_id, row.turn, 'aborted');
        if (ended) void turns.build(ended);
      } catch { error('web_turn_event_unavailable'); }
    }
  });
  const route = {
    kind: 'exact', path: ROUTE,
    handler: (req, res) => {
      const request = reportRequest(ctx, req, res, turns, store);
      if (!request) return;
      const body = JSON.stringify({ ok: true, report: request.report });
      if (Buffer.byteLength(body) > MAX_REPORT_BYTES) return json(res, 503, { ok: false, error: 'report_oversized' });
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      res.end(body);
    },
  };
  const exportRoute = {
    kind: 'exact', path: EXPORT_ROUTE,
    handler: async (req, res) => {
      const request = reportRequest(ctx, req, res, turns, store, true,
        (reply, code, body) => exportError(req, reply, code, body.error));
      if (!request) return;
      if (exporting) return exportError(req, res, 429, 'export_busy');
      exporting = true;
      try {
        const archive = await runExport(store, request.row, request.report, request.shots, options);
        const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 13).replace('T', '-');
        res.writeHead(200, { 'Content-Type': 'application/zip', 'Cache-Control': 'no-store',
          'X-Content-Type-Options': 'nosniff',
          'Content-Disposition': `attachment; filename="dsb-feedback-${stamp}.zip"` });
        res.end(archive);
      } catch (failure) {
        const oversized = failure.message === 'screenshot_limit_exceeded';
        exportError(req, res, oversized ? 413 : 503, oversized ?
          'screenshot_limit_exceeded' : 'export_unavailable');
      } finally {
        exporting = false;
      }
    },
  };
  ctx.effect(() => ctx.webServer.register(exportRoute), 'dsb-report: authenticated export route');
  ctx.effect(() => ctx.webServer.register(route), 'dsb-report: authenticated route');
  const timer = setInterval(() => {
    try { turns?.watchdog(); turns?.flush(); store.prune(); }
    catch { error('report_prune_unavailable'); }
  }, 30000);
  timer.unref?.();
  ctx.effect(() => () => clearInterval(timer), 'dsb-report: retention');
  return { turns, route, exportRoute, store };
}

export function apply(ctx) { createWebReport(ctx); }
