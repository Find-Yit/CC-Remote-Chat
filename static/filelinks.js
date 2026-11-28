// 文件路径识别与文件 chip（docs/SPEC_V2_TUI.md §10.3）。
// 识别规则移植自 T3 Code（MIT）packages/shared/src/markdownLinks.ts / fileLinks.ts（去掉 Windows 部分）：
//   - 行内代码：必须含 / 或带行号后缀（:12、:12:3、:12-20），末段要像文件（有扩展名或是 Makefile 这类无扩展名文件），
//     不像主机名（example.com/x）、不像版本号、不含省略段（…/ 或 .../）；带空格时只认绝对路径且每段都是普通名字；
//   - markdown 链接 [x](path)：不是外部 scheme、不是 #锚点，像文件路径；
//   - Bash 命令里的参数、Bash 输出里的绝对路径；
//   - 相对路径按会话 cwd 解析。
// 本机会话：chip 经 /local/stat 确认存在后可点 → 右侧预览面板；服务器会话：点击 → 右侧面板预览服务器上的文件
// （srvfiles.js，经 agent 只读取回；渲染时不发请求，点了才查）。
// 另外识别 http://localhost:PORT/… 与 http://127.0.0.1:PORT/…（排除本 app 自己的端口），本机会话可"在面板中预览"。
import { h, icon, copyText, basename } from './dom.js';
import { isLocal, statPath, openPanel, sessionCwd, toast, vizCtx, localOpen, rootOf } from './viz.js';
import { chatImage } from './blobimg.js';
import { isSrv } from './srvfiles.js';

const POSITION_SUFFIX = /:\d+(?::\d+|-\d+)?$/;
const POSITION_CAPTURE = /:(\d+)(?::(\d+)|-(\d+))?$/;
const POSITION_HASH = /^#L(\d+)(?:C(\d+)|-L?(\d+))?$/i;
const PATH_SEP = /[\\/]/;
const ELIDED = /(?:^|[\\/])(?:\.{3,}|…)(?:[\\/]|$)/;
const FILE_EXT = /\.[A-Za-z0-9_-]+$/;
const ANY_EXT = /\.[^./\\]+$/;
const VERSION_SUFFIX = /\d\.\d[^.]*$/;
const NUMERIC_DOTTED = /^\d+(?:\.\d+)+$/;
const REL_PREFIX = /^(~\/|\.{1,2}\/)/;
const EXTERNAL_SCHEME = /^([A-Za-z][A-Za-z0-9+.-]*):(.*)$/;
const POSITION_ONLY = /^\d+(?::\d+|-\d+)?$/;
const RELATIVE_FILE_PATH = /^(?:[\p{L}\p{M}\p{N}._$+@()[\]-]+(?: +[\p{L}\p{M}\p{N}._$+@()[\]-]+)*\/)+(?:[\p{L}\p{M}\p{N}._$+@()[\]-]+(?: +[\p{L}\p{M}\p{N}._$+@()[\]-]+)*(?::\d+(?::\d+|-\d+)?)?)?$/u;
const RELATIVE_FILE_NAME = /^[\p{L}\p{M}\p{N}._$+()[\]-]+(?: +[\p{L}\p{M}\p{N}._$+()[\]-]+)*\.[\p{L}\p{M}\p{N}_-]+(?::\d+(?::\d+|-\d+)?)?$/u;
const SPACED_ROOT = /^(?:\/|~\/)/;
const SPACED_BODY = /^(?:[\p{L}\p{M}\p{N}._$+@()[\]-]+(?: (?!-+\p{L})[\p{L}\p{M}\p{N}._$+@()[\]-]+)*(?:[\\/]|$))+$/u;

const POSIX_ROOTS = ['/Users/', '/home/', '/tmp/', '/var/', '/etc/', '/opt/', '/mnt/', '/Volumes/', '/private/', '/root/',
  '/usr/', '/srv/', '/media/', '/workspace/', '/workspaces/', '/scratch/', '/data/'];

export const EXTENSIONLESS = new Set(['Makefile', 'makefile', 'GNUmakefile', 'Dockerfile', 'Containerfile', 'Justfile',
  'justfile', 'Rakefile', 'Gemfile', 'Procfile', 'Brewfile', 'Caddyfile', 'Vagrantfile', 'Jenkinsfile', 'Podfile',
  'BUILD', 'WORKSPACE', 'LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'AUTHORS', 'CHANGELOG', 'README', 'CODEOWNERS']);

const GENERIC_TLDS = new Set(['com', 'net', 'org', 'io', 'dev', 'app', 'ai', 'co', 'edu', 'gov', 'mil', 'info', 'biz',
  'xyz', 'me', 'tv', 'gg', 'chat', 'cloud', 'site', 'online', 'tech', 'store', 'link']);
const COUNTRY_TLDS = new Set(['uk', 'de', 'fr', 'nl', 'se', 'no', 'fi', 'dk', 'pl', 'ch', 'at', 'be', 'es', 'it', 'pt',
  'eu', 'us', 'ca', 'au', 'nz', 'jp', 'kr', 'cn', 'br', 'ru', 'mx', 'ie', 'cz', 'tr', 'sg', 'hk']);

export const IMAGE_EXT = /\.(png|jpe?g|gif|webp|avif|bmp)$/i;
export const HTML_EXT = /\.html?$/i;

function looksLikeHostname(seg, hasPosition) {
  if (seg.startsWith('.')) return false;
  const low = seg.toLowerCase();
  if (low === 'localhost' || NUMERIC_DOTTED.test(seg)) return true;
  const labels = low.split('.');
  const last = labels[labels.length - 1];
  if (labels.length < 2 || !last) return false;
  if (GENERIC_TLDS.has(last)) return true;
  return !hasPosition && COUNTRY_TLDS.has(last);
}

/** 'src/a.ts:12:3' → {path:'src/a.ts', line:12, column:3}；'#L5-L9' 形式的锚点也认 */
export function splitPosition(path, hash = '') {
  const m = path.match(POSITION_CAPTURE) || hash.match(POSITION_HASH);
  if (!m || !m[1]) return { path };
  const fromSuffix = !!path.match(POSITION_CAPTURE);
  const line = parseInt(m[1], 10);
  const out = { path: fromSuffix ? path.slice(0, -m[0].length) : path };
  if (line > 0) out.line = line;
  if (m[2] !== undefined && +m[2] > 0) out.column = +m[2];
  if (line > 0 && m[3] !== undefined && +m[3] > line) out.endLine = +m[3];
  return out;
}

function isSpacedAbsolute(text) {
  const root = (SPACED_ROOT.exec(text) || [])[0];
  if (root === undefined) return false;
  const body = text.slice(root.length).replace(POSITION_SUFFIX, '');
  if (!SPACED_BODY.test(body)) return false;
  const firstSpace = body.indexOf(' ');
  const folders = body.slice(0, firstSpace).split(PATH_SEP).slice(0, -1);
  if (folders.some((f) => f === 'bin' || f === 'sbin')) return false;
  const spaceInFolder = PATH_SEP.test(body.slice(firstSpace));
  if (!spaceInFolder && root === '/' && !/^(?:Users|home)[\\/]/.test(body)) return false;
  if (PATH_SEP.test(body.slice(-1))) return true;
  const base = body.split(PATH_SEP).pop() || '';
  return FILE_EXT.test(base) || EXTENSIONLESS.has(base);
}

/** 行内代码是否像文件路径：返回候选（可能带行号后缀）或 null */
export function inlineCodePath(code) {
  const t = String(code ?? '').trim();
  if (!t || t.includes('`') || t.length > 1024) return null;
  if (/\s/.test(t) && !isSpacedAbsolute(t)) return null;
  const c = t;
  const hasPos = POSITION_SUFFIX.test(c);
  if (!hasPos && !PATH_SEP.test(c)) return null;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(c)) return null;          // URL
  if (ELIDED.test(c.replace(POSITION_SUFFIX, ''))) return null;
  const explicit = REL_PREFIX.test(c) || c.startsWith('/');
  if (!explicit) {
    const wo = c.replace(POSITION_SUFFIX, '');
    if (wo.endsWith('/')) return null;
    const first = wo.split('/')[0] || wo;
    if (looksLikeHostname(first, hasPos)) return null;
    const base = wo.replace(/[/\\]+$/, '').split(/[\\/]/).pop() || '';
    if (VERSION_SUFFIX.test(base)) return null;
    if (!FILE_EXT.test(base)) {
      if (!hasPos) return EXTENSIONLESS.has(base) ? c : null;
      if (wo.includes('/') && !ANY_EXT.test(base) && !EXTENSIONLESS.has(base)) return null;
    }
  } else if (c.startsWith('/')) {
    // 绝对路径：在常见根目录下，或末段像文件
    const wo = c.replace(POSITION_SUFFIX, '');
    const base = wo.slice(wo.lastIndexOf('/') + 1);
    if (!POSIX_ROOTS.some((r) => wo.startsWith(r)) && !hasPos && !FILE_EXT.test(base) && !EXTENSIONLESS.has(base)) return null;
    if (wo === '/' || wo.split('/').filter(Boolean).length < 2 && !FILE_EXT.test(base)) return null;
  }
  return c;
}

function looksLikeFilePath(path, authored) {
  if (REL_PREFIX.test(path)) return true;
  if (path.startsWith('/')) {
    if (POSIX_ROOTS.some((p) => path.startsWith(p))) return true;
    if (POSITION_SUFFIX.test(authored)) return true;
    const base = path.slice(path.lastIndexOf('/') + 1);
    return EXTENSIONLESS.has(base) || FILE_EXT.test(base);
  }
  if (EXTENSIONLESS.has(path)) return true;
  return RELATIVE_FILE_PATH.test(authored) || RELATIVE_FILE_NAME.test(path);
}

function safeDecode(v) { try { return decodeURIComponent(v); } catch (_) { return v; } }

/** markdown 链接目标是否是文件：{path, line, column, endLine} 或 null（外部链接、锚点等） */
export function markdownLinkPath(href) {
  let s = String(href ?? '').trim();
  if (s.startsWith('<') && s.endsWith('>')) s = s.slice(1, -1).trim();
  if (!s || s.startsWith('#') || s.startsWith('//')) return null;
  if (/^file:\/\//i.test(s)) {
    try { const u = new URL(s); if (u.hostname && u.hostname !== 'localhost') return null; s = u.pathname + u.hash; } catch (_) { return null; }
  }
  const hi = s.indexOf('#');
  const hash = hi >= 0 ? s.slice(hi) : '';
  let path = hi >= 0 ? s.slice(0, hi) : s;
  const qi = path.indexOf('?');
  if (qi >= 0) path = path.slice(0, qi);
  path = safeDecode(path.trim());
  if (!path) return null;
  const m = path.match(EXTERNAL_SCHEME);
  if (m && (m[2].startsWith('//') || !POSITION_ONLY.test(m[2]))) return null;
  const pos = splitPosition(path, safeDecode(hash));
  if (ELIDED.test(pos.path) || !looksLikeFilePath(pos.path, path)) return null;
  return pos;
}

/** Bash 命令里像文件路径的参数（去掉引号、重定向、--opt= 前缀） */
export function bashPaths(command) {
  const out = [];
  const seen = new Set();
  const toks = String(command ?? '').slice(0, 20000).match(/"(?:[^"\\]|\\.)*"|'[^']*'|[^\s;|&()<>]+/g) || [];
  for (let t of toks) {
    if (t.startsWith('"') || t.startsWith("'")) t = t.slice(1, -1);
    t = t.replace(/^\d?>>?|^</, '');
    if (/^-/.test(t)) { const i = t.indexOf('='); if (i < 0) continue; t = t.slice(i + 1); }
    else if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(t)) t = t.slice(t.indexOf('=') + 1);
    if (!t || /[$`*?{}]/.test(t)) continue;
    const c = inlineCodePath(t);
    if (!c) continue;
    const p = splitPosition(c).path;
    if (!seen.has(p)) { seen.add(p); out.push(p); }
  }
  return out;
}

/** 普通文本（Bash 输出）里的绝对路径（必须在常见根目录下且末段像文件，或带行号） */
export function textPaths(text) {
  const out = [];
  const seen = new Set();
  const src = String(text ?? '').slice(0, 60000);
  for (const m of src.matchAll(/(^|[\s(["'=:])((?:~|\/)[^\s<>"'`]*\/[^\s<>"'`]+)/g)) {
    let p = m[2].replace(/[.,;:!?)\]]+$/, '');
    if (p.startsWith('~') && !p.startsWith('~/')) continue;
    const pos = splitPosition(p);
    const base = pos.path.slice(pos.path.lastIndexOf('/') + 1);
    if (!p.startsWith('~/') && !POSIX_ROOTS.some((r) => pos.path.startsWith(r)) && !pos.line) continue;
    if (!FILE_EXT.test(base) && !EXTENSIONLESS.has(base) && !pos.line) continue;
    if (ELIDED.test(pos.path) || seen.has(pos.path)) continue;
    seen.add(pos.path);
    out.push(pos.path);
  }
  return out;
}

/** http://localhost:PORT/… 与 http://127.0.0.1:PORT/…（排除本 app 自己的端口） */
export function localhostUrls(text) {
  const out = [];
  const seen = new Set();
  for (const m of String(text ?? '').slice(0, 60000).matchAll(/https?:\/\/(?:localhost|127\.0\.0\.1)(?::\d{1,5})?(?:\/[^\s<>"'`）】」]*)?/g)) {
    const u = m[0].replace(/[.,;:!?)\]]+$/, '');
    if (!isPreviewableLocalUrl(u) || seen.has(u)) continue;
    seen.add(u);
    out.push(u);
  }
  return out;
}

export function isPreviewableLocalUrl(u) {
  try {
    const x = new URL(u);
    if (x.protocol !== 'http:' && x.protocol !== 'https:') return false;
    if (x.hostname !== 'localhost' && x.hostname !== '127.0.0.1') return false;
    const port = x.port || (x.protocol === 'https:' ? '443' : '80');
    return port !== String(vizCtx().appPort || location.port || '');
  } catch (_) { return false; }
}

// ---------------------------------------------------------------- 路径解析

function normalize(p) {
  const abs = p.startsWith('/');
  const parts = [];
  for (const seg of p.split('/')) {
    if (!seg || seg === '.') continue;
    if (seg === '..') { if (parts.length && parts[parts.length - 1] !== '..') parts.pop(); else if (!abs) parts.push('..'); continue; }
    parts.push(seg);
  }
  return (abs ? '/' : '') + parts.join('/');
}

/** 相对路径按 cwd 解析（cwd 也可以是 ~ 开头，交给后端展开） */
export function resolvePath(raw, cwd) {
  let p = String(raw ?? '').trim();
  if (!p) return '';
  if (p.startsWith('/')) return normalize(p);
  if (p === '~' || p.startsWith('~/')) return '~' + (normalize(p.slice(1)) || '');
  const base = String(cwd || '').replace(/\/+$/, '');
  if (!base) return p;
  if (base === '~' || base.startsWith('~/')) return '~' + normalize(base.slice(1) + '/' + p);
  return normalize(base + '/' + p);
}

/** 显示用：在 cwd 下的路径显示相对路径 */
export function displayPath(abs, cwd) {
  const c = String(cwd || '').replace(/\/+$/, '');
  if (c && c !== '/' && abs.startsWith(c + '/')) return abs.slice(c.length + 1);
  return abs;
}

// ---------------------------------------------------------------- chip

/**
 * 文件 chip。本机会话：stat 确认存在 → 点击在右侧面板预览（目录 → 在 Finder 中显示）；不存在 → 灰色，点击复制路径。
 * 服务器会话：点击 → 右侧面板预览服务器文件（srvfiles.js）。opts.code = 渲染成行内代码样式（markdown 行内代码）；opts.label = 显示文字。
 */
export function fileChip(s, raw, opts = {}) {
  const pos = splitPosition(String(raw));
  const cwd = sessionCwd(s);
  const abs = resolvePath(pos.path, cwd);
  const local = isLocal(s);
  const srv = !local && isSrv(s);   // 服务器会话：点了才经 agent 查 / 取（§10.10）
  let label = opts.label != null ? opts.label : opts.code ? String(raw) : displayPath(abs, cwd) + (pos.line ? ':' + pos.line : '');
  let ic = opts.icon || 'file';
  if (opts.tool) { label = local || srv ? '预览' : '复制路径'; ic = local || srv ? 'eye' : 'copy'; }   // 工具卡标题行右侧的小 chip
  const el = h('button', { class: 'fchip' + (opts.code ? ' md-inline code' : '') + (local ? ' local checking' : srv ? ' remote srv' : ' remote') + (opts.cls ? ' ' + opts.cls : ''),
    type: 'button', title: local ? '检查文件…' : srv ? '在右侧面板预览服务器上的文件：' + abs : '复制路径：' + abs },
  opts.code ? null : icon(ic), label ? h('span', { class: 'fchip-t' }, label) : null);
  el.dataset.path = abs;
  let info = null;
  if (local) {
    statPath(s, abs).then((r) => {
      info = r;
      el.classList.remove('checking');
      if (r && r.exists && r.type === 'file' && r.ok) {
        el.classList.add('ok');
        el.title = '在右侧面板预览：' + (r.real || abs);
      } else if (r && r.exists && r.type === 'dir') {
        el.classList.add('ok', 'dir');
        el.title = '在 Finder 中显示：' + (r.real || abs);
      } else {
        el.classList.add('missing');
        el.title = (r && r.reason ? r.reason : '文件不存在') + '（点击复制路径）：' + abs;
      }
    });
  }
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (srv) { openPanel(s, { kind: 'file', path: abs, line: pos.line || null }); return; }
    if (local && info && info.exists && info.type === 'file' && info.ok) {
      openPanel(s, { kind: 'file', path: info.real || abs, line: pos.line || null, info });
      return;
    }
    if (local && info && info.exists && info.type === 'dir') { localOpen(s, info.real || abs, 'reveal'); return; }
    const full = info && info.real ? info.real : abs;
    copyText(full).then((ok) => toast(ok ? '已复制路径：' + full : '复制失败'));
  });
  return el;
}

/** localhost 链接旁的「在面板中预览」按钮（只在本机会话） */
export function urlPreviewButton(s, url) {
  if (!isLocal(s) || !isPreviewableLocalUrl(url)) return null;
  const b = h('button', { class: 'url-pv', type: 'button', title: '在右侧面板中预览 ' + url }, icon('eye'), '预览');
  b.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); openPanel(s, { kind: 'url', url }); });
  return b;
}

/** markdown 渲染上下文（markdown.js 的钩子）：行内代码路径 / 文件链接 / 本机图片 / localhost 链接 */
export function markdownCtx(s) {
  if (!s) return null;
  return {
    code(text) {
      const c = inlineCodePath(text);
      return c ? fileChip(s, c, { code: true }) : null;
    },
    link(raw, labelText, isImage) {
      const pos = markdownLinkPath(raw);
      if (!pos) return null;
      const target = pos.path + (pos.line ? ':' + pos.line : '');
      if (isImage && IMAGE_EXT.test(pos.path) && isLocal(s)) return localImage(s, pos.path, labelText);
      return fileChip(s, target, { label: labelText || undefined, icon: isImage ? 'image' : 'file' });
    },
    url(href, node) {
      const b = urlPreviewButton(s, href);
      if (!b) return null;
      return h('span', { class: 'url-wrap' }, node, b);
    },
  };
}

/** 本机图片文件（markdown 图片 / 本轮新图片）：stat 后用 /preview 地址显示缩略图，点击灯箱 */
export function localImage(s, rawPath, alt) {
  const cwd = sessionCwd(s);
  const abs = resolvePath(rawPath, cwd);
  const box = h('span', { class: 'limg' }, h('span', { class: 'cimg-why' }, alt || basename(abs)));
  statPath(s, abs).then((r) => {
    if (!(r && r.ok && r.url && r.kind === 'image')) {
      box.replaceWith(fileChip(s, abs, { label: alt || undefined, icon: 'image' }));
      return;
    }
    const img = chatImage(rootOf(s), null, { desc: { s: rootOf(s), url: r.url, name: basename(r.real || abs), path: r.real || abs }, alt: alt || basename(abs), cls: 'md-img' });
    box.replaceChildren(img);
  });
  return box;
}
