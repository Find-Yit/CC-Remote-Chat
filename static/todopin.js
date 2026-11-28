// 置顶任务清单（SPEC §7「输入框：置顶任务清单」）：输入框上方显示当前会话最新的任务清单，可折叠，完成数 / 总数，进行中的高亮。
// 数据只来自前端已有的 transcript 条目（render.js 的 s.items 里的工具卡），不发请求、不读文件。
// 两种来源（CLI 2.1.332 实测，见 fixtures/transcript_tasks_2_1_332.jsonl）：
// - TodoWrite：input.todos = [{content, status, activeForm}]，每次调用就是整张清单的快照；
// - TaskCreate / TaskUpdate / TaskList（新版 CLI 的任务清单）：TaskCreate 一次加一项，编号看结果
//   （toolUseResult.task.id，或文字 "Task #N created successfully: …"；模型可能把 subject 写成 title，以结果里的 subject 为准）；
//   TaskUpdate {taskId, status: pending|in_progress|completed|deleted, subject?, activeForm?}（出错的不算；还没出结果的先按它显示，
//   与 CLI 自己的做法一致）；TaskList 的结果（"#1 [completed] 标题"）是整张清单的快照，用来对齐。
// 两种都有时以最后更新的那一种为准。CLI 2.1.332 只对部分模型提供这些工具（实测 Haiku 5.5 没有、Haiku 4.5 有 Task*）。
import { h, icon } from './dom.js';

const STATUSES = new Set(['pending', 'in_progress', 'completed']);
const TASK_TOOLS = new Set(['TodoWrite', 'TaskCreate', 'TaskUpdate', 'TaskList']);
const str = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const statusOf = (v) => (STATUSES.has(v) ? v : 'pending');

function resultText(r) {
  if (typeof r === 'string') return r;
  if (Array.isArray(r)) return r.map((b) => (b && typeof b.text === 'string' ? b.text : '')).join('\n');
  return '';
}

/** 增量收集一个会话的任务类工具卡（s.items 被整体替换 / 变短 = transcript reset → 重新收集） */
export function taskTools(s) {
  const items = (s && s.items) || [];
  let c = s.__todoScan;
  if (!c || c.items !== items || c.n > items.length) c = s.__todoScan = { items, n: 0, tools: [] };
  for (let i = c.n; i < items.length; i++) {
    const it = items[i];
    if (it && it.kind === 'tool' && TASK_TOOLS.has(it.name)) c.tools.push(it);
  }
  c.n = items.length;
  return c.tools;
}

/** 工具卡（按时间顺序）→ 当前清单 {source:'todo'|'tasks', items:[{id, content, activeForm, status}], done, total, current} | null */
export function todoModel(tools) {
  let todo = null, todoAt = -1, tasksAt = -1;
  let tasks = new Map();
  (tools || []).forEach((t, k) => {
    if (!t || !t.name) return;
    const inp = t.input && typeof t.input === 'object' ? t.input : {};
    const failed = t.done && t.isError;
    if (t.name === 'TodoWrite') {
      if (failed || !Array.isArray(inp.todos)) return;
      todo = inp.todos.filter((x) => x && typeof x === 'object').map((x, i) => ({
        id: String(i + 1), content: str(x.content || x.subject), activeForm: str(x.activeForm), status: statusOf(x.status),
      }));
      todoAt = k;
    } else if (t.name === 'TaskCreate') {
      if (failed) return;
      const st = t.structured && typeof t.structured === 'object' ? t.structured : null;
      const task = st && st.task && typeof st.task === 'object' ? st.task : null;
      let id = task && task.id != null ? String(task.id) : '';
      if (!id && t.done) { const m = /^Task #(\S+) created successfully/.exec(resultText(t.result)); if (m) id = m[1]; }
      if (!id) { if (t.done) return; id = 'new:' + (t.toolId || k); }   // 还没出结果：先显示，编号等结果
      const subject = str((task && task.subject) || inp.subject || inp.title || inp.content || inp.description);
      tasks.set(id, { id, content: subject, activeForm: str(inp.activeForm || inp.active_form), status: 'pending' });
      tasksAt = k;
    } else if (t.name === 'TaskUpdate') {
      const st = t.structured && typeof t.structured === 'object' ? t.structured : null;
      if (failed || (st && st.success === false)) return;
      const id = str(inp.taskId ?? inp.task_id ?? inp.id ?? (st && st.taskId));
      if (!id) return;
      if (inp.status === 'deleted') { tasks.delete(id); tasksAt = k; return; }
      const prev = tasks.get(id);
      tasks.set(id, {
        id,
        content: str(inp.subject || (prev && prev.content) || '#' + id),
        activeForm: str(inp.activeForm || inp.active_form || (prev && prev.activeForm)),
        status: STATUSES.has(inp.status) ? inp.status : (prev ? prev.status : 'pending'),
      });
      tasksAt = k;
    } else if (t.name === 'TaskList') {
      if (!t.done || failed) return;
      const text = resultText(t.result).trim();
      if (/^No tasks found/.test(text)) { tasks = new Map(); tasksAt = k; return; }
      const next = new Map();
      for (const line of text.split('\n')) {
        const m = /^#(\S+) \[(\w+)\] (.*?)(?: \([^()]*\))?(?: \[blocked by [^\]]*\])?$/.exec(line.trim());
        if (!m) continue;
        const prev = tasks.get(m[1]);
        next.set(m[1], { id: m[1], content: m[3], activeForm: prev ? prev.activeForm : '', status: statusOf(m[2]) });
      }
      if (next.size || !text) { tasks = next; tasksAt = k; }
    }
  });
  let list, source;
  if (todoAt > tasksAt) { list = todo || []; source = 'todo'; }
  else if (tasksAt >= 0) {
    source = 'tasks';
    list = [...tasks.values()].sort((a, b) => {
      const x = Number(a.id), y = Number(b.id);
      if (Number.isFinite(x) && Number.isFinite(y)) return x - y;
      if (Number.isFinite(x)) return -1;
      if (Number.isFinite(y)) return 1;
      return 0;
    });
  } else return null;
  if (!list.length) return null;
  const done = list.filter((x) => x.status === 'completed').length;
  const cur = list.find((x) => x.status === 'in_progress') || null;
  return { source, items: list, done, total: list.length, current: cur, allDone: done === list.length };
}

/** 进行中的一项显示 activeForm（"正在…"），其余显示 content */
export function itemText(x) { return x.status === 'in_progress' && x.activeForm ? x.activeForm : x.content || ('#' + x.id); }

/** 清单的指纹：内容 / 状态变了才重画（也用于「收起到下次更新」） */
export function modelKey(m) { return m ? m.source + '|' + m.items.map((x) => x.id + ':' + x.status + ':' + itemText(x)).join('\n') : ''; }

const COLLAPSE_KEY = 'ccr.todoPin.collapsed';

/** 输入框上方的任务清单条。update(s) 每次界面刷新时调用（只在清单变化时改 DOM） */
export class TodoPin {
  constructor({ storage = null } = {}) {
    this.storage = storage;
    this.collapsed = false;
    try { this.collapsed = !!storage && storage.getItem(COLLAPSE_KEY) === '1'; } catch (_) { /* 忽略 */ }
    this.key = null;
    this.model = null;
    this.sid = null;
    this.openDone = false;   // 全部完成时默认收起；用户点开后在清单变化前保持展开
    this.el = h('div', { class: 'todo-pin', hidden: true, role: 'region', 'aria-label': '任务清单' });
    this.head = h('button', { class: 'todo-pin-head', type: 'button' });
    this.list = h('ol', { class: 'todo-pin-list' });
    this.el.append(this.head, this.list);
    this.head.addEventListener('click', () => this.toggle());
  }

  isCollapsed() { return this.model && this.model.allDone ? !this.openDone : this.collapsed; }

  toggle() {
    if (this.model && this.model.allDone) this.openDone = !this.openDone;
    else {
      this.collapsed = !this.collapsed;
      try { if (this.storage) this.storage.setItem(COLLAPSE_KEY, this.collapsed ? '1' : '0'); } catch (_) { /* 忽略 */ }
    }
    this.paint();
  }

  /** s = 当前会话（null = 没有会话 / 历史预览） */
  update(s) {
    const m = s && !s.readOnly ? todoModel(taskTools(s)) : null;
    const key = (s ? s.sid : '') + '#' + modelKey(m);
    if (key === this.key) return;
    if (!s || s.sid !== this.sid || !this.model || !m || modelKey(m) !== modelKey(this.model)) this.openDone = false;
    this.key = key;
    this.sid = s ? s.sid : null;
    this.model = m;
    this.paint();
  }

  paint() {
    const m = this.model;
    this.el.hidden = !m;
    if (!m) { this.head.replaceChildren(); this.list.replaceChildren(); return; }
    const collapsed = this.isCollapsed();
    this.el.classList.toggle('collapsed', collapsed);
    this.el.classList.toggle('all-done', m.allDone);
    this.el.dataset.source = m.source;
    this.head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    this.head.title = (collapsed ? '展开' : '收起') + '任务清单（来自 ' + (m.source === 'todo' ? 'TodoWrite' : 'TaskCreate / TaskUpdate') + '）';
    const pct = m.total ? Math.round((m.done / m.total) * 100) : 0;
    this.head.replaceChildren(...[
      icon(collapsed ? 'chevron' : 'chevdown', 'todo-pin-chev'),
      h('span', { class: 'todo-pin-title' }, m.allDone ? '任务清单 · 全部完成' : '任务清单'),
      h('span', { class: 'todo-pin-count' }, m.done + '/' + m.total),
      h('span', { class: 'todo-pin-bar', 'aria-hidden': 'true' }, h('span', { class: 'todo-pin-fill', style: { width: pct + '%' } })),
      collapsed && m.current ? h('span', { class: 'todo-pin-cur' }, itemText(m.current)) : null].filter(Boolean));
    this.list.hidden = collapsed;
    this.list.replaceChildren(...m.items.map((x) => h('li', { class: 'todo-pin-item st-' + x.status, title: x.content },
      x.status === 'completed' ? icon('check', 'todo-pin-ic') : h('span', { class: 'todo-pin-dot' }),
      h('span', { class: 'todo-pin-text' }, itemText(x)))));
  }
}
