// 可视化公共上下文（docs/SPEC_V2_TUI.md §10）：本机会话判断、本机文件 stat（批量 + 缓存）、
// 预览面板入口、图片引用（ccr_ref）的取回。其它可视化模块（blobimg / lightbox / filelinks / preview / turnfiles）
// 都从这里拿 api 与回调，渲染核心（render.js / tools.js）只依赖这些模块导出的纯 DOM 函数。
//
// 本机会话 = jobid === 'local'（本机模式），或 fake 模式（fake claude 本来就在本机跑）；历史预览（hist:…）不是。
// 本机文件只能经后端 /api/sessions/<sid>/local/*（需 token、仅本机会话）与 /preview/<cap>/…（只读）访问。

import { TOKEN_HEADER, CREDENTIALS } from './api.js';

export const LOCAL_JOBID = 'local';

const ctx = {
  api: null,
  getConfig: () => ({}),
  toast: () => {},
  panel: null,            // PreviewPanel（preview.js），由 app.js 注入
  appPort: String(location.port || ''),
};

export function initViz(opts = {}) {
  if (opts.api) ctx.api = opts.api;
  if (opts.getConfig) ctx.getConfig = opts.getConfig;
  if (opts.toast) ctx.toast = opts.toast;
  if (opts.panel !== undefined) ctx.panel = opts.panel;
}

export function vizCtx() { return ctx; }
export function toast(text, level) { try { ctx.toast(text, level); } catch (_) { /* 忽略 */ } }

/** 子代理抽屉里的只读视图要回到所属会话 */
export function rootOf(s) { return (s && s.parent) || s; }

/** 本机会话（可以预览本机文件 / 在 Finder 中显示） */
export function isLocal(s) {
  const r = rootOf(s);
  if (!r || !r.meta || typeof r.sid !== 'string' || r.sid.startsWith('hist:')) return false;
  if (r.localOverride != null) return !!r.localOverride;   // 回放页用
  const cfg = (ctx.getConfig && ctx.getConfig()) || {};
  return r.meta.jobid === LOCAL_JOBID || !!cfg.fake;
}

/** 会话的绝对 cwd：transcript 条目自带的 cwd（绝对路径）优先，其次 meta.cwd（可能是 ~） */
export function sessionCwd(s) {
  const r = rootOf(s);
  return (r && (r.absCwd || (r.meta && r.meta.cwd))) || '';
}

// ---------------------------------------------------------------- 本机文件 stat（批量 + 缓存）

const STAT_BATCH_MS = 40;
const TTL_OK = 30000;
const TTL_MISS = 4000;
const statCache = new Map();     // sid + '\n' + path -> {at, res}
const pending = new Map();       // sid -> {paths:Set, waiters:Map(path -> [resolve…]), timer}

function statKey(sid, p) { return sid + '\n' + p; }

/** 查询本机文件信息：{exists, type, size, mtime, ok, kind, url, real, reason}；非本机会话 / 失败 → null */
export function statPath(s, path, { force = false } = {}) {
  const r = rootOf(s);
  if (!isLocal(r) || !path || !ctx.api) return Promise.resolve(null);
  const key = statKey(r.sid, path);
  const hit = statCache.get(key);
  if (!force && hit && Date.now() - hit.at < (hit.res && hit.res.exists ? TTL_OK : TTL_MISS)) return Promise.resolve(hit.res);
  return new Promise((resolve) => {
    let p = pending.get(r.sid);
    if (!p) { p = { paths: new Set(), waiters: new Map(), timer: 0, s: r }; pending.set(r.sid, p); }
    p.paths.add(path);
    if (!p.waiters.has(path)) p.waiters.set(path, []);
    p.waiters.get(path).push(resolve);
    if (!p.timer) p.timer = setTimeout(() => flushStat(r.sid), STAT_BATCH_MS);
  });
}

/** 批量强制查询（预览面板轮询 mtime 用，不走缓存） */
export async function statMany(s, paths) {
  const r = rootOf(s);
  if (!isLocal(r) || !ctx.api || !paths.length) return [];
  try {
    const res = await ctx.api.post(ctx.api.sessionPath(r.sid, '/local/stat'), { paths });
    const list = Array.isArray(res && res.results) ? res.results : [];
    for (const x of list) if (x && typeof x.path === 'string') statCache.set(statKey(r.sid, x.path), { at: Date.now(), res: x });
    return list;
  } catch (_) { return []; }
}

export const STAT_BATCH = 200;   // 后端 /local/stat 一次最多 200 个路径

async function flushStat(sid) {
  const p = pending.get(sid);
  if (!p) return;
  pending.delete(sid);
  const all = [...p.paths];
  const batch = all.slice(0, STAT_BATCH);
  const rest = all.slice(STAT_BATCH);
  if (rest.length) {
    // 超出一批的路径连同它们的等待者放回队列，下一批再查（不能先用 null 把它们解析掉）
    let q = pending.get(sid);
    if (!q) { q = { paths: new Set(), waiters: new Map(), timer: 0, s: p.s }; pending.set(sid, q); }
    for (const path of rest) {
      q.paths.add(path);
      q.waiters.set(path, (q.waiters.get(path) || []).concat(p.waiters.get(path) || []));
    }
    if (!q.timer) q.timer = setTimeout(() => flushStat(sid), 0);
  }
  const list = await statMany(p.s, batch);
  const byPath = new Map(list.map((x) => [x.path, x]));
  for (const path of batch) for (const w of p.waiters.get(path) || []) w(byPath.get(path) || null);
}

export function forgetStat(sid) {
  for (const k of [...statCache.keys()]) if (k.startsWith(sid + '\n')) statCache.delete(k);
}

/** 用户点击「在 Finder 中显示」/「用默认应用打开」（只在点击时调用） */
export async function localOpen(s, path, action) {
  const r = rootOf(s);
  if (!isLocal(r) || !ctx.api) return;
  try {
    await ctx.api.post(ctx.api.sessionPath(r.sid, '/local/open'), { path, action });
  } catch (e) {
    toast((action === 'reveal' ? '在 Finder 中显示失败：' : '打开失败：') + ((e && e.message) || e), 'error');
  }
}

// ---------------------------------------------------------------- 预览面板入口

/** 在右侧面板打开：{kind:'file', path} / {kind:'url', url} / {kind:'diff', path, patches} */
export function openPanel(s, target) {
  if (!ctx.panel) { toast('预览面板不可用', 'warn'); return; }
  ctx.panel.open(rootOf(s), target);
}

/** 打开右侧「改动」栏并定位（「本轮改动」卡片联动）：reveal = {toolId, path}；面板不支持时退回 fallback（旧的 diff 标签） */
export function openChanges(s, reveal, fallback) {
  if (ctx.panel && typeof ctx.panel.openChanges === 'function') { ctx.panel.openChanges(rootOf(s), reveal); return; }
  if (fallback) openPanel(s, fallback);
}

// ---------------------------------------------------------------- 图片引用（ccr_ref）

const blobUrls = new Map();      // sid + '\n' + ref -> Promise<objectURL>
const MAX_BLOB_URLS = 200;
export const BLOB_CONCURRENCY = 2;   // 同一时间最多取 2 张图（远端 agent 也只并发 2 个 blob，链路慢时不挤占按键/截屏）
let blobActive = 0;
const blobQueue = [];

/** 按并发上限排队执行 fn()（返回 Promise） */
export function limitBlob(fn) {
  return new Promise((resolve, reject) => {
    const run = () => {
      blobActive++;
      Promise.resolve().then(fn).then(resolve, reject).finally(() => {
        blobActive--;
        const next = blobQueue.shift();
        if (next) next();
      });
    };
    if (blobActive < BLOB_CONCURRENCY) run(); else blobQueue.push(run);
  });
}

/** 取回引用图片 → objectURL（带 token 头的 fetch；同一 ref 只取一次） */
export function blobUrl(s, ref) {
  const r = rootOf(s);
  const key = statKey(r.sid, ref);
  let p = blobUrls.get(key);
  if (p) return p;
  p = limitBlob(async () => {
    if (!ctx.api) throw new Error('未连接');
    const res = await fetch(ctx.api.sessionPath(r.sid, '/blob/' + encodeURIComponent(ref)), {
      headers: ctx.api.token ? { [TOKEN_HEADER]: ctx.api.token } : {}, credentials: CREDENTIALS,
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (_) { /* 忽略 */ }
      if (res.status === 401 && ctx.api.markAuthLost) ctx.api.markAuthLost(msg);
      throw new Error(msg);
    }
    const blob = await res.blob();
    if (!/^image\/(png|jpeg|gif|webp)$/.test(blob.type)) throw new Error('不支持的图片类型');
    return URL.createObjectURL(blob);
  });
  p.catch(() => { if (blobUrls.get(key) === p) blobUrls.delete(key); });   // 失败可重试
  blobUrls.set(key, p);
  if (blobUrls.size > MAX_BLOB_URLS) {
    const [k0, p0] = blobUrls.entries().next().value;
    blobUrls.delete(k0);
    p0.then((u) => setTimeout(() => URL.revokeObjectURL(u), 60000), () => {});
  }
  return p;
}

/** 测试/回放页：注入假的 blob 取回 */
export function _setBlobLoader(fn) { ctx.blobLoader = fn; }
