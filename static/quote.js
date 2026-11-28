// 引用回复（docs/SPEC_V2_TUI.md §7「引用回复」；用户需求）：在消息里（以及右侧预览面板的文件 / diff / 代码改动视图里）
// 选中文字 → 浮出「引用」按钮 → 输入框上方出现引用条（可多条、每条可删）→ 发送时拼成 Markdown 引用（`> …`，
// 来自文件的带 `文件:行号` 与代码围栏）放在用户文字前面。只读选区、只改我们自己的输入框 / 状态：不碰剪贴板、不发请求、不按键。
// 发出去的用户消息开头的引用在气泡里显示成内嵌卡片（userBody）。
//
// 参考（均为 MIT License）：
//   T3 Code（https://github.com/pingdotgg/t3code，Copyright (c) 2026 T3 Tools Inc.）#33124「reply to an assistant response」
//     （选中助手回复 → 引用进输入框的上下文条）、#34181「a quote in a sent message shows as an inset card」（发出的消息里引用 = 内嵌卡片）；
//   Orca（https://github.com/stablyai/orca，Copyright (c) Stably AI）#38251「Add a file, markdown preview or terminal selection to the chat」
//     （文件 / markdown 预览里的选区带上路径与行号加进聊天）。
// 与原实现的差异：原生 DOM；引用以 Markdown 引用块的形式随消息文字一起发给 claude（交互式 CLI 只收文字）；
//   引用条按会话分开保存在本页内存里（不持久化）；以 / 或 ! 开头的命令消息不带引用（引用保留到下一条普通消息）。

import { h, icon, truncate } from './dom.js';
import { renderMarkdown } from './markdown.js';

export const QUOTE_MAX = 10;          // 每个会话最多几条引用
export const QUOTE_CHARS = 8000;      // 每条引用最多多少字（超出截断并注明）

// ---------------------------------------------------------------- 纯函数

/** 规整：统一换行、不换行空格 → 空格、去掉行尾空白与首尾空行；超长截断 */
export function cleanQuote(text, max = QUOTE_CHARS) {
  let t = String(text ?? '').replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ').replace(/[ \t]+$/gm, '');
  t = t.replace(/^\n+|\n+$/g, '');
  if (t.length > max) t = t.slice(0, max).replace(/\n[^\n]*$/, '') + '\n…（已截断）';
  return t;
}

/** 围栏：比正文里最长的一串反引号还长（至少 3 个） */
export function fenceFor(text) {
  let n = 3;
  for (const m of String(text ?? '').matchAll(/`{3,}/g)) n = Math.max(n, m[0].length + 1);
  return '`'.repeat(n);
}

/** 显示用路径：在 cwd 里 → 相对路径；否则原样 */
export function relPath(p, cwd) {
  p = String(p || '');
  const c = String(cwd || '').replace(/\/+$/, '');
  if (c && c !== '/' && p.startsWith(c + '/')) return p.slice(c.length + 1);
  return p;
}

export function lineRange(a, b) {
  if (!(a > 0)) return '';
  return b > a ? ':' + a + '-' + b : ':' + a;
}

const LANG = { js: 'js', mjs: 'js', cjs: 'js', jsx: 'jsx', ts: 'ts', tsx: 'tsx', py: 'python', sh: 'bash', bash: 'bash', zsh: 'bash',
  json: 'json', html: 'html', htm: 'html', css: 'css', md: 'markdown', yml: 'yaml', yaml: 'yaml', toml: 'toml', rs: 'rust', go: 'go',
  swift: 'swift', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', java: 'java', rb: 'ruby', sql: 'sql', xml: 'xml' };
export function langOf(path) {
  const m = String(path || '').match(/\.([A-Za-z0-9]+)$/);
  return m ? LANG[m[1].toLowerCase()] || '' : '';
}

/** 引用条上显示的出处 */
export function quoteSource(q, cwd) {
  if (!q) return '';
  if (q.path) return relPath(q.path, cwd) + lineRange(q.line, q.endLine);
  return q.label || '引用';
}

/** 一条引用 → Markdown 引用块（每行前加 `> `，空行写成 `>`）。
 *  文件 / diff：第一行是 `路径:行号`；有行号或是 diff 时正文放进代码围栏（缩进、空行原样保留） */
export function quoteMarkdown(q, cwd) {
  const body = cleanQuote(q && q.text);
  const out = [];
  if (q && (q.kind === 'file' || q.kind === 'diff')) {
    const loc = q.path ? relPath(q.path, cwd) + lineRange(q.line, q.endLine) : '';
    if (loc) out.push(loc);
    if (q.kind === 'diff' || q.line > 0) {
      const f = fenceFor(body);
      out.push(f + (q.kind === 'diff' ? 'diff' : q.lang || langOf(q.path)), ...body.split('\n'), f);
    } else out.push(...body.split('\n'));
  } else out.push(...body.split('\n'));
  return out.map((l) => (l ? '> ' + l : '>')).join('\n');
}

/** 以 / 或 ! 开头 = claude 的命令 / shell 模式：前面加引用会让它变成普通文字，所以不带引用 */
export function isCommandText(text) { return /^\s*[/!]/.test(String(text ?? '')); }

/** 发送：引用块（多条之间空一行）+ 空行 + 用户文字。返回 {text, used}（used=false：没有引用或命令消息，引用保留） */
export function composeMessage(quotes, text, cwd) {
  const list = Array.isArray(quotes) ? quotes.filter((q) => q && cleanQuote(q.text)) : [];
  const raw = String(text ?? '');
  if (!list.length || isCommandText(raw)) return { text: raw, used: false };
  const head = list.map((q) => quoteMarkdown(q, cwd)).join('\n\n');
  const rest = raw.replace(/^(?:[ \t]*\n)+/, '');
  return { text: rest.trim() ? head + '\n\n' + rest : head, used: true };
}

/** 用户消息开头的引用块（`>` 开头的连续行；多块之间可以有空行）→ {quotes:[正文], rest}；开头不是引用 → quotes 为空 */
export function splitLeadingQuotes(text) {
  const src = String(text ?? '');
  const lines = src.split('\n');
  const blocks = [];
  let cur = null, i = 0;
  for (; i < lines.length; i++) {
    const m = lines[i].match(/^ {0,3}> ?(.*)$/);
    if (m) { if (!cur) blocks.push(cur = []); cur.push(m[1]); continue; }
    if (/^\s*$/.test(lines[i])) { cur = null; continue; }
    break;
  }
  if (!blocks.length) return { quotes: [], rest: src };
  return { quotes: blocks.map((b) => b.join('\n')), rest: lines.slice(i).join('\n') };
}

/** diff 视图里选中的行 → 引用正文（`+` / `-` / 空格 开头）与行号范围 */
export function diffQuoteText(rows) {
  const lines = [];
  let first = 0, last = 0;
  for (const r of rows || []) {
    if (!r) continue;
    const sign = r.sign === '+' || r.sign === '-' ? r.sign : ' ';
    lines.push(sign + String(r.text ?? '').replace(/\n/g, ' '));
    const no = r.newNo > 0 ? r.newNo : r.oldNo > 0 ? r.oldNo : 0;
    if (no) { if (!first) first = no; last = no; }
  }
  return { text: lines.join('\n'), line: first, endLine: last > first ? last : 0 };
}

/** 添加引用：去掉与已有完全相同的（同出处同文字）、最多 QUOTE_MAX 条（超出丢最早的）。返回新数组 */
export function addQuote(list, q, max = QUOTE_MAX) {
  const arr = (Array.isArray(list) ? list : []).filter((x) => !(x.text === q.text && (x.path || '') === (q.path || '') && (x.line || 0) === (q.line || 0)));
  arr.push(q);
  return arr.slice(-max);
}

// ---------------------------------------------------------------- 用户气泡：开头的引用 = 内嵌卡片

/** 用户消息正文：开头的引用块各自成一张 `.user-quote` 卡片（markdown 渲染，不带可视化钩子：不发请求），其余照旧是 `.user-text` */
export function userBody(text) {
  const frag = document.createDocumentFragment();
  const { quotes, rest } = splitLeadingQuotes(text);
  for (const qt of quotes) {
    if (!qt.trim()) continue;
    frag.appendChild(h('div', { class: 'user-quote md', title: '引用' }, renderMarkdown(qt)));
  }
  const body = quotes.length ? rest.replace(/^\n+/, '') : String(text ?? '');
  if (body) frag.appendChild(h('div', { class: 'user-text' }, body));
  return frag;
}

// ---------------------------------------------------------------- 选区 → 引用

const QUOTE_ZONES = '.chat-scroll, .pv';
const QUOTE_BLOCKED = 'input, textarea, select, .composer, .find-bar, .pmap, dialog, .popover, .quote-pop, .tbl-acts';
const QUOTE_IGNORE = '.msg-meta, .md-code-head, .md-copy, .tbl-acts, .user-tag, .tool-st, .chev, svg, button.mm-btn, .ln, .pv-gutter, .pv-linemark, .jump-btn, .more-btn';

function elOf(n) { return n && (n.nodeType === 1 ? n : n.parentElement); }

/** 选区里的纯文字：克隆后去掉按钮 / 时间 / 行号这类附属文字，放到页面外按同样的 white-space 量 innerText（块与块之间是换行） */
function rangeText(range, anc) {
  const frag = range.cloneContents();
  for (const el of frag.querySelectorAll(QUOTE_IGNORE)) el.remove();
  const box = h('div', { class: 'quote-measure', 'aria-hidden': 'true' });
  try { box.style.whiteSpace = getComputedStyle(anc).whiteSpace; } catch (_) { /* 用默认 */ }
  box.appendChild(frag);
  document.body.appendChild(box);
  const t = box.innerText;
  box.remove();
  return t;
}

function itemLabel(el) {
  const it = el && el.closest('.item');
  if (!it) return '';
  if (it.classList.contains('k-user')) return '你的消息';
  if (it.classList.contains('k-text')) return 'Claude 的回复';
  if (it.classList.contains('k-thinking')) return 'Claude 的思考';
  if (it.classList.contains('k-tool')) return '工具调用';
  return '时间线';
}

function codeQuote(range, pre, anc) {
  const before = document.createRange();
  before.setStart(pre, 0);
  before.setEnd(range.startContainer, range.startOffset);
  const line = before.toString().split('\n').length;
  let text = range.toString();
  if (text.endsWith('\n')) text = text.slice(0, -1);
  const endLine = line + (text.match(/\n/g) || []).length;
  const holder = anc.closest('[data-quote-path]');
  const path = holder ? holder.dataset.quotePath : '';
  return { kind: 'file', text, path, line, endLine: endLine > line ? endLine : 0, lang: langOf(path), label: '文件' };
}

function diffRowText(range, row, k, n) {
  const tn = row.lastChild && row.lastChild.nodeType === 3 ? row.lastChild : null;
  let text = tn ? tn.nodeValue : row.textContent;
  if (tn && k === n - 1 && range.endContainer === tn) text = text.slice(0, range.endOffset);
  if (tn && k === 0 && range.startContainer === tn) text = text.slice(range.startOffset);
  return text;
}

function diffQuote(range, diff) {
  const rows = [...diff.children].filter((r) => /\bdiff-(add|del|ctx)\b/.test(r.className) && range.intersectsNode(r));
  if (!rows.length) return null;
  const data = rows.map((r, k) => {
    const lns = r.querySelectorAll('.ln');
    const sign = (r.querySelector('.diff-sign') || {}).textContent || '';
    return { sign: sign.trim(), text: diffRowText(range, r, k, rows.length),
      oldNo: lns[0] ? Number(lns[0].textContent) || 0 : 0, newNo: lns[1] ? Number(lns[1].textContent) || 0 : 0 };
  });
  const d = diffQuoteText(data);
  const holder = diff.closest('[data-path], [data-quote-path]');
  let path = holder ? holder.dataset.path || holder.dataset.quotePath || '' : '';
  if (!path) {
    const chip = diff.closest('.k-tool') && diff.closest('.k-tool').querySelector('.fchip[data-path]');
    if (chip) path = chip.dataset.path;
  }
  return { kind: 'diff', text: d.text, path, line: d.line, endLine: d.endLine, lang: 'diff', label: 'diff' };
}

/** 当前选区 → 引用 {kind:'chat'|'file'|'diff', text, path, line, endLine, lang, label}；不在可引用区域 / 没有文字 → null */
export function selectionQuote(sel) {
  if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const anc = elOf(range.commonAncestorContainer);
  if (!anc || !anc.closest(QUOTE_ZONES) || anc.closest(QUOTE_BLOCKED)) return null;
  const a = elOf(range.startContainer), b = elOf(range.endContainer);
  if (!a || !b || a.closest(QUOTE_BLOCKED) || b.closest(QUOTE_BLOCKED)) return null;
  let q = null;
  const pre = anc.closest('.pv-lines');
  const diff = anc.closest('.diff');
  if (pre) q = codeQuote(range, pre, anc);
  else if (diff) q = diffQuote(range, diff);
  else {
    const holder = anc.closest('[data-quote-path]');
    const sub = anc.closest('.sa-drawer') ? '子代理 · ' : '';
    const la = itemLabel(a), lb = itemLabel(b);
    const label = holder ? '文件' : sub + (la && la === lb && a.closest('.item') === b.closest('.item') ? la : '时间线');
    q = { kind: holder ? 'file' : 'chat', text: rangeText(range, anc), path: holder ? holder.dataset.quotePath : '', line: 0, endLine: 0, lang: '', label };
  }
  if (!q || !cleanQuote(q.text).trim()) return null;
  q.text = cleanQuote(q.text);
  return q;
}

// ---------------------------------------------------------------- 状态 + 界面

let qseq = 0;

/** 每个会话的引用（只在本页内存里） */
export class QuoteStore {
  constructor(onChange) { this.map = new Map(); this.onChange = onChange || null; }
  list(sid) { return this.map.get(sid) || []; }
  set(sid, list) {
    if (list && list.length) this.map.set(sid, list); else this.map.delete(sid);
    if (this.onChange) this.onChange(sid);
  }
  add(sid, q) { this.set(sid, addQuote(this.list(sid), { ...q, id: 'q' + (++qseq).toString(36) })); }
  remove(sid, id) { this.set(sid, this.list(sid).filter((q) => q.id !== id)); }
  clear(sid) { this.set(sid, []); }
}

/** 输入框上方的引用条：每条一行（出处 + 第一行预览 + ×），≥2 条时末尾有「全部清除」；内容不变不重建 */
export class QuoteBar {
  constructor({ onRemove, onClear } = {}) {
    this.onRemove = onRemove || null;
    this.onClear = onClear || null;
    this.key = '';
    this.el = h('div', { class: 'quote-bar', hidden: true, role: 'list', 'aria-label': '引用（发送时放在消息前面）' });
  }

  update(list, cwd) {
    const arr = Array.isArray(list) ? list : [];
    const key = JSON.stringify([arr.map((q) => q.id), cwd || '']);
    if (key === this.key) return;
    this.key = key;
    this.el.hidden = !arr.length;
    const rows = arr.map((q) => {
      const src = quoteSource(q, cwd);
      const first = cleanQuote(q.text).split('\n').find((l) => l.trim()) || '';
      const x = h('button', { class: 'quote-x', type: 'button', title: '删除这条引用', 'aria-label': '删除引用：' + truncate(first, 40) }, icon('x'));
      x.addEventListener('mousedown', (e) => e.preventDefault());   // 不抢输入框焦点
      x.addEventListener('click', () => { if (this.onRemove) this.onRemove(q.id); });
      return h('div', { class: 'quote-chip kind-' + (q.kind || 'chat'), role: 'listitem',
        title: '发送时作为 Markdown 引用（> …）放在消息前面：\n' + truncate(cleanQuote(q.text), 600) },
      icon(q.kind === 'diff' ? 'diff' : q.kind === 'file' ? 'file' : 'chat', 'quote-ic'),
      h('span', { class: 'quote-src' + (q.path ? ' mono' : '') }, src),
      h('span', { class: 'quote-prev' }, first), x);
    });
    if (arr.length >= 2) {
      const clr = h('button', { class: 'link-btn quote-clear', type: 'button', title: '删除全部 ' + arr.length + ' 条引用' }, '全部清除');
      clr.addEventListener('mousedown', (e) => e.preventDefault());
      clr.addEventListener('click', () => { if (this.onClear) this.onClear(); });
      rows.push(h('div', { class: 'quote-foot' }, h('span', { class: 'muted' }, arr.length + ' 条引用，发送时按顺序放在消息前面'), clr));
    }
    this.el.replaceChildren(...rows);
  }
}

/**
 * 选中文字后浮出的「引用」按钮（position: fixed，挂在 body 上）。canQuote() = 当前有能发消息的会话；
 * onQuote(q) 由调用方加进引用条。只在用户松开鼠标 / 键盘改选区后出现；点击后清掉选区。
 */
export class QuotePicker {
  constructor({ canQuote, onQuote } = {}) {
    this.canQuote = canQuote || (() => true);
    this.onQuote = onQuote || null;
    this.q = null;
    this.frame = 0;
    this.btn = h('button', { class: 'quote-pop-btn', type: 'button', title: '把选中的文字作为引用放进输入框（发送时在消息前面）' }, icon('chat'), h('span', null, '引用'));
    this.el = h('div', { class: 'quote-pop', hidden: true }, this.btn);
    document.body.appendChild(this.el);
    this.btn.addEventListener('mousedown', (e) => e.preventDefault());   // 保留选区
    this.btn.addEventListener('click', () => this.pick());
    this.onUp = (e) => { if (e.target && this.el.contains(e.target)) return; setTimeout(() => this.check(), 0); };
    document.addEventListener('mouseup', this.onUp);
    // 触屏（手机远程）：长按选字后的调整在 touchend 之后才结束，稍等再看选区
    document.addEventListener('touchend', (e) => { if (e.target && this.el.contains(e.target)) return; setTimeout(() => this.check(), 350); }, { passive: true });
    document.addEventListener('keyup', (e) => {
      if (e.key === 'Escape') { this.hide(); return; }
      if (e.shiftKey || e.key === 'Shift' || ((e.metaKey || e.ctrlKey) && (e.key === 'a' || e.key === 'A'))) this.check();
    });
    document.addEventListener('mousedown', (e) => { if (!this.el.contains(e.target)) this.hide(); }, true);
    document.addEventListener('selectionchange', () => { if (!this.el.hidden) this.schedule(); });
    document.addEventListener('scroll', () => { if (!this.el.hidden) this.schedule(); }, true);
    window.addEventListener('resize', () => this.hide());
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.place(); });
  }

  check() {
    const sel = window.getSelection ? window.getSelection() : null;
    const q = this.canQuote() ? selectionQuote(sel) : null;
    this.q = q;
    if (!q) { this.hide(); return; }
    this.el.hidden = false;
    this.place();
  }

  /** 放在选区末尾的下方（放不下就放上方）；选区没了 / 滚出视口 → 隐藏 */
  place() {
    const sel = window.getSelection ? window.getSelection() : null;
    if (!this.q || !sel || !sel.rangeCount || sel.isCollapsed) { this.hide(); return; }
    const range = sel.getRangeAt(0);
    const rects = [...range.getClientRects()].filter((r) => r.width || r.height);
    const r = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    if (!r || r.bottom < 0 || r.top > vh) { this.el.style.visibility = 'hidden'; return; }
    this.el.style.visibility = '';
    const w = this.el.offsetWidth || 72, hh = this.el.offsetHeight || 30;
    let top = r.bottom + 6;
    if (top + hh > vh - 8) top = Math.max(8, r.top - hh - 6);
    const left = Math.max(8, Math.min(vw - w - 8, r.right - w / 2));
    this.el.style.left = Math.round(left) + 'px';
    this.el.style.top = Math.round(top) + 'px';
  }

  hide() { this.q = null; if (!this.el.hidden) this.el.hidden = true; }

  pick() {
    const sel = window.getSelection ? window.getSelection() : null;
    const q = (this.canQuote() && selectionQuote(sel)) || this.q;
    this.hide();
    if (!q) return;
    if (sel) sel.removeAllRanges();
    if (this.onQuote) this.onQuote(q);
  }
}
