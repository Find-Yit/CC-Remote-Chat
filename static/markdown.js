// 自写 Markdown 渲染器：输入一律视为纯文本，只生成受控标签；
// 文本节点全部经 createTextNode 写入（等价于先转义），从不使用 innerHTML。
// 支持：围栏代码块（带复制按钮）、行内代码、粗/斜/删除线、标题、有序/无序列表（可嵌套）、
// 引用、表格、分隔线、链接（仅 http/https，target=_blank rel=noopener noreferrer）、裸 URL。

import { h, copyText } from './dom.js';
import { highlightInto } from './highlight.js';
import { tableBlock } from './mdtable.js';   // 表格下方的「复制为 Markdown / CSV」

const MAX_DEPTH = 10;

// 可视化钩子（filelinks.js 的 markdownCtx）：{code(text)→Node|null, link(raw,label,isImage)→Node|null, url(href,node)→Node|null}。
// 钩子只返回受控 DOM；不给 ctx 时行为与以前完全一致。
let CTX = null;

export function renderMarkdown(src, ctx) {
  const frag = document.createDocumentFragment();
  const text = String(src ?? '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ');
  const prev = CTX;
  CTX = ctx || null;
  try {
    parseBlocks(text.split('\n'), frag, 0);
  } finally {
    CTX = prev;
  }
  return frag;
}

function hook(name, ...args) {
  if (!CTX || typeof CTX[name] !== 'function') return null;
  try { return CTX[name](...args) || null; } catch (_) { return null; }
}

// ---------- 块级 ----------

const RE_BLANK = /^\s*$/;
const RE_FENCE = /^(\s{0,3})(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/;
const RE_HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RE_HR = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const RE_QUOTE = /^\s{0,3}>/;

function indentOf(line) {
  const m = line.match(/^ */);
  return m ? m[0].length : 0;
}

function listMarker(line) {
  const m = line.match(/^( *)([-*+]|\d{1,9}[.)])( +|$)(.*)$/);
  if (!m) return null;
  const ordered = /\d/.test(m[2]);
  const spaces = m[3].length || 1;
  return {
    indent: m[1].length,
    ordered,
    start: ordered ? parseInt(m[2], 10) : 1,
    contentIndent: m[1].length + m[2].length + Math.min(spaces, 4),
    text: m[4] || '',
  };
}

function isTableSep(line) {
  if (!line || !line.includes('-')) return false;
  return line.includes('|') && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(line);
}

function startsBlock(lines, i) {
  const l = lines[i];
  return RE_FENCE.test(l) || RE_HEADING.test(l) || RE_HR.test(l) || RE_QUOTE.test(l) || !!listMarker(l) ||
    (l.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1]));
}

function parseBlocks(lines, parent, depth) {
  if (depth > MAX_DEPTH) {
    parent.appendChild(h('p', null, lines.join('\n')));
    return;
  }
  const n = lines.length;
  let i = 0;
  while (i < n) {
    const line = lines[i];
    if (RE_BLANK.test(line)) { i++; continue; }
    let m;

    // 围栏代码块（未闭合时一直到结尾——流式渲染时自然成立）
    if ((m = line.match(RE_FENCE))) {
      const indent = m[1].length;
      const fence = m[2];
      const lang = m[3] || '';
      const close = new RegExp('^\\s{0,3}' + (fence[0] === '`' ? '`' : '~') + '{' + fence.length + ',}\\s*$');
      const buf = [];
      i++;
      while (i < n && !close.test(lines[i])) {
        const l = lines[i];
        buf.push(indent ? l.replace(new RegExp('^ {0,' + indent + '}'), '') : l);
        i++;
      }
      if (i < n) i++;
      parent.appendChild(codeBlock(buf.join('\n'), lang));
      continue;
    }

    if ((m = line.match(RE_HEADING))) {
      const el = h('h' + m[1].length, { class: 'md-h' });
      renderInline(m[2], el, 0);
      parent.appendChild(el);
      i++;
      continue;
    }

    if (RE_HR.test(line)) { parent.appendChild(h('hr')); i++; continue; }

    if (RE_QUOTE.test(line)) {
      const buf = [];
      while (i < n && !RE_BLANK.test(lines[i]) && (RE_QUOTE.test(lines[i]) || !startsBlock(lines, i))) {
        buf.push(lines[i].replace(/^\s{0,3}> ?/, ''));
        i++;
      }
      const bq = h('blockquote');
      parseBlocks(buf, bq, depth + 1);
      parent.appendChild(bq);
      continue;
    }

    if (line.includes('|') && i + 1 < n && isTableSep(lines[i + 1])) {
      i = parseTable(lines, i, parent);
      continue;
    }

    if (listMarker(line)) {
      i = parseList(lines, i, parent, depth);
      continue;
    }

    // 段落：单个换行保留为 <br>（更适合聊天/中文输出）
    const buf = [line];
    i++;
    while (i < n && !RE_BLANK.test(lines[i]) && !startsBlock(lines, i)) { buf.push(lines[i]); i++; }
    const p = h('p');
    renderInline(buf.map((l) => l.replace(/^ {1,3}/, '')).join('\n'), p, 0);
    parent.appendChild(p);
  }
}

function parseList(lines, i, parent, depth) {
  const n = lines.length;
  const first = listMarker(lines[i]);
  const list = h(first.ordered ? 'ol' : 'ul', { class: 'md-list' });
  if (first.ordered && first.start !== 1) list.setAttribute('start', String(first.start));
  const base = first.indent;
  const isSibling = (mk) => mk && mk.indent >= base && mk.indent <= base + 3 && mk.ordered === first.ordered;

  let stop = false;
  while (i < n && !stop) {
    const mk = listMarker(lines[i]);
    if (!isSibling(mk)) break;
    const itemLines = [mk.text];
    const ci = mk.contentIndent;
    i++;
    while (i < n) {
      const l = lines[i];
      if (RE_BLANK.test(l)) {
        let j = i + 1;
        while (j < n && RE_BLANK.test(lines[j])) j++;
        if (j < n && indentOf(lines[j]) >= base + 2) {
          for (; i < j; i++) itemLines.push('');
          continue;
        }
        if (!(j < n && isSibling(listMarker(lines[j])))) stop = true; // 列表结束
        i = j;
        break; // 松散列表的下一个兄弟项 / 结束
      }
      const ind = indentOf(l);
      if (listMarker(l) && ind <= base + 1) break; // 兄弟项或外层
      if (ind >= base + 2) { itemLines.push(l.slice(Math.min(ind, ci))); i++; continue; }
      if (!startsBlock(lines, i)) { itemLines.push(l.trim()); i++; continue; } // 惰性续行
      stop = true;
      break;
    }
    appendItem(list, itemLines, depth);
  }
  parent.appendChild(list);
  return i;
}

function appendItem(list, itemLines, depth) {
  const li = h('li');
  const m = itemLines[0].match(/^\[([ xX])\]\s+(.*)$/); // 任务列表
  if (m) {
    li.className = 'md-task';
    li.appendChild(h('input', { type: 'checkbox', disabled: true, checked: m[1] !== ' ' }));
    itemLines = [m[2], ...itemLines.slice(1)];
  }
  parseBlocks(itemLines, li, depth + 1);
  list.appendChild(li);
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells = [];
  let cur = '', inCode = false;
  for (let k = 0; k < s.length; k++) {
    const c = s[k];
    if (c === '\\' && s[k + 1] === '|') { cur += '|'; k++; continue; }
    if (c === '`') inCode = !inCode;
    if (c === '|' && !inCode) { cells.push(cur.trim()); cur = ''; continue; }
    cur += c;
  }
  cells.push(cur.trim());
  return cells;
}

function parseTable(lines, i, parent) {
  const n = lines.length;
  const head = splitRow(lines[i]);
  const aligns = splitRow(lines[i + 1]).map((c) => {
    const l = c.startsWith(':'), r = c.endsWith(':');
    return l && r ? 'center' : r ? 'right' : l ? 'left' : '';
  });
  i += 2;
  const thead = h('thead');
  const trh = h('tr');
  head.forEach((c, k) => {
    const th = h('th', aligns[k] ? { style: { textAlign: aligns[k] } } : null);
    renderInline(c, th, 0);
    trh.appendChild(th);
  });
  thead.appendChild(trh);
  const tbody = h('tbody');
  const rawRows = [];
  while (i < n && !RE_BLANK.test(lines[i]) && lines[i].includes('|')) {
    const cells = splitRow(lines[i]);
    rawRows.push(cells.slice(0, head.length));
    const tr = h('tr');
    for (let k = 0; k < head.length; k++) {
      const td = h('td', aligns[k] ? { style: { textAlign: aligns[k] } } : null);
      renderInline(cells[k] ?? '', td, 0);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
    i++;
  }
  parent.appendChild(tableBlock(h('div', { class: 'md-table-wrap' }, h('table', { class: 'md-table' }, thead, tbody)), { head, rows: rawRows, aligns }));
  return i;
}

function codeBlock(code, lang) {
  const btn = h('button', { class: 'md-copy', type: 'button', title: '复制代码' }, '复制');
  btn.addEventListener('click', async () => {
    const ok = await copyText(code);
    btn.textContent = ok ? '已复制' : '复制失败';
    setTimeout(() => { btn.textContent = '复制'; }, 1500);
  });
  const codeEl = h('code', null, code);
  highlightInto(codeEl, code, lang);   // 极简正则高亮（只在认识的语言上生效）
  return h('div', { class: 'md-code' },
    h('div', { class: 'md-code-head' }, h('span', { class: 'md-lang' }, lang || 'text'), btn),
    h('pre', null, codeEl));
}

// ---------- 行内 ----------

const ESCAPABLE = /[\\`*_{}\[\]()#+\-.!|~>]/;
const URL_RE = /^https?:\/\/[^\s<>"'`　-〿！-／：-＠]+/;

export function safeHref(url) {
  url = String(url || '').trim();
  if (!/^https?:\/\//i.test(url)) return null;
  try {
    const u = new URL(url);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u.href;
  } catch (_) {
    return null;
  }
}

function appendText(parent, s) {
  if (!s) return;
  const parts = s.split('\n');
  parts.forEach((p, k) => {
    if (k > 0) parent.appendChild(h('br'));
    if (p) parent.appendChild(document.createTextNode(p));
  });
}

function isWordChar(c) { return !!c && /[\p{L}\p{N}]/u.test(c); }

function findClosingSingle(text, start, ch) {
  for (let k = start; k < text.length; k++) {
    const c = text[k];
    if (c === '\\') { k++; continue; }
    if (c === '`') { // 跳过行内代码
      const e = text.indexOf('`', k + 1);
      if (e > 0) { k = e; continue; }
    }
    if (c === ch && text[k + 1] !== ch && text[k - 1] !== ch && !/\s/.test(text[k - 1])) {
      if (ch === '_' && isWordChar(text[k + 1])) continue;
      return k;
    }
  }
  return -1;
}

function findMatchingBracket(text, i) {
  let d = 0;
  for (let k = i; k < text.length; k++) {
    const c = text[k];
    if (c === '\\') { k++; continue; }
    if (c === '[') d++;
    else if (c === ']') { d--; if (d === 0) return k; }
  }
  return -1;
}

function renderInline(text, parent, depth) {
  if (depth > MAX_DEPTH) { appendText(parent, text); return; }
  let buf = '';
  const flush = () => { appendText(parent, buf); buf = ''; };
  const n = text.length;
  let i = 0;
  while (i < n) {
    const c = text[i];

    if (c === '\\' && i + 1 < n && ESCAPABLE.test(text[i + 1])) { buf += text[i + 1]; i += 2; continue; }

    if (c === '`') {
      let run = 1;
      while (text[i + run] === '`') run++;
      const fence = '`'.repeat(run);
      let j = text.indexOf(fence, i + run);
      while (j >= 0 && text[j + run] === '`') j = text.indexOf(fence, j + run + 1);
      if (j >= 0) {
        flush();
        let code = text.slice(i + run, j).replace(/\n/g, ' ');
        if (/^ .* $/.test(code) && code.trim()) code = code.slice(1, -1);
        parent.appendChild(hook('code', code) || h('code', { class: 'md-inline' }, code));
        i = j + run;
        continue;
      }
      buf += fence; i += run; continue;
    }

    if ((c === '*' || c === '_') && text[i + 1] === c) {
      const close = text.indexOf(c + c, i + 2);
      const canOpen = !/\s/.test(text[i + 2] || ' ') && !(c === '_' && isWordChar(text[i - 1]));
      if (canOpen && close > i + 2 && !/\s/.test(text[close - 1])) {
        flush();
        const el = h('strong');
        renderInline(text.slice(i + 2, close), el, depth + 1);
        parent.appendChild(el);
        i = close + 2;
        continue;
      }
    } else if (c === '*' || c === '_') {
      const canOpen = text[i + 1] && !/\s/.test(text[i + 1]) && !(c === '_' && isWordChar(text[i - 1]));
      if (canOpen) {
        const close = findClosingSingle(text, i + 1, c);
        if (close > i + 1) {
          flush();
          const el = h('em');
          renderInline(text.slice(i + 1, close), el, depth + 1);
          parent.appendChild(el);
          i = close + 1;
          continue;
        }
      }
    }

    if (c === '~' && text[i + 1] === '~') {
      const close = text.indexOf('~~', i + 2);
      if (close > i + 2) {
        flush();
        const el = h('del');
        renderInline(text.slice(i + 2, close), el, depth + 1);
        parent.appendChild(el);
        i = close + 2;
        continue;
      }
    }

    if (c === '[' ) {
      const close = findMatchingBracket(text, i);
      if (close > i && text[close + 1] === '(') {
        const pe = text.indexOf(')', close + 2);
        if (pe > close) {
          const label = text.slice(i + 1, close);
          const raw = text.slice(close + 2, pe).trim().split(/\s+/)[0].replace(/^<|>$/g, '');
          const href = safeHref(raw);
          flush();
          const isImage = text[i - 1] === '!';
          if (href) {
            const a = h('a', { href, target: '_blank', rel: 'noopener noreferrer', title: href });
            if (isImage) a.appendChild(document.createTextNode('[图片] '));
            renderInline(label || href, a, depth + 1);
            parent.appendChild(hook('url', href, a) || a);
          } else {
            // 非 http/https 链接：像文件路径时交给可视化钩子（文件 chip / 本机图片），否则只显示文字，不生成可点击链接
            const node = hook('link', raw, label, isImage);
            if (node) parent.appendChild(node);
            else {
              const span = h('span', { class: 'md-badlink', title: raw });
              renderInline(label || raw, span, depth + 1);
              parent.appendChild(span);
            }
          }
          i = pe + 1;
          continue;
        }
      }
    }

    if (c === '!' && text[i + 1] === '[') { i++; continue; } // 图片语法：不加载，交给链接分支

    if ((c === 'h' || c === 'H') && !isWordChar(text[i - 1])) {
      const m = text.slice(i, i + 2100).match(URL_RE);
      if (m) {
        let url = m[0];
        // 去掉尾随标点与不配对的右括号
        while (/[.,;:!?)\]}]$/.test(url)) {
          if (url.endsWith(')') && (url.match(/\(/g) || []).length >= (url.match(/\)/g) || []).length) break;
          url = url.slice(0, -1);
        }
        const href = safeHref(url);
        if (href) {
          flush();
          const a = h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, url);
          parent.appendChild(hook('url', href, a) || a);
          i += url.length;
          continue;
        }
      }
    }

    buf += c;
    i++;
  }
  flush();
}

export { renderInline };
