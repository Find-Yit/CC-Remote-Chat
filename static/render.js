// 渲染核心（v2：交互式 Claude Code）：与网络层完全解耦。
//   state = createState({actions, onMeta, onSession, onScreen, onModelList, onModelInfo})
//   applyEnvelope(state, env)     —— 处理一条事件信封（SSE / GET events / 回放页共用）
//   applyTranscript(s, entries, reset) —— 渲染 transcript 条目（按 uuid 去重；reset=true 时整体重建）
//   applyScreen(s, st, text)      —— 最新屏幕帧：实时草稿 + 对话框卡片
//   applySubagent(s, d)           —— 子代理摘要/条目（kind=subagent）；托盘与抽屉见 subagents.js
//   mountSession(session, host)   —— 为会话创建消息视图（增量 DOM，rAF 合批）
// 数据来源只有两类：Claude Code 自己写的 transcript（只读）与 tmux 屏幕解析结果。
// 所有内容只作为文本或经 markdown.js 受控渲染；对话框卡片的按钮只在用户点击时调用 actions（红线 §0.5）。

import { h, icon, spinner, fmtTime, fmtDuration, truncate, jsonPretty, copyText } from './dom.js';
import { renderMarkdown } from './markdown.js';
import { toolInfo, renderToolInput, renderToolResult, toolCategory } from './tools.js';
// 可视化（§10）：图片引用/灯箱、文件 chip、本轮改动卡片、工具组摘要
import { chatImage, viewedImagesRow } from './blobimg.js';
import { fileChip, bashPaths, textPaths, localhostUrls, urlPreviewButton, markdownCtx } from './filelinks.js';
import { collectTurn, renderTurnSummary } from './turnfiles.js';
import { groupSummary, noteToolAdded } from './toolgroups.js';
import { exitNoticeText } from './exitnotice.js';

const INITIAL_WINDOW = 300;   // 首次挂载时最多渲染的条目数
const MORE_STEP = 200;
const IDLE_GRACE_MS = 3000;   // 忙→空闲后，草稿最多再保留这么久（等 transcript 条目写入）
const RECENT_ENTRY_MS = 2000; // 空闲时若刚收到过助手条目，草稿立即隐藏
const PENDING_RESET_MS = 5000; // 卡片按钮点击后，若屏幕一直没变化，这么久后恢复可点

let seq = 0;
const uid = (p) => p + (++seq).toString(36);

export const DEFAULT_META = {
  sid: '', title: '', jobid: '', cwd: '', launch: null, claude_session_id: null, claude_pid: null,
  state: 'starting', status: null, mode: null, busy: false, dialog_kind: null,
  created_at: 0, last_activity: 0, error: null, link: 'connecting',
};

export const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];

export function createState(opts = {}) {
  return {
    sessions: new Map(),
    lastGseq: 0,
    actions: opts.actions || {},
    onMeta: opts.onMeta || null,
    onSession: opts.onSession || null,
    onScreen: opts.onScreen || null,
    onModelList: opts.onModelList || null,
    onModelInfo: opts.onModelInfo || null,
    onSubagent: opts.onSubagent || null,
    onFlush: opts.onFlush || null,   // 条目写进 DOM / 补渲染之后（左侧 prompt 小格子据此重建，见 minimap.js）
  };
}

export function ensureSession(state, sid, meta) {
  let s = state.sessions.get(sid);
  if (s) {
    if (meta) s.meta = { ...s.meta, ...meta };
    return s;
  }
  s = {
    sid, state,
    meta: { ...DEFAULT_META, sid, ...(meta || {}) },
    readOnly: false,
    lastGseq: 0,
    hold: false, held: [],
    items: [], tools: new Map(), orphanResults: new Map(), entryIds: new Set(), transcriptId: null,
    dirty: new Set(), view: null, renderStart: 0, renderedTo: 0, needsReorder: false,
    // 屏幕
    screen: null,              // {state, text, ts}
    draft: { awaiting: false, busyEndedAt: 0, timer: 0, lastLive: '' },
    lastAssistantAt: 0,
    dialogUi: null,            // 对话框卡片的本地状态（自由输入文字、提交中…）
    modelInfo: { model: '', effort: '', modelSrc: '', effortSrc: '' },
    modelList: null,           // 最近一次 model_list（完整列表）
    screenModelRows: new Map(), // 用户自己打开 /model 时，按编号累积看到过的行
    // 子代理
    subagents: new Map(),       // agent_id -> {id, gen, summary, entries, base, total, serverBase, stale}
    subByTool: new Map(),       // Agent 工具调用 id -> agent_id
    sidechain: false,           // true = 子代理抽屉里的只读视图（条目本身带 isSidechain）
    parent: null,               // 子代理视图所属的会话
  };
  state.sessions.set(sid, s);
  if (state.onSession) state.onSession(s);
  return s;
}

// ====================== 事件应用 ======================

export function applyEnvelope(state, env) {
  if (!env || typeof env !== 'object' || !env.sid) return null;
  if (typeof env.gseq === 'number' && env.gseq > state.lastGseq) state.lastGseq = env.gseq;
  const s = ensureSession(state, env.sid, null);
  if (s.hold) { s.held.push(env); return s; }
  applyToSession(s, env);
  return s;
}

/** 对单个会话应用信封（按 gseq 去重：回放与 SSE 重叠时只处理一次） */
export function applyToSession(s, env) {
  if (typeof env.gseq === 'number') {
    if (env.gseq <= s.lastGseq) return;
    s.lastGseq = env.gseq;
  }
  const d = env.data;
  if (!d || typeof d !== 'object') return;
  try {
    if (env.kind === 'transcript' || d.type === 'transcript') {
      applyTranscript(s, d.entries, !!d.reset, d.session_id);
    } else if (env.kind === 'screen' || d.type === 'screen') {
      applyScreen(s, d.state, d.text, env.ts);
    } else if (env.kind === 'subagent' || d.type === 'subagent') {
      applySubagent(s, d);
    } else if (env.kind === 'app') {
      applyApp(s, d, env.ts);
    }
  } catch (err) {
    addItem(s, 'hint', { level: 'warn', text: '渲染事件出错：' + (err && err.message), detail: jsonPretty(d).slice(0, 4000) });
  }
  scheduleFlush(s);
}

/** 释放暂存（首屏回放完成后调用） */
export function releaseHold(s) {
  s.hold = false;
  const held = s.held;
  s.held = [];
  for (const env of held) applyToSession(s, env);
  if (s.state.onMeta) s.state.onMeta(s);
  scheduleFlush(s);
}

function addItem(s, kind, props) {
  const item = { kind, id: uid('i'), el: null, ...props };
  s.items.push(item);
  return item;
}

function markDirty(s, item) { if (item) s.dirty.add(item); }

function applyApp(s, d, ts) {
  switch (d.type) {
    case 'meta': {
      const prev = s.meta;
      s.meta = { ...s.meta, ...(d.meta || {}) };
      if (prev.state !== s.meta.state) for (const it of s.items) if (it.action === 'restart') markDirty(s, it);
      if ((s.meta.state === 'exited' || s.meta.state === 'error') && prev.state !== s.meta.state) {
        // 进程已不在：旧屏幕上的对话框/草稿不再可操作
        if (s.screen) s.screen = { ...s.screen, stale: true };
        hideDraft(s);
      }
      if (s.state.onMeta) s.state.onMeta(s);
      break;
    }
    case 'notice':
      addItem(s, 'notice', { level: d.level || 'info', text: String(d.text ?? ''), ts });
      break;
    case 'exit': {
      const code = d.exit_status ?? d.code;
      const expected = d.expected !== false && (code === 0 || code == null || d.expected === true);
      const text = exitNoticeText(code, expected, !!s.meta && s.meta.jobid === 'local');   // 本机 127 = 找不到 claude
      addItem(s, 'notice', { level: expected ? 'info' : 'error', text, ts, action: 'restart' });
      hideDraft(s);
      break;
    }
    case 'model_list':
      setModelList(s, d);
      break;
    default:
      break;
  }
}

/** 完整模型列表（POST model_picker 返回值或 app/model_list 事件） */
export function setModelList(s, d) {
  if (!d || !Array.isArray(d.rows)) return;
  s.modelList = { rows: d.rows, cursor: typeof d.cursor === 'number' ? d.cursor : null, effort: d.effort || null, at: Date.now() };
  const cur = d.rows.find((r) => r && r.current);
  if (cur && cur.name) setModelInfo(s, 'model', String(cur.name), 'model_list');
  if (d.effort && d.effort.level) setModelInfo(s, 'effort', String(d.effort.level), 'model_list');
  if (s.state.onModelList) s.state.onModelList(s);
}

// ====================== 子代理 ======================
// 服务器 data = {type:'subagent', agent_id, gen, summary, entries, from, total, reset}（回放时 entries 为空，
// 只有最新摘要；本地条目不连续/不完整时由抽屉 GET /subagents/<id> 补齐）。{agent_id, removed:true} = 已淘汰。

const AGENT_TOOLS = new Set(['Agent', 'Task']);
const AGENT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

/** 子代理视图（抽屉）里查摘要要回到所属会话 */
function rootOf(s) { return s.parent || s; }

export function applySubagent(s, d) {
  const aid = typeof d.agent_id === 'string' && AGENT_ID_RE.test(d.agent_id) ? d.agent_id : null;
  if (!aid) return;
  if (d.removed) {
    const old = s.subagents.get(aid);
    s.subagents.delete(aid);
    if (old && old.summary && old.summary.tool_use_id) s.subByTool.delete(old.summary.tool_use_id);
    if (s.state.onSubagent) s.state.onSubagent(s, aid, [], true);
    return;
  }
  let a = s.subagents.get(aid);
  let reset = false;
  if (!a || (typeof d.gen === 'number' && a.gen !== d.gen)) {
    a = { id: aid, gen: d.gen, summary: a ? a.summary : null, entries: [], base: 0, total: 0, serverBase: null, stale: false };
    s.subagents.set(aid, a);
    reset = true;
  }
  if (d.summary && typeof d.summary === 'object') a.summary = d.summary;
  const list = Array.isArray(d.entries) ? d.entries.filter((e) => e && typeof e === 'object') : [];
  const from = typeof d.from === 'number' ? d.from : null;
  let added = [];
  if (list.length && from != null) {
    const end = a.base + a.entries.length;
    if (!a.entries.length) { a.base = from; added = list; }
    else if (from <= end) added = list.slice(end - from);
    else { a.entries = []; a.base = from; added = list; a.stale = true; reset = true; } // 中间有缺口：保留最新的连续段
    a.entries.push(...added);
  }
  if (typeof d.total === 'number') a.total = d.total;
  const tid = a.summary && a.summary.tool_use_id;
  if (tid) {
    s.subByTool.set(tid, aid);
    const t = s.tools.get(tid);
    if (t) markDirty(s, t);
  }
  for (const it of s.items) if (it.kind === 'subnote' && (it.agentId === aid || (tid && it.toolUseId === tid))) markDirty(s, it);
  if (s.state.onSubagent) s.state.onSubagent(s, aid, added, reset);
}

/** 本地缓存的条目是否完整（否则打开抽屉时要 GET 补齐） */
export function subagentComplete(a) {
  if (!a) return false;
  const start = a.serverBase != null ? a.serverBase : 0;
  return !a.stale && a.base <= start && a.base + a.entries.length >= a.total;
}

/** GET /subagents/<id> 的结果整体替换本地缓存 */
export function setSubagentDetail(s, r) {
  if (!r || typeof r.agent_id !== 'string') return null;
  const a = { id: r.agent_id, gen: r.gen, summary: r.summary || null, entries: Array.isArray(r.entries) ? r.entries : [],
    base: typeof r.from === 'number' ? r.from : 0, total: typeof r.total === 'number' ? r.total : 0,
    serverBase: typeof r.from === 'number' ? r.from : 0, stale: false, truncated: !!r.truncated };
  s.subagents.set(a.id, a);
  if (a.summary && a.summary.tool_use_id) s.subByTool.set(a.summary.tool_use_id, a.id);
  return a;
}

/** 某个 Agent/Task 工具调用对应的子代理（按 tool_use_id / 启动结果里的 agentId / 通知） */
export function subagentForTool(s, t) {
  const root = rootOf(s);
  let aid = root.subByTool.get(t.toolId) || (t.note && t.note.agentId) || null;
  if (!aid && t.structured && typeof t.structured.agentId === 'string') aid = t.structured.agentId;
  if (!aid && t.result != null) {
    const m = resultText(t.result).match(/\bagentId:\s*([A-Za-z0-9_-]{1,64})/);
    if (m) aid = m[1];
  }
  return aid && AGENT_ID_RE.test(aid) ? aid : null;
}

function resultText(c) {
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) return c.filter((b) => b && b.type === 'text').map((b) => String(b.text || '')).join('\n');
  return '';
}

const NOTE_TAGS = ['task-id', 'tool-use-id', 'status', 'summary', 'output-file'];

/** <task-notification> 原文 → {agentId, toolUseId, status, rawStatus, summary, result, usage} */
export function parseTaskNotification(text) {
  text = String(text ?? '');
  if (!/<task-notification>/.test(text)) return null;
  const rm = text.match(/<result>([\s\S]*)<\/result>/);
  const head = rm ? text.slice(0, rm.index) : text;
  const tail = rm ? text.slice(rm.index + rm[0].length) : text;
  const tag = (k, src) => { const m = src.match(new RegExp('<' + k + '>([\\s\\S]*?)</' + k + '>')); return m ? m[1].trim() : null; };
  const out = {};
  for (const k of NOTE_TAGS) out[k] = tag(k, head);
  const usage = {};
  for (const k of ['subagent_tokens', 'total_tokens', 'tool_uses', 'duration_ms']) {
    const v = tag(k, tail);
    if (v != null && /^\d+$/.test(v)) usage[k] = Number(v);
  }
  const raw = String(out.status || '');
  return {
    agentId: out['task-id'] && AGENT_ID_RE.test(out['task-id']) ? out['task-id'] : null,
    toolUseId: out['tool-use-id'] || null,
    rawStatus: raw,
    status: normStatus(raw),
    summary: out.summary || '',
    result: rm ? rm[1].trim() : null,
    usage: Object.keys(usage).length ? usage : null,
  };
}

function normStatus(raw) {
  const s = String(raw || '').toLowerCase();
  if (['completed', 'complete', 'done', 'success', 'succeeded', 'finished'].includes(s)) return 'completed';
  if (/fail|error/.test(s)) return 'failed';
  if (/kill|stop|cancel|abort|interrupt|terminat/.test(s)) return 'killed';
  return 'completed';
}

/** CLI 自己注入的“用户”消息（子代理通知等）：不要显示成用户气泡 */
function isSystemInjected(e) {
  const kind = e.origin && typeof e.origin === 'object' ? e.origin.kind : null;
  if (e.promptSource === 'system') return true;
  return typeof kind === 'string' && kind !== '' && kind !== 'human' && e.promptSource !== 'typed';
}

export const SA_STATUS_TEXT = { running: '运行中', completed: '已完成', failed: '失败', killed: '已终止' };

export function saStatusIcon(st) {
  if (st === 'running') return spinner();
  if (st === 'completed') return icon('check', 'st-ok');
  if (st === 'failed') return icon('x', 'st-err');
  return icon('stop', 'st-warn');
}

/** 子代理一行状态："已完成 · 2.8s · 0 次工具调用" / "运行中 · 12s · 3 次工具 · Read foo.py" */
export function saStatusLine(sum, note) {
  const st = (sum && sum.status) || (note && note.status) || 'running';
  const parts = [SA_STATUS_TEXT[st] || st];
  const usage = (sum && sum.usage) || (note && note.usage) || null;
  const dur = sum && sum.duration_ms != null ? sum.duration_ms : usage && usage.duration_ms != null ? usage.duration_ms : null;
  const tools = sum && typeof sum.tool_uses === 'number' ? sum.tool_uses : usage && typeof usage.tool_uses === 'number' ? usage.tool_uses : null;
  return { st, parts, dur, tools, action: sum && sum.last_action ? String(sum.last_action) : '' };
}

/** 运行时长：运行中用 started_at 实时走秒（data-sa-since 由 subagents.js 的定时器刷新） */
export function saElapsed(sum, dur) {
  if (sum && sum.status === 'running' && typeof sum.started_at === 'number') {
    const el = h('span', { class: 'sa-elapsed', dataset: { saSince: String(sum.started_at) } });
    el.textContent = fmtElapsed(Date.now() / 1000 - sum.started_at);
    return el;
  }
  if (dur != null) return h('span', { class: 'sa-elapsed' }, fmtDuration(dur));
  return null;
}

export function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), r = sec % 60;
  if (m < 60) return m + 'm' + String(r).padStart(2, '0') + 's';
  return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0') + 'm';
}

// ---------- 当前模型 / effort（只从 Claude Code 自己显示/写入的内容推断） ----------

function setModelInfo(s, key, value, src) {
  if (!value) return;
  const mi = s.modelInfo;
  if (mi[key] === value && mi[key + 'Src'] === src) return;
  mi[key] = value;
  mi[key + 'Src'] = src;
  if (s.state.onModelInfo) s.state.onModelInfo(s);
}

const STATUS_MODEL_RE = /\[([^\[\]·]+?)\s*·[^\[\]]*?effort:\s*([a-z]+)\s*\]/i;
const BANNER_RE = /^[^\n]*?\s{2,}([A-Z][^\n]*?) with ([a-z]+) effort(?: ·|\s*$)/m;
const SPINNER_EFFORT_RE = /thinking with ([a-z]+) effort/i;
const SET_MODEL_RE = /(?:Set model to|Kept model as)\s+`?\**([^`*\n(]+?)\**`?(?:\s*\(|\s*$|\s*·|\s+with\b)/i;
const SET_EFFORT_RE = /effort(?: level)?(?: to| as|:)\s+`?\**([a-z]+)/i;

function inferFromScreen(s, st, text) {
  const mh = st && st.model_hint && typeof st.model_hint === 'object' ? st.model_hint : null; // 后端可选的辅助字段
  if (mh && mh.model && mh.source !== 'banner') {
    setModelInfo(s, 'model', String(mh.model), 'status');
    if (mh.effort) setModelInfo(s, 'effort', String(mh.effort), 'status');
    return;
  }
  const sl = st && typeof st.status_line === 'string' ? st.status_line : '';
  const m = sl.match(STATUS_MODEL_RE);
  if (m) { setModelInfo(s, 'model', m[1].trim(), 'status'); setModelInfo(s, 'effort', m[2].toLowerCase(), 'status'); return; }
  const sp = st && typeof st.spinner === 'string' ? st.spinner.match(SPINNER_EFFORT_RE) : null;
  if (sp) setModelInfo(s, 'effort', sp[1].toLowerCase(), 'spinner');
  if (!s.modelInfo.model && typeof text === 'string') {
    const b = text.slice(0, 2000).match(BANNER_RE);
    if (b) {
      setModelInfo(s, 'model', b[1].trim(), 'banner');
      if (!s.modelInfo.effort) setModelInfo(s, 'effort', b[2].toLowerCase(), 'banner');
    }
  }
}

// ====================== transcript ======================

const INTERRUPT_RE = /^\[Request interrupted by user/;

function stripReminders(t) {
  return String(t ?? '').replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '');
}

/** 解析 transcript 里的本地命令标记：<command-name>/x</command-name><command-args>…</command-args> 与 <local-command-stdout> */
function parseCommandText(t) {
  const cmd = t.match(/<command-name>([\s\S]*?)<\/command-name>/);
  const out = t.match(/<local-command-(stdout|stderr)>([\s\S]*?)<\/local-command-\1>/);
  const bashIn = t.match(/<bash-input>([\s\S]*?)<\/bash-input>/);
  const bashOut = t.match(/<bash-(stdout|stderr)>([\s\S]*?)<\/bash-\1>/);
  if (!cmd && !out && !bashIn && !bashOut) return null;
  const r = {};
  if (cmd) {
    let name = cmd[1].trim();
    if (!name.startsWith('/')) name = '/' + name;
    const args = ((t.match(/<command-args>([\s\S]*?)<\/command-args>/) || [])[1] || '').trim();
    r.command = (name + ' ' + args).trim();
  }
  if (bashIn) r.command = '! ' + bashIn[1].trim();
  if (out) { r.output = out[2].trim(); r.isError = out[1] === 'stderr'; }
  if (bashOut) {
    const all = [...t.matchAll(/<bash-(stdout|stderr)>([\s\S]*?)<\/bash-\1>/g)].map((x) => x[2]).filter((x) => x.trim()).join('\n');
    r.output = all.trim();
  }
  return r;
}

/** 本地命令 / 输出：命令单独一条，紧随其后的输出并入同一条 */
function addCommand(s, c, ts, level) {
  const last = s.items[s.items.length - 1];
  if (!c.command && c.output != null && last && last.kind === 'cmd' && last.output == null) {
    last.output = c.output;
    last.isError = !!c.isError;
    markDirty(s, last);
    noteCommandOutput(s, c.output);
    return;
  }
  addItem(s, 'cmd', { command: c.command || '', output: c.output ?? null, isError: !!c.isError, ts, level });
  if (c.output) noteCommandOutput(s, c.output);
}

function noteCommandOutput(s, out) {
  const m = String(out).match(SET_MODEL_RE);
  if (m) setModelInfo(s, 'model', m[1].trim(), 'command');
  const e = String(out).match(SET_EFFORT_RE);
  if (e && /^(low|medium|high|xhigh|max)$/i.test(e[1])) setModelInfo(s, 'effort', e[1].toLowerCase(), 'command');
}

/**
 * 应用一批 transcript 条目（type ∈ user/assistant/system，后端已过滤 isSidechain）。
 * reset=true：首次 / 切换 transcript 文件（如 /clear）→ 整体重建。返回新增条目数。
 */
export function applyTranscript(s, entries, reset, sessionId) {
  if (reset) {
    s.items = [];
    s.tools = new Map();
    s.orphanResults = new Map();
    s.entryIds = new Set();
    if (s.subagents.size || s.subByTool.size) {
      // 子代理随主 transcript 一起从头重建（服务器随后会重新推送）
      s.subagents = new Map();
      s.subByTool = new Map();
      if (s.state.onSubagent) s.state.onSubagent(s, null, [], true);
    }
    s.dirty.clear();
    s.renderStart = 0;
    s.renderedTo = 0;
    s.needsReorder = true;
  }
  if (sessionId) s.transcriptId = sessionId;
  const before = s.items.length;
  let assistantSeen = false;
  for (const e of Array.isArray(entries) ? entries : []) {
    if (!e || typeof e !== 'object') continue;
    if ((e.isSidechain && !s.sidechain) || e.isMeta) continue;
    const id = typeof e.uuid === 'string' && e.uuid ? e.uuid : null;
    if (id) {
      if (s.entryIds.has(id)) continue;
      s.entryIds.add(id);
    }
    const ts = e.timestamp ? Date.parse(e.timestamp) / 1000 : null;
    if (typeof e.cwd === 'string' && e.cwd.startsWith('/') && !s.sidechain) s.absCwd = e.cwd;   // 相对路径按它解析
    try {
      if (e.type === 'assistant') { onAssistant(s, e, ts); assistantSeen = true; }
      else if (e.type === 'user') { if (onUser(s, e, ts)) assistantSeen = true; }
      else if (e.type === 'system') onSystem(s, e, ts);
    } catch (err) {
      addItem(s, 'hint', { level: 'warn', text: 'transcript 条目渲染出错：' + (err && err.message) });
    }
  }
  if (reset && s.view) {
    s.renderStart = Math.max(0, s.items.length - INITIAL_WINDOW);
    s.renderedTo = s.renderStart;
    s.needsReorder = true;
  }
  if (assistantSeen && !reset) {
    s.lastAssistantAt = performance.now();
    const st = s.screen && s.screen.state;
    // 空闲后到达的助手条目：草稿的使命已完成
    if (!(st && st.busy)) hideDraft(s);
  }
  scheduleFlush(s);
  return s.items.length - before;
}

function blockKind(type) {
  if (type === 'text') return 'text';
  if (type === 'thinking' || type === 'redacted_thinking') return 'thinking';
  if (type === 'tool_use' || type === 'server_tool_use' || type === 'mcp_tool_use') return 'tool';
  return 'block';
}

function onAssistant(s, e, ts) {
  const m = e.message || {};
  const blocks = Array.isArray(m.content) ? m.content : typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : [];
  if (typeof e.effort === 'string' && /^(low|medium|high|xhigh|max)$/.test(e.effort)) setModelInfo(s, 'effort', e.effort, 'transcript');
  for (const b of blocks) {
    if (!b || typeof b !== 'object') continue;
    const kind = blockKind(b.type);
    const props = { msgId: m.id || null, ts, truncated: !!(b.truncated || e.truncated) };
    if (kind === 'text') {
      const text = String(b.text ?? '');
      if (!text.trim()) continue;
      addItem(s, 'text', { ...props, text, apiError: !!e.isApiErrorMessage });
    } else if (kind === 'thinking') {
      const text = String(b.thinking ?? '');
      addItem(s, 'thinking', { ...props, text, redacted: b.type === 'redacted_thinking', expanded: false });
    } else if (kind === 'tool') {
      const toolId = b.id || uid('tool');
      const t = addItem(s, 'tool', {
        ...props, toolId, name: String(b.name || ''), input: b.input && typeof b.input === 'object' ? b.input : null,
        result: undefined, isError: false, denied: false, done: false, interrupted: false, expanded: b.name === 'TodoWrite',
      });
      s.tools.set(toolId, t);
      noteToolAdded(s, t, (x) => markDirty(s, x));
      const orphan = s.orphanResults.get(toolId);
      if (orphan) { s.orphanResults.delete(toolId); applyToolResult(s, orphan.b, orphan.e); }
    } else addItem(s, 'block', { ...props, block: b });
  }
  if (m.stop_reason === 'end_turn') closeTurn(s);
}

/** 一轮结束（turn_duration / end_turn / 下一条用户消息 / 中断）：本轮有文件改动等 → 追加「本轮改动」卡片 */
function closeTurn(s) {
  const props = collectTurn(s);
  if (props) addItem(s, 'turnsum', props);
}

function applyToolResult(s, b, e) {
  const t = s.tools.get(b.tool_use_id);
  if (!t) { if (b.tool_use_id) s.orphanResults.set(b.tool_use_id, { b, e }); return; }
  t.result = b.content;
  t.isError = !!b.is_error;
  t.done = true;
  if (b.truncated || e.truncated) t.truncated = true;
  if (e.toolDenialKind || (typeof e.toolUseResult === 'string' && /rejected/i.test(e.toolUseResult))) t.denied = true;
  if (e.toolUseResult && typeof e.toolUseResult === 'object' && !Array.isArray(e.toolUseResult)) t.structured = e.toolUseResult;
  markDirty(s, t);
  if (t.groupHead) markDirty(s, t.groupHead);   // 工具组摘要（进行中/失败数）
}

/** 返回 true 表示这是一轮结束类的条目（中断 / 工具结果），用于草稿隐藏判断 */
function onUser(s, e, ts) {
  const m = e.message || {};
  const hasToolResult = Array.isArray(m.content) && m.content.some((b) => b && b.type === 'tool_result');
  if (!hasToolResult) {
    const raw = typeof m.content === 'string' ? m.content : resultText(m.content);
    const note = /^\s*<task-notification>/.test(raw) || isSystemInjected(e) ? parseTaskNotification(raw) : null;
    if (note) { addSubnote(s, note, ts); return false; }
    const text0 = stripReminders(raw).trim();
    if (isSystemInjected(e) && !INTERRUPT_RE.test(text0) && !parseCommandText(text0) && !/^<local-command-caveat>/.test(text0)) {
      // 其它系统注入消息：小系统行（可展开原文），不伪装成用户消息（中断标记、本地命令记录仍走原来的渲染）
      const text = text0;
      if (text) addItem(s, 'hint', { level: 'info', text: '系统消息：' + truncate(text.split('\n')[0], 160), detail: text, ts });
      return false;
    }
  }
  if (typeof m.content === 'string') {
    const raw = stripReminders(m.content);
    const c = parseCommandText(raw);
    if (c) { addCommand(s, c, ts); return false; }
    if (/^\s*<local-command-caveat>/.test(raw)) return false;
    const text = raw.trim();
    if (text) { closeTurn(s); addItem(s, 'user', { text, ts, truncated: !!e.truncated }); }
    return false;
  }
  const content = Array.isArray(m.content) ? m.content : [];
  const texts = [];
  const images = [];
  let ended = false;
  for (const b of content) {
    if (!b || typeof b !== 'object') continue;
    if (b.type === 'tool_result') { applyToolResult(s, b, e); ended = true; }
    else if (b.type === 'text') {
      const t = String(b.text || '');
      if (INTERRUPT_RE.test(t)) {
        for (const it of s.tools.values()) if (!it.done && !it.interrupted) { it.interrupted = true; markDirty(s, it); }
        closeTurn(s);
        addItem(s, 'notice', { level: 'warn', text: /tool use/.test(t) ? '已中断（工具调用被拒绝或取消）' : '已中断本轮回复', ts });
        ended = true;
      } else {
        const raw = stripReminders(t);
        const c = parseCommandText(raw);
        if (c) addCommand(s, c, ts);
        else if (raw.trim() && !/^\s*<local-command-caveat>/.test(raw)) texts.push(raw.trim());
      }
    } else if (b.type === 'image') images.push(b);
  }
  if (texts.length || images.length) {
    closeTurn(s);
    addItem(s, 'user', { text: texts.join('\n\n'), imageBlocks: images, ts, truncated: !!e.truncated });
  }
  return ended;
}

/** 子代理结束通知：挂到对应 Agent 卡上（卡片状态/结果），并在时间线上留一条小系统行 */
function addSubnote(s, note, ts) {
  const t = note.toolUseId ? s.tools.get(note.toolUseId) : null;
  if (t) { t.note = note; markDirty(s, t); }
  if (note.agentId && note.toolUseId && !rootOf(s).subByTool.has(note.toolUseId)) rootOf(s).subByTool.set(note.toolUseId, note.agentId);
  addItem(s, 'subnote', { note, agentId: note.agentId, toolUseId: note.toolUseId, ts, expanded: false,
    desc: t && t.input && t.input.description ? String(t.input.description) : '' });
}

function onSystem(s, e, ts) {
  const st = e.subtype;
  const content = typeof e.content === 'string' ? stripReminders(e.content) : '';
  if (st === 'compact_boundary') { addItem(s, 'divider', { text: '上下文已压缩' }); return; }
  if (st === 'turn_duration') { closeTurn(s); return; }
  if (content) {
    const c = parseCommandText(content);
    if (c) { addCommand(s, c, ts, e.level); return; }
  }
  if (!content.trim()) return; // turn_duration 等无正文的系统条目不显示
  const level = e.level === 'error' ? 'error' : e.level === 'warning' || e.level === 'warn' ? 'warn' : 'info';
  addItem(s, 'hint', { level, text: truncate(content.trim().split('\n')[0], 200), detail: content.includes('\n') || content.length > 200 ? content : '', ts });
}

/** 历史预览：/api/history/<id> 的 entries（与 transcript 同结构）。返回渲染条目数 */
export function applyHistory(s, entries) {
  applyTranscript(s, entries, true);
  return s.items.length;
}

/** 追加一条本地提示（例如网络错误），不经过事件信封 */
export function addLocalNotice(s, level, text) {
  addItem(s, 'notice', { level, text, ts: Date.now() / 1000 });
  scheduleFlush(s);
}

// ====================== 屏幕：草稿与对话框 ======================

/** 应用最新屏幕帧（只保留最新一帧） */
export function applyScreen(s, st, text, ts) {
  if (!st || typeof st !== 'object') st = {};
  const prev = s.screen && s.screen.state;
  const wasBusy = !!(prev && prev.busy && !s.screen.stale);
  s.screen = { state: st, text: typeof text === 'string' ? text : '', ts: ts || Date.now() / 1000, stale: false };
  const d = s.draft;
  if (st.busy) {
    if (!wasBusy) d.lastLive = '';
    if (typeof st.live === 'string' && st.live.trim()) d.lastLive = st.live;
    d.awaiting = true;
    d.busyEndedAt = 0;
    clearTimeout(d.timer);
  } else if (wasBusy) {
    d.busyEndedAt = performance.now();
    if (performance.now() - s.lastAssistantAt < RECENT_ENTRY_MS) hideDraft(s);
    else {
      clearTimeout(d.timer);
      d.timer = setTimeout(() => { hideDraft(s); scheduleFlush(s); }, IDLE_GRACE_MS);
    }
  }
  if (st.dialog && st.dialog.kind === 'model' && Array.isArray(st.dialog.rows)) {
    for (const r of st.dialog.rows) if (r && typeof r.n === 'number') s.screenModelRows.set(r.n, r);
  } else if (!st.dialog) s.screenModelRows.clear();
  inferFromScreen(s, st, text);
  if (s.state.onScreen) s.state.onScreen(s);
  scheduleFlush(s);
}

function hideDraft(s) {
  const d = s.draft;
  d.awaiting = false;
  d.lastLive = '';
  clearTimeout(d.timer);
  d.timer = 0;
}

/** 草稿要显示的实时内容：忙时 = 本帧 live；刚空闲、transcript 尚未到达时保留最后一次看到的内容（平滑过渡） */
function draftLive(s) {
  const st = (s.screen && s.screen.state) || {};
  const live = typeof st.live === 'string' ? st.live : '';
  if (st.busy) return live;
  return live.trim() ? live : s.draft.lastLive;
}

/** 当前是否显示“生成中”草稿 */
export function draftVisible(s) {
  if (s.readOnly || !s.screen || s.screen.stale) return false;
  const st = s.screen.state || {};
  if (st.busy) return true;
  return !!(s.draft.awaiting && String(draftLive(s) || '').trim());
}

/** 当前（未过期）的对话框 */
export function currentDialog(s) {
  if (s.readOnly || !s.screen || s.screen.stale) return null;
  const st = s.screen.state;
  return st && st.dialog && typeof st.dialog === 'object' ? st.dialog : null;
}

// ====================== 视图 ======================

export function mountSession(s, host) {
  if (s.view) { host.appendChild(s.view.root); return s.view; }
  const more = h('button', { class: 'more-btn', type: 'button', hidden: true });
  const list = h('div', { class: 'msg-list' });
  const starting = h('div', { class: 'working', hidden: true });
  const draftSpin = h('span', { class: 'draft-spin' });
  const draftText = h('span', { class: 'draft-status' });
  const draftPre = h('pre', { class: 'draft-live' });
  const draft = h('div', { class: 'item draft', hidden: true, 'aria-live': 'off' },
    h('div', { class: 'draft-head' }, draftSpin, draftText, h('span', { class: 'draft-tag' }, '实时草稿')),
    draftPre);
  const dialog = h('div', { class: 'dialog-slot' });
  const tail = h('div', { class: 'msg-tail' }, starting, draft, dialog);
  const col = h('div', { class: 'chat-col' }, more, list, tail);
  const jump = h('button', { class: 'jump-btn', type: 'button', hidden: true, title: '滚动到底部' }, icon('down'), '最新');
  const root = h('div', { class: 'chat-scroll', dataset: { sid: s.sid } }, col, jump);
  const view = {
    root, col, list, tail, more, jump, stick: true,
    starting, draft, draftSpin, draftText, draftPre, dialog, dialogKey: null, dialogCard: null,
  };
  s.view = view;
  root.addEventListener('scroll', () => {
    view.stick = root.scrollHeight - root.scrollTop - root.clientHeight < 60;
    if (view.stick) jump.hidden = true;
  }, { passive: true });
  jump.addEventListener('click', () => { root.scrollTop = root.scrollHeight; view.stick = true; jump.hidden = true; });
  more.addEventListener('click', () => showMore(s));
  s.renderStart = Math.max(0, s.items.length - INITIAL_WINDOW);
  s.renderedTo = s.renderStart;
  s.needsReorder = true;
  host.appendChild(root);
  flushSession(s);
  root.scrollTop = root.scrollHeight;
  return view;
}

export function scrollToBottom(s) {
  if (!s.view) return;
  s.view.root.scrollTop = s.view.root.scrollHeight;
  s.view.stick = true;
  s.view.jump.hidden = true;
}

function showMore(s) {
  renderEarlier(s, Math.max(0, s.renderStart - MORE_STEP));
}

/** 把 [start, renderStart) 补渲染到列表前面，并保持屏幕上的内容不动（WebKit 没有滚动锚定，手动补 scrollTop） */
function renderEarlier(s, start) {
  const v = s.view;
  const oldStart = s.renderStart;
  if (!(start < oldStart)) return;
  s.renderStart = start;
  const before = v.root.scrollHeight;
  const frag = document.createDocumentFragment();
  for (let k = s.renderStart; k < oldStart; k++) frag.appendChild(ensureEl(s, s.items[k]));
  v.list.insertBefore(frag, v.list.firstChild);
  updateMore(s);
  v.root.scrollTop += v.root.scrollHeight - before;
  notifyFlush(s);
}

/** 确保条目已渲染进 DOM（左侧 prompt 小格子跳转用）：还在 renderStart 之前 → 从它开始补渲染（保持滚动位置）。
 *  返回 false = 不是这个会话的条目 */
export function revealItem(s, item) {
  if (!s.view) return false;
  if (s.needsReorder || s.renderedTo < s.items.length) flushSession(s);
  const k = s.items.indexOf(item);
  if (k < 0) return false;
  if (k < s.renderStart) renderEarlier(s, k);
  return true;
}

function notifyFlush(s) { if (s.state && s.state.onFlush) s.state.onFlush(s); }

function updateMore(s) {
  const v = s.view;
  v.more.hidden = s.renderStart <= 0;
  v.more.textContent = `显示更早的 ${Math.min(MORE_STEP, s.renderStart)} 条（共 ${s.renderStart} 条未显示）`;
}

const pendingFlush = new Set();
let rafId = 0;

export function scheduleFlush(s) {
  pendingFlush.add(s);
  if (!rafId) rafId = requestAnimationFrame(flushAll);
}

function flushAll() {
  rafId = 0;
  const list = [...pendingFlush];
  pendingFlush.clear();
  for (const s of list) if (s.view) flushSession(s);
}

/** 立即同步刷新（回放页/测试用） */
export function flushNow(s) { if (s.view) flushSession(s); }

function ensureEl(s, item) {
  if (!item.el) { item.el = h('div', { class: 'item' }); renderItem(s, item); s.dirty.delete(item); }
  return item.el;
}

function flushSession(s) {
  const v = s.view;
  const root = v.root;
  const stick = v.stick;
  const prevHeight = root.scrollHeight;
  if (s.needsReorder) {
    if (s.renderStart > s.items.length) s.renderStart = Math.max(0, s.items.length - INITIAL_WINDOW);
    const frag = document.createDocumentFragment();
    for (let k = s.renderStart; k < s.items.length; k++) frag.appendChild(ensureEl(s, s.items[k]));
    v.list.replaceChildren(frag);
    s.renderedTo = s.items.length;
    s.needsReorder = false;
  } else if (s.renderedTo < s.items.length) {
    const frag = document.createDocumentFragment();
    // 实时追加的少量条目轻微淡入（一次涌入很多条 = 打开会话/补历史，不做动画）
    const enter = s.items.length - s.renderedTo <= 4;
    for (let k = s.renderedTo; k < s.items.length; k++) {
      const el = ensureEl(s, s.items[k]);
      if (enter) el.classList.add('enter');
      frag.appendChild(el);
    }
    v.list.appendChild(frag);
    s.renderedTo = s.items.length;
  }
  for (const item of s.dirty) if (item.el) renderItem(s, item);
  s.dirty.clear();
  updateMore(s);
  renderTail(s);
  if (stick) root.scrollTop = root.scrollHeight;
  else if (root.scrollHeight > prevHeight + 4) v.jump.hidden = false;
  notifyFlush(s);
}

/** 状态变化时由外部调用（meta 更新后刷新尾部） */
export function refreshTail(s) { if (s.view) { renderTail(s); if (s.view.stick) s.view.root.scrollTop = s.view.root.scrollHeight; } }

function renderTail(s) {
  const v = s.view;
  const m = s.meta;
  // 启动中
  const starting = !s.readOnly && m.state === 'starting';
  v.starting.hidden = !starting;
  if (starting && !v.starting.firstChild) v.starting.replaceChildren(spinner(), m.jobid === 'local' ? '正在启动 claude…（本机 tmux，通常几秒）' : '正在启动 claude…（ssh → srun → tmux，通常需要 10–30 秒）');
  // 草稿：只改文字，不重建节点（平滑不闪）
  const show = draftVisible(s);
  v.draft.hidden = !show;
  if (show) {
    const st = s.screen.state || {};
    const busy = !!st.busy;
    const status = st.spinner ? String(st.spinner) : busy ? 'Claude 正在工作…' : '等待 transcript 写入…';
    if (v.draftText.textContent !== status) v.draftText.textContent = status;
    v.draft.classList.toggle('busy', busy);
    const live = String(draftLive(s) || '').replace(/[ \t]+$/gm, '').replace(/^\n+|\n+$/g, '');
    if (v.draftPre.textContent !== live) v.draftPre.textContent = live;
    v.draftPre.hidden = !live;
  }
  // 对话框卡片
  renderDialogSlot(s);
}

// ---------- 对话框卡片 ----------

const TYPE_SOMETHING_RE = /^type something\.?$/i;

function dialogKey(d) {
  const opts = Array.isArray(d.options) ? d.options.map((o) => [o.index, o.n, o.label, o.desc]) : [];
  const lines = (d.kind === 'trust' || d.kind === 'generic' || d.kind === 'effort')
    ? (Array.isArray(d.lines) ? d.lines.map((l) => String(l).replace(/❯/g, ' ').trimEnd()) : []) : [];
  return JSON.stringify([d.kind, d.title, d.tool, d.question, d.header, d.path, d.preview, d.hints, opts, d.kind === 'effort' ? null : lines]);
}

function renderDialogSlot(s) {
  const v = s.view;
  const d = currentDialog(s);
  if (!d) {
    if (v.dialogKey !== null) { v.dialog.replaceChildren(); v.dialogKey = null; v.dialogCard = null; }
    s.dialogUi = null;
    return;
  }
  const key = dialogKey(d) + '|' + (d.kind === 'effort' ? String(d.level) + String(d.ultracode) : '');
  if (key !== v.dialogKey) {
    // 同类对话框内容变化（例如光标移到自由输入项后行文字变化）时保留用户已输入的文字，但清除“提交中”
    const prev = s.dialogUi && s.dialogUi.kind === d.kind ? s.dialogUi : null;
    const ui = { key, kind: d.kind, freeIndex: prev ? prev.freeIndex : null, freeText: prev ? prev.freeText : '' };
    ui.pending = false;
    ui.error = '';
    ui.expect = dialogFingerprint(d);   // choose 时带给后端：远端对话框已不是这张卡片 → 409 dialog_changed
    s.dialogUi = ui;
    const active = document.activeElement;
    const hadFocus = active && v.dialog.contains(active) && active.classList.contains('dlg-free-input');
    const card = buildDialogCard(s, d);
    if (v.dialogKey === null) card.classList.add('enter');   // 新出现的对话框轻微淡入；同一对话框内容变化重建时不再动画
    v.dialog.replaceChildren(card);
    v.dialogKey = key;
    v.dialogCard = card;
    if (hadFocus) { const inp = card.querySelector('.dlg-free-input'); if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); } }
  }
  // 选中项（光标）只更新样式，不重建
  updateSelection(v.dialogCard, d);
  updatePending(s);
}

function updateSelection(card, d) {
  if (!card) return;
  const sel = Array.isArray(d.options) ? d.options.find((o) => o && o.selected) : null;
  for (const b of card.querySelectorAll('[data-opt-index]')) {
    b.classList.toggle('cursor', !!sel && String(sel.index) === b.dataset.optIndex);
  }
}

function updatePending(s) {
  const v = s.view;
  const ui = s.dialogUi;
  if (!v.dialogCard || !ui) return;
  const busy = !!ui.pending;
  for (const b of v.dialogCard.querySelectorAll('button.dlg-act, input.dlg-free-input')) b.disabled = busy;
  const st = v.dialogCard.querySelector('.dlg-status');
  if (st) {
    if (busy) st.replaceChildren(spinner(), '已发送，等待终端响应…');
    else if (ui.error) st.replaceChildren(h('span', { class: 'card-error' }, ui.error));
    else st.replaceChildren();
  }
}

/** 对话框指纹（与 ccr/session.py 的 dialog_fingerprint 一致） */
function dialogFingerprint(d) {
  return {
    kind: String(d.kind || ''), title: String(d.title || ''), question: String(d.question || ''),
    preview: Array.isArray(d.preview) ? d.preview.map(String) : [],
    labels: Array.isArray(d.options) ? d.options.map((o) => String((o && o.label) || '')) : [],
  };
}

/** 卡片按钮 → actions（只在用户点击时调用） */
function dialogAction(s, name, ...args) {
  const fn = s.state.actions && s.state.actions[name];
  if (!fn || !s.dialogUi || s.dialogUi.pending) return;
  const ui = s.dialogUi;
  if (name === 'choose' || name === 'freeText') args.push(ui.expect);   // 最后一个参数 = 指纹
  ui.pending = true;
  ui.error = '';
  if (s.view) updatePending(s);
  const reset = setTimeout(() => {
    if (s.dialogUi === ui && ui.pending) { ui.pending = false; if (s.view) updatePending(s); }
  }, PENDING_RESET_MS);
  Promise.resolve().then(() => fn(s, ...args)).then(() => {
    // 成功：等下一帧屏幕把对话框变化/关闭；PENDING_RESET_MS 后仍无变化则恢复可点
  }, (err) => {
    clearTimeout(reset);
    if (s.dialogUi !== ui) return;
    ui.pending = false;
    ui.error = (err && err.message) || String(err);
    if (s.view) updatePending(s);
  });
}

function optButton(s, o, cls) {
  const label = (o.n != null ? o.n + '. ' : '') + String(o.label ?? '');
  const b = h('button', { class: 'dlg-opt dlg-act' + (cls ? ' ' + cls : ''), type: 'button', dataset: { optIndex: String(o.index) }, title: '选择此项（在终端里移动光标并按 Enter）' },
    h('span', { class: 'dlg-opt-label' }, label),
    o.desc ? h('span', { class: 'dlg-opt-desc' }, String(o.desc)) : null);
  b.addEventListener('click', () => dialogAction(s, 'choose', o.index));
  return b;
}

function escButton(s, text = '取消（Esc）') {
  const b = h('button', { class: 'btn ghost small dlg-act', type: 'button', title: '在终端里按 Esc' }, text);
  b.addEventListener('click', () => dialogAction(s, 'cancelDialog'));
  return b;
}

const KEYPAD = [
  ['↑', 'Up'], ['↓', 'Down'], ['←', 'Left'], ['→', 'Right'], ['Enter', 'Enter'], ['Esc', 'Escape'], ['Tab', 'Tab'],
];

export function keypad(onKey, keys = KEYPAD, cls = 'dlg-act') {
  return h('div', { class: 'keypad' }, keys.map(([label, key]) => {
    const b = h('button', { class: 'key ' + cls, type: 'button', title: '发送按键 ' + key }, label);
    b.addEventListener('click', () => onKey(key));
    return b;
  }));
}

function dlgShell(kind, iconName, title, ...body) {
  return h('div', { class: 'item card dlg-card kind-' + kind },
    h('div', { class: 'card-head' }, icon(iconName, 'card-ic'), h('span', { class: 'card-title' }, title)),
    ...body);
}

function optionsOf(d) { return Array.isArray(d.options) ? d.options.filter((o) => o && typeof o.index === 'number') : []; }

function rawLines(d, skip) {
  const lines = Array.isArray(d.lines) ? d.lines.map((l) => String(l)) : [];
  return lines.filter((l) => !skip || !skip(l)).join('\n').replace(/[ \t]+$/gm, '').replace(/\n{3,}/g, '\n\n').replace(/^\n+|\n+$/g, '');
}

function buildDialogCard(s, d) {
  switch (d.kind) {
    case 'permission': return permissionCard(s, d);
    case 'question': return questionCard(s, d);
    case 'trust': return trustCard(s, d);
    case 'model': return modelCard(s, d);
    case 'effort': return effortCard(s, d);
    default: return genericCard(s, d);
  }
}

/** 权限对话框的描述行：标题之后、预览（╌ 包围）之前，去掉标题本身 */
function permDescription(d) {
  const lines = Array.isArray(d.lines) ? d.lines.map((l) => String(l).trim()) : [];
  const out = [];
  const title = String(d.tool || d.title || '').trim();
  let started = false;
  for (const l of lines) {
    if (/^[─▔]{3,}/.test(l)) continue;
    if (!started) { if (l === title || !title) { started = true; if (l === title) continue; } else continue; }
    if (/^╌{3,}/.test(l)) break;
    if (d.question && l === String(d.question).trim()) break;
    if (/^(❯\s*)?\d+\.\s/.test(l)) break;
    if (l) out.push(l);
  }
  return out.join('\n');
}

function permissionCard(s, d) {
  const opts = optionsOf(d);
  const desc = typeof d.description === 'string' ? d.description : permDescription(d);
  const preview = Array.isArray(d.preview) ? d.preview.map(String).join('\n').replace(/[ \t]+$/gm, '') : '';
  const last = opts.length - 1;
  const buttons = opts.map((o, k) => optButton(s, o, k === 0 ? 'primary' : (k === last && /^no\b/i.test(String(o.label))) ? 'deny' : ''));
  return dlgShell('permission', 'shield', '需要授权',
    h('div', { class: 'dlg-sub' }, h('span', { class: 'tool-name' }, String(d.tool || d.title || '工具'))),
    desc ? h('div', { class: 'card-desc dlg-pre-wrap' }, desc) : null,
    d.tip ? h('div', { class: 'muted small-text dlg-pre-wrap' }, String(d.tip)) : null,
    preview ? h('pre', { class: 'tool-pre cmd dlg-preview' }, preview) : null,
    d.question ? h('div', { class: 'dlg-question' }, String(d.question)) : null,
    h('div', { class: 'dlg-opts' }, buttons),
    h('div', { class: 'card-actions' }, escButton(s), d.hints ? h('span', { class: 'muted small-text' }, String(d.hints)) : null, h('span', { class: 'dlg-status' })));
}

function questionCard(s, d) {
  const ui = s.dialogUi;
  const opts = optionsOf(d);
  const freeBox = h('div', { class: 'dlg-free', hidden: ui.freeIndex == null });
  const input = h('textarea', { class: 'input dlg-free-input', rows: '2', name: 'free-answer', 'aria-label': '自由输入回答', placeholder: '输入你的回答（Enter 提交 · Shift+Enter 换行）' });
  input.value = ui.freeText || '';
  const submit = h('button', { class: 'btn primary small dlg-act', type: 'button' }, '提交回答');
  const doSubmit = () => {
    const text = input.value.trim();
    if (!text || ui.freeIndex == null) { input.focus(); return; }
    dialogAction(s, 'freeText', ui.freeIndex, text);
  };
  input.addEventListener('input', () => { ui.freeText = input.value; });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); doSubmit(); }
  });
  submit.addEventListener('click', doSubmit);
  const cancelFree = h('button', { class: 'btn ghost small', type: 'button' }, '收起');
  cancelFree.addEventListener('click', () => { ui.freeIndex = null; freeBox.hidden = true; });
  freeBox.append(input, h('div', { class: 'card-actions' }, submit, cancelFree,
    h('span', { class: 'muted small-text' }, '提交 = 在终端里选中“Type something.”、粘贴文字并回车')));
  const buttons = opts.map((o) => {
    if (o.free_text === true || TYPE_SOMETHING_RE.test(String(o.label || '').trim())) {
      // 自由输入：点击只展开文本框（不发请求）；提交时才 choose(index) + 发送文字
      const b = h('button', { class: 'dlg-opt dlg-act free', type: 'button', dataset: { optIndex: String(o.index) } },
        h('span', { class: 'dlg-opt-label' }, (o.n != null ? o.n + '. ' : '') + '自己输入…'),
        h('span', { class: 'dlg-opt-desc' }, String(o.label)));
      b.addEventListener('click', () => { ui.freeIndex = o.index; freeBox.hidden = false; input.focus(); });
      return b;
    }
    return optButton(s, o);
  });
  return dlgShell('question', 'question', 'Claude 想问你',
    d.header ? h('div', { class: 'dlg-sub' }, h('span', { class: 'chip' }, String(d.header))) : null,
    d.question ? h('div', { class: 'dlg-question' }, String(d.question)) : null,
    h('div', { class: 'dlg-opts' }, buttons),
    freeBox,
    h('div', { class: 'card-actions' }, escButton(s), d.hints ? h('span', { class: 'muted small-text' }, String(d.hints)) : null, h('span', { class: 'dlg-status' })));
}

function trustCard(s, d) {
  const optLabels = new Set(optionsOf(d).map((o) => String(o.label).trim()));
  const text = rawLines(d, (l) => {
    const t = l.replace(/^\s*❯?\s*/, '').trim();
    return optLabels.has(t) || /^[─▔]{3,}/.test(l.trim()) || (d.hints && t === String(d.hints).trim());
  });
  const opts = optionsOf(d);
  return dlgShell('trust', 'folder', '是否信任此目录？',
    d.path ? h('div', { class: 'perm-path' }, icon('folder'), String(d.path)) : null,
    h('div', { class: 'dlg-raw' }, text),
    h('div', { class: 'dlg-opts' }, opts.map((o) => optButton(s, o, /trust/i.test(String(o.label)) ? 'primary' : ''))),
    h('div', { class: 'card-actions' }, escButton(s), d.hints ? h('span', { class: 'muted small-text' }, String(d.hints)) : null, h('span', { class: 'dlg-status' })));
}

/** 模型面板已打开：主要交互在弹窗里（app 负责），卡片只提供“打开选择窗口 / 取消” */
function modelCard(s, d) {
  const open = h('button', { class: 'btn primary small dlg-act', type: 'button' }, icon('spark'), '打开模型选择窗口');
  open.addEventListener('click', () => { const fn = s.state.actions.openModelDialog; if (fn) fn(s); });
  return dlgShell('model', 'spark', '终端里的模型选择面板已打开',
    h('div', { class: 'card-desc' }, '请在弹出的窗口中选择模型与 effort；“仅本会话”不会改动任何设置文件。'),
    h('div', { class: 'card-actions' }, open, escButton(s), h('span', { class: 'dlg-status' })));
}

/** /effort 面板（用户自己输入 /effort 打开）：显示当前档位，按钮发送 ←/→/s/Enter/Esc */
function effortCard(s, d) {
  const level = d.level ? String(d.level) : '';
  const seg = h('div', { class: 'seg readonly' }, EFFORT_LEVELS.map((l) => h('span', { class: 'seg-item' + (l === level ? ' on' : '') }, l)));
  const key = (k) => dialogAction(s, 'keys', [k]);
  const left = h('button', { class: 'btn small dlg-act', type: 'button' }, '← 降低');
  left.addEventListener('click', () => key('Left'));
  const right = h('button', { class: 'btn small dlg-act', type: 'button' }, '提高 →');
  right.addEventListener('click', () => key('Right'));
  const sess = h('button', { class: 'btn primary small dlg-act', type: 'button' }, '仅本会话（s）');
  sess.addEventListener('click', () => key('s'));
  const save = h('button', { class: 'btn small dlg-act', type: 'button' }, '保存为默认（Enter）');
  save.addEventListener('click', () => key('Enter'));
  return dlgShell('effort', 'spark', '终端里的 effort 面板已打开',
    seg,
    d.ultracode ? h('div', { class: 'muted small-text' }, 'Ultracode：' + String(d.ultracode)) : null,
    h('div', { class: 'card-actions' }, left, right),
    h('div', { class: 'card-actions' }, sess, save, escButton(s), h('span', { class: 'dlg-status' })),
    h('div', { class: 'muted small-text' }, '“保存为默认”会写入' + (s.meta && s.meta.jobid === 'local' ? '本机' : '服务器') + ' ~/.claude/settings.json，和你在终端里按 Enter 一样。'));
}

function genericCard(s, d) {
  const text = rawLines(d);
  const opts = optionsOf(d);
  return dlgShell('generic', 'terminal', d.title ? String(d.title) : '终端里有一个未识别的对话框',
    h('pre', { class: 'tool-pre dlg-generic' }, text || '（无内容）'),
    opts.length ? h('div', { class: 'dlg-opts' }, opts.map((o) => optButton(s, o))) : null,
    keypad((k) => dialogAction(s, 'keys', [k])),
    h('div', { class: 'card-actions' }, escButton(s), d.hints ? h('span', { class: 'muted small-text' }, String(d.hints)) : null, h('span', { class: 'dlg-status' })));
}

// ---------- 各类条目渲染 ----------

function renderItem(s, item) {
  const el = item.el;
  const cls = ['item', 'k-' + item.kind];
  switch (item.kind) {
    case 'user': return renderUser(s, item, el, cls);
    case 'text': return renderText(s, item, el, cls);
    case 'turnsum': return renderTurnSummary(s, item, el, cls);
    case 'thinking': return renderThinking(s, item, el, cls);
    case 'tool': return renderTool(s, item, el, cls);
    case 'cmd': return renderCmd(item, el, cls);
    case 'hint': return renderHint(item, el, cls);
    case 'notice': return renderNotice(s, item, el, cls);
    case 'subnote': return renderSubnote(s, item, el, cls);
    case 'divider':
      el.className = cls.join(' ');
      el.replaceChildren(h('span', null, item.text));
      return;
    case 'block':
      el.className = cls.join(' ');
      el.replaceChildren(h('details', { class: 'hint-detail' }, h('summary', null, '内容块：' + String(item.block && item.block.type)), h('pre', { class: 'tool-pre' }, jsonPretty(item.block))));
      return;
    default:
      el.className = cls.join(' ');
  }
}

/** 消息时间：今天只显示 时:分，否则带上日期 */
function shortTime(ts) {
  const d = new Date(ts * 1000);
  if (isNaN(d)) return '';
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  return d.toDateString() === new Date().toDateString() ? hm : (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm;
}

/** 悬停才显示的消息元信息（参考 T3 Code）：时间 + 复制原文按钮；样式见 style.css .msg-meta */
function msgMeta(text, ts, cls, copyFirst) {
  const time = ts ? h('span', { class: 'mm-time', title: fmtTime(ts) }, shortTime(ts)) : null;
  let btn = null;
  if (text) {
    btn = h('button', { class: 'mm-btn', type: 'button', title: '复制', 'aria-label': '复制这条消息' }, icon('copy'));
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const ok = await copyText(text);
      btn.classList.toggle('ok', ok);
      btn.title = ok ? '已复制' : '复制失败';
      btn.replaceChildren(icon(ok ? 'check' : 'x'));
      setTimeout(() => { btn.classList.remove('ok'); btn.title = '复制'; btn.replaceChildren(icon('copy')); }, 1500);
    });
  }
  if (!time && !btn) return null;
  return h('div', { class: 'msg-meta ' + cls }, copyFirst ? [btn, time] : [time, btn]);
}

function renderUser(s, item, el, cls) {
  el.className = cls.join(' ');
  if (item.ts) el.title = fmtTime(item.ts);
  const bubble = h('div', { class: 'bubble' });
  if (item.imageBlocks && item.imageBlocks.length) {
    // 用户粘贴的图片：小图内联，大图是 ccr_ref 引用（按需加载）；点击打开灯箱
    const imgs = item.imageBlocks.map((b, k) => chatImage(s, b, { alt: '图片', name: '粘贴的图片 ' + (k + 1) }));
    bubble.appendChild(h('div', { class: 'user-imgs' }, imgs));
  }
  if (item.text) bubble.appendChild(h('div', { class: 'user-text' }, item.text));
  if (item.truncated) bubble.appendChild(h('div', { class: 'tool-desc' }, '（内容过长，已截断）'));
  el.replaceChildren(bubble);
  const meta = msgMeta(item.text, item.ts, 'mm-user', false);   // 气泡左侧、底对齐
  if (meta) el.insertBefore(meta, bubble);
}

function renderText(s, item, el, cls) {
  if (item.apiError) cls.push('api-error');
  el.className = cls.join(' ') + ' md';
  if (item.ts) el.title = fmtTime(item.ts);
  const frag = renderMarkdown(item.text, markdownCtx(s));   // 行内代码/链接里的文件路径 → chip；localhost 链接可预览
  if (item.truncated) frag.appendChild(h('p', { class: 'tool-desc' }, '（内容过长，已截断）'));
  el.replaceChildren(frag);
  const meta = msgMeta(item.text, item.ts, 'mm-text', true);   // 只在一轮最后一段文字下方显示（CSS 决定）
  if (meta) el.appendChild(meta);
}

function renderThinking(s, item, el, cls) {
  el.className = cls.join(' ');
  const label = item.redacted ? '思考（已加密）' : '思考';
  const head = h('button', { class: 'think-head', type: 'button', 'aria-expanded': String(!!item.expanded) },
    icon('spark'), h('span', { class: 'think-label' }, label), icon(item.expanded ? 'chevdown' : 'chevron', 'chev'));
  head.addEventListener('click', () => { item.expanded = !item.expanded; renderItem(s, item); });
  const nodes = [head];
  if (item.expanded) nodes.push(h('div', { class: 'think-body' }, item.text ? item.text : '（思考内容未记录）'));
  el.replaceChildren(...nodes);
}

function toolStatus(t) {
  if (t.denied) return ['denied', '被拒绝'];
  if (t.done) return t.isError ? ['error', '出错'] : ['done', '完成'];
  if (t.interrupted) return ['interrupted', '已中断'];
  return ['running', '运行中'];
}

function statusIcon(st) {
  if (st === 'running') return spinner();
  if (st === 'done') return icon('check', 'st-ok');
  if (st === 'error') return icon('x', 'st-err');
  if (st === 'denied') return icon('ban', 'st-warn');
  return icon('stop', 'st-muted');
}

/** Agent/Task 卡片对应的子代理状态：摘要（服务器）> 结束通知 > 启动结果（"Async agent launched" = 后台运行中） */
function agentCardState(s, t) {
  const aid = subagentForTool(s, t);
  const a = aid ? rootOf(s).subagents.get(aid) : null;
  const sum = a && a.summary ? a.summary : null;
  const launched = (t.structured && t.structured.status === 'async_launched') || /^\s*Async agent launched/.test(resultText(t.result));
  let st = sum ? sum.status : t.note ? t.note.status : null;
  if (!st && launched) st = s.readOnly && !s.sidechain ? null : 'running';
  return { aid, sum, st, launched };
}

const SA_CARD_ST = { running: ['running', '子代理运行中'], completed: ['done', '子代理已完成'], failed: ['error', '子代理失败'], killed: ['interrupted', '子代理已终止'] };

function agentCardRow(s, t, ag) {
  const { st, parts, dur, tools, action } = saStatusLine(ag.sum, t.note);
  const canOpen = !!(ag.aid && s.state.actions && s.state.actions.openSubagent);
  const bits = [h('span', { class: 'sa-st-text' }, parts[0])];
  const el = saElapsed(ag.sum, dur);
  if (el) bits.push(h('span', { class: 'sa-sep' }, '·'), el);
  if (tools != null) bits.push(h('span', { class: 'sa-sep' }, '·'), h('span', null, tools + ' 次工具调用'));
  if (action && st === 'running') bits.push(h('span', { class: 'sa-sep' }, '·'), h('span', { class: 'sa-action mono' }, action));
  const row = h('div', { class: 'sa-card-row st-' + st }, saStatusIcon(st), h('span', { class: 'sa-card-text' }, bits));
  if (canOpen) {
    const b = h('button', { class: 'btn ghost small sa-card-open', type: 'button', title: '在右侧查看这个子代理的完整过程' }, '查看过程', icon('chevron'));
    b.addEventListener('click', (e) => { e.stopPropagation(); s.state.actions.openSubagent(rootOf(s), ag.aid); });
    row.appendChild(b);
  }
  return row;
}

function renderTool(s, t, el, cls) {
  const info = toolInfo(t.name, t.input || {});
  let [st, stText] = toolStatus(t);
  const ag = AGENT_TOOLS.has(t.name) ? agentCardState(s, t) : null;
  if (ag && ag.st && !t.denied && !(t.done && t.isError)) [st, stText] = SA_CARD_ST[ag.st] || [st, stText];
  cls.push('st-' + st);
  cls.push('cat-' + toolCategory(t.name));   // 彩色区分：按工具类别着色（读取/编辑/命令/MCP/联网/子代理/Todo）
  if (ag) cls.push('k-agent');
  if (t.expanded) cls.push('open');
  el.className = cls.join(' ');
  const head = h('button', { class: 'tool-head', type: 'button', 'aria-expanded': String(!!t.expanded), title: info.sub || info.summary || '' },
    icon(info.icon, 'tool-ic'),
    h('span', { class: 'tool-name' }, info.label),
    h('span', { class: 'tool-sum' }, info.summary || ''),
    h('span', { class: 'tool-st', title: stText }, statusIcon(st), h('span', { class: 'st-text' }, stText)),
    icon(t.expanded ? 'chevdown' : 'chevron', 'chev'));
  head.addEventListener('click', () => { t.expanded = !t.expanded; renderItem(s, t); });
  const nodes = [];
  const gs = groupSummary(s, t);   // 连续工具调用组的摘要："读取 3 个文件、运行 2 条命令"
  if (gs) nodes.push(gs);
  const fpath = t.input && typeof t.input === 'object' ? (t.input.file_path || t.input.notebook_path) : null;
  if (typeof fpath === 'string' && fpath && FILE_TOOLS.has(t.name)) {
    // 文件工具：标题行右侧的文件 chip（本机：预览；服务器：复制路径）
    nodes.push(h('div', { class: 'tool-headrow' }, head, fileChip(s, fpath, { tool: true, cls: 'tool-fchip' })));
  } else nodes.push(head);
  if (Array.isArray(t.result) && !t.isError) {
    // Read 读图片 / MCP 截图：卡片上直接显示缩略图条（不用展开）
    const row = viewedImagesRow(s, t.result, t.name === 'Read' ? fpath || null : null);
    if (row) nodes.push(row);
  }
  if (ag && ag.st) nodes.push(agentCardRow(s, t, ag));
  if (t.expanded) {
    const body = h('div', { class: 'tool-body' });
    if (t.input) body.appendChild(renderToolInput(t.name, t.input, t.structured));
    if (t.name === 'Bash' && t.input) { const ex = bashExtras(s, t); if (ex) body.appendChild(ex); }
    const res = ag ? ((ag.sum && ag.sum.status !== 'running' && ag.sum.result) || (t.note && t.note.result)) : null;
    if (ag && (res || ag.launched)) {
      body.appendChild(h('div', { class: 'tool-sec' }, h('div', { class: 'tool-sec-title' }, '子代理结果'),
        res ? h('div', { class: 'md sa-result-md' }, renderMarkdown(String(res)))
          : h('div', { class: 'tool-desc' }, '已转入后台运行' + (ag.aid ? '（agentId ' + ag.aid + '）' : '') + '，完成后这里会显示结果。')));
    } else if (t.name === 'AskUserQuestion' && t.structured && t.structured.answers) {
      body.appendChild(h('div', { class: 'tool-sec' }, h('div', { class: 'tool-sec-title' }, '回答'),
        Object.entries(t.structured.answers).map(([q, a]) => h('div', { class: 'tool-desc' }, q, ' → ', h('b', null, String(a))))));
    } else if (t.done) {
      body.appendChild(renderToolResult(t.result, t.isError, t.truncated,
        { image: (b) => chatImage(s, b, { path: t.name === 'Read' ? fpath : null, alt: '工具返回的图片', cls: 'tool-img' }) }));
    }
    nodes.push(body);
  }
  el.replaceChildren(...nodes);
}

const FILE_TOOLS = new Set(['Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit']);

/** Bash 卡片：命令/输出里出现的文件路径（chip）与 localhost 链接（本机可在面板预览） */
function bashExtras(s, t) {
  const out = resultText(t.result);
  const paths = [...new Set([...bashPaths(t.input.command), ...textPaths(out)])].slice(0, 12);
  const urls = localhostUrls(String(t.input.command || '') + '\n' + out).slice(0, 4);
  const pvs = urls.map((u) => { const b = urlPreviewButton(s, u); return b ? h('span', { class: 'url-wrap' }, h('span', { class: 'mono url-t' }, u), b) : null; }).filter(Boolean);
  if (!paths.length && !pvs.length) return null;
  return h('div', { class: 'tool-sec' }, h('div', { class: 'tool-sec-title' }, '涉及的文件 / 地址'),
    h('div', { class: 'fchips' }, paths.map((p) => fileChip(s, p)), pvs));
}

/** 子代理结束通知：小系统行「子代理「…」已完成 · 2.8s · 0 次工具调用」，结果可展开，点击打开抽屉 */
function renderSubnote(s, item, el, cls) {
  const n = item.note || {};
  const root = rootOf(s);
  const aid = n.agentId || (n.toolUseId && root.subByTool.get(n.toolUseId)) || null;
  const a = aid ? root.subagents.get(aid) : null;
  const sum = a && a.summary;
  const t = n.toolUseId ? root.tools.get(n.toolUseId) : null;
  const desc = (sum && sum.description) || item.desc || (t && t.input && t.input.description) || (n.summary || '').replace(/^Agent "(.*)" \w+.*$/, '$1') || '子代理';
  const st = n.status || 'completed';
  cls.push('st-' + st);
  if (item.expanded) cls.push('open');
  el.className = cls.join(' ');
  if (item.ts) el.title = fmtTime(item.ts);
  const usage = n.usage || {};
  const bits = ['子代理「' + desc + '」' + (SA_STATUS_TEXT[st] || st)];
  if (usage.duration_ms != null) bits.push(fmtDuration(usage.duration_ms));
  if (usage.tool_uses != null) bits.push(usage.tool_uses + ' 次工具调用');
  const line = h('span', { class: 'sa-note-text' }, bits.join(' · '));
  const nodes = [saStatusIcon(st), line];
  const canOpen = !!(aid && s.state.actions && s.state.actions.openSubagent);
  if (n.result) {
    const tog = h('button', { class: 'link-btn sa-note-tog', type: 'button', 'aria-expanded': String(!!item.expanded) }, item.expanded ? '收起结果' : '结果');
    tog.addEventListener('click', () => { item.expanded = !item.expanded; renderItem(s, item); });
    nodes.push(tog);
  }
  if (canOpen) {
    const b = h('button', { class: 'link-btn sa-note-open', type: 'button', title: '在右侧查看这个子代理的完整过程' }, '查看过程');
    b.addEventListener('click', () => s.state.actions.openSubagent(root, aid));
    nodes.push(b);
  }
  const out = [h('div', { class: 'sa-note-line' }, nodes)];
  if (item.expanded && n.result) out.push(h('div', { class: 'sa-note-result md' }, renderMarkdown(String(n.result))));
  el.replaceChildren(...out);
}

function renderCmd(item, el, cls) {
  if (item.isError) cls.push('err');
  el.className = cls.join(' ');
  if (item.ts) el.title = fmtTime(item.ts);
  const nodes = [];
  if (item.command) nodes.push(h('div', { class: 'cmd-line' }, icon('terminal'), h('span', { class: 'mono' }, item.command)));
  if (item.output != null && item.output !== '') {
    nodes.push(h('div', { class: 'cmd-out' + (item.command ? '' : ' solo') }, h('span', { class: 'cmd-mark' }, '⎿'), h('span', { class: 'cmd-out-text' }, truncate(item.output.replace(/`/g, ''), 4000))));
  }
  el.replaceChildren(...nodes);
}

function renderHint(item, el, cls) {
  cls.push('lv-' + (item.level || 'info'));
  el.className = cls.join(' ');
  if (item.detail) {
    el.replaceChildren(h('details', { class: 'hint-detail' }, h('summary', null, item.text), h('pre', { class: 'tool-pre' }, truncate(item.detail, 20000))));
  } else el.replaceChildren(h('span', null, item.text));
}

function renderNotice(s, item, el, cls) {
  cls.push('lv-' + (item.level || 'info'));
  el.className = cls.join(' ');
  const ic = item.level === 'error' ? 'x' : item.level === 'warn' ? 'stop' : 'chat';
  const nodes = [icon(ic), h('span', null, item.text)];
  const dead = s.meta.state === 'exited' || s.meta.state === 'error';
  if (item.action === 'restart' && dead && !s.readOnly && s.state.actions && s.state.actions.restart) {
    const b = h('button', { class: 'btn small', type: 'button' }, icon('refresh'), '重连');
    b.addEventListener('click', () => s.state.actions.restart(s));
    nodes.push(b);
  }
  el.replaceChildren(...nodes);
}
