// dmg 版自动更新的界面（docs/SPEC_V2_TUI.md §14.11；后端 ccr/updater.py）。
// 只有后端开启了自动更新（内置模式 + .app 里有更新源）时 bootstrap 才带 `update`，否则本模块什么都不挂（私有版不变）。
// - 主区域上方一条提示：新版本可用（立即更新 / 稍后 / 更新说明）、等会话空闲、下载 % / 校验 / 安装 / 即将重新打开窗口、
//   失败（原因 + 重试 + 打开下载页）、无法自动替换（打开已下载的安装包 + 打开下载页）、更新完成后「已更新到 X」一次。
// - 设置弹窗的「更新」一节：自动检查开关、立即检查（会清除「稍后」）、上次检查结果；有新版本时**始终**显示
//   「发现新版本 X」+ 立即更新 + 更新说明 + 打开下载页（提示条点过「24 小时后再提醒」也能在这里装），下载 / 校验 / 安装 /
//   等待空闲 / 失败 / 需要手动安装同步显示（与提示条同一个 store、同一个渲染函数）；写明会访问的域名与经哪个系统代理。
// 所有请求都只在用户点击时发出；「打开下载页」/「在 GitHub 上查看」是 target=_blank 的普通链接（原生壳交给默认浏览器）。
// 纯函数（bannerModel / settingsModel / checkText / netText / snoozeText / pctOf）供回放自测页直接验证。
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
  return updateModel(st, false);
}

/** 设置里「更新」一节的状态框：与提示条相同，但有新版本时不管点没点过「稍后」都显示（没有「稍后」按钮，多「打开下载页」） */
export function settingsModel(st) {
  return updateModel(st, true);
}

function updateModel(st, inSettings) {
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
  if (av && inSettings) {
    return { kind: 'info', text: `发现新版本 ${ver}`, page: av.page || page, notes: av.notes || '',
      actions: [{ id: 'install', label: '立即更新', primary: true }, { id: 'page', label: '打开下载页' }] };
  }
  if (av && !av.snoozed) {
    return { kind: 'info', text: `新版本 ${ver} 可用`, page: av.page || page, notes: av.notes || '',
      actions: [{ id: 'install', label: '立即更新', primary: true }, { id: 'later', label: '24 小时后再提醒' }] };
  }
  return null;
}

/** 设置里：窗口上方的提醒是否被「24 小时后再提醒」暂时隐藏（纯函数） */
export function snoozeText(st) {
  const av = st && st.available;
  if (!av || !av.snoozed) return '';
  const t = av.snoozed_until ? new Date(av.snoozed_until * 1000) : null;
  const when = t && !isNaN(t) ? t.toLocaleString('zh-CN', { hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
  return '已暂时隐藏窗口上方的提醒' + (when ? `（${when} 后再提醒）` : '') + '；点「立即检查」会重新显示';
}

/** 设置里：经哪个系统代理（纯函数） */
export function netText(st) {
  const n = st && st.network;
  if (!n) return '网络：检查时按系统代理设置连接（还没有连接过）';
  if (n.mode === 'proxy') return `网络：经系统代理 ${n.proxy}（不会绕过代理直连）`;
  if (n.mode === 'unsupported') return `网络：系统代理是 ${n.why || 'PAC / SOCKS'}，暂不支持，未检查更新（不会绕过代理直连）`;
  return '网络：未设置系统代理（直接访问 GitHub）';
}

/** 设置里：会访问的域名（纯函数） */
export function hostsText(st) {
  const hs = (st && st.hosts) || {};
  const check = hs.check || 'github.com';
  const dl = Array.isArray(hs.download) && hs.download.length ? hs.download : ['github.com', 'release-assets.githubusercontent.com'];
  return `只访问 GitHub：检查更新 ${check}；下载新版本 ${dl.join(' → ')}（GitHub 的 Release 下载服务器）`;
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
    if (!this.box) this.box = new UpdateBox(this.el, 'upd-banner', (id) => this.act(id, this.store.st));
    this.el.hidden = false;
    this.box.show(m);
  }
}

/** 渲染一个状态框（提示条与设置里的「更新」一节共用）：图标 / spinner、文字、更新说明（可展开）、进度条、按钮。
 *  按钮只在用户点击时调用 act(id)；「打开下载页」「在 GitHub 上查看」是 target=_blank 链接。 */
export class UpdateBox {
  constructor(el, base, act) { this.el = el; this.base = base; this.act = act; this.notesOpen = false; this.key = ''; this.m = null; }

  show(m) {
    const key = JSON.stringify(m);
    if (key === this.key) return;
    if (this.m && this.m.text.split(' ')[0] !== m.text.split(' ')[0]) this.notesOpen = false;
    this.key = key;
    this.m = m;
    this.draw();
  }

  draw() {
    const m = this.m;
    this.el.className = this.base + ' is-' + m.kind;
    this.el.dataset.kind = m.kind;
    const lead = m.kind === 'progress' ? spinner() : icon({ ok: 'check', info: 'download', warn: 'shield', error: 'x' }[m.kind] || 'download', 'upd-ic');
    const parts = [lead, h('span', { class: 'upd-text' }, m.text)];
    if (m.notes) {
      parts.push(h('button', { class: 'upd-link', type: 'button', 'aria-expanded': String(this.notesOpen),
        onclick: () => { this.notesOpen = !this.notesOpen; this.draw(); } }, '更新说明'));
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
        try { await this.act(a.id); } finally { b.disabled = false; }
      });
      acts.appendChild(b);
    }
    parts.push(acts);
    const kids = [h('div', { class: 'upd-row' }, parts)];
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

/** 设置弹窗里的一节：自动检查开关 + 立即检查 + 状态行 + 新版本 / 进度状态框（含「立即更新」）+ 域名与网络说明 */
export function updateSettingsField(api, store, { onError } = {}) {
  const box = h('input', { type: 'checkbox', name: 'update_auto_check', checked: !!(store.st && store.st.auto_check) });
  const row = h('label', { class: 'check settings-check' }, box,
    h('span', null, h('b', null, '自动检查更新'), h('span', { class: 'muted' }, ' — 每天一次，有新版本时在窗口上方提示；不会自动安装')));
  const now = h('button', { class: 'btn ghost', type: 'button', 'data-act': 'check' }, '立即检查');
  const line = h('div', { class: 'field-hint upd-check-line' });
  const ver = h('span', { class: 'muted small-text' });
  const stEl = h('div', { class: 'upd-set-box', role: 'status', 'aria-live': 'polite', hidden: true });
  const stBox = new UpdateBox(stEl, 'upd-set-box', async (id) => {
    try { await updateAction(api, store, id); } catch (e) { if (onError) onError(e); }
  });
  const snz = h('div', { class: 'field-hint upd-snooze-line', hidden: true });
  const hosts = h('div', { class: 'field-hint upd-hosts-line' });
  const net = h('div', { class: 'field-hint upd-net-line' });
  const sync = () => {
    const st = store.st;
    if (!st) return;
    box.checked = !!st.auto_check;
    line.textContent = checkText(st);
    ver.textContent = '当前版本 ' + (st.current || '?');
    now.disabled = !!st.checking;
    const m = settingsModel(st);
    stEl.hidden = !m;
    if (m) stBox.show(m);
    const sn = snoozeText(st);
    snz.hidden = !sn;
    snz.textContent = sn;
    hosts.textContent = hostsText(st);
    net.textContent = netText(st);
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
    h('div', { class: 'row' }, now, ver), line, stEl, snz, hosts, net);
  return { el, dispose: unsub };
}
