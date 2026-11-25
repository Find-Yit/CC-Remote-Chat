// 每轮回复结束处的卡片（docs/SPEC_V2_TUI.md §10.5）：
// - 「本轮改动」（服务器/本机都有）：本轮 Write/Edit/MultiEdit/NotebookEdit 改动的文件与 +N/−M，数据只取 transcript 的
//   toolUseResult.structuredPatch（没有时退回 LCS / 新建文件全部算 +）；点击文件 → 打开右侧「改动」栏、切到这一轮、
//   展开并滚到该文件（changes.js；不读文件、不发请求）。
// - 「网页预览」（仅本机）：本轮写/改了 *.html → 内嵌缩小的沙箱 iframe（高约 320px）+ 全屏 / 侧栏打开 / 浏览器打开。
// - 「新图片」（仅本机）：本轮 Write 的图片，以及 Bash 命令/输出里出现、经 stat 确认存在的 png/jpg… 显示缩略图。
// 一轮的边界由 render.js 判定（system turn_duration / assistant stop_reason=end_turn / 下一条用户消息 / 中断）。
import { h, icon, basename } from './dom.js';
import { isLocal, statPath, openPanel, openChanges, sessionCwd } from './viz.js';
import { EDIT_TOOLS, fileChanges } from './changes.js';
import { bashPaths, textPaths, displayPath, resolvePath, IMAGE_EXT, HTML_EXT, localImage, fileChip } from './filelinks.js';

const MAX_WEB_CARDS = 2;
const MAX_IMAGES = 12;

function str(v) { return typeof v === 'string' ? v : v == null ? '' : String(v); }

function resultText(c) {
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) return c.filter((b) => b && b.type === 'text').map((b) => str(b.text)).join('\n');
  return '';
}

/** 轮次结束：从末尾往回找到上一个用户消息 / 上一张本轮卡片，收集其间的工具调用；已收尾或没有工具 → null */
export function collectTurn(s) {
  const tools = [];
  for (let k = s.items.length - 1; k >= 0; k--) {
    const it = s.items[k];
    if (it.kind === 'turnsum') return null;
    if (it.kind === 'user' && !it.midTurn) break;   // 回复中发送的消息属于这一轮，不是边界
    if (it.kind === 'tool') tools.unshift(it);
  }
  if (!tools.some((t) => EDIT_TOOLS.has(t.name) || t.name === 'Bash')) return null;
  return { tools, expanded: false };
}

// 一次编辑 / 本轮改动的文件：与右侧「改动」栏共用 changes.js 的 editOf / fileChanges（同一口径）

/** 本轮出现的图片路径候选（Write 的图片、Bash 命令/输出里的图片路径） */
function imageCandidates(tools, changes) {
  const out = [];
  const seen = new Set();
  const add = (p) => { if (p && IMAGE_EXT.test(p) && !seen.has(p)) { seen.add(p); out.push(p); } };
  for (const c of changes) add(c.path);
  for (const t of tools) {
    if (t.name !== 'Bash' || !t.done || t.isError) continue;
    for (const p of bashPaths(t.input && t.input.command)) add(p);
    for (const p of textPaths(resultText(t.result))) add(p);
  }
  return out.slice(0, MAX_IMAGES * 2);
}

// 最新的网页预览卡片自动加载，较早轮次的同一文件只在点击后加载
const latestWeb = new Map();   // sid + path -> 卡片 token

export function renderTurnSummary(s, item, el, cls) {
  el.className = cls.join(' ');
  const tools = Array.isArray(item.tools) ? item.tools : [];
  const changes = fileChanges(tools);
  const local = isLocal(s) && !s.readOnly;
  const cwd = sessionCwd(s);
  const parts = [];
  if (changes.length) parts.push(changesCard(s, item, changes, cwd));
  if (local) {
    const htmls = changes.filter((c) => HTML_EXT.test(c.path)).slice(0, MAX_WEB_CARDS);
    for (const c of htmls) parts.push(webCard(s, item, resolvePath(c.path, cwd)));
    const imgs = imageCandidates(tools, changes);
    if (imgs.length) parts.push(imagesCard(s, imgs.map((p) => resolvePath(p, cwd)), () => { el.hidden = false; }));
  }
  // 只有"候选图片"时先隐藏，等 stat 确认至少有一张存在再显示
  el.hidden = !parts.some((p) => !p.hidden);
  el.replaceChildren(...parts);
}

function changesCard(s, item, changes, cwd) {
  const add = changes.reduce((a, c) => a + c.add, 0), del = changes.reduce((a, c) => a + c.del, 0);
  const rows = changes.map((c) => {
    const abs = resolvePath(c.path, cwd);
    const b = h('button', { class: 'tc-row', type: 'button', title: '在右侧「改动」栏查看这一轮的 diff：' + abs },
      icon(c.edits.some((e) => e.created) ? 'plus' : 'edit', 'tc-ic'),
      h('span', { class: 'tc-path mono' }, displayPath(abs, cwd)),
      c.edits.length > 1 ? h('span', { class: 'tc-n' }, c.edits.length + ' 次') : null,
      h('span', { class: 'tc-stat' }, h('span', { class: 'tc-add' }, '+' + c.add), h('span', { class: 'tc-del' }, '−' + c.del)));
    b.addEventListener('click', () => openChanges(s, { toolId: c.toolIds[0], path: abs }, { kind: 'diff', path: abs, change: c }));
    return b;
  });
  const LIMIT = 8;
  const shown = item.expanded ? rows : rows.slice(0, LIMIT);
  const more = rows.length > LIMIT && !item.expanded
    ? h('button', { class: 'link-btn tc-more', type: 'button' }, `还有 ${rows.length - LIMIT} 个文件…`) : null;
  if (more) more.addEventListener('click', () => { item.expanded = true; const el = more.closest('.item'); if (el) renderTurnSummary(s, item, el, el.className.split(' ')); });
  return h('div', { class: 'turn-card tc-changes' },
    h('div', { class: 'tc-head' }, icon('diff', 'tc-hic'), h('span', { class: 'tc-title' }, '本轮改动'),
      h('span', { class: 'tc-sub' }, changes.length + ' 个文件'),
      h('span', { class: 'tc-stat' }, h('span', { class: 'tc-add' }, '+' + add), h('span', { class: 'tc-del' }, '−' + del))),
    h('div', { class: 'tc-list' }, shown, more));
}

function webCard(s, item, abs) {
  const key = s.sid + '\n' + abs;
  const token = {};
  latestWeb.set(key, token);
  const frameBox = h('div', { class: 'web-frame-box' }, h('div', { class: 'web-ph muted' }, '正在检查文件…'));
  const title = h('span', { class: 'tc-path mono', title: abs }, displayPath(abs, sessionCwd(s)));
  const btn = (ic, label, fn) => {
    const b = h('button', { class: 'btn ghost small', type: 'button', title: label, disabled: true }, icon(ic), h('span', null, label));
    b.addEventListener('click', fn);
    return b;
  };
  let info = null;
  const full = btn('fit', '全屏', () => {
    const f = frameBox.querySelector('iframe') || load();
    const box = frameBox;
    if (box.requestFullscreen) box.requestFullscreen().catch(() => openPanel(s, { kind: 'file', path: abs, info }));
    else openPanel(s, { kind: 'file', path: abs, info });
    return f;
  });
  const side = btn('panel', '侧栏打开', () => openPanel(s, { kind: 'file', path: abs, info }));
  const browser = btn('external', '浏览器打开', () => { if (info && info.url) window.open(info.url, '_blank', 'noopener,noreferrer'); });
  const load = () => {
    if (!info || !info.url) return null;
    let f = frameBox.querySelector('iframe');
    if (f) { f.src = info.url + '?ccr_v=' + Date.now(); return f; }
    f = h('iframe', { class: 'web-frame', sandbox: 'allow-scripts allow-forms allow-popups allow-modals', src: info.url,
      title: '网页预览：' + abs, referrerpolicy: 'no-referrer', loading: 'lazy' });
    frameBox.replaceChildren(f);
    return f;
  };
  statPath(s, abs, { force: true }).then((r) => {
    info = r;
    if (!(r && r.ok && r.url)) {
      frameBox.replaceChildren(h('div', { class: 'web-ph muted' }, (r && r.reason) || '文件不存在（可能已被删除或移动）'));
      return;
    }
    for (const b of [full, side, browser]) b.disabled = false;
    if (latestWeb.get(key) === token) load();
    else {
      const go = h('button', { class: 'btn small', type: 'button' }, icon('eye'), '加载预览');
      go.addEventListener('click', () => load());
      frameBox.replaceChildren(h('div', { class: 'web-ph' }, h('span', { class: 'muted' }, '较早一轮的预览（文件可能已被后续修改）'), go));
    }
  });
  return h('div', { class: 'turn-card web-card' },
    h('div', { class: 'tc-head' }, icon('globe', 'tc-hic'), h('span', { class: 'tc-title' }, '网页预览'), title,
      h('span', { class: 'spacer' }), full, side, browser),
    frameBox);
}

function imagesCard(s, paths, onShow) {
  const strip = h('div', { class: 'viewed-imgs tc-imgs' });
  const card = h('div', { class: 'turn-card tc-images', hidden: true },
    h('div', { class: 'tc-head' }, icon('image', 'tc-hic'), h('span', { class: 'tc-title' }, '本轮图片'), h('span', { class: 'tc-sub' })), strip);
  let n = 0;
  for (const p of paths) {
    statPath(s, p).then((r) => {
      if (!(r && r.ok && r.kind === 'image') || n >= MAX_IMAGES) return;
      n++;
      strip.appendChild(localImage(s, r.real || p, basename(p)));
      card.hidden = false;
      card.querySelector('.tc-sub').textContent = n + ' 张';
      if (onShow) onShow();
    });
  }
  return card;
}

export { fileChip };
