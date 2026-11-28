// 聊天里渲染出的 Markdown 表格：悬停出现「复制为 Markdown」「复制为 CSV」（docs/SPEC_V2_TUI.md §7「表格复制」）。
// 只在用户点击时调用 dom.js 的 copyText（回放页 / 测试用 _setTableCopy 换成桩，绝不碰真实剪贴板）；不发请求、不按键。
//
// 参考 T3 Code（https://github.com/pingdotgg/t3code，MIT License，Copyright (c) 2026 T3 Tools Inc.）：
//   apps/web/src/components/chat/MarkdownTable.tsx（表格下方右对齐的复制入口）、
//   apps/web/src/markdown-clipboard.ts（serializeTable：`| a | b |` + 按对齐方式的分隔行；csvCell：空白折叠成一个空格、
//   含 `"` `,` 换行时加双引号并把 `"` 写成 `""`，行之间用 \n）。
// 与 T3 的差异：Markdown 用 markdown.js 解析出的单元格原文（保留行内代码 / 粗体等记号），不从 DOM 反推；
//   单元格里的 `|` 一律写成 `\|`；两个格式直接是两个小按钮（没有菜单），不做 HTML / PNG。

import { h, icon, copyText } from './dom.js';

let copyImpl = copyText;
/** 回放页 / 测试：换掉复制实现（fn(text) → Promise<boolean>）；不给参数 = 恢复 copyText */
export function _setTableCopy(fn) { copyImpl = typeof fn === 'function' ? fn : copyText; }

// ---------------------------------------------------------------- 纯函数

/** 单元格原文 → 表格里的 Markdown（折叠空白、未转义的 | 写成 \|） */
export function mdCell(v) {
  const s = String(v ?? '').replace(/\s+/g, ' ').trim();
  return s.replace(/\\\|/g, '\u0000').replace(/\|/g, '\\|').replace(/\u0000/g, '\\|');
}

function sepFor(align) {
  if (align === 'center') return ':---:';
  if (align === 'right') return '---:';
  if (align === 'left') return ':---';
  return '---';
}

/** 表头 + 行（单元格原文）+ 对齐 → GFM 表格文本（列数按表头补齐 / 截断） */
export function tableToMarkdown(head, rows, aligns = []) {
  const hd = (Array.isArray(head) ? head : []).map(mdCell);
  if (!hd.length) return '';
  const n = hd.length;
  const fit = (r) => Array.from({ length: n }, (_, k) => mdCell((Array.isArray(r) ? r : [])[k] ?? ''));
  const line = (cells) => '| ' + cells.join(' | ') + ' |';
  const out = [line(hd), line(Array.from({ length: n }, (_, k) => sepFor(aligns[k])))];
  for (const r of Array.isArray(rows) ? rows : []) out.push(line(fit(r)));
  return out.join('\n');
}

/** CSV 单元格（RFC 4180 的引号规则；空白折叠成一个空格） */
export function csvCell(v) {
  const s = String(v ?? '').replace(/\s+/g, ' ').trim();
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

/** 行（含表头那一行）→ CSV 文本，行之间 \n */
export function tableToCSV(rows) {
  return (Array.isArray(rows) ? rows : []).map((r) => (Array.isArray(r) ? r : []).map(csvCell).join(',')).join('\n');
}

/** 渲染好的 <table> → 每行单元格的纯文本（CSV 用：用户看到的文字，不含 Markdown 记号） */
export function tableTextRows(table) {
  const out = [];
  if (!table) return out;
  for (const tr of table.querySelectorAll('tr')) {
    const cells = [...tr.children].filter((c) => c.tagName === 'TH' || c.tagName === 'TD').map((c) => c.textContent || '');
    if (cells.length) out.push(cells);
  }
  return out;
}

// ---------------------------------------------------------------- DOM

/**
 * markdown.js parseTable 的钩子：把表格包成 `.md-table-box`（表格横向滚动区 + 下方右对齐的复制按钮行）。
 * src = { head:[原文], rows:[[原文]], aligns:['left'|'center'|'right'|''] }
 */
export function tableBlock(wrap, src) {
  const btn = (label, title, fn) => {
    const b = h('button', { class: 'tbl-copy', type: 'button', title }, icon('copy'), h('span', null, label));
    b.addEventListener('mousedown', (e) => e.preventDefault());   // 不抢输入框焦点、不清掉别处的选区
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      let ok = false;
      try { ok = await copyImpl(fn()); } catch (_) { ok = false; }
      b.classList.toggle('ok', ok);
      b.classList.toggle('bad', !ok);
      b.lastChild.textContent = ok ? '已复制' : '复制失败';
      clearTimeout(b._t);
      b._t = setTimeout(() => { b.classList.remove('ok', 'bad'); b.lastChild.textContent = label; }, 1500);
    });
    return b;
  };
  const table = wrap.querySelector('table');
  const md = btn('Markdown', '复制为 Markdown 表格（保留单元格里的代码、粗体等记号）',
    () => tableToMarkdown(src.head, src.rows, src.aligns));
  const csv = btn('CSV', '复制为 CSV（逗号分隔，可直接粘贴进表格软件）', () => tableToCSV(tableTextRows(table)));
  const acts = h('div', { class: 'tbl-acts', role: 'toolbar', 'aria-label': '复制表格' }, h('span', { class: 'tbl-acts-l' }, '复制表格'), md, csv);
  return h('div', { class: 'md-table-box' }, wrap, acts);
}
