// 左侧 prompt 小格子（docs/SPEC_V2_TUI.md §7「左侧 prompt 小格子」；用户需求，参考 T3 Code 的 TimelineMinimap）：
// 时间线左侧留白里，每条用户消息一格 2px 细横条、按顺序竖排；悬停鱼眼放大 + 预览卡，点击 / Enter 跳到那条消息；
// 屏幕里可见的那几轮加深；上一轮 / 下一轮小箭头；滚轮转发给时间线；从别处开始的拖动（选文字）经过时穿透。
// 只读：不发请求、不按键、不碰剪贴板；是 #chat-host 里的覆盖层，不改变时间线（.chat-col）的宽度、位置与高度。
//
// 移植自 T3 Code（https://github.com/pingdotgg/t3code，MIT License，Copyright (c) 2026 T3 Tools Inc.）：
//   apps/web/src/components/chat/TimelineMinimap.tsx（结构、鱼眼 1 / .667 / .417 / .333、预览卡、键盘、导航按钮、拖动穿透）、
//   timelineMinimapItems.ts（条目 = 用户消息 + 该轮最后一段助手文字；预览转一行纯文本，最多解析 1500 字）、
//   MessagesTimeline.logic.ts（间距 / 最少条数 / 跳转偏移 / 常驻留白等常量与纯函数）、timelineMinimapWheel.ts（滚轮转发）。
// 与 T3 的差异：React → 原生 DOM；markdown 转纯文本用正则粗略去记号（T3 走完整 markdown 解析）；
//   竖向上限按我们的 #chat-host 不含输入框改成 calc(100% - 9rem)；导航箭头在悬停 / 聚焦整个小格子时出现（T3 要悬停到箭头本身）；
//   点击导航箭头也不抢输入框焦点；可见范围用二分查找已渲染条目的位置（T3 读虚拟列表的状态）；
//   未渲染（renderStart 之前）的条目由调用方传入的 reveal(s, item) 先补渲染再跳转。

import { h, icon } from './dom.js';

export const MM_SPACING = 8;            // 相邻两格的间距（px）
export const MM_MIN_ITEMS = 2;          // 少于这么多条用户消息不显示
export const MM_TARGET_OFFSET = 24;     // 跳转后目标消息顶端在视口顶端下方这么多 px
export const MM_PERSISTENT_GUTTER = 48; // 左侧留白 ≥ 这么多才常驻；否则只在（精确指针）悬停时淡入
export const MM_HIT_LEFT = 12;          // 小格子离 #chat-host 左边
export const MM_HIT_MAX = 40;           // 命中区最宽
export const MM_NAV_REACH = 14;         // 命中区至少这么宽，导航箭头才可点
export const MM_PREVIEW_MAX = 1500;     // 预览最多处理这么多字
export const MM_CARD_W = 320;           // 预览卡宽（窄时按 #chat-host 收窄）
export const MM_CARD_LEFT = 32;         // 预览卡离小格子左边
// 竖向上限：相对 #chat-host（只有时间线，不含会话头与输入框），上下各留 4.5rem，两端那一格的预览卡（约 110px 高，
// 以该格为中线）和上 / 下箭头（26px）都放得下。T3 是 calc(100% - 14.75rem)，它的时间线区域还包含输入框。
export const MM_MAX_HEIGHT_CSS = 'max(4rem, calc(100% - 9rem))';

// ---------------------------------------------------------------- 纯函数

/** 时间线条目（render.js 的 s.items）→ 小格子条目：每条用户消息一格（含只有图片的） */
export function minimapEntries(items, from = 0, out = []) {
  const list = items || [];
  for (let i = from; i < list.length; i++) {
    const it = list[i];
    if (it && it.kind === 'user') out.push({ id: it.id, index: i, item: it });
  }
  return out;
}

/** markdown → 一行纯文本（粗略去记号；只处理前 max 字）。空 → null */
export function plainText(md, max = MM_PREVIEW_MAX) {
  if (typeof md !== 'string' || !md) return null;
  let t = md.slice(0, max);
  t = t
    .replace(/^[ \t]*(?:```|~~~).*$/gm, ' ')                               // 代码块围栏行（内容保留）
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')                               // 图片 → alt
    .replace(/\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)/g, '$1')                 // 链接 → 文字
    .replace(/<\/?[A-Za-z][^>\n]*>/g, ' ')                                   // HTML 标签
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, '')                               // 标题
    .replace(/^[ \t]*(?:>[ \t]?)+/gm, '')                                    // 引用
    .replace(/^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm, '') // 列表 / 任务框
    .replace(/^[ \t]*(?:[-*_][ \t]*){3,}$/gm, ' ')                          // 分隔线
    .replace(/^[ \t]*\|?(?:[ \t]*:?-{2,}:?[ \t]*\|)+[ \t]*(?::?-{2,}:?)?[ \t]*\|?[ \t]*$/gm, ' ')   // 表格分隔行
    .replace(/^[ \t]*\|(.*)$/gm, (m, x) => x.replace(/\|/g, ' '))           // 表格行的竖线
    .replace(/`+([^`\n]*?)`+/g, '$1')                                        // 行内代码
    .replace(/(\*\*|__|~~)(?=\S)([^\n]*?\S)\1/g, '$2')                       // 粗体 / 删除线
    .replace(/(^|[^\w*\\])[*_](?=\S)([^*_\n]*?\S)[*_](?![\w*])/g, '$1$2')     // 斜体
    .replace(/\\([\\`*_{}[\]()#+\-.!|>~])/g, '$1')                          // 转义
    .replace(/\s+/g, ' ')
    .trim();
  return t || null;
}

/** 预览内容：用户文字一行 + 该轮（到下一条用户消息之前）最后一段助手文字 */
export function turnPreview(items, entry, nextIndex) {
  if (!entry || !entry.item) return { user: null, assistant: null };
  const it = entry.item;
  let user = plainText(typeof it.text === 'string' ? it.text : '');
  const imgs = Array.isArray(it.imageBlocks) ? it.imageBlocks.length : 0;
  if (!user && imgs) user = imgs > 1 ? `图片消息（${imgs} 张）` : '图片消息';
  let assistant = null;
  const list = items || [];
  const end = Math.min(nextIndex == null ? list.length : nextIndex, list.length);
  for (let i = end - 1; i > entry.index; i--) {
    const x = list[i];
    if (x && x.kind === 'text' && typeof x.text === 'string' && x.text.trim()) { assistant = plainText(x.text); break; }
  }
  return { user, assistant };
}

export function heightStyle(n) {
  const natural = Math.max(1, (n - 1) * MM_SPACING);
  return `min(${natural}px, ${MM_MAX_HEIGHT_CSS})`;
}

export function topPercent(i, n) {
  if (n <= 1) return 0;
  return (Math.max(0, Math.min(i, n - 1)) / (n - 1)) * 100;
}

export function indexFromPointer({ itemCount, railTop, railHeight, pointerY }) {
  if (itemCount <= 0 || railHeight <= 0) return null;
  if (itemCount === 1) return 0;
  const p = Math.max(0, Math.min(1, (pointerY - railTop) / railHeight));
  return Math.max(0, Math.min(itemCount - 1, Math.round(p * (itemCount - 1))));
}

/**
 * 视口 [viewTop, viewBottom) 里的条目范围（二分查找）。rectAt(k) → {top, bottom}（与 viewTop 同一坐标系），
 * 行按文档顺序排列。视口在所有行下方（只看得到尾部的草稿 / 对话框卡片）→ {start: count, end: count}（虚拟的尾行，
 * 算最后一轮）；没有任何行与视口相交 → {start: null, end: null}。
 */
export function visibleRange(count, rectAt, viewTop, viewBottom) {
  const none = { start: null, end: null };
  if (!(count > 0) || !(viewBottom > viewTop)) return none;
  const bottomOf = (r) => Math.max(r.bottom, r.top + 1);
  let lo = 0, hi = count;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bottomOf(rectAt(mid)) > viewTop) hi = mid; else lo = mid + 1;
  }
  const start = lo;
  if (start >= count) return { start: count, end: count };
  if (!(rectAt(start).top < viewBottom)) return none;
  lo = start; hi = count;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rectAt(mid).top < viewBottom) lo = mid + 1; else hi = mid;
  }
  return { start, end: lo - 1 };
}

/** 一轮 = 本条用户消息到下一条用户消息之前；与可见范围相交就算可见（长回复整轮都亮） */
export function turnVisible(turnStart, nextTurnStart, visibleStart, visibleEnd) {
  return visibleStart != null && visibleEnd != null &&
    turnStart <= visibleEnd && (nextTurnStart == null || nextTurnStart > visibleStart);
}

/** 当前下标：视口里第一条可见的用户消息；没有就是正在读的那一轮 */
export function currentIndex({ visibleStart, visibleEnd, rowIndices }) {
  if (visibleStart == null || visibleEnd == null) return null;
  let preceding = null;
  for (let i = 0; i < rowIndices.length; i++) {
    const r = rowIndices[i];
    if (r >= visibleStart) return r <= visibleEnd ? i : preceding;
    preceding = i;
  }
  return preceding;
}

export function hasPersistentGutter(gutter) {
  return Number.isFinite(gutter) && gutter >= MM_PERSISTENT_GUTTER;
}

export function hitStripWidth(gutter) {
  if (!Number.isFinite(gutter)) return 0;
  return Math.max(0, Math.min(MM_HIT_MAX, Math.floor(gutter) - MM_HIT_LEFT));
}

export function navInteractive(hit) { return hit >= MM_NAV_REACH; }

/** 鱼眼：离悬停那一格的距离 → scaleX（CSS 里按 data-d 0/1/2 写死，这里给单测与说明用） */
export function fisheyeScale(distance) {
  if (distance == null) return 0.333;
  return distance === 0 ? 1 : distance === 1 ? 0.667 : distance === 2 ? 0.417 : 0.333;
}

/** 上一轮 / 下一轮的目标下标（null = 禁用）：最前一条没有上一轮；已滚到底没有下一轮 */
export function navTargets(current, count, atEnd) {
  const cur = current != null && current >= 0 && current < count ? current : null;
  return {
    prev: cur != null && cur > 0 ? cur - 1 : null,
    next: cur != null && !atEnd && cur + 1 < count ? cur + 1 : null,
  };
}

/** 滚轮 delta → 像素（deltaMode 0 = 像素、1 = 行（按 16px）、2 = 页（按时间线可视高度）） */
export function wheelPixels(deltaY, deltaMode, pageHeight) {
  const unit = deltaMode === 1 ? 16 : deltaMode === 2 ? pageHeight : 1;
  return deltaY * unit;
}

// ---------------------------------------------------------------- 滚轮转发（timelineMinimapWheel.ts）

const WHEEL_GLIDE_MS = 150;
const PRECISE_RATIOS = [1, 3];
const glides = new WeakMap();

function reducedMotion() {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** 触控板 / 高精度滚轮直接滚；普通鼠标滚轮（一格一跳）用 150ms 缓动 */
function isPreciseWheel(e) {
  if (e.deltaMode !== 0) return false;
  const wd = e.wheelDeltaY;
  return wd === undefined || PRECISE_RATIOS.some((r) => Math.abs(wd + r * e.deltaY) < 1);
}

function stopGlide(el) {
  const g = glides.get(el);
  if (!g) return undefined;
  cancelAnimationFrame(g.frame);
  g.stop.abort();
  glides.delete(el);
  return g;
}

function glideBy(el, distance) {
  const cur = stopGlide(el);
  const top = el.scrollTop;
  const base = cur ? cur.to + top - cur.written : top;
  const max = el.scrollHeight - el.clientHeight;
  const g = { from: top, to: Math.max(0, Math.min(max, base + distance)), start: performance.now(), written: top, frame: 0, stop: new AbortController() };
  for (const type of ['pointerdown', 'keydown']) window.addEventListener(type, () => stopGlide(el), { capture: true, signal: g.stop.signal });
  const step = (time) => {
    if (!el.isConnected) { stopGlide(el); return; }
    const drift = el.scrollTop - g.written;   // 期间别处改了 scrollTop（新内容贴底等）：整体平移
    g.from += drift; g.to += drift;
    const p = Math.min(1, Math.max(0, (time - g.start) / WHEEL_GLIDE_MS));
    el.scrollTop = g.from + (g.to - g.from) * (1 - (1 - p) ** 3);
    g.written = el.scrollTop;
    if (p < 1) g.frame = requestAnimationFrame(step); else stopGlide(el);
  };
  g.frame = requestAnimationFrame(step);
  glides.set(el, g);
}

export function forwardWheel(e, timeline) {
  if (!timeline || e.defaultPrevented || e.ctrlKey || e.deltaY === 0) return;
  e.preventDefault();
  const d = wheelPixels(e.deltaY, e.deltaMode, timeline.clientHeight);
  if (isPreciseWheel(e) || reducedMotion()) { stopGlide(timeline); timeline.scrollTop += d; return; }
  glideBy(timeline, d);
}

// 窗口外松开鼠标可能收不到 mouseup：之后第一次不带按键的 mousemove 也算松开
function onButtonsReleased(fn) {
  const check = (e) => { if (e.buttons !== 0) return; stop(); fn(); };
  const stop = () => { window.removeEventListener('mouseup', check); window.removeEventListener('mousemove', check); };
  window.addEventListener('mouseup', check);
  window.addEventListener('mousemove', check);
  return stop;
}

// ---------------------------------------------------------------- 组件

/**
 * 在 host（#chat-host）里挂一个覆盖层；setSession(s) 跟随当前会话 / 历史预览（null = 隐藏）；
 * onFlush(s) 由 render.js 的 state.onFlush 钩子调用（条目渲染 / 补渲染之后）。
 * reveal(s, item)：目标在 renderStart 之前还没渲染时先补渲染（render.js revealItem，保持滚动位置）。
 */
export class PromptMinimap {
  constructor(host, { reveal } = {}) {
    this.host = host;
    this.reveal = reveal || null;
    this.s = null;
    this.view = null;
    this.entries = [];
    this.scanArr = null;
    this.scanLen = 0;
    this.builtFor = -1;      // 已建横条对应的条目数
    this.strips = [];
    this.active = null;      // 悬停 / 键盘高亮的下标
    this.current = null;     // 当前下标（视口）
    this.atEnd = false;
    this.gutter = NaN;
    this.hit = -1;
    this.pressed = false;    // 在小格子上按下的（拖动时继续跟随）
    this.passThrough = false;// 从别处开始的拖动经过：不拦截
    this.frame = 0;
    this.kbAt = 0;

    this.prevBtn = h('button', { class: 'pmap-nav-btn', type: 'button', title: '上一轮', 'aria-label': '上一轮' }, icon('chevup'));
    this.nextBtn = h('button', { class: 'pmap-nav-btn', type: 'button', title: '下一轮', 'aria-label': '下一轮' }, icon('chevdown'));
    this.prevBtn.disabled = this.nextBtn.disabled = true;
    this.stripBox = h('span', { class: 'pmap-strips', 'aria-hidden': 'true' });
    this.btn = h('button', { class: 'pmap-hit', type: 'button', 'aria-label': '跳到消息' }, this.stripBox);
    this.cardUser = h('span', { class: 'pmap-card-user' });
    this.cardText = h('span', { class: 'pmap-card-text' });
    this.card = h('span', { class: 'pmap-card', 'aria-hidden': 'true', hidden: true }, h('span', { class: 'pmap-card-in' }, this.cardUser, this.cardText));
    this.rail = h('div', { class: 'pmap-rail' },
      h('span', { class: 'pmap-nav pmap-prev' }, this.prevBtn), this.btn, this.card, h('span', { class: 'pmap-nav pmap-next' }, this.nextBtn));
    this.root = h('div', { class: 'pmap', hidden: true, dataset: { persistent: 'false' } }, this.rail);
    host.appendChild(this.root);
    this.bind();
    this.onScroll = () => this.schedule();
    if (typeof ResizeObserver === 'function') new ResizeObserver(() => this.measure()).observe(host);
  }

  bind() {
    const { rail, btn } = this;
    this.root.addEventListener('wheel', (e) => { if (this.view) forwardWheel(e, this.view.root); }, { passive: false });
    rail.addEventListener('mousedown', () => {
      if (this.pressed) return;
      this.pressed = true;
      onButtonsReleased(() => { this.pressed = false; });
    });
    rail.addEventListener('mousemove', (e) => {
      if (e.buttons === 0 || this.pressed || this.passThrough) return;
      this.setPassThrough(true);
      onButtonsReleased(() => this.setPassThrough(false));
    });
    // 不抢输入框焦点、不开始选文字
    for (const b of [btn, this.prevBtn, this.nextBtn]) b.addEventListener('mousedown', (e) => e.preventDefault());
    btn.addEventListener('mousemove', (e) => {
      if (e.buttons !== 0 && !this.pressed) return;
      this.setActive(this.fromPointer(e));
    });
    btn.addEventListener('mouseleave', () => this.setActive(null));
    btn.addEventListener('click', (e) => {
      let i;
      if (e.detail === 0) {   // 键盘 / 辅助技术触发的 click
        if (performance.now() - this.kbAt < 500) return;   // 已在 keydown 里处理
        i = this.active ?? this.current;
      } else i = this.fromPointer(e);
      if (i != null) this.select(i);
      btn.blur();
    });
    btn.addEventListener('focus', () => { if (this.active == null) this.setActive(this.current ?? 0); });
    btn.addEventListener('blur', () => this.setActive(null));
    btn.addEventListener('keydown', (e) => {
      const n = this.entries.length;
      if (!n || e.altKey || e.ctrlKey || e.metaKey) return;
      const base = this.active ?? 0;
      if (e.key === 'ArrowDown') this.setActive(Math.min(n - 1, base + 1));
      else if (e.key === 'ArrowUp') this.setActive(Math.max(0, base - 1));
      else if (e.key === 'Home') this.setActive(0);
      else if (e.key === 'End') this.setActive(n - 1);
      else if (e.key === 'Enter' || e.key === ' ') { this.kbAt = performance.now(); if (this.active != null) this.select(this.active); }
      else return;
      e.preventDefault();
    });
    this.prevBtn.addEventListener('click', () => { const t = navTargets(this.current, this.entries.length, this.atEnd).prev; if (t != null) this.select(t); });
    this.nextBtn.addEventListener('click', () => { const t = navTargets(this.current, this.entries.length, this.atEnd).next; if (t != null) this.select(t); });
  }

  /** 跟随当前会话 / 历史预览（null = 隐藏）。同一个会话、同一个视图时只做增量同步 */
  setSession(s) {
    const v = (s && s.view) || null;
    if (s === this.s && v === this.view) { this.sync(); return; }
    if (this.view) this.view.root.removeEventListener('scroll', this.onScroll);
    this.s = s || null;
    this.view = v;
    this.scanArr = null;
    this.builtFor = -1;
    this.active = null;
    this.current = null;
    if (v) v.root.addEventListener('scroll', this.onScroll, { passive: true });
    this.sync(true);
  }

  /** render.js 的 state.onFlush：条目刷新 / 补渲染之后 */
  onFlush(s) { if (s && s === this.s) this.sync(); }

  shown() {
    const v = this.view;
    return !!(v && v.root.isConnected && !v.root.hidden && this.entries.length >= MM_MIN_ITEMS);
  }

  sync(remeasure) {
    const s = this.s;
    if (s && (this.scanArr !== s.items || this.scanLen > s.items.length)) { this.scanArr = s.items; this.scanLen = 0; this.entries = []; }
    if (s) { minimapEntries(s.items, this.scanLen, this.entries); this.scanLen = s.items.length; }
    else this.entries = [];
    const show = this.shown();
    const wasHidden = this.root.hidden;
    if (wasHidden !== !show) this.root.hidden = !show;
    if (!show) { this.setActive(null); return; }
    if (this.builtFor !== this.entries.length) this.build();
    if (remeasure || wasHidden) this.measure();
    this.schedule();
  }

  build() {
    const n = this.entries.length;
    this.builtFor = n;
    this.active = null;
    this.card.hidden = true;
    this.strips = this.entries.map((_, i) => h('span', { class: 'pmap-strip', dataset: { inView: 'false' }, style: { top: topPercent(i, n) + '%' } }));
    this.stripBox.replaceChildren(...this.strips);
    this.rail.style.height = heightStyle(n);
  }

  /** 左侧留白 = #chat-host 左边到 .chat-col 内容起点（含 col 的左内边距：那里本来就空着） */
  measure() {
    const v = this.view;
    if (!v || this.root.hidden || !v.root.isConnected) return;
    const hr = this.host.getBoundingClientRect();
    const cr = v.col.getBoundingClientRect();
    if (!hr.width || !cr.width) return;
    const pad = parseFloat(getComputedStyle(v.col).paddingLeft) || 0;
    const gutter = cr.left + pad - hr.left;
    const hit = hitStripWidth(gutter);
    const persistent = hasPersistentGutter(gutter) ? 'true' : 'false';
    if (this.root.dataset.persistent !== persistent) this.root.dataset.persistent = persistent;
    if (hit !== this.hit) {
      this.hit = hit;
      this.rail.style.width = hit + 'px';
      this.rail.classList.toggle('off', hit <= 0);
      this.rail.classList.toggle('nav-off', !navInteractive(hit));
    }
    this.gutter = gutter;
    const cardW = Math.max(160, Math.min(MM_CARD_W, Math.floor(hr.width - MM_HIT_LEFT - MM_CARD_LEFT - 12))) + 'px';
    if (this.card.style.width !== cardW) this.card.style.width = cardW;
    this.schedule();
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.refresh(); });
  }

  /** 滚动 / 内容变化后：可见的轮次加深（值不变不写 DOM）、当前下标、是否在底部、导航按钮 */
  refresh() {
    const s = this.s, v = this.view, n = this.entries.length;
    if (!s || !v || this.root.hidden || this.strips.length !== n) return;
    const root = v.root;
    const rr = root.getBoundingClientRect();
    const first = s.renderStart || 0;
    const count = Math.max(0, Math.min(s.renderedTo ?? s.items.length, s.items.length) - first);
    // 隐藏（display:none）的条目没有位置：用其后第一条有位置的代替，保持单调
    const rectAt = (k) => {
      for (let j = k; j < count; j++) {
        const el = s.items[first + j] && s.items[first + j].el;
        if (!el || !el.isConnected) continue;
        const r = el.getBoundingClientRect();
        if (!r.width && !r.height) continue;
        return { top: r.top - rr.top, bottom: r.bottom - rr.top };
      }
      return { top: Infinity, bottom: Infinity };
    };
    const abs = (r) => (r.start == null ? r : { start: r.start + first, end: r.end + first });
    const vis = abs(visibleRange(count, rectAt, 0, root.clientHeight));
    for (let i = 0; i < n; i++) {
      const next = i + 1 < n ? this.entries[i + 1].index : null;
      const val = turnVisible(this.entries[i].index, next, vis.start, vis.end) ? 'true' : 'false';
      if (this.strips[i].dataset.inView !== val) this.strips[i].dataset.inView = val;
    }
    // 跳转把目标放在视口顶端下方 24px：顶端这 25px 里结束的条目不算，否则「下一轮」会一直指向同一条
    const inset = MM_TARGET_OFFSET + 1;
    const cr = abs(visibleRange(count, rectAt, inset, root.clientHeight));
    this.current = currentIndex({ visibleStart: cr.start, visibleEnd: cr.end, rowIndices: this.entries.map((e) => e.index) });
    this.atEnd = root.scrollHeight - root.scrollTop - root.clientHeight <= 1;
    const t = navTargets(this.current, n, this.atEnd);
    if (this.prevBtn.disabled !== (t.prev == null)) this.prevBtn.disabled = t.prev == null;
    if (this.nextBtn.disabled !== (t.next == null)) this.nextBtn.disabled = t.next == null;
  }

  fromPointer(e) {
    const r = this.btn.getBoundingClientRect();
    return indexFromPointer({ itemCount: this.entries.length, railTop: r.top, railHeight: r.height, pointerY: e.clientY });
  }

  setPassThrough(on) {
    if (this.passThrough === on) return;
    this.passThrough = on;
    this.rail.classList.toggle('pass', on);
    if (on) this.setActive(null);
  }

  /** 鱼眼 + 预览卡。只改高亮附近几格的 data-d（0 / 1 / 2；更远不写 = .333） */
  setActive(i) {
    const n = this.strips.length;
    if (i != null && !(i >= 0 && i < n)) i = null;
    const old = this.active;
    if (i === old) return;
    this.active = i;
    const touch = new Set();
    for (const c of [old, i]) if (c != null) for (let k = Math.max(0, c - 2); k <= Math.min(n - 1, c + 2); k++) touch.add(k);
    for (const k of touch) {
      const d = i == null ? null : Math.abs(k - i);
      const val = d != null && d <= 2 ? String(d) : null;
      const el = this.strips[k];
      if (val == null) { if (el.hasAttribute('data-d')) el.removeAttribute('data-d'); } else if (el.dataset.d !== val) el.dataset.d = val;
    }
    if (i == null) { this.card.hidden = true; this.btn.setAttribute('aria-label', '跳到消息'); return; }
    const p = turnPreview(this.s ? this.s.items : [], this.entries[i], i + 1 < this.entries.length ? this.entries[i + 1].index : null);
    const user = p.user || '用户消息';
    this.cardUser.textContent = user;
    this.cardText.textContent = p.assistant || '';
    this.cardText.hidden = !p.assistant;
    this.card.style.top = topPercent(i, n) + '%';
    this.card.hidden = false;
    this.btn.setAttribute('aria-label', '跳到消息：' + user);
  }

  /** 跳到第 i 条用户消息：必要时先补渲染，再把它的顶端滚到视口顶端下方 24px（平滑；减弱动效时瞬移） */
  select(i) {
    const s = this.s, v = this.view, e = this.entries[i];
    if (!s || !v || !e) return;
    if (this.reveal) this.reveal(s, e.item);
    const el = e.item.el;
    if (!el || !el.isConnected) return;
    const root = v.root;
    stopGlide(root);
    const max = Math.max(0, root.scrollHeight - root.clientHeight);
    const top = el.getBoundingClientRect().top - root.getBoundingClientRect().top + root.scrollTop - MM_TARGET_OFFSET;
    const target = Math.max(0, Math.min(max, Math.round(top)));
    // 离开底部：先放开贴底（平滑滚动的第一帧之前若有新内容刷新，不会被拉回底部）；到底部时滚动事件会重新贴底
    v.stick = max - target < 60;
    if (reducedMotion()) root.scrollTop = target;
    else root.scrollTo({ top: target, behavior: 'smooth' });
    this.schedule();
  }
}
