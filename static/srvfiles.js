// 服务器会话的文件预览与下载（docs/SPEC_V2_TUI.md §10.10）。
// 文件只经后端 POST /api/sessions/<sid>/srvfile/stat（token）与 /preview/<cap>/…（只读，后端再经该作业的 node agent
// 只读 op 取回）访问；全部只在用户点击时发生（文件 chip、工具卡「预览」、「改动」栏「打开文件」、标签里的「刷新 / 下载」），
// 渲染聊天记录时不发任何请求。预览上限 20MB、下载上限 512MB（后端 ccr/srvfiles.py）；
// 远端慢 / 断线 / 作业结束时面板里显示「正在从服务器读取…」与失败原因 +「重试」。
import { h, icon, copyText, basename, fmtSize } from './dom.js';
import { CREDENTIALS } from './api.js';
import { vizCtx, toast, rootOf, isLocal } from './viz.js';
import { errorText } from './textdecode.js';

export const SRV_POLL_MS = 5000;          // 打开着的服务器文件标签多久查一次 mtime/size（本机是 1.5s）
export const SRV_TEXT_HEAD = 2 * 1024 * 1024;   // 文本只取开头这么多字节（面板最多显示 2MB）
const TTL_OK = 5000;
const statCache = new Map();             // sid + '\n' + path -> {at, res}

/** 服务器会话（可以经 agent 预览服务器上的文件）：不是本机会话、不是历史预览、有 jobid */
export function isSrv(s) {
  const r = rootOf(s);
  if (!r || !r.meta || typeof r.sid !== 'string' || r.sid.startsWith('hist:')) return false;
  if (r.localOverride != null) return r.localOverride === false;   // 回放页用
  return !isLocal(r) && typeof r.meta.jobid === 'string' && !!r.meta.jobid;
}

/** 这个会话的文件能不能在右侧面板打开（本机文件或服务器文件） */
export function canOpenFiles(s) { return isLocal(s) || isSrv(s); }

class SrvError extends Error {
  constructor(message, status) { super(message); this.status = status || 0; }
}

/** 批量查询服务器文件：[{path, abs, real, exists, type, size, mtime, ok, kind, url, download_url, too_large, downloadable, reason}]；
 *  链路问题抛 SrvError（message 是后端的中文说明，如「与计算节点的连接已断开，正在重连…」） */
export async function srvStatMany(s, paths) {
  const r = rootOf(s);
  const api = vizCtx().api;
  if (!isSrv(r) || !api) throw new SrvError('不是服务器会话');
  let res;
  try {
    res = await api.post(api.sessionPath(r.sid, '/srvfile/stat'), { paths });
  } catch (e) {
    throw new SrvError((e && e.message) || String(e), e && e.status);
  }
  const list = Array.isArray(res && res.results) ? res.results : [];
  for (const x of list) if (x && typeof x.path === 'string') statCache.set(r.sid + '\n' + x.path, { at: Date.now(), res: x });
  return list;
}

/** 单个路径（force=false 时 5 秒内复用结果） */
export async function srvStat(s, path, { force = false } = {}) {
  const r = rootOf(s);
  const hit = statCache.get((r && r.sid) + '\n' + path);
  if (!force && hit && hit.res && hit.res.exists && Date.now() - hit.at < TTL_OK) return hit.res;
  const [x] = await srvStatMany(s, [path]);
  return x || null;
}

/** 取回 /preview 地址的内容（检查是否成功；失败抛 SrvError，带后端的中文原因） */
async function get(url) {
  let res;
  try {
    res = await fetch(url, { credentials: CREDENTIALS, cache: 'no-store' });
  } catch (_) {
    throw new SrvError('无法连接本地服务（应用可能已退出）');
  }
  if (!res.ok) throw new SrvError(await errorText(res), res.status);
  return res;
}

/** 图片：取回 → objectURL（失败能显示原因并重试，而不是一张破图） */
export async function srvObjectUrl(url) {
  const res = await get(url);
  return URL.createObjectURL(await res.blob());
}

/** HTML / PDF / 音视频：先取一次确认读得到（后端会缓存，iframe 再按同一地址加载时不再从服务器传内容） */
export async function srvProbe(url) { await get(url); }

// ---------------------------------------------------------------- 下载（只在用户点击时）
const anchorDownload = (url, name) => {
  const a = h('a', { href: url, download: name || '', rel: 'noopener', hidden: true });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 0);
};
let downloader = anchorDownload;
/** 测试用：替换真正触发下载的函数（null = 恢复） */
export function _setDownloader(fn) { downloader = fn || anchorDownload; }

/** 「下载」：先重新查一次（文件可能变了 / 链路可能断了），再用 <a download> 交给浏览器 / 原生窗口的下载处理
 *  （后端 Content-Disposition: attachment；原生窗口保存到「下载」文件夹、重名加 (1)） */
export async function srvDownload(s, path) {
  let r;
  try {
    r = await srvStat(s, path, { force: true });
  } catch (e) {
    toast('下载失败：' + ((e && e.message) || e), 'error');
    return false;
  }
  if (!r || !r.exists || r.type !== 'file') { toast('下载失败：' + ((r && r.reason) || '文件不存在'), 'error'); return false; }
  if (!r.ok) { toast('下载失败：' + (r.reason || '无法读取'), 'error'); return false; }
  if (!r.downloadable || !r.download_url) { toast('文件过大（' + fmtSize(r.size) + '），超过下载上限 512MB，请用 scp / rsync', 'warn'); return false; }
  const name = basename(r.real || path);
  downloader(r.download_url, name);
  toast('开始下载 ' + name + '（' + fmtSize(r.size) + '）· 从服务器传回，保存到「下载」文件夹');
  return true;
}

// ---------------------------------------------------------------- 面板里的状态块
export function loadingBox(text, path) {
  return h('div', { class: 'pv-loading pv-srv-loading' }, h('span', { class: 'spin', 'aria-hidden': 'true' }),
    h('div', null, text || '正在从服务器读取…'), path ? h('div', { class: 'muted mono small-text' }, path) : null);
}

/** 失败：原因 + 「重试」（+ 可选的其它按钮） */
export function errorBox(msg, path, onRetry, extra = []) {
  const btns = [];
  if (onRetry) {
    const b = h('button', { class: 'btn small pv-srv-btn pv-retry', type: 'button' }, icon('refresh'), '重试');
    b.addEventListener('click', () => onRetry());
    btns.push(b);
  }
  return h('div', { class: 'pv-empty pv-srv-err' }, icon('server'), h('div', { class: 'pv-err-msg' }, msg),
    path ? h('div', { class: 'muted mono small-text' }, path) : null,
    btns.length || extra.length ? h('div', { class: 'pv-err-acts' }, ...btns, ...extra) : null);
}

export function downloadButton(s, path, label = '下载到本机') {
  const b = h('button', { class: 'btn small pv-srv-btn pv-dl', type: 'button', title: '从服务器下载到本机的「下载」文件夹' }, icon('download'), label);
  b.addEventListener('click', () => srvDownload(s, path));
  return b;
}

export function copyButton(path) {
  const b = h('button', { class: 'btn small ghost pv-srv-btn', type: 'button' }, icon('copy'), '复制路径');
  b.addEventListener('click', () => copyText(path).then((ok) => toast(ok ? '已复制：' + path : '复制失败')));
  return b;
}

/** 超过预览上限 / 二进制：说明 + 下载 */
export function noPreviewBox(s, info, msg) {
  const extra = [];
  if (info && info.downloadable) extra.push(downloadButton(s, info.real || info.path));
  else if (info && info.size) extra.push(h('div', { class: 'muted small-text' }, '超过下载上限 512MB，请用 scp / rsync'));
  return h('div', { class: 'pv-empty pv-srv-big' }, icon('file'), h('div', null, msg),
    info && info.size != null ? h('div', { class: 'muted' }, fmtSize(info.size)) : null, ...extra);
}

/**
 * 预览面板里服务器文件标签的加载（preview.js 的 loadFile 交过来）：查文件 → 失败 / 不存在 / 目录 / 过大 各有说明，
 * 能预览就交回 panel.fillFile。panel = PreviewPanel（只用 renderBar / current / fillFile / reload）。
 */
export async function srvLoad(panel, s, tab, box) {
  box.replaceChildren(loadingBox('正在从服务器读取文件信息…', tab.path));
  let r;
  try {
    r = await srvStat(s, tab.path, { force: true });
  } catch (e) {
    box.replaceChildren(errorBox('无法读取服务器文件：' + ((e && e.message) || e), tab.path, () => panel.reload(tab, true)));
    return;
  }
  if (!r || !r.exists) {
    box.replaceChildren(errorBox((r && r.reason) || '文件不存在', tab.path, () => panel.reload(tab, true), [copyButton(tab.path)]));
    return;
  }
  if (r.type === 'dir') {
    box.replaceChildren(h('div', { class: 'pv-empty' }, icon('folder'), h('div', null, '这是服务器上的目录（不列目录）'),
      h('div', { class: 'muted mono small-text' }, r.real || tab.path), copyButton(r.real || tab.path)));
    return;
  }
  if (!r.ok) { box.replaceChildren(errorBox(r.reason || '无法读取', tab.path, () => panel.reload(tab, true))); return; }
  tab.info = r;
  tab.mtime = r.mtime;
  tab.size = r.size;
  tab.path = r.real || tab.path;
  if (panel.current().tab === tab) panel.renderBar(s, tab);
  if (r.too_large) { box.replaceChildren(noPreviewBox(s, r, r.reason || '文件较大，超过服务器文件预览上限 20MB')); return; }
  await panel.fillFile(s, tab, box);
}
