// 连续工具调用组的摘要（docs/SPEC_V2_TUI.md §10.7，P2；思路参考 T3 Code 的 summarizeToolGroup，MIT）：
// 一串连续的工具卡（中间夹着的“思考”不打断）在组首显示一行"读取 3 个文件、运行 2 条命令"。
// 组在追加条目时以 O(1) 维护（noteToolAdded），不在渲染时扫描整个列表。
import { h, icon } from './dom.js';
import { toolCategory } from './tools.js';

const MIN_GROUP = 2;

/** render.js 每追加一个工具卡调用一次：与前一个工具卡（跳过思考）连成一组，组首标脏以刷新摘要 */
export function noteToolAdded(s, t, markDirty) {
  let prev = null;
  for (let k = s.items.length - 2; k >= 0; k--) {
    const it = s.items[k];
    if (it.kind === 'thinking') continue;
    prev = it;
    break;
  }
  if (prev && prev.kind === 'tool') {
    const head = prev.groupHead || prev;
    t.groupHead = head;
    (head.group || (head.group = [head])).push(t);
    markDirty(head);
  } else {
    t.groupHead = null;
    t.group = [t];
  }
}

function plural(n, unit, verb) { return verb + ' ' + n + ' ' + unit; }

/** 组首的摘要行（组内 ≥2 个工具时）；不是组首返回 null */
export function groupSummary(s, t) {
  if (t.groupHead || !Array.isArray(t.group) || t.group.length < MIN_GROUP) return null;
  const tools = t.group;
  const c = { read: new Set(), search: 0, edit: new Set(), bash: 0, mcp: 0, web: 0, agent: 0, todo: 0, other: 0 };
  let failed = 0, running = 0;
  for (const x of tools) {
    const cat = toolCategory(x.name);
    const i = x.input || {};
    if (x.isError || x.denied) failed++;
    else if (!x.done && !x.interrupted) running++;
    if (cat === 'read') { if (x.name === 'Read' || x.name === 'NotebookRead') c.read.add(String(i.file_path || i.notebook_path || x.toolId)); else c.search++; }
    else if (cat === 'edit') c.edit.add(String(i.file_path || i.notebook_path || x.toolId));
    else if (cat in c) c[cat]++;
    else c.other++;
  }
  const parts = [];
  if (c.read.size) parts.push(plural(c.read.size, '个文件', '读取'));
  if (c.search) parts.push(plural(c.search, '次', '搜索'));
  if (c.edit.size) parts.push(plural(c.edit.size, '个文件', '修改'));
  if (c.bash) parts.push(plural(c.bash, '条命令', '运行'));
  if (c.mcp) parts.push(plural(c.mcp, '次', '浏览器/MCP 操作'));
  if (c.web) parts.push(plural(c.web, '次', '联网'));
  if (c.agent) parts.push(plural(c.agent, '个子代理', '派出'));
  if (c.todo) parts.push('更新待办');
  if (c.other) parts.push(plural(c.other, '个工具', '其它'));
  const tail = [];
  if (running) tail.push(running + ' 个进行中');
  if (failed) tail.push(failed + ' 个失败');
  return h('div', { class: 'tool-group-sum' + (failed ? ' has-fail' : '') },
    icon('list', 'tgs-ic'), h('span', null, tools.length + ' 个工具调用：' + parts.join('、')),
    tail.length ? h('span', { class: 'tgs-tail' }, '（' + tail.join('，') + '）') : null);
}
