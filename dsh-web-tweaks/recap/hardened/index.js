/**
 * dsh-recap (host half) —— 回合结束后空闲 M 毫秒，用 LLM 生成三条式回顾，
 * 经 HTTP 路由暴露给浏览器半，由浏览器半渲染成对话尾部气泡。
 *
 * 设计约束（依据 dsh-recap-notes-host.md 的源码核实）：
 *  - 不往 session log 写自定义事件（持久化读路径会拒绝未知事件类型）；
 *  - 一切注册挂在 fiber 的 effect 上，插件卸载即自动清理；
 *  - LLM 调用带 deadline，失败只记日志，绝不抛进会话循环。
 */
import z from '@deepseek-ai/schemastery';
import { BlockAssembler, createUserMessage } from '@deepseek-ai/dsh-llm';
import { deadline, MAX_TIMER_DELAY_MS } from '@deepseek-ai/dsh-timeout';

export const name = 'recap';

/** 硬依赖：缺任何一个就不激活。settings 走可选注入（见 apply 末尾）。 */
export const inject = ['sessions', 'llm', 'webServer', 'timer', 'connection'];

/** 设置命名空间；浏览器侧设置卡的 key 必须与它一致。 */
export const NS = 'recap';

const RECAP_TIMEOUT_CODE = 'RECAP_TIMEOUT';

/** 配置 schema：默认值写在这里，也是设置页表单的来源。 */
export const Config = z.object({
  enabled: z.boolean().default(false),
  idleMs: z.number().step(1).min(1000).max(MAX_TIMER_DELAY_MS).default(1000),
  provider: z.string().default('deepseek-official'),
  model: z.string().default('deepseek-flash'),
  // 2026-09-22 用户指定 low。DeepSeek 只认 off/low/high/max（没有 medium/xhigh）。
  // 3 行总结属"遵循格式指令"任务、不是推理任务 → 低档更快更省且不损质量。
  reasoningEffort: z.union(['off', 'low', 'high', 'max']).default('low'),
  timeoutMs: z.number().step(1).min(1).max(MAX_TIMER_DELAY_MS).default(45000),
  maxInputBytes: z.number().step(1).min(1).default(32768),
  maxMessages: z.number().step(1).min(1).default(40),
  maxOutputTokens: z.number().step(1).min(1).default(1500),
  routePath: z.string().default('/recap/api'),
});

const DEFAULT_SYSTEM_PROMPT = [
  // ═══ 2026-09-22 v3：融合两个开源 skill 的规则 ═══
  //  - ayghri/i-have-adhd (50k★)：结论/下一步前置、禁开场白禁客套、
  //    "读者只读首尾两行也必须知道『刚发生了什么』和『下一步做什么』"
  //  - op7418/Humanizer-zh (17.7k★)：去 AI 味（禁"值得注意的是"类填充、
  //    禁通用积极结论、禁三段式、禁过度限定、禁协作痕迹）
  // 精简原则：两份原文共 560+ 行，总结只有 3 行 → 只保留**直接影响这 3 行**的规则，
  // 否则提示词本身会变成噪音，稀释真正重要的指令。
  'You write the 3-line recap shown to the user right after one answer.',
  'Assume the reader is tired, in a hurry, and will read ONLY these 3 lines.',
  '',
  '═══ THE ONE RULE THAT MATTERS MOST ═══',
  'Be FRANK about the outcome. If it failed, is blocked, or is still unverified,',
  'say that in line 1. Never make a failure sound like a success.',
  'Never claim something was done if it was not confirmed.',
  '',
  '═══ SCOPE ═══',
  'Recap ONLY the latest exchange (the user\'s last request + the answer after it).',
  'Ignore all earlier turns entirely.',
  '',
  '═══ LANGUAGE ═══',
  'Write in the same language as the conversation, in plain everyday speech —',
  'the way you would tell a friend who is not technical.',
  'Keep an exact name (file, app, setting) ONLY when the name itself is the point.',
  'Short sentences.',
  '',
  '═══ OUTPUT — EXACTLY 3 LINES, plain text, nothing else ═══',
  'No bullets, no numbering, no markdown, no preamble, no closing line.',
  '',
  'Line 1 — THE RESULT. Lead with the verdict, not the story:',
  '  "搞定了 — ..." / "没搞定 — 卡在 ..." / "查到了 — ..." / "还不确定 — ..."',
  '  (use the conversation\'s language, those are shape examples not fixed words)',
  'Line 2 — WHAT CHANGED on the phone or computer (files, settings, versions, state).',
  '  If nothing changed at all, write only:  无（只是说明）',
  'Line 3 — WHAT THE USER SHOULD DO. ONE action, doable in under two minutes.',
  '  If the user needs to do nothing, write only:  无需操作',
  '',
  '═══ BANNED — these are the "AI smell", delete on sight ═══',
  'Openers that announce yourself: "让我...", "我将...", "好的，", "这是个好问题",',
  '  "值得注意的是", "总的来说", "综上所述", "首先...其次...最后"',
  'Closers / collaboration traces: "希望这有帮助", "如需帮助请告诉我", "随时问我",',
  '  "让我们", "我已经为你..."',
  'Generic positive endings: "未来可期", "迈出了重要一步", "效果值得期待"',
  'Empty intensifiers and promo words: "强大的", "无缝的", "全面的", "显著的",',
  '  "极大地", "有效地", "成功地"',
  'Hedging that carries no real uncertainty: "可能", "或许", "似乎", "某种程度上"',
  '  (keep a hedge ONLY when the uncertainty is real)',
  'Triple structures and "不是A而是B" rhetorical contrast.',
  'Metaphor, idiom, or anything that sounds quotable. Say the literal thing.',
  'Any two sentences that say the same thing.',
  '',
  '═══ FINAL CHECK before you answer ═══',
  'If the reader reads ONLY line 1 and line 3, do they know:',
  '  (a) what just happened, and (b) what to do next?',
  'If not, rewrite. Then output.',
].join('\n');

// ── 小工具 ──────────────────────────────────────────────────────────────────
function textOf(content) {
  return (content || [])
    .filter((b) => b.type === 'text')
    .map((b) => b.text)
    .join('\n')
    .trim();
}

function messageOf(error) {
  return error instanceof Error ? error.message : String(error);
}

function sendJson(res, status, payload) {
  if (res.headersSent) { res.end(); return; }
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(payload));
}

// ── 会话读取：只取【最后一轮】——最后一条用户消息 + 它之后最后一条助手消息 ────
// 之前的实现会回溯最近若干轮，导致回顾变成"最近一段工作的摘要"；用户要的是
// "只看我最后问的那句、和我最后给出的那次回答"。
export function lastTurn(session, opts) {
  const maxInputBytes = opts.maxInputBytes;
  const events = session.snapshotEvents();

  let assistant = null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== 'assistant/message') continue;
    const text = textOf(e.data.message.content);
    if (text.length > 0) { assistant = { seq: Number(e.seq), time: e.time, role: 'assistant', text: text }; break; }
  }
  if (assistant === null) return [];

  let user = null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (Number(e.seq) >= assistant.seq) continue;
    if (e.type !== 'user/message') continue;
    if (e.data.source.kind !== 'user') continue;
    const text = textOf(e.data.content);
    if (text.length > 0) { user = { seq: Number(e.seq), time: e.time, role: 'user', text: text }; break; }
  }

  const rows = [];
  if (user !== null) rows.push(user);
  rows.push(assistant);

  // 字节预算：优先保留助手消息（要被总结的那条），超预算时先削用户消息
  const budget = maxInputBytes;
  const cost = (r) => Buffer.byteLength(r.text, 'utf8') + 32;
  for (const r of rows) {
    if (cost(r) > budget) r.text = r.text.slice(0, Math.max(0, budget - 64)).trimEnd() + '…';
  }
  if (rows.length > 1 && cost(rows[0]) + cost(rows[1]) > budget) {
    rows[0].text = rows[0].text.slice(0, Math.max(0, budget - cost(rows[1]) - 64)).trimEnd() + '…';
  }
  return rows;
}

// 旧的实现保留为内部参考（当前不再使用）
function recentTurnsLegacy(session, opts) {
  const maxMessages = opts.maxMessages;
  const maxInputBytes = opts.maxInputBytes;
  const events = session.snapshotEvents();
  const picked = [];
  let bytes = 0;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    let role;
    let text;
    if (e.type === 'user/message') {
      if (e.data.source.kind !== 'user') continue;
      role = 'user';
      text = textOf(e.data.content);
    } else if (e.type === 'assistant/message') {
      role = 'assistant';
      text = textOf(e.data.message.content);
    } else {
      continue;
    }
    if (text.length === 0) continue;
    const cost = Buffer.byteLength(text, 'utf8') + 32;
    if (bytes + cost > maxInputBytes) break;
    bytes += cost;
    picked.push({ seq: Number(e.seq), time: e.time, role: role, text: text });
    if (picked.length >= maxMessages) break;
  }
  return picked.reverse();
}

function renderDigest(rows) {
  return rows.map((r) => '[' + r.role + ' #' + r.seq + ']\n' + r.text).join('\n\n');
}

/** 只保留前 3 行非空文本；不截断内容（弹层/卡片都可滚动查看全文）。 */
export function normalizeRecap(text) {
  const lines = String(text)
    .replace(/\r/g, '')
    .split('\n')
    .map((l) => l.replace(/^\s*(?:[-*•]|\d+[.)])\s*/, '').trim())
    .filter((l) => l.length > 0);
  return lines.slice(0, 3).join('\n');
}

// ── 一次性 LLM 调用（依据 dsh-session-title-llm 的调用链） ───────────────────
function finishError(finish) {
  switch (finish.kind) {
    case 'stop': return undefined;
    case 'error':
    case 'aborted': {
      const error = new Error((finish.failure && finish.failure.message) || 'model call failed');
      if (finish.failure && finish.failure.code !== undefined) error.code = finish.failure.code;
      return error;
    }
    // A truncated recap is still useful: keep whatever text came back.
    case 'max-tokens': return undefined;
    case 'tool-calls': return new Error('recap: model unexpectedly requested a tool');
    default: return new Error('recap: unsupported finish reason "' + String(finish.kind) + '"');
  }
}

export async function oneShotText(ctx, opts) {
  const provider = opts.provider;
  const model = opts.model;
  const timeoutMs = opts.timeoutMs;
  const sessionId = opts.sessionId;

  if (typeof provider !== 'string' || provider.length === 0) throw new Error('recap: provider is required');
  if (typeof model !== 'string' || model.length === 0) throw new Error('recap: model is required');
  if (typeof timeoutMs !== 'number' || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error('recap: timeoutMs must be a positive number');
  }

  const messages = [createUserMessage({
    content: [{ type: 'text', text: opts.input }],
    source: { kind: 'plugin', plugin: name },
  })];

  const call = deadline(opts.signal, timeoutMs, RECAP_TIMEOUT_CODE);
  try {
    const options = Object.freeze({
      provider: provider,
      model: model,
      messages: messages,
      system: opts.system,
      maxTokens: opts.maxOutputTokens,
      ...(opts.reasoningEffort === undefined ? {} : { reasoningEffort: opts.reasoningEffort }),
      ...(sessionId === undefined ? {} : { sessionId: sessionId }),
      signal: call.signal,
    });

    const assembler = new BlockAssembler();
    for await (const chunk of ctx.llm.stream(options)) {
      call.signal.throwIfAborted();
      assembler.push(chunk);
    }
    call.signal.throwIfAborted();

    const terminal = finishError(assembler.finish);
    if (terminal !== undefined) throw terminal;

    const blocks = assembler.blocks();
    if (blocks.some((b) => b.type === 'tool-call')) throw new Error('recap: output must be text only');
    const text = blocks.filter((b) => b.type === 'text').map((b) => b.text).join('\n').trim();
    if (text.length === 0) throw new Error('recap: model produced no text');
    return { text: normalizeRecap(text), usage: assembler.usage };
  } finally {
    call[Symbol.dispose]();
  }
}

// ── apply ───────────────────────────────────────────────────────────────────
export function apply(ctx, entryConfig) {
  let source = () => entryConfig;

  const recaps = new Map();     // sessionId -> 最近一次回顾记录
  const timers = new Map();     // sessionId -> timer dispose
  const inflight = new Map();   // sessionId -> Promise（防重入）
  // 自诊断：远程即可判断回合结束事件是否到达、为何跳过
  const stats = { startedAt: Date.now(), turnEnds: 0, turnStarts: 0, skipped: {}, generated: 0, lastTurnEndAt: 0, lastSkip: null, lastError: null };

  const cfg = () => source();

  function disarm(sessionId) {
    const dispose = timers.get(sessionId);
    if (dispose !== undefined) { dispose(); timers.delete(sessionId); }
  }

  async function generate(session, turn) {
    const config = cfg();
    if (!config.enabled) return undefined;
    const rows = lastTurn(session, { maxInputBytes: config.maxInputBytes });
    if (rows.length === 0) return undefined;

    const out = await oneShotText(ctx, {
      provider: config.provider,
      model: config.model,
      system: DEFAULT_SYSTEM_PROMPT,
      input: renderDigest(rows),
      timeoutMs: config.timeoutMs,
      maxOutputTokens: config.maxOutputTokens,
      reasoningEffort: config.reasoningEffort,
      sessionId: session.id,
    });

    const last = rows[rows.length - 1];
    const record = {
      session: String(session.id),
      turn: turn,
      at: Date.now(),
      provider: config.provider,
      model: config.model,
      uptoSeq: last === undefined ? null : last.seq,
      recap: out.text,
      usage: out.usage,
    };
    recaps.set(String(session.id), record);
    stats.generated += 1;
    ctx.logger.info('recap: generated for ' + session.id + ' (turn ' + turn + ')');
    return record;
  }

  function run(sessionId, turn) {
    const existing = inflight.get(sessionId);
    if (existing !== undefined) return existing;
    const session = ctx.sessions.get(sessionId);
    if (session === undefined) return Promise.resolve(undefined);
    const task = generate(session, turn)
      .catch((error) => {
        ctx.logger.warn('recap: recap for ' + sessionId + ' failed: ' + messageOf(error));
        return undefined;
      })
      .finally(() => { inflight.delete(sessionId); });
    inflight.set(sessionId, task);
    return task;
  }

  function armIdle(sessionId, turn) {
    const config = cfg();
    if (!config.enabled) { disarm(sessionId); return; }
    disarm(sessionId);
    timers.set(sessionId, ctx.timeout(() => {
      timers.delete(sessionId);
      void run(sessionId, turn);
    }, config.idleMs));
  }

  // ── 回合结束 + 空闲计时 ───────────────────────────────────────────────────
  ctx.on('session/event', (session, event) => {
    const sessionId = String(session.id);
    if (event.type === 'turn/start') { stats.turnStarts += 1; disarm(sessionId); return; }
    if (event.type !== 'turn/end') return;
    stats.turnEnds += 1;
    stats.lastTurnEndAt = Date.now();
    const skip = (why) => { stats.skipped[why] = (stats.skipped[why] || 0) + 1; stats.lastSkip = why; };
    if (session.header.origin === 'subagent') { skip('subagent'); return; }
    const reason = event.data && event.data.reason ? event.data.reason.kind : 'unknown';
    if (reason !== 'completed') { skip('reason:' + String(reason)); return; }
    armIdle(sessionId, event.data.turn);
  });

  // 会话重新开始活动 → 取消计时
  ctx.on('agent/status', (payload) => {
    if (payload.status === 'running') disarm(String(payload.agent.id));
  });

  // ── HTTP 路由（只读轮询） ────────────────────────────────────────────────
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: entryConfig.routePath,
    handler: async (req, res) => {
      try {
        const config = cfg();
        if (req.method === 'POST') {
          sendJson(res, 410, { ok: false, error: 'recap_write_disabled' });
          return;
        }
        if (!config.enabled) {
          sendJson(res, 404, { ok: false, error: 'not_found' });
          return;
        }
        // Exact WebServer routes have no auth middleware. Reuse the /api boundary.
        let rejection = 401;
        try {
          if (typeof ctx.connection?.requestRejection === 'function') {
            const status = ctx.connection.requestRejection(req);
            if (status === undefined || status === 403) rejection = status;
          }
        } catch { /* Missing/incompatible auth must fail closed. */ }
        if (rejection !== undefined) {
          sendJson(res, rejection, { ok: false, error: 'authentication_required' });
          return;
        }
        const url = new URL(req.url || '/', 'http://x');

        if (req.method === 'GET' || req.method === 'HEAD') {
          const which = url.searchParams.get('session');
          if (which !== null) {
            sendJson(res, 200, { ok: true, enabled: config.enabled, recap: recaps.get(which) || null });
            return;
          }
          const items = [...recaps.values()].sort((a, b) => b.at - a.at);
          sendJson(res, 200, {
            ok: true,
            enabled: config.enabled,
            idleMs: config.idleMs,
            count: items.length,
            items: items,
            debug: {
              uptimeMs: Date.now() - stats.startedAt,
              turnStarts: stats.turnStarts,
              turnEnds: stats.turnEnds,
              generated: stats.generated,
              timersArmed: timers.size,
              inflight: inflight.size,
              lastTurnEndAt: stats.lastTurnEndAt,
              lastSkip: stats.lastSkip,
              skipped: stats.skipped,
            },
          });
          return;
        }

        sendJson(res, 405, { ok: false, error: 'method_not_allowed' });
      } catch (error) {
        ctx.logger.warn('recap: http request failed');
        sendJson(res, 500, { ok: false, error: 'request_failed' });
      }
    },
  }), 'recap: http route');

  // ── 设置命名空间 ─────────────────────────────────────────────────────────
  ctx.inject(['settings'], (settingsCtx) => {
    settingsCtx.settings.installSection(ctx, NS, Config, entryConfig, {
      validate: (value) => {
        if (value.enabled && value.timeoutMs >= value.idleMs) {
          throw new Error('recap: timeoutMs must be smaller than idleMs');
        }
        if (!value.routePath.startsWith('/')) {
          throw new Error('recap: routePath must be an absolute pathname');
        }
      },
      setSource: (current) => { source = current; },
      onChange: () => {
        if (!cfg().enabled) for (const id of [...timers.keys()]) disarm(id);
      },
    });
    ctx.logger.info('recap: settings namespace "' + NS + '" registered');
  });
}

export default { name, inject, Config, apply, NS };
