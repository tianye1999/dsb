#!/usr/bin/env node
// dsh-bridge v3 —— 通用「手机代理」桥接。设计原则：
//   * App 只负责「看屏幕 / 点屏幕」，所有决定都由 DSH 做。
//   * 这里不含任何 App 专用知识（没有京东深链、没有微信技能、没有包名规则）。
//   * 默认大脑 = dsh --profile headless；BRIDGE_BRAIN=api 可切到直连模型（快，但绕过了 DSH）。
//
// 接口：
//   GET  /health
//   POST /step   {goal, history, imageBase64, ext, nodes, foregroundApp, notifications} -> 下一个动作 JSON
//   POST /ask    {prompt, imageBase64?, ext?, nodes?, foregroundApp, notifications}      -> 文字回答
import http from 'node:http';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import { TaskLedger } from './task-ledger.mjs';
import { listenPrivate } from './private-listener.mjs';

const DIR = process.env.BRIDGE_DIR || '/tmp/dsh-bridge';
const DSH = process.env.BRIDGE_DSH || 'dsh';
const HOME = process.env.HOME || '/root';
const taskLedger = new TaskLedger(path.join(HOME, '.dsh-phone', 'bridge-tasks.json'));
let cancellationInFlight = false;
const PROFILE = process.env.BRIDGE_PROFILE || 'headless';
const BRAIN = process.env.BRIDGE_BRAIN || 'dsh';           // dsh | api
const PERMISSION_MODE = process.env.BRIDGE_PERMISSION_MODE || 'danger-full-access';  // 和网页会话一致：不设限
const API = process.env.BRIDGE_API || 'https://api.deepseek.com/chat/completions';
const MODEL = process.env.BRIDGE_MODEL || 'deepseek-flash';
const MAX_TOKENS = Number(process.env.BRIDGE_MAX_TOKENS || 1600);
const BRAIN_TIMEOUT_MS = Number(process.env.BRIDGE_TIMEOUT_MS || 600000);
const CRED = process.env.BRIDGE_CRED || path.join(process.env.HOME || '/root', '.dsh/.credentials.yaml');
fs.mkdirSync(DIR, { recursive: true });

let cancelGeneration = 0;
const activeChildren = new Set();
const ownedGroups = new Set();
const activeRequests = new Set();
function checkGeneration(generation) {
  if (generation !== cancelGeneration) throw new Error('Task cancelled');
}
function killTree(child) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  // Bridge-owned workers are process-group leaders, including their tools.
  try { process.kill(-child.pid, 'SIGKILL'); }
  catch { try { child.kill('SIGKILL'); } catch {} }
}

function groupDrained(pid) {
  try { process.kill(-pid, 0); }
  catch (e) { return e.code === 'ESRCH'; }
  if (process.platform !== 'linux') return false;
  try {
    for (const entry of fs.readdirSync('/proc')) {
      if (!/^\d+$/.test(entry)) continue;
      let stat;
      try { stat = fs.readFileSync('/proc/' + entry + '/stat', 'utf8'); }
      catch (e) { if (e.code === 'ENOENT' || e.code === 'ESRCH') continue; throw e; }
      const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
      if (Number(fields[2]) === pid && !['Z', 'X'].includes(fields[0])) return false;
    }
    return true;
  } catch { return false; }
}

function runtimeLeaderAlive(child) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return false;
  try {
    if (process.platform === 'linux') {
      const stat = fs.readFileSync('/proc/' + child.pid + '/stat', 'utf8');
      return !['Z', 'X'].includes(stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/)[0]);
    }
    process.kill(child.pid, 0);
    return true;
  } catch { return false; }
}

let cachedKey = null;
function apiKey() {
  if (cachedKey) return cachedKey;
  const m = fs.readFileSync(CRED, 'utf8').match(/sk-[A-Za-z0-9_-]{10,}/);
  if (!m) throw new Error('no api key in ' + CRED);
  cachedKey = m[0];
  return cachedKey;
}

function saveImage(b64, name, ext) {
  if (!b64) return null;
  const clean = String(b64).replace(/^data:image\/\w+;base64,/, '');
  const e = (ext || 'jpg').replace(/[^a-z0-9]/gi, '');
  const file = path.join(DIR, (name || 'shot') + '_' + crypto.randomBytes(3).toString('hex') + '.' + e);
  fs.writeFileSync(file, Buffer.from(clean, 'base64'));
  return file;
}

function pruneDir(keep = 80) {
  try {
    const files = fs.readdirSync(DIR).map(f => path.join(DIR, f))
      .map(f => ({ f, t: fs.statSync(f).mtimeMs })).sort((a, b) => b.t - a.t);
    for (const x of files.slice(keep)) fs.unlinkSync(x.f);
  } catch {}
}

function extractJson(text) {
  if (!text) return null;
  const fence = text.match(/\`\`\`(?:json)?\s*([\s\S]*?)\`\`\`/);
  const body = fence ? fence[1] : text;
  const start = body.indexOf('{');
  if (start < 0) return null;
  let depth = 0;
  for (let i = start; i < body.length; i++) {
    if (body[i] === '{') depth++;
    else if (body[i] === '}') {
      depth--;
      if (depth === 0) { try { return JSON.parse(body.slice(start, i + 1)); } catch { return null; } }
    }
  }
  return null;
}

// ---------- 大脑 1：DSH（默认） ----------
function runDsh(prompt) {
  return new Promise((resolve) => {
    const started = Date.now();
    const p = spawn(DSH, ['--profile', PROFILE, prompt], {
      cwd: process.env.HOME || '/root',
      detached: true,
      // 关键：网页会话是 danger-full-access，这里必须一致，否则 bash 会被沙箱挡住 → DSH 没法用 adb 自己动手
      env: { ...process.env, DSH_PERMISSION_MODE: PERMISSION_MODE },
    });
    activeChildren.add(p);
    if (p.pid) ownedGroups.add(p.pid);
    let out = '', err = '';
    const timer = setTimeout(() => killTree(p), BRAIN_TIMEOUT_MS);
    p.stdout.on('data', d => out += d);
    p.stderr.on('data', d => err += d);
    p.on('exit', () => killTree(p));
    p.on('close', code => { activeChildren.delete(p); clearTimeout(timer); resolve({ ok: code === 0, text: out.trim(), err: err.trim().slice(-800), ms: Date.now() - started }); });
    p.on('error', e => { activeChildren.delete(p); clearTimeout(timer); resolve({ ok: false, text: '', err: String(e), ms: Date.now() - started }); });
  });
}

// ---------- 大脑 2：直连模型（可选，快） ----------
async function runApi(prompt, imagePath) {
  const started = Date.now();
  const content = [{ type: 'text', text: prompt }];
  if (imagePath) {
    const b64 = fs.readFileSync(imagePath).toString('base64');
    content.push({ type: 'image_url', image_url: { url: 'data:image/jpeg;base64,' + b64 } });
  }
  const ctrl = new AbortController();
  activeRequests.add(ctrl);
  const timer = setTimeout(() => ctrl.abort(), BRAIN_TIMEOUT_MS);
  try {
    const res = await fetch(API, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer ' + apiKey() },
      body: JSON.stringify({ model: MODEL, messages: [{ role: 'user', content }], max_tokens: MAX_TOKENS }),
      signal: ctrl.signal,
    });
    const data = JSON.parse(await res.text());
    if (!res.ok) return { ok: false, text: '', err: JSON.stringify(data).slice(0, 300), ms: Date.now() - started };
    const msg = data.choices?.[0]?.message;
    return { ok: true, text: (msg?.content || '').trim(), err: '', ms: Date.now() - started };
  } catch (e) {
    return { ok: false, text: '', err: String(e), ms: Date.now() - started };
  } finally { clearTimeout(timer); activeRequests.delete(ctrl); }
}

async function think(prompt, imagePath) {
  if (BRAIN === 'api') return runApi(prompt, imagePath);
  const withImage = imagePath
    ? prompt + '\n\n（当前手机截图已保存到 ' + imagePath + '，请先用 read_image 查看这张图再决定。）'
    : prompt;
  const r = await runDsh(withImage);
  return { ok: r.ok, text: r.text, err: r.err, ms: r.ms };
}

// ---------- 通用提示词（与任何具体 App 无关） ----------
const RULES = [
  '你是一个通用的 Android 手机操作代理，通过「看当前屏幕 → 给出下一个动作」来完成任务。',
  '你没有、也不需要任何 App 的专用接口、深链或包名知识——所有事情都靠看屏幕、点屏幕完成。',
  '',
  '【看屏幕：元素树优先，截图按需】',
  '- 每轮都会给你「界面元素列表」（无障碍控件树，带文字和中心坐标）。这是首选依据，成本几乎为零。',
  '- 不要习惯性要求截图。列表非空时，直接用列表判断该点哪里。',
  '- 只有元素树确实解释不了时（自绘/游戏/Canvas、图片里的字、需要确认视觉状态），才输出 {"action":"screenshot"}，系统会在下一轮把截图给你。',
  '- 列表为空说明这个界面读不到控件树，此时系统会自动附带截图，你不用再要。',
  '',
  '【批量动作：确定无疑的连续操作一次做完】',
  '- 如果接下来 2~5 步是机械确定的（例如：点输入框 → 输入文字 → 点发送），不要分轮，直接输出：',
  '  {"actions":[{"action":"tap","x":数字,"y":数字},{"action":"type","text":"文字"},{"action":"tap","x":数字,"y":数字}],"reason":"一句话理由"}',
  '- 只有当每一步的结果都不影响下一步时才批量；任何需要「看一眼再决定」的步骤都必须拆成单独一轮。',
  '- 批量最多 5 个动作；批量里不要放 screenshot；done / ask 只能是最后一个。',
  '- 同一批内动作之间只等约 0.4 秒，所以别把依赖界面加载的步骤塞进同一批。',
  '',
  '【单动作 JSON 格式】',
  '只输出一行 JSON，不要解释、不要代码块：',
  '{"action":"tap|longpress|swipe|type|open_app|back|home|recents|wait|screenshot|ask|done","x":数字,"y":数字,"x2":数字,"y2":数字,"text":"文字或App名","reason":"一句话理由","sensitive":true/false}',
  '动作含义：',
  '- tap / longpress：点坐标（屏幕宽 1260、高 2800，左上角为原点）',
  '- swipe：从 (x,y) 划到 (x2,y2)',
  '- type：往当前输入框写入 text',
  '- open_app：按名字打开一个 App（text 写名字）；不确定图标在哪时优先用它',
  '- back / home / recents：系统导航',
  '- wait：界面还在加载，稍后再看',
  '- screenshot：需要看一眼真实画面（下一轮带截图）',
  '- ask：需要用户补充信息（reason 写要问什么）',
  '- done：目标已完成（reason 写结论或答案）',
  '规则：',
  '- 「界面元素列表」非空时优先点列表里的元素，坐标用它的中心点。',
  '- 会花钱、删除数据、发消息、点「确认/提交/删除」类按钮时 sensitive=true。',
  '- 连续两步画面没有变化就换个办法，或用 ask 求助，不要反复点同一个点。',
].join('\n');

function stateBlock(b) {
  const parts = [];
  parts.push('用户目标：' + (b.goal || '(未提供)'));
  parts.push('已执行过的动作：\n' + ((b.history || '').trim() || '(无)'));
  if (b.foregroundApp) parts.push('当前前台应用包名：' + b.foregroundApp);
  const nodes = (b.nodes || '').trim();
  parts.push('界面元素列表（可点元素的文字 @中心坐标）：\n' + (nodes || '（空：这个界面读不到控件树，只能靠截图目测）'));
  if (b.shot === false) parts.push('（本次没有附带截图：请优先用上面的元素树列表判断；确实需要看像素时才输出 {"action":"screenshot"}）');
  const notif = (b.notifications || '').trim();
  if (notif) parts.push('最近的设备通知（可能需要参考）：\n' + notif);
  return parts.join('\n\n');
}


// ============ 常驻 SDK runtime：一个进程、一个会话，多轮有记忆 ============
// 说明：dsh-sdk-jsonrpc-server 给每个 sessionId 建一个 agent，进程活着期间会话就在；
//       SDK 协议没有 resume，所以进程重启后要换新 sessionId（会话仍会写进共享 session 存储，
//       你在网页版会话列表里能看到历史）。
const RUNTIME_PROFILE = process.env.BRIDGE_RUNTIME_PROFILE || 'phone';
const PROVIDER = process.env.BRIDGE_PROVIDER || 'deepseek-official';
const TURN_TIMEOUT_MS = Number(process.env.BRIDGE_TURN_TIMEOUT_MS || 600000);
// ---- 常驻记忆：跨 runtime / 跨重启都在 ----
const MEM_DIR = process.env.BRIDGE_MEM_DIR || path.join(HOME, '.dsh-phone');
const MEM_FILE = path.join(MEM_DIR, 'MEMORY.md');
const TRANSCRIPT = path.join(MEM_DIR, 'transcript.jsonl');
const MEM_RECENT = Number(process.env.BRIDGE_MEM_RECENT || 6);
// ---- 记忆压缩：每 N 轮（或 MEMORY.md 超过 8KB）自动整理一次 ----
const COMPACT_EVERY = Number(process.env.BRIDGE_COMPACT_EVERY ?? 20);
const MEM_STATE = path.join(MEM_DIR, 'mem-state.json');
let memState = { turns: 0, lastCompact: 0 };
try { memState = { ...memState, ...JSON.parse(fs.readFileSync(MEM_STATE, 'utf8')) }; } catch {}
function saveMemState() { try { ensureMemDir(); fs.writeFileSync(MEM_STATE, JSON.stringify(memState)); } catch {} }
let turnQueue = Promise.resolve();
function enqueueTurn(fn) {
  const generation = cancelGeneration;
  const run = () => { checkGeneration(generation); return fn(); };
  const p = turnQueue.then(run, run);
  turnQueue = p.then(() => {}, () => {});
  return p;
}
let runtime = null;

function startRuntime() {
  const sid = 'phone-' + Date.now();
  const child = spawn(DSH, ['--profile', RUNTIME_PROFILE], {
    cwd: HOME,
    detached: true,
    env: { ...process.env, DSH_PERMISSION_MODE: PERMISSION_MODE },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const rt = { child, alive: true, sid, nextId: 1, pending: new Map(), assistant: [], onTurnEnd: null };
  activeChildren.add(child);
  if (child.pid) ownedGroups.add(child.pid);
  rt.fail = (error) => {
    rt.alive = false;
    if (runtime === rt) runtime = null;
    for (const q of rt.pending.values()) q.reject(error);
    rt.pending.clear();
    if (rt.onTurnEnd) { rt.onTurnEnd.reject(error); rt.onTurnEnd = null; }
    killTree(child);
  };
  child.stderr.on('data', () => {});
  child.on('exit', () => { activeChildren.delete(child); rt.fail(new Error('DSH runtime exited')); });
  child.on('error', e => { activeChildren.delete(child); rt.fail(e); });
  child.stdin.on('error', e => rt.fail(e));
  const rl = readline.createInterface({ input: child.stdout });
  rl.on('line', (line) => {
    let m; try { m = JSON.parse(line); } catch { return; }
    if (m.id !== undefined && m.method === undefined) {
      const q = rt.pending.get(m.id);
      if (q) { rt.pending.delete(m.id); m.error ? q.reject(new Error(JSON.stringify(m.error))) : q.resolve(m.result); }
      return;
    }
    if (m.method !== 'session.event') return;
    const { sessionId, event } = m.params || {};
    if (sessionId !== rt.sid || !event) return;
    if (event.type === 'assistant/message') {
      const txt = ((event.data && event.data.message && event.data.message.content) || [])
        .filter(b => b.type === 'text').map(b => b.text).join('');
      if (txt) rt.assistant.push(txt);
    }
    if (event.type === 'turn/end') { const cb = rt.onTurnEnd; rt.onTurnEnd = null; if (cb) cb.resolve(); }
  });
  rt.send = (method, params) => {
    const id = rt.nextId++;
    return new Promise((resolve, reject) => {
      if (!rt.alive) { reject(new Error('DSH runtime is stopped')); return; }
      const timer = setTimeout(() => rt.fail(new Error('DSH RPC timed out: ' + method)), BRAIN_TIMEOUT_MS);
      rt.pending.set(id, {
        resolve: value => { clearTimeout(timer); resolve(value); },
        reject: error => { clearTimeout(timer); reject(error); },
      });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n',
        error => { if (error) rt.fail(error); });
    });
  };
  return rt;
}

async function ensureRuntime() {
  if (runtime && runtime.alive) return runtime.ready;
  const rt = startRuntime();
  runtime = rt; // Initialization must already be visible to /cancel.
  rt.ready = (async () => {
    const init = { cwd: HOME, provider: PROVIDER, model: MODEL };
    const effort = process.env.BRIDGE_REASONING_EFFORT;
    if (effort) init.reasoningEffort = effort;
    try {
      await rt.send('initialize', init);
    } catch (e) {
      if (effort && rt.alive) {   // 推理强度不被接受就去掉再试
        delete init.reasoningEffort;
        await rt.send('initialize', init);
      } else throw e;
    }
    console.log('[runtime] started session=%s effort=%s', rt.sid, init.reasoningEffort || '(default)');
    // Memory restoration is a separate, bounded turn before user work.
    rt.memReady = false;
    rt.assistant = [];
    await promptTurn(rt, memoryPrimer(), 120000);
    rt.memReady = true;
    console.log('[runtime] memory restored (mem file: %s)', fs.existsSync(MEM_FILE) ? 'yes' : 'no');
    return rt;
  })().catch(e => { rt.fail(e); throw e; });
  return rt.ready;
}

async function promptTurn(rt, text, timeoutMs) {
  if (!rt.alive) throw new Error('DSH runtime is stopped');
  let waiter;
  const done = new Promise((resolve, reject) => { waiter = { resolve, reject }; rt.onTurnEnd = waiter; });
  const timer = setTimeout(() => rt.fail(new Error('DSH turn timed out')), timeoutMs);
  try {
    await Promise.all([done, rt.send('session/prompt', {
      sessionId: rt.sid, contentBlocks: [{ type: 'text', text }],
    })]);
    if (!rt.alive) throw new Error('DSH runtime is stopped');
  } finally {
    clearTimeout(timer);
    if (rt.onTurnEnd === waiter) rt.onTurnEnd = null;
  }
}

/** 一轮对话（串行执行，避免和"记忆压缩"打架） */
async function rawTurn(text, timeoutMs) {
  const rt = await ensureRuntime();
  rt.assistant = [];
  await promptTurn(rt, text, timeoutMs);
  return rt.assistant.filter(Boolean);
}

/** 用户的一轮：原话进、最后答复出；之后按需排队做记忆压缩 */
async function askRuntime(text) {
  return enqueueTurn(async () => {
    const rt = await ensureRuntime();
    const all = await rawTurn(text, TURN_TIMEOUT_MS);
    const answer = all.length ? all[all.length - 1] : '';
    if (answer) appendTranscript(text, answer);
    memState.turns += 1; saveMemState();
    maybeCompact();
    return { text: answer, sessionId: rt.sid, steps: all.length };
  });
}

function shouldCompact() {
  if (!COMPACT_EVERY) return false;
  if (memState.turns >= COMPACT_EVERY) return true;
  try { if (fs.existsSync(MEM_FILE) && fs.statSync(MEM_FILE).size > 8000) return true; } catch {}
  return false;
}

let compacting = false;
function maybeCompact() {
  if (compacting || !shouldCompact()) return;
  compacting = true;
  console.log('[compact] start (turns=%s)', memState.turns);
  compactMemory().catch(e => logError('compact', e)).finally(() => { compacting = false; });
}

/** 让代理自己把长期记忆重写一遍：去重、合并、删过期 */
async function compactMemory() {
  ensureMemDir();
  const bak = MEM_FILE + '.bak';
  try { if (fs.existsSync(MEM_FILE)) fs.copyFileSync(MEM_FILE, bak); } catch {}
  const prompt = [
    '【记忆整理】这是维护任务，不要回答用户任何问题。',
    '1) 读 ' + MEM_FILE + '（你的长期记忆）和 ' + TRANSCRIPT + ' 的最后 60 行。',
    '2) 重写 ' + MEM_FILE + '：相同/相近的条目合并；一次性的、过时的删掉；保留稳定事实（用户偏好、常用路径、重要结论、踩过的坑）。',
    '3) Markdown 分节，控制在 60 行以内，只写要点；不要写密码/验证码等敏感信息。',
    '4) 完成后只回复 COMPACTED。',
  ].join('\n');
  await enqueueTurn(() => rawTurn(prompt, 300000));
  let size = 0;
  try { size = fs.statSync(MEM_FILE).size; } catch {}
  if (size < 40) {                                   // 校验：被写空了就回滚
    try { fs.copyFileSync(bak, MEM_FILE); } catch {}
    console.log('[compact] output too small, restored backup');
    return;
  }
  memState.turns = 0; memState.lastCompact = Date.now(); saveMemState();
  console.log('[compact] done, MEMORY.md=%s bytes', size);
}

function ensureMemDir() { try { fs.mkdirSync(MEM_DIR, { recursive: true }); } catch {} }

/** 追加一轮对话到原始记录（保底记忆，压缩保留最近 300 轮） */
function appendTranscript(goal, answer) {
  try {
    ensureMemDir();
    fs.appendFileSync(TRANSCRIPT, JSON.stringify({ t: Date.now(), goal, answer: (answer || '').slice(0, 2000) }) + '\n');
    const lines = fs.readFileSync(TRANSCRIPT, 'utf8').split('\n').filter(Boolean);
    if (lines.length > 300) fs.writeFileSync(TRANSCRIPT, lines.slice(-300).join('\n') + '\n');
  } catch (e) { logError('transcript-append', e); }
}

/** 生成记忆恢复用的开头（单独一轮，不碰用户原话） */
function memoryPrimer() {
  ensureMemDir();
  const parts = [];
  parts.push('DSH 手机代理 · 启动（加载长期记忆）');
  parts.push('1) 如果 ' + MEM_FILE + ' 存在，先读它；不存在就创建。它是你的长期记忆：把用户偏好、常用路径、重要结论写进去（保持简洁，别写隐私密码）。');
  parts.push('2) 你的原始对话记录在 ' + TRANSCRIPT + '，需要回忆细节时可以查。');
  try {
    const lines = fs.readFileSync(TRANSCRIPT, 'utf8').split('\n').filter(Boolean).slice(-MEM_RECENT);
    if (lines.length) {
      const recent = lines.map(l => { try { const j = JSON.parse(l); return '- 用户：' + String(j.goal).slice(0, 120) + ' → 你：' + String(j.answer || '').slice(0, 160); } catch { return null; } }).filter(Boolean);
      if (recent.length) parts.push('3) 最近几轮（摘要）：\n' + recent.join('\n'));
    }
  } catch {}
  parts.push('读完后只回复 READY，不要做别的事。');
  return parts.join('\n');
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', c => { data += c; if (data.length > 40 * 1024 * 1024) { reject(new Error('too large')); req.destroy(); } });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

function logError(scope, error) {
  const previous = Error.prepareStackTrace;
  try {
    // Structured V8 frames exclude even multiline exception messages containing task text.
    Error.prepareStackTrace = (_error, frames) => frames;
    let frames = error instanceof Error ? error.stack : null;
    if (!Array.isArray(frames)) frames = new Error().stack;
    const type = ['Error', 'TypeError', 'RangeError', 'SyntaxError'].includes(error?.name) ? error.name : 'Error';
    const locations = Array.isArray(frames) ? frames.map(frame =>
      `${frame.getFileName()}:${frame.getLineNumber()}:${frame.getColumnNumber()}`).join('\n') : '';
    console.error('[%s] %s\n%s', scope, type, locations);
  } finally { Error.prepareStackTrace = previous; }
}

function clientError(error, fallback) {
  logError('bridge-error', error);
  for (const code of ['valid task_id required', 'duplicate task_id', 'task_busy',
    'task_capacity_exhausted', 'task_state_unavailable', 'task_state_unknown_after_restart',
    'task_mismatch', 'Task cancelled']) {
    if (error?.message === code) return code;
  }
  return fallback;
}

function send(res, code, obj) {
  if (res.taskId) {
    obj = { ...obj, task_id: res.taskId, protocol: 2, transport_settled: false };
    if (code === 200 && obj.ok) {
      try {
        taskLedger.reply(res.taskId);
        obj.transport_settled = true;
      } catch (e) {
        code = 503;
        obj = { ok: false, task_id: res.taskId, protocol: 2, transport_settled: false,
          error: clientError(e, 'task_state_unavailable'), goal_status: 'unverified', goal_verified: false };
      }
    }
  }
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url.startsWith('/health')) {
      return send(res, 200, { ok: true, mode: 'v4', brain: 'sdk-runtime(单会话/有记忆)', runtimeProfile: RUNTIME_PROFILE, runtimeAlive: !!(runtime && runtime.alive), memTurns: memState.turns, compactEvery: COMPACT_EVERY, compacting, sessionId: runtime ? runtime.sid : null, permissionMode: PERMISSION_MODE, provider: PROVIDER, model: MODEL, dir: DIR, ts: Date.now() });
    }
    if (req.method !== 'POST') return send(res, 405, { error: 'method not allowed' });
    const raw = await readBody(req);
    let body = {};
    try { body = raw ? JSON.parse(raw) : {}; } catch { return send(res, 400, { error: 'bad json' }); }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return send(res, 400, { ok: false, error: 'object required' });
    if (Object.hasOwn(body, 'transport_benchmark'))
      return send(res, 409, { ok: false, error: 'benchmark_requires_disposable_fixture' });
    if (['/do', '/ask', '/step'].includes(req.url)) {
      if (cancellationInFlight) return send(res, 409, { ok: false, error: 'cancellation_in_flight' });
      // Android/proot may expose a dead leader before Node delivers its exit event.
      if (runtime && !runtimeLeaderAlive(runtime.child))
        return send(res, 409, { ok: false, error: 'prior_execution_unknown' });
      if (!runtime && [...ownedGroups].some(pid => !groupDrained(pid)))
        return send(res, 409, { ok: false, error: 'prior_execution_unknown' });
      try { taskLedger.admit(body.task_id); }
      catch (e) { return send(res, 409, { ok: false, error: clientError(e, 'task_admission_failed'), protocol: 2, transport_settled: false }); }
      res.taskId = body.task_id;
    } else if (req.url !== '/cancel') {
      return send(res, 404, { ok: false, error: 'not found' });
    }
    pruneDir();
    const generation = cancelGeneration;

    if (req.url.startsWith('/step')) {
      const img = saveImage(body.imageBase64 || body.image, 'step', body.ext || 'jpg');
      const prompt = RULES + '\n\n' + stateBlock(body);
      const r = await think(prompt, img);
      checkGeneration(generation);
      let action = extractJson(r.text);
      if (!action && r.ok) {
        // 没吐 JSON：让 DSH 带着纠正提示再来一次
        const r2 = await think(prompt + '\n\n你刚才的回复不是合法 JSON。请只输出一行 JSON 对象，第一个字符必须是 {。', img);
        checkGeneration(generation);
        action = extractJson(r2.text);
        if (!action && r2.text) action = { action: 'ask', reason: r2.text.slice(0, 200) };
        console.log('[step] retry pid=%s ms=%s action=%s', process.pid, r2.ms, JSON.stringify(action));
        return send(res, 200, { ok: !!action, action, raw: r2.text.slice(0, 800), ms: r2.ms, image: img });
      }
      console.log('[step] ms=%s action=%s', r.ms, JSON.stringify(action));
      return send(res, 200, { ok: r.ok && !!action, action, raw: (r.text || '').slice(0, 800), error: r.ok ? '' : 'brain_error', ms: r.ms, image: img });
    }

    if (req.url.startsWith('/ask')) {
      const img = saveImage(body.imageBase64 || body.image, 'ask', body.ext || 'jpg');
      const parts = ['用户的问题：' + ((body.prompt || '').trim() || '(未提供)')];
      if (img) parts.push('当前手机截图：' + img + '（请先用 read_image 看图）');
      if (body.foregroundApp) parts.push('当前前台应用包名：' + body.foregroundApp);
      if ((body.nodes || '').trim()) parts.push('界面元素列表：\n' + body.nodes.trim());
      if ((body.notifications || '').trim()) parts.push('最近的设备通知：\n' + body.notifications.trim());
      const r = await think(parts.join('\n\n'), img);
      checkGeneration(generation);
      return send(res, 200, { ok: r.ok, text: r.text, error: r.ok ? '' : 'brain_error', ms: r.ms, image: img });
    }

    if (req.url.startsWith('/do')) {
      const goal = (body.goal || '').trim();
      if (!goal) return send(res, 400, { error: 'goal required' });
      const started = Date.now();
      try {
        const r = await askRuntime(goal);          // ← 原话直接传，不加任何前后缀
        console.log('[do] session=%s ms=%s', r.sessionId, Date.now() - started);
        const hasReply = !!r.text.trim();
        // A received reply is not independent evidence that the goal was achieved.
        return send(res, 200, { ok: hasReply, text: r.text, sessionId: r.sessionId,
          run_status: hasReply ? 'reply_received' : 'empty_reply',
          goal_status: 'unverified', goal_verified: false,
          error: hasReply ? '' : 'Empty assistant reply', ms: Date.now() - started });
      } catch (e) {
        return send(res, 200, { ok: false, run_status: 'error',
          goal_status: 'unverified', goal_verified: false,
          error: clientError(e, 'runtime_error'), ms: Date.now() - started });
      }
    }

    if (req.url.startsWith('/cancel')) {
      const id = body.task_id;
      let status = taskLedger.cancellation(id);
      if (status === 'unknown') {
        try { taskLedger.cancelUnadmitted(id); status = taskLedger.cancellation(id); }
        catch { /* Missing history or prior ownership remains unknown. */ }
      }
      const receipt = { protocol: 2, task_id: id, stop_confirmed: false };
      if (status === 'confirmed') return send(res, 200, { ...receipt, ok: true, stop_confirmed: true });
      if (status !== 'owned' || cancellationInFlight)
        return send(res, 409, { ...receipt, ok: false, error: 'stop_' + (cancellationInFlight ? 'in_flight' : status) });
      cancellationInFlight = true;
      try {
        cancelGeneration++;
        const rt = runtime;
        if (rt) rt.fail(new Error('Task cancelled'));
        for (const child of activeChildren) killTree(child);
        for (const request of activeRequests) request.abort();
        const deadline = Date.now() + 3000;
        const drained = () => activeRequests.size === 0 && [...ownedGroups].every(groupDrained);
        while (!drained() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
        if (!drained()) return send(res, 409, { ...receipt, ok: false, error: 'stop_unverified' });
        taskLedger.confirmStopped(id);
        ownedGroups.clear();
        return send(res, 200, { ...receipt, ok: true, stop_confirmed: true });
      } finally { cancellationInFlight = false; }
    }

    return send(res, 404, { error: 'not found', try: ['/health', '/step', '/ask', '/do', '/cancel'] });
  } catch (e) {
    return send(res, 500, { ok: false, error: clientError(e, 'internal_error') });
  }
});

listenPrivate(server, process.env.BRIDGE_SOCKET, () => console.log('dsh-bridge private socket ready'));
