// 网络层：仅同源请求（/api/*）。不使用 Cookie：
//   页面打开时用一次性登录码（#code=...）换取会话 token，存 sessionStorage['ccr_token']；
//   之后所有 /api/* 请求（含 GET、SSE）都带请求头 X-CCR-Token。

export const TOKEN_KEY = 'ccr_token';
export const TOKEN_HEADER = 'X-CCR-Token';

// 手机远程访问（私有版，docs/REMOTE_ACCESS.md）：页面不是从 127.0.0.1 / localhost 打开的 = 经公网域名进来的远程口。
//   - 设备 token 存 localStorage（Safari 被杀掉 / 主屏 App 重开后不用重新配对）；本机窗口仍用 sessionStorage、逐字不变。
//   - 远程口前面有 Caddy 暗门 cookie，所以请求要带同源 cookie；后端本身仍然只认 X-CCR-Token 头、不看 cookie。
export const REMOTE = typeof location !== 'undefined' && !['127.0.0.1', 'localhost'].includes(location.hostname);
export const CREDENTIALS = REMOTE ? 'same-origin' : 'omit';
const DEVICE_TOKEN_KEY = 'ccr_device_token';
const tokenKey = () => (REMOTE ? DEVICE_TOKEN_KEY : TOKEN_KEY);
const tokenStore = () => (REMOTE ? localStorage : sessionStorage);

export class ApiError extends Error {
  constructor(message, status, code = '') { super(message); this.status = status; this.code = code || ''; }
}

export function loadToken() {
  try { return tokenStore().getItem(tokenKey()) || ''; } catch (_) { return ''; }
}

/** 从 location.hash 取出一次性登录码，并立即把 hash 从地址栏/历史记录中清掉 */
export function takeLoginCode() {
  const raw = location.hash ? location.hash.slice(1) : '';
  if (!raw) return '';
  let code = '';
  try { code = new URLSearchParams(raw).get('code') || ''; } catch (_) { code = ''; }
  if (!code) return '';
  try { history.replaceState(null, '', location.pathname + location.search); } catch (_) { location.hash = ''; }
  return code;
}

export class Api {
  /** onAuthLost(message)：任何请求 401 时调用（只调用一次） */
  constructor({ onAuthLost } = {}) {
    this.token = loadToken();
    this.onAuthLost = onAuthLost || null;
    this.authLost = false;
  }

  setToken(t) {
    this.token = t || '';
    try {
      if (this.token) tokenStore().setItem(tokenKey(), this.token);
      else tokenStore().removeItem(tokenKey());
    } catch (_) { /* 忽略 */ }
  }

  headers(extra) {
    const hd = { ...(extra || {}) };
    if (this.token) hd[TOKEN_HEADER] = this.token;
    return hd;
  }

  /** 401 → 清除 token 并通知（会话失效，停止一切重连） */
  markAuthLost(msg) {
    this.setToken('');
    if (this.authLost) return;
    this.authLost = true;
    if (this.onAuthLost) this.onAuthLost(msg || '');
  }

  async request(method, path, body, { auth = true } = {}) {
    const opts = { method, headers: auth ? this.headers() : {}, credentials: CREDENTIALS, cache: 'no-store' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(REMOTE ? '连不上 Mac（网络、隧道或 Mac 上的应用没在运行）' : '无法连接本地服务（应用可能已退出）', 0);
    }
    let data = null;
    const text = await res.text().catch(() => '');
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = null; }
    }
    if (!res.ok) {
      const msg = (data && typeof data.error === 'string' && data.error) || `请求失败（HTTP ${res.status}）`;
      if (res.status === 401 && auth) this.markAuthLost(msg);
      const code = (data && typeof data.code === 'string' && data.code) || '';
      throw new ApiError(msg, res.status, code);
    }
    return data ?? {};
  }

  get(path) { return this.request('GET', path); }
  post(path, body = {}) { return this.request('POST', path, body); }
  patch(path, body = {}) { return this.request('PATCH', path, body); }
  del(path) { return this.request('DELETE', path); }

  /** 用一次性登录码换会话 token（不带 token 头） */
  async login(code) {
    const r = await this.request('POST', '/api/login', { code }, { auth: false });
    if (!r || typeof r.token !== 'string' || !r.token) throw new ApiError('登录失败：服务器未返回 token', 500);
    this.authLost = false;
    this.setToken(r.token);
    return r.token;
  }

  /** 手机远程访问：用 Mac 上生成的配对码换设备 token（不带 token 头）→ 设备信息 */
  async pair(code, name) {
    const r = await this.request('POST', '/api/remote/pair', { code, name }, { auth: false });
    if (!r || typeof r.token !== 'string' || !r.token) throw new ApiError('配对失败：服务器未返回 token', 500);
    this.authLost = false;
    this.setToken(r.token);
    return r.device || null;
  }

  bootstrap() { return this.get('/api/bootstrap'); }

  sessionPath(sid, tail = '') { return '/api/sessions/' + encodeURIComponent(sid) + tail; }
}

// ====================== SSE（fetch + ReadableStream） ======================

const IDLE_TIMEOUT = 45000; // 服务器每 15s 发 `: ping`；超过此时长无任何数据视为连接已死
const MAX_DELAY = 15000;

/**
 * 增量 SSE 解析器：feed(文本块) → 每个完整事件回调 onEvent({type, data, id})。
 * 支持 `id:` / `event:` / `data:`（多行拼接）/ `retry:` / 注释行（`: ping`）；事件以空行分隔；行尾 \n 或 \r\n。
 */
export class SseParser {
  constructor(onEvent, onComment) {
    this.onEvent = onEvent;
    this.onComment = onComment || (() => {});
    this.buf = '';
    this.resetEvent();
    this.lastId = '';
    this.retry = null;
  }

  resetEvent() { this.type = ''; this.data = []; this.hasData = false; this.id = null; }

  feed(chunk) {
    this.buf += chunk;
    let start = 0;
    for (;;) {
      const nl = this.buf.indexOf('\n', start);
      if (nl < 0) break;
      let line = this.buf.slice(start, nl);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      start = nl + 1;
      this.line(line);
    }
    this.buf = this.buf.slice(start);
  }

  line(line) {
    if (line === '') { this.dispatch(); return; }
    if (line[0] === ':') { this.onComment(line.slice(1).trim()); return; }
    const c = line.indexOf(':');
    const field = c < 0 ? line : line.slice(0, c);
    let value = c < 0 ? '' : line.slice(c + 1);
    if (value[0] === ' ') value = value.slice(1);
    if (field === 'data') { this.data.push(value); this.hasData = true; }
    else if (field === 'event') this.type = value;
    else if (field === 'id') { if (!value.includes('\0')) this.id = value; }
    else if (field === 'retry') { if (/^\d+$/.test(value)) this.retry = Number(value); }
  }

  dispatch() {
    if (this.id !== null) this.lastId = this.id;
    const ev = { type: this.type || 'message', data: this.data.join('\n'), id: this.lastId, hasData: this.hasData };
    const any = this.hasData || this.type;
    this.resetEvent();
    if (any) this.onEvent(ev);
  }
}

/**
 * 单个 SSE 连接接收全部会话事件：GET /api/stream?since=<gseq>&instance=<instance_id>，带 X-CCR-Token 头。
 * 断线按指数退避重连（since 取 getSince() = 最后处理的 gseq，instance 取 getInstance()）。
 * beforeReconnect() 返回 'ok' | 'retry'（稍后再试）| 'stop'（放弃，例如认证失效）。
 * 收到 `event: reset` → 停止并调用 onReset(data)；401 → api.markAuthLost()，停止。
 */
export class EventStream {
  constructor({ api, getSince, getInstance, onEnvelope, onStatus, beforeReconnect, onReset, onConfig, onUpdate }) {
    this.api = api;
    this.getSince = getSince;
    this.getInstance = getInstance || (() => '');
    this.onEnvelope = onEnvelope;
    this.onStatus = onStatus || (() => {});
    this.beforeReconnect = beforeReconnect || (async () => 'ok');
    this.onReset = onReset || null;
    this.onConfig = onConfig || null;
    this.onUpdate = onUpdate || null;   // dmg 版自动更新状态（只有后端开启了自动更新才会推 event: update）
    this.ctrl = null;
    this.delay = 1000;
    this.timer = 0;
    this.idleTimer = 0;
    this.stopped = true;
    this.conn = 0;
  }

  start() {
    this.stopped = false;
    this.delay = 1000;
    this.open();
  }

  url() {
    const since = Math.max(0, this.getSince() | 0);
    const inst = this.getInstance() || '';
    return '/api/stream?since=' + since + (inst ? '&instance=' + encodeURIComponent(inst) : '');
  }

  async open() {
    this.abort();
    if (this.stopped) return;
    if (this.api.authLost || !this.api.token) { this.stop(); this.api.markAuthLost(''); return; }
    const conn = ++this.conn;
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    const alive = () => !this.stopped && this.conn === conn;
    this.onStatus('connecting');
    let res;
    try {
      res = await fetch(this.url(), {
        method: 'GET', headers: this.api.headers({ Accept: 'text/event-stream' }),
        credentials: CREDENTIALS, cache: 'no-store', signal: ctrl.signal,
      });
    } catch (_) {
      if (alive()) this.failed();
      return;
    }
    if (!alive()) { try { ctrl.abort(); } catch (_) { /* 忽略 */ } return; }
    if (res.status === 401) {
      let msg = '';
      try { const j = await res.json(); msg = (j && j.error) || ''; } catch (_) { /* 忽略 */ }
      this.stop();
      this.api.markAuthLost(msg);
      return;
    }
    if (!res.ok || !res.body) { this.failed(); return; }
    this.delay = 1000;
    this.onStatus('open');
    this.armIdle(conn);

    let resetData = null;
    const parser = new SseParser((ev) => {
      if (!alive() || resetData) return;
      if (ev.type === 'reset') {
        try { resetData = ev.data ? JSON.parse(ev.data) : {}; } catch (_) { resetData = {}; }
        return;
      }
      if (ev.type === 'config') {
        // 配置被外部修改（如 app.py --jobid）：同步侧栏
        if (this.onConfig && ev.data) {
          try { const j = JSON.parse(ev.data); if (j && j.config) this.onConfig(j.config); } catch (_) { /* 忽略 */ }
        }
        return;
      }
      if (ev.type === 'update') {
        if (this.onUpdate && ev.data) {
          try { const j = JSON.parse(ev.data); if (j && j.update) this.onUpdate(j.update); } catch (_) { /* 忽略 */ }
        }
        return;
      }
      if (ev.type !== 'message' || !ev.data) return;
      let env;
      try { env = JSON.parse(ev.data); } catch (_) { return; }
      try { this.onEnvelope(env); } catch (err) { console.warn('处理事件失败', err); } // eslint-disable-line no-console
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder('utf-8');
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (!alive()) break;
        if (done) break;
        this.armIdle(conn);
        parser.feed(dec.decode(value, { stream: true }));
        if (resetData) break;
      }
    } catch (_) {
      // 网络中断 / abort
    }
    try { reader.cancel(); } catch (_) { /* 忽略 */ }
    if (!alive()) return;
    if (resetData) {
      this.stop();
      if (this.onReset) this.onReset(resetData);
      return;
    }
    this.failed();
  }

  armIdle(conn) {
    clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => {
      if (this.stopped || this.conn !== conn) return;
      this.abort();
      this.failed();
    }, IDLE_TIMEOUT);
  }

  failed() {
    this.abort();
    if (this.stopped) return;
    this.onStatus('error');
    this.scheduleReconnect();
  }

  scheduleReconnect() {
    clearTimeout(this.timer);
    const delay = this.delay;
    this.delay = Math.min(this.delay * 2, MAX_DELAY);
    this.timer = setTimeout(async () => {
      if (this.stopped) return;
      let r = 'ok';
      try { r = await this.beforeReconnect(); } catch (_) { r = 'retry'; }
      if (this.stopped || r === 'stop') return;
      if (r === 'retry') { this.onStatus('error'); this.scheduleReconnect(); } else this.open();
    }, delay);
  }

  abort() {
    clearTimeout(this.idleTimer);
    this.conn++;
    if (this.ctrl) { try { this.ctrl.abort(); } catch (_) { /* 忽略 */ } }
    this.ctrl = null;
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.abort();
  }
}
