// 本地命令输出里的 ANSI 颜色与 /context 卡片（只读、纯展示，不发任何请求、不按任何键）。
//
// CLI（2.1.331 实测，fixtures/transcript_context_*.jsonl）把 /context 的输出原样写进 transcript 的 <local-command-stdout>：
//   带 ANSI 颜色码（\x1b[38;5;245m…）、左边是 ⛁ ⛀ ⛶ ⛝ 方块网格（每格 = 窗口的 1%），右边一列是
//   显示名 / 模型 id / "16k/1m tokens (2%)" / "Estimated usage by category" / 各分类 "⛁ System prompt: 3.1k tokens (0.3%)"，
//   之后是 "Auto-compact window:  1m tokens" 与 MCP tools / Custom agents / Memory files / Skills 小节（"└ 1 agent · 73 tokens"）。
// 以前直接当纯文本显示：ESC 不可见但 "[38;5;245m" 之类全露出来，网格用比例字体也对不齐。
// 现在：/context → 卡片（总量 + 用量条 + 分类列表 + 小节，原文可展开，原文按 ANSI 上色、方块固定 1ch 宽对齐）；
//       其它带 ANSI 的命令输出 → 上色的等宽原文。
import { h } from './dom.js';

// CSI（含 SGR）、OSC、两字节 ESC 序列
const ANSI_RE = /\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;
const GRID_CH = '⛀⛁⛶⛝';
const GRID_RE = /[⛀⛁⛶⛝]/;

export function hasAnsi(s) { return typeof s === 'string' && s.includes('\x1b'); }

export function stripAnsi(s) { return String(s ?? '').replace(ANSI_RE, ''); }

const BASIC = ['#000000', '#cd3131', '#0dbc79', '#e5e510', '#2472c8', '#bc3fbc', '#11a8cd', '#e5e5e5',
  '#666666', '#f14c4c', '#23d18b', '#f5f543', '#3b8eea', '#d670d6', '#29b8db', '#ffffff'];
const hex2 = (v) => v.toString(16).padStart(2, '0');

/** xterm 256 色 → '#rrggbb'（0–15 基本色，16–231 6×6×6 立方，232–255 灰阶） */
export function xterm256(n) {
  n = Number(n);
  if (!Number.isInteger(n) || n < 0 || n > 255) return null;
  if (n < 16) return BASIC[n];
  if (n < 232) {
    const i = n - 16, lv = [0, 95, 135, 175, 215, 255];
    return '#' + hex2(lv[Math.floor(i / 36)]) + hex2(lv[Math.floor(i / 6) % 6]) + hex2(lv[i % 6]);
  }
  const g = 8 + (n - 232) * 10;
  return '#' + hex2(g) + hex2(g) + hex2(g);
}

/** 带 ANSI 的文本 → 行数组，每行是 [{text, fg, bold, dim}]（只认颜色/粗体/暗淡，其它 SGR 与控制序列丢弃） */
export function ansiLines(text) {
  const st = { fg: null, bold: false, dim: false };
  const lines = [[]];
  const push = (t) => {
    const parts = t.split('\n');
    parts.forEach((p, i) => {
      if (i > 0) lines.push([]);
      if (!p) return;
      const cur = lines[lines.length - 1];
      const last = cur[cur.length - 1];
      if (last && last.fg === st.fg && last.bold === st.bold && last.dim === st.dim) last.text += p;
      else cur.push({ text: p, fg: st.fg, bold: st.bold, dim: st.dim });
    });
  };
  const s = String(text ?? '').replace(/\r\n?/g, '\n');
  let pos = 0;
  for (const m of s.matchAll(ANSI_RE)) {
    if (m.index > pos) push(s.slice(pos, m.index));
    pos = m.index + m[0].length;
    const sgr = /^\x1b\[([0-9;:]*)m$/.exec(m[0]);
    if (!sgr) continue;
    const codes = sgr[1] === '' ? [0] : sgr[1].split(/[;:]/).map((x) => (x === '' ? 0 : Number(x)));
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) { st.fg = null; st.bold = false; st.dim = false; }
      else if (c === 1) st.bold = true;
      else if (c === 2) st.dim = true;
      else if (c === 22) { st.bold = false; st.dim = false; }
      else if (c === 39) st.fg = null;
      else if (c >= 30 && c <= 37) st.fg = BASIC[c - 30];
      else if (c >= 90 && c <= 97) st.fg = BASIC[c - 90 + 8];
      else if (c === 38 && codes[i + 1] === 5) { st.fg = xterm256(codes[i + 2]); i += 2; }
      else if (c === 38 && codes[i + 1] === 2) {
        const [r, g, b] = codes.slice(i + 2, i + 5).map((v) => Math.max(0, Math.min(255, v | 0)));
        st.fg = '#' + hex2(r) + hex2(g) + hex2(b);
        i += 4;
      } else if (c === 48 && (codes[i + 1] === 5 || codes[i + 1] === 2)) i += codes[i + 1] === 5 ? 2 : 4;   // 背景色：不显示
    }
  }
  if (pos < s.length) push(s.slice(pos));
  return lines;
}

const lineText = (segs) => segs.map((x) => x.text).join('');

/** 'Context usage' 等 CLI 紧凑数字：'16k' → 16000、'1m' → 1e6、'950.4k'、'~480'、'73' */
export function parseTokens(t) {
  const m = /^~?\s*(\d+(?:\.\d+)?)\s*([kKmM])?$/.exec(String(t ?? '').trim());
  if (!m) return null;
  const v = parseFloat(m[1]) * ({ k: 1e3, m: 1e6 }[(m[2] || '').toLowerCase()] || 1);
  return Number.isFinite(v) ? Math.round(v) : null;
}

export function isContextCommand(cmd) { return /^\/context(?:\s|$)/.test(String(cmd ?? '').trim()); }

const HEAD_RE = /^\s*(?:⎿\s*)?Context usage\s*$/i;
const TOTAL_RE = /^(\d+(?:\.\d+)?[kKmM]?)\s*\/\s*(\d+(?:\.\d+)?[kKmM]?)\s+tokens\s*\(\s*(<?\s*\d+(?:\.\d+)?%)\s*\)$/;
const CAT_RE = /^([⛀⛁⛶⛝])\s+(.+?):\s+(~?\d+(?:\.\d+)?[kKmM]?)\s+tokens\s*\(\s*(<?\s*\d+(?:\.\d+)?%)\s*\)$/;
// 网格 = 至少 2 个方块、单个空格隔开（分类行开头的单个 ⛁ 是图例，不是网格）
const GRID_SPLIT_RE = /^(\s*[⛀⛁⛶⛝](?:[ \u00a0][⛀⛁⛶⛝])+[ \u00a0]?)/;
const TREE_RE = /^\s*[└├│]\s?(.*)$/;

/** 一行 → {grid: [{ch, fg}], rest: 去掉网格后的右列文字, restSegs} */
function splitGrid(segs) {
  const plain = lineText(segs);
  const m = GRID_SPLIT_RE.exec(plain);
  const cut = m ? m[1].length : 0;
  const grid = [];
  const restSegs = [];
  let off = 0;
  for (const sg of segs) {
    const end = off + sg.text.length;
    if (off < cut) {
      for (const ch of sg.text.slice(0, Math.max(0, cut - off))) if (GRID_CH.includes(ch)) grid.push({ ch, fg: sg.fg });
      if (end > cut) restSegs.push({ ...sg, text: sg.text.slice(cut - off) });
    } else restSegs.push(sg);
    off = end;
  }
  return { grid, rest: lineText(restSegs).trim(), restSegs };
}

/**
 * /context 的 stdout（可带 ANSI）→ 结构化结果；不是 /context 的输出（找不到 "Context usage" 标题或总量行）返回 null。
 * {model, modelId, used, limit, usedText, limitText, pctText, cats:[{mark, kind:'used'|'free'|'buffer', name, tokens, tokensText, pctText, color}],
 *  autoCompact, sections:[{title, note, groups:[{label, items:[…]}]}], footer, gridRows}
 */
export function parseContextOutput(output) {
  if (typeof output !== 'string' || !/context usage/i.test(output)) return null;
  const lines = ansiLines(output.replace(/<\/?local-command-(?:stdout|stderr)>/g, ''));
  const plain = lines.map(lineText);
  const hi = plain.findIndex((l) => HEAD_RE.test(l));
  if (hi < 0) return null;
  const r = { model: null, modelId: null, used: null, limit: null, usedText: '', limitText: '', pctText: '',
    cats: [], autoCompact: null, sections: [], footer: null, gridRows: 0 };
  // ---- 网格 + 右列（直到分类列表之后的第一个空行）
  const right = [];
  let i = hi + 1;
  let seenCats = false;
  for (; i < lines.length; i++) {
    if (!plain[i].trim()) {
      if (seenCats) break;
      continue;
    }
    const { grid, rest, restSegs } = splitGrid(lines[i]);
    if (grid.length) r.gridRows++;
    if (!grid.length && /^\s*(Auto-compact window:|MCP tools|Custom agents|Memory files|Skills)\b/.test(plain[i]) && seenCats) break;
    if (!rest) continue;
    if (/^[⛀⛁⛶⛝]\s/.test(rest) || /^Estimated usage by category$/i.test(rest)) seenCats = true;
    right.push({ text: rest, segs: restSegs });
  }
  // 窄终端时分类行会折行（"⛝ Autocompact buffer: 33k" + "tokens (3%)"）：并回去
  for (let k = 0; k < right.length - 1; k++) {
    if (/^[⛀⛁⛶⛝]\s/.test(right[k].text) && !CAT_RE.test(right[k].text) && !/^[⛀⛁⛶⛝]\s/.test(right[k + 1].text)
        && CAT_RE.test(right[k].text + ' ' + right[k + 1].text)) {
      right[k] = { text: right[k].text + ' ' + right[k + 1].text, segs: right[k].segs.concat(right[k + 1].segs) };
      right.splice(k + 1, 1);
    }
  }
  const ti = right.findIndex((x) => TOTAL_RE.test(x.text));
  if (ti < 0) return null;
  const tm = TOTAL_RE.exec(right[ti].text);
  r.usedText = tm[1]; r.limitText = tm[2]; r.pctText = tm[3].replace(/\s+/g, '');
  r.used = parseTokens(tm[1]); r.limit = parseTokens(tm[2]);
  if (ti >= 1) r.modelId = right[ti - 1].text;
  if (ti >= 2) r.model = right[ti - 2].text;
  if (r.model && r.modelId && !/^[\w.:@/[\]-]{1,100}$/.test(r.modelId)) r.modelId = null;
  for (const x of right.slice(ti + 1)) {
    const m = CAT_RE.exec(x.text);
    if (!m) continue;
    const markSeg = x.segs.find((sg) => sg.text.trimStart().startsWith(m[1]));
    const kind = m[1] === '⛶' ? 'free' : m[1] === '⛝' ? 'buffer' : 'used';
    r.cats.push({ mark: m[1], kind, name: m[2], tokensText: m[3], tokens: parseTokens(m[3]), pctText: m[4].replace(/\s+/g, ''),
      color: kind === 'used' && markSeg ? markSeg.fg : null });
  }
  // ---- 之后：Auto-compact window、各小节、提示行
  // 小节标题是粗体（"Custom agents · .claude/agents/"）；展开版（/context all、经典渲染器）里小节内还有暗色的分组名
  // （"Plugin"、"Built-in"），分组之间隔着空行。没有 ANSI 时只能按空行分节。
  const boldHeads = lines.slice(i).some((l) => l.length && l[0].bold);
  let sec = null;
  for (; i < lines.length; i++) {
    const t = plain[i].trim();
    if (!t) { if (!boldHeads) sec = null; continue; }
    const ac = /^Auto-compact window:\s*(.+)$/.exec(t);
    if (ac) { r.autoCompact = ac[1].trim(); sec = null; continue; }
    if (/^\/context\b/.test(t)) { r.footer = t; sec = null; continue; }
    const tr = TREE_RE.exec(plain[i]);
    if (sec && tr) {
      const g = sec.groups[sec.groups.length - 1] || (sec.groups[0] = { label: null, items: [] });
      g.items.push(tr[1].trim());
      continue;
    }
    const first = lines[i].find((sg) => sg.text.trim());
    if (sec && !(boldHeads && first && first.bold)) { sec.groups.push({ label: t, items: [] }); continue; }
    const [title, ...note] = t.split(' · ');
    sec = { title, note: note.join(' · '), groups: [] };
    r.sections.push(sec);
  }
  for (const s of r.sections) s.groups = s.groups.filter((g) => g.items.length || g.label);
  return r;
}

// ------------------------------------------------------------------ DOM

/** 带 ANSI 的文本 → <pre>：颜色/粗体/暗淡按 ANSI 显示（CSSOM 赋色，不拼 HTML），方块字符固定 1ch 宽（网格对齐） */
export function ansiPre(text, cls = '') {
  const pre = h('pre', { class: ('tool-pre ansi-pre ' + cls).trim() });
  const lines = ansiLines(text);
  lines.forEach((segs, li) => {
    for (const sg of segs) {
      const parts = sg.text.split(/([⛀⛁⛶⛝])/);
      for (const p of parts) {
        if (!p) continue;
        const glyph = GRID_RE.test(p) && p.length === 1;
        const span = h('span', glyph ? { class: 'ansi-g' } : null, p);
        // 方块保留 CLI 的颜色；文字里的灰色（CLI 的 dim 色）改用主题的弱化色，深浅主题都看得清
        if (sg.fg && (glyph || !isGrey(sg.fg))) span.style.color = sg.fg;
        else if (sg.fg) span.classList.add('ansi-grey');
        if (sg.bold) span.classList.add('ansi-b');
        if (sg.dim) span.classList.add('ansi-dim');
        pre.appendChild(span);
      }
    }
    if (li < lines.length - 1) pre.appendChild(document.createTextNode('\n'));
  });
  return pre;
}

function isGrey(c) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(c || '');
  return !!m && m[1] === m[2] && m[2] === m[3] && !/^(00|ff)$/i.test(m[1]);
}

const FALLBACK_COLORS = ['#8a8a8a', '#626262', '#5f87ff', '#ffaf5f', '#ffaf00', '#af00af', '#5fafaf', '#d75f87'];

/** 卡片：标题行（模型）+ 总量 + 用量条 + 分类列表 + 自动压缩窗口 + 小节 + 可展开原文 */
export function contextCard(output, parsed) {
  const p = parsed || parseContextOutput(output);
  if (!p) return null;
  const total = p.limit || 0;
  const head = h('div', { class: 'ctx-head' },
    h('span', { class: 'ctx-title' }, '上下文用量'),
    p.model ? h('span', { class: 'ctx-model' }, p.model) : null,
    p.modelId ? h('span', { class: 'ctx-model-id mono' }, p.modelId) : null);
  const sum = h('div', { class: 'ctx-total' },
    h('span', { class: 'ctx-total-num' }, p.usedText + ' / ' + p.limitText + ' tokens'),
    h('span', { class: 'ctx-total-pct' }, p.pctText));
  // 用量条：已用分类依次排开，自动压缩预留在最右，中间是空闲
  const bar = h('div', { class: 'ctx-bar', role: 'img', 'aria-label': '已用 ' + p.pctText + '（' + p.usedText + ' / ' + p.limitText + '）' });
  let ci = 0;
  const colorOf = (c) => c.color || FALLBACK_COLORS[ci++ % FALLBACK_COLORS.length];
  const colors = new Map();
  for (const c of p.cats) if (c.kind === 'used') colors.set(c, colorOf(c));
  if (total > 0) {
    for (const c of p.cats) {
      if (c.kind !== 'used' || !(c.tokens > 0)) continue;
      const seg = h('span', { class: 'ctx-seg', title: c.name + ' · ' + c.tokensText + ' tokens（' + c.pctText + '）' });
      seg.style.width = Math.min(100, (c.tokens * 100) / total).toFixed(3) + '%';
      seg.style.background = colors.get(c);
      bar.appendChild(seg);
    }
    const buf = p.cats.find((c) => c.kind === 'buffer');
    bar.appendChild(h('span', { class: 'ctx-free' }));
    if (buf && buf.tokens > 0) {
      const seg = h('span', { class: 'ctx-seg ctx-buf', title: buf.name + ' · ' + buf.tokensText + ' tokens（' + buf.pctText + '）' });
      seg.style.width = Math.min(100, (buf.tokens * 100) / total).toFixed(3) + '%';
      bar.appendChild(seg);
    }
  }
  const cats = h('ul', { class: 'ctx-cats' }, p.cats.map((c) => {
    const dot = h('span', { class: 'ctx-dot ' + c.kind, 'aria-hidden': 'true' });
    if (c.kind === 'used') dot.style.background = colors.get(c);
    return h('li', { class: 'ctx-cat ' + c.kind },
      h('span', { class: 'ctx-cat-name' }, dot, c.name),
      h('span', { class: 'ctx-cat-tok' }, c.tokensText), h('span', { class: 'ctx-cat-pct' }, c.pctText));
  }));
  const meta = p.autoCompact ? h('div', { class: 'ctx-meta' }, h('span', { class: 'ctx-meta-k' }, '自动压缩窗口'), h('span', null, p.autoCompact)) : null;
  const secs = p.sections.length ? h('div', { class: 'ctx-secs' }, p.sections.map((s) => {
    const items = s.groups.reduce((n, g) => n + g.items.length, 0);
    const label = [h('span', { class: 'ctx-sec-title' }, s.title), s.note ? h('span', { class: 'ctx-sec-note' }, s.note) : null];
    if (items <= 1 && s.groups.every((g) => !g.label)) {
      return h('div', { class: 'ctx-sec' }, label, items ? h('span', { class: 'ctx-sec-val' }, s.groups[0].items[0]) : null);
    }
    return h('details', { class: 'ctx-sec' }, h('summary', null, label, h('span', { class: 'ctx-sec-val' }, items + ' 项')),
      h('div', { class: 'ctx-sec-body' }, s.groups.map((g) => [
        g.label ? h('div', { class: 'ctx-sec-group' }, g.label) : null,
        g.items.map((it) => h('div', { class: 'ctx-sec-item' }, it)),
      ])));
  })) : null;
  const raw = h('details', { class: 'ctx-raw' }, h('summary', null, '原文' + (p.footer ? '（' + p.footer + '）' : '')), ansiPre(output, 'ctx-pre'));
  return h('div', { class: 'ctx-card' }, head, sum, bar, cats, meta, secs, raw);
}
