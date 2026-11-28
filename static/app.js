// 应用入口（v2：交互式 Claude Code）：网络 ↔ 渲染核心 ↔ 侧栏/头部/输入框/终端面板
import { Api, EventStream, takeLoginCode, REMOTE, CREDENTIALS } from './api.js';
import { h, icon, spinner, fmtRelTime, fmtSize, basename, truncate } from './dom.js';
import {
  createState, ensureSession, forgetSession, applyEnvelope, applyToSession, releaseHold, applyHistory, applyScreen, mountSession,
  addLocalNotice, scrollToBottom, refreshTail, currentDialog, setModelList, revealItem, flushNow,
} from './render.js';
import { toast, confirmDialog, promptDialog, jobsPopover, newSessionDialog, JOBID_RE, isLocalMeta, targetQuery } from './dialogs.js';
import { openModelModal, openEffortModal, TermPanel, effortLockText, appliedToast } from './tui.js';
import { SubagentUI } from './subagents.js';
import { StatsBar } from './stats.js';
import { apiLabel, apiUsageTitle, restartApiDialog, loadApiKeys, loadUsage, onApiKeysChange, setDefaultBaseUrl } from './apikeys.js';
import { initViz } from './viz.js';           // 可视化（§10）：图片引用 / 灯箱 / 文件 chip / 右侧预览面板
import { openLightbox, lightboxOpen } from './lightbox.js';  // 图片条缩略图点击放大
import { PreviewPanel } from './preview.js';
import { HintBar, installChords, screenHints } from './hotkeys.js';
import { settingsDialog, setUpdateStore, setExtraSection } from './settings.js';
import { modeName, cycleOf } from './modes.js';
import { ComposerControls } from './composerbar.js';   // 输入框底栏：模型/effort 胶囊、权限模式、Shift+Tab
import { AutoSize } from './autosize.js';  // 输入框自适应高度（镜像测量，不临时缩小输入框，打字不抖）
import { PromptNav, promptsFromItems, mergePending, addPending, historyDir, recallInto } from './prompthist.js';   // 输入框 ↑/↓ 历史
import { sessionStatus, rowClass, isUnread, docTitle, unreadCount } from './sessrow.js';   // 侧栏行状态（新回复 / 待处理）
import { buildLayout, compareSessions, flatten, locate, projectOf, projectKey, projectName, normPath, inProject, targetOf, CHAT_KEY,
  moveSession as layoutMoveSession, moveSessionEdge, moveSessionStep, moveProject, moveProjectEdge, moveProjectStep,
  aggregate, projectHeader, sectionHead, CollapseStore, HomeBook } from './projects.js';   // 侧栏：项目（按运行位置 + cwd 归组）与聊天
import { PromptMinimap } from './minimap.js';   // 时间线左侧 prompt 小格子（只读覆盖层）
import { ChangesButton } from './changes.js';   // 右侧「代码改动」栏的会话头开关（数据只来自前端已有的 transcript）
import { UpdateStore, UpdateBanner, updateAction } from './update.js';   // dmg 版自动更新（只有后端开启时才显示）
import { OpBanner, OpGuard, busyLabel, jobPromptModel, opRunning } from './opprogress.js';   // 重启 / 续接进度横幅 + 防连点
import { sysNotify } from './sysnotify.js';   // macOS 系统通知（只在原生窗口里有 handler；浏览器 / 手机里什么都不做）
import { ImageTray, DropOverlay, transferInfo, dragHasFiles, splitDropPaths, insertPaths, inputImages, inputHasOther,
  imageBlocked, dropHint, trayModel, lightboxList } from './attach.js';   // 本机会话发图片：⌘V = claude 的 Ctrl+V、原生窗口拖入 = 粘贴图片路径
import { Completer, CompleteCache, atSafeTail } from './complete.js';   // 输入框 / 命令与 @ 文件补全（内置按 CLI 版本 + 后端只读列出）
import { TodoPin } from './todopin.js';   // 输入框上方的置顶任务清单（只读 transcript 里的 TodoWrite / TaskCreate / TaskUpdate）
import { DraftStore } from './drafts.js';   // 输入框草稿按会话存 localStorage（退出 app 也不丢；多窗口以最后输入为准）
import { SessionFind, installFindKeys } from './finder.js';   // 会话内查找（⌘F，搜全部条目，只读）
import { ToolTiers, keepReading } from './toolview.js';      // 工具调用显示档位（全部 / 隐藏成功的 / 全部隐藏 / 只看对话）
import { QuoteStore, QuoteBar, QuotePicker, composeMessage, isCommandText } from './quote.js';   // 引用回复（选中文字 → 引用条 → 发送时 > …）
import { setWidthWrapper } from './chatwidth.js';   // 聊天宽度（设置里切换；首帧由 theme.js 写好）

const $ = (id) => document.getElementById(id);
const api = new Api({ onAuthLost: (msg) => showAuthLost(msg) });
// 手机远程（触屏）：回车 = 换行、点发送按钮发送（避免半句话被误发进 claude）；本机窗口与以前完全相同
const TOUCH_COMPOSE = REMOTE && typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;

let config = {};
// 重启 / 续接进度：防连点（同一会话进行中时不再发请求）+ 输入框上方的进度横幅（数据 = 后端 meta.op）
const opGuard = new OpGuard(() => { headerKey = ''; scheduleUI(); });
const opBanner = new OpBanner({ onAction: (id) => onOpAction(id) });
setInterval(() => {   // 计时 / "比平时慢"：每秒原地更新（只在横幅显示时）
  const s = activeSession();
  if (s && active.type === 'session' && !opBanner.el.hidden && opBanner.sid === s.sid) opBanner.update(opGuard.view(s.sid, s.meta));
}, 1000);
// 本机 tmux 的 -L 名：平时 ccr；内置模式（dmg 版，后端 --bundled）由后端在配置里给出 tmux_label（ccr-app）
const tmuxLabel = () => (config && config.tmux_label) || 'ccr';
let stream = null;
let instanceId = '';        // 服务器实例 id（bootstrap 返回），SSE 带 instance=
let epoch = 0;              // 每次（重新）加载递增；旧加载流程的异步回调据此作废
let active = null;          // {type:'session', sid} | {type:'preview', uuid, session, item}
const drafts = new Map();   // sid -> {text, nav}（nav = 输入框 ↑/↓ 历史的浏览状态，prompthist.js）
const sentPending = new Map();   // sid -> [{text, n}]：本页发出、transcript 里还没出现的消息（↑/↓ 历史用）
const draftStore = new DraftStore({
  storage: (() => { try { return window.localStorage; } catch (_) { return null; } })(),
  legacy: (() => { try { return window.sessionStorage; } catch (_) { return null; } })(),
  onTooBig: (sid, n) => toast('草稿太长（' + n + ' 字，超过 10 万字），不会保存到本机：退出 app 后会丢失', 'warn', 8000),
});
let lastInputAt = 0;   // 本窗口最后一次在输入框里打字的时刻（performance.now）
const todoPin = new TodoPin({ storage: (() => { try { return window.localStorage; } catch (_) { return null; } })() });
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
// claude 输入框里的图片（[Image #N]）：输入框顶部的图片条 + 拖动覆盖层；imgOps = sid -> 进行中的图片操作（串行）
const imageTray = new ImageTray({ onClear: () => clearImages(), onRemove: (n) => removeImage(n), onOpen: (n) => openTrayImage(n) });
const dropOverlay = new DropOverlay();
const imgOps = new Map();     // sid -> { kind:'paste'|'paths'|'clear'|'remove', n, promise }
// sid -> Map(N -> { url, name, kind:'paste'|'drop', token, path, loading, failed })：缩略图（blob URL，只在本页内存里）：
// ⌘V 的 = 浏览器 paste 事件给的图片；拖入的 = 后端只读通道 GET image/file/<token>（只读那个刚拖入的文件）
const imgThumbs = new Map();
const NATIVE_APP = typeof navigator !== 'undefined' && /CCRemoteChat\//.test(navigator.userAgent || '');
let composerCtl = null;            // 输入框底栏左侧控件组（composerbar.js）
let minimap = null;                // 时间线左侧 prompt 小格子（minimap.js）：#chat-host 里只挂一个，跟随当前会话 / 历史预览
// 会话头「改动」按钮：只建一次，每次重建会话头时挂回去，角标 / 按下状态原地更新
const chgBtn = new ChangesButton({ onClick: () => { const s = activeSession(); if (s && pvPanel) pvPanel.toggleChanges(s); } });
// 会话内查找 / 工具调用显示档位 / 引用条（finder.js、toolview.js、quote.js）：都是纯前端，只读 transcript、只改我们自己的输入框
let finder = null;
const tiers = new ToolTiers({ onChange: () => { const s = activeSession(); if (s && minimap) minimap.onFlush(s); } });
const quotes = new QuoteStore(() => scheduleUI());
const quoteBar = new QuoteBar({ onRemove: (id) => { const s = liveSession(); if (s) quotes.remove(s.sid, id); }, onClear: () => { const s = liveSession(); if (s) quotes.clear(s.sid); } });
const findBtn = h('button', { class: 'btn ghost small find-open-btn', type: 'button', title: '在会话中查找（⌘F）：搜本会话全部消息、工具调用与思考' }, icon('search'), h('span', null, '查找'));
findBtn.addEventListener('click', () => { if (finder) finder.open(); });
// 条目写进 DOM 之后：左侧小格子 + 右侧「改动」栏（增量）+ 查找高亮 / 「只看对话」标记
const minimapFlush = (s) => {
  if (minimap) minimap.onFlush(s);
  if (pvPanel) pvPanel.onSessionUpdate(s);
  if (finder) finder.onFlush(s);
  tiers.onFlush(s);
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
  // 结果以 CLI 的回报为准：原文（没有就不显示，绝不显示 null）+ 后端的提醒（effort 被环境变量锁定 / 结果未知）
  onApplied: (s, r, scope) => { const t = appliedToast(r, scope); toast(t.text, t.level, t.level === 'warn' ? 9000 : undefined); },
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
  onMeta: (s) => { updateDocTitle(); scheduleUI(); sysNotify.onMeta(s); },
  onSession: () => scheduleUI(),
  onScreen: (s) => onScreen(s),
  onModelList: (s) => { if (modelCtrl && modelCtrl.sid === s.sid && s.modelList) modelCtrl.ctrl.setFull(s.modelList); },
  onModelInfo: () => scheduleUI(),
  onSubagent: (s, aid, added, reset) => { if (saUI) saUI.onUpdate(s, aid, added, reset); if (pvPanel) pvPanel.onSessionUpdate(s); scheduleUI(); },
  onFlush: minimapFlush,
  onRemoved: (sid) => dropSession(sid),   // 会话在别的窗口被删除（后端推 app/removed）
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
  finder = new SessionFind($('chat-host'), { reveal: revealItem, flush: flushNow });
  installFindKeys(finder, { blocked: () => !!document.querySelector('dialog[open]') || lightboxOpen() });
  $('composer-box').before(quoteBar.el);   // 引用条：输入框上方
  new QuotePicker({ canQuote: () => !!liveSession() && !$('composer').hidden,
    onQuote: (q) => { const s = liveSession(); if (!s) return; quotes.add(s.sid, q); $('input').focus({ preventScroll: true }); } });
  // 聊天宽度（设置里改）：保持阅读位置；宽度变了（含别的标签页改的）小格子按新的留白重新测量
  setWidthWrapper((apply) => { const s = activeSession(); if (s && s.view) keepReading(s, apply); else apply(); });
  window.addEventListener('ccr:chatw', () => { if (minimap) minimap.measure(); });
  $('composer-stats').appendChild(statsBar.el);
  saUI = new SubagentUI({ api, toast });
  pvPanel = new PreviewPanel({ host: $('app'), onState: () => updateChgBtn() });
  initViz({ api, getConfig: () => config, toast, panel: pvPanel });
  // CLI 快捷键提示：「转到后台运行」(Ctrl+B Ctrl+B)、「立即发送排队消息」(Ctrl+X Ctrl+S)；按钮与键盘和弦共用同一组动作
  hintBar = new HintBar($('hint-bar'), { onBackground: () => hintAction('background'), onSendNow: () => hintAction('send_now') });
  $('hint-bar').parentNode.insertBefore(todoPin.el, $('hint-bar'));   // 置顶任务清单：输入框（和快捷键提示条）上方
  installChords({ getHints: chordHints, onBackground: () => hintAction('background'), onSendNow: () => hintAction('send_now') });
  term = new TermPanel($('term-panel'), {
    onKeys: (keys) => { const s = liveSession(); return s ? api.post(sp(s, '/keys'), { keys }) : Promise.resolve(); },
    onRefresh: () => refreshScreen(liveSession()),
    onClose: () => setTerm(false),
  });
  if (REMOTE) document.documentElement.classList.add('remote');   // 手机远程访问：没有窗口登录码，401 → 配对页
  if (!REMOTE) window.addEventListener('hashchange', async () => {
    const code = takeLoginCode();
    if (!code) return;
    try { await api.login(code); location.reload(); }
    catch (e) { if (e.status !== 401) toast('登录失败：' + e.message, 'error'); else showAuthLost(e.message); }
  });
  const code = REMOTE ? '' : takeLoginCode(); // 先清掉地址栏中的登录码，再去兑换
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
  initSysNotify();
  // API key 列表/用量（会话头 chip、会话列表标签用）：变化时重画
  onApiKeysChange(() => { headerKey = ''; listKey = ''; scheduleUI(); if (historyItems) renderHistoryList(); });
  loadApiKeys(api).then(() => loadUsage(api)).catch(() => {});
  await loadAll();
}

// ---------- macOS 系统通知（static/sysnotify.js，SPEC §12.1；只在原生窗口里有 handler） ----------
function initSysNotify() {
  sysNotify.init({
    activeSid: () => (active && active.type === 'session' ? active.sid : null),
    hasSession: (sid) => state.sessions.has(sid),
    foreground: () => document.visibilityState === 'visible' && document.hasFocus(),
    dialog: (s) => currentDialog(s),
    // 点了通知 = 用户要看这个会话：与点侧栏那一行相同（打开并标为已读）
    onOpen: (sid) => { const s = state.sessions.get(sid); if (!s) return; activate(sid); markSeen(s); },
  });
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
  if (b.remote || b.remote_access) setupRemote(b.remote || null);
  state.lastGseq = typeof b.gseq === 'number' ? b.gseq : 0;

  for (const meta of Array.isArray(b.sessions) ? b.sessions : []) {
    if (!meta || !meta.sid || state.removed.has(meta.sid)) continue;   // 本页已删除的（bootstrap 可能取于删除完成之前）
    const s = ensureSession(state, meta.sid, meta);
    s.hold = true; // 回放完成前暂存 SSE 事件
  }
  sysNotify.seed(state.sessions.values());   // 系统通知的基线：之后 done_at / attn_at 变大才通知
  draftStore.gc((Array.isArray(b.sessions) ? b.sessions : []).map((m) => m && m.sid));   // 别处删掉的会话：草稿也删
  updateDocTitle();

  stream = new EventStream({
    api,
    getSince: () => state.lastGseq,
    getInstance: () => instanceId,
    onEnvelope: (env) => applyEnvelope(state, env),
    onStatus: (st) => setConn(st),
    beforeReconnect,
    onReset: () => resetAll('服务已重启或部分实时事件无法回放'),
    onConfig: (c) => {
      config = c; applyConfigUI();
      // 别的窗口改了设置里的默认 base_url：更新 key 的地址显示与用量
      if (setDefaultBaseUrl(c.default_base_url || '')) loadUsage(api).catch(() => {});
    },
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

// ---------- 手机远程访问（私有版：static/remote.js；开源版没有这个文件，bootstrap 也不会带 remote / remote_access） ----------
let remoteMod = null;
function loadRemote() {
  if (!remoteMod) remoteMod = import('./remote.js').catch((e) => { remoteMod = null; throw e; });
  return remoteMod;
}
let remoteReady = false;
function setupRemote(remote) {
  if (remoteReady) return;
  remoteReady = true;
  loadRemote().then((m) => setExtraSection((a) => Object.assign(m.settingsSection(a, remote), { hideTest: !!remote })))
    .catch(() => { remoteReady = false; });
}

let authShown = false;
/** 没有 token / 任何请求 401 → 全屏提示，停止重连 */
function showAuthLost(detail) {
  if (detail && /python3 app\.py|会话已失效/.test(detail)) detail = '';
  epoch++;
  if (stream) { stream.stop(); stream = null; }
  if (REMOTE) {   // 手机：没有窗口可以重新打开 → 配对页
    setConn('auth', '需要配对');
    loadRemote().then((m) => m.showPairing(api, detail, { onPaired: () => location.reload() }),
      (e) => setConn('auth', '加载配对页失败：' + e.message));
    return;
  }
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

/** 全局顺序：手动排过的按 meta.order（小的在上）；没排过的（新会话）按创建时间、新的在最上，并排在所有排过的前面（projects.js） */
function sortedSessions() {
  return [...state.sessions.values()].filter((s) => !s.readOnly).sort(compareSessions);
}

// ====================== 侧栏：项目与聊天（SPEC §15.2，static/projects.js） ======================
// 项目 = 运行位置 + cwd，只由 meta.in_project 的会话组成；其余在「聊天」区。折叠状态 / 学到的 home 只在本页 localStorage。
const safeStorage = (() => { try { return window.localStorage; } catch (_) { return null; } })();
const projCollapse = new CollapseStore(safeStorage);
const projHomes = new HomeBook(safeStorage);
// 新建 / 移入项目 / 切换过去的会话：它所在的项目自动展开（cwd 从 ~ 变成 CLI 报告的绝对路径、换了组时再展开一次）
const revealSids = new Map();   // sid -> 截止时间（ms）
function revealSoon(sid) { revealSids.set(sid, Date.now() + 60000); }

/** 当前侧栏布局（顺手记下各会话 cwd 的变化，学这台机器的 home） */
function sidebarLayout(sessions = sortedSessions()) {
  for (const s of sessions) projHomes.observe(s.sid, targetOf(s.meta), s.meta.cwd);
  return buildLayout(sessions.map((s) => ({ sid: s.sid, meta: s.meta })), { homes: projHomes.homes });
}

/** 手动排序：ids = 从上到下的全部会话 id（各项目依次 + 聊天）；先在本地生效，再告诉后端（存进 sessions.json） */
async function applyOrder(ids) {
  if (!ids) return;
  ids.forEach((id, i) => { const x = state.sessions.get(id); if (x) x.meta = { ...x.meta, order: i }; });
  renderSessionList();
  try { await api.post('/api/sessions/order', { sids: ids }); } catch (e) { toast('保存排序失败：' + e.message, 'error'); }
}
/** 会话只在所在的项目（或聊天区）内移动；跨组 = null（不调整） */
function moveSession(sid, targetSid, after) { applyOrder(layoutMoveSession(sidebarLayout(), sid, targetSid, after)); }
function moveSessionTo(sid, where) { applyOrder(moveSessionEdge(sidebarLayout(), sid, where)); }
function moveSessionBy(sid, delta) { applyOrder(moveSessionStep(sidebarLayout(), sid, delta)); }
function moveProjectTo(key, where) { applyOrder(moveProjectEdge(sidebarLayout(), key, where)); }
function moveProjectBy(key, delta) { applyOrder(moveProjectStep(sidebarLayout(), key, delta)); }

/** 移入 / 移出项目（只改显示归属；乐观更新，以服务器回推的 meta 为准，失败回滚） */
async function setInProject(s, on) {
  const prev = s.meta.in_project;
  s.meta = { ...s.meta, in_project: on };
  if (on) revealSoon(s.sid);
  listKey = '';
  scheduleUI();
  try {
    await api.post(sp(s, '/project'), { in: on });
  } catch (e) {
    if (state.sessions.get(s.sid) === s && s.meta.in_project === on) { s.meta = { ...s.meta, in_project: prev }; listKey = ''; scheduleUI(); }
    toast((on ? '移入项目' : '移出项目') + '失败：' + e.message, 'error');
  }
}

/** 右键菜单 / 长按菜单（手机远程的 mobile.js 把长按转成 contextmenu） */
function popMenu(label, items, x, y) {
  document.querySelectorAll('.popover.sess-menu').forEach((p) => p.remove());
  const pop = h('div', { class: 'popover sess-menu', role: 'menu', 'aria-label': label });
  const dismiss = (e) => {
    if (e && e.type === 'mousedown' && pop.contains(e.target)) return;
    if (e && e.type === 'keydown' && e.key !== 'Escape') return;
    pop.remove();
    document.removeEventListener('mousedown', dismiss, true);
    document.removeEventListener('keydown', dismiss, true);
  };
  for (const it of items) {
    if (!it) continue;
    const b = h('button', { class: 'menu-item', type: 'button', role: 'menuitem', title: it.title || null }, icon(it.icon), h('span', { class: 'menu-label' }, it.label));
    b.addEventListener('click', () => { dismiss(); it.fn(); });
    pop.appendChild(b);
  }
  document.body.appendChild(pop);
  const r = pop.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + 'px';
  pop.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
  setTimeout(() => { document.addEventListener('mousedown', dismiss, true); document.addEventListener('keydown', dismiss, true); }, 0);
}
function sessionMenu(s, x, y) {
  // 新回复：未读 →「标为已读」；有过完成记录、当前已读 →「标为未读」
  const unread = isUnread(s.meta, s.localSeen);
  const seenItem = unread ? { label: '标为已读', icon: 'check', fn: () => markSeen(s) }
    : typeof s.meta.done_at === 'number' ? { label: '标为未读', icon: 'bell', fn: () => markUnseen(s) } : null;
  const inProj = inProject(s.meta);
  const pk = projectKey(s.meta, projHomes.homes);
  const g = projectOf(sidebarLayout(), pk);
  const name = g ? g.name : projectName(pk.includes(':') ? pk.slice(pk.indexOf(':') + 1) : '', projHomes.homes[targetOf(s.meta)]);
  popMenu('会话操作', [
    seenItem,
    { label: '移到最上', icon: 'up', fn: () => moveSessionTo(s.sid, 'top') },
    { label: '移到最下', icon: 'down', fn: () => moveSessionTo(s.sid, 'bottom') },
    inProj ? { label: '移出项目', icon: 'chat', title: '移到侧栏的「聊天」里（只改显示，不影响 claude）', fn: () => setInProject(s, false) }
      : { label: '移入项目「' + truncate(name, 16) + '」', icon: 'folder', title: '归入' + (targetOf(s.meta) === 'local' ? '本机' : '服务器') + '上 ' + (s.meta.cwd || '') + ' 这个项目（只改显示，不影响 claude）', fn: () => setInProject(s, true) },
    { label: '删除会话…', icon: 'trash', fn: () => doDelete(s) },
  ], x, y);
}
function projectMenu(g, x, y) {
  const collapsed = projCollapse.has(g.key);
  popMenu('项目操作', [
    { label: '新建会话', icon: 'plus', fn: () => newInProject(g) },
    { label: collapsed ? '展开' : '折叠', icon: collapsed ? 'chevdown' : 'chevron', fn: () => toggleProject(g.key) },
    { label: '移到最上', icon: 'up', fn: () => moveProjectTo(g.key, 'top') },
    { label: '移到最下', icon: 'down', fn: () => moveProjectTo(g.key, 'bottom') },
  ], x, y);
}
function toggleProject(key) {
  projCollapse.toggle(key);
  listKey = '';
  renderSessionList();
}
/** 项目标题行的「+」：预填该项目的运行位置、目录（服务器 = 当前作业，没设置时用组里最新会话的作业），放进项目 */
function newInProject(g) {
  if (!g.target) { openNewSession({ project: true, heading: '新建项目' }); return; }
  const local = g.target === 'local';
  openNewSession({
    target: g.target, cwd: g.path, jobid: local ? 'local' : (config.jobid || g.jobid || ''), project: true,
    project_name: g.name, heading: '在项目「' + truncate(g.name, 24) + '」中新建会话',
  });
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
  const lk = s.meta.effort_locked;
  if (lk && lk.level) return lk.level;   // 被环境变量锁定：CLI 实际用的档位
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
  sysNotify.clear(sid);   // 通知中心里这个会话的通知已经没用了
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
  if (finder) finder.setSession(s && s.view ? s : null);
  tiers.setSession(s && s.view ? s : null);
}

/** 会话头「改动」按钮：本会话改动文件数 + 是否正在显示（只读：数据来自前端已有的 transcript，不发请求） */
function updateChgBtn() {
  const s = activeSession();
  if (!s || !pvPanel) { chgBtn.update(0, false); return; }
  chgBtn.update(pvPanel.changeCount(s), pvPanel.isShowingChanges() && pvPanel.s === (s.parent || s));
}

let listKey = '';
// 侧栏拖动：{kind:'sess', sid, sec（所在项目的 key 或 CHAT_KEY）} | {kind:'proj', key}。会话只能在本组内移动；项目整体移动
let drag = null;
function clearDropMarks() {
  for (const el of $('session-list').querySelectorAll('.drop-before, .drop-after, .drop-deny')) el.classList.remove('drop-before', 'drop-after', 'drop-deny');
}
function markDrop(el, cls) {
  if (!el || el.classList.contains(cls)) return;
  clearDropMarks();
  el.classList.add(cls);
}
/** 不接受这次放下（不 preventDefault → 浏览器显示禁止光标、不会触发 drop）+ 目标组画虚线框与「不能放这里」 */
function denyDrop(e, block) {
  e.dataTransfer.dropEffect = 'none';
  markDrop(block, 'drop-deny');
}

/** 聊天区会话行里的目录名：文件夹名；home 显示 "~"（知道这台机器的 home 时） */
function cwdLabel(m) {
  const hm = projHomes.homes[targetOf(m)];
  return projectName(normPath(m.cwd, hm), hm) || basename(m.cwd);
}

/** 侧栏一行会话。inProj = 在项目里（标题行已经写了运行位置与目录，行里就不再重复） */
function sessionRow(s, st, stText, sec) {
  const m = s.meta;
  const inProj = sec !== CHAT_KEY;
  const del = h('button', { class: 'icon-btn row-act', type: 'button', title: '删除会话' }, icon('trash'));
  del.addEventListener('click', (e) => { e.stopPropagation(); doDelete(s); });
  const toBottom = h('button', { class: 'icon-btn row-act row-act-2', type: 'button', title: inProj ? '移到项目最下（暂时不用的会话放到底部）' : '移到最下（暂时不用的会话放到底部）' }, icon('down'));
  toBottom.addEventListener('click', (e) => { e.stopPropagation(); moveSessionTo(s.sid, 'bottom'); });
  const row = h('div', { class: rowClass(st, isActive(s)), role: 'listitem', tabindex: '0', draggable: 'true', 'data-sid': s.sid,
    'data-fk': 's:' + s.sid, 'data-nav': '1',
    title: (m.cwd || '') + '\n拖动可' + (inProj ? '在项目内' : '在「聊天」里') + '调整顺序；右键：移到最上 / 最下、' + (inProj ? '移出项目' : '移入项目') + '；Alt+↑/↓ 上下移动' },
    h('span', { class: 'dot st-' + st, title: stText }),
    h('div', { class: 'sess-main' },
      h('div', { class: 'sess-title' }, sessionTitle(s)),
      h('div', { class: 'sess-sub' },
        isLocalMeta(m) ? (inProj ? null : h('span', { class: 'loc-tag', title: '在本机运行' }, icon('laptop'), '本机'))
          : m.jobid ? h('span', { class: 'mono' }, 'job ' + m.jobid) : null,
        m.cwd && !inProj ? h('span', { class: 'mono', title: m.cwd }, cwdLabel(m)) : null,
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
    e.stopPropagation();   // 不要让外层的项目当成拖整个项目
    drag = { kind: 'sess', sid: s.sid, sec };
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', s.sid); } catch (_) { /* 忽略 */ }
    row.classList.add('dragging');
  });
  row.addEventListener('dragend', () => { drag = null; row.classList.remove('dragging'); clearDropMarks(); });
  row.addEventListener('dragover', (e) => {
    if (!drag || drag.kind !== 'sess') return;   // 拖项目：交给外层项目块（冒泡）
    e.stopPropagation();
    if (drag.sec !== sec) { denyDrop(e, row.closest('.proj, .chat-sec')); return; }   // 跨项目 / 项目↔聊天：不接受
    if (drag.sid === s.sid) { clearDropMarks(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const r = row.getBoundingClientRect();
    markDrop(row, e.clientY > r.top + r.height / 2 ? 'drop-after' : 'drop-before');
  });
  row.addEventListener('drop', (e) => {
    if (!drag || drag.kind !== 'sess') return;
    e.preventDefault();
    e.stopPropagation();
    const after = row.classList.contains('drop-after');
    const from = drag.sid;
    clearDropMarks();
    if (drag.sec === sec) moveSession(from, s.sid, after);
  });
  return row;
}

/** 一个项目：标题行（可拖动整组、点击折叠 / 展开、「+」）+ 组内会话 */
function projectBlock(g, idx, statuses, rows) {
  const collapsed = projCollapse.has(g.key);
  const hasActive = g.sids.some((sid) => active && active.type === 'session' && active.sid === sid);
  const model = { ...g, n: g.sids.length, agg: aggregate(g.sids.map((sid) => statuses.get(sid))), collapsed, active: hasActive, bodyId: 'proj-body-' + idx };
  const hd = projectHeader(model, { onToggle: () => toggleProject(g.key), onAdd: () => newInProject(g) });
  const body = h('div', { class: 'proj-body', id: model.bodyId, role: 'list', hidden: collapsed }, rows);
  const block = h('div', { class: 'proj', role: 'listitem', 'data-pkey': g.key }, hd, body);
  const toggle = hd.querySelector('.proj-toggle');
  toggle.addEventListener('keydown', (e) => {
    if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); moveProjectBy(g.key, e.key === 'ArrowUp' ? -1 : 1); }
  });
  hd.addEventListener('contextmenu', (e) => { e.preventDefault(); projectMenu(g, e.clientX, e.clientY); });
  hd.addEventListener('dragstart', (e) => {
    drag = { kind: 'proj', key: g.key };
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', g.key); } catch (_) { /* 忽略 */ }
    block.classList.add('dragging');
  });
  hd.addEventListener('dragend', () => { drag = null; block.classList.remove('dragging'); clearDropMarks(); });
  block.addEventListener('dragover', (e) => {
    if (!drag) return;
    if (drag.kind === 'sess') {   // 拖会话经过标题行：本组 = 放到最上面；别的组 = 不接受
      if (drag.sec !== g.key) { denyDrop(e, block); return; }
      const first = body.querySelector('.sess');
      if (!e.target.closest('.proj-hd') || !first || collapsed) { clearDropMarks(); return; }
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      markDrop(first, 'drop-before');
      return;
    }
    if (drag.key === g.key) { clearDropMarks(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const r = block.getBoundingClientRect();
    markDrop(block, e.clientY > r.top + r.height / 2 ? 'drop-after' : 'drop-before');
  });
  block.addEventListener('drop', (e) => {
    if (!drag) return;
    e.preventDefault();
    const d = drag;
    const after = block.classList.contains('drop-after');
    const first = body.querySelector('.sess.drop-before');
    clearDropMarks();
    if (d.kind === 'sess') { if (d.sec === g.key && first && first.dataset.sid) moveSession(d.sid, first.dataset.sid, false); }
    else if (d.key !== g.key) applyOrder(moveProject(sidebarLayout(), d.key, g.key, after));
  });
  return block;
}

function renderSessionList() {
  const list = $('session-list');
  const sessions = sortedSessions();
  const layout = sidebarLayout(sessions);
  // 新建 / 移入项目 / 切换过去的会话：所在项目自动展开（cwd 定成绝对路径、不会再换组后停止）
  const now = Date.now();
  for (const [sid, until] of revealSids) {
    const s = state.sessions.get(sid);
    const loc = s ? locate(layout, sid) : null;
    if (!s || now > until) { revealSids.delete(sid); continue; }
    if (loc && loc.key !== CHAT_KEY) projCollapse.set(loc.key, false);
    if (typeof s.meta.cwd === 'string' && s.meta.cwd.startsWith('/')) revealSids.delete(sid);
  }
  const statuses = new Map(sessions.map((s) => [s.sid, statusOf(s)]));
  const key = JSON.stringify([layout.projects.map((g) => [g.key, g.name, g.sids, projCollapse.has(g.key)]), layout.chats,
    sessions.map((s) => [s.sid, sessionTitle(s), s.meta.jobid, s.meta.cwd, statuses.get(s.sid), isActive(s), runningSubagents(s), apiLabel(s.meta, { short: true })])]);
  if (key === listKey) return;
  listKey = key;
  // 整个列表重建：记下焦点在哪一项（会话行 / 项目标题 / 「+」），重建后放回去
  const ae = document.activeElement;
  const fk = ae && list.contains(ae) ? (ae.closest('[data-fk]') || {}).dataset?.fk : null;
  const byId = new Map(sessions.map((s) => [s.sid, s]));
  const row = (sid, sec) => { const s = byId.get(sid); const [st, stText] = statuses.get(sid); return sessionRow(s, st, stText, sec); };
  const out = [sectionHead('项目', {
    addTitle: '新建项目：选运行位置和文件夹，项目随第一个会话一起出现（项目 = 运行位置 + 工作目录）', addLabel: '新建项目', fk: 'sec:proj',
    onAdd: () => openNewSession({ project: true, heading: '新建项目' }),
  })];
  if (!sessions.length) out.push(h('div', { class: 'side-empty' }, '还没有会话。点上方「新建聊天」开始；或点「项目」旁的 + 选一个文件夹新建项目。'));
  else if (!layout.projects.length) out.push(h('div', { class: 'side-hint' }, '还没有项目：点 + 选一个文件夹；聊天也可以在右键菜单里移入项目'));
  layout.projects.forEach((g, i) => out.push(projectBlock(g, i, statuses, g.sids.map((sid) => row(sid, g.key)))));
  if (layout.chats.length) {
    const sec = h('div', { class: 'chat-sec', role: 'listitem' },
      sectionHead('聊天', { count: layout.chats.length, addTitle: '新建聊天（不放进项目）', addLabel: '新建聊天', fk: 'sec:chat', onAdd: () => openNewSession({ heading: '新建聊天' }) }),
      h('div', { class: 'chat-body', role: 'list' }, layout.chats.map((sid) => row(sid, CHAT_KEY))));
    sec.addEventListener('dragover', (e) => { if (drag && (drag.kind === 'proj' || drag.sec !== CHAT_KEY)) denyDrop(e, sec); });
    out.push(sec);
  }
  list.replaceChildren(...out);
  if (fk) {
    const el = [...list.querySelectorAll('[data-fk]')].find((x) => x.dataset.fk === fk);
    if (el && el.offsetParent !== null) el.focus({ preventScroll: true });
  }
}
/** 让新建 / 切换过去的会话在侧栏里看得见（所在项目已由 revealSids 展开） */
function revealRow(sid) {
  requestAnimationFrame(() => {
    const el = $('session-list').querySelector('.sess[data-sid="' + CSS.escape(sid) + '"]');
    if (el && el.offsetParent !== null) el.scrollIntoView({ block: 'nearest' });
  });
}

let headerKey = '';
let usageAskedAt = 0;   // API chip 悬停时最多每分钟拉一次用量
function renderHeader() {
  const hd = $('chat-header');
  if (hd.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return; // 正在改名
  const s = activeSession();
  const key = !s ? 'none' : active.type === 'preview' ? 'p:' + active.uuid + ':' + (active.item.cwd || '')
    : JSON.stringify([s.sid, sessionTitle(s), statusOf(s), s.meta.jobid, s.meta.cwd, s.meta.error, s.meta.link,
      isBusy(s), isDead(s.meta), termOpen, opGuard.busyKind(s.sid, s.meta), apiLabel(s.meta), (screenHints(s) || {}).shells || 0]);
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
      h('div', { class: 'hd-actions' }, findBtn, tiers.el, chgBtn.el, cont));
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
  // 重启 / 续接 / 启动进行中（本地请求未返回或后端 meta.op 在进行）：同类按钮全部禁用，重启按钮转圈并写明在做什么
  const opKind = opGuard.busyKind(s.sid, m);
  // 运行位置：本机（不可改）/ 作业 N（点击 = 换作业 --resume 续接）
  let jobBadge;
  if (local) {
    jobBadge = h('span', { class: 'chip job-badge local', title: `在本机运行：这台 Mac 的私有 tmux（-L ${tmuxLabel()}）里的交互式 claude` }, icon('laptop'), '本机');
  } else {
    jobBadge = h('button', { class: 'chip job-badge mono', type: 'button', disabled: !!opKind, title: '点击修改 jobid（在新作业上 --resume 续接本会话）' }, icon('terminal'), 'job ' + (m.jobid || '—'));
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
  const apiChip = h('button', { class: 'chip chip-btn api-chip' + (m.api_key_missing ? ' missing' : ''), type: 'button', disabled: !!opKind, title: apiTitle() },
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
  actionsEl.push(findBtn, tiers.el, chgBtn.el);
  const termBtn = h('button', { class: 'btn small' + (termOpen ? ' primary' : ' ghost'), type: 'button', title: '显示/隐藏终端面板（屏幕原文 + 键盘）' }, icon('terminal'), h('span', null, '终端'));
  termBtn.addEventListener('click', () => setTerm(!termOpen));
  actionsEl.push(termBtn);
  const ended = jobEnded(m);
  const re = h('button', { class: 'btn ghost small restart-btn' + (opKind ? ' is-busy' : ''), type: 'button', disabled: !!opKind, 'aria-busy': opKind ? 'true' : 'false',
    title: opKind ? busyLabel(opKind) + '进度见输入框上方' : ended ? '作业 ' + (m.jobid || '') + ' 已结束：选择新的作业，以 --resume 续接本会话' : dead ? (local ? '在本机 --resume 重新连接' : '在当前作业上 --resume 重新连接') : '关闭并以 --resume 重启 claude' },
  opKind ? spinner() : icon('refresh'), h('span', null, opKind ? busyLabel(opKind) : ended ? '换作业续接' : dead ? '重连' : '重启'));
  re.addEventListener('click', () => doRestart(s));
  actionsEl.push(re);
  if (!dead) {
    const close = h('button', { class: 'btn ghost small', type: 'button', disabled: !!opKind, title: '在终端里输入 /exit 结束 claude（保留在列表中）' }, icon('power'), h('span', null, '关闭'));
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
    sid: s.sid, model: modelLabel(s), effort: effortLabel(s), why, effortLock: effortLockText(m),
    mode: currentMode(s), cycle: cycleOf(m), modeBlocked,
    onModel: () => openModelFor(s, true),
    onEffort: () => openEffortModal({ s, actions, current: (m.effort_locked && m.effort_locked.level) || s.modelInfo.effort || (s.meta.launch && s.meta.launch.effort) || '' }),
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
  if (!s || active.type !== 'session') { comp.hidden = true; todoPin.update(null); return; }
  comp.hidden = false;
  todoPin.update(s);
  const m = s.meta;
  const dead = isDead(m);
  const d = currentDialog(s);
  const busy = isBusy(s);
  const banner = $('composer-banner');
  // 重启 / 续接 / 启动进度：显示时替换下面这些提示条（"在新作业上续接""正在启动 claude…""会话出错"…）
  if (!opBanner.el.isConnected) banner.parentNode.insertBefore(opBanner.el, banner);
  const om = opGuard.view(s.sid, m);
  opBanner.sid = s.sid;
  opBanner.update(om);
  if (om) {
    setBanner(banner, '', null);
  } else if (dead && jobEnded(m)) {
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
    : TOUCH_COMPOSE ? '给 Claude 发消息（/ 命令 · @ 文件）' : '给 Claude 发消息（Enter 发送 · Shift+Enter 换行 · / 命令 · @ 文件）';
  // 先改占位文字再测高：空输入框的高度按占位文字量（AutoSize 连同 placeholder 一起量）
  const phChanged = ta.placeholder !== ph;
  if (phChanged) ta.placeholder = ph;
  const disChanged = ta.disabled !== blocked;
  if (disChanged) ta.disabled = blocked;
  if (phChanged || disChanged) autosize();
  $('send-btn').disabled = blocked;
  $('stop-btn').hidden = !(busy && !dead);
  if (hintBar) hintBar.update(dead ? null : screenHints(s), { blocked: dead || !!d || m.link === 'down' });
  // claude 输入框里的图片（⌘V / 拖入后出现的 [Image #N]）：输入框顶部的图片条，内容不变不重建
  if (!imageTray.el.isConnected) $('composer-box').prepend(imageTray.el);
  imageTray.update(trayFor(s));
  quoteBar.update(quotes.list(s.sid), s.absCwd || m.cwd || '');   // 引用条（内容不变不重建）
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
  if (opGuard.busy(s.sid, s.meta)) { toastBusy(s); return; }
  // 作业已结束：预填设置里的当前作业（与"从历史续接"一致）；以前预填已结束的旧作业，直接点下一步会被静默忽略
  const pm = jobPromptModel(s.meta, config.jobid, { ended: jobEnded(s.meta), dead: isDead(s.meta) });
  const j = await promptDialog(pm.title, {
    label: pm.label, value: pm.value, placeholder: pm.placeholder, validate: pm.validate, okText: pm.okText,
    extra: (input) => {
      const b = h('button', { class: 'btn ghost', type: 'button' }, '我的作业');
      b.addEventListener('click', () => jobsPopover(api, b, (id) => { input.value = id; }));
      return b;
    },
  });
  if (!j || j === s.meta.jobid) return;
  if (pm.confirm) {
    const ok = await confirmDialog('确认切换作业',
      `将在当前终端里退出 claude${isBusy(s) ? '（正在运行的回复会被中断）' : ''}，并在作业 ${j} 上以 --resume 续接此会话。`, { okText: '切换并续接' });
    if (!ok) return;
  }
  await restartOn(s, j, apiKeyId);
}


function toastBusy(s) {
  toast(busyLabel(opGuard.busyKind(s.sid, s.meta) || 'restart') + '进度见输入框上方，请稍候');
}

function onOpAction(id) {
  const s = activeSession();
  if (!s || active.type !== 'session') return;
  if (id === 'dismiss') { opGuard.dismiss(s.sid, s.meta); return; }
  if (id === 'term') { setTerm(true); return; }
  if (id === 'newjob') { opGuard.dismiss(s.sid, s.meta); changeSessionJob(s); return; }
  if (id === 'retry') {
    const q = opGuard.last.get(s.sid) || {};
    opGuard.dismiss(s.sid, s.meta);
    restartOn(s, q.jobid || null, q.apiKeyId, q.mode);
  }
}

/** apiKeyId：undefined = 沿用当前 API；null = 服务器默认；字符串 = 改用该 key（claude 只在启动时读 key） */
/** mode：覆盖权限启动参数（模式选择栏里"需重启"的模式）：bypassPermissions = 跳过权限，其它 = --permission-mode */
async function restartOn(s, jobid, apiKeyId, mode) {
  if (opGuard.busy(s.sid, s.meta)) { toastBusy(s); return; }
  const body = {};
  if (jobid) body.jobid = jobid;
  if (apiKeyId !== undefined) body.api_key_id = apiKeyId;
  if (mode) body.mode = mode;
  const switching = apiKeyId !== undefined && (apiKeyId || '') !== ((s.meta.launch && s.meta.launch.api_key_id) || '');
  const kind = jobid && jobid !== s.meta.jobid ? 'resume_new_job' : mode ? 'mode' : switching ? 'switch_api' : isDead(s.meta) ? 'reconnect' : 'restart';
  try {
    // 点下去立刻进入"进行中"（本地 pending），后端的阶段进度经 SSE 到达后接着显示；HTTP 在停止旧 claude 之后才返回
    const out = await opGuard.run(s.sid, kind, s.meta, () => withForceRetry((force) => api.post(sp(s, '/restart'), force ? { ...body, force: true } : body),
      '无法关闭旧会话', jobid ? '在新作业上续接' : '重启', isLocalMeta(s.meta)), { jobid, apiKeyId, mode });
    if (out.skipped) { toastBusy(s); return; }
    const r = out.result;
    if (r && r.sid) {
      const { op_reused: reused, ...meta } = r;
      s.meta = { ...s.meta, ...meta };
      if (reused) toast('已经在进行中，进度见输入框上方');   // 另一个窗口 / 连点：后端没有再执行一次
    }
  } catch (e) {
    if (e && e.status === 409 && e.code === 'job_ended') {
      toast(e.message, 'warn');
      changeSessionJob(s, apiKeyId);   // 旧作业已结束：直接让用户选新作业
      return;
    }
    if (e && e.status === 409 && e.code === 'api_key_missing') {
      toast(e.message, 'warn');
      switchApi(s, jobid, true);       // 该会话用的 key 已被删除：让用户改选
      return;
    }
    if (e && e.status === 409 && e.code === 'target_mismatch') { toast(e.message, 'warn'); return; }
    if (e && e.message !== '已取消') opGuard.fail(s.sid, kind, e.message, e.code);   // 横幅里显示原因 + 重试（不再弹错误）
  } finally {
    headerKey = '';
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
  if (opGuard.busy(s.sid, s.meta)) { toastBusy(s); return; }
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
  if (opGuard.busy(s.sid, s.meta)) { toastBusy(s); return; }    // 防连点：进行中不再弹对话框、不再发请求
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
  if (opGuard.busy(s.sid, s.meta)) { toastBusy(s); return; }
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
  forgetSession(state, sid);   // 记住已删除：之后迟到的该 sid 事件（exited meta、子代理…）不会把它加回侧栏
  drafts.delete(sid);
  draftStore.remove(sid);       // 会话删除：本机保存的草稿一起删
  completeCache.drop(sid);
  sentPending.delete(sid);
  projHomes.forget(sid);
  revealSids.delete(sid);
  updateDocTitle();
  if (active && active.type === 'session' && active.sid === sid) {
    const next = flatten(sidebarLayout())[0];   // 侧栏里最上面那个（各项目依次 + 聊天）
    if (next) activate(next); else showEmpty();
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
  if (inProject(s.meta)) revealSoon(s.sid);   // 所在项目自动展开
  activate(s.sid);
  revealRow(s.sid);
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
  $('new-session-btn').addEventListener('click', () => openNewSession({ heading: '新建聊天' }));   // 不放进项目（SPEC §15.2）
  // 侧栏：↑/↓ 在看得见的会话行与项目标题之间移动焦点（折叠的项目里的会话自然跳过）；Alt+↑/↓ 仍是调整顺序
  const sl = $('session-list');
  sl.addEventListener('keydown', (e) => {
    if ((e.key !== 'ArrowUp' && e.key !== 'ArrowDown') || e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
    const cur = e.target.closest && e.target.closest('[data-nav]');
    if (!cur) return;
    const all = [...sl.querySelectorAll('[data-nav]')].filter((el) => el.offsetParent !== null);
    const nx = all[all.indexOf(cur) + (e.key === 'ArrowUp' ? -1 : 1)];
    if (nx) { e.preventDefault(); nx.focus(); }
  });
  // 拖出侧栏：去掉插入线 / 不能放的提示（dragend 也会清）
  sl.addEventListener('dragleave', (e) => { if (!e.relatedTarget || !sl.contains(e.relatedTarget)) clearDropMarks(); });
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
    if (await confirmDialog('会话已在运行', `该 claude 会话已在本地会话“${sessionTitle(running)}”中运行（同一会话只能有一个进程）。切换过去吗？`, { okText: '切换过去' })) {
      revealSoon(running.sid);
      activate(running.sid);
      revealRow(running.sid);
    }
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

let composing = false;
let compositionEndAt = 0;
let sendWaitImg = false;   // 发送正在等图片操作结束

// / 命令与 @ 文件补全（complete.js）：内置命令按会话的 CLI 版本（slashcmds.js），自定义命令 / skills / 插件命令与 cwd 下的
// 文件由后端只读列出（GET …/complete/commands|files），按会话 + cwd 缓存；弹层挂在 #slash-pop
const completeCache = new CompleteCache((sess, kind) => api.get(api.sessionPath(sess.sid, '/complete/' + kind)),
  { onChange: () => { if (completer) completer.refresh(); } });
let completer = null;
function completeSession() {
  const s = liveSession();
  if (!s) return null;
  return { sid: s.sid, cwd: (s.meta && s.meta.cwd) || '', version: s.cliVersion || '', local: isLocalMeta(s.meta) };
}
function updateSlash() { if (completer) completer.update(); }
function closeSlash() { if (completer) completer.close(); }

function currentDraft() {
  const s = activeSession();
  if (!s || active.type !== 'session') return null;
  let d = drafts.get(s.sid);
  if (!d) {
    const st = draftStore.load(s.sid);   // localStorage（退出 app / 重启后端 / ⌘R 后恢复）
    d = { sid: s.sid, text: st ? st.t : '', at: st ? st.at : 0 };
    drafts.set(s.sid, d);
  }
  return d;
}

/** 输入框内容 → 当前会话的草稿（内容变了才记，400ms 后写进 localStorage） */
function saveDraft() {
  const d = currentDraft();
  if (d && d.text !== $('input').value) noteDraft(d, $('input').value);
}
function noteDraft(d, text) {
  d.text = text;
  d.at = Date.now();
  draftStore.queue(d.sid, text, d.at);
}

/** 离开页面 / 隐藏（⌘R、退出 app、原生窗口在后端重启后重新载入）：没写出去的草稿立即写进 localStorage */
function persistDrafts() {
  try { saveDraft(); } catch (_) { /* 忽略 */ }
  draftStore.flush();
}
window.addEventListener('pagehide', persistDrafts);
window.addEventListener('beforeunload', persistDrafts);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') persistDrafts(); });

/** 别的窗口（同一会话）写了更新的草稿：本窗口没有没写出去的输入、也不在打字时才换上（以最后输入为准，不丢字） */
function adoptDraft(sid, entry) {
  const d = drafts.get(sid);
  if (!d) return;   // 本窗口还没打开过它：下次打开时从 localStorage 读
  const text = entry ? entry.t : '';
  const at = entry ? entry.at : Date.now();
  if (draftStore.isPending(sid) || d.text === text || at < d.at) return;
  const ta = $('input');
  const isCur = active && active.type === 'session' && active.sid === sid;
  if (isCur && (composing || (document.activeElement === ta && performance.now() - lastInputAt < 1500))) return;
  d.text = text;
  d.at = at;
  if (d.nav) d.nav.reset();
  if (isCur) {
    const pos = Math.min(ta.selectionStart, text.length);
    ta.value = text;
    if (document.activeElement === ta) ta.setSelectionRange(pos, pos);
    autosize();
    closeSlash();
  }
}
draftStore.onExternal(adoptDraft);

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
  completer = new Completer({ ta, pop: $('slash-pop'), cache: completeCache, session: completeSession,
    composing: () => composing, onApply: () => { saveDraft(); autosize(); } });
  ta.addEventListener('input', () => { lastInputAt = performance.now(); autosize(); saveDraft(); updateSlash(); });
  ta.addEventListener('keydown', onComposerKey);
  ta.addEventListener('compositionend', () => setTimeout(updateSlash, 0));
  ta.addEventListener('click', updateSlash);   // 光标移到别的词上
  ta.addEventListener('keyup', (e) => { if (/^(ArrowLeft|ArrowRight|Home|End)$/.test(e.key)) updateSlash(); });
  ta.addEventListener('blur', () => {
    setTimeout(closeSlash, 150);
    const d = currentDraft();   // 离开输入框：别的窗口刚才写了更新的草稿就换上
    if (d) adoptDraft(d.sid, draftStore.load(d.sid));
  });
  ta.addEventListener('paste', onComposerPaste);   // ⌘V 里有图片 → claude 自己的 Ctrl+V（只拦图片，文字照常粘贴）
  bindImageDrop();
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
  if (completer && completer.isOpen && !isImeEnter(e) && completer.onKey(e)) return;   // 补全列表打开时 ↑/↓/Tab/Enter/Esc 归它
  const dir = historyDir(e, composing);
  if (dir && recallPrompt(dir)) { e.preventDefault(); return; }
  if (e.key === 'Enter' && !e.shiftKey && !e.altKey) {
    if (isImeEnter(e) || TOUCH_COMPOSE) return;
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
  noteDraft(d, text);
  autosize();
  closeSlash();
  ta.scrollTop = dir < 0 ? 0 : ta.scrollHeight;   // 内容超过最大高度时让光标所在的一端可见
  return true;
}


// ====================== 图片（SPEC §4.2「图片」）：claude 输入框里的 [Image #N] ======================

function screenState(s) { return s && s.screen && !s.screen.stale ? s.screen.state : null; }

/** attach.js 的 imageBlocked 用的会话情况 */
function imageSess(s) {
  if (!s) return null;
  const m = s.meta;
  return { local: isLocalMeta(m), dead: isDead(m), dialog: !!currentDialog(s), linkDown: m.link === 'down', remote: REMOTE };
}

/** 图片操作串行执行（同一会话一次一个）；进行中时图片条显示转圈，发送会先等它结束。
 *  结束后马上取一帧最新屏幕（推送的屏幕帧有节流，图片条不闪回旧状态） */
function runImageOp(s, kind, n, fn) {
  const prev = imgOps.get(s.sid);
  const op = { kind, n, promise: null };
  const fresh = () => refreshScreen(s).catch(() => {});
  op.promise = (prev ? prev.promise.catch(() => {}) : Promise.resolve()).then(fn).then(async (r) => { await fresh(); return r; },
    async (e) => { await fresh(); throw e; }).finally(() => {
    if (imgOps.get(s.sid) === op) imgOps.delete(s.sid);
    scheduleUI();
  });
  imgOps.set(s.sid, op);
  scheduleUI();
  return op.promise;
}

/** 输入框 ⌘V：剪贴板里有图片 → 让本机 claude 自己读剪贴板（终端里的 Ctrl+V）；文字照常粘进输入框 */
function onComposerPaste(e) {
  const s = liveSession();
  if (!s) return;
  const info = transferInfo(e.clipboardData);
  if (!info.images) return;            // 纯文字：浏览器默认行为，什么都不改
  if (!info.text) e.preventDefault();  // 只有图片：不往 textarea 里塞任何东西
  const why = imageBlocked(imageSess(s), 'paste');
  if (why) { toast(why, 'warn'); return; }
  const url = info.file && typeof URL !== 'undefined' && URL.createObjectURL ? URL.createObjectURL(info.file) : '';
  runImageOp(s, 'paste', 1, () => api.post(sp(s, '/image/paste'))).then((r) => {
    const added = (r && Array.isArray(r.added)) ? r.added : [];
    if (added.length && url) thumbsOf(s).set(added[added.length - 1], { url, name: '剪贴板里的图片', kind: 'paste', at: Date.now() });
    else if (url) URL.revokeObjectURL(url);
    scheduleUI();
  }, (err) => {
    if (url) URL.revokeObjectURL(url);
    toast('粘贴图片失败：' + err.message, 'error', 8000);
  });
}

function thumbsOf(s) {
  let m = imgThumbs.get(s.sid);
  if (!m) { m = new Map(); imgThumbs.set(s.sid, m); }
  return m;
}

/** 屏幕上已经没有的图片编号（发出去了 / 清掉了 / claude 重启了）：释放缩略图。
 *  刚加进来、推送的屏幕还没刷到的（后端已确认、SSE 的屏幕帧慢一点）先留着：在屏幕上出现过之后才按"没了"处理，
 *  10 秒都没出现也释放。 */
function pruneThumbs(s, ids) {
  const m = imgThumbs.get(s.sid);
  if (!m) return;
  const now = Date.now();
  for (const [n, t] of m) {
    if (ids.includes(n)) { t.seen = true; continue; }
    if (!t.seen && now - (t.at || 0) < 10000) continue;
    if (t.url) URL.revokeObjectURL(t.url);
    m.delete(n);
  }
}

function trayFor(s) {
  const st = screenState(s);
  const ids = inputImages(st);
  const op = imgOps.get(s.sid);
  if (!op && st && st.input && st.input.visible) pruneThumbs(s, ids);   // 对话框挡住输入框时不算"图片没了"
  const sess = imageSess(s);
  return trayModel({ images: ids, pending: op ? { kind: op.kind, n: op.n } : null, other: inputHasOther(st),
    thumbs: imgThumbs.get(s.sid), blocked: sess.dead || sess.dialog || sess.linkDown ? 'blocked' : null, removable: sess.local });
}

/** 图片条某张的 ×：只删这一张（后端把终端光标挪到 [Image #N] 后面按退格，每步带 guard；只由这次点击触发） */
function removeImage(n) {
  const s = liveSession();
  if (!s) return;
  const sess = imageSess(s);
  const why = sess.dead ? '会话已退出' : sess.dialog ? '请先处理上方对话框' : sess.linkDown ? '与本机 agent 的连接已断开，请稍后再试' : null;
  if (why || !sess.local) { if (why) toast(why, 'warn'); return; }
  const expect = inputImages(screenState(s));   // 图片条上此刻显示的编号：与屏幕不一致时后端一个键都不按
  if (!expect.includes(n)) return;
  runImageOp(s, 'remove', n, () => api.post(sp(s, '/image/remove'), { n, expect })).then((r) => {
    const m = imgThumbs.get(s.sid);
    const t = m && m.get(n);
    if (t) { if (t.url) URL.revokeObjectURL(t.url); m.delete(n); }
    if (r && r.warning) toast(r.warning, 'warn', 8000);
    scheduleUI();
  }, (e) => toast('删除图片失败：' + e.message, 'error', 8000));
}

/** 点图片条上的缩略图：用灯箱放大（图片条上有缩略图的几张可以 ←/→ 切换） */
function openTrayImage(n) {
  const s = liveSession();
  if (!s) return;
  const m = imgThumbs.get(s.sid);
  const { list, index } = lightboxList(imageTray.model, n, (c) => {
    const t = m && m.get(c.n);
    return t && t.path ? { s, path: t.path } : {};
  });
  if (list.length) openLightbox(list, index);
}

/** 拖入的图片：经后端只读通道取回文件做缩略图（带 token 头；只读该会话刚拖入并校验过的那个文件） */
async function loadDropThumb(s, n, token) {
  const m = thumbsOf(s);
  const fail = () => { const t = m.get(n); if (t && t.token === token) { t.loading = false; t.failed = true; scheduleUI(); } };
  try {
    const res = await fetch(sp(s, '/image/file/' + encodeURIComponent(token)), { headers: api.headers(), credentials: CREDENTIALS, cache: 'no-store' });
    if (!res.ok) { fail(); return; }
    const blob = await res.blob();
    if (!/^image\/(png|jpeg|gif|webp)$/.test(blob.type)) { fail(); return; }
    const t = m.get(n);
    if (!t || t.token !== token) return;   // 期间已被删掉 / 换了
    t.url = URL.createObjectURL(blob);
    t.loading = false;
    scheduleUI();
  } catch (_) { fail(); }
}

/** 图片条「清除」：claude 输入框里只有图片时在终端里按 Ctrl+U（后端带 guard；只由这次点击触发） */
function clearImages() {
  const s = liveSession();
  if (!s) return;
  runImageOp(s, 'clear', 0, () => api.post(sp(s, '/image/clear'))).catch((e) => toast('清除图片失败：' + e.message, 'error'));
}

/** 拖放：全局拦掉文件拖放（否则浏览器 / WKWebView 会把文件当成导航打开）；原生窗口交来的路径见 ccr-native-* 事件 */
function bindImageDrop() {
  const main = document.querySelector('.main');
  if (main && !dropOverlay.el.isConnected) main.appendChild(dropOverlay.el);
  let depth = 0;
  window.addEventListener('dragenter', (e) => {
    if (!dragHasFiles(e.dataTransfer)) return;
    depth++;
    dropOverlay.show(dropHint({ native: false }));
  });
  window.addEventListener('dragleave', (e) => {
    if (!dragHasFiles(e.dataTransfer)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) dropOverlay.hide();
  });
  window.addEventListener('dragover', (e) => {
    if (!dragHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'none';
  });
  window.addEventListener('drop', (e) => {
    if (!dragHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    depth = 0;
    dropOverlay.hide();
    // 能走到这里 = 没有路径（浏览器；或原生窗口里从网页拖来的图片、只有文件承诺的拖动）
    toast(NATIVE_APP ? '拿不到这个文件的路径（例如从网页拖来的图片）：可以复制图片后在输入框里按 ⌘V'
      : '浏览器里拿不到文件路径：截图请在输入框里按 ⌘V 粘贴（截屏时加按 Control 会直接存进剪贴板），或在 CC Remote Chat 原生窗口里拖入', 'warn', 8000);
  });
  // 原生窗口（tools/macapp/main.swift 的 CCRWebView）：拖入的是本机文件 → 带路径的事件，页面收不到 DOM 的拖放事件
  window.addEventListener('ccr-native-drag', (e) => {
    const d = (e && e.detail) || {};
    if (d.phase === 'leave') { dropOverlay.hide(); return; }
    dropOverlay.show(dropHint({ native: true, paths: d.paths, sess: imageSess(liveSession()) }));
  });
  window.addEventListener('ccr-native-drop', (e) => {
    dropOverlay.hide();
    onNativeDrop(((e && e.detail) || {}).paths);
  });
}

/** 原生窗口拖入的本机文件：图片 → 粘贴路径进 claude 输入框（claude 变成 [Image #N]）；其它文件 → 路径插入输入框 */
function onNativeDrop(paths) {
  const s = liveSession();
  const sp2 = splitDropPaths(paths);
  const why = imageBlocked(imageSess(s), 'drop');
  if (why) { toast(why, 'warn', 6000); return; }
  if (sp2.others.length) {
    const ta = $('input');
    if (!ta.disabled) {
      const r = insertPaths(ta.value, ta.selectionStart, ta.selectionEnd, sp2.others);
      ta.value = r.value;
      const d = currentDraft();
      if (d) { noteDraft(d, r.value); if (d.nav) d.nav.reset(); }
      ta.focus();
      ta.setSelectionRange(r.caret, r.caret);
      autosize();
    }
  }
  if (!sp2.images.length) return;
  if (sp2.images.length > 10) { toast('一次最多拖入 10 张图片', 'warn'); return; }
  const names = sp2.images.map((p) => p.slice(p.lastIndexOf('/') + 1));
  runImageOp(s, 'paths', sp2.images.length, () => api.post(sp(s, '/image/paths'), { paths: sp2.images })).then((r) => {
    const added = (r && Array.isArray(r.added)) ? r.added : [];
    const files = (r && r.files && typeof r.files === 'object') ? r.files : {};
    added.forEach((n, k) => {
      const token = typeof files[String(n)] === 'string' ? files[String(n)] : '';
      if (!names[k]) return;
      thumbsOf(s).set(n, { url: '', name: names[k], kind: 'drop', token, path: sp2.images[k], loading: !!token && !REMOTE, at: Date.now() });
      if (token && !REMOTE) loadDropThumb(s, n, token);
    });
    scheduleUI();
  }, (err) => toast('拖入图片失败：' + err.message, 'error', 8000));
}

async function send() {
  const s = liveSession();
  if (!s) return;
  const ta = $('input');
  const d = currentDraft();
  // 图片操作（⌘V / 拖入）还在进行：先等它结束并取一帧最新屏幕，这样刚粘的图片会一起发出（等待期间再按回车不重复发）
  const op = imgOps.get(s.sid);
  if (op) {
    if (sendWaitImg) return;
    sendWaitImg = true;
    try { await op.promise; await refreshScreen(s); } catch (e) { /* 失败已提示 */ } finally { sendWaitImg = false; }
    if (liveSession() !== s) return;
  }
  const typed = ta.value;
  // 引用条：拼成 Markdown 引用放在文字前面（/ 或 ! 开头的命令消息不带，引用保留）；
  // 正文以 @引用 结尾时补一个空格，免得终端里 claude 的 @ 补全列表吃掉回车（引用在前面，不影响结尾）
  const qs = quotes.list(s.sid);
  const comp = composeMessage(qs, atSafeTail(typed), s.absCwd || s.meta.cwd || '');
  const text = comp.text;
  const imgs = inputImages(screenState(s));
  if (!text.trim() && !imgs.length) return;
  if (isDead(s.meta)) { toast('会话已退出，请先点击“重连”', 'warn'); return; }
  if (currentDialog(s)) { toast('请先处理上方对话框', 'warn'); return; }
  if (comp.used) quotes.clear(s.sid);
  else if (qs.length && isCommandText(typed)) toast('命令消息不带引用（引用保留到下一条消息）', 'info');
  ta.value = '';
  noteDraft(d, '');
  draftStore.write(d.sid);   // 发出去了：本机保存的草稿立即删掉
  if (d.nav) d.nav.reset();
  autosize();
  closeSlash();
  scrollToBottom(s);
  // 发送前就记成“待出现”（等粘贴确认的一两秒里按 ↑ 也能翻到）；发送失败再撤回
  const pend = sentPending.get(s.sid) || [];
  addPending(pend, text, promptsFromItems(s.items).length);
  const mine = text.trim() ? pend[pend.length - 1] : null;   // 只有图片的消息不进 ↑/↓ 历史
  if (pend.length) sentPending.set(s.sid, pend);
  try {
    // expect_images：此刻图片条上显示的图片（可以是空数组）；后端核对屏幕，不一致就不发（不发用户没看到的图片）；只有图片也能发
    await withClearRetry((extra) => api.post(sp(s, '/message'), { text, expect_images: imgs, ...extra }));
  } catch (e) {
    const cur = sentPending.get(s.sid);
    const k = cur && mine ? cur.indexOf(mine) : -1;
    if (k >= 0) cur.splice(k, 1);
    toast('发送失败：' + e.message, 'error');
    if (comp.used && !quotes.list(s.sid).length) quotes.set(s.sid, qs);   // 引用放回引用条
    if (!ta.value) { ta.value = typed; noteDraft(d, typed); autosize(); }
  }
}

function setStaticIcons() {
  $('settings-btn').appendChild(icon('gear'));
  $('history-refresh').appendChild(icon('refresh'));
  $('send-btn').appendChild(icon('send'));
  $('stop-btn').prepend(icon('stop'));
}

boot();
