// 输入框草稿持久化（SPEC §7「输入框：草稿」）：按会话 sid 存进 localStorage，退出 app / 重启后端 / ⌘R 都不丢。
// - 键 `ccr.draft.<sid>` = {"t": 文字, "at": 最后一次输入的时刻(ms), "w": 写入的窗口 id}；空草稿 = 删掉这个键。
// - 输入后 400ms 写一次（同一会话合并），页面隐藏 / 关闭时立即写出没写的；会话删除时删掉；
//   每条最多 MAX_CHARS 字（超过就不保存，提示一次）、最多 MAX_ENTRIES 条（多了先删最旧的；存储满了从最旧的删起直到写得进去）。
// - 多窗口同时开着同一会话：每个窗口只写自己真正输入过的内容，写之前看一眼存储里的时刻，比自己新就不覆盖（旧窗口关闭时
//   不会把别的窗口刚打的字盖掉）；别的窗口写了更新的草稿 → storage 事件 → onExternal（调用方决定要不要放进输入框：
//   本窗口还有没写出去的输入、或正在打字时不放，以最后输入的为准）。
// - 以前存在 sessionStorage 'ccr.drafts' 里的草稿（只在刷新时保留）第一次载入时搬过来。
export const PREFIX = 'ccr.draft.';
export const MAX_CHARS = 100000;
export const MAX_ENTRIES = 200;
export const DEBOUNCE_MS = 400;
export const GC_AGE_MS = 10 * 60 * 1000;
const LEGACY_KEY = 'ccr.drafts';

function parse(raw) {
  if (typeof raw !== 'string' || !raw) return null;
  try {
    const d = JSON.parse(raw);
    if (d && typeof d === 'object' && typeof d.t === 'string') return { t: d.t, at: Number(d.at) || 0, w: typeof d.w === 'string' ? d.w : '' };
  } catch (_) { /* 坏数据当没有 */ }
  return null;
}

export class DraftStore {
  constructor({ storage, legacy = null, now = () => Date.now(), windowId = null, maxChars = MAX_CHARS, maxEntries = MAX_ENTRIES,
    debounceMs = DEBOUNCE_MS, setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (t) => clearTimeout(t),
    onTooBig = null } = {}) {
    this.storage = storage || null;
    this.now = now;
    this.windowId = windowId || Math.random().toString(36).slice(2, 10);
    this.maxChars = maxChars;
    this.maxEntries = maxEntries;
    this.debounceMs = debounceMs;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.onTooBig = onTooBig;
    this.pending = new Map();   // sid -> {text, at, timer}
    this.tooBig = new Set();
    this.listeners = [];
    if (legacy) this.migrate(legacy);
  }

  key(sid) { return PREFIX + sid; }

  /** 存储里的草稿 {t, at, w} | null */
  load(sid) {
    if (!this.storage || !sid) return null;
    try { return parse(this.storage.getItem(this.key(sid))); } catch (_) { return null; }
  }

  /** 本窗口对这个会话还有没写出去的输入 */
  isPending(sid) { return this.pending.has(sid); }

  /** 用户输入了（at = 输入时刻）：合并 debounceMs 后写出 */
  queue(sid, text, at = this.now()) {
    if (!sid) return;
    const p = this.pending.get(sid);
    if (p) this.clearTimer(p.timer);
    const e = { text: String(text ?? ''), at, timer: null };
    e.timer = this.setTimer(() => this.write(sid), this.debounceMs);
    this.pending.set(sid, e);
  }

  /** 立即写出（发送清空、页面隐藏时） */
  write(sid) {
    const p = this.pending.get(sid);
    if (!p) return;
    this.clearTimer(p.timer);
    this.pending.delete(sid);
    this.put(sid, p.text, p.at);
  }

  flush() { for (const sid of [...this.pending.keys()]) this.write(sid); }

  put(sid, text, at) {
    if (!this.storage) return;
    const k = this.key(sid);
    const cur = this.load(sid);
    if (cur && cur.at > at) return;   // 别的窗口在更晚的时刻输入过：以它为准，不覆盖
    try {
      if (!text) { this.storage.removeItem(k); this.tooBig.delete(sid); return; }
      if (text.length > this.maxChars) {
        this.storage.removeItem(k);   // 旧的那份已经过时，留着反而会在重启后恢复成旧内容
        if (!this.tooBig.has(sid)) { this.tooBig.add(sid); if (this.onTooBig) this.onTooBig(sid, text.length); }
        return;
      }
      this.tooBig.delete(sid);
      const val = JSON.stringify({ t: text, at, w: this.windowId });
      try { this.storage.setItem(k, val); } catch (_) {
        // 存储满：从最旧的草稿删起，直到写得进去（实在写不进去就放弃，内存里的草稿还在）
        let ok = false;
        for (const e of this.entries()) {
          if (e.sid === sid) continue;
          this.storage.removeItem(this.key(e.sid));
          try { this.storage.setItem(k, val); ok = true; break; } catch (_e) { /* 继续删 */ }
        }
        if (!ok) return;
      }
      this.prune(this.maxEntries, sid);
    } catch (_) { /* localStorage 不可用 / 仍然满：放弃（内存里的草稿还在） */ }
  }

  /** 会话删除：删掉它的草稿（以及本窗口还没写出去的） */
  remove(sid) {
    const p = this.pending.get(sid);
    if (p) { this.clearTimer(p.timer); this.pending.delete(sid); }
    this.tooBig.delete(sid);
    try { if (this.storage) this.storage.removeItem(this.key(sid)); } catch (_) { /* 忽略 */ }
  }

  /** 所有草稿 [{sid, at}]（按时刻从旧到新） */
  entries() {
    const out = [];
    if (!this.storage) return out;
    try {
      for (let i = 0; i < this.storage.length; i++) {
        const k = this.storage.key(i);
        if (!k || !k.startsWith(PREFIX)) continue;
        const d = parse(this.storage.getItem(k));
        out.push({ sid: k.slice(PREFIX.length), at: d ? d.at : 0 });
      }
    } catch (_) { /* 忽略 */ }
    return out.sort((a, b) => a.at - b.at);
  }

  prune(limit, keep) {
    const all = this.entries();
    let extra = all.length - Math.max(0, limit);
    for (const e of all) {
      if (extra <= 0) break;
      if (e.sid === keep) continue;
      try { this.storage.removeItem(this.key(e.sid)); } catch (_) { /* 忽略 */ }
      extra--;
    }
  }

  /** 载入会话列表后：不在列表里、而且 GC_AGE_MS 内没人动过的草稿删掉（会话在别处被删了） */
  gc(liveSids, olderThan = GC_AGE_MS) {
    const live = new Set(liveSids || []);
    const t = this.now();
    for (const e of this.entries()) {
      if (live.has(e.sid) || this.pending.has(e.sid) || t - e.at < olderThan) continue;
      try { this.storage.removeItem(this.key(e.sid)); } catch (_) { /* 忽略 */ }
    }
  }

  /** 以前的 sessionStorage 草稿（{sid: text}）搬进来：只补存储里还没有的，然后删掉旧键 */
  migrate(legacy) {
    try {
      const raw = legacy.getItem(LEGACY_KEY);
      if (!raw) return;
      legacy.removeItem(LEGACY_KEY);
      const old = JSON.parse(raw) || {};
      for (const [sid, text] of Object.entries(old)) {
        if (typeof text === 'string' && text && !this.load(sid)) this.put(sid, text, this.now());
      }
    } catch (_) { /* 忽略 */ }
  }

  /** 别的窗口改了草稿：cb(sid, {t, at, w} | null)。返回取消函数。win = 监听 storage 事件的 window */
  onExternal(cb, win = typeof window !== 'undefined' ? window : null) {
    if (!win) return () => {};
    const fn = (e) => {
      if (e.storageArea && this.storage && e.storageArea !== this.storage) return;
      if (typeof e.key !== 'string' || !e.key.startsWith(PREFIX)) return;
      const d = parse(e.newValue);
      if (d && d.w === this.windowId) return;
      cb(e.key.slice(PREFIX.length), d);
    };
    win.addEventListener('storage', fn);
    return () => win.removeEventListener('storage', fn);
  }
}
