// 子代理面板：输入框上方的托盘（正在干活的子代理一目了然）+ 右侧抽屉（某个子代理的完整过程）。
// 数据来自 render.js 维护的 s.subagents（服务器 kind=subagent 事件：摘要 + 增量条目）；抽屉复用主聊天的渲染器
// （mountSession/applyTranscript，条目本身带 isSidechain，视图标记 sidechain=true）。只读：这里没有任何发往终端的操作。
import { h, icon, truncate } from './dom.js';
import {
  createState, ensureSession, applyTranscript, mountSession, scrollToBottom, flushNow,
  subagentComplete, setSubagentDetail, saStatusIcon, saStatusLine, saElapsed, fmtElapsed, SA_STATUS_TEXT,
} from './render.js';
import { renderMarkdown } from './markdown.js';

const ORDER = { running: 0, failed: 1, killed: 2, completed: 3 };

function sortedAgents(s) {
  const out = [];
  for (const a of s.subagents.values()) if (a.summary) out.push(a);
  out.sort((x, y) => {
    const a = x.summary, b = y.summary;
    const ra = a.status === 'running' ? 0 : 1, rb = b.status === 'running' ? 0 : 1;
    if (ra !== rb) return ra - rb;
    const ta = (ra === 0 ? a.started_at : a.ended_at || a.last_activity) || 0;
    const tb = (rb === 0 ? b.started_at : b.ended_at || b.last_activity) || 0;
    return tb - ta || (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || String(x.id).localeCompare(String(y.id));
  });
  return out;
}

function typeModel(sum) {
  const bits = [];
  if (sum.agent_type) bits.push(sum.agent_type);
  if (sum.model) bits.push(sum.model);
  return bits.join(' · ');
}

function fmtTokens(n) {
  if (typeof n !== 'number') return '';
  return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k tokens' : n + ' tokens';
}

/** 子代理视图要显示的条目：去掉开头那条任务 prompt（抽屉顶部已单独显示） */
function bodyEntries(entries) {
  return entries.filter((e) => !(e.type === 'user' && e.parentUuid == null && typeof (e.message || {}).content === 'string'));
}

export class SubagentUI {
  constructor({ api, toast }) {
    this.api = api;
    this.toast = toast || (() => {});
    this.session = null;       // 当前（活动）会话
    this.trayUi = new Map();   // sid -> {collapsed:null|bool, lastRunning}
    this.drawer = null;        // {s, aid, ps, view, els, loading}
    this.trayKey = '';
    this.tray = h('div', { class: 'sa-tray', hidden: true, role: 'region', 'aria-label': '子代理' });
    const inner = document.querySelector('#composer .composer-inner');
    if (inner) inner.insertBefore(this.tray, inner.firstChild);
    this.ticker = setInterval(() => this.tick(), 1000);
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !this.drawer || e.defaultPrevented) return;
      if (document.querySelector('dialog[open], .popover')) return;   // Esc 先给弹窗/弹层
      e.preventDefault();
      this.close();
    });
  }

  // ------------------------------------------------------------ 外部接口
  /** 当前会话（null = 没有 / 历史预览） */
  setSession(s) {
    if (s !== this.session) {
      this.session = s;
      this.trayKey = '';
      if (this.drawer && this.drawer.s !== s) this.close();
    }
    this.renderTray();
  }

  /** render.js 的 onSubagent 回调：aid=null 表示整体重建 */
  onUpdate(s, aid, added, reset) {
    if (s === this.session) this.renderTray();
    const d = this.drawer;
    if (!d || d.s !== s) return;
    if (aid === null) { this.renderDrawerHead(); return; }
    if (aid !== d.aid) return;
    const a = s.subagents.get(aid);
    if (!a) { this.renderDrawerHead(); return; }
    if (reset || d.gen !== a.gen) {
      if (subagentComplete(a)) this.rebuild(a);
      else this.load();
    } else if (added && added.length) {
      applyTranscript(d.ps, bodyEntries(added), false);
    }
    this.renderDrawerHead();
  }

  // ------------------------------------------------------------ 托盘
  renderTray() {
    // 托盘只显示正在运行的子代理；完成/失败/终止后立即从托盘移除（过程仍可从聊天里的 Agent 卡或完成通知行打开抽屉查看）。
    const s = this.session;
    const running = s && !s.readOnly ? sortedAgents(s).filter((a) => a.summary.status === 'running') : [];
    if (!running.length) {
      if (!this.tray.hidden) { this.tray.hidden = true; this.tray.replaceChildren(); }
      this.trayKey = '';
      return;
    }
    let ui = this.trayUi.get(s.sid);
    if (!ui) { ui = { collapsed: null, lastRunning: 0 }; this.trayUi.set(s.sid, ui); }
    if (!ui.lastRunning) ui.collapsed = null;   // 有新的子代理开始干活：自动展开
    ui.lastRunning = running.length;
    const collapsed = ui.collapsed != null ? ui.collapsed : false;
    const openAid = this.drawer && this.drawer.s === s ? this.drawer.aid : null;
    const key = JSON.stringify([s.sid, collapsed, openAid, running.map((a) => {
      const m = a.summary;
      return [a.id, m.status, m.description, m.agent_type, m.model, m.tool_uses, m.last_action, m.started_at, m.duration_ms];
    })]);
    if (key === this.trayKey) return;
    this.trayKey = key;
    const head = h('button', { class: 'sa-tray-head', type: 'button', 'aria-expanded': String(!collapsed), title: collapsed ? '展开子代理列表' : '收起子代理列表' },
      icon('agent', 'sa-tray-ic'),
      h('span', { class: 'sa-tray-title' }, '子代理'),
      h('span', { class: 'sa-count run' }, spinnerDot(), running.length + ' 个运行中'),
      h('span', { class: 'spacer' }),
      icon(collapsed ? 'chevron' : 'chevdown', 'chev'));
    head.addEventListener('click', () => { ui.collapsed = !collapsed; this.trayKey = ''; this.renderTray(); });
    const nodes = [head];
    if (!collapsed) {
      nodes.push(h('div', { class: 'sa-list', role: 'list' }, running.map((a) => this.trayItem(s, a, a.id === openAid))));
    }
    this.tray.replaceChildren(...nodes);
    this.tray.hidden = false;
  }

  trayItem(s, a, isOpen) {
    const m = a.summary;
    const { st, dur, tools, action } = saStatusLine(m, null);
    const tm = typeModel(m);
    const meta = [];
    if (tm) meta.push(h('span', { class: 'sa-type mono' }, tm));
    const el = saElapsed(m, dur);
    if (el) meta.push(el);
    if (tools != null) meta.push(h('span', { class: 'sa-tools' }, tools + ' 次工具'));
    const act = st === 'running' ? action : (m.result ? '结果：' + String(m.result).split('\n')[0] : action);
    const b = h('button', { class: 'sa-item st-' + st + (isOpen ? ' open' : ''), type: 'button', role: 'listitem',
      title: (m.description || '') + (m.prompt ? '\n\n' + truncate(m.prompt, 400) : '') + '\n\n点击查看完整过程' },
    h('span', { class: 'sa-st', title: SA_STATUS_TEXT[st] || st }, saStatusIcon(st)),
    h('span', { class: 'sa-desc' }, m.description || a.id),
    h('span', { class: 'sa-meta' }, meta),
    act ? h('span', { class: 'sa-act mono' }, act) : null);
    b.addEventListener('click', () => this.open(s, a.id));
    return b;
  }

  tick() {
    const now = Date.now() / 1000;
    for (const el of document.querySelectorAll('[data-sa-since]')) {
      const t = Number(el.dataset.saSince);
      if (isFinite(t)) {
        const txt = fmtElapsed(now - t);
        if (el.textContent !== txt) el.textContent = txt;
      }
    }
  }

  // ------------------------------------------------------------ 抽屉
  open(s, aid) {
    if (!s || !aid) return;
    if (this.drawer && this.drawer.s === s && this.drawer.aid === aid) { this.renderDrawerHead(); return; }
    this.close(true);
    const host = document.querySelector('main.main') || document.body;
    const els = {};
    els.st = h('span', { class: 'sa-st' });
    els.title = h('div', { class: 'sa-dtitle' });
    const closeBtn = h('button', { class: 'icon-btn sa-close', type: 'button', title: '关闭（Esc）', 'aria-label': '关闭' }, icon('x'));
    closeBtn.addEventListener('click', () => this.close());
    els.meta = h('div', { class: 'sa-dmeta' });
    els.prompt = h('details', { class: 'sa-prompt' });
    els.result = h('div', { class: 'sa-dresult' });
    els.note = h('div', { class: 'sa-dnote', hidden: true });
    els.body = h('div', { class: 'sa-dbody' });
    els.root = h('aside', { class: 'sa-drawer', role: 'dialog', 'aria-label': '子代理过程' },
      h('div', { class: 'sa-dhead' }, els.st, els.title, closeBtn),
      els.meta, els.prompt, els.result, els.note, els.body);
    host.appendChild(els.root);
    host.classList.add('sa-drawer-open');   // 宽屏时主区让出右侧（见 style.css），窄屏为覆盖层
    const ps = this.makeView(s, aid);
    this.drawer = { s, aid, ps, els, gen: null, loading: false, promptKey: '', resultKey: '' };
    const a = s.subagents.get(aid);
    if (a && subagentComplete(a)) this.rebuild(a);
    else this.load();
    this.renderDrawerHead();
    this.trayKey = '';
    this.renderTray();
  }

  close(silent) {
    const d = this.drawer;
    if (!d) return;
    this.drawer = null;
    d.els.root.remove();
    if (d.els.root.parentNode === null && !document.querySelector('.sa-drawer')) {
      for (const el of document.querySelectorAll('.sa-drawer-open')) el.classList.remove('sa-drawer-open');
    }
    if (!silent) { this.trayKey = ''; this.renderTray(); }
  }

  makeView(s, aid) {
    const st = createState({ actions: { openSubagent: (root, id) => this.open(root, id) } });
    const ps = ensureSession(st, 'sub:' + aid, { title: aid, state: 'exited' });
    ps.readOnly = true;
    ps.sidechain = true;
    ps.parent = s;
    return ps;
  }

  rebuild(a) {
    const d = this.drawer;
    if (!d) return;
    d.gen = a.gen;
    const ps = this.makeView(d.s, d.aid);
    d.ps = ps;
    d.els.body.replaceChildren();
    applyTranscript(ps, bodyEntries(a.entries), true);
    mountSession(ps, d.els.body);
    flushNow(ps);
    requestAnimationFrame(() => scrollToBottom(ps));
    d.els.note.hidden = !(a.truncated || a.base > 0);
    d.els.note.textContent = a.base > 0 ? `更早的 ${a.base} 条记录已超出缓存上限，未显示` : '';
    this.renderDrawerHead();
  }

  async load() {
    const d = this.drawer;
    if (!d || d.loading) return;
    d.loading = true;
    this.renderDrawerHead();
    const { s, aid } = d;
    try {
      const r = await this.api.get(this.api.sessionPath(s.sid, '/subagents/' + encodeURIComponent(aid)));
      if (this.drawer !== d) return;
      const a = setSubagentDetail(s, r);
      d.loading = false;
      if (a) this.rebuild(a);
    } catch (e) {
      if (this.drawer !== d) return;
      d.loading = false;
      if (e && e.status === 404) {
        // 还没收到这个子代理的记录（例如刚启动）：等实时事件
        d.els.note.hidden = false;
        d.els.note.textContent = '暂时还没有这个子代理的过程记录，收到后会自动显示。';
      } else this.toast('读取子代理记录失败：' + ((e && e.message) || e), 'error');
    }
    this.renderDrawerHead();
  }

  renderDrawerHead() {
    const d = this.drawer;
    if (!d) return;
    const a = d.s.subagents.get(d.aid);
    const m = (a && a.summary) || {};
    const { st, dur, tools } = saStatusLine(m.agent_id ? m : null, null);
    const els = d.els;
    els.root.classList.toggle('running', st === 'running');
    els.st.replaceChildren(saStatusIcon(st));
    els.st.title = SA_STATUS_TEXT[st] || st;
    els.title.textContent = m.description || d.aid;
    const bits = [];
    bits.push(h('span', { class: 'sa-chip st-' + st }, SA_STATUS_TEXT[st] || st));
    const tm = typeModel(m);
    if (tm) bits.push(h('span', { class: 'mono' }, tm + (m.resolved_model && m.resolved_model !== m.model ? '（' + m.resolved_model + '）' : '')));
    if (m.background) bits.push(h('span', null, '后台'));
    const el = saElapsed(m.agent_id ? m : null, dur);
    if (el) bits.push(h('span', null, st === 'running' ? '已运行 ' : '用时 ', el));
    if (tools != null) bits.push(h('span', null, tools + ' 次工具调用'));
    const tok = m.usage && (m.usage.subagent_tokens ?? m.usage.total_tokens);
    if (tok != null) bits.push(h('span', null, fmtTokens(tok)));
    if (st === 'running' && m.last_action) bits.push(h('span', { class: 'mono sa-action' }, m.last_action));
    if (d.loading) bits.push(h('span', { class: 'muted' }, '加载中…'));
    bits.push(h('span', { class: 'muted mono sa-aid', title: 'agentId' }, d.aid));
    els.meta.replaceChildren(...bits);
    // prompt（子代理收到的任务）：只在内容变化时重建，保留展开状态
    const prompt = m.prompt ? String(m.prompt) : '';
    if (prompt !== d.promptKey) {
      d.promptKey = prompt;
      els.prompt.hidden = !prompt;
      els.prompt.open = prompt.length <= 400;
      els.prompt.replaceChildren(h('summary', null, '任务（子代理收到的 prompt）'), h('div', { class: 'sa-prompt-body' }, prompt));
    }
    const res = st !== 'running' && m.result ? String(m.result) : '';
    const rkey = st + '|' + res;
    if (rkey !== d.resultKey) {
      d.resultKey = rkey;
      if (res) {
        els.result.replaceChildren(h('details', { class: 'sa-result', open: true },
          h('summary', null, st === 'completed' ? '结果' : '结束信息', m.result_truncated ? '（已截断）' : ''),
          h('div', { class: 'md sa-result-md' }, renderMarkdown(res))));
        els.result.hidden = false;
      } else {
        els.result.replaceChildren();
        els.result.hidden = true;
      }
    }
  }
}

function spinnerDot() { return h('span', { class: 'sa-dot' }); }

