// 受控 DOM 构造工具：所有文本一律经 createTextNode / textContent 写入，绝不拼接 innerHTML。

const SVG_NS = 'http://www.w3.org/2000/svg';

function appendChildren(el, children) {
  for (const c of children) {
    if (c == null || c === false || c === true) continue;
    if (Array.isArray(c)) appendChildren(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

/**
 * h('div', {class:'x', onclick: fn, title: '...'}, child, 'text', [more])
 * 特殊 props：class、text、style(对象)、dataset、on*(函数)、value/checked/disabled/hidden 等走 property。
 */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  let deferredValue;
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = String(v);
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'value') deferredValue = v;
      else if (k === 'checked' || k === 'disabled' || k === 'hidden' || k === 'selected' ||
               k === 'multiple' || k === 'readOnly' || k === 'open') el[k] = !!v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  appendChildren(el, children);
  if (deferredValue !== undefined) el.value = deferredValue;
  return el;
}

export function clear(el) { el.replaceChildren(); return el; }

// 固定图标路径（常量，非外部资源）
const ICONS = {
  chevron: 'M6 4l4 4-4 4',
  chevdown: 'M4 6l4 4 4-4',
  chevup: 'M4 10l4-4 4 4',
  check: 'M3.5 8.5l3 3 6-7',
  x: 'M4.5 4.5l7 7M11.5 4.5l-7 7',
  terminal: 'M2.5 3.5h11v9h-11z M5 6.5l2 1.5-2 1.5 M8.5 10h2.5',
  file: 'M4 2h5l3 3v9H4z M9 2v3h3',
  edit: 'M10.5 2.5l3 3-7.5 7.5H3v-3z',
  search: 'M7 2.5a4.5 4.5 0 1 0 0 9a4.5 4.5 0 1 0 0-9z M10.4 10.4l3.1 3.1',
  globe: 'M8 2a6 6 0 1 0 0 12A6 6 0 1 0 8 2z M2 8h12 M8 2c2.2 2.2 2.2 9.8 0 12 M8 2c-2.2 2.2-2.2 9.8 0 12',
  list: 'M6 4h7.5 M6 8h7.5 M6 12h7.5 M2.5 4h1 M2.5 8h1 M2.5 12h1',
  agent: 'M8 1.8l1.6 3.6 3.6 1.6-3.6 1.6L8 12.2 6.4 8.6 2.8 7l3.6-1.6z M12.5 11.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z',
  tool: 'M9.8 2.2a3.4 3.4 0 0 0-3.4 4.5L2.3 10.8v2.9h2.9l4.1-4.1a3.4 3.4 0 0 0 4.5-3.4l-2 2-2-.5-.5-2z',
  question: 'M8 2a6 6 0 1 0 0 12A6 6 0 1 0 8 2z M6.2 6.3a1.9 1.9 0 1 1 2.7 1.7c-.6.3-.9.7-.9 1.3v.3 M8 11.4v.2',
  stop: 'M4.5 4.5h7v7h-7z',
  send: 'M8 13V3.5 M3.8 7.5L8 3.3l4.2 4.2',
  clip: 'M11.3 6.8l-4.7 4.7a2 2 0 0 1-2.8-2.8l5.3-5.3a3 3 0 0 1 4.2 4.2l-5.6 5.6',
  plus: 'M8 3v10 M3 8h10',
  folder: 'M2 4.2h4.2l1.5 1.6H14v6.7H2z',
  refresh: 'M13.2 8a5.2 5.2 0 1 1-1.6-3.7 M13.2 2.6v3.1h-3.1',
  ban: 'M8 2a6 6 0 1 0 0 12A6 6 0 1 0 8 2z M3.8 3.8l8.4 8.4',
  spark: 'M8 2.2v2.6 M8 11.2v2.6 M2.2 8h2.6 M11.2 8h2.6 M3.9 3.9l1.8 1.8 M10.3 10.3l1.8 1.8 M3.9 12.1l1.8-1.8 M10.3 5.7l1.8-1.8',
  copy: 'M5.5 5.5h7.5v8H5.5z M3 10.5V2.5h7.5',
  shield: 'M8 1.8l5 2v4.1c0 3-2.1 5.3-5 6.3-2.9-1-5-3.3-5-6.3V3.8z',
  clock: 'M8 2a6 6 0 1 0 0 12A6 6 0 1 0 8 2z M8 4.8V8l2.2 1.4',
  chat: 'M2.5 3.5h11v7.5h-6.2l-3 2.5V11H2.5z',
  trash: 'M3 4.5h10 M6.5 4.5V3h3v1.5 M4.5 4.5l.6 9h5.8l.6-9',
  power: 'M8 2.2v5.5 M4.6 4.4a5 5 0 1 0 6.8 0',
  menu: 'M2.5 4h11 M2.5 8h11 M2.5 12h11',
  up: 'M8 13V3.5 M3.8 7.5L8 3.3l4.2 4.2',
  down: 'M8 3v9.5 M3.8 8.5L8 12.7l4.2-4.2',
  home: 'M2.5 7.5L8 3l5.5 4.5 M4 6.5V13h8V6.5',
  // 运行位置：本机（笔记本电脑）/ 服务器（机架）
  laptop: 'M3.5 4h9v6.5h-9z M1.8 12.5h12.4 M6.8 12.5l.4-1h1.6l.4 1',
  server: 'M2.5 3h11v4h-11z M2.5 9h11v4h-11z M5 5h.01 M5 11h.01 M8 5h3 M8 11h3',
  bell: 'M4 11V7.2a4 4 0 0 1 8 0V11l1.2 1.3H2.8z M6.6 13.6a1.5 1.5 0 0 0 2.8 0',
  gear: 'M8 5.8a2.2 2.2 0 1 0 0 4.4a2.2 2.2 0 1 0 0-4.4z M8 1.8v1.6 M8 12.6v1.6 M1.8 8h1.6 M12.6 8h1.6 M3.6 3.6l1.1 1.1 M11.3 11.3l1.1 1.1 M3.6 12.4l1.1-1.1 M11.3 4.7l1.1-1.1',
  history: 'M2.8 8a5.2 5.2 0 1 0 1.6-3.7 M2.8 2.6v3.1h3.1 M8 5v3l2 1.3',
  image: 'M2.5 3.5h11v9h-11z M2.5 10.5l3-3 3 3 2-2 3 3 M10.5 6a.01.01 0 1 0 0 .02',
  key: 'M5 5.4a2.6 2.6 0 1 0 0 5.2a2.6 2.6 0 1 0 0-5.2z M7.6 8h6 M11.8 8v2.3 M9.9 8v1.7',
  lock: 'M4.5 7.5h7v6h-7z M6 7.5V5.5a2 2 0 0 1 4 0v2',
  // 可视化（灯箱 / 预览面板 / 文件 chip）
  chevleft: 'M10 4L6 8l4 4',
  minus: 'M3 8h10',
  fit: 'M2.5 6V2.5H6 M10 2.5h3.5V6 M13.5 10v3.5H10 M6 13.5H2.5V10',
  download: 'M8 2.5v8 M4.5 7.5L8 11l3.5-3.5 M3 13.5h10',
  external: 'M9 2.5h4.5V7 M13.5 2.5L7.5 8.5 M11.5 9.5v4h-9v-9h4',
  eye: 'M1.8 8s2.3-4.3 6.2-4.3S14.2 8 14.2 8s-2.3 4.3-6.2 4.3S1.8 8 1.8 8z M8 6.2a1.8 1.8 0 1 0 0 3.6a1.8 1.8 0 1 0 0-3.6z',
  diff: 'M4.5 2.5v6 M1.5 5.5h6 M8.5 12.5h6 M11.5 2.5l-3 11',
  panel: 'M2.5 3h11v10h-11z M9.5 3v10',
  palette: 'M8 2a6 6 0 1 0 0 12c.9 0 1.3-.6 1.3-1.2 0-.8-.6-1-.6-1.8 0-.7.6-1.2 1.4-1.2H12a2 2 0 0 0 2-2C14 4.6 11.3 2 8 2z M5 7.2h.01 M6.8 4.8h.01 M9.8 4.8h.01',
};

export function icon(name, cls) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('class', 'ic' + (cls ? ' ' + cls : ''));
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS(SVG_NS, 'path');
  p.setAttribute('d', ICONS[name] || ICONS.tool);
  svg.appendChild(p);
  return svg;
}

export function spinner() { return h('span', { class: 'spin', 'aria-label': '进行中' }); }

export function fmtCost(v) {
  if (v == null || !isFinite(v)) return '';
  const n = Number(v);
  if (n === 0) return '$0';
  if (n < 0.01) return '$' + n.toFixed(4);
  if (n < 1) return '$' + n.toFixed(3);
  return '$' + n.toFixed(2);
}

export function fmtDuration(ms) {
  if (ms == null || !isFinite(ms)) return '';
  const s = ms / 1000;
  if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + 's';
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  if (m < 60) return `${m}m${String(r).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`;
}

export function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return '';
  return d.toLocaleString('zh-CN', { hour12: false });
}

export function fmtRelTime(ts) {
  if (!ts) return '';
  const now = Date.now() / 1000;
  const d = now - ts;
  if (d < 60) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  if (d < 86400 * 30) return Math.floor(d / 86400) + ' 天前';
  return new Date(ts * 1000).toLocaleDateString('zh-CN');
}

export function fmtSize(n) {
  if (n == null || !isFinite(n)) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

export function basename(p) {
  if (!p) return '';
  const s = String(p).replace(/\/+$/, '');
  if (!s) return '/';
  const i = s.lastIndexOf('/');
  return i >= 0 ? s.slice(i + 1) || s : s;
}

export function truncate(s, n) {
  s = String(s ?? '');
  return s.length > n ? s.slice(0, n) + '…' : s;
}

export function firstLine(s) {
  s = String(s ?? '');
  const i = s.indexOf('\n');
  return i >= 0 ? s.slice(0, i) + ' …' : s;
}

/** 仅允许 data:image/(png|jpeg|gif|webp);base64 —— 本地数据，不会触发外部请求 */
export function safeImageSrc(mediaType, data) {
  if (typeof data !== 'string' || !/^[A-Za-z0-9+/=\s]+$/.test(data.slice(0, 200))) return null;
  if (!/^image\/(png|jpeg|gif|webp)$/.test(String(mediaType))) return null;
  return `data:${mediaType};base64,${data}`;
}

/** 复制到剪贴板（127.0.0.1 属安全上下文；失败时用隐藏 textarea 兜底） */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = h('textarea', { style: { position: 'fixed', left: '-9999px', top: '0' } });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }
}

export function jsonPretty(v) {
  try { return JSON.stringify(v, null, 2); } catch (_) { return String(v); }
}
