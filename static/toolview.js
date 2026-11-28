// 工具调用显示档位（docs/SPEC_V2_TUI.md §7「工具调用显示档位」；用户需求）：全部 / 隐藏成功的 / 全部隐藏 / 只看对话。
// 纯前端：时间线根节点（.chat-scroll）上写 data-tools，CSS 按 render.js 已有的条目 class（k-tool / st-done / k-thinking…）
// 隐藏；「只看对话」每轮只留 Claude 最后一段回复（条目上的 data-zen=keep，重渲染也不会被 className 冲掉）。
// 档位存 localStorage「ccr.tools」（全局，对所有会话与历史预览生效；子代理抽屉不受影响）。
// 切换时保持阅读位置：WebKit 没有滚动锚定，先记下视口里第一条切换后仍显示的条目离视口顶端的距离，切换后把 scrollTop 补回去；
// 原本贴底就继续贴底。查找（finder.js）命中被隐藏的条目时临时显示那一条（data-find-show）。
//
// 参考 T3 Code（https://github.com/pingdotgg/t3code，MIT License，Copyright (c) 2026 T3 Tools Inc.）#30000
//   「a Labs setting hides tool calls so threads read like a chat」：四档 Show all / Hide successful / Hide all / Zen 的含义
//   （隐藏成功的 = 运行中的照常显示、失败的留下；Zen = 每轮只有你的消息和 Claude 最后的回复，回复中途发的消息之前那段回复也留下；
//   各档都保留审批 / 提问 / 错误 / 压缩分隔线）。
// 与 T3 的差异：用 CSS 隐藏已渲染的条目（不重建列表、不改 renderStart）；没有「Worked for」折叠行；入口在会话头而不是设置页。

import { h, icon } from './dom.js';

export const TOOLS_KEY = 'ccr.tools';
export const TIERS = [
  { id: 'all', name: '全部', short: '工具', desc: '显示所有工具调用（默认）' },
  { id: 'hideok', name: '隐藏成功的', short: '隐藏成功', desc: '只隐藏已成功完成的工具调用；运行中（含等待审批）、失败、被拒绝、已中断的照常显示' },
  { id: 'hideall', name: '全部隐藏', short: '隐藏工具', desc: '不显示工具调用；Claude 的中间说明、思考、本轮改动卡片照常显示' },
  { id: 'zen', name: '只看对话', short: '只看对话', desc: '每轮只显示你的消息和 Claude 最后一段回复；中断、错误、上下文压缩提示仍显示' },
];

// ---------------------------------------------------------------- 纯函数

export function normTier(v) { return TIERS.some((t) => t.id === v) ? v : 'all'; }
export function tierInfo(id) { return TIERS.find((t) => t.id === normTier(id)); }

/** 工具卡的显示状态（与 render.js toolStatus 一致；Agent 卡由调用方按子代理状态给出 st） */
export function toolState(t) {
  if (!t) return 'running';
  if (t.denied) return 'denied';
  if (t.done) return t.isError ? 'error' : 'done';
  if (t.interrupted) return 'interrupted';
  return 'running';
}

/** 「只看对话」要留下的回复：每条用户消息（含回复中途发的）之前、以及最后一轮的最后一段 Claude 文字。返回 Set<item> */
export function zenKeep(items) {
  const keep = new Set();
  let last = null;
  for (const it of items || []) {
    if (!it) continue;
    if (it.kind === 'user') { if (last) keep.add(last); last = null; }
    else if (it.kind === 'text') last = it;
  }
  if (last) keep.add(last);
  return keep;
}

/**
 * 条目在某一档里是否显示（纯逻辑，与 CSS 规则一一对应；回放页逐条核对）。
 * st = 工具卡当前的显示状态（默认按 toolState 推）；keep = zenKeep 的结果
 */
export function itemShown(item, tier, { st, keep } = {}) {
  if (!item) return false;
  const t = normTier(tier);
  if (t === 'all') return true;
  const k = item.kind;
  if (t === 'hideok') return !(k === 'tool' && (st || toolState(item)) === 'done');
  if (t === 'hideall') return k !== 'tool';
  // 只看对话
  if (k === 'user' || k === 'notice' || k === 'divider') return true;
  if (k === 'text') return !!item.apiError || !!(keep && keep.has(item));
  if (k === 'hint') return item.level === 'error' || item.level === 'warn';
  return false;
}

/** 隐藏掉的工具调用数（会话头按钮的提示用） */
export function hiddenToolCount(items, tier, stOf) {
  let n = 0;
  for (const it of items || []) if (it && it.kind === 'tool' && !itemShown(it, tier, { st: stOf ? stOf(it) : undefined })) n++;
  return n;
}

/**
 * 切换前后保持阅读位置：cands = 视口附近已渲染条目（按时间线顺序）[{top, bottom, keep}]（top/bottom 相对视口顶端，keep = 切换后仍显示）。
 * 返回锚点下标：第一个切换后仍显示、且底边在视口顶端之下的条目（视口里一个都不剩时，用其后第一个仍显示的；再没有就用之前最后一个）。-1 = 没有
 */
export function pickAnchor(cands) {
  const list = cands || [];
  for (let i = 0; i < list.length; i++) if (list[i] && list[i].keep && list[i].bottom > 0) return i;
  for (let i = list.length - 1; i >= 0; i--) if (list[i] && list[i].keep) return i;
  return -1;
}

// ---------------------------------------------------------------- 存储

function store() { try { return window.localStorage; } catch (_) { return null; } }
export function loadTier() { const ls = store(); try { return normTier(ls && ls.getItem(TOOLS_KEY)); } catch (_) { return 'all'; } }
export function saveTier(id) {
  const ls = store();
  try { if (ls) { if (normTier(id) === 'all') ls.removeItem(TOOLS_KEY); else ls.setItem(TOOLS_KEY, normTier(id)); } } catch (_) { /* 只在本页生效 */ }
}

// ---------------------------------------------------------------- DOM

/** 已渲染工具卡的状态：渲染时 render.js 写在 class 上（st-done / st-running…，Agent 卡已按子代理状态换算） */
export function renderedState(it) {
  const el = it && it.el;
  if (el) for (const c of el.classList) if (c.startsWith('st-')) return c.slice(3);
  return toolState(it);
}

/** 视口附近的已渲染条目（二分找到视口顶端附近，再向下取到视口底端以下一屏） */
function viewportItems(s) {
  const v = s.view, root = v.root;
  const rr = root.getBoundingClientRect();
  const H = root.clientHeight;
  const out = [];
  const end = Math.min(s.renderedTo ?? s.items.length, s.items.length);
  for (let k = s.renderStart || 0; k < end; k++) {
    const it = s.items[k];
    const el = it && it.el;
    if (!el || !el.isConnected) continue;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) { out.push({ it, top: NaN, bottom: NaN, hidden: true }); continue; }
    const top = r.top - rr.top, bottom = r.bottom - rr.top;
    if (bottom < -H) continue;
    out.push({ it, top, bottom });
    if (top > 2 * H) break;
  }
  return out;
}

/**
 * 把 fn()（改档位 / 宽度 / 临时显示某条）前后的阅读位置保持住。stillShown(item) = 改完以后这一条是否显示。
 * 贴底（view.stick）的会话改完继续贴底。
 */
export function keepReading(s, fn, stillShown) {
  const v = s && s.view;
  if (!v || !v.root.isConnected) { fn(); return; }
  const root = v.root;
  if (v.stick) { fn(); root.scrollTop = root.scrollHeight; return; }
  const near = viewportItems(s).filter((c) => !c.hidden);
  const k = pickAnchor(near.map((c) => ({ top: c.top, bottom: c.bottom, keep: stillShown ? stillShown(c.it) : true })));
  const a = k >= 0 ? near[k] : null;
  fn();
  if (!a || !a.it.el || !a.it.el.isConnected) return;
  const r = a.it.el.getBoundingClientRect();
  if (!r.width && !r.height) return;
  const now = r.top - root.getBoundingClientRect().top;
  const d = now - a.top;
  if (Math.abs(d) >= 1) root.scrollTop += d;
}

/**
 * 档位控制：会话头的一个按钮（当前档位；非「全部」时高亮）+ 点开的菜单（四档单选）。
 * apply(s) 把当前档位写到会话视图上（renderAll / onFlush 调用：「只看对话」的 keep 标记按条目增量维护）。
 */
export class ToolTiers {
  constructor({ onChange, persist = true, initial } = {}) {
    this.persist = persist;   // false：只在本页生效、不读写 localStorage（回放页用）
    this.tier = initial != null ? normTier(initial) : persist ? loadTier() : 'all';
    this.onChange = onChange || null;
    this.s = null;
    this.label = h('span', null, '');
    this.el = h('button', { class: 'btn ghost small tier-btn', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon('eye'), this.label);
    this.el.addEventListener('click', () => (this.menu ? this.closeMenu() : this.openMenu()));
    this.menu = null;
    this.sync();
    if (persist) window.addEventListener('storage', (e) => { if (e.key === TOOLS_KEY) this.set(loadTier(), { persist: false }); });
  }

  sync() {
    const t = tierInfo(this.tier);
    this.label.textContent = t.id === 'all' ? '工具' : t.short;
    this.el.classList.toggle('on', t.id !== 'all');
    this.el.dataset.tier = t.id;
    this.el.title = '工具调用显示：' + t.name + '（' + t.desc + '）· 点击切换';
  }

  /** 跟随当前会话 / 历史预览（null = 没有） */
  setSession(s) { this.s = s && s.view ? s : null; if (this.s) this.apply(this.s); }

  /** 写到视图上：data-tools + 「只看对话」的 keep 标记（值不变不写 DOM） */
  apply(s) {
    const v = s && s.view;
    if (!v) return;
    const t = this.tier;
    if ((v.root.dataset.tools || 'all') !== t) { if (t === 'all') delete v.root.dataset.tools; else v.root.dataset.tools = t; }
    if (t === 'zen') markZen(s);
  }

  onFlush(s) { if (s && s.view && this.tier === 'zen') markZen(s); }

  /** 切换档位（保持阅读位置；persist=false 只在本页生效，回放页用） */
  set(id, { persist = this.persist } = {}) {
    const t = normTier(id);
    if (persist) saveTier(t);
    if (t === this.tier) { this.sync(); return; }
    const s = this.s;
    const keep = t === 'zen' && s ? zenKeep(s.items) : null;
    const apply = () => { this.tier = t; this.sync(); if (s) this.apply(s); };
    if (s) keepReading(s, apply, (it) => it.el && it.el.dataset.findShow ? true : itemShown(it, t, { st: renderedState(it), keep }));
    else apply();
    if (this.onChange) this.onChange(t);
  }

  openMenu() {
    this.closeMenu();
    const rows = TIERS.map((t) => {
      const cur = t.id === this.tier;
      const b = h('button', { class: 'mode-row tier-row' + (cur ? ' on' : ''), type: 'button', role: 'menuitemradio', 'aria-checked': String(cur), dataset: { tier: t.id } },
        h('span', { class: 'mode-check' }, cur ? icon('check') : null),
        h('span', { class: 'mode-main' }, h('span', { class: 'mode-name' }, t.name), h('span', { class: 'mode-desc' }, t.desc)));
      b.addEventListener('click', () => { this.closeMenu(); this.set(t.id); });
      return b;
    });
    const pop = h('div', { class: 'popover mode-menu tier-menu', role: 'menu', 'aria-label': '工具调用显示' },
      h('div', { class: 'pop-head' }, '工具调用显示'),
      h('div', { class: 'mode-list' }, rows),
      h('div', { class: 'mode-foot' }, h('span', { class: 'muted small-text' }, '只影响这里的显示（所有会话），不改 Claude Code 的任何设置；查找时命中被隐藏的条目会临时显示。')));
    document.body.appendChild(pop);
    const r = this.el.getBoundingClientRect();
    const w = pop.offsetWidth;
    pop.style.left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) + 'px';
    pop.style.top = (r.bottom + 6) + 'px';
    const dismiss = (e) => {
      if (e && e.type === 'mousedown' && (pop.contains(e.target) || this.el.contains(e.target))) return;
      if (e && e.type === 'keydown' && e.key !== 'Escape') return;
      if (e && e.type === 'keydown') e.preventDefault();   // Esc 只关这个菜单（子代理抽屉等看到 defaultPrevented 就不再处理）
      const refocus = pop.contains(document.activeElement);
      this.closeMenu();
      if (refocus) this.el.focus({ preventScroll: true });
    };
    document.addEventListener('mousedown', dismiss, true);
    document.addEventListener('keydown', dismiss, true);
    this.menu = { pop, dismiss };
    this.el.setAttribute('aria-expanded', 'true');
    const cur = pop.querySelector('.tier-row.on');
    if (cur) cur.focus({ preventScroll: true });
  }

  closeMenu() {
    const m = this.menu;
    if (!m) return;
    this.menu = null;
    m.pop.remove();
    document.removeEventListener('mousedown', m.dismiss, true);
    document.removeEventListener('keydown', m.dismiss, true);
    this.el.setAttribute('aria-expanded', 'false');
  }
}

/** 「只看对话」：每轮最后一段回复写 data-zen=keep（其它文字段去掉）；只写已渲染的条目，值不变不写 */
export function markZen(s) {
  const keep = zenKeep(s.items);
  for (const it of s.items) {
    if (!it || it.kind !== 'text' || !it.el) continue;
    const want = keep.has(it) ? 'keep' : '';
    if ((it.el.dataset.zen || '') !== want) { if (want) it.el.dataset.zen = want; else delete it.el.dataset.zen; }
  }
}
