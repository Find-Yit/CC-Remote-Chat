// 侧栏：项目与聊天（docs/SPEC_V2_TUI.md §15.2；用户需求「左边这一栏可以做成 project，每个文件夹路径算一个 project」
// 「project 中可以增加聊天，就是在 project 中增加。然后开启新聊天，就是不在 project 中显示」）。
// 纯函数（app.js 与回放自测 static/dev/fixture.js 共用；tests/test_projects.py 用 node 跑）+ 项目标题行 / 分区标题的 DOM。
//
// - 归属是显式的：meta.in_project（后端持久化；项目的「+」/「新建项目」= true，侧栏顶部「新建聊天」= false；
//   旧记录没有这个字段 = true）。项目 = 运行位置（本机 / 服务器）+ 规范化后的 cwd，只由 in_project 的会话组成；
//   服务器上同一路径、不同作业号算同一个项目（文件系统共享）。in_project=false 的会话在「聊天」区（不分组）。
// - 顺序：会话的全局顺序不变（meta.order，小的在上；没排过的 = 新建的，按创建时间、新的在最上）。项目的位置 = 组内排过序的
//   会话里最靠前那个的位置；组内全是没排过的（新项目）→ 排在所有项目最前面（多个时按项目里最早的会话、新建的项目在上）。
//   所以在已有项目里新建会话只出现在该项目的最上面，项目本身不挪位置（从没排过序时也一样）。
//   任何一次调整顺序都把「各项目依次 + 聊天」压平成新的全局顺序，再照旧 POST /api/sessions/order。
// - 拖动 / 移动只在同一个项目（或聊天区）内；项目整体移动。改归属只走右键菜单的「移出项目 / 移入项目」。
//
// 项目标题行（chevron、名称、数量、需要处理时的状态点、悬停才出现的「+」）参考 T3 Code 的 SidebarProjectFolderHeader，
// 聚合状态的优先级同它的 resolveSidebarFolderDot（需要你 > 新回复 > 在干活）。
// 参考自 T3 Code（https://github.com/pingdotgg/t3code，MIT License，Copyright (c) 2026 T3 Tools Inc.）。
import { h, icon } from './dom.js';

export const LOCAL_JOBID = 'local';
export const UNKNOWN_KEY = '?';           // 没有 cwd 的会话（理论上不会出现）归入这一组
export const CHAT_KEY = '#chat';          // 「聊天」区在拖动 / 移动里的分组 key
export const COLLAPSE_KEY = 'ccr.projCollapsed';
export const HOMES_KEY = 'ccr.projHomes';
const COLLAPSE_MAX = 300;

/** 运行位置：'local' | 'server' */
export function targetOf(meta) { return meta && meta.jobid === LOCAL_JOBID ? 'local' : 'server'; }
export function placeLabel(target) { return target === 'local' ? '本机' : target === 'server' ? '服务器' : ''; }
/** 是否在项目里（缺字段 = 旧后端 / 旧记录 = 在项目里） */
export function inProject(meta) { return !meta || meta.in_project !== false; }

function cleanHome(home) {
  const p = typeof home === 'string' ? home.trim().replace(/\/{2,}/g, '/') : '';
  if (!p.startsWith('/')) return '';
  return p.length > 1 ? p.replace(/\/+$/, '') : p;
}

/**
 * 规范化 cwd：去掉首尾空白、重复的 / 与末尾的 /（根目录 / 保留）；以 ~ 开头且知道这台机器的 home 时展开，
 * 这样 claude 启动前的 "~/x" 与启动后 CLI 报告的 "/Users/u/x" 是同一个项目。不是路径 → ''。
 */
export function normPath(cwd, home = '') {
  let p = typeof cwd === 'string' ? cwd.trim() : '';
  if (!(p.startsWith('/') || p === '~' || p.startsWith('~/'))) return '';
  p = p.replace(/\/{2,}/g, '/');
  if (p.length > 1) p = p.replace(/\/+$/, '') || '/';
  if (p === '~' || p.startsWith('~/')) {
    const hm = cleanHome(home);
    if (hm) p = hm === '/' ? (p.slice(1) || '/') : hm + p.slice(1);
  }
  return p;
}

/** 项目 key：'local:/abs' | 'server:/abs'（作业号不参与）；没有 cwd → UNKNOWN_KEY */
export function projectKey(meta, homes = {}) {
  const t = targetOf(meta);
  const p = normPath(meta && meta.cwd, homes && homes[t]);
  return p ? t + ':' + p : UNKNOWN_KEY;
}

function segsOf(path) { return String(path || '').split('/').filter(Boolean); }

/** 显示名：文件夹名；home → "~"，根目录 → "/"，没有 cwd → "未知目录" */
export function projectName(path, home = '') {
  if (!path) return '未知目录';
  const hm = cleanHome(home);
  if (path === '~' || (hm && path === hm)) return '~';
  if (path === '/') return '/';
  const s = segsOf(path);
  return s[s.length - 1] || path;
}

/** 同一运行位置下文件夹名相同的项目：往上多带几级目录区分（a/src、b/src） */
function disambiguate(groups) {
  const by = new Map();
  for (const g of groups) {
    const k = g.target + '\u0000' + g.name;
    if (!by.has(k)) by.set(k, []);
    by.get(k).push(g);
  }
  for (const list of by.values()) {
    if (list.length < 2 || list.some((g) => !g.path || g.name === '~' || g.name === '/')) continue;
    for (let d = 2; d <= 8; d++) {
      const names = list.map((g) => { const s = segsOf(g.path); return s.slice(-d).join('/'); });
      if (new Set(names).size === names.length || d === 8) { list.forEach((g, i) => { g.name = names[i]; }); break; }
    }
  }
}

/** 全局顺序：手动排过的按 meta.order（小的在上）；没排过的按创建时间、新的在最上，并排在所有排过的前面 */
export function orderKey(meta) {
  const o = meta && meta.order;
  return typeof o === 'number' && Number.isFinite(o) ? o : -(Number(meta && meta.created_at) || 0);
}
/** items: [{sid, meta}] 的比较函数（与以前 app.js 的 sortedSessions 完全相同） */
export function compareSessions(a, b) {
  return orderKey(a.meta) - orderKey(b.meta) || (Number(b.meta && b.meta.created_at) || 0) - (Number(a.meta && a.meta.created_at) || 0)
    || String(a.sid).localeCompare(String(b.sid));
}
function hasOrder(meta) { const o = meta && meta.order; return typeof o === 'number' && Number.isFinite(o); }

/**
 * 侧栏布局：{ projects: [{key, target, path, name, home, sids, jobid}], chats: [sid] }。
 * items = [{sid, meta}]（任意顺序）；homes = {local, server}（已知的 home，用于展开 ~）。
 */
export function buildLayout(items, { homes = {} } = {}) {
  const sorted = [...(items || [])].filter((x) => x && x.sid).sort(compareSessions);
  const groups = new Map();
  const chats = [];
  for (const it of sorted) {
    const m = it.meta || {};
    if (!inProject(m)) { chats.push(it.sid); continue; }
    const key = projectKey(m, homes);
    let g = groups.get(key);
    if (!g) {
      const target = key === UNKNOWN_KEY ? '' : targetOf(m);
      const path = key === UNKNOWN_KEY ? '' : key.slice(key.indexOf(':') + 1);
      g = { key, target, path, home: cleanHome(homes && homes[target]), name: '', sids: [], jobid: '', first: Infinity, newest: -Infinity, oldest: Infinity };
      groups.set(key, g);
    }
    g.sids.push(it.sid);
    if (hasOrder(m)) g.first = Math.min(g.first, m.order);
    // 服务器项目「+」预填的作业号（没有设置当前作业时）：组里最新建的那个会话的作业
    const c = Number(m.created_at) || 0;
    const srvJob = m.jobid && m.jobid !== LOCAL_JOBID ? String(m.jobid) : '';
    if (c > g.newest) { g.newest = c; if (srvJob) g.jobid = srvJob; } else if (!g.jobid && srvJob) g.jobid = srvJob;
    g.oldest = Math.min(g.oldest, c);
  }
  const projects = [...groups.values()].sort((a, b) => {
    const ua = a.first === Infinity, ub = b.first === Infinity;
    if (ua !== ub) return ua ? -1 : 1;
    // 都没排过：按项目「建立」的时间（组里最早的会话），新项目在上——往已有项目里加会话不会让它挪位置
    return (ua ? b.oldest - a.oldest : a.first - b.first) || a.key.localeCompare(b.key);
  });
  for (const g of projects) g.name = projectName(g.path, g.home);
  disambiguate(projects);
  for (const g of projects) { delete g.first; delete g.newest; delete g.oldest; }
  return { projects, chats };
}

/** 压平成全局顺序：各项目依次，然后是聊天区（over = {分组 key: 新的会话列表}） */
export function flatten(layout, over = {}) {
  const out = [];
  for (const g of layout.projects) out.push(...(over[g.key] || g.sids));
  out.push(...(over[CHAT_KEY] || layout.chats));
  return out;
}

/** 会话在哪：{key（项目 key 或 CHAT_KEY）, list（该组的会话）, index}；找不到 → null */
export function locate(layout, sid) {
  for (const g of layout.projects) {
    const i = g.sids.indexOf(sid);
    if (i >= 0) return { key: g.key, list: g.sids, index: i, project: g };
  }
  const i = layout.chats.indexOf(sid);
  return i >= 0 ? { key: CHAT_KEY, list: layout.chats, index: i, project: null } : null;
}

export function projectOf(layout, key) { return layout.projects.find((g) => g.key === key) || null; }

function same(a, b) { return a.length === b.length && a.every((x, i) => x === b[i]); }
function withList(layout, key, list, old) { return same(list, old) ? null : flatten(layout, { [key]: list }); }

/** 拖动会话到 targetSid 之前 / 之后：只在同一个项目（或聊天区）内；跨组、没变化 → null（不调整） */
export function moveSession(layout, sid, targetSid, after) {
  const a = locate(layout, sid), b = locate(layout, targetSid);
  if (!a || !b || a.key !== b.key || sid === targetSid) return null;
  const list = a.list.filter((x) => x !== sid);
  let i = list.indexOf(targetSid);
  if (after) i += 1;
  list.splice(i, 0, sid);
  return withList(layout, a.key, list, a.list);
}
/** 移到所在组的最上 / 最下 */
export function moveSessionEdge(layout, sid, where) {
  const a = locate(layout, sid);
  if (!a) return null;
  const list = a.list.filter((x) => x !== sid);
  if (where === 'top') list.unshift(sid); else list.push(sid);
  return withList(layout, a.key, list, a.list);
}
/** 在所在组内上移 / 下移 delta 格（到头了 → null） */
export function moveSessionStep(layout, sid, delta) {
  const a = locate(layout, sid);
  if (!a) return null;
  const j = a.index + delta;
  if (j < 0 || j >= a.list.length) return null;
  const list = a.list.slice();
  list.splice(a.index, 1);
  list.splice(j, 0, sid);
  return withList(layout, a.key, list, a.list);
}
/** 拖动 sid 经过 overKey 这一组时能不能放：只能放在自己的组里 */
export function canDropSession(layout, sid, overKey) {
  const a = locate(layout, sid);
  return !!a && a.key === overKey;
}

function projectsFlat(layout, order) {
  const by = new Map(layout.projects.map((g) => [g.key, g]));
  return flatten({ projects: order.map((k) => by.get(k)), chats: layout.chats });
}
/** 项目整体移到 targetKey 之前 / 之后（组内会话顺序不变） */
export function moveProject(layout, key, targetKey, after) {
  const keys = layout.projects.map((g) => g.key);
  if (key === targetKey || !keys.includes(key) || !keys.includes(targetKey)) return null;
  const order = keys.filter((k) => k !== key);
  let i = order.indexOf(targetKey);
  if (after) i += 1;
  order.splice(i, 0, key);
  return same(order, keys) ? null : projectsFlat(layout, order);
}
export function moveProjectEdge(layout, key, where) {
  const keys = layout.projects.map((g) => g.key);
  if (!keys.includes(key)) return null;
  const order = keys.filter((k) => k !== key);
  if (where === 'top') order.unshift(key); else order.push(key);
  return same(order, keys) ? null : projectsFlat(layout, order);
}
export function moveProjectStep(layout, key, delta) {
  const keys = layout.projects.map((g) => g.key);
  const i = keys.indexOf(key);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= keys.length) return null;
  const order = keys.slice();
  order.splice(i, 1);
  order.splice(j, 0, key);
  return projectsFlat(layout, order);
}

/**
 * 项目的聚合状态（statuses = 组内各会话的 sessionStatus 结果 [类名后缀, 中文]）：
 * top = 'pending'（待处理 / 待信任目录）> 'unread'（新回复）> 'busy'（运行中 / 子代理运行中）> 'error' > null
 */
export function aggregate(statuses) {
  const c = { n: 0, pending: 0, unread: 0, busy: 0, error: 0, top: null };
  for (const st of statuses || []) {
    c.n++;
    const k = st && st[0];
    if (k === 'pending' || k === 'unread' || k === 'busy' || k === 'error') c[k]++;
  }
  c.top = c.pending ? 'pending' : c.unread ? 'unread' : c.busy ? 'busy' : c.error ? 'error' : null;
  return c;
}
export function aggregateText(c, sep = ' · ') {
  if (!c) return '';
  const out = [];
  if (c.pending) out.push(c.pending + ' 个待处理');
  if (c.unread) out.push(c.unread + ' 个新回复');
  if (c.busy) out.push(c.busy + ' 个运行中');
  if (c.error) out.push(c.error + ' 个出错');
  return out.join(sep);
}

/** 标题行 class：折叠时把组内最要紧的状态画在标题行上（浅黄 = 待处理、浅红 = 新回复，与会话行同一套颜色）；
 *  折叠着且当前会话在里面 → active（选中底色，与会话行一致） */
export function headerClass(agg, collapsed, hasActive) {
  let c = 'proj-hd';
  if (collapsed && agg && agg.top === 'pending') c += ' attn';
  else if (collapsed && agg && agg.top === 'unread') c += ' unread';
  if (collapsed && hasActive) c += ' active';
  return c;
}

// ---------------------------------------------------------------- 本页记住的状态（localStorage，按项目 key）

function readJSON(storage, key, dflt) {
  try { const v = JSON.parse(storage && storage.getItem(key)); return v == null ? dflt : v; } catch (_) { return dflt; }
}
function writeJSON(storage, key, v) { try { if (storage) storage.setItem(key, JSON.stringify(v)); } catch (_) { /* 忽略：隐私模式等 */ } }

/** 折叠状态：localStorage[COLLAPSE_KEY] = 折叠着的项目 key（最多 COLLAPSE_MAX 个，最近改过的在后） */
export class CollapseStore {
  constructor(storage, key = COLLAPSE_KEY) {
    this.storage = storage;
    this.key = key;
    const v = readJSON(storage, key, []);
    this.set_ = new Set(Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);
  }
  has(k) { return this.set_.has(k); }
  /** 返回是否有变化 */
  set(k, collapsed) {
    if (typeof k !== 'string' || this.set_.has(k) === !!collapsed) return false;
    if (collapsed) this.set_.add(k); else this.set_.delete(k);
    const arr = [...this.set_];
    if (arr.length > COLLAPSE_MAX) { this.set_ = new Set(arr.slice(-COLLAPSE_MAX)); }
    writeJSON(this.storage, this.key, [...this.set_]);
    return true;
  }
  toggle(k) { this.set(k, !this.has(k)); return this.has(k); }
}

/**
 * claude 启动前 meta.cwd 是用户填的（"~" / "~/x"），启动后变成 CLI 报告的绝对路径。看到同一个会话从 ~ 形式变成绝对路径时
 * 记下这台机器（本机 / 服务器）的 home，之后其它还是 ~ 形式的会话（例如启动失败的）也能归到同一个项目。只在本页 localStorage。
 */
export function inferHome(before, after) {
  const a = normPath(after);
  if (!a.startsWith('/')) return '';
  const b = normPath(before);
  if (b === '~') return a;
  if (!b.startsWith('~/')) return '';
  const suffix = b.slice(1);
  if (!a.endsWith(suffix) || a.length <= suffix.length) return '';
  return a.slice(0, a.length - suffix.length) || '';
}
export class HomeBook {
  constructor(storage, key = HOMES_KEY) {
    this.storage = storage;
    this.key = key;
    const v = readJSON(storage, key, {});
    this.homes = { local: cleanHome(v && v.local), server: cleanHome(v && v.server) };
    this.last = new Map();
  }
  /** 记下会话的 cwd；学到了新的 home → true */
  observe(sid, target, cwd) {
    const prev = this.last.get(sid);
    this.last.set(sid, cwd);
    if (typeof prev !== 'string' || prev === cwd || !prev.startsWith('~')) return false;
    const hm = cleanHome(inferHome(prev, cwd));
    if (!hm || (target !== 'local' && target !== 'server') || this.homes[target] === hm) return false;
    this.homes[target] = hm;
    writeJSON(this.storage, this.key, this.homes);
    return true;
  }
  forget(sid) { this.last.delete(sid); }
}

// ---------------------------------------------------------------- DOM（与 app.js 的侧栏同一份，回放页第 14i 节直接用）

function locTag(target) {
  if (target === 'local') return h('span', { class: 'loc-tag', title: '在本机运行' }, icon('laptop'), '本机');
  if (target === 'server') return h('span', { class: 'loc-tag srv', title: '在服务器（计算节点）上运行；同一路径、不同作业算同一个项目' }, icon('server'), '服务器');
  return null;
}

/**
 * 项目标题行：<div.proj-hd draggable> <button.proj-toggle aria-expanded>（chevron、名称、本机/服务器、会话数、状态点）
 * <button.proj-add>（悬停 / 聚焦 / 触屏常显）</div>。
 * g = { key, name, path, target, n, agg, collapsed, active, bodyId }；拖动 / 菜单由调用方挂在外层。
 */
export function projectHeader(g, { onToggle, onAdd } = {}) {
  const sum = aggregateText(g.agg);
  const place = placeLabel(g.target);
  const dot = g.agg && g.agg.top ? h('span', { class: 'dot proj-dot st-' + g.agg.top, title: sum }) : null;
  const title = [(g.path || '未知目录（会话没有工作目录）'), [place, g.n + ' 个会话', sum].filter(Boolean).join(' · '),
    '点击' + (g.collapsed ? '展开' : '折叠') + '；拖动可调整项目顺序；右键：更多'].join('\n');
  const toggle = h('button', {
    class: 'proj-toggle', type: 'button', 'aria-expanded': String(!g.collapsed), 'aria-controls': g.bodyId || null,
    'aria-label': '项目 ' + g.name + '（' + [place, g.n + ' 个会话', aggregateText(g.agg, '，')].filter(Boolean).join('，') + '）',
    title, 'data-fk': 'p:' + g.key, 'data-nav': '1',
  },
  icon('chevdown', 'proj-chev'),
  h('span', { class: 'proj-name' }, g.name),
  locTag(g.target),
  h('span', { class: 'proj-count', 'aria-hidden': 'true' }, String(g.n)),
  dot,
  h('span', { class: 'deny-tip' }, '不同项目，不能放这里'));
  const add = h('button', { class: 'icon-btn proj-add', type: 'button', 'data-fk': 'a:' + g.key,
    title: '在这个项目里新建会话' + (g.path ? '（' + place + ' ' + g.path + '）' : ''), 'aria-label': '在项目「' + g.name + '」中新建会话' }, icon('plus'));
  if (onToggle) toggle.addEventListener('click', () => onToggle(g));
  if (onAdd) add.addEventListener('click', (e) => { e.stopPropagation(); onAdd(g); });
  return h('div', { class: headerClass(g.agg, g.collapsed, g.active), draggable: 'true', 'data-pkey': g.key }, toggle, add);
}

/** 分区标题（「项目」「聊天」）：小号弱化文字 + 可选的「+」按钮 */
export function sectionHead(label, { count = null, addTitle = '', addLabel = '', onAdd = null, fk = '' } = {}) {
  const add = onAdd ? h('button', { class: 'icon-btn side-sec-add', type: 'button', title: addTitle, 'aria-label': addLabel || addTitle, 'data-fk': fk || null }, icon('plus')) : null;
  if (add) add.addEventListener('click', () => onAdd());
  return h('div', { class: 'side-sec' },
    h('span', { class: 'side-sec-t' }, label),
    count != null ? h('span', { class: 'side-sec-n', 'aria-hidden': 'true' }, String(count)) : null,
    h('span', { class: 'deny-tip' }, '不能放这里'),
    add);
}
