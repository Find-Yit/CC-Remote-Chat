// 工具调用卡片的摘要/详情渲染（纯函数，返回受控 DOM）
import { h, icon, firstLine, truncate, jsonPretty, safeImageSrc } from './dom.js';

const PATH_KEYS = ['file_path', 'notebook_path', 'path'];

function str(v) { return typeof v === 'string' ? v : v == null ? '' : String(v); }

function shortPath(p) {
  p = str(p);
  if (p.length <= 64) return p;
  const parts = p.split('/');
  return '…/' + parts.slice(-3).join('/');
}

/** 从尚未完整的 input_json 里尽力取出一个可读字段（流式时显示命令/路径） */
export function peekPartialJson(partial) {
  if (!partial) return null;
  try { return JSON.parse(partial); } catch (_) { /* 继续尝试 */ }
  const out = {};
  const re = /"(command|file_path|notebook_path|pattern|path|url|query|description|skill|prompt|subagent_type)"\s*:\s*"((?:[^"\\]|\\.)*)/g;
  let m;
  while ((m = re.exec(partial))) {
    let v = m[2].replace(/\\+$/, '');
    try { v = JSON.parse('"' + v + '"'); } catch (_) { /* 保留原样 */ }
    out[m[1]] = v;
  }
  return Object.keys(out).length ? out : null;
}

/** 工具类别（彩色区分 / 工具组摘要用）：read / edit / bash / mcp / web / agent / todo / ask / other */
export function toolCategory(name) {
  const n = str(name);
  if (['Read', 'Grep', 'Glob', 'LS', 'NotebookRead', 'ToolSearch'].includes(n)) return 'read';
  if (['Write', 'Edit', 'MultiEdit', 'NotebookEdit'].includes(n)) return 'edit';
  if (['Bash', 'BashOutput', 'KillShell', 'KillBash'].includes(n)) return 'bash';
  if (n.startsWith('mcp__')) return 'mcp';
  if (n === 'WebFetch' || n === 'WebSearch') return 'web';
  if (n === 'Task' || n === 'Agent') return 'agent';
  if (n === 'TodoWrite') return 'todo';
  if (n === 'AskUserQuestion') return 'ask';
  return 'other';
}

export function toolInfo(name, input) {
  const i = input && typeof input === 'object' ? input : {};
  const n = str(name) || '工具';
  switch (n) {
    case 'Bash':
      return { icon: 'terminal', label: 'Bash', summary: firstLine(i.command), sub: str(i.description) };
    case 'BashOutput':
      return { icon: 'terminal', label: 'BashOutput', summary: str(i.bash_id || i.shell_id) };
    case 'KillShell': case 'KillBash':
      return { icon: 'terminal', label: n, summary: str(i.shell_id || i.bash_id) };
    case 'Read': {
      let s = shortPath(i.file_path);
      if (i.offset || i.limit) s += ` (${i.offset || 1}${i.limit ? '+' + i.limit : ''})`;
      return { icon: 'file', label: 'Read', summary: s };
    }
    case 'Write':
      return { icon: 'file', label: 'Write', summary: shortPath(i.file_path) };
    case 'Edit': case 'MultiEdit':
      return { icon: 'edit', label: n, summary: shortPath(i.file_path) };
    case 'NotebookEdit':
      return { icon: 'edit', label: n, summary: shortPath(i.notebook_path) };
    case 'Glob':
      return { icon: 'search', label: 'Glob', summary: str(i.pattern) + (i.path ? '  ·  ' + shortPath(i.path) : '') };
    case 'Grep':
      return { icon: 'search', label: 'Grep', summary: str(i.pattern) + (i.path ? '  ·  ' + shortPath(i.path) : i.glob ? '  ·  ' + i.glob : '') };
    case 'WebFetch':
      return { icon: 'globe', label: 'WebFetch', summary: str(i.url) };
    case 'WebSearch':
      return { icon: 'globe', label: 'WebSearch', summary: str(i.query) };
    case 'TodoWrite': {
      const todos = Array.isArray(i.todos) ? i.todos : [];
      const done = todos.filter((t) => t && t.status === 'completed').length;
      const cur = todos.find((t) => t && t.status === 'in_progress');
      return { icon: 'list', label: '待办清单', summary: `${done}/${todos.length} 已完成` + (cur ? '  ·  ' + str(cur.activeForm || cur.content) : '') };
    }
    case 'Task': case 'Agent':
      return { icon: 'agent', label: n === 'Task' ? '子任务' : 'Agent', summary: str(i.description) + (i.subagent_type ? '  ·  ' + i.subagent_type : '') };
    case 'AskUserQuestion': {
      const qs = Array.isArray(i.questions) ? i.questions : [];
      return { icon: 'question', label: '提问', summary: qs.map((q) => str(q && (q.header || q.question))).join('；') };
    }
    case 'Skill':
      return { icon: 'spark', label: 'Skill', summary: str(i.skill || i.command) };
    case 'SlashCommand':
      return { icon: 'spark', label: '命令', summary: str(i.command) };
    case 'ToolSearch':
      return { icon: 'search', label: 'ToolSearch', summary: str(i.query) };
    case 'ExitPlanMode':
      return { icon: 'list', label: '提交计划', summary: firstLine(truncate(i.plan, 120)) };
    case 'EnterPlanMode':
      return { icon: 'list', label: '进入计划模式', summary: '' };
    default: {
      let label = n;
      const mm = n.match(/^mcp__(.+?)__(.+)$/);
      if (mm) label = `${mm[1]} · ${mm[2]}`;
      let summary = '';
      for (const k of PATH_KEYS) if (typeof i[k] === 'string') { summary = shortPath(i[k]); break; }
      if (!summary) {
        for (const v of Object.values(i)) if (typeof v === 'string' && v) { summary = firstLine(truncate(v, 160)); break; }
      }
      return { icon: 'tool', label, summary };
    }
  }
}

// ---------- 差异对比 ----------

function lineDiff(a, b) {
  // LCS 行级 diff；过大时退化为整体替换
  const A = a.split('\n'), B = b.split('\n');
  if (A.length * B.length > 250000) {
    return [...A.map((t) => ['-', t]), ...B.map((t) => ['+', t])];
  }
  const n = A.length, m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let x = n - 1; x >= 0; x--) {
    for (let y = m - 1; y >= 0; y--) {
      dp[x][y] = A[x] === B[y] ? dp[x + 1][y + 1] + 1 : Math.max(dp[x + 1][y], dp[x][y + 1]);
    }
  }
  const out = [];
  let x = 0, y = 0;
  while (x < n && y < m) {
    if (A[x] === B[y]) { out.push([' ', A[x]]); x++; y++; }
    else if (dp[x + 1][y] >= dp[x][y + 1]) { out.push(['-', A[x]]); x++; }
    else { out.push(['+', B[y]]); y++; }
  }
  while (x < n) out.push(['-', A[x++]]);
  while (y < m) out.push(['+', B[y++]]);
  return out;
}

export function renderDiff(oldStr, newStr) {
  const rows = lineDiff(str(oldStr), str(newStr));
  const pre = h('pre', { class: 'diff' });
  // 只保留变化附近 3 行上下文
  const keep = new Array(rows.length).fill(false);
  rows.forEach((r, k) => {
    if (r[0] !== ' ') for (let d = -3; d <= 3; d++) if (k + d >= 0 && k + d < rows.length) keep[k + d] = true;
  });
  let skipped = false;
  rows.forEach((r, k) => {
    if (!keep[k]) {
      if (!skipped) pre.appendChild(h('div', { class: 'diff-skip' }, '⋯'));
      skipped = true;
      return;
    }
    skipped = false;
    const cls = r[0] === '-' ? 'diff-del' : r[0] === '+' ? 'diff-add' : 'diff-ctx';
    pre.appendChild(h('div', { class: cls }, h('span', { class: 'diff-sign' }, r[0]), r[1] || ' '));
  });
  return pre;
}

// ---------- structuredPatch（Edit/Write/MultiEdit 的 toolUseResult.structuredPatch，带新旧行号） ----------

const MAX_PATCH_LINES = 5000;

/** 校验并规整 structuredPatch：[{oldStart, oldLines, newStart, newLines, lines:[' a','-b','+c']}] → 数组或 null */
export function normPatch(p) {
  if (!Array.isArray(p) || !p.length) return null;
  const out = [];
  for (const x of p.slice(0, 500)) {
    if (!x || typeof x !== 'object' || !Array.isArray(x.lines)) return null;
    const num = (v) => (Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0);
    out.push({ oldStart: num(x.oldStart), oldLines: num(x.oldLines), newStart: num(x.newStart), newLines: num(x.newLines),
      lines: x.lines.filter((l) => typeof l === 'string') });
  }
  return out;
}

/** 增删行数 {add, del} */
export function patchStats(hunks) {
  let add = 0, del = 0;
  for (const hk of hunks || []) for (const l of hk.lines || []) { if (l[0] === '+') add++; else if (l[0] === '-') del++; }
  return { add, del };
}

/** LCS 结果的增删行数（没有 structuredPatch 时的后备） */
export function diffStats(oldStr, newStr) {
  let add = 0, del = 0;
  for (const r of lineDiff(str(oldStr), str(newStr))) { if (r[0] === '+') add++; else if (r[0] === '-') del++; }
  return { add, del };
}

/** unified diff（带旧/新行号）：hunk 头 @@ -a,b +c,d @@，行首 ' '/'-'/'+'，"\ No newline…" 作为说明行 */
export function renderPatch(hunks, opts = {}) {
  const box = h('div', { class: 'diff patch' + (opts.cls ? ' ' + opts.cls : '') });
  let maxNo = 1;
  for (const hk of hunks) maxNo = Math.max(maxNo, hk.oldStart + hk.oldLines, hk.newStart + hk.newLines);
  box.style.setProperty('--ln-w', String(maxNo).length + 'ch');
  let shown = 0;
  for (const hk of hunks) {
    box.appendChild(h('div', { class: 'diff-hunk' }, h('span', { class: 'ln' }), h('span', { class: 'ln' }), h('span', { class: 'diff-sign' }),
      `@@ -${hk.oldStart},${hk.oldLines} +${hk.newStart},${hk.newLines} @@`));
    let o = hk.oldStart, n = hk.newStart;
    for (const l of hk.lines) {
      if (shown++ >= MAX_PATCH_LINES) {
        box.appendChild(h('div', { class: 'diff-skip' }, `…（共 ${patchStats(hunks).add + patchStats(hunks).del} 行改动，已截断显示）`));
        return box;
      }
      const c = l[0], text = l.slice(1);
      if (c === '\\') { box.appendChild(h('div', { class: 'diff-meta' }, h('span', { class: 'ln' }), h('span', { class: 'ln' }), h('span', { class: 'diff-sign' }), l)); continue; }
      const cls = c === '-' ? 'diff-del' : c === '+' ? 'diff-add' : 'diff-ctx';
      const on = c === '+' ? '' : String(o), nn = c === '-' ? '' : String(n);
      if (c !== '+') o++;
      if (c !== '-') n++;
      box.appendChild(h('div', { class: cls }, h('span', { class: 'ln' }, on), h('span', { class: 'ln' }, nn),
        h('span', { class: 'diff-sign' }, c === '-' || c === '+' ? c : ' '), text || ' '));
    }
  }
  return box;
}

/** 工具调用对应的 structuredPatch（Edit/MultiEdit/Write update）；没有返回 null */
export function toolPatch(name, structured) {
  if (!structured || typeof structured !== 'object') return null;
  if (!['Edit', 'MultiEdit', 'Write', 'NotebookEdit'].includes(name)) return null;
  return normPatch(structured.structuredPatch);
}

export function renderTodos(todos) {
  const list = h('ul', { class: 'todos' });
  for (const t of Array.isArray(todos) ? todos : []) {
    if (!t) continue;
    const st = str(t.status);
    const mark = st === 'completed' ? icon('check', 'todo-ic done') : st === 'in_progress' ? h('span', { class: 'todo-dot active' }) : h('span', { class: 'todo-dot' });
    list.appendChild(h('li', { class: 'todo ' + st }, mark,
      h('span', { class: 'todo-text' }, str(st === 'in_progress' && t.activeForm ? t.activeForm : t.content))));
  }
  return list;
}

function codeBox(text, cls) {
  return h('pre', { class: 'tool-pre' + (cls ? ' ' + cls : '') }, text);
}

/** 长文本：默认截断显示，可展开 */
export function longText(text, cls, limit = 6000) {
  text = str(text);
  if (text.length <= limit) return codeBox(text, cls);
  const pre = codeBox(text.slice(0, limit), cls);
  const btn = h('button', { class: 'link-btn', type: 'button' }, `显示全部（共 ${text.length} 字符）`);
  btn.addEventListener('click', () => { pre.textContent = text; btn.remove(); });
  return h('div', null, pre, btn);
}

function section(title, ...body) {
  return h('div', { class: 'tool-sec' }, h('div', { class: 'tool-sec-title' }, title), ...body);
}

/** 工具输入的详细展示（structured = toolUseResult：有 structuredPatch 时优先用它渲染带行号的 diff） */
export function renderToolInput(name, input, structured) {
  const i = input && typeof input === 'object' ? input : {};
  const patch = toolPatch(name, structured);
  switch (name) {
    case 'Bash':
      return section('命令', codeBox(str(i.command), 'cmd'), i.description ? h('div', { class: 'tool-desc' }, str(i.description)) : null);
    case 'Write':
      if (patch) return section('写入 ' + str(i.file_path) + '（覆盖原文件）', renderPatch(patch));
      return section('写入 ' + str(i.file_path), longText(i.content, '', 4000));
    case 'Edit':
      return section('编辑 ' + str(i.file_path) + (i.replace_all ? '（全部替换）' : ''), patch ? renderPatch(patch) : renderDiff(i.old_string, i.new_string));
    case 'MultiEdit':
      if (patch) return section('编辑 ' + str(i.file_path), renderPatch(patch));
      return section('编辑 ' + str(i.file_path),
        ...(Array.isArray(i.edits) ? i.edits : []).map((e) => renderDiff(e && e.old_string, e && e.new_string)));
    case 'TodoWrite':
      return renderTodos(i.todos);
    case 'Task': case 'Agent':
      return section((i.subagent_type ? `子代理 ${i.subagent_type} · ` : '') + str(i.description),
        h('div', { class: 'tool-prompt' }, str(i.prompt)));
    case 'AskUserQuestion':
      return section('问题', ...(Array.isArray(i.questions) ? i.questions : []).map((q) =>
        h('div', { class: 'tool-desc' }, str(q && q.question), ' — ',
          (Array.isArray(q && q.options) ? q.options : []).map((o) => str(o && o.label)).join(' / '))));
    default:
      return section('输入', codeBox(jsonPretty(i)));
  }
}

/** 工具结果：string 或 block 数组（text/image/…）。ctx.image(block) = 图片渲染（blobimg.js：引用图片按需加载 + 灯箱） */
export function renderToolResult(content, isError, truncated, ctx) {
  const wrap = h('div', { class: 'tool-sec' }, h('div', { class: 'tool-sec-title' }, isError ? '错误' : '结果'));
  const cls = isError ? 'err' : '';
  if (typeof content === 'string') {
    wrap.appendChild(longText(content || '（无输出）', cls));
  } else if (Array.isArray(content)) {
    for (const b of content) {
      if (!b || typeof b !== 'object') continue;
      if (b.type === 'text') wrap.appendChild(longText(str(b.text), cls));
      else if (b.type === 'image' && b.source) {
        const node = ctx && ctx.image ? ctx.image(b) : null;
        const src = node ? null : b.source.type === 'base64' ? safeImageSrc(b.source.media_type, b.source.data) : null;
        wrap.appendChild(node || (src ? h('img', { class: 'tool-img', src, alt: '工具返回的图片' }) : h('div', { class: 'tool-desc' }, '[图片]')));
      } else wrap.appendChild(codeBox(jsonPretty(b)));
    }
  } else if (content != null) {
    wrap.appendChild(codeBox(jsonPretty(content), cls));
  } else {
    wrap.appendChild(h('div', { class: 'tool-desc' }, '（无输出）'));
  }
  if (truncated) wrap.appendChild(h('div', { class: 'tool-desc' }, '（内容过长，已截断）'));
  return wrap;
}

/** 审批卡里的输入预览 */
export function renderPermissionPreview(toolName, input, req) {
  const i = input && typeof input === 'object' ? input : {};
  const out = [];
  switch (toolName) {
    case 'Bash':
      out.push(codeBox(str(i.command), 'cmd'));
      break;
    case 'Write':
      out.push(h('div', { class: 'perm-path' }, icon('file'), str(i.file_path)));
      out.push(longText(i.content, '', 3000));
      break;
    case 'Edit':
      out.push(h('div', { class: 'perm-path' }, icon('edit'), str(i.file_path)));
      out.push(renderDiff(i.old_string, i.new_string));
      break;
    case 'MultiEdit':
      out.push(h('div', { class: 'perm-path' }, icon('edit'), str(i.file_path)));
      for (const e of Array.isArray(i.edits) ? i.edits : []) out.push(renderDiff(e && e.old_string, e && e.new_string));
      break;
    case 'WebFetch':
      out.push(h('div', { class: 'perm-path' }, icon('globe'), str(i.url)));
      if (i.prompt) out.push(h('div', { class: 'tool-desc' }, str(i.prompt)));
      break;
    case 'ExitPlanMode':
      out.push(longText(str(i.plan), '', 6000));
      break;
    default:
      out.push(codeBox(jsonPretty(i)));
  }
  if (req && req.blocked_path) out.push(h('div', { class: 'tool-desc' }, '涉及路径：' + str(req.blocked_path)));
  if (req && req.decision_reason) out.push(h('div', { class: 'tool-desc' }, '原因：' + str(typeof req.decision_reason === 'string' ? req.decision_reason : jsonPretty(req.decision_reason))));
  return out;
}
