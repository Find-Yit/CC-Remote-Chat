// 会话内查找（⌘F；docs/SPEC_V2_TUI.md §7「会话内查找」；用户需求）：在当前会话的**全部条目**（s.items，包括「显示更早」之前
// 还没渲染的）里搜，不只搜 DOM。命中在未渲染区 → render.js revealItem 先补渲染（与小格子跳转同法保持滚动位置）再滚到位；
// 命中在折叠的工具卡 / 思考 / 系统消息详情 / 子代理结果里 → 展开并在查找栏下方说明；命中的条目被「工具调用显示」档位隐藏 →
// 查找期间临时显示那一条。高亮用 CSS Custom Highlight API（只画视口上下各一屏以内的命中），不支持时退回 <mark>。
// 只读：不发请求、不按键、不碰剪贴板；查找栏是 #chat-host 里的覆盖层，不改变时间线的布局。
//
// 移植自 T3 Code（https://github.com/pingdotgg/t3code，MIT License，Copyright (c) 2026 T3 Tools Inc.）：
//   apps/web/src/components/chat/threadFind.ts（计数 n/m、上一个 / 下一个循环）、ThreadFindBar.tsx（查找栏：输入框、计数、
//   上 / 下 / 关闭，Enter / Shift+Enter，输入法组字中的回车不算，最多 200 字）、threadFindHighlights.ts（文字收集：块级元素断开、
//   pre 外的换行当空格、跳过忽略区；两个 Highlight：全部 + 当前；#38329「只画视口附近的命中」：视口上下各一屏）、
//   threadFindPosition.ts（新查询从视口顶端开始的第一处命中算起）。
// 与 T3 的差异：数据层在前端按条目文字搜（T3 由服务端搜整个线程）；文字/Markdown 条目的可搜文字 = 用同一套渲染器在页面外渲染后
//   收集的文字（与界面上的一致），工具卡正文按字段（命令、参数、输出全文）搜，界面上对应不上时就近定位并提示；
//   命中计数上限 MATCH_CAP；查找期间流式到达的新条目会重新计数并保持当前那一处。

import { h, icon } from './dom.js';
import { renderMarkdown } from './markdown.js';
import { userBody } from './quote.js';
import { toolInfo } from './tools.js';
import { stripAnsi } from './ctxcard.js';
import { keepReading } from './toolview.js';

export const FIND_MAX = 200;           // 查询最多多少字（T3 同）
export const MATCH_CAP = 9999;         // 最多数这么多处（再多显示 9999+）
export const FIND_HL = 'ccr-find';
export const FIND_HL_ACTIVE = 'ccr-find-active';
export const FIND_TOP = 64;            // 滚到位时命中离视口顶端至少这么多（查找栏压在时间线右上角）
// 收集文字时跳过：按钮 / 时间 / 语言标签 / 状态 / 图标 / 行号 / 「显示全部」等附属文字
export const FIND_IGNORE = '.msg-meta, .md-code-head, .md-copy, .tbl-acts, .user-tag, .user-imgs, .tool-st, .chev, .url-pv, .cmd-mark, svg, button.link-btn, .ln, .diff-sign, .find-ignore';
const BLOCK_TAGS = new Set(['P', 'DIV', 'LI', 'UL', 'OL', 'PRE', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'TABLE', 'THEAD', 'TBODY',
  'TR', 'TD', 'TH', 'DETAILS', 'SUMMARY', 'SECTION', 'HR', 'DL', 'DT', 'DD']);
const USER_SCOPE = '.user-quote, .user-text';

// ---------------------------------------------------------------- 纯函数

/** 忽略大小写（逐字转小写，长度不变：位置能一一对应回原文） */
export function foldCase(s) {
  s = String(s ?? '');
  const low = s.toLowerCase();
  if (low.length === s.length) return low;
  let out = '';
  for (const ch of s) { const l = ch.toLowerCase(); out += l.length === ch.length ? l : ch; }
  return out;
}

/** 查询规整：最多 FIND_MAX 字；全是空白 = 空 */
export function normQuery(q) {
  const s = String(q ?? '').slice(0, FIND_MAX);
  return s.trim() ? s : '';
}

/** 不重叠的出现位置（text、q 都已 foldCase） */
export function occurrences(text, q, cap = Infinity) {
  const out = [];
  if (!q) return out;
  for (let i = text.indexOf(q); i >= 0 && out.length < cap; i = text.indexOf(q, i + q.length)) out.push(i);
  return out;
}

function flatStrings(v, out, depth = 0) {
  if (typeof v === 'string') { if (v) out.push(v); return; }
  if (depth > 4 || v == null || typeof v !== 'object') return;
  if (Array.isArray(v)) { for (const x of v) flatStrings(x, out, depth + 1); return; }
  for (const k of Object.keys(v)) flatStrings(v[k], out, depth + 1);
}

function resultBlocks(c) {
  if (typeof c === 'string') return [c];
  if (Array.isArray(c)) return c.filter((b) => b && b.type === 'text').map((b) => String(b.text || ''));
  return [];
}

/** 工具卡展开后的正文（按字段：参数里的字符串、输出全文、子代理结果、提问的回答） */
export function toolBodyBlocks(t) {
  const out = [];
  flatStrings(t.input, out);
  out.push(...resultBlocks(t.result));
  if (t.note && t.note.result) out.push(String(t.note.result));
  if (t.structured && t.structured.answers && typeof t.structured.answers === 'object') flatStrings(t.structured.answers, out);
  return out;
}

/**
 * 条目的可搜内容：[{scope, blocks:[文字], fold}]。scope = 条目元素里对应的 DOM 区域（CSS 选择器，':scope' = 条目本身）；
 * fold = 折叠类型（'tool' | 'thinking' | 'subnote' | 'details'；null = 一直显示）。
 * env.md(text) / env.user(text) → 文字块（浏览器里 = 用同一套渲染器在页面外渲染后收集的文字）。不可搜的条目 → null
 */
export function itemParts(item, env) {
  if (!item) return null;
  const md = (env && env.md) || ((t) => [String(t ?? '')]);
  const user = (env && env.user) || ((t) => [String(t ?? '')]);
  switch (item.kind) {
    case 'user': return item.text ? [{ scope: USER_SCOPE, blocks: user(item.text), fold: null }] : null;
    case 'text': return item.text ? [{ scope: ':scope', blocks: md(item.text), fold: null }] : null;
    case 'thinking': return item.text && !item.redacted ? [{ scope: '.think-body', blocks: [item.text], fold: 'thinking' }] : null;
    case 'tool': {
      const info = toolInfo(item.name, item.input || {});
      return [{ scope: '.tool-name, .tool-sum', blocks: [info.label || '', info.summary || ''], fold: null },
        { scope: '.tool-body', blocks: toolBodyBlocks(item), fold: 'tool' }];
    }
    case 'cmd': {
      const parts = [];
      if (item.command) parts.push({ scope: '.cmd-line', blocks: [item.command], fold: null });
      if (item.output) parts.push({ scope: '.cmd-out', blocks: [stripAnsi(String(item.output)).replace(/`/g, '')], fold: null });
      return parts.length ? parts : null;
    }
    case 'hint': {
      const parts = [{ scope: 'summary, :scope > span', blocks: [item.text || ''], fold: null }];
      if (item.detail) parts.push({ scope: 'details > pre', blocks: [String(item.detail)], fold: 'details' });
      return parts;
    }
    case 'notice': case 'divider': return item.text ? [{ scope: ':scope > span', blocks: [item.text], fold: null }] : null;
    case 'subnote': {
      const parts = [{ scope: '.sa-note-text', blocks: [item.desc || ''], fold: null }];
      const r = item.note && item.note.result;
      if (r) parts.push({ scope: '.sa-note-result', blocks: md(String(r)), fold: 'subnote' });
      return parts;
    }
    default: return null;
  }
}

/**
 * 全部命中：[{index, item, part, occ}]（按条目顺序；occ = 该条目该部分里的第几处）。
 * partsOf(item) → [{folded:[已 foldCase 的文字块]}]（调用方缓存）。超过 cap 处停止，返回 {matches, capped}
 */
export function buildMatches(items, query, partsOf, cap = MATCH_CAP) {
  const q = foldCase(normQuery(query));
  const matches = [];
  if (!q) return { matches, capped: false };
  const list = items || [];
  for (let index = 0; index < list.length; index++) {
    const parts = partsOf(list[index]);
    if (!parts) continue;
    for (let part = 0; part < parts.length; part++) {
      let occ = 0;
      for (const b of parts[part].folded || []) {
        for (const _ of occurrences(b, q, cap - matches.length + 1)) {
          if (matches.length >= cap) return { matches, capped: true };
          matches.push({ index, item: list[index], part, occ: occ++ });
        }
      }
    }
  }
  return { matches, capped: false };
}

/** 上一个 / 下一个（循环）；没有命中 = -1 */
export function stepIndex(i, n, d) {
  if (!(n > 0)) return -1;
  const c = Number.isFinite(i) && i >= 0 ? Math.min(Math.trunc(i), n - 1) : (d < 0 ? 0 : -1);
  return (((c + d) % n) + n) % n;
}

/** 计数文字：「3/17」；没有命中「0/0」；超过上限「3/9999+」 */
export function formatCount(i, n, capped) {
  if (!(n > 0)) return '0/0';
  const c = Number.isFinite(i) && i >= 0 ? Math.min(i, n - 1) : 0;
  return (c + 1) + '/' + n + (capped ? '+' : '');
}

/** 新查询从哪一处开始：视口里第一条（及以后）条目里的第一处；都在上面 → 最后一处 */
export function startIndex(matches, fromIndex) {
  if (!matches || !matches.length) return -1;
  for (let i = 0; i < matches.length; i++) if (matches[i].index >= fromIndex) return i;
  return matches.length - 1;
}

/** 重新计数后找回原来那一处（同一条目、同一部分、同一序号；没有了就取其后最近的一处） */
export function relocate(matches, prev) {
  if (!matches || !matches.length) return -1;
  if (!prev) return -1;
  let after = -1;
  for (let i = 0; i < matches.length; i++) {
    const m = matches[i];
    if (m.item === prev.item && m.part === prev.part && m.occ === prev.occ) return i;
    if (after < 0 && (m.index > prev.index || (m.item === prev.item && (m.part > prev.part || (m.part === prev.part && m.occ > prev.occ))))) after = i;
  }
  return after >= 0 ? after : matches.length - 1;
}

/** 文字块里的位置 → 文本节点区间：nodes = [{start, end}]（按顺序），返回 {si, so, ei, eo}；越界 = null */
export function locate(nodes, offset, len) {
  let si = -1, ei = -1;
  for (let k = 0; k < nodes.length; k++) {
    if (si < 0 && nodes[k].end > offset) si = k;
    if (si >= 0 && nodes[k].end >= offset + len) { ei = k; break; }
  }
  if (si < 0 || ei < 0) return null;
  return { si, so: offset - nodes[si].start, ei, eo: offset + len - nodes[ei].start };
}

// ---------------------------------------------------------------- DOM：收集文字

/** roots 里的文字块：[{text, nodes:[{node, start, end}]}]。块级元素与 <br> 断开、忽略区断开；pre 里保留换行，其它换行 → 空格（长度不变） */
export function textBlocks(roots, ignore = FIND_IGNORE) {
  const blocks = [];
  let cur = { text: '', nodes: [] };
  const flush = () => { if (cur.nodes.length) blocks.push(cur); cur = { text: '', nodes: [] }; };
  const visit = (node, inPre) => {
    if (node.nodeType === 3) {
      const v = node.nodeValue;
      if (!v) return;
      const start = cur.text.length;
      cur.text += inPre ? v : v.replace(/[\r\n]/g, ' ');
      cur.nodes.push({ node, start, end: cur.text.length });
      return;
    }
    if (node.nodeType !== 1 && node.nodeType !== 11) return;
    const tag = node.nodeType === 1 ? node.tagName : '';
    if (tag) {
      if (node.matches(ignore)) { flush(); return; }
      if (tag === 'BR') { flush(); return; }
    }
    const block = BLOCK_TAGS.has(tag);
    if (block) flush();
    for (const c of node.childNodes) visit(c, inPre || tag === 'PRE');
    if (block) flush();
  };
  for (const r of roots || []) {
    if (!r) continue;
    flush();
    visit(r, !!(r.nodeType === 1 && r.parentElement && r.parentElement.closest('pre')));
    flush();
  }
  return blocks;
}

function scopeEls(el, scope) {
  if (!el) return [];
  if (scope === ':scope') return [el];
  return [...el.querySelectorAll(scope)];
}

/** 页面外渲染后收集文字（与界面同一套渲染器、不带可视化钩子：不发请求） */
function detachedBlocks(frag, scope) {
  const box = document.createElement('div');
  box.appendChild(frag);
  return textBlocks(scopeEls(box, scope)).map((b) => b.text);
}

export const BROWSER_ENV = {
  md: (t) => detachedBlocks(renderMarkdown(t), ':scope'),
  user: (t) => detachedBlocks(userBody(t), USER_SCOPE),
};

/** 条目某一部分在界面上的全部命中：[{blk, si, so, ei, eo}]（blk = 文字块） */
export function domMatches(el, scope, q) {
  const out = [];
  if (!el || !q) return out;
  for (const blk of textBlocks(scopeEls(el, scope))) {
    for (const off of occurrences(foldCase(blk.text), q)) {
      const loc = locate(blk.nodes, off, q.length);
      if (loc) out.push({ blk, ...loc });
    }
  }
  return out;
}

function toRange(m) {
  const r = document.createRange();
  r.setStart(m.blk.nodes[m.si].node, m.so);
  r.setEnd(m.blk.nodes[m.ei].node, m.eo);
  return r;
}

function highlightApi() {
  return typeof CSS !== 'undefined' && CSS && CSS.highlights && typeof Highlight === 'function';
}

const SIG_KEYS = ['text', 'detail', 'command', 'output', 'result', 'input', 'structured', 'note', 'desc', 'name', 'redacted'];

// ---------------------------------------------------------------- 查找栏 + 控制

/**
 * 在 host（#chat-host）里挂查找栏。setSession(s) 跟随当前会话 / 历史预览；onFlush(s) 由 render.js 的 state.onFlush 调用。
 * reveal(s, item) = render.js revealItem；flush(s) = render.js flushNow（展开折叠的条目后立即重渲染）。
 * forceMark = true：不用 CSS Highlight API，改用 <mark>（回放页验证退路）。
 */
export class SessionFind {
  constructor(host, { reveal, flush, onClose, forceMark = false } = {}) {
    this.host = host;
    this.reveal = reveal || null;
    this.flush = flush || null;
    this.onCloseCb = onClose || null;
    this.useMark = forceMark || !highlightApi();
    this.s = null;
    this.view = null;
    this.query = '';
    this.matches = [];
    this.capped = false;
    this.active = -1;
    this.cache = new WeakMap();   // item → {sig, parts}
    this.shown = new Set();       // 被档位隐藏、查找期间临时显示的条目
    this.marks = [];
    this.frame = 0;
    this.searchTimer = 0;
    this.flushTimer = 0;
    this.lastFlushSearch = 0;
    this.restoreFocus = null;
    this.pending = false;        // 当前那一处还在折叠 / 被隐藏的条目里（边输入边找时不展开），下一次 Enter 先展开它
    this.activeDom = null;
    this.activeRange = null;
    this.painted = 0;

    this.input = h('input', { class: 'find-input', type: 'text', name: 'find', 'aria-label': '在会话中查找', placeholder: '在会话中查找',
      maxlength: String(FIND_MAX), autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', autocorrect: 'off' });
    this.count = h('span', { class: 'find-count', 'aria-hidden': 'true' });
    this.live = h('span', { class: 'find-live', 'aria-live': 'polite' });
    this.prevBtn = h('button', { class: 'find-btn', type: 'button', title: '上一个（Shift+Enter）', 'aria-label': '上一个' }, icon('chevup'));
    this.nextBtn = h('button', { class: 'find-btn', type: 'button', title: '下一个（Enter）', 'aria-label': '下一个' }, icon('chevdown'));
    this.closeBtn = h('button', { class: 'find-btn', type: 'button', title: '关闭（Esc）', 'aria-label': '关闭查找' }, icon('x'));
    this.note = h('div', { class: 'find-note', hidden: true });
    this.el = h('div', { class: 'find-bar', role: 'search', 'aria-label': '在会话中查找', hidden: true },
      h('div', { class: 'find-row' }, icon('search', 'find-ic'), this.input, this.count, this.live, this.prevBtn, this.nextBtn, this.closeBtn),
      this.note);
    host.appendChild(this.el);
    for (const b of [this.prevBtn, this.nextBtn, this.closeBtn]) b.addEventListener('mousedown', (e) => e.preventDefault());
    this.prevBtn.addEventListener('click', () => this.prev());
    this.nextBtn.addEventListener('click', () => this.next());
    this.closeBtn.addEventListener('click', () => this.close());
    this.input.addEventListener('input', () => { clearTimeout(this.searchTimer); this.searchTimer = setTimeout(() => this.run(true), 90); });
    this.input.addEventListener('keydown', (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); this.flushPending(); if (e.shiftKey) this.prev(); else this.next(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.close(); }
    });
    this.onScroll = () => this.schedule();
    if (typeof ResizeObserver === 'function') new ResizeObserver(() => { if (this.isOpen()) this.schedule(); }).observe(host);
  }

  isOpen() { return !this.el.hidden; }

  /** 打开（已打开 = 聚焦并全选）；seed = 预填的查询（例如当前选中的文字） */
  open(seed) {
    if (!this.isOpen()) {
      const a = document.activeElement;
      this.restoreFocus = a && a !== document.body && !this.el.contains(a) ? a : null;
      this.el.hidden = false;
    }
    if (typeof seed === 'string' && seed.trim() && !seed.includes('\n')) this.input.value = seed.slice(0, FIND_MAX);
    this.input.focus({ preventScroll: true });
    this.input.select();
    this.run(true);
  }

  close() {
    if (!this.isOpen()) return;
    clearTimeout(this.searchTimer);
    this.el.hidden = true;
    this.clearPaint();
    if (this.shown.size && this.s && this.s.view) { const sh = this.shown; keepReading(this.s, () => this.unshowAll(), (it) => !sh.has(it)); }
    else this.unshowAll();
    this.pending = false;
    this.matches = [];
    this.active = -1;
    this.note.hidden = true;
    const f = this.restoreFocus;
    this.restoreFocus = null;
    if (f && f.isConnected && !f.disabled) f.focus({ preventScroll: true });
    if (this.onCloseCb) this.onCloseCb();
  }

  /** 跟随当前会话 / 历史预览（null = 没有）。换会话时：清掉旧会话上的高亮，查找栏开着就在新会话里重新搜 */
  setSession(s) {
    const v = (s && s.view) || null;
    if (s === this.s && v === this.view) return;
    if (this.view) this.view.root.removeEventListener('scroll', this.onScroll);
    this.clearPaint();
    this.unshowAll();
    this.s = s || null;
    this.view = v;
    this.matches = [];
    this.active = -1;
    if (v) v.root.addEventListener('scroll', this.onScroll, { passive: true });
    if (this.isOpen()) this.run(false);
  }

  /** 条目写进 DOM / 补渲染之后：查找栏开着就重画；有新条目时（节流）重新计数，保持当前那一处 */
  onFlush(s) {
    if (!this.isOpen() || !s || s !== this.s) return;
    this.schedule();
    if (this.flushTimer) return;
    const wait = Math.max(0, 250 - (performance.now() - this.lastFlushSearch));
    this.flushTimer = setTimeout(() => { this.flushTimer = 0; this.lastFlushSearch = performance.now(); this.run(false, true); }, wait);
  }

  flushPending() {
    if (this.searchTimer) { clearTimeout(this.searchTimer); this.searchTimer = 0; if (normQuery(this.input.value) !== this.query) this.run(true); }
  }

  /** 条目的可搜内容（缓存：相关字段的引用都没变就复用） */
  partsOf(item) {
    const sig = SIG_KEYS.map((k) => item[k]);
    const c = this.cache.get(item);
    if (c && c.sig.every((v, k) => v === sig[k])) return c.parts;
    let parts = null;
    try { parts = itemParts(item, BROWSER_ENV); } catch (_) { parts = null; }
    if (parts) for (const p of parts) p.folded = p.blocks.map(foldCase);
    this.cache.set(item, { sig, parts });
    return parts;
  }

  /** 重新搜。fresh = 新查询（从视口开始；不在视口里就滚过去）；否则保持当前那一处 */
  run(fresh, quiet) {
    this.searchTimer = 0;
    if (!this.isOpen()) return;   // 关掉之后到期的定时器（输入防抖 / 流式重新计数）什么都不做，更不会滚动
    const s = this.s;
    const q = normQuery(this.input.value);
    const prev = this.active >= 0 ? this.matches[this.active] : null;
    const changed = q !== this.query;
    this.query = q;
    if (!s || !q) {
      this.matches = []; this.capped = false; this.active = -1;
      this.unshowAll();
      this.setNote('');
      this.renderCount();
      this.paint();
      return;
    }
    const r = buildMatches(s.items, q, (it) => this.partsOf(it));
    this.matches = r.matches;
    this.capped = r.capped;
    if (fresh || changed || !prev) {
      this.active = startIndex(this.matches, this.firstVisibleIndex());
      this.renderCount();
      if (this.active >= 0) this.go(this.active, { land: false });
      else { this.unshowAll(); this.setNote(''); this.paint(); }
      return;
    }
    this.active = relocate(this.matches, prev);
    this.renderCount(quiet);
    this.paint();
  }

  /** 视口里第一条可见条目在 s.items 里的下标（没有 = renderStart） */
  firstVisibleIndex() {
    const s = this.s, v = this.view;
    if (!s || !v) return 0;
    const rr = v.root.getBoundingClientRect();
    const end = Math.min(s.renderedTo ?? s.items.length, s.items.length);
    let lo = s.renderStart || 0, hi = end;
    const bottomOf = (k) => {
      for (let j = k; j < end; j++) {
        const el = s.items[j] && s.items[j].el;
        if (!el || !el.isConnected) continue;
        const r = el.getBoundingClientRect();
        if (r.width || r.height) return { j, b: r.bottom - rr.top };
      }
      return { j: end, b: Infinity };
    };
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const x = bottomOf(mid);
      if (x.b > 0) hi = mid; else lo = x.j + 1;
    }
    return lo;
  }

  renderCount(quiet) {
    const n = this.matches.length;
    const text = this.query ? formatCount(this.active, n, this.capped) : '';
    this.count.textContent = text;
    this.count.classList.toggle('none', !!this.query && !n);
    this.count.title = this.query && !n ? '没有找到' : '';
    this.prevBtn.disabled = this.nextBtn.disabled = !n;
    if (!quiet) this.live.textContent = this.query ? (n ? '第 ' + text + ' 处' : '没有找到') : '';
  }

  setNote(text) {
    this.note.textContent = text || '';
    this.note.hidden = !text;
  }

  /** 下一个 / 上一个；刚输入的查询停在折叠 / 被隐藏的那一处时，第一次按 Enter 先把它展开 / 显示出来 */
  next() { this.flushPending(); if (!this.matches.length) return; if (this.pending) this.go(this.active); else this.go(stepIndex(this.active, this.matches.length, 1)); }
  prev() { this.flushPending(); if (!this.matches.length) return; if (this.pending) this.go(this.active); else this.go(stepIndex(this.active, this.matches.length, -1)); }

  /**
   * 跳到第 i 处：补渲染 → 展开折叠 → 临时显示被档位隐藏的条目 → 滚到位 → 画高亮。
   * land=false（边输入边找）：不展开、不临时显示（说明里提示按 Enter），已在视口里就不滚
   */
  go(i, { land = true } = {}) {
    const s = this.s, v = this.view, m = this.matches[i];
    if (!s || !v || !m) return;
    this.active = i;
    this.pending = false;
    this.renderCount();
    const root = v.root;
    v.stick = false;   // 展开 / 补渲染时不要被拉回底部
    if (this.reveal && (!m.item.el || !m.item.el.isConnected || s.items.indexOf(m.item) < s.renderStart)) this.reveal(s, m.item);
    const el = m.item.el;
    if (!el || !el.isConnected) { this.setNote('这一处所在的条目还没有显示出来'); this.paint(); return; }
    const notes = [];
    const parts = this.partsOf(m.item) || [];
    const part = parts[m.part];
    const FOLD_TEXT = { tool: '在折叠的工具卡里', thinking: '在 Claude 的思考里', subnote: '在子代理的结果里', details: '在系统消息的详情里' };
    if (part && part.fold && this.folded(m.item, part.fold)) {
      if (land) { this.unfold(m.item, part.fold); notes.push(FOLD_TEXT[part.fold] + '，已展开'); }
      else { this.pending = true; notes.push(FOLD_TEXT[part.fold] + '（按 Enter 展开）'); }
    }
    // 被「工具调用显示」档位隐藏（display:none）：查找期间临时显示这一条
    for (const it of [...this.shown]) if (it !== m.item) this.unshow(it);
    if (m.item.el && m.item.el.getClientRects().length === 0 && !m.item.el.dataset.findShow) {
      if (land) {
        m.item.el.dataset.findShow = '1';
        this.shown.add(m.item);
        notes.push('这条被「工具调用显示」隐藏了，查找期间临时显示');
      } else { this.pending = true; notes.push('这条被「工具调用显示」隐藏了（按 Enter 临时显示）'); }
    }
    const hiddenNow = m.item.el.getClientRects().length === 0;
    let dm = this.domFor(m, part);
    if (land && !dm.list.length && part) {
      // 超长输出只显示了前一段：点开「显示全部」再找（只是展开我们自己的界面）
      for (const b of scopeEls(m.item.el, part.scope).flatMap((x) => [...x.querySelectorAll('button.link-btn')])) {
        if (/^显示全部/.test(b.textContent || '')) b.click();
      }
      dm = this.domFor(m, part);
    }
    if (land && !dm.list.length) notes.push('这一处在原始记录里，界面上没有显示出来（例如被截断的输出）');
    this.activeDom = { item: m.item, part: m.part, idx: dm.list.length ? Math.min(m.occ, dm.list.length - 1) : -1 };
    this.setNote(notes.filter(Boolean).join('；'));
    this.paint();
    if (!hiddenNow) this.scrollToActive(!land, dm.list.length ? null : m.item.el, land);
    v.stick = root.scrollHeight - root.scrollTop - root.clientHeight < 60;
  }

  domFor(m, part) {
    if (!part || !m.item.el) return { list: [] };
    return { list: domMatches(m.item.el, part.scope, foldCase(this.query)) };
  }

  folded(item, fold) {
    if (fold === 'details') { const d = item.el && item.el.querySelector('details'); return !!d && !d.open; }
    return !item.expanded;
  }

  unfold(item, fold) {
    if (fold === 'details') { const d = item.el && item.el.querySelector('details'); if (d) d.open = true; return; }
    item.expanded = true;
    if (this.s && this.s.dirty) this.s.dirty.add(item);
    if (this.flush) this.flush(this.s);
  }

  unshow(it) {
    this.shown.delete(it);
    if (it.el) delete it.el.dataset.findShow;
  }

  unshowAll() {
    if (!this.shown.size) return;
    const list = [...this.shown];
    this.shown.clear();
    for (const it of list) if (it.el) delete it.el.dataset.findShow;
  }

  /** 当前那一处滚到视口里（离顶端 FIND_TOP 以下、视口高度 1/3 左右）；onlyIfHidden = 已经完整可见就不动 */
  scrollToActive(onlyIfHidden, fallbackEl, flash = true) {
    const v = this.view;
    if (!v) return;
    const root = v.root;
    const rr = root.getBoundingClientRect();
    let r = null;
    if (fallbackEl) r = fallbackEl.getBoundingClientRect();
    else {
      const a = this.activeRangeNow();
      if (a) r = a.getBoundingClientRect();
    }
    if (!r || (!r.width && !r.height)) return;
    const top = r.top - rr.top, bottom = r.bottom - rr.top;
    if (onlyIfHidden && top >= FIND_TOP && bottom <= root.clientHeight - 24) return;
    const want = Math.max(FIND_TOP, Math.round(root.clientHeight / 3) - (bottom - top) / 2);
    const max = Math.max(0, root.scrollHeight - root.clientHeight);
    root.scrollTop = Math.max(0, Math.min(max, root.scrollTop + top - want));
    if (fallbackEl && flash) { fallbackEl.classList.remove('find-flash'); void fallbackEl.offsetWidth; fallbackEl.classList.add('find-flash'); setTimeout(() => fallbackEl.classList.remove('find-flash'), 1400); }
  }

  /** 当前那一处在界面上的 Range（<mark> 模式下是那个 mark 元素） */
  activeRangeNow() {
    if (this.useMark) {
      const mk = this.view && this.view.root.querySelector('mark.ccr-find-mark.active');
      if (!mk) return null;
      const r = document.createRange();
      r.selectNodeContents(mk);
      return r;
    }
    return this.activeRange || null;
  }

  schedule() {
    if (this.frame || !this.isOpen()) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.paint(); });
  }

  /** 有命中的已渲染条目里，视口上下各一屏以内的（二分找起点） */
  zoneItems() {
    const s = this.s, v = this.view;
    if (!s || !v || !this.matches.length) return [];
    const root = v.root;
    const rr = root.getBoundingClientRect();
    const H = root.clientHeight || rr.height;
    const top = -H, bottom = 2 * H;
    const items = [];
    let last = null;
    for (const m of this.matches) if (m.item !== last) { last = m.item; if (m.item.el && m.item.el.isConnected) items.push(m.item); }
    const rectOf = (it) => { const r = it.el.getBoundingClientRect(); return (r.width || r.height) ? { t: r.top - rr.top, b: r.bottom - rr.top } : null; };
    let lo = 0, hi = items.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      let j = mid, r = null;
      while (j < hi && !(r = rectOf(items[j]))) j++;
      if (!r || r.b >= top) hi = mid; else lo = j + 1;
    }
    const out = [];
    for (let k = lo; k < items.length; k++) {
      const r = rectOf(items[k]);
      if (!r) continue;
      if (r.t > bottom) break;
      out.push(items[k]);
    }
    if (this.activeDom && this.activeDom.item.el && !out.includes(this.activeDom.item)) out.push(this.activeDom.item);
    return out;
  }

  /** 画高亮：视口附近条目里界面上的全部命中 + 当前那一处 */
  paint() {
    const q = foldCase(this.query);
    if (!this.isOpen() || !q || !this.view) { this.clearPaint(); return; }
    if (this.useMark) this.unmark();   // 先拆掉上一次的 <mark>（拆完会合并文本节点），再收集命中
    const all = [];
    let active = null;
    for (const it of this.zoneItems()) {
      const parts = this.partsOf(it) || [];
      parts.forEach((p, pi) => {
        if (p.fold && this.folded(it, p.fold)) return;
        const list = domMatches(it.el, p.scope, q);
        list.forEach((dm, k) => {
          const isActive = !!this.activeDom && this.activeDom.item === it && this.activeDom.part === pi && this.activeDom.idx === k;
          all.push({ dm, active: isActive });
          if (isActive) active = dm;
        });
      });
    }
    if (this.useMark) { this.paintMarks(all); return; }
    const hl = new Highlight();
    for (const x of all) if (!x.active) hl.add(toRange(x.dm));
    const ah = new Highlight();
    this.activeRange = active ? toRange(active) : null;
    if (this.activeRange) ah.add(this.activeRange);
    ah.priority = 1;
    CSS.highlights.set(FIND_HL, hl);
    CSS.highlights.set(FIND_HL_ACTIVE, ah);
    this.painted = all.length;
  }

  /** 退路：<mark> 包住命中的文字（paint 已先拆掉上一次的；按文本节点从后往前包，位置不乱） */
  paintMarks(all) {
    const byNode = new Map();
    for (const x of all) {
      const { blk, si, so, ei, eo } = x.dm;
      for (let k = si; k <= ei; k++) {
        const n = blk.nodes[k];
        const a = k === si ? so : 0, b = k === ei ? eo : n.end - n.start;
        if (b <= a) continue;
        if (!byNode.has(n.node)) byNode.set(n.node, []);
        byNode.get(n.node).push({ a, b, active: x.active });
      }
    }
    for (const [node, segs] of byNode) {
      segs.sort((x, y) => y.a - x.a);
      for (const g of segs) {
        if (!node.parentNode) break;
        node.splitText(g.b);
        const mid = node.splitText(g.a);
        const mk = document.createElement('mark');
        mk.className = 'ccr-find-mark' + (g.active ? ' active' : '');
        mid.parentNode.insertBefore(mk, mid);
        mk.appendChild(mid);
        this.marks.push(mk);
      }
    }
    this.painted = all.length;
  }

  unmark() {
    const parents = new Set();
    for (const mk of this.marks) {
      const p = mk.parentNode;
      if (!p) continue;
      while (mk.firstChild) p.insertBefore(mk.firstChild, mk);
      mk.remove();
      parents.add(p);
    }
    for (const p of parents) p.normalize();
    this.marks = [];
  }

  clearPaint() {
    if (this.frame) { cancelAnimationFrame(this.frame); this.frame = 0; }
    this.activeRange = null;
    this.activeDom = null;
    this.painted = 0;
    if (this.marks.length) this.unmark();
    if (highlightApi()) { CSS.highlights.delete(FIND_HL); CSS.highlights.delete(FIND_HL_ACTIVE); }
  }
}

/** 选中的文字（一行、不长）→ 打开查找栏时预填 */
function selectedSeed() {
  const sel = window.getSelection ? window.getSelection() : null;
  if (!sel || sel.isCollapsed || !sel.rangeCount) return '';
  const a = document.activeElement;
  if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA')) return '';
  const t = sel.toString();
  return t && t.length <= 100 && !/\n/.test(t.trim()) ? t.trim() : '';
}

/**
 * 全局按键：⌘F（非 Mac 用 Ctrl+F）打开 / 聚焦查找栏（有选中的一行文字时预填）；⌘G / ⇧⌘G 下一个 / 上一个（查找栏开着时）。
 * 原生窗口的「编辑 → 查找…」菜单项派发 ccr-find 事件（detail.action = open | next | prev）。blocked() = 有弹窗 / 灯箱等，不处理
 */
export function installFindKeys(find, { blocked, enabled } = {}) {
  const mac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  const ok = () => (!blocked || !blocked()) && (!enabled || enabled());
  const act = (action) => {
    if (!ok()) return false;
    if (action === 'open') { find.open(selectedSeed()); return true; }
    if (!find.isOpen()) { if (action === 'next' || action === 'prev') { find.open(selectedSeed()); return true; } return false; }
    if (action === 'next') find.next(); else if (action === 'prev') find.prev();
    return true;
  };
  document.addEventListener('keydown', (e) => {
    const mod = mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
    if (!mod || e.altKey || e.isComposing) return;
    const k = String(e.key || '').toLowerCase();
    if (k === 'f' && !e.shiftKey) { if (act('open')) e.preventDefault(); }
    else if (k === 'g' && find.isOpen()) { if (act(e.shiftKey ? 'prev' : 'next')) e.preventDefault(); }
  });
  window.addEventListener('ccr-find', (e) => { act((e && e.detail && e.detail.action) || 'open'); });
}
