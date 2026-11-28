// macOS 系统通知（只在原生窗口里有，SPEC §12.1 / §14.12）。
//
// 判定**不另造一套**：后端在提示音的同一个确认点记 meta.done_at（完成一轮 = 侧栏「新回复」，§15.1）与 meta.attn_at
// （需要操作 = 权限审批 / 提问 / 信任目录等，与「需要我操作时」提示音同一个去抖与去重，§12）。页面只看这两个时间戳
// 有没有比上次看到的大：第一次看到某个会话（bootstrap / 新建）只记基线，SSE 重连回放的旧 meta 不会再通知。
// 通道：页面 → window.webkit.messageHandlers.ccrNotify.postMessage({op…})（原生壳的 WKScriptMessageHandlerWithReply，
// 返回 Promise）→ UNUserNotificationCenter；点通知 → 原生壳派发 `ccr-native-open-session` 事件（detail.sid）→ 打开会话。
// 浏览器 / 手机里没有这个 handler：什么都不做。开关与授权在原生壳那边（UserDefaults），默认关闭；第一次打开开关时
// 才请求系统授权（绝不在启动时请求）。纯函数部分 tests/test_sysnotify.py 用 node 测；DOM 部分在回放自测页第 14s 节。
import { h } from './dom.js';
import { REMOTE } from './api.js';

export const HANDLER = 'ccrNotify';
export const OPEN_EVENT = 'ccr-native-open-session';
export const SID_RE = /^[A-Za-z0-9_.-]{1,80}$/;   // 与 ccr/server.py 的 SID_RE 相同
export const COALESCE_MS = 20000;                 // 同一会话、同一种通知 20 秒内只发一次（原生壳那边同一会话只保留一条）
export const MAX_AGE_S = 600;                     // 超过 10 分钟的事件不通知（防御：时钟 / 回放异常）
export const TITLE_MAX = 120;
export const BODY_MAX = 160;
const PENDING_STATES = new Set(['waiting', 'trust']);
const OPEN_PENDING_MS = 60000;

const num = (v) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0);

/** 文字清理：控制字符换空格、合并空白、截断（超长加 …） */
export function cleanText(s, max) {
  const t = String(s == null ? '' : s).replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, ' ').replace(/\s+/g, ' ').trim();
  return t.length > max ? t.slice(0, Math.max(0, max - 1)).trimEnd() + '…' : t;
}

/** 基线：只记下 done_at / attn_at（不会变小），不通知 */
export function seedTrack(track, meta) {
  if (!meta || typeof meta.sid !== 'string') return;
  const prev = track.get(meta.sid);
  track.set(meta.sid, {
    done: Math.max(num(meta.done_at), prev ? prev.done : 0),
    attn: Math.max(num(meta.attn_at), prev ? prev.attn : 0),
  });
}

/**
 * meta 到达：done_at / attn_at 比记下的大 = 新的一次完成 / 需要操作。返回 [{sid, kind:'done'|'attention', at}]。
 * 第一次看到这个会话 = 基线（不通知）。完成要求此刻仍是未读（meta.unread）；需要操作要求此刻仍在等（waiting / trust）。
 */
export function detectEvents(track, meta, nowSec) {
  if (!meta || typeof meta.sid !== 'string') return [];
  const rec = track.get(meta.sid);
  if (!rec) { seedTrack(track, meta); return []; }
  const out = [];
  const fresh = (t) => !(typeof nowSec === 'number') || Math.abs(nowSec - t) <= MAX_AGE_S;
  const d = num(meta.done_at);
  if (d > rec.done) {
    rec.done = d;
    if (meta.unread === true && fresh(d)) out.push({ sid: meta.sid, kind: 'done', at: d });
  }
  const a = num(meta.attn_at);
  if (a > rec.attn) {
    rec.attn = a;
    if (PENDING_STATES.has(meta.state) && fresh(a)) out.push({ sid: meta.sid, kind: 'attention', at: a });
  }
  out.sort((x, y) => x.at - y.at);
  return out;
}

/** 要不要弹：开关打开，且（窗口不在前台，或不是当前打开的会话） */
export function shouldNotify(ev, ctx) {
  if (!ev || !ctx || !ctx.enabled) return false;
  return !(ctx.foreground && ctx.activeSid === ev.sid);
}

/** 同一会话短时间内合并：同一种通知 COALESCE_MS 内只发一次（不同种类照发）。last = Map(sid → {kind, t}) */
export function coalesce(last, ev, nowMs) {
  const p = last.get(ev.sid);
  if (p && p.kind === ev.kind && nowMs - p.t < COALESCE_MS) return false;
  last.set(ev.sid, { kind: ev.kind, t: nowMs });
  return true;
}

/** 工具名：「Bash command」→「Bash」，其它原样（Create file、Fetch…） */
export function toolName(t) {
  return cleanText(String(t || '').replace(/\s+command$/i, ''), 60);
}

/** 通知正文（简短说明） */
export function notifyBody(kind, dialog, meta) {
  if (kind === 'done') return '完成';
  const d = dialog && typeof dialog === 'object' ? dialog : null;
  const k = d ? d.kind : (meta && meta.state === 'trust' ? 'trust' : (meta && meta.dialog_kind) || '');
  let text;
  if (k === 'permission') {
    const t = d ? toolName(d.tool || d.title) : '';
    text = t ? '需要审批：' + t : '需要审批';
  } else if (k === 'question') {
    const q = d ? cleanText(d.header || d.question || '', 80) : '';
    text = q ? '需要回答：' + q : '需要回答问题';
  } else if (k === 'trust') {
    text = '需要确认：信任这个目录';
  } else {
    const t = d ? cleanText(d.title || '', 80) : '';
    text = t ? '需要处理：' + t : '需要你处理';
  }
  return cleanText(text, BODY_MAX);
}

export function notifyTitle(meta) {
  const m = meta || {};
  return cleanText(m.display_title || m.title || '', TITLE_MAX) || '会话';
}

/** 交给原生壳的消息（字段与 tools/macapp/main.swift 的 ccrParseNotify 一一对应） */
export function postPayload(ev, meta, dialog, activeSid) {
  return { op: 'post', sid: ev.sid, kind: ev.kind, title: notifyTitle(meta), body: notifyBody(ev.kind, dialog, meta),
    current: ev.sid === activeSid };
}

/** 设置弹窗里的说明（按原生壳回报的授权状态） */
export function authHint(st) {
  if (!st || !st.available) return '这个窗口不能发系统通知（原生壳不是正常安装的 .app）';
  if (st.auth === 'denied') return 'macOS 不允许本应用发通知：请到「系统设置 → 通知 → CC Remote Chat」里打开「允许通知」，再回来勾选。';
  if (st.error && !st.enabled) return '请求通知授权失败：' + st.error + '。可以到「系统设置 → 通知 → CC Remote Chat」里手动允许，再回来勾选。';
  if (st.auth === 'notDetermined') return '第一次打开时 macOS 会弹出「是否允许通知」的授权框，请点「允许」。';
  if (st.enabled) return '已授权；在「系统设置 → 通知 → CC Remote Chat」里可以改横幅样式。';
  return '已授权，当前关闭。';
}

export class SysNotify {
  constructor(win) {
    this.win = win || null;
    this.track = new Map();    // sid → {done, attn}（已看到的最大时间戳）
    this.last = new Map();     // sid → {kind, t}（合并用）
    this.status = null;        // 原生壳回报：{available, enabled, auth}
    this.deps = null;
    this.pendingOpen = null;   // 点了通知、但会话还没加载出来（冷启动）：{sid, t}
    this.listeners = new Set();
    this.posted = [];          // 最近发出的消息（调试 / 回放自测用，最多 20 条）
    this._onOpenEvent = (e) => {
      if (e && typeof e.preventDefault === 'function') e.preventDefault();   // 告诉原生壳：页面接手了
      this.requestOpen(e && e.detail ? e.detail.sid : null);
    };
  }

  bridge() {
    const w = this.win;
    const hd = w && w.webkit && w.webkit.messageHandlers && w.webkit.messageHandlers[HANDLER];
    return hd && typeof hd.postMessage === 'function' ? hd : null;
  }

  get available() { return !REMOTE && !!this.bridge(); }

  /** deps = { activeSid(), hasSession(sid), foreground(), meta(sid)?, dialog(s), onOpen(sid) } */
  init(deps) {
    this.deps = deps || null;
    if (!this.available || this._inited) return;
    this._inited = true;
    this.win.addEventListener(OPEN_EVENT, this._onOpenEvent);
    this.refresh();
  }

  async call(msg) {
    const b = this.available ? this.bridge() : null;
    if (!b) return null;
    try {
      const r = await b.postMessage(msg);
      return r && typeof r === 'object' ? r : null;
    } catch (_) {
      return null;
    }
  }

  setStatus(r) {
    this.status = { available: r.available !== false, enabled: r.enabled === true, auth: typeof r.auth === 'string' ? r.auth : 'unknown',
      error: typeof r.error === 'string' && r.error ? cleanText(r.error, 200) : null };
    if (typeof r.open === 'string') this.requestOpen(r.open);   // 冷启动：点通知把应用打开的
    for (const fn of this.listeners) { try { fn(this.status); } catch (_) { /* 设置弹窗已关 */ } }
  }

  async refresh() {
    const r = await this.call({ op: 'status' });
    if (r) this.setStatus(r);
    return this.status;
  }

  async setEnabled(on) {
    const r = await this.call({ op: 'enable', on: !!on });
    if (r) this.setStatus(r);
    return this.status;
  }

  /** bootstrap 之后：这些会话的当前 done_at / attn_at 作为基线 */
  seed(sessions) {
    for (const s of sessions || []) if (s && s.meta) seedTrack(this.track, s.meta);
    this.flushOpen();
  }

  /** 每条 meta 合并之后（app.js 的 onMeta）调用 */
  onMeta(s) {
    if (!this.available || !s || !s.meta || s.readOnly) return;
    const evs = detectEvents(this.track, s.meta, Date.now() / 1000);
    if (!evs.length || !this.status || !this.status.enabled) return;
    const d = this.deps || {};
    const ctx = { enabled: true, foreground: d.foreground ? !!d.foreground() : false, activeSid: d.activeSid ? d.activeSid() : null };
    for (const ev of evs) {
      if (!shouldNotify(ev, ctx) || !coalesce(this.last, ev, Date.now())) continue;
      const msg = postPayload(ev, s.meta, ev.kind === 'attention' && d.dialog ? d.dialog(s) : null, ctx.activeSid);
      this.posted.push(msg);
      if (this.posted.length > 20) this.posted.shift();
      this.call(msg);
    }
  }

  /** 用户打开了这个会话：通知中心里它那条通知已经没用了 */
  clear(sid) {
    if (!this.available || !this.status || !this.status.enabled || !SID_RE.test(String(sid || ''))) return;
    this.last.delete(sid);
    this.call({ op: 'clear', sid });
  }

  requestOpen(sid) {
    if (typeof sid !== 'string' || !SID_RE.test(sid) || !this.deps) return;
    if (this.deps.hasSession && this.deps.hasSession(sid)) { this.pendingOpen = null; this.deps.onOpen(sid); }
    else this.pendingOpen = { sid, t: Date.now() };
  }

  flushOpen() {
    const p = this.pendingOpen;
    if (!p || !this.deps) return;
    if (Date.now() - p.t > OPEN_PENDING_MS) { this.pendingOpen = null; return; }
    if (this.deps.hasSession && this.deps.hasSession(p.sid)) { this.pendingOpen = null; this.deps.onOpen(p.sid); }
  }
}

export const sysNotify = new SysNotify(typeof window !== 'undefined' ? window : null);

/** 设置弹窗里的一节。手机远程访问时不显示；浏览器里显示为不可用。返回 { el, dispose } 或 null */
export function sysNotifySection(ctl = sysNotify) {
  if (REMOTE) return null;
  const box = h('input', { type: 'checkbox', name: 'sys_notify', disabled: true });
  const row = h('label', { class: 'check settings-check' }, box,
    h('span', null, h('b', null, '系统通知'), h('span', { class: 'muted' }, ' — 会话完成一轮、或需要你审批 / 回答时，在 macOS 通知中心弹通知；点通知打开那个会话')));
  const hint = h('div', { class: 'field-hint sysn-hint' });
  const el = h('div', { class: 'field sysn-field' }, h('label', { class: 'field-label' }, '系统通知'), row,
    h('div', { class: 'muted small-text' }, '只在窗口不在前台、或不是当前打开的会话时弹；同一会话 20 秒内同类通知只弹一次。'
      + '通知标题是会话名，正文如「完成」「需要审批：Bash」；不带声音（声音由上面的提示音负责）。开关只对这个原生窗口生效。'),
    hint);
  if (!ctl.available) {
    row.classList.add('disabled');
    hint.textContent = '只在 Mac 上的 CC Remote Chat 原生窗口里可用（浏览器里没有系统通知通道）。';
    return { el, dispose() {} };
  }
  let busy = false;
  const render = (st) => {
    box.disabled = busy || !st || !st.available;
    box.checked = !!(st && st.enabled);
    hint.textContent = st ? authHint(st) : '正在读取通知授权状态…';
    hint.classList.toggle('warn', !!(st && (st.auth === 'denied' || !st.available || (st.error && !st.enabled))));
  };
  const onStatus = (st) => render(st);
  ctl.listeners.add(onStatus);
  box.addEventListener('change', async () => {
    const want = box.checked;
    busy = true;
    render(ctl.status);
    box.checked = want;
    if (want && ctl.status && ctl.status.auth === 'notDetermined') hint.textContent = '正在请求系统授权（请在弹出的授权框里点「允许」）…';
    const st = await ctl.setEnabled(want);
    busy = false;
    render(st);
  });
  render(ctl.status);
  ctl.refresh();
  return { el, dispose() { ctl.listeners.delete(onStatus); } };
}
