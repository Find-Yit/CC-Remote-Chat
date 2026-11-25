// 右侧「代码改动」栏（docs/SPEC_V2_TUI.md §10.4「改动」标签；用户需求，参考 Claude Code CLI 的 /diff 面板）：
// 本会话 Claude 用 Edit / Write / MultiEdit / NotebookEdit 改了哪些文件、每个文件 +/− 行数、点开看 diff。
// 数据**只**来自前端已经拿到的 transcript 条目（主会话 s.items 里的工具调用 + 子代理 s.subagents 的条目），
// 不跑 git、不读 ~/.claude/file-history、不读文件、不发任何请求、不按任何键（绝不发 /diff）。本机与服务器会话同一套逻辑。
//
// 口径：只统计成功的编辑（失败 / 被拒绝 / 未完成的不算）；+/− 是各次编辑相加（structuredPatch 的 +/- 行数；
// 新建文件 = 内容行数；没有 structuredPatch 时按 old/new 字符串 LCS），不是 git 的净改动。
//
// 移植 / 参考（MIT License）：
//   T3 Code（https://github.com/pingdotgg/t3code，Copyright (c) 2026 T3 Tools Inc.）：
//     apps/web/src/components/chat/DiffStatLabel.tsx 的 formatCompactDiffCount（→ compactCount）、
//     apps/web/src/lib/toolCallDiff.ts 的「绝对路径 → 相对工作区路径」（→ relPath）、
//     apps/web/src/components/DiffPanel.tsx 的面板结构（按轮选择、全部展开/折叠、onOpenTurnDiff + revealRequestId 联动 → reveal）、
//     ClaudeAdapterV2.ts 的 Write 新建规则（structuredPatch 为空且 originalFile 为 null → 用 content 生成全新增 patch）。
//   Orca（Copyright (c) 2026 Lovecast Inc.）：src/shared/native-chat/tools/native-chat-turn-diffs.ts（按轮、按文件合并多次编辑、
//     保留每次编辑的顺序）、native-chat-edit-normalize.ts（没有结构化结果时，结果文字以 "File created successfully" 开头才算新建，
//     否则当覆盖编辑）。
// 与它们的差异：原生 DOM；NotebookEdit 也计入（toolUseResult 里有 old_source / new_source，用 LCS 比较，标「单元格」；
//   Orca 不算它是因为它只有新内容）；不做「合并后的净 diff」（见 SPEC：中间可能有 Bash / 用户改动，大文件的 originalFile 会被置 null）。
//
// 增量维护：ChangeTracker 记住扫描到 s.items 的哪一条与还没有结果的编辑调用；每次 flush 只看新条目和待定的调用；
// s.items 被整体替换（reset、/clear）时从头重建。子代理按 (gen, base, 条目数) 判断有没有变化，变了才重算该子代理。
import { h, icon, copyText } from './dom.js';
import { normPatch, patchStats, diffStats, renderPatch, renderDiff } from './tools.js';

export const EDIT_TOOLS = new Set(['Write', 'Edit', 'MultiEdit', 'NotebookEdit']);
const AGENT_TOOLS = new Set(['Agent', 'Task']);
const CREATED_RE = /^\s*File created successfully/;
export const TURN_TEXT_MAX = 30;     // 按轮选择里显示该轮第一句提问的前 N 个字
export const EAGER_LINES = 400;      // 小于这么多行的 diff 展开时直接渲染，更大的进入视口才渲染
export const NOTE_TEXT = '只统计 Claude 用 Edit / Write / NotebookEdit 改的文件；用 Bash 命令或在会话外改的文件不在其中；+/− 是各次编辑相加，不是 git 净改动';

function str(v) { return typeof v === 'string' ? v : v == null ? '' : String(v); }
function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : null; }

// ---------------------------------------------------------------- 纯函数

/** 移植自 T3 formatCompactDiffCount：999 → "999"，1234 → "1.2k"，15400 → "15k"，2.5e6 → "2.5m" */
export function compactCount(value) {
  const v = Math.max(0, Math.floor(Number(value) || 0));
  if (v < 1000) return String(v);
  const fmt = (x) => (x < 10 ? x.toFixed(1).replace(/\.0$/, '') : String(Math.round(x)));
  if (v < 999500) return fmt(v / 1000) + 'k';
  if (v < 999500000) return fmt(v / 1e6) + 'm';
  return fmt(v / 1e9) + 'b';
}

/** tool_result 的文字（string 或 [{type:'text'}…]） */
export function resultText(c) {
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) return c.filter((b) => b && b.type === 'text').map((b) => str(b.text)).join('\n');
  return '';
}

function normalize(p) {
  const abs = p.startsWith('/');
  const parts = [];
  for (const seg of p.split('/')) {
    if (!seg || seg === '.') continue;
    if (seg === '..') { if (parts.length && parts[parts.length - 1] !== '..') parts.pop(); else if (!abs) parts.push('..'); continue; }
    parts.push(seg);
  }
  return (abs ? '/' : '') + parts.join('/');
}

/** 记录的路径 → 绝对路径（相对路径按会话 cwd；与 filelinks.resolvePath 相同规则，这里不引 filelinks 以便 node 单测） */
export function absPath(raw, cwd) {
  const p = str(raw).trim();
  if (!p) return '';
  if (p.startsWith('/')) return normalize(p);
  if (p === '~' || p.startsWith('~/')) return '~' + (normalize(p.slice(1)) || '');
  const base = str(cwd).replace(/\/+$/, '');
  if (!base) return normalize(p);
  if (base === '~' || base.startsWith('~/')) return '~' + normalize(base.slice(1) + '/' + p);
  return normalize(base + '/' + p);
}

/** 显示用：在 cwd 下的文件显示相对路径（T3 toolCallDiff 的 relativeHeaderPaths 思路），其余保持绝对路径 */
export function relPath(abs, cwd) {
  const c = str(cwd).replace(/\/+$/, '');
  if (c && c !== '/' && c !== '~' && abs.startsWith(c + '/')) return abs.slice(c.length + 1);
  return abs;
}

/** 'a/b/c.py' → {dir:'a/b/', name:'c.py'} */
export function splitPath(p) {
  const s = str(p);
  const i = s.lastIndexOf('/');
  return i >= 0 ? { dir: s.slice(0, i + 1), name: s.slice(i + 1) } : { dir: '', name: s };
}

function lineCount(text) {
  const t = str(text);
  if (!t) return 0;
  return t.replace(/\n$/, '').split('\n').length;
}

/** old → new 的增删行数（LCS；一侧为空时直接按行数，避免把空串当成一行"删除"） */
function strStats(oldS, newS) {
  if (!str(oldS)) return { add: lineCount(newS), del: 0 };
  if (!str(newS)) return { add: 0, del: lineCount(oldS) };
  return diffStats(oldS, newS);
}

/** 编辑调用的文件路径（input 优先，其次 toolUseResult） */
export function editPath(input, structured) {
  const i = obj(input) || {}, st = obj(structured) || {};
  return str(i.file_path || i.notebook_path || st.filePath || st.notebook_path);
}

/**
 * 一次编辑 → {tool, label, hunks|null, content|null, old|null, new|null, created, add, del, truncated, approx, lineNos, notebook, replaceAll}
 *   hunks：structuredPatch（带新旧行号）；content：新建文件的全部内容（按全部新增显示，有行号）；
 *   old/new：没有 structuredPatch 时按字符串 LCS 比较（无行号）。
 *   approx = 行数可能不准（transcript 里内容被截断，或覆盖写入但不知道旧内容）。
 * opts = {truncated, resultText}
 */
export function editOf(name, input, structured, opts = {}) {
  const i = obj(input) || {};
  const st = obj(structured);
  const truncated = !!opts.truncated;
  const e = {
    tool: str(name), label: str(name), hunks: null, content: null, old: null, new: null, created: false,
    add: 0, del: 0, truncated, approx: false, lineNos: false, notebook: null, replaceAll: !!(i.replace_all || (st && st.replaceAll)),
  };
  if (name === 'NotebookEdit') {
    const mode = str(i.edit_mode || (st && st.edit_mode)) || 'replace';
    e.notebook = { cell: str((st && st.cell_id) ?? i.cell_id), type: str((st && st.cell_type) ?? i.cell_type), mode };
    const oldS = st && typeof st.old_source === 'string' ? st.old_source : null;
    e.old = mode === 'insert' ? '' : oldS ?? '';
    e.new = mode === 'delete' ? '' : str(st && typeof st.new_source === 'string' ? st.new_source : i.new_source);
    e.label = 'NotebookEdit' + (mode === 'insert' ? '（插入单元格）' : mode === 'delete' ? '（删除单元格）' : '');
    Object.assign(e, strStats(e.old, e.new));
    if (truncated || (oldS == null && mode === 'replace')) e.approx = true;
    return e;
  }
  const hunks = st ? normPatch(st.structuredPatch) : null;
  if (hunks) {
    e.hunks = hunks;
    e.lineNos = true;
    e.created = st.type === 'create';
    if (name === 'Write') e.label = e.created ? 'Write（新建）' : 'Write（覆盖）';
    Object.assign(e, patchStats(hunks));
    return e;
  }
  if (name === 'Write' || (st && (st.type === 'create' || st.type === 'update'))) {
    // 新建：structuredPatch 为空、originalFile 为 null → 用 content 生成全新增（T3 ClaudeAdapterV2 同样规则）。
    // 没有结构化结果时按 Orca：结果文字以 "File created successfully" 开头才算新建，否则当覆盖编辑。
    const created = st && (st.type === 'create' || st.type === 'update') ? st.type === 'create' : CREATED_RE.test(str(opts.resultText));
    const content = str(st && typeof st.content === 'string' ? st.content : i.content);
    e.created = created;
    if (!created && st && st.type === 'update' && Array.isArray(st.structuredPatch) && !st.structuredPatch.length) {
      e.label = 'Write（覆盖，内容未变）';
      return e;
    }
    if (!created && st && typeof st.originalFile === 'string') {
      e.old = st.originalFile;
      e.new = content;
      e.label = 'Write（覆盖）';
      Object.assign(e, strStats(e.old, e.new));
      if (truncated) e.approx = true;
      return e;
    }
    e.content = content;
    e.lineNos = true;
    e.add = lineCount(content);
    e.label = created ? 'Write（新建）' : 'Write（覆盖，旧内容未知）';
    if (truncated || !created) e.approx = true;
    return e;
  }
  if (name === 'Edit') {
    e.old = str(i.old_string ?? (st && st.oldString));
    e.new = str(i.new_string ?? (st && st.newString));
  } else if (name === 'MultiEdit' && Array.isArray(i.edits)) {
    e.old = i.edits.map((x) => str(x && x.old_string)).join('\n⋯\n');
    e.new = i.edits.map((x) => str(x && x.new_string)).join('\n⋯\n');
  } else return e;
  Object.assign(e, strStats(e.old, e.new));
  if (truncated) e.approx = true;
  return e;
}

/** render.js 的工具条目 → editOf */
export function editOfTool(t) {
  return editOf(t.name, t.input, t.structured, { truncated: !!t.truncated, resultText: resultText(t.result) });
}

/** 成功完成的调用才计入（失败 / 被拒绝 / 未完成 / 被中断的不算） */
export function toolOk(t) { return !!(t && t.done && !t.isError && !t.denied); }

/** 「本轮改动」卡片用：一轮的工具调用 → [{path, edits:[…], add, del}]（按首次出现顺序） */
export function fileChanges(tools) {
  const by = new Map();
  for (const t of tools || []) {
    if (!t || !EDIT_TOOLS.has(t.name) || !toolOk(t)) continue;
    const p = editPath(t.input, t.structured);
    if (!p) continue;
    let c = by.get(p);
    if (!c) { c = { path: p, edits: [], add: 0, del: 0, toolIds: [] }; by.set(p, c); }
    const e = editOfTool(t);
    c.edits.push(e);
    c.toolIds.push(t.toolId);
    c.add += e.add;
    c.del += e.del;
  }
  return [...by.values()];
}

function entryTs(e) {
  const v = e && e.timestamp ? Date.parse(e.timestamp) : NaN;
  return Number.isFinite(v) ? v / 1000 : null;
}

/** 子代理的原始 transcript 条目 → 编辑类工具调用（与 render.js 的工具条目同形：name/input/structured/result/done/isError/denied） */
export function sidechainTools(entries) {
  const calls = new Map();
  for (const e of entries || []) {
    if (!e || typeof e !== 'object' || e.isMeta) continue;
    const m = e.message || {};
    const content = Array.isArray(m.content) ? m.content : [];
    if (e.type === 'assistant') {
      for (const b of content) {
        if (!b || b.type !== 'tool_use' || !EDIT_TOOLS.has(b.name) || !b.id || calls.has(b.id)) continue;
        calls.set(b.id, { toolId: b.id, name: b.name, input: obj(b.input), structured: null, result: undefined, ts: entryTs(e),
          done: false, isError: false, denied: false, truncated: !!(b.truncated || e.truncated) });
      }
    } else if (e.type === 'user') {
      for (const b of content) {
        if (!b || b.type !== 'tool_result') continue;
        const c = calls.get(b.tool_use_id);
        if (!c || c.done) continue;
        c.done = true;
        c.result = b.content;
        c.isError = !!b.is_error;
        if (e.toolDenialKind || (typeof e.toolUseResult === 'string' && /rejected/i.test(e.toolUseResult))) c.denied = true;
        c.structured = obj(e.toolUseResult);
        if (b.truncated || e.truncated) c.truncated = true;
      }
    }
  }
  return [...calls.values()];
}

function userText(it) {
  let t = str(it && it.text).replace(/\s+/g, ' ').trim();
  if (!t && it && Array.isArray(it.imageBlocks) && it.imageBlocks.length) t = '图片消息（' + it.imageBlocks.length + ' 张）';
  return t;
}

/** 子代理条目是否完整（与 render.js 的 subagentComplete 相同；这里不引 render.js 以便 node 单测） */
function agentComplete(a) {
  const start = a.serverBase != null ? a.serverBase : 0;
  return !a.stale && a.base <= start && a.base + (a.entries ? a.entries.length : 0) >= (a.total || 0);
}

function cmpEdit(a, b) {
  if (a.ts != null && b.ts != null && a.ts !== b.ts) return a.ts - b.ts;
  return a.seq - b.seq;
}

/**
 * 一个会话的改动（增量维护）。s = render.js 的会话：{items, subagents}（items 里 kind:'user' 分轮、kind:'tool' 是工具调用）。
 * 轮：turns[0] = 第一条用户消息之前（通常没有）；turns[k] = 第 k 条用户消息开始的一轮。
 */
export class ChangeTracker {
  constructor() { this.version = 0; this.reset(null); }

  reset(items) {
    this.items = items;
    this.scanned = 0;
    this.turns = [{ idx: 0, n: 0, text: '', ts: null, item: null }];
    this.main = [];             // 主会话的编辑（按 seq = 条目下标排序）
    this.pending = [];          // 还没有结果的编辑调用 {t, seq, turn}
    this.toolTurn = new Map();  // 工具调用 id → 轮（子代理的 Agent 调用找轮用）
    this.subs = new Map();      // agent_id → {key, edits, label, complete, cache:Map(toolId → edit)}
    this.cache = null;
    this.version++;
  }

  /** 吸收新条目 / 新结果 / 子代理变化；有变化返回 true（version 加一） */
  update(s) {
    if (!s) return false;
    let changed = false;
    const items = Array.isArray(s.items) ? s.items : [];
    if (items !== this.items) { this.reset(items); changed = true; }
    for (; this.scanned < items.length; this.scanned++) {
      const it = items[this.scanned];
      if (!it) continue;
      if (it.kind === 'user' && !it.midTurn) {   // 回复中发送的消息（midTurn）属于当前这一轮，不另起一轮
        const n = this.turns.length;
        this.turns.push({ idx: n, n, text: userText(it), ts: typeof it.ts === 'number' ? it.ts : null, item: it });
        changed = true;
      } else if (it.kind === 'tool') {
        const turn = this.turns.length - 1;
        if (it.toolId) this.toolTurn.set(it.toolId, turn);
        if (EDIT_TOOLS.has(it.name)) this.pending.push({ t: it, seq: this.scanned, turn });
      }
    }
    if (this.pending.length) {
      const keep = [];
      for (const p of this.pending) {
        const t = p.t;
        if (!t.done) { if (!t.interrupted) keep.push(p); continue; }
        if (!toolOk(t)) continue;
        const path = editPath(t.input, t.structured);
        if (!path) continue;
        const rec = Object.assign(editOfTool(t), { key: str(t.toolId) || 'i' + p.seq, path, seq: p.seq, turn: p.turn,
          ts: typeof t.ts === 'number' ? t.ts : null, sub: null });
        let k = this.main.length;
        while (k > 0 && this.main[k - 1].seq > rec.seq) k--;
        this.main.splice(k, 0, rec);
        changed = true;
      }
      this.pending = keep;
    }
    if (this.updateSubs(s)) changed = true;
    if (changed) { this.version++; this.cache = null; }
    return changed;
  }

  updateSubs(s) {
    let changed = false;
    const seen = new Set();
    const agents = s.subagents instanceof Map ? [...s.subagents.values()] : [];
    agents.forEach((a, ai) => {
      if (!a || !a.id) return;
      seen.add(a.id);
      const sum = a.summary || {};
      const tid = str(sum.tool_use_id);
      const entries = Array.isArray(a.entries) ? a.entries : [];
      const key = [a.gen, a.base, entries.length, a.total, a.stale ? 1 : 0, tid, this.toolTurn.has(tid) ? 1 : 0,
        this.turns.length, str(sum.description)].join('|');
      const prev = this.subs.get(a.id);
      if (prev && prev.key === key) return;
      const cache = prev ? prev.cache : new Map();
      const label = str(sum.description) || str(sum.agent_type) || a.id;
      const turnOfAgent = tid && this.toolTurn.has(tid) ? this.toolTurn.get(tid) : null;
      const edits = [];
      sidechainTools(entries).forEach((t, k) => {
        if (!toolOk(t)) return;
        const path = editPath(t.input, t.structured);
        if (!path) return;
        let base = cache.get(t.toolId);
        if (!base) { base = editOfTool(t); cache.set(t.toolId, base); }
        edits.push(Object.assign({}, base, { key: a.id + ':' + t.toolId, path, seq: 1e9 + ai * 1e6 + k, ts: t.ts,
          turn: turnOfAgent != null ? turnOfAgent : this.turnAt(t.ts), sub: { id: a.id, label } }));
      });
      this.subs.set(a.id, { key, edits, label, complete: agentComplete(a), cache, hasSummary: !!a.summary });
      changed = true;
    });
    for (const id of [...this.subs.keys()]) if (!seen.has(id)) { this.subs.delete(id); changed = true; }
    return changed;
  }

  /** 按时间找轮（子代理的 Agent 调用不在主时间线上时的后备）：开始时间 ≤ ts 的最后一轮 */
  turnAt(ts) {
    if (ts == null) return this.turns.length - 1;
    let r = 0;
    for (let k = 1; k < this.turns.length; k++) if (this.turns[k].ts != null && this.turns[k].ts <= ts) r = k;
    return r;
  }

  /** 工具调用所在的轮（「本轮改动」卡片联动用）；不认识 → null */
  turnOfTool(toolId) { return this.toolTurn.has(toolId) ? this.toolTurn.get(toolId) : null; }

  /** 全部编辑（主会话 + 子代理），按时间顺序 */
  allEdits() {
    const c = this.cacheOf();
    if (!c.all) {
      const all = this.main.slice();
      for (const sub of this.subs.values()) all.push(...sub.edits);
      all.sort(cmpEdit);
      c.all = all;
    }
    return c.all;
  }

  cacheOf() { if (!this.cache) this.cache = { all: null, agg: new Map(), turns: null }; return this.cache; }

  /** 子代理里本页还没有完整条目的个数（它们的改动可能没计入） */
  incompleteAgents() {
    let n = 0;
    for (const sub of this.subs.values()) if (sub.hasSummary && !sub.complete) n++;
    return n;
  }

  /** scope = 'all' | 轮下标 → {files:[{abs, rel, dir, name, edits, add, del, created, sub, main, approx, truncated, notebook}], add, del, edits} */
  aggregate(scope, cwd) {
    const c = this.cacheOf();
    const ck = scope + '\n' + str(cwd);
    if (c.agg.has(ck)) return c.agg.get(ck);
    const by = new Map();
    let n = 0;
    for (const e of this.allEdits()) {
      if (scope !== 'all' && e.turn !== scope) continue;
      n++;
      const abs = absPath(e.path, cwd);
      let f = by.get(abs);
      if (!f) {
        const rel = relPath(abs, cwd);
        const sp = splitPath(rel);
        f = { abs, rel, dir: sp.dir, name: sp.name, edits: [], add: 0, del: 0, created: !!e.created, sub: false, main: false,
          approx: false, truncated: false, notebook: false };
        by.set(abs, f);
      }
      f.edits.push(e);
      f.add += e.add;
      f.del += e.del;
      if (e.sub) f.sub = true; else f.main = true;
      if (e.approx) f.approx = true;
      if (e.truncated) f.truncated = true;
      if (e.notebook) f.notebook = true;
    }
    const files = [...by.values()].sort((a, b) => a.rel.localeCompare(b.rel, undefined, { numeric: true }));
    const out = { files, add: files.reduce((x, f) => x + f.add, 0), del: files.reduce((x, f) => x + f.del, 0), edits: n };
    c.agg.set(ck, out);
    return out;
  }

  /** 有改动的轮（最新的在前）：[{turn, files, add, del}] */
  turnsWithEdits() {
    const c = this.cacheOf();
    if (c.turns) return c.turns;
    const by = new Map();
    for (const e of this.allEdits()) {
      let x = by.get(e.turn);
      if (!x) { x = { turn: this.turns[e.turn] || { idx: e.turn, n: e.turn, text: '', ts: null }, paths: new Set(), add: 0, del: 0 }; by.set(e.turn, x); }
      x.paths.add(e.path);
      x.add += e.add;
      x.del += e.del;
    }
    c.turns = [...by.values()].sort((a, b) => b.turn.idx - a.turn.idx)
      .map((x) => ({ turn: x.turn, files: x.paths.size, add: x.add, del: x.del }));
    return c.turns;
  }
}

const trackers = new WeakMap();
/** 会话的 tracker（挂在会话对象上，随会话回收） */
export function trackerOf(s) {
  let t = trackers.get(s);
  if (!t) { t = new ChangeTracker(); trackers.set(s, t); }
  return t;
}

/** HH:MM（不是今天的加 M/D） */
export function shortTime(ts, now = Date.now()) {
  if (ts == null || !Number.isFinite(ts)) return '';
  const d = new Date(ts * 1000);
  const n = new Date(now);
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  return d.toDateString() === n.toDateString() ? hm : (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
}

/** 按轮选择的选项文字：「第 3 轮 · 14:02 · 把 calc.py 改成…」 */
export function turnLabel(turn, now) {
  if (!turn || !turn.n) return '第一条消息之前';
  const t = str(turn.text);
  const text = t.length > TURN_TEXT_MAX ? t.slice(0, TURN_TEXT_MAX) + '…' : t;
  return ['第 ' + turn.n + ' 轮', shortTime(turn.ts, now), text].filter(Boolean).join(' · ');
}

/** 统计文字：「3 个文件 +12 −1」（0 的一侧不显示） */
export function statText(add, del) {
  const bits = [];
  if (add) bits.push('+' + compactCount(add));
  if (del) bits.push('−' + compactCount(del));
  return bits.join(' ');
}

/** 一次编辑 diff 的大致行数（懒渲染占位高度用） */
export function editLines(e) {
  if (e.hunks) return e.hunks.reduce((n, hk) => n + 1 + hk.lines.length, 0);
  if (e.content != null) return lineCount(e.content) + 1;
  if (e.old != null || e.new != null) return Math.min(lineCount(e.old) + lineCount(e.new), 4000) + 1;
  return 1;
}

// ---------------------------------------------------------------- 视图

function statEl(add, del, cls) {
  return h('span', { class: 'chg-stat' + (cls ? ' ' + cls : '') },
    add ? h('span', { class: 'chg-add' }, '+' + compactCount(add)) : null,
    del ? h('span', { class: 'chg-del' }, '−' + compactCount(del)) : null);
}

function tag(text, cls, title) { return h('span', { class: 'chg-tag ' + cls, title: title || null }, text); }

/**
 * 某个会话的改动视图（PreviewPanel 的「改动」标签内容；每个会话一个，换会话时保留展开状态）。
 * opts = {session: () => 会话对象（同一 sid 的会话对象可能被替换，例如历史预览重开）, cwd: () => 会话 cwd,
 *         local: () => 是否本机会话, onOpenFile(abs), toast(text)}
 */
export class ChangesView {
  constructor(s, opts = {}) {
    this.s0 = s;
    this.opts = opts;
    this.itemsSeen = null;
    this.scope = 'all';
    this.expanded = new Set();      // 展开的文件（绝对路径；按 scope 分开记）
    this.nodes = new Map();         // abs → {sig, el, body, built}
    this.version = -1;
    this.key = '';
    this.optKey = '';
    this.build();
  }

  get s() { return (this.opts.session && this.opts.session()) || this.s0; }
  tracker() { return trackerOf(this.s); }
  cwd() { return this.opts.cwd ? this.opts.cwd() : ''; }

  build() {
    this.scopeSel = h('select', { class: 'mini-select chg-scope', 'aria-label': '改动范围：本会话或某一轮', title: '范围：本会话全部 / 某一轮' });
    this.scopeSel.addEventListener('change', () => { this.setScope(this.scopeSel.value === 'all' ? 'all' : Number(this.scopeSel.value)); });
    this.sumEl = h('span', { class: 'chg-sum' });
    this.allBtn = h('button', { class: 'btn ghost small chg-all', type: 'button' }, icon('chevron', 'chg-all-ic'), h('span', null, '全部展开'));
    this.allBtn.addEventListener('click', () => this.toggleAll());
    this.noteEl = h('div', { class: 'chg-note', hidden: true });
    this.list = h('div', { class: 'chg-list', role: 'list' });
    this.empty = h('div', { class: 'pv-empty chg-empty', hidden: true }, icon('diff'), h('div', { class: 'chg-empty-t' }), h('div', { class: 'muted small-text' }, NOTE_TEXT));
    this.scroll = h('div', { class: 'pv-scroll chg-scroll' }, this.noteEl, this.list, this.empty);
    this.root = h('div', { class: 'pv-view kind-changes chg' },
      h('div', { class: 'chg-head' }, this.scopeSel, this.sumEl, h('span', { class: 'spacer' }), this.allBtn),
      this.scroll,
      h('div', { class: 'chg-foot' }, NOTE_TEXT));
    this.io = typeof IntersectionObserver === 'function'
      ? new IntersectionObserver((ents) => { for (const en of ents) if (en.isIntersecting) this.fillLazy(en.target); }, { root: this.scroll, rootMargin: '600px 0px' })
      : null;
  }

  setScope(scope) {
    if (scope !== 'all' && !this.tracker().turns[scope]) scope = 'all';
    if (scope === this.scope) return;
    this.scope = scope;
    this.scroll.scrollTop = 0;
    this.update(true);
  }

  expandedKey(abs) { return this.scope + '\n' + abs; }

  /** 重新渲染（tracker 没变化且 force=false 时什么也不做）；返回当前范围的汇总 */
  update(force = false) {
    const tr = this.tracker();
    tr.update(this.s);
    if (tr.items !== this.itemsSeen) {
      // 换了一份 transcript（reset、/clear、会话对象被替换）：轮的编号不再对应，范围与展开状态从头来
      if (this.itemsSeen !== null) { this.scope = 'all'; this.expanded.clear(); }
      this.itemsSeen = tr.items;
      force = true;
    }
    const cwd = this.cwd();
    if (!force && tr.version === this.version && cwd === this.cwdSeen) return this.agg;
    this.version = tr.version;
    this.cwdSeen = cwd;
    this.renderOptions(tr);
    if (this.scope !== 'all' && !tr.turns[this.scope]) this.scope = 'all';
    if (this.scopeSel.value !== String(this.scope)) this.scopeSel.value = String(this.scope);
    const agg = tr.aggregate(this.scope, cwd);
    this.agg = agg;
    // 汇总
    this.sumEl.replaceChildren(h('span', { class: 'chg-sum-n' }, agg.files.length + ' 个文件'), statEl(agg.add, agg.del));
    this.sumEl.title = agg.files.length + ' 个文件，共 ' + agg.edits + ' 次编辑，+' + agg.add + ' −' + agg.del + '（各次编辑相加）';
    // 子代理条目不完整的提示
    const inc = tr.incompleteAgents();
    this.noteEl.hidden = !inc;
    if (inc) this.noteEl.textContent = '有 ' + inc + ' 个子代理的过程本页还没有完整加载，它们的改动可能没有计入（打开该子代理的抽屉即可加载）。';
    // 空状态
    this.empty.hidden = agg.files.length > 0;
    if (!agg.files.length) this.empty.querySelector('.chg-empty-t').textContent = this.scope === 'all' ? '本会话还没有改动文件' : '这一轮没有改动文件';
    this.allBtn.disabled = !agg.files.length;
    // 文件：内容（编辑列表与统计）没变的节点原样复用，保持展开状态与已渲染的 diff；锚定第一个可见文件，避免上方插入时跳动
    const anchor = this.anchor();
    const want = [];
    const keep = new Map();
    for (const f of agg.files) {
      const sig = this.scope + '|' + f.rel + '|' + f.edits.map((e) => e.key + ':' + e.add + ':' + e.del).join(',');
      let node = this.nodes.get(f.abs);
      if (!node || node.sig !== sig) node = this.fileNode(f, sig);
      node.file = f;
      keep.set(f.abs, node);
      want.push(node.el);
    }
    for (const [abs, node] of this.nodes) if (!keep.has(abs) && this.io) for (const ph of node.el.querySelectorAll('.chg-lazy')) this.io.unobserve(ph);
    this.nodes = keep;
    const cur = [...this.list.children];
    if (cur.length !== want.length || cur.some((el, k) => el !== want[k])) this.list.replaceChildren(...want);
    this.syncAllBtn();
    this.restoreAnchor(anchor);
    return agg;
  }

  renderOptions(tr) {
    const turns = tr.turnsWithEdits();
    const total = tr.aggregate('all', this.cwd());
    const key = turns.map((x) => [x.turn.idx, x.files, x.add, x.del, x.turn.text].join(':')).join('|') + '#' + total.files.length + ':' + total.add + ':' + total.del + ':' + tr.turns.length;
    if (key === this.optKey) return;
    this.optKey = key;
    const now = Date.now();
    const nTurns = tr.turns.length - 1;
    const opts = [h('option', { value: 'all', title: total.files.length + ' 个文件 ' + statText(total.add, total.del) }, '本会话' + (nTurns > 0 ? '（全部 ' + nTurns + ' 轮）' : ''))];
    for (const x of turns) {
      opts.push(h('option', { value: String(x.turn.idx), title: x.turn.text || '' }, turnLabel(x.turn, now) + '（' + x.files + ' 个文件）'));
    }
    // 选中的轮已经没有改动（例如 reset 之后）也保留一个选项，避免 select 跳回第一个
    if (this.scope !== 'all' && !turns.some((x) => x.turn.idx === this.scope) && tr.turns[this.scope]) {
      opts.push(h('option', { value: String(this.scope) }, turnLabel(tr.turns[this.scope], now) + '（0 个文件）'));
    }
    this.scopeSel.replaceChildren(...opts);
    this.scopeSel.value = String(this.scope);
  }

  fileNode(f, sig) {
    const open = this.expanded.has(this.expandedKey(f.abs));
    const ic = f.notebook ? 'list' : f.created ? 'plus' : 'edit';
    const tags = [];
    if (f.created) tags.push(tag('新建', 'is-new', '本范围内第一次编辑是新建文件'));
    if (f.notebook) tags.push(tag('单元格', 'is-nb', 'Jupyter 笔记本单元格（NotebookEdit）：按单元格旧/新内容比较'));
    if (f.sub) tags.push(tag(f.main ? '含子代理' : '子代理', 'is-sub', '由子代理改动：' + [...new Set(f.edits.filter((e) => e.sub).map((e) => e.sub.label))].join('、')));
    if (f.approx) tags.push(tag('≈', 'is-approx', 'transcript 里内容过长已截断，或覆盖写入时不知道旧内容：行数可能不准'));
    const row = h('button', { class: 'chg-row', type: 'button', 'aria-expanded': String(open), title: f.abs },
      icon('chevron', 'chg-chev'), icon(ic, 'chg-ic'),
      h('span', { class: 'chg-path mono' }, f.dir ? h('span', { class: 'chg-dir' }, f.dir) : null, h('span', { class: 'chg-name' }, f.name)),
      tags,
      f.edits.length > 1 ? h('span', { class: 'chg-n', title: '编辑了 ' + f.edits.length + ' 次' }, f.edits.length + ' 次') : null,
      statEl(f.add, f.del));
    const body = h('div', { class: 'chg-body pv-diff', hidden: !open });
    const el = h('div', { class: 'chg-file' + (open ? ' open' : ''), role: 'listitem', dataset: { path: f.abs } }, row, body);
    const node = { sig, el, row, body, built: false, file: f };
    row.addEventListener('click', () => this.setOpen(node, !el.classList.contains('open')));
    if (open) this.fillBody(node);
    return node;
  }

  setOpen(node, open, eager = false) {
    const k = this.expandedKey(node.file.abs);
    if (open) this.expanded.add(k); else this.expanded.delete(k);
    node.el.classList.toggle('open', open);
    node.row.setAttribute('aria-expanded', String(open));
    node.body.hidden = !open;
    if (open && !node.built) this.fillBody(node, eager);
    this.syncAllBtn();
  }

  fillBody(node, eager = false) {
    node.built = true;
    const f = node.file;
    const acts = [];
    if (this.opts.local && this.opts.local() && this.opts.onOpenFile) {
      const b = h('button', { class: 'btn ghost small pv-act', type: 'button', title: '在预览面板中打开当前文件（本机）' }, icon('file'), h('span', null, '打开文件'));
      b.addEventListener('click', () => this.opts.onOpenFile(f.abs));
      acts.push(b);
    }
    const cp = h('button', { class: 'btn ghost small pv-act', type: 'button', title: '复制绝对路径：' + f.abs }, icon('copy'), h('span', null, '复制路径'));
    cp.addEventListener('click', () => copyText(f.abs).then((ok) => this.opts.toast && this.opts.toast(ok ? '已复制：' + f.abs : '复制失败')));
    acts.push(cp);
    const tr = this.tracker();
    const now = Date.now();
    let budget = eager ? Infinity : EAGER_LINES;
    const secs = f.edits.map((e, k) => {
      const turn = tr.turns[e.turn];
      const head = h('div', { class: 'pv-diff-head chg-edit-head' },
        h('span', { class: 'pv-diff-n' }, (f.edits.length > 1 ? '第 ' + (k + 1) + ' 次 · ' : '') + e.label),
        this.scope === 'all' ? h('span', { class: 'chg-turn', title: turn && turn.text ? turn.text : '' }, turn && turn.n ? '第 ' + turn.n + ' 轮' : '第一条消息之前') : null,
        e.ts != null ? h('span', { class: 'muted' }, shortTime(e.ts, now)) : null,
        e.sub ? tag('子代理「' + e.sub.label + '」', 'is-sub') : null,
        statEl(e.add, e.del),
        e.notebook ? h('span', { class: 'muted small-text' }, '单元格 ' + (e.notebook.cell || '?') + (e.notebook.type ? '（' + e.notebook.type + '）' : '')) : null,
        e.replaceAll ? h('span', { class: 'muted small-text' }, '全部替换') : null,
        !e.hunks && e.content == null && (e.old != null || e.new != null) ? h('span', { class: 'muted small-text' }, '没有 structuredPatch：按旧/新字符串比较，无行号') : null,
        e.approx ? h('span', { class: 'chg-warn small-text' }, e.truncated ? '已截断，行数可能不准' : '不知道旧内容，按全部新增计') : null);
      const lines = editLines(e);
      let bodyEl;
      if (budget >= lines) { budget -= lines; bodyEl = editBody(e); } else {
        bodyEl = h('div', { class: 'chg-lazy', style: { height: Math.min(lines, 6000) * 18.6 + 'px' } });
        bodyEl._edit = e;
        if (this.io) this.io.observe(bodyEl); else bodyEl.replaceWith(editBody(e));
      }
      return h('div', { class: 'pv-diff-sec chg-sec', dataset: { key: e.key } }, head, bodyEl);
    });
    node.body.replaceChildren(h('div', { class: 'chg-acts' }, acts), ...secs);
  }

  fillLazy(ph) {
    if (!ph.isConnected || !ph._edit) return;
    if (this.io) this.io.unobserve(ph);
    const e = ph._edit;
    ph._edit = null;
    ph.replaceWith(editBody(e));
  }

  allOpen() {
    return !!this.agg && this.agg.files.length > 0 && this.agg.files.every((f) => this.expanded.has(this.expandedKey(f.abs)));
  }

  syncAllBtn() {
    const open = this.allOpen();
    this.allBtn.lastChild.textContent = open ? '全部折叠' : '全部展开';
    this.allBtn.classList.toggle('is-open', open);
    this.allBtn.title = open ? '折叠所有文件的 diff' : '展开所有文件的 diff（大 diff 滚动到时才渲染）';
  }

  toggleAll() {
    const open = !this.allOpen();
    for (const node of this.nodes.values()) this.setOpen(node, open);
  }

  /** 第一个可见文件的位置（更新前后保持它在屏幕上的位置不变） */
  anchor() {
    const top = this.scroll.scrollTop;
    if (!top) return null;
    for (const node of this.nodes.values()) {
      const el = node.el;
      if (el.isConnected && el.offsetTop + el.offsetHeight > top) return { el, off: el.offsetTop - top };
    }
    return null;
  }

  restoreAnchor(a) {
    if (!a || !a.el.isConnected) return;
    const want = a.el.offsetTop - a.off;
    if (Math.abs(this.scroll.scrollTop - want) > 1) this.scroll.scrollTop = want;
  }

  /** 联动（「本轮改动」卡片）：切到那一轮、展开并滚到那个文件。{turn: 轮下标|null, path: 绝对路径}；找到返回 true */
  reveal(target) {
    const tr = this.tracker();
    this.update();   // 先跟上最新条目（若换了 transcript 会在这里重置范围）
    if (target && target.turn != null && tr.turns[target.turn]) this.scope = target.turn;
    this.update(true);
    const node = target && target.path ? this.nodes.get(target.path) : null;
    if (!node) return false;
    for (const other of this.nodes.values()) if (other !== node) this.setOpen(other, false);   // 聚焦到这个文件
    this.setOpen(node, true, true);
    // 只滚面板自己的滚动容器（不用 scrollIntoView：它会连带滚动外层，WebKit 下可能牵动时间线）
    const top = node.el.getBoundingClientRect().top - this.scroll.getBoundingClientRect().top + this.scroll.scrollTop;
    this.scroll.scrollTop = Math.max(0, top - 4);
    node.el.classList.remove('chg-hit');
    void node.el.offsetWidth;
    node.el.classList.add('chg-hit');
    clearTimeout(this.hitTimer);
    this.hitTimer = setTimeout(() => node.el.classList.remove('chg-hit'), 1600);
    return true;
  }
}

/** 一次编辑的 diff：structuredPatch（带行号）/ 新建文件全部内容（带行号）/ old→new LCS（无行号） */
export function editBody(e) {
  if (e.hunks) return renderPatch(e.hunks, { cls: 'full' });
  if (e.content != null) {
    const lines = str(e.content).replace(/\n$/, '').split('\n');
    if (!e.content) return h('div', { class: 'muted small-text chg-none' }, '（空文件）');
    return renderPatch([{ oldStart: 0, oldLines: 0, newStart: 1, newLines: lines.length, lines: lines.map((l) => '+' + l) }], { cls: 'full' });
  }
  if (e.old != null || e.new != null) return renderDiff(e.old || '', e.new || '');
  return h('div', { class: 'muted small-text chg-none' }, '（没有可显示的内容）');
}

/** 会话头的开关按钮：图标 + 「改动」+ 文件数角标（角标用 <b>：窄窗口隐藏按钮文字时它仍显示） */
export class ChangesButton {
  constructor({ onClick } = {}) {
    this.count = h('b', { class: 'chg-count', hidden: true });
    this.el = h('button', { class: 'btn ghost small chg-btn', type: 'button', 'aria-pressed': 'false', title: '显示/隐藏右侧「代码改动」栏（本会话 Claude 改了哪些文件）' },
      icon('diff'), h('span', null, '改动'), this.count);
    if (onClick) this.el.addEventListener('click', onClick);
  }

  update(n, on) {
    const text = n ? compactCount(n) : '';
    if (this.count.textContent !== text) this.count.textContent = text;
    this.count.hidden = !n;
    this.el.classList.toggle('on', !!on);
    this.el.setAttribute('aria-pressed', String(!!on));
    this.el.title = (on ? '隐藏' : '显示') + '右侧「代码改动」栏' + (n ? '（本会话改了 ' + n + ' 个文件）' : '（本会话还没有改动文件）');
  }
}
