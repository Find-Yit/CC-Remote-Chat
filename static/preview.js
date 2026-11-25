// 右侧预览面板（docs/SPEC_V2_TUI.md §10.4）：可拖拽改宽度、可收起/关闭、多标签，每个会话一组标签。
// 标签类型：
//   file（仅本机会话）：图片 / SVG（当图片显示）/ HTML（沙箱 iframe，绝不 allow-same-origin）/ Markdown（markdown.js）/
//        文本代码（带行号，大文件截断）/ PDF / 音视频 / 二进制（给"用默认应用打开"）。打开期间每 1.5s 查一次 mtime，
//        变了自动刷新（HTML 页面连同本会话改过的同目录文件一起盯）。
//   url（仅本机会话）：http://localhost:PORT/…（不同端口 = 不同 origin），沙箱 iframe；可选"兼容模式"（仅对这类页面
//        加 allow-same-origin，给 Vite 等模块脚本页面用；/preview/ 同源页面永远不给）。
//   diff（服务器 + 本机）：本轮改动的 unified diff，数据全部来自 transcript 的 structuredPatch，不读文件。
//        （「本轮改动」卡片现在改为打开下面的「改动」标签；openPanel({kind:'diff'}) 仍可用。）
//   changes「改动」（服务器 + 本机，changes.js）：固定在最左、不可关闭；开关状态全局一个（localStorage ccr.chg），
//        每个会话一个 ChangesView（范围 / 展开状态随会话保留）。数据只来自前端已有的 transcript 条目，不发请求。
// 所有文件访问都经后端 /api/sessions/<sid>/local/*（token）与 /preview/<cap>/…（只读）；按钮只在用户点击时调用。
import { h, icon, copyText, basename, fmtSize } from './dom.js';
import { CREDENTIALS } from './api.js';
import { isLocal, statMany, localOpen, toast, rootOf } from './viz.js';
import { renderMarkdown } from './markdown.js';
import { renderPatch, renderDiff } from './tools.js';
import { markdownCtx, displayPath, resolvePath } from './filelinks.js';
import { chatImage } from './blobimg.js';
import { highlightInto } from './highlight.js';
import { ChangesView, trackerOf, compactCount } from './changes.js';

const POLL_MS = 1500;
const MAX_TEXT = 2 * 1024 * 1024;
const MAX_LINES = 20000;
const W_KEY = 'ccr.pvw';
const SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals';
const CHG_KEY = 'ccr.chg';
const NARROW = '(max-width: 1100px)';   // 与 style.css 一致：这么窄时面板是覆盖层
const CHG = { id: 'chg', kind: 'changes', key: 'changes', path: '' };   // 「改动」标签（所有会话共用这个标签对象）

let seq = 0;

export class PreviewPanel {
  /** opts：host；chgKey（开关状态的 localStorage 键，回放页用自己的键）；onState()（面板显示 / 改动数变化时，会话头按钮据此刷新） */
  constructor({ host, chgKey = CHG_KEY, onState = null } = {}) {
    this.host = host || document.getElementById('app') || document.body;
    this.bySid = new Map();      // sid -> {s, tabs:[], active, chg: ChangesView|null}
    this.s = null;
    this.collapsed = false;
    this.chgKey = chgKey;
    this.onState = onState;
    this.chgOpen = readFlag(chgKey);
    // 记住的是「打开」，但窄窗口下面板是覆盖层：启动时先收起成右缘胶囊，不盖住时间线
    if (this.chgOpen && matchNarrow()) this.collapsed = true;
    this.width = clampW(Number(localStorage.getItem(W_KEY)) || 560);
    this.build();
    this.timer = setInterval(() => this.poll(), POLL_MS);
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') this.poll(); });
  }

  // ------------------------------------------------------------ DOM
  build() {
    this.tabsEl = h('div', { class: 'pv-tabs', role: 'tablist' });
    const collapse = h('button', { class: 'icon-btn pv-hd-btn', type: 'button', title: '收起面板（标签保留）', 'aria-label': '收起面板' }, icon('chevron'));
    collapse.addEventListener('click', () => this.setCollapsed(true));
    const closeAll = h('button', { class: 'icon-btn pv-hd-btn', type: 'button', title: '关闭面板（所有标签，含「改动」）', 'aria-label': '关闭面板' }, icon('x'));
    closeAll.addEventListener('click', () => this.closeAll());
    this.barEl = h('div', { class: 'pv-bar' });
    this.bodyEl = h('div', { class: 'pv-body' });
    this.handle = h('div', { class: 'pv-resize', role: 'separator', 'aria-orientation': 'vertical', title: '拖拽调整宽度（双击恢复默认）' });
    this.root = h('aside', { class: 'pv', hidden: true, 'aria-label': '预览面板' },
      this.handle, h('div', { class: 'pv-head' }, this.tabsEl, collapse, closeAll), this.barEl, this.bodyEl);
    this.pill = h('button', { class: 'pv-pill', type: 'button', hidden: true, title: '展开预览面板' }, icon('panel'), h('span', null, '预览'));
    this.pill.addEventListener('click', () => this.setCollapsed(false));
    this.host.appendChild(this.root);
    document.body.appendChild(this.pill);
    this.applyWidth();
    this.handle.addEventListener('pointerdown', (e) => this.startResize(e));
    this.handle.addEventListener('dblclick', () => { this.width = 560; this.applyWidth(); localStorage.setItem(W_KEY, '560'); });
  }

  applyWidth() { document.documentElement.style.setProperty('--pv-w', this.width + 'px'); }

  startResize(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    this.root.classList.add('resizing');
    const move = (ev) => { this.width = clampW(window.innerWidth - ev.clientX); this.applyWidth(); };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      this.root.classList.remove('resizing');
      localStorage.setItem(W_KEY, String(Math.round(this.width)));
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  // ------------------------------------------------------------ 外部接口
  /** 当前会话（切换会话时由 app.js 调用；null = 没有） */
  setSession(s) {
    s = s ? rootOf(s) : null;
    if (s === this.s) { this.updateVisibility(); return; }
    this.s = s;
    this.renderAll();
  }

  // ------------------------------------------------------------ 「改动」标签
  /** 「改动」标签是否正在显示（会话头按钮的按下状态） */
  isShowingChanges() {
    if (!this.chgOpen || this.collapsed || this.root.hidden || !this.s) return false;
    return this.current().tab === CHG;
  }

  /** 会话头按钮：正在显示 → 关掉「改动」标签；否则打开并切到它 */
  toggleChanges(s) {
    if (this.isShowingChanges() && (!s || rootOf(s) === this.s)) this.setChanges(false);
    else this.openChanges(s || this.s, null);
  }

  /** 打开「改动」标签并切到它；reveal = {toolId?, turn?, path?}（「本轮改动」卡片联动：切到那一轮、展开并滚到那个文件） */
  openChanges(s, reveal) {
    s = s ? rootOf(s) : null;
    if (!s) return;
    this.setChanges(true, false);
    this.s = s;
    const st = this.stateOf(s);
    st.active = CHG.id;
    this.collapsed = false;
    this.renderAll();
    if (reveal) {
      const view = this.viewOf(st);
      let turn = reveal.turn != null ? reveal.turn : null;
      if (turn == null && reveal.toolId) turn = trackerOf(s).turnOfTool(reveal.toolId);
      view.reveal({ turn, path: reveal.path });
    }
  }

  /** 打开 / 关闭「改动」标签（全局；记在 localStorage） */
  setChanges(v, render = true) {
    v = !!v;
    if (v !== this.chgOpen) {
      this.chgOpen = v;
      try { localStorage.setItem(this.chgKey, v ? '1' : '0'); } catch (_) { /* 忽略 */ }
    }
    if (render) this.renderAll();
  }

  viewOf(st) {
    if (!st.chg) {
      const s = st.s;
      st.chg = new ChangesView(s, {
        session: () => st.s,
        cwd: () => (st.s.absCwd || (st.s.meta && st.s.meta.cwd) || ''),
        local: () => isLocal(st.s),
        onOpenFile: (abs) => this.open(st.s, { kind: 'file', path: abs }),
        toast: (t) => toast(t),
      });
    }
    return st.chg;
  }

  /** 会话有新条目 / 子代理有变化（app.js 的 onFlush / onSubagent）：增量更新改动；正在显示「改动」标签就原地刷新 */
  onSessionUpdate(s) {
    s = s ? rootOf(s) : null;
    if (!s || s !== this.s) return;
    const tr = trackerOf(s);
    tr.update(s);   // 增量：只看新条目与还没有结果的编辑调用
    if (tr.version === this.seenVersion && s === this.seenS) return;
    this.seenVersion = tr.version;
    this.seenS = s;
    if (this.isShowingChanges()) this.viewOf(this.stateOf(s)).update();
    this.updateChgTab();
    if (this.onState) this.onState();
  }

  /** 本会话改了几个文件（会话头按钮的角标） */
  changeCount(s) {
    s = s ? rootOf(s) : null;
    if (!s) return 0;
    const tr = trackerOf(s);
    tr.update(s);
    return tr.aggregate('all', s.absCwd || (s.meta && s.meta.cwd) || '').files.length;
  }

  updateChgTab() {
    const el = this.tabsEl.querySelector('.pv-tab.is-chg .pv-tab-n');
    if (!el || !this.s) return;
    const n = this.changeCount(this.s);
    const text = n ? compactCount(n) : '';
    if (el.textContent !== text) el.textContent = text;
    el.hidden = !n;
  }

  /** 打开一个标签：{kind:'file', path, info?, line?} / {kind:'url', url} / {kind:'diff', path, change} */
  open(s, target) {
    s = rootOf(s);
    if (!s || !target) return;
    if ((target.kind === 'file' || target.kind === 'url') && !isLocal(s)) {
      toast('服务器会话不能预览本机文件', 'warn');
      return;
    }
    const st = this.stateOf(s);
    const key = target.kind + ':' + (target.kind === 'url' ? target.url : target.path) + (target.kind === 'diff' ? ':' + (target.change ? target.change.edits.length : 0) : '');
    let tab = st.tabs.find((t) => t.key === key);
    if (!tab) {
      tab = { id: 'pv' + (++seq), key, ...target, sameOrigin: false, v: 0, mtime: null, size: null, el: null };
      st.tabs.push(tab);
      if (st.tabs.length > 12) st.tabs.shift();
    } else if (target.change) tab.change = target.change;
    if (target.line) tab.line = target.line;
    st.active = tab.id;
    if (this.s !== s) this.s = s;
    this.collapsed = false;
    tab.el = null;   // 重建内容（例如 diff 有更新）
    this.renderAll();
  }

  stateOf(s) {
    let st = this.bySid.get(s.sid);
    if (!st) { st = { s, tabs: [], active: null }; this.bySid.set(s.sid, st); }
    st.s = s;
    return st;
  }

  /** 标签列表：「改动」（打开时，固定最左）+ 该会话的其它标签 */
  tabsOf(st) { return st ? (this.chgOpen ? [CHG, ...st.tabs] : st.tabs) : []; }

  current() {
    const st = this.s ? (this.chgOpen ? this.stateOf(this.s) : this.bySid.get(this.s.sid)) : null;
    const tabs = this.tabsOf(st);
    if (!tabs.length) return { st, tab: null };
    return { st, tab: tabs.find((t) => t.id === st.active) || tabs[tabs.length - 1] };
  }

  setCollapsed(v) {
    this.collapsed = !!v;
    if (!this.collapsed) { this.renderAll(); return; }   // 收起期间「改动」不刷新，展开时补上
    this.updateVisibility();
  }

  closeTab(id) {
    const { st } = this.current();
    if (!st) return;
    const k = st.tabs.findIndex((t) => t.id === id);
    if (k < 0) return;
    st.tabs.splice(k, 1);
    const tabs = this.tabsOf(st);
    if (st.active === id) st.active = tabs.length ? tabs[Math.max(0, Math.min(tabs.length - 1, k - 1 + (this.chgOpen ? 1 : 0)))].id : null;
    this.renderAll();
  }

  /** 关闭面板：该会话的所有标签 + 「改动」标签（全局开关一起关掉） */
  closeAll() {
    const { st } = this.current();
    if (st) { st.tabs = []; st.active = null; }
    this.setChanges(false, false);
    this.renderAll();
  }

  updateVisibility() {
    const { st, tab } = this.current();
    const n = this.tabsOf(st).length;
    const show = n > 0 && !this.collapsed;
    const was = !this.root.hidden;
    this.root.hidden = !show;
    this.pill.hidden = !(n > 0 && this.collapsed);
    if (!this.pill.hidden) this.pill.lastChild.textContent = tab === CHG && n === 1 ? '改动' : '预览 · ' + n;
    document.body.classList.toggle('pv-open', show);
    if (this.onState && was !== show) this.onState();
  }

  // ------------------------------------------------------------ 渲染
  renderAll() {
    const { st, tab } = this.current();
    this.updateVisibility();
    if (this.onState) this.onState();
    if (!st || !tab) { this.tabsEl.replaceChildren(); this.barEl.replaceChildren(); this.bodyEl.replaceChildren(); return; }
    st.active = tab.id;
    const tabs = this.tabsOf(st);
    this.tabsEl.replaceChildren(...tabs.map((t) => {
      if (t === CHG) {
        const n = this.changeCount(st.s);
        const b = h('div', { class: 'pv-tab is-chg' + (t === tab ? ' on' : ''), role: 'tab', 'aria-selected': String(t === tab),
          title: '代码改动：本会话 Claude 用 Edit / Write / NotebookEdit 改了哪些文件（固定标签；关闭请点会话头的「改动」按钮或右上角 ×）' },
        icon('diff', 'pv-tab-ic'), h('span', { class: 'pv-tab-t' }, '改动'), h('b', { class: 'pv-tab-n', hidden: !n }, n ? compactCount(n) : ''));
        b.addEventListener('click', () => { st.active = CHG.id; this.renderAll(); });
        return b;
      }
      const b = h('div', { class: 'pv-tab' + (t === tab ? ' on' : ''), role: 'tab', 'aria-selected': String(t === tab), title: tabTitle(t) },
        icon(t.kind === 'diff' ? 'diff' : t.kind === 'url' ? 'globe' : kindIcon(t), 'pv-tab-ic'),
        h('span', { class: 'pv-tab-t' }, tabLabel(t)));
      const x = h('button', { class: 'pv-tab-x', type: 'button', title: '关闭标签', 'aria-label': '关闭标签' }, icon('x'));
      x.addEventListener('click', (e) => { e.stopPropagation(); this.closeTab(t.id); });
      b.appendChild(x);
      b.addEventListener('click', () => { st.active = t.id; this.renderAll(); });
      return b;
    }));
    if (tab === CHG) {
      this.barEl.hidden = true;
      this.barEl.replaceChildren();
      const view = this.viewOf(st);
      if (this.bodyEl.firstChild !== view.root) this.bodyEl.replaceChildren(view.root);
      if (!this.root.hidden) view.update();
      return;
    }
    this.barEl.hidden = false;
    this.renderBar(st.s, tab);
    if (!tab.el) tab.el = this.buildView(st.s, tab);
    if (this.bodyEl.firstChild !== tab.el) this.bodyEl.replaceChildren(tab.el);
  }

  renderBar(s, tab) {
    const btn = (ic, label, fn, on) => {
      const b = h('button', { class: 'btn ghost small pv-act' + (on ? ' on' : ''), type: 'button', title: label }, icon(ic), h('span', null, label));
      b.addEventListener('click', fn);
      return b;
    };
    const acts = [];
    let where = '';
    if (tab.kind === 'file') {
      where = tab.path;
      acts.push(btn('refresh', '刷新', () => this.reload(tab, true)));
      if (tab.info && tab.info.url && tab.info.kind !== 'binary') acts.push(btn('external', '在浏览器中打开', () => window.open(tab.info.url, '_blank', 'noopener,noreferrer')));
      acts.push(btn('folder', '在 Finder 中显示', () => localOpen(s, tab.path, 'reveal')));
      // 「用默认应用打开」只对白名单类型（后端 stat 的 openable；可执行/脚本/安装包等只能在 Finder 中显示）
      if (tab.info && tab.info.openable) acts.push(btn('eye', '用默认应用打开', () => localOpen(s, tab.path, 'open')));
      acts.push(btn('copy', '复制路径', () => copyPath(tab.path)));
    } else if (tab.kind === 'url') {
      where = tab.url;
      acts.push(btn('refresh', '刷新', () => this.reload(tab, true)));
      acts.push(btn('external', '在浏览器中打开', () => window.open(tab.url, '_blank', 'noopener,noreferrer')));
      acts.push(btn('copy', '复制地址', () => copyPath(tab.url)));
      acts.push(btn('shield', tab.sameOrigin ? '兼容模式：开' : '兼容模式', () => {
        tab.sameOrigin = !tab.sameOrigin;
        tab.el = null;
        this.renderAll();
        toast(tab.sameOrigin ? '已对该 localhost 页面开启 allow-same-origin（不同端口 = 不同源，碰不到本程序）' : '已恢复严格沙箱');
      }, tab.sameOrigin));
    } else {
      where = tab.path;
      if (isLocal(s)) acts.push(btn('file', '打开文件', () => this.open(s, { kind: 'file', path: tab.path })));
      acts.push(btn('copy', '复制路径', () => copyPath(tab.path)));
    }
    const meta = tab.kind === 'file' && tab.size != null ? fmtSize(tab.size) : '';
    const short = where.length > 64 ? '…' + where.slice(-63) : where;
    const parts = [h('div', { class: 'pv-where mono', title: where }, short)];
    if (meta) parts.push(h('span', { class: 'pv-meta muted' }, meta));
    parts.push(h('div', { class: 'pv-acts' }, acts));
    this.barEl.replaceChildren(...parts);
  }

  buildView(s, tab) {
    const box = h('div', { class: 'pv-view kind-' + tab.kind });
    if (tab.kind === 'diff') { box.appendChild(diffView(tab)); return box; }
    if (tab.kind === 'url') {
      box.appendChild(h('iframe', { class: 'pv-frame', src: tab.url, title: tab.url, referrerpolicy: 'no-referrer',
        sandbox: tab.sameOrigin ? SANDBOX + ' allow-same-origin' : SANDBOX }));
      box.appendChild(h('div', { class: 'pv-note muted' }, '沙箱预览（不同端口 = 不同源）。页面空白时（如 Vite 的模块脚本）可试"兼容模式"或在浏览器中打开。'));
      return box;
    }
    box.appendChild(h('div', { class: 'pv-loading muted' }, '加载中…'));
    this.loadFile(s, tab, box);
    return box;
  }

  async loadFile(s, tab, box) {
    const [r] = await statMany(s, [tab.path]);
    if (!r || !r.ok || !r.url) {
      box.replaceChildren(h('div', { class: 'pv-empty' }, icon('file'), h('div', null, (r && r.reason) || '文件不存在'),
        h('div', { class: 'muted mono' }, tab.path)));
      return;
    }
    tab.info = r;
    tab.mtime = r.mtime;
    tab.size = r.size;
    tab.path = r.real || tab.path;
    if (this.current().tab === tab) this.renderBar(s, tab);
    await this.fillFile(s, tab, box);
  }

  async fillFile(s, tab, box) {
    const r = tab.info;
    const url = r.url + (tab.v ? '?ccr_v=' + tab.v : '');
    const k = r.kind;
    let node;
    if (k === 'image' || k === 'svg') {
      node = h('div', { class: 'pv-img' }, chatImage(s, null, { desc: { s, url, name: basename(tab.path), path: tab.path }, cls: 'pv-imgbtn', alt: basename(tab.path) }));
    } else if (k === 'html') {
      node = h('iframe', { class: 'pv-frame', src: url, title: tab.path, referrerpolicy: 'no-referrer', sandbox: SANDBOX });
    } else if (k === 'pdf') {
      // Chrome 的 PDF 查看器不能在沙箱 iframe 里运行；PDF 以 application/pdf + nosniff 返回，不会被当成 HTML
      node = h('iframe', { class: 'pv-frame pdf', src: url, title: tab.path });
    } else if (k === 'video') {
      node = h('video', { class: 'pv-media', src: url, controls: true, preload: 'metadata' });
    } else if (k === 'audio') {
      node = h('audio', { class: 'pv-media', src: url, controls: true, preload: 'metadata' });
    } else if (k === 'markdown' || k === 'text') {
      try {
        const res = await fetch(url, { credentials: CREDENTIALS, cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        let text = await res.text();
        let cut = false;
        if (text.length > MAX_TEXT) { text = text.slice(0, MAX_TEXT); cut = true; }
        node = k === 'markdown' ? markdownView(s, tab, text, cut) : textView(tab, text, cut);
      } catch (e) {
        node = h('div', { class: 'pv-empty' }, '读取失败：' + ((e && e.message) || e));
      }
    } else {
      const b = r.openable
        ? h('button', { class: 'btn small', type: 'button' }, icon('eye'), '用默认应用打开')
        : h('button', { class: 'btn small', type: 'button', title: '这种类型不会被直接打开' }, icon('folder'), '在 Finder 中显示');
      b.addEventListener('click', () => localOpen(s, tab.path, r.openable ? 'open' : 'reveal'));
      node = h('div', { class: 'pv-empty' }, icon('file'), h('div', null, '二进制文件，无法在这里预览'), h('div', { class: 'muted' }, fmtSize(r.size)), b);
    }
    const keep = box.querySelector('.pv-scroll');
    const top = keep ? keep.scrollTop : 0;
    box.replaceChildren(node);
    const sc = box.querySelector('.pv-scroll');
    if (sc && top) sc.scrollTop = top;
    else if (sc && tab.line && !tab.scrolled) {
      tab.scrolled = true;
      const row = sc.querySelector('[data-ln="' + tab.line + '"]');
      requestAnimationFrame(() => { sc.scrollTop = Math.max(0, (tab.line - 4) * lineHeight(sc)); if (row) row.classList.add('hl'); });
    }
  }

  /** 刷新：重新加载 iframe / 图片 / 文本（user=true 时也重新 stat） */
  reload(tab, user) {
    const { st } = this.current();
    if (!st) return;
    tab.v = Date.now();
    if (tab.kind === 'url') {
      const f = tab.el && tab.el.querySelector('iframe');
      if (f) f.src = tab.url;
      return;
    }
    if (tab.kind !== 'file') return;
    if (user || !tab.info) { tab.el = null; this.renderAll(); return; }
    if (tab.el) this.fillFile(st.s, tab, tab.el);
  }

  /** 每 1.5s：活动的 file 标签（及 HTML 同目录里本会话改过的文件）mtime/size 变了 → 自动刷新 */
  async poll() {
    if (this.polling || this.root.hidden || document.visibilityState !== 'visible') return;
    const { st, tab } = this.current();
    if (!st || !tab || tab.kind !== 'file' || !tab.info || !isLocal(st.s)) return;
    this.polling = true;
    try {
      const paths = [tab.path, ...(tab.info.kind === 'html' ? relatedFiles(st.s, tab.path) : [])];
      const res = await statMany(st.s, paths);
      const sig = res.map((r) => (r ? [r.exists, r.mtime, r.size].join('|') : '-')).join(';');
      if (tab.sig != null && sig !== tab.sig && this.current().tab === tab) {
        const main = res[0];
        if (main && main.ok) { tab.mtime = main.mtime; tab.size = main.size; tab.info = { ...tab.info, ...main }; }
        this.reload(tab, false);
        this.renderBar(st.s, tab);
      }
      tab.sig = sig;
    } finally { this.polling = false; }
  }
}

function readFlag(key) { try { return localStorage.getItem(key) === '1'; } catch (_) { return false; } }
function matchNarrow() { try { return !!(window.matchMedia && window.matchMedia(NARROW).matches); } catch (_) { return false; } }

function clampW(w) {
  const max = Math.max(360, window.innerWidth - 380);
  return Math.min(max, Math.max(320, w || 560));
}

function lineHeight(sc) {
  const row = sc.querySelector('.pv-lines');
  const lh = row ? parseFloat(getComputedStyle(row).lineHeight) : 0;
  return lh > 0 ? lh : 19;
}

function kindIcon(t) {
  const k = t.info && t.info.kind;
  return k === 'image' || k === 'svg' ? 'image' : k === 'html' ? 'globe' : 'file';
}

function tabLabel(t) {
  if (t.kind === 'url') { try { const u = new URL(t.url); return u.host + (u.pathname !== '/' ? u.pathname : ''); } catch (_) { return t.url; } }
  return basename(t.path);
}

function tabTitle(t) { return t.kind === 'url' ? t.url : (t.kind === 'diff' ? '本轮改动 diff：' : '') + t.path; }

function copyPath(p) { copyText(p).then((ok) => toast(ok ? '已复制：' + p : '复制失败')); }

/** 本会话改过的、与 HTML 同目录（及子目录）的文件（盯 mtime 用：改了 css/js 也刷新页面） */
function relatedFiles(s, htmlPath) {
  const dir = htmlPath.slice(0, htmlPath.lastIndexOf('/') + 1);
  const out = new Set();
  const cwd = s.absCwd || (s.meta && s.meta.cwd) || '';
  for (const t of s.tools ? s.tools.values() : []) {
    if (!['Write', 'Edit', 'MultiEdit'].includes(t.name) || !t.input) continue;
    const p = resolvePath(String(t.input.file_path || ''), cwd);
    if (p && p !== htmlPath && p.startsWith(dir)) out.add(p);
    if (out.size >= 30) break;
  }
  return [...out];
}

function textView(tab, text, cut) {
  let lines = text.split('\n');
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  let cutLines = false;
  if (lines.length > MAX_LINES) { lines = lines.slice(0, MAX_LINES); cutLines = true; }
  const nums = h('pre', { class: 'pv-gutter', 'aria-hidden': 'true' }, lines.map((_, k) => k + 1).join('\n'));
  const code = h('code', null, lines.join('\n'));
  const pre = h('pre', { class: 'pv-lines' }, code);
  const ext = (tab.path.match(/\.([A-Za-z0-9]+)$/) || [])[1] || '';
  if (text.length < 300000) highlightInto(code, lines.join('\n'), ext);
  const note = cut || cutLines ? h('div', { class: 'pv-note muted' }, `文件较大，只显示前 ${cutLines ? MAX_LINES + ' 行' : '2MB'}`) : null;
  if (tab.line) {
    const mark = h('div', { class: 'pv-linemark', dataset: { ln: String(tab.line) } });
    mark.style.top = 'calc(' + (tab.line - 1) + ' * var(--pv-lh) + 8px)';
    return h('div', { class: 'pv-scroll pv-text' }, note, h('div', { class: 'pv-code' }, mark, nums, pre));
  }
  return h('div', { class: 'pv-scroll pv-text' }, note, h('div', { class: 'pv-code' }, nums, pre));
}

function markdownView(s, tab, text, cut) {
  // markdown 里的相对链接/图片按该文件所在目录解析
  const dir = tab.path.slice(0, tab.path.lastIndexOf('/')) || '/';
  const ps = { sid: s.sid, meta: s.meta, absCwd: dir, parent: null, localOverride: s.localOverride, readOnly: true };
  const md = h('div', { class: 'md pv-md' }, renderMarkdown(text, markdownCtx(ps)));
  return h('div', { class: 'pv-scroll' }, cut ? h('div', { class: 'pv-note muted' }, '文件较大，只显示前 2MB') : null, md);
}

function diffView(tab) {
  const c = tab.change;
  if (!c || !Array.isArray(c.edits)) return h('div', { class: 'pv-empty' }, '没有改动数据');
  const secs = c.edits.map((e, k) => {
    const head = h('div', { class: 'pv-diff-head' }, h('span', { class: 'pv-diff-n' }, c.edits.length > 1 ? `第 ${k + 1} 次 · ${e.label}` : e.label),
      h('span', { class: 'tc-add' }, '+' + e.add), h('span', { class: 'tc-del' }, '−' + e.del),
      e.hunks ? null : h('span', { class: 'muted small-text' }, e.content != null ? '（新建文件：全部内容）' : '（没有 structuredPatch：按 old/new 字符串比较，无行号）'),
      e.truncated ? h('span', { class: 'muted small-text' }, '（transcript 中内容过长已截断）') : null);
    let body;
    if (e.hunks) body = renderPatch(e.hunks, { cls: 'full' });
    else if (e.content != null) {
      const lines = String(e.content).replace(/\n$/, '').split('\n');
      body = renderPatch([{ oldStart: 0, oldLines: 0, newStart: 1, newLines: lines.length, lines: lines.map((l) => '+' + l) }], { cls: 'full' });
    } else if (e.old != null || e.new != null) body = renderDiff(e.old || '', e.new || '');
    else body = h('div', { class: 'muted' }, '（没有可显示的内容）');
    return h('div', { class: 'pv-diff-sec' }, head, body);
  });
  return h('div', { class: 'pv-scroll pv-diff' },
    h('div', { class: 'pv-diff-title' }, icon('diff'), h('span', { class: 'mono' }, c.path),
      h('span', { class: 'tc-add' }, '+' + c.add), h('span', { class: 'tc-del' }, '−' + c.del),
      h('span', { class: 'muted small-text' }, '数据来自 transcript（structuredPatch），不读取文件')),
    secs);
}

export { displayPath };
