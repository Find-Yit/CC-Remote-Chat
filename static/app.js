// 应用入口（v2：交互式 Claude Code）：网络 ↔ 渲染核心 ↔ 侧栏/头部/输入框/终端面板
import { Api, EventStream, takeLoginCode } from './api.js';
import { h, icon, spinner, fmtRelTime, fmtSize, basename, truncate } from './dom.js';
import {
  createState, ensureSession, applyEnvelope, applyToSession, releaseHold, applyHistory, applyScreen, mountSession,
  addLocalNotice, scrollToBottom, refreshTail, currentDialog, setModelList, revealItem,
} from './render.js';
import { toast, confirmDialog, promptDialog, jobsPopover, newSessionDialog, JOBID_RE, isLocalMeta, targetQuery } from './dialogs.js';
import { openModelModal, openEffortModal, TermPanel } from './tui.js';
import { SubagentUI } from './subagents.js';
import { StatsBar } from './stats.js';
import { apiLabel, apiUsageTitle, restartApiDialog, loadApiKeys, loadUsage, onApiKeysChange } from './apikeys.js';
import { initViz } from './viz.js';           // 可视化（§10）：图片引用 / 灯箱 / 文件 chip / 右侧预览面板
import { PreviewPanel } from './preview.js';
import { HintBar, installChords, screenHints } from './hotkeys.js';
import { settingsDialog, setUpdateStore } from './settings.js';
import { modeName, cycleOf } from './modes.js';
import { ComposerControls } from './composerbar.js';   // 输入框底栏：模型/effort 胶囊、权限模式、Shift+Tab
import { AutoSize } from './autosize.js';  // 输入框自适应高度（镜像测量，不临时缩小输入框，打字不抖）
import { PromptNav, promptsFromItems, mergePending, addPending, historyDir, recallInto } from './prompthist.js';   // 输入框 ↑/↓ 历史
import { sessionStatus, rowClass, isUnread, docTitle, unreadCount } from './sessrow.js';   // 侧栏行状态（新回复 / 待处理）
import { PromptMinimap } from './minimap.js';   // 时间线左侧 prompt 小格子（只读覆盖层）
import { ChangesButton } from './changes.js';   // 右侧「代码改动」栏的会话头开关（数据只来自前端已有的 transcript）
import { UpdateStore, UpdateBanner, updateAction } from './update.js';   // dmg 版自动更新（只有后端开启时才显示）

const $ = (id) => document.getElementById(id);
const api = new Api({ onAuthLost: (msg) => showAuthLost(msg) });

let config = {};
// 本机 tmux 的 -L 名：平时 ccr；内置模式（dmg 版，后端 --bundled）由后端在配置里给出 tmux_label（ccr-app）
const tmuxLabel = () => (config && config.tmux_label) || 'ccr';
let stream = null;
let instanceId = '';        // 服务器实例 id（bootstrap 返回），SSE 带 instance=
let epoch = 0;              // 每次（重新）加载递增；旧加载流程的异步回调据此作废
let active = null;          // {type:'session', sid} | {type:'preview', uuid, session, item}
const drafts = new Map();   // sid -> {text, nav}（nav = 输入框 ↑/↓ 历史的浏览状态，prompthist.js）
const sentPending = new Map();   // sid -> [{text, n}]：本页发出、transcript 里还没出现的消息（↑/↓ 历史用）
let historyItems = null;     // 当前历史面板（historyTarget）的列表；每项带 target
let historyLoading = false;
// 历史面板的运行位置：'server' | 'local'（记住上次选择）；两边的列表各自缓存
let historyTarget = localStorage.getItem('ccr.histTarget') === 'local' ? 'local' : 'server';
const historyCache = { server: null, local: null };
let historyReq = 0;
let modelCtrl = null;       // {sid, ctrl} 当前打开的模型弹窗
let termOpen = localStorage.getItem('ccr.term') === '1';
let term = null;
let saUI = null;            // 子代理托盘 + 抽屉（subagents.js）
let pvPanel = null;         // 右侧预览面板（preview.js）
let hintBar = null;         // CLI 快捷键提示条（hotkeys.js）
const statsBar = new StatsBar();   // 统计（上下文圆环 / 花费 / tok/s）：只建一次，boot 时挂进输入框底栏的固定槽位，之后原地更新
let composerCtl = null;            // 输入框底栏左侧控件组（composerbar.js）
let minimap = null;                // 时间线左侧 prompt 小格子（minimap.js）：#chat-host 里只挂一个，跟随当前会话 / 历史预览
// 会话头「改动」按钮：只建一次，每次重建会话头时挂回去，角标 / 按下状态原地更新
const chgBtn = new ChangesButton({ onClick: () => { const s = activeSession(); if (s && pvPanel) pvPanel.toggleChanges(s); } });
// 条目写进 DOM 之后：左侧小格子 + 右侧「改动」栏（增量）
const minimapFlush = (s) => {
  if (minimap) minimap.onFlush(s);
  if (pvPanel) pvPanel.onSessionUpdate(s);
};

const sp = (s, tail) => api.sessionPath(s.sid, tail);

const actions = {
  // 对话框卡片（只在用户点击时调用）
  // expect = 卡片渲染时的对话框指纹；远端对话框已变（终端里答掉/取消、换成别的、另一个窗口已操作）→ 409 dialog_changed
  choose: (s, index, expect) => guardDialog(s, () => api.post(sp(s, '/choose'), { index, expect })),
  cancelDialog: (s) => api.post(sp(s, '/cancel_dialog')),
  keys: (s, keys) => api.post(sp(s, '/keys'), { keys }),
  // AskUserQuestion 的 “Type something.”：后端在同一次操作里选中该项、粘贴文字并回车（choose 带 text）
  freeText: (s, index, text, expect) => guardDialog(s, () => api.post(sp(s, '/choose'), { index, text, expect })),
  restart: (s) => doRestart(s),
  openModelDialog: (s) => openModelFor(s, false),
  applyModel: (s, body) => api.post(sp(s, '/model'), body),
  applyEffort: (s, body) => withClearRetry((extra) => api.post(sp(s, '/effort'), { ...body, ...extra })),
  openSubagent: (s, aid) => { if (saUI) saUI.open(s, aid); },
  onApplied: (s, r, scope) => toast((r && typeof r.result === 'string' && r.result ? r.result.replace(/`/g, '') + ' · ' : '') + (scope === 'save' ? '已保存为默认' : '仅本会话')),
};

/**
 * 终端输入框里已有未发送的文字（例如用户在终端面板里打过字）时，后端返回 409 input_not_empty；
 * 经用户确认后带 clear_input:true 重试（清空 = 在终端里按 Ctrl+U，和手动操作一样）。
 */
async function withClearRetry(fn) {
  try {
    return await fn({});
  } catch (e) {
    if (!(e && e.status === 409 && e.code === 'input_not_empty')) throw e;
    const ok = await confirmDialog('终端输入框不是空的', e.message + '\n\n清空后继续吗？（会在终端里按 Ctrl+U 清掉这些文字）', { okText: '清空并继续' });
    if (!ok) throw new Error('已取消');
    return fn({ clear_input: true });
  }
}

/** 对话框卡片操作：后端 409 dialog_changed（远端对话框已不是卡片上这个）→ 提示并立即刷新屏幕/卡片 */
async function guardDialog(s, fn) {
  try {
    return await fn();
  } catch (e) {
    if (e && e.status === 409 && e.code === 'dialog_changed') {
      toast('对话框已变化，请重新确认', 'warn');
      refreshScreen(s).catch(() => {});
      throw new Error('对话框已变化，请重新确认');
    }
    throw e;
  }
}

/**
 * 关闭/重启/删除：与计算节点（本机会话 = 本机 agent）连不上而作业仍在（或状态未知）时后端返回 409 link_down；
 * 经用户确认后带 force 重试（只在本地标记，远端 / 本机 tmux 里的 claude 可能残留）。
 */
async function withForceRetry(fn, title, what, local = false) {
  try {
    return await fn(false);
  } catch (e) {
    if (!(e && e.status === 409 && e.code === 'link_down')) throw e;
    const left = local ? `本机 tmux（-L ${tmuxLabel()}）里的 claude 进程可能残留，需要时可用 tmux -L ${tmuxLabel()} kill-session 自行结束`
      : '远端的 claude 进程可能残留，需要时可在终端里自行结束';
    const ok = await confirmDialog(title, e.message + '\n\n仍要' + what + '吗？（只在本地处理，' + left + '）',
      { okText: '强制' + what, danger: true });
    if (!ok) throw new Error('已取消');
    return fn(true);
  }
}

const state = createState({
  actions,
  // 窗口标题（未读数）在 meta 到达时直接更新：窗口在后台时 rAF 不执行，原生壳的程序坞角标要靠它
  onMeta: () => { updateDocTitle(); scheduleUI(); },
  onSession: () => scheduleUI(),
  onScreen: (s) => onScreen(s),
  onModelList: (s) => { if (modelCtrl && modelCtrl.sid === s.sid && s.modelList) modelCtrl.ctrl.setFull(s.modelList); },
  onModelInfo: () => scheduleUI(),
  onSubagent: (s, aid, added, reset) => { if (saUI) saUI.onUpdate(s, aid, added, reset); if (pvPanel) pvPanel.onSessionUpdate(s); scheduleUI(); },
  onFlush: minimapFlush,
});

// ====================== 启动 / 登录 / 重置 ======================

const AUTH_LOST_TEXT = '会话已失效，请在终端运行 python3 app.py（或打开 CC Remote Chat.app）重新打开窗口';

async function boot() {
  setStaticIcons();
  bindSidebar();
  bindComposer();
  // 输入框底栏：左侧控件组 + 右侧统计（统计元素只在这里挂一次，以后不再移动）
  composerCtl = new ComposerControls($('composer-ctl'));
  minimap = new PromptMinimap($('chat-host'), { reveal: revealItem });
  $('composer-stats').appendChild(statsBar.el);
  saUI = new SubagentUI({ api, toast });
  pvPanel = new PreviewPanel({ host: $('app'), onState: () => updateChgBtn() });
  initViz({ api, getConfig: () => config, toast, panel: pvPanel });
  // CLI 快捷键提示：「转到后台运行」(Ctrl+B Ctrl+B)、「立即发送排队消息」(Ctrl+X Ctrl+S)；按钮与键盘和弦共用同一组动作
  hintBar = new HintBar($('hint-bar'), { onBackground: () => hintAction('background'), onSendNow: () => hintAction('send_now') });
  installChords({ getHints: chordHints, onBackground: () => hintAction('background'), onSendNow: () => hintAction('send_now') });
  term = new TermPanel($('term-panel'), {
    onKeys: (keys) => { const s = liveSession(); return s ? api.post(sp(s, '/keys'), { keys }) : Promise.resolve(); },
    onRefresh: () => refreshScreen(liveSession()),
    onClose: () => setTerm(false),
  });
  window.addEventListener('hashchange', async () => {
    const code = takeLoginCode();
    if (!code) return;
    try { await api.login(code); location.reload(); }
    catch (e) { if (e.status !== 401) toast('登录失败：' + e.message, 'error'); else showAuthLost(e.message); }
  });
  const code = takeLoginCode(); // 先清掉地址栏中的登录码，再去兑换
  if (code) {
    try {
      await api.login(code);
    } catch (e) {
      if (!api.token) {
        if (e.status === 401 || e.status === 403) showAuthLost(e.message);
        else setConn('auth', '无法连接本地服务：' + e.message);
        return;
      }
    }
  }
  if (!api.token) { showAuthLost(''); return; }
  // API key 列表/用量（会话头 chip、会话列表标签用）：变化时重画
  onApiKeysChange(() => { headerKey = ''; listKey = ''; scheduleUI(); if (historyItems) renderHistoryList(); });
  loadApiKeys(api).then(() => loadUsage(api)).catch(() => {});
  await loadAll();
}

// ---------- dmg 版自动更新（static/update.js；后端没开启时 bootstrap 不带 update，这里什么都不做） ----------
const updates = new UpdateStore();
let updateBanner = null;
function setUpdate(u) {
  updates.set(u);
  if (!updateBanner && updates.enabled) {
    setUpdateStore(updates);   // 设置弹窗里多一节「更新」
    updateBanner = new UpdateBanner(updates, {
      mount: (el) => { const hd = $('chat-header'); hd.parentNode.insertBefore(el, hd); },
      act: async (id) => {
        try { await updateAction(api, updates, id); } catch (e) { toast('操作失败：' + e.message, 'error'); }
      },
    });
  }
}

/** 拉取 bootstrap，建立 SSE，回放各会话事件。reset 时整体重新调用。 */
async function loadAll() {
  const my = ++epoch;
  let b;
  try {
    b = await api.bootstrap();
  } catch (e) {
    if (my !== epoch || api.authLost) return;
    setConn('error', '无法连接本地服务：' + e.message + '（稍后自动重试）');
    setTimeout(() => { if (my === epoch && !api.authLost) loadAll(); }, 3000);
    return;
  }
  if (my !== epoch || api.authLost) return;
  instanceId = typeof b.instance_id === 'string' ? b.instance_id : '';
  config = b.config || {};
  applyConfigUI();
  if (b.update) setUpdate(b.update);
  state.lastGseq = typeof b.gseq === 'number' ? b.gseq : 0;

  for (const meta of Array.isArray(b.sessions) ? b.sessions : []) {
    if (!meta || !meta.sid) continue;
    const s = ensureSession(state, meta.sid, meta);
    s.hold = true; // 回放完成前暂存 SSE 事件
  }
  updateDocTitle();

  stream = new EventStream({
    api,
    getSince: () => state.lastGseq,
    getInstance: () => instanceId,
    onEnvelope: (env) => applyEnvelope(state, env),
    onStatus: (st) => setConn(st),
    beforeReconnect,
    onReset: () => resetAll('服务已重启或部分实时事件无法回放'),
    onConfig: (c) => { config = c; applyConfigUI(); },
    onUpdate: (u) => setUpdate(u),
  });
  stream.start();

  const last = localStorage.getItem('ccr.active');
  const keep = active && active.type === 'session' && state.sessions.has(active.sid) ? active.sid : null;
  const first = keep || (last && state.sessions.has(last) ? last : sortedSessions()[0]?.sid);
  active = null;
  if (first) activate(first);
  else showEmpty();

  await runLimited([...state.sessions.values()].filter((s) => s.hold), 6, (s) => loadSessionEvents(s, my));
  if (my === epoch) scheduleUI();
}

async function loadSessionEvents(s, my) {
  try {
    const r = await api.get(sp(s, '/events'));
    if (my !== epoch) return;
    if (r.truncated === true) addLocalNotice(s, 'warn', '更早的实时事件已截断');
    for (const env of Array.isArray(r.events) ? r.events : []) {
      if (typeof env.gseq === 'number' && env.gseq > state.lastGseq) state.lastGseq = env.gseq;
      applyToSession(s, env);
    }
  } catch (e) {
    if (my !== epoch) return;
    addLocalNotice(s, 'error', '事件回放失败：' + e.message);
  }
  releaseHold(s);
  if (isActive(s)) { scrollToBottom(s); onScreen(s); }
}

async function runLimited(items, limit, fn) {
  const queue = items.slice();
  const workers = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    while (queue.length) await fn(queue.shift());
  });
  await Promise.all(workers);
}

async function beforeReconnect() {
  if (api.authLost) return 'stop';
  try {
    const b = await api.get('/api/bootstrap');
    const inst = typeof b.instance_id === 'string' ? b.instance_id : '';
    if ((inst && instanceId && inst !== instanceId) || (typeof b.gseq === 'number' && b.gseq < state.lastGseq)) {
      resetAll('服务已重启');
      return 'stop';
    }
    if (b.config) { config = b.config; applyConfigUI(); }
    if (b.update) setUpdate(b.update);
    const alive = new Set((Array.isArray(b.sessions) ? b.sessions : []).map((m) => m && m.sid));
    for (const s of sortedSessions()) if (!alive.has(s.sid)) dropSession(s.sid);
    for (const m of Array.isArray(b.sessions) ? b.sessions : []) {
      if (!m || !m.sid) continue;
      const s = state.sessions.get(m.sid);
      if (s && !s.hold) s.meta = { ...s.meta, ...m };
    }
    updateDocTitle();
    scheduleUI();
    return 'ok';
  } catch (e) {
    if (e.status === 401 || api.authLost) return 'stop';
    return 'retry';
  }
}

const resetTimes = [];
/** 收到 reset（或检测到服务重启）→ 丢弃本地状态，重新 bootstrap + 各会话 /events */
function resetAll(reason) {
  if (stream) { stream.stop(); stream = null; }
  if (api.authLost) return;
  const now = Date.now();
  while (resetTimes.length && now - resetTimes[0] > 60000) resetTimes.shift();
  resetTimes.push(now);
  if (resetTimes.length > 5) {
    setConn('error', '服务反复要求重置，已停止自动重连；请刷新页面');
    return;
  }
  saveDraft();
  epoch++;
  closeModelModal();
  for (const s of state.sessions.values()) {
    if (s.view) { s.view.root.remove(); s.view = null; }
    clearTimeout(s.draft.timer);
  }
  state.sessions.clear();
  state.lastGseq = 0;
  instanceId = '';
  if (active && active.type === 'preview' && active.session && active.session.view) active.session.view.root.remove();
  hideAllViews();
  setConn('connecting', reason + '，正在重新加载…');
  toast(reason + '，已重新加载会话', 'warn');
  loadAll();
}

let authShown = false;
/** 没有 token / 任何请求 401 → 全屏提示，停止重连 */
function showAuthLost(detail) {
  if (detail && /python3 app\.py|会话已失效/.test(detail)) detail = '';
  epoch++;
  if (stream) { stream.stop(); stream = null; }
  setConn('auth', '会话已失效');
  if (authShown) {
    const card = document.querySelector('.auth-lost .auth-card');
    if (card && detail) {
      const old = card.querySelector('.auth-detail');
      const d = h('div', { class: 'auth-detail muted' }, detail);
      if (old) old.replaceWith(d); else card.appendChild(d);
    }
    return;
  }
  authShown = true;
  const cmd = h('code', null, 'python3 app.py');
  const box = h('div', { class: 'auth-lost', role: 'alertdialog', 'aria-modal': 'true', 'aria-labelledby': 'auth-lost-title' },
    h('div', { class: 'auth-card' },
      icon('shield', 'auth-ic'),
      h('div', { id: 'auth-lost-title', class: 'auth-title' }, '会话已失效'),
      h('div', { class: 'auth-text' }, '请在终端运行 ', cmd, '（或打开 CC Remote Chat.app）重新打开窗口'),
      detail ? h('div', { class: 'auth-detail muted' }, detail) : null));
  box.dataset.text = AUTH_LOST_TEXT;
  document.body.appendChild(box);
}

function setConn(st, text) {
  const el = $('conn-status');
  el.dataset.state = st;
  const map = { connecting: '连接中…', open: '已连接', error: '连接断开，正在重连…', auth: '会话已失效' };
  el.querySelector('.conn-text').textContent = text || map[st] || st;
}

// ====================== 会话辅助 ======================

/** 侧栏顺序：手动排过的按 meta.order（小的在上）；没排过的（新会话）按创建时间、新的在最上，并排在所有排过的前面 */
function orderKey(s) {
  const o = s.meta.order;
  return typeof o === 'number' && Number.isFinite(o) ? o : -(Number(s.meta.created_at) || 0);
}
function sortedSessions() {
  return [...state.sessions.values()].filter((s) => !s.readOnly)
    .sort((a, b) => orderKey(a) - orderKey(b) || (Number(b.meta.created_at) || 0) - (Number(a.meta.created_at) || 0)
      || String(a.sid).localeCompare(String(b.sid)));
}

/** 手动排序：ids = 从上到下的全部会话 id；先在本地生效，再告诉后端（存进 sessions.json） */
async function applyOrder(ids) {
  ids.forEach((id, i) => { const x = state.sessions.get(id); if (x) x.meta = { ...x.meta, order: i }; });
  renderSessionList();
  try { await api.post('/api/sessions/order', { sids: ids }); } catch (e) { toast('保存排序失败：' + e.message, 'error'); }
}
function moveSession(sid, targetSid, after) {
  if (!sid || sid === targetSid) return;
  const ids = sortedSessions().map((x) => x.sid).filter((x) => x !== sid);
  let i = ids.indexOf(targetSid);
  if (i < 0) return;
  if (after) i += 1;
  ids.splice(i, 0, sid);
  applyOrder(ids);
}
function moveSessionTo(sid, where) {
  const ids = sortedSessions().map((x) => x.sid).filter((x) => x !== sid);
  if (where === 'top') ids.unshift(sid); else ids.push(sid);
  applyOrder(ids);
}
function moveSessionBy(sid, delta) {
  const ids = sortedSessions().map((x) => x.sid);
  const i = ids.indexOf(sid);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return;
  ids.splice(i, 1);
  ids.splice(j, 0, sid);
  applyOrder(ids).then(() => { const el = $('session-list').querySelector('[data-sid="' + sid + '"]'); if (el) el.focus(); });
}
function sessionMenu(s, x, y) {
  document.querySelectorAll('.popover.sess-menu').forEach((p) => p.remove());
  const item = (label, ic, fn) => {
    const b = h('button', { class: 'menu-item', type: 'button', role: 'menuitem' }, icon(ic), label);
    b.addEventListener('click', () => { dismiss(); fn(); });
    return b;
  };
  // 新回复：未读 →「标为已读」；有过完成记录、当前已读 →「标为未读」
  const unread = isUnread(s.meta, s.localSeen);
  const seenItem = unread ? item('标为已读', 'check', () => markSeen(s))
    : typeof s.meta.done_at === 'number' ? item('标为未读', 'bell', () => markUnseen(s)) : null;
  const pop = h('div', { class: 'popover sess-menu', role: 'menu', 'aria-label': '会话操作' },
    seenItem,
    item('移到最上', 'up', () => moveSessionTo(s.sid, 'top')),
    item('移到最下', 'down', () => moveSessionTo(s.sid, 'bottom')),
    item('删除会话…', 'trash', () => doDelete(s)));
  document.body.appendChild(pop);
  pop.style.left = Math.max(8, Math.min(x, window.innerWidth - 180)) + 'px';
  pop.style.top = Math.max(8, Math.min(y, window.innerHeight - (seenItem ? 176 : 140))) + 'px';
  const dismiss = (e) => {
    if (e && e.type === 'mousedown' && pop.contains(e.target)) return;
    if (e && e.type === 'keydown' && e.key !== 'Escape') return;
    pop.remove();
    document.removeEventListener('mousedown', dismiss, true);
    document.removeEventListener('keydown', dismiss, true);
  };
  setTimeout(() => { document.addEventListener('mousedown', dismiss, true); document.addEventListener('keydown', dismiss, true); }, 0);
}

function isActive(s) { return !!active && active.type === 'session' && active.sid === s.sid; }
function activeSession() {
  if (!active) return null;
  if (active.type === 'session') return state.sessions.get(active.sid) || null;
  return active.session || null;
}
function liveSession() { const s = activeSession(); return s && active.type === 'session' ? s : null; }

function isDead(m) { return m.state === 'exited' || m.state === 'error'; }
function isBusy(s) {
  const st = s.screen && !s.screen.stale ? s.screen.state : null;
  return st ? !!st.busy : !!(s.meta.busy || s.meta.state === 'busy');
}

/** 作业已结束（squeue 查不到）：link 不再重连，只能在新作业上 --resume 续接（本机会话没有作业，永远不会结束） */
function jobEnded(m) { return m.link === 'ended' && !isLocalMeta(m); }

/** 中文提示里的运行位置：本机 / 计算节点 */
function placeOf(m) { return isLocalMeta(m) ? '本机' : '计算节点'; }

/** 状态（优先级见 sessrow.js）：死状态 > 待处理（对话框）> 运行中 > 新回复（完成但未看）> 空闲 */
function statusOf(s) {
  const m = s.meta;
  return sessionStatus({ meta: m, dialog: currentDialog(s), busy: isBusy(s), subRunning: runningSubagents(s),
    jobEnded: jobEnded(m), localSeen: s.localSeen });
}

// ====================== 新回复（完成但未看） ======================

/** 窗口标题 "(n) CC Remote Chat"：n = 侧栏显示「新回复」的会话数（原生壳解析前缀设置程序坞角标） */
function updateDocTitle() {
  const t = docTitle(unreadCount(sortedSessions().map(statusOf)));
  if (document.title !== t) document.title = t;
}

/** 用户点了这一行 / 当前会话的时间线或输入框：标为已读。乐观更新，以服务器回推的 meta 为准；失败回滚 */
async function markSeen(s) {
  if (!s || s.readOnly || !isUnread(s.meta, s.localSeen)) return;
  const done = s.meta.done_at;
  s.localSeen = done;
  updateDocTitle();
  scheduleUI();
  try {
    await api.post(sp(s, '/seen'), { done_at: done });
  } catch (e) {
    if (s.localSeen === done) { s.localSeen = null; updateDocTitle(); scheduleUI(); }
    toast('标为已读失败：' + e.message, 'error');
  }
}

/** 右键「标为未读」：已看水位退到 done_at 之前（后端 /unseen）；没完成过的会话没有这一项 */
async function markUnseen(s) {
  s.localSeen = null;
  try {
    await api.post(sp(s, '/unseen'));
  } catch (e) {
    toast('标为未读失败：' + e.message, 'error');
  }
}

/** 正在运行的子代理数（本地摘要优先，其次会话 meta.subagents） */
function runningSubagents(s) {
  if (isDead(s.meta)) return 0;
  let n = 0;
  for (const a of s.subagents.values()) if (a.summary && a.summary.status === 'running') n++;
  if (!n && !s.subagents.size && s.meta.subagents && typeof s.meta.subagents.running === 'number') n = s.meta.subagents.running;
  return n;
}

function sessionTitle(s) {
  return s.meta.display_title || s.meta.title || (s.meta.cwd ? basename(s.meta.cwd) : '') || '未命名会话';
}

function currentMode(s) {
  const st = s.screen && !s.screen.stale ? s.screen.state : null;
  return (st && st.mode) || s.meta.mode || (s.meta.launch && s.meta.launch.permission_mode) || '';
}

function modelLabel(s) {
  const l = s.meta.launch || {};
  return s.modelInfo.model || s.meta.model || l.model || '默认模型';
}

function effortLabel(s) {
  const l = s.meta.launch || {};
  return s.modelInfo.effort || s.meta.effort || l.effort || '默认';
}

/** 当前会话可用的快捷键提示（和弦用）：没有活动会话、会话已退出、有对话框或有弹窗打开时 → null */
function chordHints() {
  const s = liveSession();
  if (!s || isDead(s.meta) || currentDialog(s) || document.querySelector('dialog[open]')) return null;
  return screenHints(s);
}

const HINT_ACTIONS = {
  background: { path: '/background', ok: '已发送 Ctrl+B：命令转到后台运行', fail: '转到后台运行失败' },
  send_now: { path: '/send_now', ok: '已发送 Ctrl+X Ctrl+S：立即发送排队的消息', fail: '立即发送失败' },
};

/** 只在提示出现时由用户点击/和弦触发；后端再核对最新屏幕并带 guard 发键（提示已消失 → 409，什么都不按） */
async function hintAction(kind) {
  const s = liveSession();
  const a = HINT_ACTIONS[kind];
  if (!s || !a) return;
  try {
    await api.post(sp(s, a.path));
    toast(a.ok);
  } catch (e) {
    toast(a.fail + '：' + e.message, e && e.status === 409 ? 'warn' : 'error');
    refreshScreen(s).catch(() => {});
  }
}

// ====================== 视图切换 ======================

function hideAllViews() {
  for (const el of $('chat-host').querySelectorAll('.chat-scroll')) el.hidden = true;
}

function activate(sid) {
  const s = state.sessions.get(sid);
  if (!s) return;
  saveDraft();
  if (active && active.type === 'preview' && active.session && active.session.view) active.session.view.root.remove();
  if (modelCtrl && modelCtrl.sid !== sid) closeModelModal();
  active = { type: 'session', sid };
  localStorage.setItem('ccr.active', sid);
  hideAllViews();
  $('empty-state').hidden = true;
  const isNew = !s.view;
  mountSession(s, $('chat-host'));
  s.view.root.hidden = false;
  if (isNew) scrollToBottom(s);
  loadDraft();
  closeSidebarOnNarrow();
  renderAll();
  if (termOpen) { term.setSession(s); refreshScreen(s); }
  if (!$('input').disabled) $('input').focus();
}

function showEmpty() {
  active = null;
  hideAllViews();
  $('empty-state').hidden = false;
  renderAll();
}

async function refreshScreen(s) {
  if (!s || s.readOnly) return;
  const r = await api.get(sp(s, '/screen'));
  if (state.sessions.get(s.sid) !== s) return;
  if (r && (r.state || typeof r.text === 'string')) applyScreen(s, r.state || {}, r.text || '');
}

// ====================== 屏幕帧 → 头部/输入框/终端/模型弹窗 ======================

function onScreen(s) {
  if (s.hold) return;
  const d = currentDialog(s);
  if (!d || d.kind !== 'model') s.modelSuppress = false;
  if (!isActive(s)) { scheduleUI(); return; }
  // 模型弹窗：屏幕出现 model 对话框（例如用户自己输入 /model）→ 用同一弹窗呈现；面板消失 → 弹窗随之关闭
  if (modelCtrl && modelCtrl.sid === s.sid) {
    if ((!d || d.kind !== 'model') && !modelCtrl.ctrl.busy) closeModelModal();
    else modelCtrl.ctrl.update();
  } else if (d && d.kind === 'model' && !s.modelSuppress && !modelCtrl) {
    openModelFor(s, false);
  }
  if (termOpen) term.update(s);
  scheduleUI();
}

function openModelFor(s, viaButton) {
  if (modelCtrl) {
    if (modelCtrl.sid === s.sid && modelCtrl.ctrl.isOpen()) return;
    closeModelModal();
  }
  let pending = null;
  if (viaButton) {
    pending = withClearRetry((extra) => api.post(sp(s, '/model_picker'), extra)).then((r) => { if (r && Array.isArray(r.rows)) setModelList(s, r); return r; });
  }
  s.modelSuppress = false;
  const ctrl = openModelModal({
    s, actions, pending,
    onClosed: () => {
      if (modelCtrl && modelCtrl.ctrl === ctrl) modelCtrl = null;
      // 面板仍在屏幕上时，关闭后不再自动弹出（直到面板消失）
      const d = currentDialog(s);
      s.modelSuppress = !!(d && d.kind === 'model');
    },
  });
  modelCtrl = { sid: s.sid, ctrl };
}

function closeModelModal() {
  if (!modelCtrl) return;
  const c = modelCtrl.ctrl;
  modelCtrl = null;
  c.close(true);
}

// ====================== UI 渲染（rAF 合批） ======================

let uiRaf = 0;
function scheduleUI() {
  if (!uiRaf) uiRaf = requestAnimationFrame(() => { uiRaf = 0; renderAll(); });
}

function renderAll() {
  updateDocTitle();
  renderSessionList();
  renderHeader();
  renderComposerControls();
  renderStats();
  renderComposerState();
  renderTerm();
  const s = activeSession();
  if (s) refreshTail(s);
  if (saUI) saUI.setSession(liveSession());
  if (pvPanel) pvPanel.setSession(s);
  updateChgBtn();
  if (minimap) minimap.setSession(s && s.view ? s : null);
}

/** 会话头「改动」按钮：本会话改动文件数 + 是否正在显示（只读：数据来自前端已有的 transcript，不发请求） */
function updateChgBtn() {
  const s = activeSession();
  if (!s || !pvPanel) { chgBtn.update(0, false); return; }
  chgBtn.update(pvPanel.changeCount(s), pvPanel.isShowingChanges() && pvPanel.s === (s.parent || s));
}

let listKey = '';
let dragSid = null;   // 侧栏拖动排序中的会话
function clearDropMarks() { for (const el of document.querySelectorAll('.sess.drop-before, .sess.drop-after')) el.classList.remove('drop-before', 'drop-after'); }
function renderSessionList() {
  const list = $('session-list');
  const sessions = sortedSessions();
  const key = JSON.stringify(sessions.map((s) => [s.sid, sessionTitle(s), s.meta.jobid, s.meta.cwd, statusOf(s), isActive(s), runningSubagents(s), apiLabel(s.meta, { short: true })]));
  if (key === listKey) return;
  listKey = key;
  if (!sessions.length) {
    list.replaceChildren(h('div', { class: 'side-empty' }, '还没有会话。点击“新建会话”开始。'));
    return;
  }
  list.replaceChildren(...sessions.map((s) => {
    const m = s.meta;
    const [st, stText] = statusOf(s);
    const del = h('button', { class: 'icon-btn row-act', type: 'button', title: '删除会话' }, icon('trash'));
    del.addEventListener('click', (e) => { e.stopPropagation(); doDelete(s); });
    const toBottom = h('button', { class: 'icon-btn row-act row-act-2', type: 'button', title: '移到最下（暂时不用的会话放到底部）' }, icon('down'));
    toBottom.addEventListener('click', (e) => { e.stopPropagation(); moveSessionTo(s.sid, 'bottom'); });
    const row = h('div', { class: rowClass(st, isActive(s)), role: 'listitem', tabindex: '0', draggable: 'true', 'data-sid': s.sid,
      title: (m.cwd || '') + '\n拖动可调整顺序；右键：移到最上 / 最下；Alt+↑/↓ 上下移动' },
      h('span', { class: 'dot st-' + st, title: stText }),
      h('div', { class: 'sess-main' },
        h('div', { class: 'sess-title' }, sessionTitle(s)),
        h('div', { class: 'sess-sub' },
          isLocalMeta(m) ? h('span', { class: 'loc-tag', title: '在本机运行' }, icon('laptop'), '本机')
            : m.jobid ? h('span', { class: 'mono' }, 'job ' + m.jobid) : null,
          m.cwd ? h('span', { class: 'mono' }, basename(m.cwd)) : null,
          h('span', { class: 'st-label st-' + st }, stText),
          h('span', { class: 'api-tag' + (m.launch && m.launch.api_key_id ? '' : ' is-default') + (m.api_key_missing ? ' missing' : ''), title: '所用 API：' + apiLabel(m) }, apiLabel(m, { short: true })),
          runningSubagents(s) ? h('span', { class: 'sa-badge', title: '正在运行的子代理' }, icon('agent'), String(runningSubagents(s))) : null)),
      toBottom, del);
    // 点这一行（包括点已选中的那一行）= 看过了：清除「新回复」
    row.addEventListener('click', () => { markSeen(s); activate(s.sid); });
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { markSeen(s); activate(s.sid); }
      else if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); moveSessionBy(s.sid, e.key === 'ArrowUp' ? -1 : 1); }
    });
    row.addEventListener('contextmenu', (e) => { e.preventDefault(); sessionMenu(s, e.clientX, e.clientY); });
    row.addEventListener('dragstart', (e) => {
      dragSid = s.sid;
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', s.sid); } catch (_) { /* 忽略 */ }
      row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => { dragSid = null; row.classList.remove('dragging'); clearDropMarks(); });
    row.addEventListener('dragover', (e) => {
      if (!dragSid || dragSid === s.sid) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const r = row.getBoundingClientRect();
      const after = e.clientY > r.top + r.height / 2;
      if (row.classList.contains(after ? 'drop-after' : 'drop-before')) return;
      clearDropMarks();
      row.classList.add(after ? 'drop-after' : 'drop-before');
    });
    row.addEventListener('drop', (e) => {
      if (!dragSid) return;
      e.preventDefault();
      const after = row.classList.contains('drop-after');
      const from = dragSid;
      clearDropMarks();
      moveSession(from, s.sid, after);
    });
    return row;
  }));
}

let headerKey = '';
let usageAskedAt = 0;   // API chip 悬停时最多每分钟拉一次用量
function renderHeader() {
  const hd = $('chat-header');
  if (hd.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return; // 正在改名
  const s = activeSession();
  const key = !s ? 'none' : active.type === 'preview' ? 'p:' + active.uuid + ':' + (active.item.cwd || '')
    : JSON.stringify([s.sid, sessionTitle(s), statusOf(s), s.meta.jobid, s.meta.cwd, s.meta.error, s.meta.link,
      isBusy(s), isDead(s.meta), termOpen, restarting.has(s.sid), apiLabel(s.meta), (screenHints(s) || {}).shells || 0]);
  if (key === headerKey) return;
  headerKey = key;
  const menuBtn = h('button', { class: 'icon-btn menu-btn', type: 'button', title: '会话列表' }, icon('menu'));
  menuBtn.addEventListener('click', () => document.body.classList.toggle('side-open'));
  if (!s) { hd.replaceChildren(menuBtn, h('div', { class: 'hd-title muted' }, '未选择会话')); return; }

  if (active.type === 'preview') {
    const it = active.item || {};
    const cont = h('button', { class: 'btn primary small', type: 'button' }, icon('history'), '在此继续');
    cont.addEventListener('click', () => continueHistory(it));
    const local = it.target === 'local';
    hd.replaceChildren(menuBtn,
      h('div', { class: 'hd-main' },
        h('div', { class: 'hd-row' }, h('span', { class: 'badge' }, '历史预览'), h('div', { class: 'hd-title' }, it.title || it.claude_session_id || '')),
        h('div', { class: 'hd-sub' },
          local ? h('span', { class: 'chip job-badge local', title: '本机的历史会话（本机 ~/.claude/projects）' }, icon('laptop'), '本机')
            : h('span', { class: 'chip job-badge', title: '服务器上的历史会话' }, icon('server'), '服务器'),
          h('span', { class: 'chip mono', title: it.cwd || '' }, icon('folder'), it.cwd || '未知目录'),
          h('span', { class: 'chip mono' }, String(it.claude_session_id || '')))),
      h('div', { class: 'hd-actions' }, chgBtn.el, cont));
    updateChgBtn();
    return;
  }

  const m = s.meta;
  const [st, stText] = statusOf(s);
  const dead = isDead(m);
  const busy = isBusy(s);
  const title = h('button', { class: 'hd-title editable', type: 'button', title: '点击改名' }, sessionTitle(s));
  title.addEventListener('click', () => editTitle(s, title));
  const local = isLocalMeta(m);
  // 运行位置：本机（不可改）/ 作业 N（点击 = 换作业 --resume 续接）
  let jobBadge;
  if (local) {
    jobBadge = h('span', { class: 'chip job-badge local', title: `在本机运行：这台 Mac 的私有 tmux（-L ${tmuxLabel()}）里的交互式 claude` }, icon('laptop'), '本机');
  } else {
    jobBadge = h('button', { class: 'chip job-badge mono', type: 'button', title: '点击修改 jobid（在新作业上 --resume 续接本会话）' }, icon('terminal'), 'job ' + (m.jobid || '—'));
    jobBadge.addEventListener('click', () => changeSessionJob(s));
  }

  // 模型 / effort / 权限模式 / 统计在输入框底栏（renderComposerControls、renderStats）
  // 后台 shell 数（Claude Code 底栏 "N shell"）
  const shells = !dead ? Number((screenHints(s) || {}).shells) || 0 : 0;
  const shellChip = shells ? h('span', { class: 'chip shell-chip', title: 'Claude Code 底栏显示有 ' + shells + ' 个后台 shell 在运行（可在终端面板里按 ↓ 管理）' },
    icon('terminal'), '后台 shell ' + shells) : null;
  // 当前使用的 API（--settings 的 key 名称；默认 = 服务器 settings.json）：点击 = 换 API 重启续接
  const apiTitle = () => apiUsageTitle(m) + '\n' + (m.api_key_missing ? '该 API key 已被删除，重启/续接前需要改选。\n' : '')
    + '点击可换 API：以 --resume 重启 claude 续接本会话';
  const apiChip = h('button', { class: 'chip chip-btn api-chip' + (m.api_key_missing ? ' missing' : ''), type: 'button', disabled: restarting.has(s.sid), title: apiTitle() },
    icon('key'), apiLabel(m), h('span', { class: 'chip-act' }, '换 API'));
  apiChip.addEventListener('click', () => switchApi(s));
  apiChip.addEventListener('mouseenter', () => {   // tooltip 显示该 key 的近 7 天用量（后端缓存 5 分钟）
    apiChip.title = apiTitle();
    if (Date.now() - usageAskedAt > 60000) { usageAskedAt = Date.now(); loadUsage(api).catch(() => {}); }
  });

  const actionsEl = [];
  if (busy && !dead) {
    const stop = h('button', { class: 'btn danger-ghost small', type: 'button', title: '在终端里按 Esc' }, icon('stop'), h('span', null, '中断（Esc）'));
    stop.addEventListener('click', () => doInterrupt(s));
    actionsEl.push(stop);
  }
  actionsEl.push(chgBtn.el);
  const termBtn = h('button', { class: 'btn small' + (termOpen ? ' primary' : ' ghost'), type: 'button', title: '显示/隐藏终端面板（屏幕原文 + 键盘）' }, icon('terminal'), h('span', null, '终端'));
  termBtn.addEventListener('click', () => setTerm(!termOpen));
  actionsEl.push(termBtn);
  const ended = jobEnded(m);
  const re = h('button', { class: 'btn ghost small', type: 'button', disabled: restarting.has(s.sid),
    title: ended ? '作业 ' + (m.jobid || '') + ' 已结束：选择新的作业，以 --resume 续接本会话' : dead ? (local ? '在本机 --resume 重新连接' : '在当前作业上 --resume 重新连接') : '关闭并以 --resume 重启 claude' },
  icon('refresh'), h('span', null, ended ? '换作业续接' : dead ? '重连' : '重启'));
  re.addEventListener('click', () => doRestart(s));
  actionsEl.push(re);
  if (!dead) {
    const close = h('button', { class: 'btn ghost small', type: 'button', title: '在终端里输入 /exit 结束 claude（保留在列表中）' }, icon('power'), h('span', null, '关闭'));
    close.addEventListener('click', () => doClose(s));
    actionsEl.push(close);
  }
  hd.replaceChildren(menuBtn,
    h('div', { class: 'hd-main' },
      h('div', { class: 'hd-row' }, title, h('span', { class: 'status-chip st-' + st }, h('span', { class: 'dot st-' + st }), stText)),
      h('div', { class: 'hd-sub' },
        jobBadge,
        h('span', { class: 'chip mono', title: m.cwd || '' }, icon('folder'), m.cwd ? truncate(m.cwd, 48) : '~'),
        apiChip, shellChip,
        m.error ? h('span', { class: 'chip err', title: m.error }, truncate(m.error, 60)) : null)),
    h('div', { class: 'hd-actions' }, actionsEl));
  updateChgBtn();
}

/** 输入框底栏左侧：模型/effort 胶囊（两个点击区）+ 权限模式 chip + Shift+Tab。
 *  ComposerControls 自己按 key 判断是否重建（模型/effort 文字、禁用原因、模式、循环、会话）；预览/未选会话时清空 */
function renderComposerControls() {
  if (!composerCtl) return;
  const s = activeSession();
  if (!s || !active || active.type !== 'session') { composerCtl.update(null); return; }
  const m = s.meta;
  const dead = isDead(m);
  const dialog = !!currentDialog(s);
  const busy = isBusy(s);
  // 模型 / effort：已退出、有对话框、运行中、断线时不可用（title 说明原因）
  const why = dead ? '会话已退出' : dialog ? '请先处理当前对话框' : busy ? '运行中，请等本轮结束（或先中断）' : m.link === 'down' ? '与' + placeOf(m) + '的连接已断开' : '';
  // 权限模式（SPEC §13）：运行中也允许切换；已退出 / 有对话框 / 断线时循环里的模式不可选（「需重启」仍可选）
  const modeBlocked = dead ? '会话已退出' : dialog ? '请先处理当前对话框' : m.link === 'down' ? '与' + placeOf(m) + '的连接已断开' : '';
  const cycleMode = () => api.post(sp(s, '/mode/cycle')).catch((e) => toast('切换权限模式失败：' + e.message, 'error'));
  composerCtl.update({
    sid: s.sid, model: modelLabel(s), effort: effortLabel(s), why,
    mode: currentMode(s), cycle: cycleOf(m), modeBlocked,
    onModel: () => openModelFor(s, true),
    onEffort: () => openEffortModal({ s, actions, current: s.modelInfo.effort || (s.meta.launch && s.meta.launch.effort) || '' }),
    onPickMode: (id, needsRestart) => pickMode(s, id, needsRestart),
    onCycle: cycleMode,
  });
}

/** 统计条：数据 = meta.stats（后端节流后随 meta 推送）；忙时显示生成中 tok/s，空闲显示上一轮平均 */
function renderStats() {
  const s = activeSession();
  if (!s || !active || active.type !== 'session') { statsBar.update(null); return; }
  statsBar.update(s.meta.stats, { busy: isBusy(s), dead: isDead(s.meta) });
}

function renderComposerState() {
  const comp = $('composer');
  const s = activeSession();
  if (!s || active.type !== 'session') { comp.hidden = true; return; }
  comp.hidden = false;
  const m = s.meta;
  const dead = isDead(m);
  const d = currentDialog(s);
  const busy = isBusy(s);
  const banner = $('composer-banner');
  if (dead && jobEnded(m)) {
    // 作业已结束：不再"自动重连"，只能在新作业上 --resume 续接
    const btn = h('button', { class: 'btn primary small', type: 'button' }, icon('refresh'), '在新作业上续接');
    btn.addEventListener('click', () => changeSessionJob(s));
    setBanner(banner, 'ended:' + m.jobid, () => [h('span', null, '作业 ' + (m.jobid || '') + ' 已结束，可在新作业上续接（--resume，对话历史不丢）'), btn]);
  } else if (dead) {
    const btn = h('button', { class: 'btn primary small', type: 'button' }, icon('refresh'), '重连');
    btn.addEventListener('click', () => doRestart(s));
    setBanner(banner, 'dead:' + m.state + m.error, () => [h('span', null, m.state === 'error' ? '会话出错' + (m.error ? '：' + truncate(m.error, 120) : '') : '会话已退出（可 --resume 重连）'), btn]);
  } else if (jobEnded(m)) {
    setBanner(banner, 'ended-live:' + m.jobid, () => [h('span', null, '作业 ' + (m.jobid || '') + ' 已结束，可在新作业上续接')]);
  } else if (m.link === 'down') {
    setBanner(banner, 'link:' + placeOf(m), () => [spinner(), h('span', null, isLocalMeta(m)
      ? '与本机 agent 的连接已断开，正在自动重连…（本机 tmux 里的 claude 不受影响）'
      : '与计算节点的连接已断开，正在自动重连…（远端 claude 不受影响）')]);
  } else if (m.state === 'starting') {
    setBanner(banner, 'starting', () => [spinner(), h('span', null, '正在启动 claude…')]);
  } else if (d) {
    setBanner(banner, 'dialog', () => [icon('shield'), h('span', null, '请先处理上方对话框（选项按钮会把对应按键发给终端）')]);
  } else if (m.state === 'waiting') {
    // claude 自己报告在等审批/对话框，但画面上没识别出来：发消息会被拒绝，引导去终端面板
    const btn = h('button', { class: 'btn small', type: 'button' }, icon('terminal'), '打开终端面板');
    btn.addEventListener('click', () => setTerm(true));
    setBanner(banner, 'waiting', () => [icon('shield'), h('span', null, 'Claude 正在等待你处理一个对话框（画面未识别），请在终端面板里查看'), btn]);
  } else setBanner(banner, '', null);

  const ta = $('input');
  const blocked = dead || !!d;
  // 运行中的提示写进占位文字（底栏留给控件）；比默认占位短，不会因此多折一行
  const ph = d ? '请先处理上方对话框' : dead ? '会话已退出' : busy ? '运行中：新消息会进入 claude 的输入队列'
    : '给 Claude 发消息（Enter 发送 · Shift+Enter 换行 · / 命令）';
  // 先改占位文字再测高：空输入框的高度按占位文字量（AutoSize 连同 placeholder 一起量）
  const phChanged = ta.placeholder !== ph;
  if (phChanged) ta.placeholder = ph;
  const disChanged = ta.disabled !== blocked;
  if (disChanged) ta.disabled = blocked;
  if (phChanged || disChanged) autosize();
  $('send-btn').disabled = blocked;
  $('stop-btn').hidden = !(busy && !dead);
  if (hintBar) hintBar.update(dead ? null : screenHints(s), { blocked: dead || !!d || m.link === 'down' });
}

function setBanner(el, key, build) {
  if (el.dataset.key === key) return;
  el.dataset.key = key;
  if (!build) { el.hidden = true; el.replaceChildren(); return; }
  el.replaceChildren(...build());
  el.hidden = false;
}

function renderTerm() {
  const panel = $('term-panel');
  const s = liveSession();
  panel.hidden = !termOpen || !s;
  if (!panel.hidden) term.setSession(s);
}

function setTerm(on) {
  termOpen = !!on;
  localStorage.setItem('ccr.term', termOpen ? '1' : '0');
  const s = liveSession();
  if (termOpen && s) { term.setSession(s); refreshScreen(s).catch((e) => toast('读取屏幕失败：' + e.message, 'error')); }
  renderAll();
}

// ====================== 会话操作 ======================

async function editTitle(s, btn) {
  const shown = sessionTitle(s);
  const input = h('input', { class: 'input hd-title-input', type: 'text', name: 'title', 'aria-label': '会话标题', value: shown,
    placeholder: '留空 = 使用 Claude Code 里的标题' + (s.meta.cli_title ? '（' + s.meta.cli_title + '）' : ''),
    title: '回车保存；清空后保存 = 改回使用 Claude Code 里的会话标题（在 CLI 里 /rename 也会同步过来）' });
  btn.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const commit = async (save) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    input.blur();
    if (save && v !== shown) {
      try {
        const r = await api.patch(sp(s, ''), { title: v });
        if (r && r.sid) s.meta = { ...s.meta, ...r };
        else if (v) s.meta = { ...s.meta, title: v, title_user: true, display_title: v };
        else s.meta = { ...s.meta, title_user: false, display_title: s.meta.cli_title || s.meta.title };
      } catch (e) { toast('改名失败：' + e.message, 'error'); }
    }
    headerKey = '';
    renderAll();
  };
  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); commit(true); }
    if (e.key === 'Escape') { e.preventDefault(); commit(false); }
  });
  input.addEventListener('blur', () => commit(true));
}

/** apiKeyId：undefined = 沿用当前 API；null = 服务器默认；字符串 = 改用该 key */
async function changeSessionJob(s, apiKeyId) {
  if (isLocalMeta(s.meta)) { toast('本机会话没有作业，只能在本机重启 / 重连', 'warn'); return; }
  const j = await promptDialog('修改此会话的 jobid', {
    label: '新的作业 jobid（将在该作业上 --resume 续接本会话）',
    value: s.meta.jobid || config.jobid || '',
    validate: (v) => (JOBID_RE.test(v) ? '' : 'jobid 格式不正确'),
    okText: '下一步',
    extra: (input) => {
      const b = h('button', { class: 'btn ghost', type: 'button' }, '我的作业');
      b.addEventListener('click', () => jobsPopover(api, b, (id) => { input.value = id; }));
      return b;
    },
  });
  if (!j || j === s.meta.jobid) return;
  const ok = await confirmDialog('确认切换作业', isDead(s.meta)
    ? `将在作业 ${j} 上以 --resume 续接此会话（对话历史不丢）。`
    : `将在当前终端里退出 claude${isBusy(s) ? '（正在运行的回复会被中断）' : ''}，并在作业 ${j} 上以 --resume 续接此会话。`, { okText: '切换并续接' });
  if (!ok) return;
  await restartOn(s, j, apiKeyId);
}

const restarting = new Set();

/** apiKeyId：undefined = 沿用当前 API；null = 服务器默认；字符串 = 改用该 key（claude 只在启动时读 key） */
/** mode：覆盖权限启动参数（模式选择栏里"需重启"的模式）：bypassPermissions = 跳过权限，其它 = --permission-mode */
async function restartOn(s, jobid, apiKeyId, mode) {
  if (restarting.has(s.sid)) { toast('正在重启…请稍候'); return; }
  const body = {};
  if (jobid) body.jobid = jobid;
  if (apiKeyId !== undefined) body.api_key_id = apiKeyId;
  if (mode) body.mode = mode;
  const switching = apiKeyId !== undefined && (apiKeyId || '') !== ((s.meta.launch && s.meta.launch.api_key_id) || '');
  restarting.add(s.sid);
  scheduleUI();
  toast(jobid ? `正在作业 ${jobid} 上续接…` : mode ? `正在以「${modeName(mode)}」重启并续接…` : switching ? '正在换 API 并续接…' : '正在重新连接…');
  try {
    const r = await withForceRetry((force) => api.post(sp(s, '/restart'), force ? { ...body, force: true } : body),
      '无法关闭旧会话', jobid ? '在新作业上续接' : '重启', isLocalMeta(s.meta));
    if (r && r.sid) { s.meta = { ...s.meta, ...r }; }
  } catch (e) {
    if (e && e.status === 409 && e.code === 'job_ended') {
      toast(e.message, 'warn');
      restarting.delete(s.sid);
      changeSessionJob(s, apiKeyId);   // 旧作业已结束：直接让用户选新作业
      return;
    }
    if (e && e.status === 409 && e.code === 'api_key_missing') {
      toast(e.message, 'warn');
      restarting.delete(s.sid);
      switchApi(s, jobid, true);       // 该会话用的 key 已被删除：让用户改选
      return;
    }
    if (e && e.status === 409 && e.code === 'target_mismatch') { toast(e.message, 'warn'); return; }
    if (e && e.message !== '已取消') toast('重启失败：' + e.message, 'error');
  } finally {
    restarting.delete(s.sid);
    scheduleUI();
  }
}

/** 模式选择栏：循环里的模式 → POST /mode（后端逐个 Shift+Tab 并确认）；不在循环里的 → 确认后以 --resume 重启并改启动参数 */
async function pickMode(s, mode, needsRestart) {
  if (!needsRestart) {
    try {
      const r = await api.post(sp(s, '/mode'), { mode });
      toast('已切换到「' + modeName(mode) + '」' + (r && r.steps ? '（按了 ' + r.steps + ' 次 Shift+Tab）' : ''));
    } catch (e) {
      if (e && e.status === 409 && e.code === 'mode_needs_restart') { pickMode(s, mode, true); return; }
      toast('切换权限模式失败：' + e.message, e && e.status === 409 ? 'warn' : 'error');
      refreshScreen(s).catch(() => {});
    }
    return;
  }
  const arg = mode === 'bypassPermissions' ? '--dangerously-skip-permissions' : '--permission-mode ' + mode;
  const ok = await confirmDialog('切换到「' + modeName(mode) + '」需要重启',
    '「' + modeName(mode) + '」（' + mode + '）不在本次启动的 Shift+Tab 循环里。\n\n'
    + '将在终端里退出 claude（/exit），再以 --resume 和 ' + arg + ' 重启并续接同一段对话（对话历史保留；模型、effort 沿用当前值）。'
    + (isBusy(s) ? '\n\n当前正在生成，本轮回复会被中断。' : currentDialog(s) ? '\n\n终端里有未处理的对话框，重启会取消它。' : ''),
    { okText: '重启并切换', danger: isBusy(s) });
  if (!ok) return;
  await restartOn(s, null, undefined, mode);
}

async function doRestart(s) {
  if (jobEnded(s.meta)) { await changeSessionJob(s); return; }   // 作业已结束：只能换作业续接
  if (isDead(s.meta)) { await restartOn(s, null); return; }       // 已退出："重连" = 直接续接（沿用 API）
  const m = s.meta;
  const r = await restartApiDialog(api, {
    title: '重启会话',
    message: '将在终端里退出 claude（输入 /exit），再以 --resume 重新启动并续接本会话；当前的模型、effort、权限模式会保留。',
    warn: isBusy(s) ? '当前正在运行，本轮回复会被中断。' : currentDialog(s) ? '终端里有未处理的对话框，重启会取消它。' : '',
    okText: '重启', current: (m.launch && m.launch.api_key_id) || '', currentName: m.api_key_name || '',
    local: isLocalMeta(m),
  });
  if (!r) return;
  await restartOn(s, null, r.api_key_id);
}

/**
 * 会话头「API」chip：换 API 重启续接。claude 只在启动时读取 key（运行中改 --settings 文件不生效），
 * 所以换 API = /exit 后以 --resume + 新的 --settings 重启 claude（同一个本地会话、同一段对话）。
 * afterMissing：重启时发现原 key 已被删除，要求改选。
 */
async function switchApi(s, jobid = null, afterMissing = false) {
  if (restarting.has(s.sid)) { toast('正在重启…请稍候'); return; }
  const m = s.meta;
  const dead = isDead(m);
  const ended = jobEnded(m) && !jobid;
  const interrupting = () => !isDead(s.meta) && (isBusy(s) || !!currentDialog(s));
  const message = dead
    ? '将以 --resume 续接此会话并改用所选 API（对话历史不丢）。' + (ended ? `作业 ${m.jobid || ''} 已结束，下一步还需要选择新的作业。` : '')
    : 'claude 只在启动时读取 API key，所以换 API = 在终端里退出 claude（/exit），再用所选 key 以 --resume 重启并续接同一段对话'
      + '（本地会话不变，通常几秒到半分钟）。当前的模型、effort、权限模式会保留。';
  // 换 key 的代价（提示缓存可能失效…）由下拉框在改选后显示（SWITCH_COST_HINT）
  const r = await restartApiDialog(api, {
    title: afterMissing ? '请改选 API' : '换 API 重启续接', message,
    warn: interrupting() ? (isBusy(s) ? 'Claude 正在回复：切换会中断当前这一轮回复。' : '终端里有未处理的对话框：切换会取消它。') : '',
    okText: dead ? '续接' : '重启并切换', current: (m.launch && m.launch.api_key_id) || '', currentName: m.api_key_name || '',
    sameOk: afterMissing, local: isLocalMeta(m),
  });
  if (!r) return;
  // 回复进行中 / 有对话框：再确认一次（以点击时的最新状态为准）
  if (interrupting() && !(await confirmDialog('确认切换', '切换会中断当前这一轮回复/对话框，确定？', { okText: '中断并切换', danger: true }))) return;
  if (ended) { await changeSessionJob(s, r.api_key_id); return; }
  await restartOn(s, jobid, r.api_key_id);
}

async function doClose(s) {
  if (!(await confirmDialog('关闭会话', '在终端里输入 /exit 结束 claude（会话保留在列表中，可随时“重连”续接）。', { okText: '关闭' }))) return;
  try {
    await withForceRetry((force) => api.post(sp(s, '/close'), force ? { force: true } : {}),
      isLocalMeta(s.meta) ? '无法关闭本机会话' : '无法关闭远端会话', '关闭', isLocalMeta(s.meta));
  } catch (e) { if (e && e.message !== '已取消') toast('关闭失败：' + e.message, 'error'); }
}

async function doDelete(s) {
  if (!(await confirmDialog('删除会话', `从列表中移除“${sessionTitle(s)}”并结束其进程。claude 的历史记录不受影响，可在“历史会话”中找回。`, { okText: '删除', danger: true }))) return;
  try {
    await api.del(sp(s, ''));
    dropSession(s.sid);
  } catch (e) {
    if (!(e && e.status === 409)) { toast('删除失败：' + e.message, 'error'); return; }
    // 409（连不上计算节点 / 本机 agent、正在执行其它操作…）：提供"强制删除"（只删本地记录，远端 / 本机 tmux 里的进程可能残留）
    const left = isLocalMeta(s.meta) ? `本机 tmux（-L ${tmuxLabel()}）里的 claude 进程可能残留` : '远端的 claude 进程可能残留（需要时可在终端里自行结束）';
    const ok = await confirmDialog('删除失败', e.message + '\n\n要强制删除吗？只从列表中移除本地记录，' + left + '。',
      { okText: '强制删除', danger: true });
    if (!ok) return;
    try {
      await api.del(sp(s, '?force=1'));
      dropSession(s.sid);
    } catch (e2) { toast('强制删除失败：' + e2.message, 'error'); }
  }
}

function dropSession(sid) {
  const s = state.sessions.get(sid);
  if (s && s.view) s.view.root.remove();
  if (s) clearTimeout(s.draft.timer);
  if (modelCtrl && modelCtrl.sid === sid) closeModelModal();
  state.sessions.delete(sid);
  drafts.delete(sid);
  sentPending.delete(sid);
  updateDocTitle();
  if (active && active.type === 'session' && active.sid === sid) {
    const next = sortedSessions()[0];
    if (next) activate(next.sid); else showEmpty();
  } else scheduleUI();
}

async function doInterrupt(s) {
  try { await api.post(sp(s, '/interrupt')); } catch (e) { toast('中断失败：' + e.message, 'error'); }
}

function modelHints() {
  const out = [];
  for (const s of state.sessions.values()) {
    for (const r of (s.modelList && s.modelList.rows) || []) {
      const id = String(r.desc || '').match(/\((claude-[A-Za-z0-9._\-\[\]]+)\)/);
      if (id) out.push(id[1]);
    }
  }
  return out;
}

async function openNewSession(preset) {
  const meta = await newSessionDialog(api, { config, preset, modelHints: modelHints() });
  if (!meta || !meta.sid) return;
  const s = ensureSession(state, meta.sid, meta);
  activate(s.sid);
}

// ====================== 侧栏 ======================

function applyConfigUI() {
  const ji = $('jobid-input');
  if (document.activeElement !== ji) ji.value = config.jobid || '';
  $('host-label').textContent = (config.host ? ' @' + config.host : '') + (config.fake ? '（演示模式）' : '');
}

function bindSidebar() {
  const ji = $('jobid-input');
  const hint = $('job-hint');
  const saveJob = async () => {
    const v = ji.value.trim();
    if (v === (config.jobid || '')) { hint.textContent = '未改变'; return; }
    if (v && !JOBID_RE.test(v)) { hint.textContent = 'jobid 格式不正确（数字，或 数字_数字）'; hint.className = 'field-hint err'; return; }
    hint.className = 'field-hint';
    hint.textContent = '保存中…';
    try {
      const r = await api.post('/api/config', { jobid: v });
      config = r && typeof r === 'object' && 'jobid' in r ? r : { ...config, jobid: v };
      hint.textContent = '已保存：新会话将使用作业 ' + (config.jobid || '（未设置）');
      hint.className = 'field-hint ok';
    } catch (e) {
      hint.textContent = '保存失败：' + e.message;
      hint.className = 'field-hint err';
    }
  };
  ji.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); saveJob(); }
    if (e.key === 'Escape') { ji.value = config.jobid || ''; ji.blur(); }
  });
  ji.addEventListener('input', () => { hint.className = 'field-hint'; hint.textContent = '按回车保存'; });
  $('jobs-btn').addEventListener('click', () => jobsPopover(api, $('jobs-btn'), (j) => { ji.value = j; saveJob(); }));
  $('new-session-btn').addEventListener('click', () => openNewSession({}));
  $('settings-btn').addEventListener('click', () => settingsDialog(api, { config, onSaved: (c) => { config = { ...config, ...c }; applyConfigUI(); } }));

  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => {
      for (const t of document.querySelectorAll('.tab')) t.classList.toggle('active', t === tab);
      const hist = tab.dataset.tab === 'history';
      $('session-list').hidden = hist;
      $('history-pane').hidden = !hist;
      if (hist && !historyItems && !historyLoading) loadHistoryList();
    });
  }
  $('history-refresh').addEventListener('click', () => loadHistoryList());
  $('history-filter').addEventListener('input', () => renderHistoryList());
  // 历史面板：服务器 / 本机 切换（各自缓存；续接时用对应的运行位置）
  for (const b of document.querySelectorAll('#history-target .seg-item')) {
    b.addEventListener('click', () => setHistoryTarget(b.dataset.target));
  }
  renderHistoryTarget();
}

function renderHistoryTarget() {
  for (const b of document.querySelectorAll('#history-target .seg-item')) {
    const on = b.dataset.target === historyTarget;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  }
}

function setHistoryTarget(t) {
  t = t === 'local' ? 'local' : 'server';
  if (t === historyTarget) return;
  historyTarget = t;
  localStorage.setItem('ccr.histTarget', t);
  renderHistoryTarget();
  historyItems = historyCache[t];
  if (historyItems) renderHistoryList();
  else loadHistoryList();
}

async function loadHistoryList() {
  const target = historyTarget;
  const my = ++historyReq;
  historyLoading = true;
  const list = $('history-list');
  list.replaceChildren(h('div', { class: 'side-empty' }, spinner(), target === 'local' ? ' 正在读取本机历史…' : ' 正在读取远端历史…'));
  try {
    const r = await api.get('/api/history?limit=100' + targetQuery(target));
    const items = (Array.isArray(r.sessions) ? r.sessions : []).map((it) => ({ ...it, target }));
    historyCache[target] = items;
    if (my !== historyReq || target !== historyTarget) return;   // 期间切换了运行位置
    historyItems = items;
    renderHistoryList();
  } catch (e) {
    if (my !== historyReq || target !== historyTarget) return;
    list.replaceChildren(h('div', { class: 'side-empty err' }, '读取失败：' + e.message));
  } finally { if (my === historyReq) historyLoading = false; }
}

function renderHistoryList() {
  const list = $('history-list');
  if (!historyItems) return;
  const q = $('history-filter').value.trim().toLowerCase();
  const items = historyItems.filter((it) => !q || String(it.title || '').toLowerCase().includes(q) || String(it.cwd || '').toLowerCase().includes(q));
  if (!items.length) { list.replaceChildren(h('div', { class: 'side-empty' }, q ? '没有匹配项' : historyTarget === 'local' ? '本机没有历史会话' : '没有历史会话')); return; }
  list.replaceChildren(...items.map((it) => {
    const isCur = active && active.type === 'preview' && active.uuid === it.claude_session_id && active.item && active.item.target === it.target;
    const row = h('div', { class: 'sess hist' + (isCur ? ' active' : ''), tabindex: '0', title: it.cwd || '' },
      icon('history', 'hist-ic'),
      h('div', { class: 'sess-main' },
        h('div', { class: 'sess-title' }, it.title || '（无标题）'),
        h('div', { class: 'sess-sub' },
          it.cwd ? h('span', { class: 'mono' }, basename(it.cwd)) : null,
          it.mtime ? h('span', null, fmtRelTime(it.mtime)) : null,
          it.size != null ? h('span', null, fmtSize(it.size)) : null,
          it.api_key ? h('span', { class: 'api-tag' + (it.api_key.id ? '' : ' is-default') + (it.api_key.missing ? ' missing' : ''), title: '该会话上次使用的 API' + (it.api_key.missing ? '（已删除）' : '') },
            apiLabel({ jobid: it.target === 'local' ? 'local' : '', launch: { api_key_id: it.api_key.id }, api_key_name: it.api_key.name, api_key_missing: it.api_key.missing }, { short: true })) : null)));
    row.addEventListener('click', () => showPreview(it));
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter') showPreview(it); });
    return row;
  }));
}

async function showPreview(it) {
  saveDraft();
  closeModelModal();
  if (active && active.type === 'preview' && active.session && active.session.view) active.session.view.root.remove();
  const s = ensureSession(createState({ actions: {}, onFlush: minimapFlush }), 'hist:' + it.claude_session_id, { title: it.title, cwd: it.cwd, state: 'exited' });
  s.readOnly = true;
  active = { type: 'preview', uuid: it.claude_session_id, session: s, item: it };
  hideAllViews();
  $('empty-state').hidden = true;
  mountSession(s, $('chat-host'));
  const loading = h('div', { class: 'history-loading' }, spinner(), ' 正在加载…');
  s.view.col.insertBefore(loading, s.view.list);
  closeSidebarOnNarrow();
  renderAll();
  renderHistoryList();
  try {
    const r = await api.get('/api/history/' + encodeURIComponent(it.claude_session_id) + targetQuery(it.target, '?'));
    if (active.session !== s) return;
    if (r.cwd && !it.cwd) it.cwd = r.cwd;
    const n = applyHistory(s, r.entries);
    if (!n) addLocalNotice(s, 'info', '这个会话没有可显示的消息');
    requestAnimationFrame(() => scrollToBottom(s));
    headerKey = '';
    renderHeader();
  } catch (e) {
    addLocalNotice(s, 'error', '加载失败：' + e.message);
  } finally { loading.remove(); }
}

async function continueHistory(it) {
  const running = sortedSessions().find((s) => s.meta.claude_session_id === it.claude_session_id && !isDead(s.meta));
  if (running) {
    if (await confirmDialog('会话已在运行', `该 claude 会话已在本地会话“${sessionTitle(running)}”中运行（同一会话只能有一个进程）。切换过去吗？`, { okText: '切换过去' })) activate(running.sid);
    return;
  }
  // 沿用该 claude 会话上次用的 API key（session_keys.json 的记录；没有记录才默认"服务器默认"/本机默认）
  const local = it.target === 'local';
  const k = it.api_key || null;
  const last = k ? apiLabel({ launch: { api_key_id: k.id }, api_key_name: k.name }) : '';
  await openNewSession({
    target: local ? 'local' : 'server',   // 续接只能在这段对话所在的机器上
    cwd: it.cwd || (local ? '~' : config.default_cwd || '~'), resume: it.claude_session_id,
    api_key_id: k && k.id ? k.id : '', api_key_name: k ? (k.name || '') : '',
    api_note: k ? '该会话上次使用：' + (k.id ? last : local ? '默认（本机 settings.json）' : '服务器默认（settings.json）') : '',
    api_missing_text: k && k.missing ? `上次使用的 key「${k.name || k.id}」已删除，请改选` : '',
  });
}

function closeSidebarOnNarrow() { document.body.classList.remove('side-open'); }

// ====================== 输入框 ======================

// 交互式 claude 的常用命令（静态列表；发送后由 claude 自己处理，/model、/effort 会弹出同一套选择界面）
const SLASH_COMMANDS = ['model', 'effort', 'clear', 'compact', 'context', 'cost', 'status', 'help', 'init', 'memory', 'review', 'resume', 'exit'];

let composing = false;
let compositionEndAt = 0;
let slashIndex = 0;
let slashMatches = [];
let restoredDrafts = null;

function currentDraft() {
  const s = activeSession();
  if (!s || active.type !== 'session') return null;
  let d = drafts.get(s.sid);
  if (!d) {
    if (!restoredDrafts) {
      try { restoredDrafts = JSON.parse(sessionStorage.getItem('ccr.drafts') || '{}') || {}; } catch (_) { restoredDrafts = {}; }
      sessionStorage.removeItem('ccr.drafts');
    }
    d = { text: String(restoredDrafts[s.sid] || '') };
    drafts.set(s.sid, d);
  }
  return d;
}

function saveDraft() {
  const d = currentDraft();
  if (d) d.text = $('input').value;
}

/** 离开页面前（⌘R、原生窗口在后端重启后重新载入等）把各会话的草稿存进 sessionStorage，载入后由 currentDraft() 恢复 */
function persistDrafts() {
  try {
    saveDraft();
    const out = {};
    if (restoredDrafts) for (const [k, v] of Object.entries(restoredDrafts)) if (v) out[k] = String(v);
    for (const [sid, d] of drafts) {
      if (d && d.text) out[sid] = d.text;
      else delete out[sid];
    }
    if (Object.keys(out).length) sessionStorage.setItem('ccr.drafts', JSON.stringify(out));
    else sessionStorage.removeItem('ccr.drafts');
  } catch (_) { /* sessionStorage 不可用时放弃 */ }
}
window.addEventListener('pagehide', persistDrafts);
window.addEventListener('beforeunload', persistDrafts);

function loadDraft() {
  const d = currentDraft();
  $('input').value = d ? d.text : '';
  autosize();
  closeSlash();
}

let sizer = null;
function autosize() {
  if (!sizer) sizer = new AutoSize($('input'), { onChange: keepChatPinned });
  sizer.update();
}

/** 时间线上方/下方的高度变了（输入框多一行、快捷键提示条/横幅出现或消失、窗口缩放）：
 *  原本贴底就继续贴底；停在中间则 scrollTop 不动（内容在屏幕上不动）。WebKit 没有滚动锚定，不能指望浏览器代劳 */
function keepChatPinned() {
  const s = activeSession();
  const v = s && s.view;
  if (v && v.stick && v.root.isConnected) v.root.scrollTop = v.root.scrollHeight;
}

function bindComposer() {
  const ta = $('input');
  ta.addEventListener('compositionstart', () => { composing = true; });
  ta.addEventListener('compositionend', () => { composing = false; compositionEndAt = performance.now(); });
  ta.addEventListener('input', () => { autosize(); updateSlash(); });
  ta.addEventListener('keydown', onComposerKey);
  ta.addEventListener('blur', () => setTimeout(closeSlash, 150));
  $('send-btn').addEventListener('click', send);
  $('stop-btn').addEventListener('click', () => { const s = liveSession(); if (s) doInterrupt(s); });
  window.addEventListener('resize', () => autosize());
  autosize();
  // ResizeObserver 回调在布局之后、绘制之前执行：贴底时在同一帧里补回，不会先露出一帧错位
  if (typeof ResizeObserver === 'function') new ResizeObserver(keepChatPinned).observe($('chat-host'));
  // 在当前会话的时间线或输入框里点一下 = 看过了（只认指针点击；滚动、窗口获得焦点都不算）
  const seenOnPointer = (e) => {
    const el = e.target;
    if (el && el.clientWidth && e.offsetX >= el.clientWidth) return;   // 拖滚动条 = 滚动，不算
    const s = liveSession();
    if (s) markSeen(s);
  };
  $('chat-host').addEventListener('pointerdown', seenOnPointer);
  $('composer').addEventListener('pointerdown', seenOnPointer);
}

function isImeEnter(e) {
  // 中文输入法：组字期间的回车只是确认候选词，不能发送
  return e.isComposing || composing || e.keyCode === 229 || performance.now() - compositionEndAt < 30;
}

function onComposerKey(e) {
  const pop = $('slash-pop');
  if (!pop.hidden && slashMatches.length) {
    if (e.key === 'ArrowDown') { e.preventDefault(); slashIndex = (slashIndex + 1) % slashMatches.length; renderSlash(); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); slashIndex = (slashIndex - 1 + slashMatches.length) % slashMatches.length; renderSlash(); return; }
    if ((e.key === 'Tab' || e.key === 'Enter') && !e.shiftKey && !isImeEnter(e)) { e.preventDefault(); pickSlash(slashMatches[slashIndex]); return; }
    if (e.key === 'Escape') { e.preventDefault(); closeSlash(); return; }
  }
  const dir = historyDir(e, composing);
  if (dir && recallPrompt(dir)) { e.preventDefault(); return; }
  if (e.key === 'Enter' && !e.shiftKey && !e.altKey) {
    if (isImeEnter(e)) return;
    e.preventDefault();
    send();
  }
}

/** 当前会话的 ↑/↓ 历史：transcript 里的用户消息 + 本页发出、还没写进 transcript 的（运行中排队的等） */
function promptList(s) {
  const r = mergePending(promptsFromItems(s.items), sentPending.get(s.sid) || []);
  if (r.pending.length) sentPending.set(s.sid, r.pending); else sentPending.delete(s.sid);
  return r.list;
}

/** ↑/↓ 把之前发过的消息放进输入框（只改输入框，不发请求、不按键）。返回 true = 已处理 */
function recallPrompt(dir) {
  const s = liveSession();
  const d = currentDraft();
  if (!s || !d) return false;
  const ta = $('input');
  if (!sizer) autosize();
  if (!d.nav) d.nav = new PromptNav();
  const text = recallInto(ta, dir, sizer, d.nav, () => promptList(s));
  if (text == null) return false;
  d.text = text;
  autosize();
  closeSlash();
  ta.scrollTop = dir < 0 ? 0 : ta.scrollHeight;   // 内容超过最大高度时让光标所在的一端可见
  return true;
}

function updateSlash() {
  const ta = $('input');
  const v = ta.value.slice(0, ta.selectionStart);
  const m = v.match(/^\/([^\s/]*)$/);
  if (!liveSession() || !m) { closeSlash(); return; }
  const q = m[1].toLowerCase();
  slashMatches = SLASH_COMMANDS.filter((c) => c.startsWith(q)).concat(SLASH_COMMANDS.filter((c) => !c.startsWith(q) && c.includes(q)));
  if (!slashMatches.length || (slashMatches.length === 1 && slashMatches[0] === q)) { closeSlash(); return; }
  slashIndex = Math.min(slashIndex, slashMatches.length - 1);
  renderSlash();
}

function renderSlash() {
  const pop = $('slash-pop');
  pop.hidden = false;
  pop.replaceChildren(...slashMatches.map((c, k) => {
    const b = h('button', { class: 'slash-item' + (k === slashIndex ? ' active' : ''), type: 'button', role: 'option' }, '/' + c);
    b.addEventListener('mousedown', (e) => { e.preventDefault(); pickSlash(c); });
    return b;
  }));
}

function pickSlash(c) {
  const ta = $('input');
  const rest = ta.value.slice(ta.selectionStart).replace(/^\S*/, '');
  ta.value = '/' + c + (rest.trim() ? ' ' + rest.replace(/^\s+/, '') : '');
  const pos = c.length + 1;
  ta.setSelectionRange(pos, pos);
  closeSlash();
  autosize();
}

function closeSlash() { $('slash-pop').hidden = true; slashMatches = []; slashIndex = 0; }

async function send() {
  const s = liveSession();
  if (!s) return;
  const ta = $('input');
  const d = currentDraft();
  const text = ta.value;
  if (!text.trim()) return;
  if (isDead(s.meta)) { toast('会话已退出，请先点击“重连”', 'warn'); return; }
  if (currentDialog(s)) { toast('请先处理上方对话框', 'warn'); return; }
  ta.value = '';
  d.text = '';
  if (d.nav) d.nav.reset();
  autosize();
  closeSlash();
  scrollToBottom(s);
  // 发送前就记成“待出现”（等粘贴确认的一两秒里按 ↑ 也能翻到）；发送失败再撤回
  const pend = sentPending.get(s.sid) || [];
  addPending(pend, text, promptsFromItems(s.items).length);
  const mine = pend[pend.length - 1];
  if (pend.length) sentPending.set(s.sid, pend);
  try {
    await withClearRetry((extra) => api.post(sp(s, '/message'), { text, ...extra }));
  } catch (e) {
    const cur = sentPending.get(s.sid);
    const k = cur && mine ? cur.indexOf(mine) : -1;
    if (k >= 0) cur.splice(k, 1);
    toast('发送失败：' + e.message, 'error');
    if (!ta.value) { ta.value = text; d.text = text; autosize(); }
  }
}

function setStaticIcons() {
  $('settings-btn').appendChild(icon('gear'));
  $('history-refresh').appendChild(icon('refresh'));
  $('send-btn').appendChild(icon('send'));
  $('stop-btn').prepend(icon('stop'));
}

boot();
