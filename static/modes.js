// 权限模式选择栏（输入框底栏，SPEC §13）：列出全部模式并标出当前模式；不在本次启动的 Shift+Tab 循环里的标「需重启」。
// 真机实测（claude v2.1.330）循环顺序：manual → acceptEdits → plan → [bypassPermissions，仅以跳过权限启动] → auto → manual；
// dontAsk 不在循环里。选循环里的模式 = 后端逐个发 Shift+Tab 并确认；"需重启"的 = 以 --resume 重启并改启动参数。
import { h, icon } from './dom.js';

export const MODE_INFO = [
  { id: 'manual', name: '手动审批', desc: '每次工具调用都先问你' },
  { id: 'acceptEdits', name: '自动接受编辑', desc: '文件编辑自动通过，其它操作仍会询问' },
  { id: 'plan', name: '计划模式', desc: '只读规划：不改文件、不执行命令' },
  { id: 'bypassPermissions', name: '跳过权限', desc: '全部放行、不再询问（--dangerously-skip-permissions）' },
  { id: 'auto', name: '自动', desc: 'auto mode：由 Claude 判断哪些操作需要询问' },
  { id: 'dontAsk', name: '不询问', desc: '不弹审批，需要审批的操作直接拒绝（--permission-mode dontAsk）' },
];
export const CYCLE_ORDER = ['manual', 'acceptEdits', 'plan', 'bypassPermissions', 'auto'];

export function modeInfo(id) { return MODE_INFO.find((m) => m.id === id) || null; }
export function modeName(id) { const m = modeInfo(id); return m ? m.name : String(id || '—'); }

/** 会话 meta 的循环（后端 meta.mode_cycle）；老版本后端没有时按启动参数推断 */
export function cycleOf(meta) {
  const m = meta || {};
  if (Array.isArray(m.mode_cycle)) return m.mode_cycle;
  const la = m.launch || {};
  const bypass = !!la.skip_permissions || la.permission_mode === 'bypassPermissions' || m.mode === 'bypassPermissions';
  return CYCLE_ORDER.filter((x) => x !== 'bypassPermissions' || bypass);
}

let openMenu = null;   // {pop, anchor, dismiss} 当前打开的模式列表（同一时间只有一个）

/** 关闭模式列表（没打开时什么都不做） */
export function closeModeMenu() { if (openMenu) openMenu.dismiss(); }
/** 模式列表打开着时返回它的锚点（模式 chip），否则 null */
export function modeMenuAnchor() { return openMenu && openMenu.pop.isConnected ? openMenu.anchor : null; }

/**
 * 在 anchor 旁弹出模式列表：下方放得下就向下，否则向上（底栏里的模式 chip 贴着窗口底部，一般向上弹）。
 * opts: {current, cycle, blocked: 不能切换的原因|'' , onPick(id, needsRestart), onCycle()}
 * 返回 popover 元素。点击外部 / Esc 关闭；打开期间 anchor 带 aria-expanded="true"（底栏 chip 保持填充）。
 */
export function modeMenu(anchor, { current, cycle, blocked = '', onPick, onCycle }) {
  closeModeMenu();
  document.querySelectorAll('.popover.mode-menu').forEach((p) => p.remove());
  const rows = MODE_INFO.map((m) => {
    const cur = m.id === current;
    const restart = !cycle.includes(m.id);
    const b = h('button', { class: 'mode-row' + (cur ? ' on' : ''), type: 'button', role: 'menuitemradio', 'aria-checked': String(cur),
      disabled: cur || (!!blocked && !restart), title: cur ? '当前模式' : blocked && !restart ? blocked : restart ? '不在本次启动的 Shift+Tab 循环里：需要以 --resume 重启' : '逐个按 Shift+Tab 切换过去' },
    h('span', { class: 'mode-check' }, cur ? icon('check') : null),
    h('span', { class: 'mode-main' },
      h('span', { class: 'mode-name' }, m.name, h('code', { class: 'mode-id' }, m.id)),
      h('span', { class: 'mode-desc' }, m.desc)),
    cur ? h('span', { class: 'mode-tag cur' }, '当前') : restart ? h('span', { class: 'mode-tag restart' }, '需重启') : null);
    b.addEventListener('click', () => { dismiss(); onPick(m.id, restart); });
    return b;
  });
  const btab = h('button', { class: 'btn ghost small', type: 'button', disabled: !!blocked, title: blocked || '在终端里按一次 Shift+Tab（循环到下一个模式）' }, 'Shift+Tab');
  btab.addEventListener('click', () => { dismiss(); onCycle(); });
  const order = cycle.map(modeName).join(' → ');
  const pop = h('div', { class: 'popover mode-menu', role: 'menu', 'aria-label': '权限模式' },
    h('div', { class: 'pop-head' }, '权限模式'),
    h('div', { class: 'mode-list' }, rows),
    h('div', { class: 'mode-foot' }, h('span', { class: 'muted small-text' }, 'Shift+Tab 循环：' + order + ' → …'), btab),
    blocked ? h('div', { class: 'mode-blocked' }, blocked) : null);
  (anchor.closest('dialog') || document.body).appendChild(pop);
  place(pop, anchor);
  const dismiss = (e) => {
    if (e && e.type === 'mousedown' && (pop.contains(e.target) || anchor.contains(e.target))) return;
    pop.remove();
    anchor.setAttribute('aria-expanded', 'false');
    if (openMenu && openMenu.pop === pop) openMenu = null;
    document.removeEventListener('mousedown', dismiss, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const onKey = (e) => { if (e.key === 'Escape') dismiss(); };
  document.addEventListener('mousedown', dismiss, true);
  document.addEventListener('keydown', onKey, true);
  anchor.setAttribute('aria-expanded', 'true');
  openMenu = { pop, anchor, dismiss };
  return pop;
}

/** 定位（position: fixed，样式用 CSSOM 赋值，CSP 允许）：水平贴 anchor 左边、不出窗口；
 *  垂直方向下方放得下就放下方，否则放空间更大的一侧，并把最大高度限制在那一侧的可用空间内 */
function place(pop, anchor) {
  const M = 8, GAP = 6;
  const r = anchor.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  const w = pop.offsetWidth;
  pop.style.left = Math.max(M, Math.min(r.left, vw - w - M)) + 'px';
  const below = vh - r.bottom - GAP - M, above = r.top - GAP - M;
  const ph = pop.offsetHeight;
  if (ph <= below || below >= above) {
    pop.style.top = (r.bottom + GAP) + 'px';
    pop.style.bottom = 'auto';
    if (ph > below) pop.style.maxHeight = Math.max(120, below) + 'px';
  } else {
    pop.style.bottom = (vh - r.top + GAP) + 'px';
    pop.style.top = 'auto';
    if (ph > above) pop.style.maxHeight = Math.max(120, above) + 'px';
  }
}
