// dmg 版自动更新的界面（docs/SPEC_V2_TUI.md §14.11；后端 ccr/updater.py）。
// 只有后端开启了自动更新（内置模式 + .app 里有更新源）时 bootstrap 才带 `update`，否则本模块什么都不挂（私有版不变）。
// - 主区域上方一条提示：新版本可用（立即更新 / 稍后 / 更新说明）、等会话空闲、下载 % / 校验 / 安装 / 即将重新打开窗口、
//   失败（原因 + 重试 + 打开下载页）、无法自动替换（打开已下载的安装包 + 打开下载页）、更新完成后「已更新到 X」一次。
// - 设置弹窗里的「自动检查更新」开关 + 「立即检查」+ 上次检查结果。
// 所有请求都只在用户点击时发出；「打开下载页」/「在 GitHub 上查看」是 target=_blank 的普通链接（原生壳交给默认浏览器）。
// 纯函数（bannerModel / checkText / pctOf）供回放自测页直接验证。
import { h, icon, spinner, fmtSize, fmtRelTime } from './dom.js';

const BUSY_PHASES = new Set(['waiting', 'downloading', 'verifying', 'installing', 'relaunching']);

export function pctOf(p) {
  if (!p || !(p.total > 0)) return null;
  return Math.max(0, Math.min(100, Math.floor((p.done / p.total) * 100)));
}

function busyText(list) {
  const n = Array.isArray(list) ? list.length : 0;
  if (!n) return '';
  const names = list.slice(0, 3).join('、') + (n > 3 ? ` 等 ${n} 个` : '');
  return `（还在忙：${names}）`;
}

/**
 * 更新提示条的内容（纯函数）。返回 null（不显示）或
 * { kind: 'info'|'progress'|'warn'|'error'|'ok', text, pct, notes, page, actions: [{ id, label, primary }] }。
 * actions 的 id：install / later / cancel / ack / retry / dismiss / open_dmg / page（page 是链接）。
 */
export function bannerModel(st) {
  if (!st || st.enabled !== true) return null;
  const av = st.available || null;
  const ver = av ? av.version : '';
  const page = typeof st.page === 'string' ? st.page : (av && av.page) || '';
  if (st.just_updated && !BUSY_PHASES.has(st.phase)) {
    return { kind: 'ok', text: `已更新到 ${st.just_updated.version}`, page, notes: '',
      actions: [{ id: 'ack', label: '知道了', primary: true }] };
  }
  switch (st.phase) {
    case 'waiting':
      return { kind: 'info', text: `等所有会话空闲后自动开始更新到 ${ver}${busyText(st.busy)}`, page: '', notes: '',
        actions: [{ id: 'cancel', label: '取消' }] };
    case 'downloading': {
      const pct = pctOf(st.progress);
      const sz = st.progress && st.progress.total ? `（${fmtSize(st.progress.done)} / ${fmtSize(st.progress.total)}）` : '';
      return { kind: 'progress', text: `正在下载新版本 ${ver}… ${pct == null ? '' : pct + '%'}${sz}`, pct, page: '', notes: '',
        actions: [{ id: 'cancel', label: '取消' }] };
    }
    case 'verifying':
      return { kind: 'progress', text: `正在校验新版本 ${ver}（签名与完整性）…`, pct: null, page: '', notes: '', actions: [] };
    case 'installing':
      return { kind: 'progress', text: `正在安装 ${ver}…`, pct: null, page: '', notes: '', actions: [] };
    case 'relaunching':
      return { kind: 'progress', text: '安装完成，即将重新打开窗口…', pct: null, page: '', notes: '', actions: [] };
    case 'manual':
      return { kind: 'warn', text: st.error || '无法自动替换，请手动安装新版本', page, notes: '',
        actions: [{ id: 'open_dmg', label: '打开已下载的安装包', primary: true }, { id: 'retry', label: '重试' },
          { id: 'page', label: '打开下载页' }, { id: 'dismiss', label: '关闭' }] };
    case 'failed':
      return { kind: 'error', text: '更新失败：' + (st.error || '未知原因'), page, notes: '',
        actions: [{ id: 'retry', label: '重试', primary: true }, { id: 'page', label: '打开下载页' }, { id: 'dismiss', label: '关闭' }] };
    default:
      break;
  }
  if (av && !av.snoozed) {
    return { kind: 'info', text: `新版本 ${ver} 可用`, page: av.page || page, notes: av.notes || '',
      actions: [{ id: 'install', label: '立即更新', primary: true }, { id: 'later', label: '稍后' }] };
  }
  return null;
}

/** 设置里「上次检查」一行（纯函数） */
export function checkText(st) {
  if (!st || st.enabled !== true) return '';
  if (st.checking) return '正在检查…';
  const lc = st.last_check;
  if (!lc || !lc.at) return '还没有检查过';
  const when = ' · ' + fmtRelTime(lc.at);
  switch (lc.result) {
    case 'available': return `发现新版本 ${st.available ? st.available.version : ''}` + when;
    case 'uptodate': return '已是最新版本' + when;
    case 'none': return '还没有发布任何版本' + when;
    case 'unsupported': return '新版本需要更新的 macOS' + when;
    default: return '检查失败：' + (lc.error || '未知原因') + when;
  }
}

/** 前端的更新状态（来自 bootstrap 与 SSE 的 event: update），提示条与设置弹窗都订阅它 */
export class UpdateStore {
  constructor() { this.st = null; this.subs = new Set(); }
  get enabled() { return !!(this.st && this.st.enabled === true); }
  set(st) {
    this.st = st && typeof st === 'object' ? st : null;
    for (const fn of [...this.subs]) { try { fn(this.st); } catch (e) { console.warn('更新状态回调出错', e); } } // eslint-disable-line no-console
  }
  subscribe(fn) { this.subs.add(fn); return () => this.subs.delete(fn); }
}

/** 主区域上方的提示条。act(id) 由调用方发请求（只在用户点击时调用）。 */
export class UpdateBanner {
  constructor(store, { mount, act }) {
    this.store = store;
    this.act = act;
    this.mount = mount;   // (el) => 插入到页面里
    this.el = null;
    this.notesOpen = false;
    this.key = '';
    store.subscribe(() => this.render());
    this.render();
  }

  render() {
    const m = bannerModel(this.store.st);
    if (!m) { if (this.el) this.el.hidden = true; this.key = ''; return; }
    if (!this.el) {
      this.el = h('div', { class: 'upd-banner', role: 'status', 'aria-live': 'polite' });
      this.mount(this.el);
    }
    const key = JSON.stringify(m);
    if (key === this.key && !this.el.hidden) return;
    if (this.key && JSON.parse(this.key).text.split(' ')[0] !== m.text.split(' ')[0]) this.notesOpen = false;
    this.key = key;
    this.el.hidden = false;
    this.el.className = 'upd-banner is-' + m.kind;
    this.el.dataset.kind = m.kind;
    const lead = m.kind === 'progress' ? spinner() : icon({ ok: 'check', info: 'download', warn: 'shield', error: 'x' }[m.kind] || 'download', 'upd-ic');
    const parts = [lead, h('span', { class: 'upd-text' }, m.text)];
    if (m.notes) {
      parts.push(h('button', { class: 'upd-link', type: 'button', 'aria-expanded': String(this.notesOpen),
        onclick: () => { this.notesOpen = !this.notesOpen; this.key = ''; this.render(); } }, '更新说明'));
    } else if (m.kind === 'ok' && m.page) {
      parts.push(h('a', { class: 'upd-link', href: m.page, target: '_blank', rel: 'noopener noreferrer' }, '更新说明'));
    }
    if (m.pct != null) parts.push(h('span', { class: 'upd-bar', 'aria-hidden': 'true' }, h('span', { style: { width: m.pct + '%' } })));
    const acts = h('span', { class: 'upd-acts' });
    for (const a of m.actions) {
      if (a.id === 'page') {
        if (m.page) acts.appendChild(h('a', { class: 'btn small ghost', href: m.page, target: '_blank', rel: 'noopener noreferrer', 'data-act': 'page' }, a.label));
        continue;
      }
      const b = h('button', { class: 'btn small' + (a.primary ? ' primary' : ' ghost'), type: 'button', 'data-act': a.id }, a.label);
      b.addEventListener('click', async () => {
        b.disabled = true;
        try { await this.act(a.id, this.store.st); } finally { b.disabled = false; }
      });
      acts.appendChild(b);
    }
    parts.push(acts);
    const row = h('div', { class: 'upd-row' }, parts);
    const kids = [row];
    if (m.notes && this.notesOpen) {
      kids.push(h('div', { class: 'upd-notes' }, h('pre', null, m.notes),
        m.page ? h('a', { href: m.page, target: '_blank', rel: 'noopener noreferrer' }, '在 GitHub 上查看') : null));
    }
    this.el.replaceChildren(...kids);
  }
}

/** 调用后端：id → 请求（只在用户点击时） */
export async function updateAction(api, store, id) {
  const st = store.st || {};
  const path = {
    install: '/api/update/install', retry: '/api/update/install', cancel: '/api/update/cancel', ack: '/api/update/ack',
    dismiss: '/api/update/dismiss', open_dmg: '/api/update/open_dmg', check: '/api/update/check', later: '/api/update/snooze',
  }[id];
  if (!path) return null;
  const body = id === 'later' ? { version: st.available ? st.available.version : '' } : {};
  const r = await api.post(path, body);
  if (r && typeof r === 'object') store.set(r);
  return r;
}

/** 设置弹窗里的一节：自动检查开关 + 立即检查 + 状态行 */
export function updateSettingsField(api, store, { onError } = {}) {
  const box = h('input', { type: 'checkbox', name: 'update_auto_check', checked: !!(store.st && store.st.auto_check) });
  const row = h('label', { class: 'check settings-check' }, box,
    h('span', null, h('b', null, '自动检查更新'), h('span', { class: 'muted' }, ' — 每天一次，有新版本时在窗口上方提示；不会自动安装')));
  const now = h('button', { class: 'btn ghost', type: 'button', 'data-act': 'check' }, '立即检查');
  const line = h('div', { class: 'field-hint upd-check-line' });
  const ver = h('span', { class: 'muted small-text' });
  const sync = () => {
    const st = store.st;
    if (!st) return;
    box.checked = !!st.auto_check;
    line.textContent = checkText(st);
    ver.textContent = '当前版本 ' + (st.current || '?');
    now.disabled = !!st.checking;
  };
  box.addEventListener('change', async () => {
    try { const r = await api.post('/api/update/settings', { auto_check: box.checked }); if (r) store.set(r); } catch (e) { if (onError) onError(e); sync(); }
  });
  now.addEventListener('click', async () => {
    now.disabled = true;
    try { await updateAction(api, store, 'check'); } catch (e) { if (onError) onError(e); }
    sync();
  });
  const unsub = store.subscribe(sync);
  sync();
  const el = h('div', { class: 'field upd-settings' }, h('label', { class: 'field-label' }, '更新'), row,
    h('div', { class: 'row' }, now, ver), line);
  return { el, dispose: unsub };
}
