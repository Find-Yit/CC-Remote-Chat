// TUI 回放自测：把 fixtures 的真实交互式 Claude Code 屏幕帧（含 stream_frames 29 帧）与 transcript
// 回放进与正式界面同一套渲染器（render.js / tui.js）。屏幕解析用 tui_sim.js（后端解析器的简化 JS 版）。
// 所有“请求”都由模拟 actions 记录到日志，并按 fixtures 切换到对应的后续画面；任何未经点击触发的请求都会被判为失败。
import { h, icon } from '/static/dom.js';
import {
  createState, ensureSession, applyEnvelope, mountSession, flushNow, forgetSession, createsSession, currentDialog, draftVisible, setModelList, revealItem, refreshTail,
  applyHistory,
} from '/static/render.js';
import { openModelModal, openEffortModal, TermPanel, modeText, effortLockText, appliedToast, resultText, effortSaveBlocked } from '/static/tui.js';
import { loadToken, TOKEN_HEADER } from '/static/api.js';
import { parseScreen } from '/static/dev/tui_sim.js';
import { StatsBar, RING_C } from '/static/stats.js';
import { ComposerControls } from '/static/composerbar.js';
import { cycleOf, closeModeMenu } from '/static/modes.js';
import { initViz, _setBlobLoader, statPath, limitBlob, STAT_BATCH, BLOB_CONCURRENCY } from '/static/viz.js';
import { PreviewPanel } from '/static/preview.js';
import { AutoSize } from '/static/autosize.js';
import { PromptNav, promptsFromItems, mergePending, addPending, historyDir, recallInto } from '/static/prompthist.js';
import { sessionStatus, rowClass, isUnread, docTitle, titleCount, unreadCount, APP_TITLE } from '/static/sessrow.js';
import * as PJ from '/static/projects.js';
import { PromptMinimap, topPercent, MM_TARGET_OFFSET, minimapEntries } from '/static/minimap.js';
import { ChangesButton, trackerOf, NOTE_TEXT, EAGER_LINES } from '/static/changes.js';
import { OpBanner, OpGuard, opModel, jobPromptModel, fmtSecs, busyLabel } from '/static/opprogress.js';
import { SubagentUI } from '/static/subagents.js';
import { apiKeySelect, baseInfo, baseEmptyHint, setDefaultBaseUrl, loadApiKeys } from '/static/apikeys.js';
import { modal as fxModal, topDialog } from '/static/dialogs.js';
import { settingsDialog } from '/static/settings.js';
import { bannerModel, settingsModel, snoozeText, netText, hostsText, checkText, pctOf, UpdateStore, UpdateBanner, updateAction, updateSettingsField } from '/static/update.js';
import { ImageTray, DropOverlay, transferInfo, dragHasFiles, trayModel, dropHint, inputImages, inputHasOther, escapeDropPath, insertPaths, lightboxList } from '/static/attach.js';
import { openLightbox, lightboxOpen, closeLightbox } from '/static/lightbox.js';
import { isSrv, _setDownloader, SRV_TEXT_HEAD } from '/static/srvfiles.js';
import { decodeText, _setGbSupport } from '/static/textdecode.js';
import { Completer, CompleteCache, atSafeTail } from '/static/complete.js';
import { TodoPin } from '/static/todopin.js';
import { DraftStore } from '/static/drafts.js';
import * as FD from '/static/finder.js';        // 会话内查找（第 14l 节）
import * as TV from '/static/toolview.js';      // 工具调用显示档位
import * as QT from '/static/quote.js';         // 引用回复
import * as MT from '/static/mdtable.js';       // 表格复制
import * as CW from '/static/chatwidth.js';     // 聊天宽度
import { renderPatch } from '/static/tools.js';

const $ = (id) => document.getElementById(id);
const SID = 'sfixture00001';
const FILES = {
  stream: '/fixtures/tui_screens_stream.ndjson',
  dialogs: '/fixtures/tui_screens_dialogs.ndjson',
  transcript: '/fixtures/transcript_interactive_59dd443a.jsonl',
  images: '/fixtures/tui_screens_images.ndjson',   // CLI 2.1.332 输入框里的 [Image #N]（第 1e 节）
};

let errors = 0;
let checksOk = 0;
let checksBad = 0;
const calls = [];
let gesture = 0; // >0 = 当前处于（模拟的）用户点击中
let gseq = 0;
const screens = new Map(); // 'stream:ready' -> 原文
let streamFrames = [];
let transcriptLines = [];
let state, s, modelCtrl = null, term, termOpen = false;
let curScene = '';
const statsBar = new StatsBar();   // 与 app.js 同一个组件（数据 = meta.stats）；只挂一次到底栏的固定槽位
$('fx-stats').appendChild(statsBar.el);
// 底栏左侧控件组：与 app.js 同一个组件，按 id 找按钮
const ctl = new ComposerControls($('fx-ctl'), { ids: { model: 'fx-model-btn', effort: 'fx-effort-btn', mode: 'fx-mode', cycle: 'fx-cycle' } });
$('fx-stop').prepend(icon('stop'));
// 左侧 prompt 小格子：与 app.js 同一个组件，在 #fx-host 里只挂一个，setup() 里跟随回放会话
const fxMinimap = new PromptMinimap($('fx-host'), { reveal: revealItem });
// 会话内查找 / 工具调用显示档位 / 引用条：与 app.js 同一套组件；回放页的档位、宽度都不写 localStorage
const fxFind = new FD.SessionFind($('fx-host'), { reveal: revealItem, flush: flushNow });
FD.installFindKeys(fxFind, { blocked: () => !!document.querySelector('dialog[open]') || lightboxOpen() });
const fxTiers = new TV.ToolTiers({ persist: false, initial: 'all', onChange: () => { if (s) fxMinimap.onFlush(s); } });
const fxQuotes = new QT.QuoteStore(() => fxQuoteSync());
const fxQuoteBar = new QT.QuoteBar({ onRemove: (id) => fxQuotes.remove(SID, id), onClear: () => fxQuotes.clear(SID) });
document.querySelector('.fx .composer-box').before(fxQuoteBar.el);
const fxPicker = new QT.QuotePicker({ canQuote: () => !!s, onQuote: (q) => { fxQuotes.add(SID, q); $('fx-input').focus({ preventScroll: true }); } });
function fxQuoteSync() { fxQuoteBar.update(fxQuotes.list(SID), (s && (s.absCwd || s.meta.cwd)) || ''); }
const fxFindBtn = h('button', { class: 'btn ghost small find-open-btn', type: 'button', id: 'fx-find-btn', title: '在会话中查找（⌘F）' }, icon('search'), h('span', null, '查找'));
fxFindBtn.addEventListener('click', () => fxFind.open());
// 聊天宽度：回放页固定用标准宽度（不跟随正式界面存在 localStorage 里的偏好；theme.js 首帧可能已按偏好写了 data-chatw）
CW.applyWidth('std', { persist: false });
// 表格复制：回放页永远用桩（绝不碰真实剪贴板）
const fxCopied = [];
MT._setTableCopy(async (t) => { fxCopied.push(t); return true; });
// 右侧「代码改动」栏的会话头开关：与 app.js 同一个组件；回放页的开关状态用自己的 localStorage 键，不影响正式界面
const FX_CHG_KEY = 'ccr.fx.chg';
try { localStorage.removeItem(FX_CHG_KEY); } catch (_) { /* 忽略 */ }
const fxChgBtn = new ChangesButton({ onClick: () => { ensureVizPanel().toggleChanges(s); } });
fxChgBtn.el.id = 'fx-chg-btn';
function fxChgSync() { fxChgBtn.update(vizPanel && s ? vizPanel.changeCount(s) : 0, !!vizPanel && vizPanel.isShowingChanges()); }

function log(text, cls = 'info') {
  const el = h('div', { class: cls }, text);
  $('fx-log').appendChild(el);
  el.scrollIntoView({ block: 'end' });
}
function check(cond, text) {
  if (cond) { checksOk++; log('✔ ' + text, 'ok'); } else { checksBad++; log('✘ ' + text, 'bad'); }
  $('fx-progress').textContent = `通过 ${checksOk} · 失败 ${checksBad} · 控制台错误 ${errors}`;
  return cond;
}

const origError = console.error.bind(console);
console.error = (...a) => { errors++; log('console.error：' + a.map(String).join(' '), 'bad'); origError(...a); };
window.addEventListener('error', (e) => { errors++; log('页面错误：' + e.message, 'bad'); });
window.addEventListener('unhandledrejection', (e) => { errors++; log('未处理的 Promise 拒绝：' + ((e.reason && e.reason.message) || e.reason), 'bad'); });

// ---------- 取 fixture ----------
async function fetchText(url) {
  const token = loadToken();
  const res = await fetch(url, { headers: token ? { [TOKEN_HEADER]: token } : {}, credentials: 'omit', cache: 'no-store' });
  if (!res.ok) throw new Error(url + '：HTTP ' + res.status + '（需要 app.py --dev 并在已登录的标签页打开，或用“本地文件”选择 fixtures）');
  return res.text();
}

function ingest(name, text) {
  if (/transcript/.test(name)) {
    transcriptLines = text.split('\n').filter(Boolean).map((l) => { try { return JSON.parse(l); } catch (_) { return null; } }).filter(Boolean);
    log(`载入 ${name}：${transcriptLines.length} 行`);
    return;
  }
  const prefix = /dialogs/.test(name) ? 'dialogs' : /images/.test(name) ? 'images' : 'stream';
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    let d; try { d = JSON.parse(line); } catch (_) { continue; }
    if (typeof d.plain === 'string') screens.set(prefix + ':' + d.name, d.plain);
    if (Array.isArray(d.frames)) streamFrames = d.frames;
  }
  log(`载入 ${name}：累计 ${screens.size} 个画面，流式帧 ${streamFrames.length}`);
}

async function loadFixtures() {
  for (const [k, url] of Object.entries(FILES)) {
    try { ingest(k, await fetchText(url)); } catch (e) { log(String(e.message || e), 'bad'); }
  }
  fillScenes();
}

function fillScenes() {
  $('fx-scene').replaceChildren(...[...screens.keys()].map((k) => h('option', { value: k }, k)));
}

// ---------- 信封 ----------
function env(kind, data) { return { gseq: ++gseq, sid: SID, ts: Date.now() / 1000, kind, data }; }
function pushScreen(text, name) {
  curScene = name || '';
  applyEnvelope(state, env('screen', { type: 'screen', state: parseScreen(text), text }));
}
function pushTranscript(entries, reset) {
  applyEnvelope(state, env('transcript', { type: 'transcript', entries, reset: !!reset, session_id: '59dd443a-c637-4148-bf1c-fd720f2e5b1f' }));
}
function pushMeta(meta) { applyEnvelope(state, env('app', { type: 'meta', meta })); }
function show(name) {
  const t = screens.get(name);
  if (t == null) { log('没有画面 ' + name, 'bad'); return; }
  pushScreen(t, name);
}

// ---------- 模拟 actions（记录 + 按 fixtures 切换后续画面） ----------
const FULL_MODELS = {
  rows: [
    { index: 0, n: 1, name: 'Default (recommended)', desc: 'Opus 5.5 · Most capable for complex work', current: false },
    { index: 1, n: 2, name: 'Opus 5.5', desc: 'Opus 5.5 · Most capable', current: false },
    { index: 2, n: 3, name: 'Fable 5.1', desc: 'Custom Fable model (claude-fable-5-1[1m])', current: false },
    { index: 3, n: 4, name: 'Sonnet', desc: 'Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok', current: false },
    { index: 4, n: 5, name: 'Haiku', desc: 'Haiku 5.5 · Fastest for quick answers · $0.10/$0.50 per Mtok', current: true },
    { index: 5, n: 6, name: 'Fable 5.2 (1M)', desc: 'claude-fable-5-2，1M 上下文', current: false },
  ],
  cursor: 4,
  effort: { level: 'low', label: 'Low effort', is_default: false },
};

const AFTER = {
  'stream:trust': 'stream:ready', 'stream:trust_down': 'stream:ready',
  'dialogs:perm': 'dialogs:perm_after_esc', 'dialogs:perm_write': 'dialogs:perm_write_after_3',
  'stream:model_down': 'stream:model_after_esc',
  'dialogs:model': 'dialogs:model_after_esc', 'dialogs:model_right': 'dialogs:model_after_esc', 'dialogs:model_down': 'dialogs:model_after_esc',
  'dialogs:effort': 'dialogs:effort_after_esc', 'sim:generic': 'dialogs:final',
};
const later = (fn, ms = 120) => setTimeout(fn, ms);

function record(op, body) {
  const ok = gesture > 0;
  calls.push({ op, body, gesture: ok });
  log(`${ok ? '→' : '⚠ 未经点击'} POST ${op} ${body ? JSON.stringify(body) : ''}`, ok ? 'info' : 'bad');
  if (!ok) checksBad++;
}

function keyTransition(k) {
  const sc = curScene;
  if (sc.startsWith('dialogs:model')) {
    if (k === 'Down') return 'dialogs:model_down';
    if (k === 'Up') return 'dialogs:model';
    if (k === 'Right') return 'dialogs:model_right';
    if (k === 'Left') return 'dialogs:model';
  }
  if (sc.startsWith('stream:trust')) {
    if (k === 'Down') return 'stream:trust_down';
    if (k === 'Up') return 'stream:trust';
  }
  if (sc === 'dialogs:effort' && (k === 'Left' || k === 'Right')) return null;
  if (k === 'Enter' || k === 'Escape' || k === 's') return AFTER[sc] || null;
  return null;
}

const actions = {
  choose: async (_s, index, expect) => { record('choose', { index, expect }); const n = AFTER[curScene]; if (n) later(() => show(n)); },
  cancelDialog: async () => { record('cancel_dialog'); const n = AFTER[curScene]; if (n) later(() => show(n)); },
  keys: async (_s, keys) => { record('keys', { keys }); for (const k of keys) { const n = keyTransition(k); if (n) later(() => show(n)); } },
  freeText: async (_s, index, text) => { record('choose', { index, text }); later(() => show('stream:model_after_esc')); },
  restart: async () => record('restart'),
  openModelDialog: (ss) => openModel(ss, false),
  applyModel: async (_s, body) => { record('model', body); await new Promise((r) => setTimeout(r, 200)); show('dialogs:model_after_esc'); return { ok: true }; },
  applyEffort: async (_s, body) => { record('effort', body); await new Promise((r) => setTimeout(r, 200)); return { ok: true }; },
  onApplied: () => log('（toast）已切换'),
};

// 用户点击：所有请求必须发生在 click() 调用栈及其后续微任务中
async function click(el, label) {
  if (!el) { check(false, '找不到要点击的元素：' + label); return; }
  gesture++;
  try { el.click(); await new Promise((r) => setTimeout(r, 80)); } finally { gesture--; }
}

// ---------- 头部 / 输入框 / 终端（与 app.js 同逻辑的精简版） ----------
function openModel(ss, viaButton) {
  if (modelCtrl && modelCtrl.isOpen()) return;
  let pending = null;
  if (viaButton) {
    record('model_picker');
    later(() => show('dialogs:model'), 60);
    pending = new Promise((r) => setTimeout(() => r(FULL_MODELS), 400)).then((r) => { setModelList(ss, r); return r; });
  }
  ss.modelSuppress = false;
  const ctrl = openModelModal({ s: ss, actions, pending, onClosed: () => { if (modelCtrl === ctrl) modelCtrl = null; const d = currentDialog(ss); ss.modelSuppress = !!(d && d.kind === 'model'); } });
  modelCtrl = ctrl;
}

function onScreen(ss) {
  const d = currentDialog(ss);
  if (!d || d.kind !== 'model') ss.modelSuppress = false;
  if (modelCtrl) {
    if ((!d || d.kind !== 'model') && !modelCtrl.busy) { const c = modelCtrl; modelCtrl = null; c.close(true); }
    else modelCtrl.update();
  } else if (d && d.kind === 'model' && !ss.modelSuppress) openModel(ss, false);
  if (termOpen) term.update(ss);
  renderChrome();
}

function renderChrome() {
  const st = (s.screen && s.screen.state) || {};
  const d = currentDialog(s);
  const busy = !!st.busy;
  const dead = s.meta.state === 'exited';
  const termBtn = h('button', { class: 'btn small ' + (termOpen ? 'primary' : 'ghost'), type: 'button', id: 'fx-term-btn' }, icon('terminal'), '终端');
  termBtn.addEventListener('click', () => { termOpen = !termOpen; $('fx-term').hidden = !termOpen; if (termOpen) term.setSession(s); renderChrome(); });
  // 头部只剩标题、状态、job 徽章与按钮（模型 / effort / 模式 / 统计都在输入框底栏）
  $('fx-header').replaceChildren(
    h('div', { class: 'hd-main' },
      h('div', { class: 'hd-row' }, h('div', { class: 'hd-title' }, '回放会话'), h('span', { class: 'status-chip' }, d ? '待处理' : busy ? '运行中' : '空闲')),
      h('div', { class: 'hd-sub' }, h('span', { class: 'chip job-badge mono' }, 'job 123456'))),
    h('div', { class: 'hd-actions' }, fxFindBtn, fxTiers.el, fxChgBtn.el, termBtn));
  fxChgSync();
  // 与 app.js renderComposerControls 同样的禁用规则：模型/effort 在已退出/对话框/运行中/断线时禁用；模式运行中仍可切换
  const why = dead ? '会话已退出' : d ? '请先处理当前对话框' : busy ? '运行中，请等本轮结束（或先中断）' : s.meta.link === 'down' ? '与计算节点的连接已断开' : '';
  const modeBlocked = dead ? '会话已退出' : d ? '请先处理当前对话框' : s.meta.link === 'down' ? '与计算节点的连接已断开' : '';
  ctl.update({
    sid: s.sid, model: s.modelInfo.model || '默认模型', effort: (s.meta.effort_locked && s.meta.effort_locked.level) || s.modelInfo.effort || '默认', why,
    effortLock: effortLockText(s.meta),
    mode: st.mode || '', cycle: cycleOf(s.meta), modeBlocked,
    onModel: () => openModel(s, true),
    onEffort: () => openEffortModal({ s, actions, current: (s.meta.effort_locked && s.meta.effort_locked.level) || s.modelInfo.effort }),
    onPickMode: (id, needsRestart) => record(needsRestart ? 'restart' : 'mode', { mode: id }),
    onCycle: () => record('mode/cycle'),
  });
  const ta = $('fx-input');
  ta.disabled = !!d;
  const ph = d ? '请先处理上方对话框' : busy ? '运行中：新消息会进入 claude 的输入队列' : '回放页：发送只记录到日志';
  if (ta.placeholder !== ph) { ta.placeholder = ph; fxSizer.update(); }
  $('fx-send').disabled = !!d;
  $('fx-stop').hidden = !busy;
  statsBar.update(s.meta.stats, { busy, dead });
  const b = $('fx-banner');
  b.hidden = !d;
  b.textContent = d ? '请先处理上方对话框（选项按钮会把对应按键发给终端）' : '';
}

function setup() {
  for (const el of $('fx-host').querySelectorAll('.chat-scroll')) el.remove();
  if (modelCtrl) { modelCtrl.close(true); modelCtrl = null; }
  closeModeMenu();
  state = createState({ actions, onScreen: (ss) => onScreen(ss), onMeta: () => renderChrome(), onModelInfo: () => renderChrome(),
    onFlush: (ss) => { fxMinimap.onFlush(ss); if (vizPanel) vizPanel.onSessionUpdate(ss); fxFind.onFlush(ss); fxTiers.onFlush(ss); },
    onSubagent: (ss) => { if (vizPanel) vizPanel.onSessionUpdate(ss); } });
  s = ensureSession(state, SID, { title: '回放会话', jobid: '123456', cwd: '/mnt/ssd/home/demouser', state: 'idle', link: 'ready', launch: { model: null, permission_mode: null, skip_permissions: false, effort: null } });
  mountSession(s, $('fx-host'));
  fxMinimap.setSession(s);
  fxFind.setSession(s);
  fxTiers.setSession(s);
  term = new TermPanel($('fx-term'), {
    onKeys: (keys) => actions.keys(s, keys),
    onRefresh: async () => { log('GET screen（回放：重放当前画面）'); if (curScene && screens.has(curScene)) show(curScene); },
    onClose: () => { termOpen = false; $('fx-term').hidden = true; renderChrome(); },
  });
  renderChrome();
}

// ---------- transcript ----------
function transcriptEntries() {
  return transcriptLines.filter((d) => (d.type === 'user' || d.type === 'assistant' || d.type === 'system') && !d.isSidechain);
}

// ---------- 流式回放 ----------
async function playStream(speed, observe) {
  if (!streamFrames.length) { log('没有 stream_frames', 'bad'); return; }
  const t0 = streamFrames[0].t;
  const start = performance.now();
  for (let i = 0; i < streamFrames.length; i++) {
    const f = streamFrames[i];
    const due = ((f.t - t0) * 1000) / speed;
    const wait = due - (performance.now() - start);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    pushScreen(f.s, 'frame' + i);
    await new Promise((r) => requestAnimationFrame(() => r()));
    if (observe) observe(i);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const raf = () => new Promise((r) => requestAnimationFrame(() => r()));
const q = (sel) => s.view.root.querySelector(sel);
const qa = (sel) => [...s.view.root.querySelectorAll(sel)];

async function autoTest() {
  $('fx-log').replaceChildren();
  calls.length = 0; checksOk = 0; checksBad = 0; errors = 0;
  setup();
  const speed = Number($('fx-speed').value) || 2;
  log('== 1. transcript 渲染 / uuid 去重 / reset');
  const ents = transcriptEntries();
  pushTranscript(ents, true);
  await raf(); flushNow(s);
  const users = qa('.k-user').map((e) => e.textContent);
  check(users.some((t) => t.includes('touch /tmp/ccr_tui_probe_denied2')) && users.some((t) => t.includes('Write 工具')), '用户消息渲染（2 条真实提示）');
  check(!users.some((t) => t.includes('local-command-caveat')), 'isMeta 的 caveat 条目不显示');
  check(qa('.k-cmd').some((e) => e.textContent.includes('/model') && e.textContent.includes('Kept model as Haiku 5.5')), '/model 本地命令 + 输出合并为一条');
  check(qa('.k-cmd').some((e) => e.textContent.includes('/effort') && e.textContent.includes('Cancelled')), '/effort 本地命令 + Cancelled');
  check(qa('.k-tool.st-denied').length === 2, '两个被拒绝的工具调用（Bash / Write）');
  check(qa('.k-notice').length === 2, '两条中断提示');
  const n1 = s.items.length;
  pushTranscript(ents.slice(-5), false);
  check(s.items.length === n1, 'uuid 去重：重复条目不再追加');
  pushTranscript(ents.slice(0, 3), true);
  await raf(); flushNow(s);
  check(s.items.length < n1 && qa('.k-tool').length === 0, 'reset=true 整体重建');
  pushTranscript(ents, true);
  await raf(); flushNow(s);

  log('== 1b. 输入框不抖动（关闭滚动锚定模拟 WebKit）');
  await typingChecks();
  pushTranscript(ents, true);
  await raf(); flushNow(s);

  log('== 1c. 输入框 ↑/↓ 历史（prompthist.js：之前发过的消息放回输入框）');
  await recallChecks();

  log('== 1d. 左侧 prompt 小格子（minimap.js：鱼眼、预览卡、跳转、可见轮次、上一轮/下一轮、键盘、滚轮、常驻/悬停）');
  await minimapChecks();

  log('== 1e. 图片（attach.js：claude 输入框里的 [Image #N] 图片条、拖动覆盖层、DataTransfer、带图片的用户消息）');
  await imageChecks();

  log('== 2. 信任目录卡片');
  show('stream:trust');
  await raf();
  check(!!q('.dlg-card.kind-trust'), '出现 trust 卡片');
  check(qa('.dlg-card.kind-trust .dlg-opt').length === 2, 'trust 两个选项');
  check((q('.dlg-card.kind-trust') || {}).textContent?.includes('home folder, so Yes lasts for this session only'), '原文含“home 目录仅本次有效”说明');
  check($('fx-input').disabled, '有对话框时输入框禁用');
  check(calls.length === 0, '对话框出现时没有自动发送任何请求');
  show('stream:trust_down');
  await raf();
  check(qa('.dlg-card.kind-trust .dlg-opt.cursor')[0]?.dataset.optIndex === '1', '光标移到第 2 项只更新样式');
  await click(qa('.dlg-card.kind-trust .dlg-opt')[1], 'trust Yes');
  check(calls.at(-1)?.op === 'choose' && calls.at(-1).body.index === 1, '点击 → choose(1)');
  await sleep(250);
  check(!q('.dlg-card'), '选择后卡片消失');
  check(!$('fx-input').disabled, '输入框恢复可用');
  check(s.modelInfo.model === 'Haiku 5.5' && s.modelInfo.effort === 'low', '头部模型/effort 来自状态栏：Haiku 5.5 / low');
  check($('fx-mode').dataset.mode === 'auto' && $('fx-mode').textContent.includes('自动') && /auto/.test($('fx-mode').title), '权限模式 chip = 自动（auto，英文 id 在 tooltip）');

  log('== 3. 实时草稿（stream_frames 29 帧）');
  const draft = s.view.draft;
  const pre = s.view.draftPre;
  let lens = []; let sawSpinner = false; let sawBusyDraft = false;
  await playStream(speed, (i) => {
    if (draftVisible(s) && !draft.hidden) sawBusyDraft = true;
    if (/Schlepping/.test(s.view.draftText.textContent)) sawSpinner = true;
    if (i >= 19 && i <= 27) lens.push(pre.textContent.length);
  });
  check(sawBusyDraft, '忙时显示“生成中”草稿卡片');
  check(sawSpinner, '草稿显示 spinner 文本（Schlepping…）');
  check(lens.length > 0 && lens.every((v, k) => k === 0 || v >= lens[k - 1]) && lens.at(-1) > lens[0], '草稿内容逐帧增长：' + lens.join(','));
  check(s.view.draftPre === pre && pre.isConnected, '草稿节点复用（不重建、不闪）');
  check(!draft.hidden && pre.textContent.includes('热泉'), '空闲后、transcript 未到时保留最后草稿');
  const finalText = '深海热泉是洋中脊、弧后盆地等板块边界上的海底喷口。……（回放合成的 assistant 条目）';
  pushTranscript([
    { type: 'user', uuid: 'sim-u1', timestamp: new Date().toISOString(), message: { role: 'user', content: '写一段约 400 字的中文短文，介绍深海热泉，不要用任何工具。' } },
    { type: 'assistant', uuid: 'sim-a1', timestamp: new Date().toISOString(), message: { id: 'msg_sim', role: 'assistant', content: [{ type: 'text', text: finalText }] } },
  ], false);
  await raf(); flushNow(s);
  check(draft.hidden, 'assistant 条目到达后草稿隐藏');
  check(qa('.k-text').some((e) => e.textContent.includes('回放合成的 assistant 条目')), 'assistant 条目以 markdown 渲染');

  log('== 4. AskUserQuestion 卡片（含 Type something 自由输入）');
  show('stream:model_down');
  await raf();
  const qc = q('.dlg-card.kind-question');
  check(!!qc && qc.textContent.includes('颜色偏好') && qc.textContent.includes('你更喜欢红色还是蓝色？'), 'question 卡片：header + 问题');
  check(qa('.dlg-card.kind-question .dlg-opt').length === 4, 'question 4 个选项');
  check(qc && qc.textContent.includes('选择红色'), '选项描述（缩进续行）');
  const before = calls.length;
  await click(q('.dlg-card.kind-question .dlg-opt.free'), 'Type something');
  check(calls.length === before, '点“自己输入…”只展开文本框，不发请求');
  const fi = q('.dlg-free-input');
  check(!!fi && !q('.dlg-free').hidden, '自由输入框出现');
  fi.value = '绿色'; fi.dispatchEvent(new Event('input'));
  await click(q('.dlg-free .btn.primary'), '提交回答');
  await sleep(120);
  check(calls.at(-1)?.op === 'choose' && calls.at(-1).body.index === 2 && calls.at(-1).body.text === '绿色', '提交 = POST choose {index:2, text:"绿色"}（选中自由输入项 + 粘贴 + 回车）');
  await sleep(200);
  check(!q('.dlg-card'), '回答后卡片消失');

  log('== 5. 权限卡片（Bash）');
  show('dialogs:perm');
  await raf();
  const pc = q('.dlg-card.kind-permission');
  check(!!pc && pc.textContent.includes('Bash command'), 'permission 卡片：工具名');
  check(pc && q('.dlg-preview')?.textContent.includes('touch /tmp/ccr_tui_probe_denied2'), '预览（等宽）');
  check(pc && pc.textContent.includes('Create probe file in /tmp') && pc.textContent.includes('Do you want to proceed?'), '描述 + 问题');
  check(qa('.dlg-card.kind-permission .dlg-opt').length === 4, 'Bash 权限 4 个选项');
  check($('fx-model-btn').disabled && $('fx-effort-btn').disabled, '有对话框时“切换模型/effort”禁用');
  await click([...pc.querySelectorAll('button')].find((b) => b.textContent.includes('取消（Esc）')), 'Esc 取消');
  check(calls.at(-1)?.op === 'cancel_dialog', 'Esc 取消 → cancel_dialog');
  await sleep(200);
  check(!q('.dlg-card'), '取消后卡片消失');

  log('== 6. 权限卡片（Write，选项折行）');
  show('dialogs:perm_write');
  await raf();
  const wopts = qa('.dlg-card.kind-permission .dlg-opt');
  check(wopts.length === 3 && wopts[1].textContent.includes('(shift+tab)'), 'Write 3 个选项，折行并入第 2 项');
  check(q('.dlg-card.kind-permission .dlg-opt.deny')?.textContent.includes('No'), '“No” 用拒绝样式');
  await click(wopts[2], 'No');
  check(calls.at(-1)?.op === 'choose' && calls.at(-1).body.index === 2, '点击 3. No → choose(2)');
  {
    const ex = calls.at(-1)?.body?.expect;
    check(ex && ex.kind === 'permission' && ex.title === 'Create file' && ex.labels.length === 3 && ex.labels[2] === 'No',
      'choose 带对话框指纹 expect {kind,title,question,preview,labels}');
  }
  await sleep(200);

  log('== 7. 用户自己输入 /model：同一弹窗（可见窗口 + 滚动累积）');
  show('dialogs:model');
  await raf(); await sleep(50);
  let dlg = document.querySelector('dialog.modal');
  check(!!dlg && dlg.textContent.includes('切换模型'), '屏幕出现 model 面板 → 自动弹出模型弹窗');
  check(calls.length && calls.at(-1).op !== 'model_picker', '自动弹窗不发 model_picker');
  check(dlg && dlg.querySelectorAll('.model-row').length === 3, '可见窗口 3 行');
  check(dlg && dlg.querySelector('.model-row.on')?.textContent.includes('Haiku') && dlg.textContent.includes('✔ 当前'), '默认选中当前（Haiku ✔）');
  await click([...dlg.querySelectorAll('button')].find((b) => b.textContent.includes('光标下移')), '光标下移');
  check(calls.at(-1)?.op === 'keys' && calls.at(-1).body.keys[0] === 'Down', '滚动 → keys Down');
  await sleep(200);
  dlg = document.querySelector('dialog.modal');
  check(dlg && dlg.querySelectorAll('.model-row').length === 4, '滚动后累积到 4 行（3–6）');
  await click([...dlg.querySelectorAll('.model-row')].find((b) => b.textContent.includes('Fable 5.2')), '选 Fable 5.2');
  await click([...dlg.querySelectorAll('.seg-item')].find((b) => b.textContent.startsWith('high')), 'effort high');
  check(dlg.textContent.includes('会写入服务器 ~/.claude/settings.json，和你在终端里按 Enter 一样'), '“保存为默认”附说明');
  await click([...dlg.querySelectorAll('button')].find((b) => b.textContent === '仅本会话（s）'), '仅本会话');
  await sleep(350);
  const mc = calls.filter((c) => c.op === 'model').at(-1);
  check(mc && mc.body.index === 5 && mc.body.effort === 'high' && mc.body.scope === 'session', 'POST model {index:5, effort:high, scope:session}');
  check(!document.querySelector('dialog.modal'), '切换后弹窗关闭');

  log('== 8. “切换模型”按钮：model_picker → 完整列表');
  show('dialogs:ready');
  await raf();
  await click($('fx-model-btn'), '切换模型');
  check(calls.some((c) => c.op === 'model_picker'), '点击 → POST model_picker');
  dlg = document.querySelector('dialog.modal');
  check(!!dlg && /正在读取/.test(dlg.textContent), '读取中提示');
  await sleep(600);
  dlg = document.querySelector('dialog.modal');
  check(dlg && dlg.querySelectorAll('.model-row').length === 6, '完整列表 6 项');
  check(dlg && dlg.querySelector('.model-row.on')?.textContent.includes('Haiku'), '光标 = 当前 Haiku');
  await click([...dlg.querySelectorAll('.model-row')].find((b) => b.textContent.includes('Sonnet')), '选 Sonnet');
  await click([...dlg.querySelectorAll('button')].find((b) => b.textContent.startsWith('保存为默认')), '保存为默认');
  await sleep(350);
  const mc2 = calls.filter((c) => c.op === 'model').at(-1);
  check(mc2 && mc2.body.index === 3 && mc2.body.effort === null && mc2.body.scope === 'save', 'POST model {index:3, effort:null, scope:save}');
  show('dialogs:model');
  await raf(); await sleep(50);
  dlg = document.querySelector('dialog.modal');
  await click([...(dlg ? dlg.querySelectorAll('button') : [])].find((b) => b.textContent === '取消（Esc）'), '取消（Esc）');
  await sleep(250);
  check(calls.at(-1)?.op === 'cancel_dialog', '弹窗“取消（Esc）” → cancel_dialog');
  check(!document.querySelector('dialog.modal'), '取消后弹窗关闭且不再自动弹出');

  log('== 9. /effort 面板卡片 + effort 弹窗');
  show('dialogs:effort');
  await raf();
  const ec = q('.dlg-card.kind-effort');
  check(!!ec && ec.querySelector('.seg-item.on')?.textContent === 'low', 'effort 卡片：当前档位 low（▲ 对齐）');
  await click([...ec.querySelectorAll('button')].find((b) => b.textContent.includes('提高')), '提高');
  check(calls.at(-1)?.op === 'keys' && calls.at(-1).body.keys[0] === 'Right', '提高 → keys Right');
  await sleep(5100); // 等卡片“提交中”状态自动恢复（屏幕未变化）
  await click([...q('.dlg-card.kind-effort').querySelectorAll('button')].find((b) => b.textContent.startsWith('仅本会话')), '仅本会话');
  check(calls.at(-1)?.op === 'keys' && calls.at(-1).body.keys[0] === 's', '仅本会话 → keys s');
  await sleep(250);
  check(!q('.dlg-card'), 'effort 面板关闭');
  await click($('fx-effort-btn'), 'effort 按钮');
  dlg = document.querySelector('dialog.modal');
  check(!!dlg && dlg.textContent.includes('调整 effort'), 'effort 弹窗');
  await click([...dlg.querySelectorAll('.seg-item')].find((b) => b.textContent === 'max'), 'max');
  await click([...dlg.querySelectorAll('button')].find((b) => b.textContent === '仅本会话（s）'), '仅本会话');
  await sleep(300);
  const ef = calls.filter((c) => c.op === 'effort').at(-1);
  check(ef && ef.body.level === 'max' && ef.body.scope === 'session', 'POST effort {level:max, scope:session}');

  log('== 10. generic 对话框 + 键盘区');
  const gen = ['', '────────────────────────────────────────', ' Resume a conversation', '', ' ❯ 1. 昨天的会话', '   2. 前天的会话', '', ' Enter to select · Esc to cancel'].join('\n');
  pushScreen(gen, 'sim:generic');
  await raf();
  const gc = q('.dlg-card.kind-generic');
  check(!!gc && gc.querySelector('.dlg-generic')?.textContent.includes('Resume a conversation'), 'generic：原文行');
  check(gc && gc.querySelectorAll('.dlg-opt').length === 2 && gc.querySelectorAll('.keypad .key').length === 7, 'generic：选项按钮 + 键盘区');
  await click([...gc.querySelectorAll('.key')].find((b) => b.textContent === '↓'), '↓');
  check(calls.at(-1)?.op === 'keys' && calls.at(-1).body.keys[0] === 'Down', '键盘区 ↓ → keys Down');

  log('== 11. 终端面板');
  await click($('fx-term-btn'), '终端');
  check(!$('fx-term').hidden && $('fx-term').querySelector('.term-screen').textContent.includes('Resume a conversation'), '终端面板显示最新屏幕原文');
  await click([...$('fx-term').querySelectorAll('.key')].find((b) => b.textContent === 'Shift+Tab'), 'Shift+Tab');
  check(calls.at(-1)?.op === 'keys' && calls.at(-1).body.keys[0] === 'BTab', 'Shift+Tab → keys BTab');
  await click([...$('fx-term').querySelectorAll('.key')].find((b) => b.textContent === 'Ctrl+C'), 'Ctrl+C');
  check(calls.at(-1)?.body.keys[0] === 'C-c', 'Ctrl+C → keys C-c');
  show('dialogs:final');
  await raf();
  check($('fx-term').querySelector('.term-screen').textContent.includes('Rejected write'), '终端面板随 screen 事件更新');
  check($('fx-mode').dataset.mode === 'manual' && $('fx-mode').textContent.includes('手动审批'), '模式 chip = 手动审批（manual）');
  await click($('fx-cycle'), '切换模式');
  check(calls.at(-1)?.op === 'mode/cycle', '切换（Shift+Tab）→ mode/cycle');

  log('== 12. busy_count → interrupted');
  show('stream:busy_count');
  await raf(); flushNow(s);
  check(!s.view.draft.hidden && s.view.draftPre.textContent.includes('20'), '忙时草稿显示逐行输出');
  check(!$('fx-stop').hidden, 'busy 时显示“中断（Esc）”');
  show('stream:interrupted');
  await sleep(3300); flushNow(s);
  check(s.view.draft.hidden, '空闲后草稿在宽限期后隐藏');

  log('== 13. 统计条（meta.stats：上下文圆环 / 花费 / tok/s）');
  await statsChecks();

  log('== 13b. 输入框底栏（模型/effort 胶囊、权限模式、Shift+Tab、不换行、分级折叠）');
  await composerBarChecks();

  log('== 13b2. effort 被 CLAUDE_CODE_EFFORT_LEVEL 锁定（CLI 2.1.332）：胶囊带锁 + 说明、弹窗提示、max 档「保存为默认」置灰、结果绝不显示 null');
  await effortLockChecks();

  log('== 13c. 侧栏「新回复」/「待处理」（sessrow.js：状态优先级、行 class、颜色、窗口标题未读数）');
  await sessRowChecks();

  log('== 14. 可视化（图片引用 / 灯箱 / 文件 chip / 本轮改动 / 网页预览 / 彩色区分）');
  await vizChecks();

  log('== 14b. 右侧代码改动栏（changes.js：按文件 / 按轮、diff 行号、联动、开关与记忆、空状态、懒渲染、不发请求、不动时间线）');
  await changesChecks();

  log('== 14c. dmg 版自动更新提示条与设置项（update.js：各状态、只在点击时请求、更新说明、下载页链接、浅色 / 深色对比度）');
  await updateChecks();

  log('== 14c2. 重启 / 续接进度（opprogress.js：阶段、计时、比平时慢、失败与重试、防连点、作业已结束时预填当前作业、浅色 / 深色对比度）');
  await opChecks();

  log('== 14d. /context 卡片与命令输出的 ANSI 颜色（ctxcard.js：真机 transcript、用量条、分类、小节、原文上色与网格对齐、浅色 / 深色）');
  await contextChecks();

  log('== 14e. 子代理被停止（CLI 2.1.332：TaskStop / 用户停止后托盘立即移除、Agent 卡与通知行「已终止」；回复中途吸收的 attachment 通知）');
  await subStopChecks();

  log('== 14f. 删除的会话不复活（迟到事件 / 别的窗口删除 / 重连回放；同名会话按 sid 区分）');
  removedChecks();

  log('== 14g. API key 对话框（叠放的对话框只有一个是模态、每个输入框都能输入、修改地址、设置里的默认 base_url）');
  await apiKeyChecks();

  log('== 14h. 回复进行中发出的消息（queued_command / commandMode=prompt：用户气泡「回复中发送」、位置、只出现一次、不另起一轮、↑/↓ 历史、小格子、待出现对账、图片、peer、历史预览）');
  await queuedPromptChecks();

  log('== 14i. 侧栏：项目与聊天（projects.js：显式归属、按 运行位置 + cwd 归组、顺序与拖动落地、聚合状态、标题行的颜色 / 折叠 / 截断 / 「+」、浅色 / 深色）');
  await projectChecks();

  log('== 14s. 系统通知与防休眠（sysnotify.js / keepawake.js：设置里的两个开关、只在点开关时请求授权、stub 掉原生 handler 核对调用时机与内容、点通知打开会话、状态文字、浅色 / 深色）');
  await sysChecks();
  log('== 14j. 服务器会话的文件预览与下载、GB18030 文本（srvfiles.js / textdecode.js：点了才查、加载中 / 失败 / 重试、各类文件、超限、下载、编码标签、本机 GBK、浅色 / 深色）');
  await srvFileChecks();
  log('== 14k. 输入框：/ 命令与 @ 文件补全、置顶任务清单（真机 2.1.332 TaskCreate / TaskUpdate）、草稿存 localStorage（complete.js / todopin.js / drafts.js）');
  await composerChecks();
  log('== 14l. 时间线工具（finder.js 会话内查找 · toolview.js 工具调用显示档位 · quote.js 引用回复 · mdtable.js 表格复制 · chatwidth.js 聊天宽度）');
  await timelineChecks();

  log('== 15. 红线 / 控制台');
  check(calls.every((c) => c.gesture), '所有请求都发生在用户点击中（共 ' + calls.length + ' 个）');
  check(errors === 0, '控制台零错误');
  $('fx-result').textContent = `自测完成：通过 ${checksOk} · 失败 ${checksBad} · 控制台错误 ${errors}`;
  window.__fxResult = { ok: checksOk, bad: checksBad, errors, calls: calls.length };
}

// ---------- API key 对话框 ----------
// 朋友在 dmg 版（WKWebView）里遇到「base_url 输入框打不进字」：API key 管理弹窗是从「新建会话」对话框里打开的，
// 叠放了多个 showModal()。现在同一时刻只有一个模态对话框，嵌套的挂在它里面（dialogs.js modal()）。
// 这里逐个检查：能获得焦点、点中的是它本身（没有覆盖层）、没有 inert / readonly / 0 宽、按键不被吞、能插入文字。
function fxCanType(el, text) {
  if (!el) return 'missing';
  el.focus();
  if (document.activeElement !== el) return 'no-focus';
  if (el.readOnly || el.disabled) return 'readonly';
  if (el.closest('[inert]')) return 'inert';
  const b = el.getBoundingClientRect();
  if (b.width < 60 || b.height < 16) return 'size ' + Math.round(b.width) + 'x' + Math.round(b.height);
  if (document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2) !== el) return 'covered';
  const kd = new KeyboardEvent('keydown', { key: 'a', code: 'KeyA', bubbles: true, cancelable: true });
  el.dispatchEvent(kd);
  if (kd.defaultPrevented) return 'keydown-prevented';
  const before = el.value;
  el.setSelectionRange(before.length, before.length);
  if (!document.execCommand('insertText', false, text) || el.value !== before + text) return 'insert-failed';
  return 'ok';
}

async function apiKeyChecks() {
  const OWN = 'http://127.0.0.1:2/own', DEF = 'http://127.0.0.1:3';
  const keys = [
    { id: 'kown', name: 'own', masked: 'sk-test…1111', base_url: OWN, is_server_default: false, created_at: 1, in_use: 0 },
    { id: 'knob', name: 'nobase', masked: 'sk-test…2222', base_url: null, is_server_default: false, created_at: 2, in_use: 1 },
  ];
  let cfgDefault = '';
  const gets = [];
  const fakeApi = {
    get: async (path) => {
      gets.push(path);
      if (path === '/api/api_keys') return { keys: keys.map((k) => ({ ...k })), default_base_url: cfgDefault };
      if (path.startsWith('/api/api_keys/usage')) return { usage: {} };
      if (path === '/api/notify') return { sounds: [], available: false };
      return {};
    },
    post: async (path, body) => {
      record('apikey ' + path, body);
      if (path === '/api/config') { cfgDefault = body.default_base_url || ''; return { default_base_url: cfgDefault }; }
      return { api_key: { id: 'knew', name: body.name, masked: 'x…y', base_url: body.base_url || null, is_server_default: false } };
    },
    patch: async (path, body) => { record('apikey PATCH ' + path, body); const k = keys.find((x) => path.endsWith('/' + x.id)); if (k) Object.assign(k, body); return { api_key: k }; },
    del: async (path) => { record('apikey DELETE ' + path); return {}; },
  };
  // 纯函数：地址显示的优先级
  check(baseInfo({ base_url: OWN }, DEF).text === OWN && baseInfo({ base_url: null }, DEF).text === DEF + '（默认）'
    && baseInfo({ base_url: null }, '').text === '默认地址（settings.json）', '地址：自己的 base_url → 默认 base_url（标注「默认」）→ 默认地址（settings.json）');
  check(baseEmptyHint(DEF).includes(DEF) && baseEmptyHint('').includes('settings.json'), 'base_url 留空时的说明随默认 base_url 变化');

  // 「新建会话」对话框（模态）里的「API」下拉 → 管理 API key…（真实路径：select 的 change）
  const pick = apiKeySelect(fakeApi, {});
  const host = fxModal({ title: '新建会话（回放）', body: h('div', { class: 'form-grid' }, pick.el, pick.hint), actions: [] });
  await raf();
  pick.el.value = '__manage__';
  pick.el.dispatchEvent(new Event('change'));
  await new Promise((r) => setTimeout(r, 120));
  const mg = topDialog();
  const modals = [...document.querySelectorAll('dialog[open]')].filter((d) => d.matches(':modal'));
  check(mg && mg !== host && mg.classList.contains('nested') && host.contains(mg) && modals.length === 1 && modals[0] === host,
    '从新建会话里打开「管理 API key」：只有一个模态对话框，管理弹窗嵌在它里面（WKWebView 叠放多个模态对话框时输入框可能打不进字）');
  check(host.querySelector(':scope > .modal-form').inert && !mg.closest('[inert]'), '下层对话框的内容 inert，上层不受影响');
  check(!mg.querySelector('input[type=password]'), '没有 type=password（不触发 WebKit / 密码管理器的强密码自动填充）');
  const nameI = mg.querySelector('input[name=api_key_name]'), keyI = mg.querySelector('input[name=api_key_value]'), baseI = mg.querySelector('input[name=api_key_base]');
  check(keyI && keyI.type === 'text' && getComputedStyle(keyI).webkitTextSecurity === 'disc' && keyI.getAttribute('autocomplete') === 'off',
    'API key 输入框：普通文本框 + 圆点遮挡，autocomplete=off');
  const r1 = [fxCanType(nameI, 'friend'), fxCanType(keyI, 'sk-test-x1234567'), fxCanType(baseI, 'http://127.0.0.1:4')];
  check(r1.every((x) => x === 'ok'), '管理弹窗：名称 / API key / base_url 都能获得焦点并输入（' + r1.join(' / ') + '）');
  check(baseI.placeholder === 'https://…' && [...mg.querySelectorAll('.apikey-add .field-sub')].map((e) => e.textContent).join() === '名称,API key,base_url（可选）',
    '每个输入框单独一行、有自己的标签；placeholder 只是示例（不会被看成"已经用默认地址"）');
  check(baseI.parentElement.querySelector('.field-hint').textContent.includes('settings.json'), '没有默认 base_url 时说明「留空 = 沿用 settings.json」');
  const rows = () => [...mg.querySelectorAll('.apikey-row')];
  check(rows().length === 2 && rows().every((r) => r.querySelector('.apikey-setbase')?.textContent === '修改地址'), '每条 key 都有「修改地址」入口');
  check(rows()[1].querySelector('.apikey-base').textContent === '默认地址（settings.json）' && rows()[0].querySelector('.apikey-base').textContent === OWN,
    '列表地址：自己的 base_url / 默认地址（settings.json）');
  check(calls.length === calls.filter((c) => c.gesture).length, '打开弹窗、输入都不发写请求');

  // 修改地址 → 编辑弹窗（嵌在管理弹窗里），焦点直接在 base_url
  await click(rows()[1].querySelector('.apikey-setbase'), '修改地址');
  await raf();
  const ed = topDialog();
  const edBase = ed && ed.querySelector('input[name=api_key_base]');
  check(ed && ed !== mg && mg.contains(ed) && document.activeElement === edBase && [...document.querySelectorAll('dialog[open]')].filter((d) => d.matches(':modal')).length === 1,
    '「修改地址」打开编辑弹窗（嵌套、仍只有一个模态），焦点直接在 base_url 上');
  check(mg.querySelector(':scope > .modal-form').inert && fxCanType(edBase, 'http://127.0.0.1:5') === 'ok', '编辑弹窗的 base_url 能输入；管理弹窗暂时 inert');
  edBase.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 60));
  check(!ed.isConnected && mg.open && host.open && !mg.querySelector(':scope > .modal-form').inert, 'Esc 只关最上层（编辑），管理弹窗恢复可用');
  check(fxCanType(nameI, '2') === 'ok', '关掉编辑弹窗后管理弹窗的输入框仍能输入');
  const nCalls = calls.length;
  await click(rows()[1].querySelector('.apikey-setbase'), '修改地址');
  await raf();
  const ed2 = topDialog();
  const ed2Base = ed2.querySelector('input[name=api_key_base]');
  ed2Base.value = '';
  fxCanType(ed2Base, 'http://127.0.0.1:6');
  await click([...ed2.querySelectorAll('button')].find((b) => b.textContent === '保存'), '编辑 · 保存');
  await new Promise((r) => setTimeout(r, 60));
  const pc = calls.slice(nCalls).find((c) => c.op.startsWith('apikey PATCH'));
  check(pc && pc.gesture && pc.body.base_url === 'http://127.0.0.1:6' && !ed2.isConnected, '保存地址：PATCH base_url（点击触发）并关闭编辑弹窗');
  keys[1].base_url = null;   // 改回没有地址，看默认 base_url 的显示
  await loadApiKeys(fakeApi);

  // 设置里的默认 base_url 生效：列表里没有地址的 key 显示「（默认）」
  setDefaultBaseUrl(DEF);
  await raf();
  check(rows()[1].querySelector('.apikey-base').textContent === DEF + '（默认）' && baseI.parentElement.querySelector('.field-hint').textContent.includes(DEF),
    '设置了默认 base_url：没有地址的 key 显示「…（默认）」，新增表单的说明随之更新');
  check([...pick.el.options].some((o) => o.textContent.includes(DEF + '（默认）')), '新建会话的 API 下拉里同样显示默认 base_url');
  await click([...mg.querySelectorAll('.modal-actions button')].find((b) => b.textContent === '完成'), '完成');
  await raf();
  check(!mg.isConnected && host.open && !host.querySelector(':scope > .modal-form').inert,
    '关掉管理弹窗回到新建会话对话框（不再 inert）');
  host.close();
  await raf();
  setDefaultBaseUrl('');

  // 设置弹窗：默认 base_url
  const sp = settingsDialog(fakeApi, { config: { default_base_url: '' } });
  await raf();
  const sd = topDialog();
  const sb = sd && sd.querySelector('input[name=default_base_url]');
  check(!!sb && fxCanType(sb, 'ftp://x') === 'ok', '设置里有「默认 base_url」输入框并能输入');
  const nc2 = calls.length;
  sb.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 30));
  check(calls.length === nc2 && sd.querySelector('.field-err').textContent.includes('格式不正确'), '格式不对：提示错误、不保存');
  sb.value = '';
  fxCanType(sb, DEF);
  await click([...sd.querySelectorAll('button')].find((b) => b.textContent === '保存'), '设置 · 保存默认 base_url');
  await new Promise((r) => setTimeout(r, 60));
  const cc = calls.slice(nc2).find((c) => c.op === 'apikey /api/config');
  check(cc && cc.gesture && cc.body.default_base_url === DEF && baseInfo({ base_url: null }).text === DEF + '（默认）', '保存：POST /api/config {default_base_url}（点击触发），key 的地址显示随之更新');
  check(sd.textContent.includes('不读写 ~/.claude') && sd.textContent.includes('/v1/usage'), '设置里说明默认 base_url 的用途（查询用量、启动时写进 key 文件；不碰 ~/.claude）');
  await click([...sd.querySelectorAll('.modal-actions button')].find((b) => b.textContent === '完成'), '设置 · 完成');
  await sp;
  setDefaultBaseUrl('');
  check(!document.querySelector('dialog[open]'), '对话框全部关闭');
}

// ---------- 输入框不抖动 ----------
// 与 app.js 同一个 AutoSize：镜像测量、不临时缩小输入框；时间线贴底时随输入框长高继续贴底，停在中间时 scrollTop 不动。
// 关掉 overflow-anchor 模拟 WebKit（原生应用的 WKWebView 没有滚动锚定）；两次按键之间模拟一次“有输出到达时贴底”。
const fxSizer = new AutoSize($('fx-input'), { onChange: fxPin });
function fxPin() { if (s && s.view && s.view.stick && s.view.root.isConnected) s.view.root.scrollTop = s.view.root.scrollHeight; }
$('fx-input').addEventListener('input', () => fxSizer.update());
window.addEventListener('resize', () => fxSizer.update());
if (typeof ResizeObserver === 'function') new ResizeObserver(fxPin).observe($('fx-host'));

// ---------- 输入框 ↑/↓ 历史 ----------
// 与 app.js 同一套 prompthist.js + AutoSize.caretEdges；列表 = 本页时间线里的用户消息（+ 模拟的“已发出、还没写进 transcript”）。
let fxNav = new PromptNav();
let fxPending = [];
$('fx-input').addEventListener('keydown', (e) => {
  const dir = historyDir(e, false);
  if (!dir || !s) return;
  const text = recallInto($('fx-input'), dir, fxSizer, fxNav, () => {
    const r = mergePending(promptsFromItems(s.items), fxPending);
    fxPending = r.pending;
    return r.list;
  });
  if (text == null) return;
  e.preventDefault();
  fxSizer.update();
});

async function recallChecks() {
  const ta = $('fx-input');
  const n0 = calls.length;
  ta.disabled = false;
  ta.focus();
  fxNav = new PromptNav();
  fxPending = [];
  const P = promptsFromItems(s.items);
  const P1 = '用 Bash 执行 touch /tmp/ccr_tui_probe_denied2 ，如果被拒绝就直接告诉我';
  const P2 = '请用 Write 工具在 /mnt/ssd/home/demouser/.ccr_probe_write_denied.txt 写入 hi，被拒绝就直接告诉我';
  check(P.length === 2 && P[0] === P1 && P[1] === P2, '历史列表 = 时间线上的 2 条用户消息（不含 /effort 命令、caveat、中断标记）');
  const set = (v, pos = v.length, end = pos) => { ta.value = v; ta.setSelectionRange(pos, end); fxSizer.update(); };
  const key = (k, opt = {}) => !ta.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...opt }));
  // 调出的消息可能折成几行（光标在开头）：真实按键会先把光标移到最后一行；合成事件没有默认动作，这里直接把光标放到末尾
  const down = (opt) => { ta.setSelectionRange(ta.value.length, ta.value.length); return key('ArrowDown', opt); };

  set('');
  check(key('ArrowUp') && ta.value === P2 && ta.selectionStart === 0, '空输入框按 ↑：放入最近一条，光标在开头');
  check(key('ArrowUp') && ta.value === P1, '再按 ↑：更早一条');
  check(!key('ArrowUp') && ta.value === P1, '已是最早一条：↑ 交给浏览器（不改内容）');
  check(down() && ta.value === P2 && ta.selectionStart === P2.length, '↓：下一条，光标在末尾');
  check(down() && ta.value === '', '↓ 翻过最新一条：回到原来的（空）草稿');
  check(!key('ArrowDown') && ta.value === '', '没在浏览时 ↓ 不处理');

  set('写了一半的草稿');
  check(key('ArrowUp') && ta.value === P2, '有草稿、光标在第一行：↑ 调出最近一条');
  check(down() && ta.value === '写了一半的草稿' && ta.selectionStart === ta.value.length, '↓ 回来：草稿原样恢复');

  set('');
  key('ArrowUp');
  set(P2 + ' 改一下');
  check(!down() && ta.value === P2 + ' 改一下', '改过调出来的内容：退出浏览，↓ 不处理（不丢字）');
  ta.setSelectionRange(0, 0);   // 改后的文字可能折成两行：光标先回到第一行
  check(key('ArrowUp') && ta.value === P2 && down() && ta.value === P2 + ' 改一下', '再按 ↑ 从最新一条开始，↓ 回到改后的文字');

  set('第一行\n第二行\n第三行', 6);
  check(!key('ArrowUp') && ta.value.startsWith('第一行'), '多行内容、光标不在第一行：↑ 只移动光标');
  set('第一行\n第二行\n第三行', 2);
  check(key('ArrowUp') && ta.value === P2, '多行内容、光标在第一行：↑ 调出历史');
  check(down() && ta.value === '第一行\n第二行\n第三行', '↓ 回到多行草稿');

  // 自动折行：一个逻辑行折成多个视觉行，光标在后面的视觉行时 ↑ 只移动光标
  const long = '很长的一段话会自动折行 '.repeat(30).trim();
  set(long);
  const wrapped = ta.offsetHeight > 50;
  const e1 = fxSizer.caretEdges(long.length), e0 = fxSizer.caretEdges(3);
  check(wrapped && !e1.first && e1.last && e0.first && !e0.last, '自动折行：caretEdges 区分第一 / 最后视觉行（末尾 ' + JSON.stringify(e1) + '，开头 ' + JSON.stringify(e0) + '）');
  check(!key('ArrowUp') && ta.value === long, '折行后光标在最后一个视觉行：↑ 只移动光标');
  set(long, 3);
  check(key('ArrowUp') && ta.value === P2, '折行后光标在第一个视觉行：↑ 调出历史');
  // 中文没有空格：一整段中文折成几行，光标在第一个视觉行时 ↑ 也要调出历史（不能把光标后面整段都当成一个词）
  const zh = '这是一段没有空格的中文长句子，用来检查自动折行时的第一行判断。'.repeat(8);
  ta.value = '';
  fxNav = new PromptNav();
  set(zh, 3);
  const z0 = fxSizer.caretEdges(3), z1 = fxSizer.caretEdges(zh.length);
  check(ta.offsetHeight > 50 && z0.first && !z0.last && !z1.first && z1.last, '中文长段折行：caretEdges 第一 / 最后视觉行判断正确（开头 ' + JSON.stringify(z0) + '，末尾 ' + JSON.stringify(z1) + '）');
  check(key('ArrowUp') && ta.value === P2, '中文长段、光标在第一个视觉行：↑ 调出历史');
  ta.value = '';
  fxNav = new PromptNav();

  set('abc', 0, 2);
  check(!key('ArrowUp') && ta.value === 'abc', '选中了文字：↑ 不处理');
  set('');
  check(!key('ArrowUp', { shiftKey: true }) && !key('ArrowUp', { altKey: true }) && !key('ArrowUp', { metaKey: true }) && ta.value === '', 'Shift / Alt / ⌘ + ↑ 不处理');
  check(!key('ArrowUp', { isComposing: true }) && !key('ArrowUp', { keyCode: 229 }) && ta.value === '', '输入法组字中的 ↑ 不处理');
  ta.disabled = true;
  check(!key('ArrowUp') && ta.value === '', '输入框禁用（有对话框 / 已退出）时不处理');
  ta.disabled = false;

  // 已发出、transcript 里还没出现的消息（运行中排队）：排在最后；写进 transcript 后不重复
  fxPending = [{ text: '排队中的消息', n: P.length }];
  set('');
  check(key('ArrowUp') && ta.value === '排队中的消息' && key('ArrowUp') && ta.value === P2, '已发出但还没写进 transcript 的消息排在最前（最近）');
  fxNav = new PromptNav();
  pushTranscript([{ type: 'user', uuid: 'fx-recall-q', timestamp: new Date().toISOString(), message: { role: 'user', content: '排队中的消息' } }], false);
  await raf(); flushNow(s);
  set('');
  key('ArrowUp'); const a1 = ta.value; key('ArrowUp'); const a2 = ta.value;
  check(a1 === '排队中的消息' && a2 === P2 && fxPending.length === 0, '写进 transcript 后只出现一次，待出现列表清空');
  ta.value = '';
  fxNav = new PromptNav();
  fxSizer.update();
  check(calls.length === n0, '↑/↓ 历史不发任何请求');
  pushTranscript(transcriptEntries(), true);   // 去掉本节补的消息，后面各节照旧
  await raf(); flushNow(s);
}

// ---------- 左侧 prompt 小格子 ----------
// 与 app.js 同一个 minimap.js（PromptMinimap 挂在 #fx-host，onFlush 钩子来自 render.js）。临时补 200 轮消息（400 个条目，
// 前 100 个不在首屏渲染窗口里）检查跳转 / 补渲染；合成鼠标、键盘、滚轮事件直接发给小格子。
function mmTranscript(nTurns) {
  const out = [];
  const ts = new Date().toISOString();
  for (let k = 0; k < nTurns; k++) {
    out.push({ type: 'user', uuid: 'mm-u' + k, timestamp: ts, message: { role: 'user', content: `第 ${k} 轮提问：请解释一下 **第 ${k} 个** 话题` } });
    out.push({ type: 'assistant', uuid: 'mm-a' + k, timestamp: ts, message: { id: 'mm-m' + k, role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text',
      text: `## 第 ${k} 轮回答\n\n- 要点一：\`code_${k}\`\n- 要点二：*斜体*说明\n\n这一轮的回答比较长，用来撑高时间线。` }] } });
  }
  return out;
}

async function minimapChecks() {
  const mm = fxMinimap;
  const host = $('fx-host');
  const ta = $('fx-input');
  const n0 = calls.length;
  const root = s.view.root;
  const settle = async () => {   // 等平滑滚动停下（连续 8 帧 scrollTop 不变）
    let last = -1, same = 0;
    for (let i = 0; i < 240 && same < 8; i++) { await raf(); if (root.scrollTop === last) same++; else { same = 0; last = root.scrollTop; } }
    await raf();
  };
  const toBottom = async () => { root.scrollTop = root.scrollHeight; s.view.stick = true; s.view.jump.hidden = true; await raf(); await raf(); };
  const n = () => mm.entries.length;
  const yOf = (i) => { const r = mm.btn.getBoundingClientRect(); return r.top + (topPercent(i, n()) / 100) * r.height; };
  const mev = (el, type, y, opt = {}) => {
    const r = el.getBoundingClientRect();
    const e = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: r.left + 4, clientY: y, buttons: 0, ...opt });
    el.dispatchEvent(e);
    return e;
  };
  const clickAt = async (i) => {
    const y = yOf(i);
    mev(mm.btn, 'mousemove', y);
    const d = mev(mm.btn, 'mousedown', y, { buttons: 1 });
    mev(mm.btn, 'mouseup', y);
    mev(mm.btn, 'click', y, { detail: 1 });
    await settle();
    return d;
  };
  const userTop = (i) => Math.round(mm.entries[i].item.el.getBoundingClientRect().top - root.getBoundingClientRect().top);
  const near24 = (i) => Math.abs(userTop(i) - MM_TARGET_OFFSET) <= 2;
  const key = (k) => mm.btn.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
  const inView = () => mm.strips.map((x) => x.dataset.inView === 'true');

  // 回放 transcript：2 条真实提示
  check(host.querySelectorAll('.pmap').length === 1 && !root.querySelector('.pmap'), '#chat-host 里只有一个小格子覆盖层（不在时间线里）');
  check(!mm.root.hidden && mm.strips.length === qa('.k-user').length && mm.strips.length === 2, '条目数 = 时间线用户消息数（' + mm.strips.length + '）');
  check(Math.round(mm.rail.getBoundingClientRect().height) === 8, '2 条时高度 = (2-1)×8px');
  const one = [{ type: 'user', uuid: 'mm-one', timestamp: new Date().toISOString(), message: { role: 'user', content: '只有一条' } }];
  pushTranscript(one, true); await raf(); flushNow(s); await raf();
  check(mm.root.hidden, '少于 2 条用户消息时隐藏');

  // 200 轮
  pushTranscript(mmTranscript(200), true); await raf(); flushNow(s);
  await toBottom();
  const N = n();
  check(!mm.root.hidden && N === 200 && mm.strips.length === 200 && s.renderStart === 100, '200 轮：200 格（前 100 个条目不在首屏渲染窗口里，renderStart=' + s.renderStart + '）');
  const tops = mm.strips.map((x) => parseFloat(x.style.top));
  check(tops[0] === 0 && tops[199] === 100 && Math.abs(tops[100] - 100 * 100 / 199) < 1e-3 && tops.every((v, i) => !i || v > tops[i - 1]), 'top% 从 0 到 100 均匀分布');
  const hostH = host.getBoundingClientRect().height, railR = mm.rail.getBoundingClientRect();
  check(Math.abs(railR.height - Math.max(64, hostH - 144)) <= 1 && Math.abs(railR.top + railR.height / 2 - (host.getBoundingClientRect().top + hostH / 2)) <= 1,
    '高度 = min(199×8px, 时间线高 − 9rem) 且竖向居中（' + Math.round(railR.height) + ' / ' + Math.round(hostH) + '）');

  // 常驻（回放页 .main 约 1060px 宽：留白 ≈ 130 + 28）
  const hr = host.getBoundingClientRect();
  check(mm.root.dataset.persistent === 'true' && getComputedStyle(mm.root).opacity === '1' && getComputedStyle(mm.root).display !== 'none',
    '宽：左侧留白 ' + Math.round(mm.gutter) + 'px ≥ 48 → 常驻显示');
  check(Math.round(railR.left - hr.left) === 12 && Math.round(railR.width) === 40, '宽：小格子在左边 12px、命中宽 40px');
  const cs = getComputedStyle(mm.root);
  check(cs.position === 'absolute' && cs.zIndex === '4' && cs.pointerEvents === 'none', '覆盖层：absolute、z-index 4（低于抽屉 40 / 弹层）、自身不接收指针');
  // 不改变时间线布局
  const colKey = () => { const r = s.view.col.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.width), root.scrollHeight, root.clientWidth].join(','); };
  const withMm = colKey();
  mm.root.hidden = true; await raf();
  const withoutMm = colKey();
  mm.root.hidden = false; await raf();
  check(withMm === withoutMm, '小格子不改变 .chat-col 位置 / 宽度和时间线 scrollHeight（' + withMm + '）');

  // 在底部：最后几轮加深、最前面的不加深；下一轮禁用
  await raf();
  let iv = inView();
  check(iv[199] && !iv[0] && !iv[150] && iv.filter(Boolean).length < 12, '在底部：最后几轮 data-in-view=true，其它为 false（' + iv.filter(Boolean).length + ' 格）');
  check(mm.nextBtn.disabled && !mm.prevBtn.disabled, '在底部：「下一轮」禁用、「上一轮」可用');

  // 悬停：鱼眼 + 预览卡
  mev(mm.btn, 'mousemove', yOf(120));
  check(mm.active === 120, '悬停：指针所在那一格高亮（120）');
  const dd = [117, 118, 119, 120, 121, 122, 123].map((i) => mm.strips[i].dataset.d ?? '-').join('');
  check(dd === '-21012-', '鱼眼：距离 0 / 1 / 2 → data-d，更远不写（' + dd + '）');
  await sleep(220);   // 等 .15s 过渡
  const sx = (i) => new DOMMatrixReadOnly(getComputedStyle(mm.strips[i]).transform).a;
  const scales = [120, 121, 122, 123, 119].map((i) => Math.round(sx(i) * 1000) / 1000);
  check(scales.join() === '1,0.667,0.417,0.333,0.667', 'scaleX：1 / .667 / .417 / .333（' + scales.join(' / ') + '）');
  const card = mm.card, cr = card.getBoundingClientRect();
  check(!card.hidden && mm.cardUser.textContent === '第 120 轮提问：请解释一下 第 120 个 话题' &&
    mm.cardText.textContent === '第 120 轮回答 要点一：code_120 要点二：斜体说明 这一轮的回答比较长，用来撑高时间线。',
    '预览卡：用户文字一行 + 该轮最后一段助手文字（纯文本，去掉 markdown 记号）');
  const sr = mm.strips[120].getBoundingClientRect();
  check(Math.abs(cr.top + cr.height / 2 - (sr.top + sr.height / 2)) <= 1.5 && Math.round(cr.left - railR.left) === 32 && getComputedStyle(card).pointerEvents === 'none',
    '预览卡在小格子右侧 32px、以悬停那一格为中线、不接收指针');
  check(cr.right <= hr.right && document.documentElement.scrollWidth <= document.documentElement.clientWidth && getComputedStyle(mm.cardText).webkitLineClamp === '3',
    '预览卡不出时间线右边、不撑出横向滚动；助手文字最多 3 行');
  mev(mm.btn, 'mousemove', yOf(0));
  const c0 = card.getBoundingClientRect();
  mev(mm.btn, 'mousemove', yOf(199));
  const c1 = card.getBoundingClientRect();
  check(c0.top >= hr.top && c1.bottom <= hr.bottom, '两端那一格的预览卡也在时间线区域内（不压到会话头 / 输入框）');
  mev(mm.btn, 'mouseleave', yOf(199));
  check(mm.active === null && card.hidden && !mm.strips.some((x) => x.dataset.d), '移开：高亮与预览卡消失');

  // 补渲染（renderStart 之前的条目）：内容在屏幕上不动
  root.style.overflowAnchor = 'none';   // 模拟 WebKit（没有滚动锚定）：位置全靠 renderEarlier 自己补
  root.scrollTop = Math.round(root.scrollHeight / 2); s.view.stick = false; await raf();
  const anchor = s.items[300].el, aTop = anchor.getBoundingClientRect().top;
  check(revealItem(s, s.items[60]) && s.renderStart === 60 && !!s.items[60].el && s.items[60].el.isConnected, 'revealItem：从目标条目开始补渲染（renderStart 100 → 60）');
  check(Math.abs(anchor.getBoundingClientRect().top - aTop) <= 1 && !s.view.more.hidden && /共 60 条/.test(s.view.more.textContent), '补渲染保持滚动位置、「显示更早」计数更新');
  root.style.overflowAnchor = '';
  pushTranscript(mmTranscript(200), true); await raf(); flushNow(s);
  await toBottom();

  // 点击跳转（已渲染）：目标顶端在视口顶端下方 24px，不抢输入框焦点
  ta.focus();
  const md = await clickAt(150);
  check(md.defaultPrevented && document.activeElement === ta, '点击：mousedown 被拦（不抢输入框焦点）');
  check(near24(150), '点击已渲染的第 150 条：消息顶端在视口顶端下方 ' + userTop(150) + 'px（期望 24）');
  check(!s.view.stick && s.view.jump.hidden, '跳离底部：不再贴底（新内容不会把视图拉回底部）');
  iv = inView();
  check(iv[150] && !iv[199] && !iv[0] && mm.current === 150, '跳转后：第 150 轮加深、底部那几轮不再加深，当前下标 = 150');
  pushTranscript([{ type: 'assistant', uuid: 'mm-late', timestamp: new Date().toISOString(), message: { id: 'mm-late', role: 'assistant', content: [{ type: 'text', text: '新到的回复' }] } }], false);
  await raf(); flushNow(s); await raf();
  check(near24(150) && !s.view.jump.hidden, '跳转后有新内容：位置不动、出现「最新」按钮');

  // 上一轮 / 下一轮
  mm.nextBtn.click(); await settle();
  check(near24(151) && mm.current === 151, '「下一轮」→ 第 151 条到视口顶端下方 24px');
  mm.prevBtn.click(); await settle(); mm.prevBtn.click(); await settle();
  check(near24(149) && mm.current === 149, '「上一轮」×2 → 第 149 条');

  // 点击 renderStart 之前、还没渲染的条目
  check(s.renderStart > 6 && !s.items[6].el, '第 3 条用户消息（条目 6）还没渲染');
  await clickAt(3);
  check(s.renderStart <= 6 && s.items[6].el && s.items[6].el.isConnected && near24(3), '点击未渲染的第 3 条：先补渲染再跳转，顶端在视口下方 ' + userTop(3) + 'px');
  check(mm.current === 3 && inView()[3] && !inView()[150], '可见轮次随滚动更新（第 3 轮加深）');
  await clickAt(0);
  check(userTop(0) >= 0 && mm.current === 0 && mm.prevBtn.disabled && !mm.nextBtn.disabled, '跳到第一条：「上一轮」禁用');

  // 键盘：↑/↓/Home/End 移动高亮，Enter 跳转，失焦清除；不影响输入框
  ta.value = '草稿不变'; fxSizer.update();
  mev(mm.btn, 'mouseleave', 0);
  // 没有系统焦点的 headless 页面里 focus() / blur() 不派发 focus / blur 事件：这时补发一个（处理函数是幂等的）
  const sysFocus = document.hasFocus();
  mm.btn.focus();
  if (!sysFocus) mm.btn.dispatchEvent(new FocusEvent('focus'));
  check(document.activeElement === mm.btn && mm.active === 0 && !card.hidden, '键盘聚焦：高亮当前那一格（0）并显示预览卡');
  key('ArrowDown'); key('ArrowDown');
  const a2 = mm.active;
  key('End'); const aEnd = mm.active;
  key('ArrowUp'); const aUp = mm.active;
  key('Home'); const aHome = mm.active;
  check(a2 === 2 && aEnd === 199 && aUp === 198 && aHome === 0, '↓↓ → 2、End → 199、↑ → 198、Home → 0');
  key('ArrowDown'); key('ArrowDown'); key('ArrowDown');
  const ent = !mm.btn.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await settle();
  check(ent && near24(3) && ta.value === '草稿不变', 'Enter：跳到高亮的第 3 条；输入框内容不变');
  const focused = document.activeElement === mm.btn;
  mm.btn.blur();
  if (!sysFocus) mm.btn.dispatchEvent(new FocusEvent('blur'));
  check(focused && mm.active === null && card.hidden, '失焦：清除高亮');
  ta.value = ''; fxSizer.update();

  // 滚轮转发给时间线
  root.scrollTop = 4000; await raf();
  const w0 = root.scrollTop;
  const we = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 120, deltaMode: 0, clientX: railR.left + 4, clientY: yOf(100) });
  mm.btn.dispatchEvent(we);
  await sleep(260);
  check(we.defaultPrevented && Math.abs(root.scrollTop - w0 - 120) <= 1, '滚轮（像素）：转发给时间线 +' + (root.scrollTop - w0) + 'px');
  const w1 = root.scrollTop;
  mm.btn.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -3, deltaMode: 1 }));
  await sleep(260);
  check(Math.abs(root.scrollTop - w1 + 48) <= 1, '滚轮（按行，deltaMode=1）：3 行 = ' + (root.scrollTop - w1) + 'px');

  // 从别处开始的拖动（选文字）经过：穿透，不高亮
  mev(mm.rail, 'mousemove', yOf(50), { buttons: 1 });
  mev(mm.btn, 'mousemove', yOf(50), { buttons: 1 });
  check(mm.rail.classList.contains('pass') && getComputedStyle(mm.rail).pointerEvents === 'none' && mm.active === null, '按着键从别处拖过来：小格子穿透（pointer-events:none）、不高亮');
  window.dispatchEvent(new MouseEvent('mouseup', { buttons: 0 }));
  check(!mm.rail.classList.contains('pass') && getComputedStyle(mm.rail).pointerEvents === 'auto', '松开后恢复');

  // 窄：留白 < 48 → 平时隐藏，只在精确指针悬停时淡入
  const main = document.querySelector('.fx-main .main');
  main.style.flex = '0 0 760px';
  await raf(); await raf(); await sleep(220);   // 等 .15s 淡出
  const fine = matchMedia('(pointer: fine)').matches;
  const ncs = getComputedStyle(mm.root);
  check(mm.root.dataset.persistent === 'false' && ncs.opacity === '0' && ncs.display === (fine ? 'block' : 'none'),
    '窄（760px，留白 ' + Math.round(mm.gutter) + 'px；' + [ncs.opacity, ncs.display].join() + '）：平时透明' + (fine ? '，精确指针下可悬停淡入' : '，非精确指针不显示'));
  check(Math.round(mm.rail.getBoundingClientRect().width) === Math.max(0, Math.min(40, Math.floor(mm.gutter) - 12)) && !mm.rail.classList.contains('nav-off'),
    '窄：命中宽 = clamp(留白 − 12, 0, 40) = ' + Math.round(mm.rail.getBoundingClientRect().width) + 'px，导航可点');
  const nKey = colKey();
  mm.root.hidden = true; await raf();
  check(colKey() === nKey, '窄：小格子同样不改变时间线布局');
  mm.root.hidden = false;
  main.style.flex = '';
  await raf(); await raf(); await raf();
  check(mm.root.dataset.persistent === 'true', '恢复宽度后重新常驻（ResizeObserver 重新测量）');

  // 颜色：浅色 / 深色都看得见，可见轮次明显更深
  const th = window.ccrTheme;
  const prevPref = th ? th.pref() : 'system';
  const colors = () => {
    const bg = rgbaOf(getComputedStyle(main).backgroundColor);
    const strip = mm.strips[0];
    const base = over(rgbaOf(getComputedStyle(strip).backgroundColor), bg);
    const lit = over(rgbaOf(getComputedStyle(strip, '::after').backgroundColor), base);
    const cardBg = over(rgbaOf(getComputedStyle(mm.card.firstChild).backgroundColor), bg);
    return { base: contrast(base, bg), lit: contrast(lit, bg), text: contrast(rgbaOf(getComputedStyle(mm.cardUser).color), cardBg),
      vars: ['--pmap-strip', '--pmap-strip-hot', '--pmap-strip-inview', '--pmap-card-bg', '--pmap-card-border'].map((v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim()) };
  };
  const res = {};
  for (const t of ['light', 'dark']) {
    if (th) th.set(t, { persist: false });
    await raf(); await raf();
    res[t] = colors();
  }
  if (th) th.set(prevPref, { persist: false });
  await raf();
  const f2 = (x) => x.toFixed(2);
  for (const t of ['light', 'dark']) {
    const c = res[t];
    check(c.vars.every(Boolean) && c.base >= 1.3 && c.lit >= 3 && c.text >= 7,
      (t === 'light' ? '浅色' : '深色') + '：横条对背景 ' + f2(c.base) + ':1、可见轮次 ' + f2(c.lit) + ':1、预览卡文字 ' + f2(c.text) + ':1');
  }
  check(res.light.vars.join() !== res.dark.vars.join(), '--pmap-* 变量浅色 / 深色取值不同');

  // 跟随会话：没有会话（空白页 / showEmpty）时隐藏，回来再显示
  fxMinimap.setSession(null);
  check(mm.root.hidden, '没有当前会话时隐藏');
  fxMinimap.setSession(s);
  check(!mm.root.hidden && mm.strips.length === 200, '重新指向会话后恢复');

  check(calls.length === n0, '小格子不发任何请求（悬停 / 点击 / 键盘 / 滚轮）');
  pushTranscript(transcriptEntries(), true);   // 去掉本节补的消息，后面各节照旧
  await raf(); flushNow(s);
  await toBottom();
}

async function typingChecks() {
  const ta = $('fx-input');
  const root = s.view.root;
  const keep = [root, ta, document.documentElement, document.body].map((el) => [el, el.style.overflowAnchor]);
  for (const [el] of keep) el.style.overflowAnchor = 'none';
  // 时间线不够长时补几条用户消息，保证能滚动
  for (let k = 0; root.scrollHeight < root.clientHeight * 2.5 && k < 40; k++) {
    pushTranscript([{ type: 'user', uuid: 'fx-typing-' + k, timestamp: new Date().toISOString(), message: { role: 'user', content: '补充的较长消息 ' + k + '：'.padEnd(200, '填') } }], false);
    await raf(); flushNow(s);
  }
  const dist = () => Math.round(root.scrollHeight - root.scrollTop - root.clientHeight);
  const max = () => Math.round(innerHeight * 0.4);
  const setVal = async (v) => { ta.value = v; ta.setSelectionRange(v.length, v.length); ta.dispatchEvent(new Event('input', { bubbles: true })); await raf(); await raf(); };
  const type = (ch) => {
    ta.focus();
    if (!document.execCommand('insertText', false, ch)) { ta.setRangeText(ch, ta.selectionStart, ta.selectionEnd, 'end'); ta.dispatchEvent(new Event('input', { bubbles: true })); }
  };
  const text = '输入框超过一行以后，每次输入都不应该让时间线或滚动条上下跳动。'.repeat(3) + '\n第二段 Shift+Enter 换行后继续输入。\n第三段 abc def ghi 0123456789';
  ta.disabled = false;
  check(root.scrollHeight > root.clientHeight * 1.5, '时间线足够长可以滚动（' + root.scrollHeight + ' / ' + root.clientHeight + '）');

  // 贴底：每次按键前若贴底就滚到底（模拟输出到达），按键后不得往下掉、仍贴底
  await setVal('');
  root.scrollTop = root.scrollHeight; await raf(); await raf();
  let drops = 0, off = 0, bars = 0, grows = 0;
  for (const ch of text) {
    if (dist() < 60) { root.scrollTop = root.scrollHeight; await raf(); }
    const t0 = root.scrollTop, h0 = ta.offsetHeight;
    type(ch); await raf();
    if (root.scrollTop < t0) drops++;
    if (dist() > 1) off++;
    if (ta.offsetHeight !== h0) grows++;
    if (ta.offsetHeight < max() && (ta.offsetWidth - ta.clientWidth > 0 || ta.scrollTop !== 0 || getComputedStyle(ta).overflowY !== 'hidden')) bars++;
  }
  check(drops === 0 && off === 0, `贴底打字 ${[...text].length} 字：时间线回跳 ${drops} 次、离开底部 ${off} 次（输入框长高 ${grows} 次）`);
  check(bars === 0, '未到最大高度时输入框没有滚动条 / 内部偏移（' + bars + ' 次）');

  // 停在中间：scrollTop 全程不变
  await setVal('');
  root.scrollTop = Math.round((root.scrollHeight - root.clientHeight) / 2); await raf(); await raf();
  const mid = root.scrollTop;
  let moved = 0;
  for (const ch of text) { type(ch); await raf(); if (root.scrollTop !== mid) moved++; }
  check(moved === 0, '停在中间打字：时间线 scrollTop 变化 ' + moved + ' 次');

  // 最大高度：高度与时间线都不动，输入框自己滚动
  await setVal(Array.from({ length: 40 }, (_, i) => '预填第 ' + i + ' 行').join('\n'));
  root.scrollTop = root.scrollHeight; await raf(); await raf();
  // 光标放到末尾后重新聚焦 → 浏览器把光标滚进视野（无头模式下已聚焦时设光标不会滚动）
  ta.blur(); ta.setSelectionRange(ta.value.length, ta.value.length); ta.focus();
  await raf();
  const capH = ta.offsetHeight, capTop = root.scrollTop, taTop0 = ta.scrollTop;
  check(capH === max() && getComputedStyle(ta).overflowY === 'auto', '超长内容：输入框封顶 40% 视口（' + capH + 'px）且可滚动');
  let capMoves = 0;
  let taBack = 0;
  for (const ch of '最大高度时继续打字') {
    const tt = ta.scrollTop;
    type(ch); await raf();
    if (ta.offsetHeight !== capH || root.scrollTop !== capTop) capMoves++;
    if (ta.scrollTop < tt) taBack++;
  }
  check(capMoves === 0 && taBack === 0 && taTop0 > 0, `最大高度时打字：高度与时间线都不动（${capMoves} 次），输入框内部没有往回滚（${taBack} 次，scrollTop ${Math.round(taTop0)}→${Math.round(ta.scrollTop)}）`);

  // 发送后清空：回到一行，仍贴底
  await setVal('');
  check(ta.offsetHeight < 40 && getComputedStyle(ta).overflowY === 'hidden' && dist() <= 1, '清空后回到一行（' + ta.offsetHeight + 'px）且仍贴底');

  // 输入框上方的横幅出现 / 消失：贴底保持贴底，中间保持不动
  const b = $('fx-banner');
  const bHidden = b.hidden, bText = b.textContent;
  root.scrollTop = root.scrollHeight; await raf(); await raf();
  b.textContent = '模拟提示条'; b.hidden = false; await raf(); await raf();
  const dShown = dist();
  b.hidden = true; await raf(); await raf();
  const dHidden = dist();
  root.scrollTop = Math.round((root.scrollHeight - root.clientHeight) / 2); await raf(); await raf();
  const m0 = root.scrollTop;
  b.hidden = false; await raf(); await raf();
  const m1 = root.scrollTop;
  b.hidden = true; await raf(); await raf();
  check(dShown <= 1 && dHidden <= 1 && m1 === m0 && root.scrollTop === m0, '横幅出现/消失：贴底时仍贴底，停在中间时不动');
  b.hidden = bHidden; b.textContent = bText;
  for (const [el, v] of keep) el.style.overflowAnchor = v;
  await setVal('');
}

// ---------- 统计条 ----------
// 后端从屏幕/transcript 汇总 meta.stats（Python 单测覆盖计算）；这里用与真实 fixtures 一致的值检查展示。
async function statsChecks() {
  const bar = statsBar.el;
  const part = (k) => bar.querySelector('.st-' + k);
  const txt = (k) => part(k).textContent;
  const vis = (el) => !!el && !el.hidden && getComputedStyle(el).display !== 'none';
  const ring = () => part('ctx').querySelector('.st-ring');
  const ringOffset = () => parseFloat(part('ctx').querySelector('.st-ring-fill').style.strokeDashoffset);
  const nCalls = calls.length;
  pushMeta({ state: 'idle', stats: null });
  show('stream:after_stream');
  await raf();
  check(bar.hidden, '没有 meta.stats 时统计条隐藏');
  // stream:after_stream 的状态栏 "3% 25k/1M $0.0035" + 上一轮（spinner 最终 211 tokens / 6s）
  pushMeta({ stats: { context_used: 25000, context_limit: 1000000, context_pct: 3, context_source: 'status', context_approx: false,
    cost_usd: 0.0035, cost_text: '$0.0035', cost_source: 'status', tok_s_live: null, tok_s_last: 35.2, last_turn_tokens: 211, last_turn_elapsed_s: 6, updated_at: Date.now() / 1000 } });
  await raf();
  check(!bar.hidden && bar.isConnected && bar.parentElement === $('fx-stats') && bar.closest('.composer-bar'), '统计在输入框底栏右侧的固定槽位');
  check(vis(ring()) && ring().tagName.toLowerCase() === 'svg' && part('ctx').querySelector('.st-pct').textContent === '3%', '上下文 = 圆环 + 3%');
  check(Math.abs(ringOffset() - RING_C * 0.97) < 0.01 && part('ctx').querySelector('.st-ring-fill').getAttribute('stroke-dasharray') === RING_C.toFixed(3),
    '圆环 dashoffset 与百分比一致（' + ringOffset().toFixed(2) + ' = 周长 × 97%）');
  check(!txt('ctx').includes('上下文') && !txt('ctx').includes('25k') && /25k \/ 1M/.test(part('ctx').title) && /25,000 \/ 1,000,000/.test(part('ctx').title),
    '「上下文」与 25k / 1M 挪进 tooltip（界面只显示百分比）');
  check(part('ctx').getAttribute('aria-label').startsWith('上下文已用 3%'), 'aria-label：上下文已用 3%');
  check(!vis(part('ctx').querySelector('.st-approx')) && /状态栏/.test(part('ctx').title), '状态栏来源：不标“约”，tooltip 说明来源');
  check(!part('ctx').classList.contains('warn'), '≤80% 不是警告色');
  check(txt('cost').includes('$0.0035') && /Claude Code 状态栏/.test(part('cost').title), '花费 $0.0035（tooltip：来自 Claude Code 状态栏）');
  check(vis(part('rate')) && txt('rate').includes('35 tok/s') && /上一轮/.test(part('rate').title) && /上一轮/.test(part('rate').getAttribute('aria-label')) && !part('rate').classList.contains('live'),
    '空闲：35 tok/s（「上一轮」在 tooltip / aria-label 里）');
  check(/211 tokens/.test(part('rate').title) && /6s/.test(part('rate').title), '上一轮 tooltip 给出口径（211 tokens ÷ 6s）');
  // 忙：生成中速率
  show('stream:busy_count');
  pushMeta({ stats: { ...s.meta.stats, tok_s_live: 46.3 } });
  await raf();
  check(part('rate').classList.contains('live') && txt('rate').includes('46 tok/s') && !/上一轮/.test(part('rate').getAttribute('aria-label')), '生成中 46 tok/s');
  check(vis(part('rate').querySelector('.st-dot')) && /生成中/.test(part('rate').title) && /thinking/.test(part('rate').title), '生成中：脉冲点 + tooltip 说明口径（含 thinking）');
  pushMeta({ stats: { ...s.meta.stats, tok_s_live: null } });
  await raf();
  check(part('rate').classList.contains('pending') && txt('rate').includes('— tok/s'), '生成中但 spinner 还没有 token 数：— tok/s');
  // 后备：transcript usage（约）+ 没有花费
  show('stream:interrupted');
  pushMeta({ stats: { context_used: 28303, context_limit: 200000, context_pct: 14.2, context_source: 'transcript', context_approx: true,
    cost_usd: null, cost_text: null, cost_source: null, tok_s_live: null, tok_s_last: null } });
  await raf();
  check(vis(part('ctx').querySelector('.st-approx')) && txt('ctx').includes('14%') && /约 14% · 28.3k \/ 200k/.test(part('ctx').title), '后备：约 14%（tooltip：约 14% · 28.3k / 200k）');
  check(/transcript/.test(part('ctx').title) && /200,000/.test(part('ctx').title), '后备 tooltip：transcript usage + 上限推断');
  // 窗口来源（ccr/stats.py 的 context_limit_source）：用户日志里的 214,632 tokens（Opus 5.5 原生 1M）→ 约 21%，不是 107%
  pushMeta({ stats: { context_used: 214632, context_limit: 1000000, context_pct: 21.5, context_source: 'transcript', context_approx: true,
    context_limit_source: 'model', context_model: 'Opus 5.5', cost_usd: null, cost_text: null, cost_source: null } });
  await raf();
  check(txt('ctx').includes('21%') && !part('ctx').classList.contains('warn') && Math.abs(ringOffset() - RING_C * (1 - 0.214632)) < 0.01
    && part('ctx').title.split('\n')[2] === '窗口 1M：按模型 Opus 5.5 推断（Claude Code 2.1.331 内置模型目录）',
    'Opus 5.5 的 214,632 tokens：约 21%（tooltip 第 3 行写出窗口来源：按模型推断）');
  for (const [src, model, re] of [['context_cmd', 'Haiku 5.5', /窗口 1M：来自 \/context（Haiku 5\.5）/], ['hint', null, /窗口 1M：模型名 \/ 状态栏/],
    ['observed', 'gw-model', /窗口 1M：本会话 gw-model 的上下文曾超过 200k/]]) {
    pushMeta({ stats: { ...s.meta.stats, context_limit_source: src, context_model: model } });
    await raf();
    check(re.test(part('ctx').title), '窗口来源 ' + src + '：' + part('ctx').title.split('\n')[2]);
  }
  // 不在 CLI 模型目录里的模型（第三方网关的叫法）：与 CLI 一致按 1M
  pushMeta({ stats: { ...s.meta.stats, context_used: 214632, context_limit: 1000000, context_pct: 21.5, context_limit_source: 'unknown_model', context_model: 'gw-opus' } });
  await raf();
  check(txt('ctx').includes('21%') && part('ctx').title.split('\n')[2] === '窗口 1M：模型 gw-opus 不在 Claude Code 2.1.331 内置模型目录里，按 Claude Code 的默认 1M 计；运行一次 /context 可得到准确值',
    '不在模型目录里的模型：约 21%（tooltip：按 Claude Code 的默认 1M 计）');
  pushMeta({ stats: { ...s.meta.stats, context_used: 50000, context_limit: 200000, context_pct: 25, context_limit_source: 'default', context_model: null } });
  await raf();
  check(txt('ctx').includes('25%') && /窗口 200k：默认估算；运行一次 \/context 可得到准确值/.test(part('ctx').title), '默认 200k（还不知道模型）：tooltip 提示可运行 /context');
  pushMeta({ stats: { ...s.meta.stats, context_used: 1200000, context_limit: 1000000, context_pct: 100, context_limit_source: 'observed' } });
  await raf();
  check(part('ctx').querySelector('.st-pct').textContent === '100%' && part('ctx').classList.contains('crit') && ringOffset() < 0.01, '推断的分母：百分比封顶 100%');
  pushMeta({ stats: { context_used: 28303, context_limit: 200000, context_pct: 14.2, context_source: 'transcript', context_approx: true,
    cost_usd: null, cost_text: null, cost_source: null, tok_s_live: null, tok_s_last: null } });
  await raf();
  check(txt('cost').includes('—') && part('cost').classList.contains('empty') && /不自行/.test(part('cost').title), '没有状态栏花费：显示 —（不估价）');
  check(!vis(part('rate')), '没有任何速率时不显示 tok/s');
  // 只有百分比
  pushMeta({ stats: { context_used: null, context_limit: null, context_pct: 45, context_source: 'status', cost_usd: 1.23, cost_text: '$1.23', cost_source: 'status' } });
  await raf();
  check(part('ctx').querySelector('.st-pct').textContent === '45%' && Math.abs(ringOffset() - RING_C * 0.55) < 0.01, '只有百分比：圆环 45%');
  // 只有 token 数（没有上限 / 百分比）：不画圆环，直接显示数字
  pushMeta({ stats: { context_used: 28303, context_limit: null, context_pct: null, context_source: 'status', cost_usd: 1.23, cost_text: '$1.23', cost_source: 'status' } });
  await raf();
  check(!vis(ring()) && part('ctx').querySelector('.st-pct').textContent === '28.3k', '只有 token 数：不画圆环，显示 28.3k');
  // 警告色
  pushMeta({ stats: { context_used: 170000, context_limit: 200000, context_pct: 85, context_source: 'status', cost_usd: 1.23, cost_text: '$1.23', cost_source: 'status' } });
  await raf();
  const fill = part('ctx').querySelector('.st-ring-fill');
  const warnColor = getComputedStyle(part('ctx').querySelector('.st-pct')).color;
  check(part('ctx').classList.contains('warn') && getComputedStyle(fill).stroke === warnColor, '>80% 圆环与百分比变警告色（' + warnColor + '）');
  pushMeta({ stats: { ...s.meta.stats, context_used: 196000, context_pct: 98 } });
  await raf();
  const critColor = getComputedStyle(part('ctx').querySelector('.st-pct')).color;
  check(part('ctx').classList.contains('crit') && !part('ctx').classList.contains('warn') && getComputedStyle(fill).stroke === critColor && critColor !== warnColor, '≥95% 危险色');
  // 主题：变量在浅色/深色都有定义，且取值不同
  const th = window.ccrTheme;
  const prevPref = th ? th.pref() : 'system';
  const vars = () => ['--stats-track', '--stats-fill', '--stats-live'].map((v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim());
  const track = () => getComputedStyle(part('ctx').querySelector('.st-ring-track')).stroke;
  if (th) th.set('light', { persist: false });
  await raf();
  const lightVars = vars(), lightTrack = track();
  if (th) th.set('dark', { persist: false });
  await raf();
  const darkVars = vars(), darkTrack = track();
  if (th) th.set(prevPref, { persist: false });
  await raf();
  check(lightVars.every(Boolean) && darkVars.every(Boolean), '浅色/深色都定义了 --stats-* 变量');
  check(lightTrack !== darkTrack, '圆环底槽随主题变化（' + lightTrack + ' / ' + darkTrack + '）');
  check(calls.length === nCalls, '统计条不发任何请求');
}

// ---------- 1e. 图片（attach.js：claude 输入框里的 [Image #N]、⌘V、拖入、只有图片的消息；SPEC §4.2「图片」） ----------
// 图片条 / 覆盖层与 app.js 同一个组件；屏幕用 CLI 2.1.332 真机画面（fixtures/tui_screens_images.ndjson）。
// 只验证组件与浏览器 API 的行为；app.js 里 paste / drop 事件的接线由 headless Chrome 对 fake 后端的端到端验证。
const fxTray = new ImageTray({ onClear: () => record('image/clear'), onRemove: (n) => record('image/remove', { n }),
  onOpen: (n) => { const { list, index } = lightboxList(fxTray.model, n); if (list.length) openLightbox(list, index); } });
const fxDrop = new DropOverlay();
const PNG1 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
function fxPng(w, h, color) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d');
  g.fillStyle = color; g.fillRect(0, 0, w, h);
  g.fillStyle = '#fff'; g.fillRect(w / 4, h / 4, w / 2, h / 2);
  return new Promise((r) => c.toBlob(r, 'image/png'));
}

async function imageChecks() {
  const nCalls = calls.length;
  const box = document.querySelector('.fx-main .composer-box');
  const mainEl = document.querySelector('.fx-main .main');
  if (!fxTray.el.isConnected) box.prepend(fxTray.el);
  if (!fxDrop.el.isConnected) mainEl.appendChild(fxDrop.el);
  if (!screens.has('images:input_images_text')) { check(false, '没有载入 tui_screens_images.ndjson'); return; }
  // 真机画面 → 图片编号（与后端 tui_parse 一致）
  show('images:input_images_text'); await raf();
  const st1 = s.screen.state;
  check(JSON.stringify(inputImages(st1)) === '[21,22]' && inputHasOther(st1), '真机画面：输入框里 [Image #21] [Image #22] + 文字');
  show('images:input_image_only'); await raf();
  check(JSON.stringify(inputImages(s.screen.state)) === '[21]' && !inputHasOther(s.screen.state), '真机画面：只有图片 [Image #21]');
  show('images:busy_input_image_text'); await raf();
  check(JSON.stringify(inputImages(s.screen.state)) === '[23]', '真机画面：回复还在刷屏时输入框里的图片 [Image #23]');
  show('images:done_after_image'); await raf();
  check(s.screen.state.live === '⏺ 这张图上写的是 "OK"。' && !inputImages(s.screen.state).length, '回显下的 "⎿  [Image #20]" 不进实时草稿');

  // 图片条：在输入框顶部，56px 小缩略图（点了放大）/ 占位图标、每张一个 ×、说明、清除
  const ta = $('fx-input');
  const taH0 = ta.getBoundingClientRect().height;
  const boxH0 = box.getBoundingClientRect().height;
  const blob = await fxPng(64, 40, '#2f6bf0');
  const url = URL.createObjectURL(blob);
  const blob2 = await fxPng(40, 64, '#e0592a');
  const url2 = URL.createObjectURL(blob2);
  const thumbs = new Map([[21, { url, name: '剪贴板里的图片', kind: 'paste' }]]);
  const m1 = trayModel({ images: [21, 22], pending: null, other: false, thumbs });
  check(fxTray.update(m1) === true, '图片条：有图片时建一次');
  await raf();
  const chips = [...fxTray.el.querySelectorAll('.img-chip')];
  check(!fxTray.el.hidden && box.firstElementChild === fxTray.el, '图片条在输入框（composer-box）顶部');
  check(chips.length === 2 && chips[0].textContent.includes('#21') && chips[1].textContent.includes('#22'), '两个图片 chip：#21 #22');
  const img = chips[0].querySelector('img');
  if (img) { try { await img.decode(); } catch (_) { /* 下面的检查会失败 */ } }
  const ir = img ? img.getBoundingClientRect() : { width: 0, height: 0 };
  check(!!img && img.naturalWidth === 64 && ir.width >= 52 && ir.width <= 64 && Math.abs(ir.height - ir.width) < 1
    && getComputedStyle(img).objectFit === 'cover', '⌘V 的图片有小缩略图（本页内存里的 blob，' + Math.round(ir.width) + 'px 方图，object-fit: cover）');
  check(parseFloat(getComputedStyle(chips[0]).borderTopLeftRadius) >= 6, '缩略图圆角');
  check(!!chips[1].querySelector('.img-chip-ph svg') && chips[1].querySelector('.img-thumb').tagName === 'SPAN', '没有缩略图（拖入的路径 / 刷新后）时显示图片图标，不是按钮');
  const th0 = chips[0].querySelector('.img-thumb');
  check(th0.tagName === 'BUTTON' && th0.getAttribute('aria-label') === '放大查看 [Image #21]' && /\[Image #21\]/.test(th0.title)
    && th0.title.includes('剪贴板里的图片') && th0.title.includes('点击放大'), '有缩略图的是按钮：aria-label、tooltip（[Image #21] · 来源 · 点击放大）');
  check(/页面刷新后/.test(chips[1].querySelector('.img-thumb').title), '没有缩略图时 tooltip 说明原因');
  // ×：每张一个，键盘可达（button + aria-label），在 chip 内不被裁掉
  const xs = [...fxTray.el.querySelectorAll('.img-x')];
  const cr0 = chips[0].getBoundingClientRect(), xr0 = xs[0] ? xs[0].getBoundingClientRect() : null;
  check(xs.length === 2 && xs.every((x) => x.tagName === 'BUTTON' && x.type === 'button') && xs[1].getAttribute('aria-label') === '删除 [Image #22]',
    '每张一个 × 按钮（button、aria-label「删除 [Image #N]」）');
  check(!!xr0 && xr0.width >= 18 && xr0.left >= cr0.left && xr0.right <= cr0.right && xr0.top >= cr0.top && xr0.bottom <= cr0.bottom
    && document.elementFromPoint(xr0.left + xr0.width / 2, xr0.top + xr0.height / 2)?.closest('.img-x') === xs[0],
  '× 在缩略图右上角、可点到（' + (xr0 ? Math.round(xr0.width) : 0) + 'px）');
  check(fxTray.el.textContent.includes('在 claude 的输入框里，发送时和文字一起发出'), '说明文字');
  const trayH = fxTray.el.getBoundingClientRect().height;
  const boxH1 = box.getBoundingClientRect().height;
  check(trayH >= 56 && trayH <= 80, '图片条一行高（' + trayH.toFixed(1) + 'px）');
  check(Math.abs(boxH1 - boxH0 - trayH - 4) <= 2, '输入框整体只多出图片条的高度（' + boxH0.toFixed(1) + ' → ' + boxH1.toFixed(1) + '）');
  check(Math.abs(ta.getBoundingClientRect().height - taH0) < 0.5, '文本框高度不变（autosize 不受影响）');
  check(fxTray.update(trayModel({ images: [21, 22], pending: null, other: false, thumbs })) === false && chips[0].isConnected, '内容不变不重建');
  // 很多张：chip 横向滚动，不撑宽、不换行
  fxTray.update(trayModel({ images: Array.from({ length: 30 }, (_, i) => i + 1), pending: null, other: false, thumbs: new Map() }));
  await raf();
  check(fxTray.el.getBoundingClientRect().height <= 80 && box.scrollWidth <= box.clientWidth + 1, '30 张图片：一行、横向滚动，不撑宽输入框');
  fxTray.update(m1); await raf();
  // 点缩略图 → 灯箱放大（不发请求）；点 × 不会打开灯箱
  const nBefore = calls.length;
  await click(fxTray.el.querySelector('.img-thumb'), '缩略图');
  await raf();
  const lb1 = document.querySelector('.lb');
  check(lightboxOpen() && !!lb1 && lb1.querySelector('.lb-title').textContent.includes('[Image #21]') && lb1.querySelector('.lb-count').textContent === ''
    && calls.length === nBefore, '点缩略图用灯箱打开原图（只有一张有图时没有切换），不发请求');
  const lbImg = lb1 && lb1.querySelector('.lb-img');
  if (lbImg && !lbImg.complete) await new Promise((r) => { lbImg.onload = r; lbImg.onerror = r; setTimeout(r, 500); });
  check(!!lbImg && lbImg.naturalWidth === 64, '灯箱里是原图（64×40）');
  lb1.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await raf();
  check(!lightboxOpen(), 'Esc 关闭灯箱');
  thumbs.set(22, { url: url2, name: 'b.png', kind: 'drop' });
  const m2 = trayModel({ images: [21, 22], pending: null, other: false, thumbs });
  fxTray.update(m2); await raf();
  await click(fxTray.el.querySelectorAll('.img-thumb')[1], '第二张缩略图');
  await raf();
  const lb2 = document.querySelector('.lb');
  check(!!lb2 && lb2.querySelector('.lb-count').textContent === '2 / 2' && lb2.querySelector('.lb-title').textContent === 'b.png · [Image #22]', '两张都有图：灯箱从点中的那张开始（2 / 2）');
  lb2.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
  await raf();
  check(lb2.querySelector('.lb-count').textContent === '1 / 2' && lb2.querySelector('.lb-title').textContent.includes('[Image #21]'), '← 切到图片条上的上一张');
  closeLightbox(); await raf();
  await click(fxTray.el.querySelectorAll('.img-x')[1], '删除 #22');
  check(calls.at(-1)?.op === 'image/remove' && calls.at(-1).body?.n === 22 && calls.at(-1).gesture && !lightboxOpen(),
    '点 × → POST image/remove {n:22}（只来自点击），不会顺带打开灯箱');
  // 正在删：那一张变淡，× 暂时都没有，说明换成「正在删」
  fxTray.update(trayModel({ images: [21, 22], pending: { kind: 'remove', n: 22 }, thumbs })); await raf();
  check(fxTray.el.querySelectorAll('.img-chip.removing').length === 1 && fxTray.el.querySelector('.img-chip.removing').dataset.n === '22'
    && !fxTray.el.querySelector('.img-x') && fxTray.el.textContent.includes('[Image #22]') && !!fxTray.el.querySelector('.spin'), '正在删 #22：变淡、转圈说明、没有 ×');
  fxTray.update(trayModel({ images: [21], pending: null, other: false, thumbs, removable: false })); await raf();
  check(!fxTray.el.querySelector('.img-x') && !!fxTray.el.querySelector('.img-clear'), '服务器会话：没有 ×（只能清除）');
  fxTray.update(trayModel({ images: [23], pending: null, other: false, thumbs: new Map([[23, { url: '', name: 'c.png', kind: 'drop', loading: true }]]) })); await raf();
  check(!!fxTray.el.querySelector('.img-thumb .spin') && /加载中/.test(fxTray.el.querySelector('.img-thumb').title), '拖入的图片：缩略图加载中转圈');
  fxTray.update(m1); await raf();
  // 清除：只来自点击
  await click(fxTray.el.querySelector('.img-clear'), '清除图片');
  check(calls.at(-1)?.op === 'image/clear' && calls.at(-1).gesture, '点「清除」→ POST image/clear（只来自点击）');
  // 进行中 / 还有别的文字 / 不可操作
  fxTray.update(trayModel({ images: [], pending: { kind: 'paste', n: 1 } })); await raf();
  check(!!fxTray.el.querySelector('.spin') && fxTray.el.textContent.includes('剪贴板') && !fxTray.el.querySelector('.img-clear'), '⌘V 进行中：转圈 + 说明，没有清除按钮');
  fxTray.update(trayModel({ images: [21], pending: null, other: true })); await raf();
  check(fxTray.el.classList.contains('warn') && !fxTray.el.querySelector('.img-clear') && fxTray.el.textContent.includes('一起清掉')
    && !!fxTray.el.querySelector('.img-x'), '输入框里还有别的文字：警示色说明，没有清除按钮，× 仍可逐张删');
  fxTray.update(trayModel({ images: [21], pending: null, other: false, blocked: 'blocked' })); await raf();
  check(!fxTray.el.querySelector('.img-clear') && !fxTray.el.querySelector('.img-x'), '已退出 / 有对话框时没有清除和 ×');
  // 浅色 / 深色：说明文字、#N 角标、× 的对比度
  const th = window.ccrTheme;
  const prevPref = th ? th.pref() : 'system';
  fxTray.update(m1); await raf();
  for (const theme of ['light', 'dark']) {
    if (th) th.set(theme, { persist: false });
    await raf();
    const tag = theme === 'light' ? '浅色' : '深色';
    const bg = rgbaOf(getComputedStyle(box).backgroundColor);
    const note = contrast(rgbaOf(getComputedStyle(fxTray.el.querySelector('.img-note')).color), bg);
    const ph = fxTray.el.querySelectorAll('.img-chip')[1];
    const phBg = over(rgbaOf(getComputedStyle(ph.querySelector('.img-chip-ph')).backgroundColor), over(rgbaOf(getComputedStyle(ph).backgroundColor), bg));
    const nEl = ph.querySelector('.img-chip-n'), xEl = ph.querySelector('.img-x');
    const nTxt = contrast(rgbaOf(getComputedStyle(nEl).color), over(rgbaOf(getComputedStyle(nEl).backgroundColor), phBg));
    const xTxt = contrast(rgbaOf(getComputedStyle(xEl).color), over(rgbaOf(getComputedStyle(xEl).backgroundColor), { r: 255, g: 255, b: 255, a: 1 }));
    check(note >= 4.5 && nTxt >= 4.5 && xTxt >= 4.5, tag + '：图片条对比度 ≥ 4.5（说明 ' + note.toFixed(2) + '，#N ' + nTxt.toFixed(2) + '，× 压在白图上 ' + xTxt.toFixed(2) + '）');
    const phIc = contrast(rgbaOf(getComputedStyle(ph.querySelector('.img-chip-ph')).color), phBg);
    check(phIc >= 3, tag + '：占位图标对比度 ' + phIc.toFixed(2) + ' ≥ 3');
    fxTray.update(trayModel({ images: [21], pending: null, other: true })); await raf();
    const warn = contrast(rgbaOf(getComputedStyle(fxTray.el.querySelector('.img-note')).color), bg);
    check(warn >= 3, tag + '：警示说明对比度 ' + warn.toFixed(2) + ' ≥ 3');
    fxTray.update(m1); await raf();
  }
  if (th) th.set(prevPref, { persist: false });
  await raf();
  fxTray.update(null); await raf();
  check(fxTray.el.hidden && Math.abs(box.getBoundingClientRect().height - boxH0) < 0.5, '没有图片时图片条隐藏、输入框恢复原高');
  URL.revokeObjectURL(url);
  URL.revokeObjectURL(url2);

  // 浏览器的 DataTransfer / ClipboardEvent（构造的，不碰系统剪贴板）
  const file = new File([blob], 'shot.png', { type: 'image/png' });
  const dt = new DataTransfer(); dt.items.add(file);
  const dtText = new DataTransfer(); dtText.setData('text/plain', '你好');
  const dtMix = new DataTransfer(); dtMix.setData('text/plain', 'img url'); dtMix.items.add(file);
  const i1 = transferInfo(dt), i2 = transferInfo(dtText), i3 = transferInfo(dtMix);
  check(i1.images === 1 && !i1.text && i1.file instanceof File && i1.file.type === 'image/png', 'DataTransfer 里只有截图：1 张图片、没有文字');
  check(i2.images === 0 && i2.text, '纯文字粘贴：不当图片处理');
  check(i3.images === 1 && i3.text, '图片 + 文字：两样都认出来（文字照常粘贴）');
  check(dragHasFiles(dt) && !dragHasFiles(dtText), '拖动里有文件才算文件拖放');
  let seen = null;
  const onPaste = (e) => { seen = transferInfo(e.clipboardData); e.preventDefault(); };
  ta.addEventListener('paste', onPaste);
  ta.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  ta.removeEventListener('paste', onPaste);
  check(seen && seen.images === 1 && !seen.text, 'paste 事件的 clipboardData 里能拿到图片（构造的 ClipboardEvent）');

  // 拖动覆盖层
  fxDrop.show(dropHint({ native: true, paths: ['/Users/u/Desktop/截屏 2026.png', '/tmp/a.py'], sess: { local: true } }));
  await raf();
  const or = fxDrop.el.getBoundingClientRect(), mr = mainEl.getBoundingClientRect();
  check(!fxDrop.el.hidden && or.width >= mr.width - 20 && or.height >= mr.height - 20 && or.left >= mr.left && or.bottom <= mr.bottom, '覆盖层盖住会话头 + 聊天区 + 输入框（' + Math.round(or.width) + '×' + Math.round(or.height) + '）');
  check(getComputedStyle(fxDrop.el).pointerEvents === 'none', '覆盖层不接收指针（拖放事件照常到下面）');
  check(fxDrop.el.textContent.includes('松开即可') && fxDrop.el.textContent.includes('1 张图片') && fxDrop.el.textContent.includes('1 个文件以路径插入'), '本机会话：说明图片进 claude 输入框、其它文件以路径插入');
  fxDrop.show(dropHint({ native: true, paths: ['/a/x.png'], sess: { local: false } })); await raf();
  check(fxDrop.el.classList.contains('bad') && fxDrop.el.textContent.includes('服务器会话'), '服务器会话：提示不支持');
  fxDrop.show(dropHint({ native: false })); await raf();
  check(fxDrop.el.classList.contains('bad') && fxDrop.el.textContent.includes('⌘V'), '浏览器（拿不到路径）：提示用 ⌘V');
  fxDrop.hide(); await raf();
  check(fxDrop.el.hidden, '离开后覆盖层隐藏');
  // 非图片文件 = 终端拖放：转义后的路径插到光标处
  const r = insertPaths('看看这个', 4, 4, ['/Users/u/my notes (1).md']);
  check(r.value === '看看这个 /Users/u/my\\ notes\\ \\(1\\).md ' && r.caret === r.value.length && escapeDropPath('/a/b c.png') === '/a/b\\ c.png', '路径按终端拖放的写法转义插入');

  // transcript：带图片的用户消息（CLI 把 "[Image #N] " 占位写进文字）
  const ts = new Date().toISOString();
  const imgB = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG1 } };
  pushTranscript([
    { type: 'user', uuid: 'img-u1', timestamp: ts, imagePasteIds: [1, 2], message: { role: 'user', content: [{ type: 'text', text: '[Image #1] [Image #2] 这两张图写了什么？' }, imgB, imgB] } },
    { type: 'user', uuid: 'img-u1m', timestamp: ts, isMeta: true, message: { role: 'user', content: [{ type: 'text', text: '[Image: source: /tmp/imgs/a.png]' }] } },
    { type: 'assistant', uuid: 'img-a1', timestamp: ts, message: { id: 'msg_img1', role: 'assistant', content: [{ type: 'text', text: '第一张 CAT，第二张 DOG。' }] } },
    { type: 'user', uuid: 'img-u2', timestamp: ts, imagePasteIds: [3], message: { role: 'user', content: [{ type: 'text', text: '[Image #3]' }, imgB] } },
  ], false);
  await raf(); flushNow(s);
  const us = qa('.k-user');
  const u1 = us.at(-2), u2 = us.at(-1);
  check(u1 && u1.querySelectorAll('.user-imgs img').length === 2 && u1.querySelector('.user-text')?.textContent === '这两张图写了什么？', '文字 + 图片：气泡里 2 张图，文字去掉了开头的 [Image #N] 占位');
  check(u2 && u2.querySelectorAll('.user-imgs img').length === 1 && !u2.querySelector('.user-text'), '只有图片：只显示图片');
  check(!qa('.k-user').some((e) => e.textContent.includes('[Image: source:')), 'isMeta 的 [Image: source: …] 不显示');
  // 发出去之后：时间线里的图片点击同样用灯箱放大（同一条消息里的两张可以切换）
  const ub = u1 && u1.querySelector('.user-imgs .cimg');
  if (ub) { ub.scrollIntoView({ block: 'center' }); await raf(); }
  await click(ub, '用户消息里的图片');
  await raf();
  const lb3 = document.querySelector('.lb');
  check(!!lb3 && lb3.querySelector('.lb-count').textContent === '1 / 2', '时间线里用户消息的图片：点击用灯箱放大（1 / 2）');
  closeLightbox(); await raf();
  const pl = promptsFromItems(s.items);
  check(pl.at(-1) === '这两张图写了什么？', '↑/↓ 历史：带图片的消息只取文字，只有图片的跳过');
  check(calls.length === nCalls + 2, '图片条 / 覆盖层 / 灯箱 / 渲染不自动发请求（只有点击 × 与「清除」那两次）');
  calls.length = nCalls;   // 后面几节按「没有任何请求」起算
}

// ---------- 侧栏「新回复」/「待处理」 ----------
// 纯函数用 app.js 同一个 sessrow.js；侧栏行在本页临时画几行样例（与 app.js 相同的结构与 class），比对浅色/深色下的计算颜色。
function rgbaOf(c) {
  const m = String(c).match(/[\d.]+/g) || [];
  return { r: +m[0] || 0, g: +m[1] || 0, b: +m[2] || 0, a: m.length > 3 ? +m[3] : 1 };
}
function over(top, base) {   // 半透明底色叠到不透明的底上
  const a = top.a;
  return { r: top.r * a + base.r * (1 - a), g: top.g * a + base.g * (1 - a), b: top.b * a + base.b * (1 - a), a: 1 };
}
function relLum(c) {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
}
function contrast(a, b) { const x = relLum(a), y = relLum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }

async function sessRowChecks() {
  const nCalls = calls.length;
  const base = { state: 'idle', link: 'ready' };
  const st = (meta, x = {}) => sessionStatus({ meta: { ...base, ...meta }, dialog: null, busy: false, subRunning: 0, jobEnded: false, localSeen: null, ...x });
  const U = { done_at: 1.7e9, seen_at: null, unread: true };
  // 状态优先级：死状态 > 待处理 > 运行中 > 新回复 > 空闲
  check(st(U)[0] === 'unread' && st(U)[1] === '新回复' && st({})[0] === 'ready', '未读且空闲 → 新回复；没有未读 → 空闲');
  check(st(U, { dialog: { kind: 'permission' } })[0] === 'pending' && st({ ...U, state: 'waiting' })[1] === '待处理'
    && st({ ...U, state: 'trust' })[1] === '待信任目录', '待处理（对话框 / 信任目录）压过新回复');
  check(st(U, { busy: true })[1] === '运行中' && st(U, { subRunning: 2 })[1] === '子代理运行中', '运行中 / 子代理运行中压过新回复（新一轮刚开始 meta.unread 还没清）');
  check(st({ ...U, state: 'exited' })[0] === 'exited' && st({ ...U, state: 'error' })[0] === 'error'
    && st({ ...U, link: 'down' })[1] === '连接断开' && st({ ...U, state: 'starting' })[1] === '启动中'
    && st({ ...U, state: 'exited' }, { jobEnded: true })[1] === '作业已结束', '死状态 / 断线 / 启动中最高');
  check(st({ ...U, state: 'waiting' }, { busy: true })[0] === 'pending', '待处理压过运行中');
  // 未读判定：meta.unread 为准；本页已乐观标为已读同一个 done_at → 不再显示
  check(!isUnread({ unread: true }) && !isUnread({ unread: false, done_at: 1 }) && isUnread(U) && !isUnread(U, U.done_at)
    && isUnread(U, U.done_at - 1) && st(U, { localSeen: U.done_at })[0] === 'ready', 'isUnread：缺 done_at / 已读 / 乐观已读都不算');
  // 行 class
  check(rowClass('unread', false) === 'sess unread' && rowClass('pending', true) === 'sess active attn' && rowClass('ready', true) === 'sess active'
    && rowClass('error', false) === 'sess' && rowClass('busy', false) === 'sess', '行 class：unread / attn / active，错误与运行中无底色');
  // 窗口标题未读数（与原生壳 main.swift 同一规则）
  const sts = [st(U), st(U, { busy: true }), st({ ...U, done_at: 1.8e9 }), st({}), st(U, { dialog: { kind: 'ask' } })];
  check(unreadCount(sts) === 2 && docTitle(unreadCount(sts)) === '(2) ' + APP_TITLE && docTitle(0) === APP_TITLE, '标题未读数 = 显示「新回复」的行数：' + docTitle(unreadCount(sts)));
  check(titleCount('(12) CC Remote Chat') === 12 && titleCount(APP_TITLE) === 0 && titleCount('(3)x') === 0 && titleCount('(ab) x') === 0, '标题前缀解析（程序坞角标）');
  // 样例侧栏：普通空闲 / 新回复 / 待处理 / 错误 / 选中×三种
  const side = h('div', { class: 'fx-side' });
  side.style.position = 'fixed'; side.style.left = '0'; side.style.top = '0'; side.style.width = '288px';
  side.style.zIndex = '50'; side.style.padding = '8px'; side.style.background = 'var(--bg-side)';
  const mk = (stt, active, title) => {
    const [cls, text] = stt;
    return h('div', { class: rowClass(cls, active), 'data-k': cls + (active ? '-active' : '') },
      h('span', { class: 'dot st-' + cls, title: text }),
      h('div', { class: 'sess-main' }, h('div', { class: 'sess-title' }, title),
        h('div', { class: 'sess-sub' }, h('span', { class: 'mono' }, 'job 123456'), h('span', { class: 'st-label st-' + cls }, text))));
  };
  side.append(mk(st({}), false, '普通空闲'), mk(st(U), false, '有新回复'), mk(st({ state: 'waiting' }), false, '等待审批'),
    mk(['error', '错误'], false, '出错了'), mk(st({}), true, '选中·空闲'), mk(st(U), true, '选中·新回复'), mk(st({ state: 'trust' }), true, '选中·待信任'));
  document.body.appendChild(side);
  const row = (k) => side.querySelector('[data-k="' + k + '"]');
  const th = window.ccrTheme;
  const prevPref = th.pref();
  for (const theme of ['light', 'dark']) {
    th.set(theme, { persist: false }); await raf();
    const sideBg = rgbaOf(getComputedStyle(side).backgroundColor);
    const bgOf = (k) => over(rgbaOf(getComputedStyle(row(k)).backgroundColor), sideBg);
    const fmt = (c) => 'rgb(' + [c.r, c.g, c.b].map((v) => Math.round(v)).join(',') + ')';
    const plain = bgOf('ready'), unread = bgOf('unread'), attn = bgOf('pending'), err = bgOf('error');
    const act = bgOf('ready-active'), unreadAct = bgOf('unread-active'), attnAct = bgOf('pending-active');
    const redness = (c) => c.r - (c.g + c.b) / 2;
    const yellowness = (c) => (c.r + c.g) / 2 - c.b;
    const tag = theme === 'light' ? '浅色' : '深色';
    check(redness(unread) > redness(plain) + 4 && fmt(unread) !== fmt(plain), tag + '：新回复行偏红（' + fmt(unread) + ' vs 普通 ' + fmt(plain) + '）');
    check(yellowness(attn) > yellowness(plain) + 4 && redness(attn) < redness(unread) + 20 && fmt(attn) !== fmt(unread), tag + '：待处理行偏黄（' + fmt(attn) + '）');
    check(fmt(err) === fmt(plain), tag + '：错误行没有底色（只靠红点/红字，与新回复区分）');
    check(redness(unreadAct) > redness(act) + 4 && redness(unreadAct) > redness(unread) && /inset/.test(getComputedStyle(row('unread-active')).boxShadow),
      tag + '：选中的新回复行仍明显偏红（' + fmt(unreadAct) + ' vs 选中 ' + fmt(act) + '）+ 左侧色条');
    check(yellowness(attnAct) > yellowness(act) + 4 && /inset/.test(getComputedStyle(row('pending-active')).boxShadow), tag + '：选中的待处理行仍偏黄（' + fmt(attnAct) + '）+ 左侧色条');
    const dot = getComputedStyle(row('unread').querySelector('.dot'));
    const errDot = getComputedStyle(row('error').querySelector('.dot'));
    check(dot.animationName === 'none' && dot.boxShadow !== 'none' && errDot.boxShadow === 'none' && dot.backgroundColor === errDot.backgroundColor,
      tag + '：新回复是实心红点 + 光圈、静态；错误红点无光圈');
    const tw = (k) => Number(getComputedStyle(row(k).querySelector('.sess-title')).fontWeight);
    check(tw('unread') > tw('ready') && tw('unread-active') > tw('ready-active'), tag + '：新回复标题加粗（' + tw('unread') + ' vs ' + tw('ready') + '）');
    const ratios = ['unread', 'pending', 'unread-active', 'pending-active'].map((k) => contrast(rgbaOf(getComputedStyle(row(k).querySelector('.sess-sub')).color), bgOf(k)));
    check(ratios.every((x) => x >= 4.5), tag + '：浅红/浅黄底上第二行文字对比度 ≥ 4.5（' + ratios.map((x) => x.toFixed(2)).join(' / ') + '）');
    const lbl = contrast(rgbaOf(getComputedStyle(row('unread').querySelector('.st-label')).color), unread);
    check(lbl >= 3, tag + '：「新回复」红色标签对比度 ' + lbl.toFixed(2) + ' ≥ 3（加粗）');
  }
  th.set(prevPref, { persist: false }); await raf();
  side.remove();
  check(calls.length === nCalls, '侧栏状态判定与渲染不发任何请求');
}

// ---------- 侧栏：项目与聊天（SPEC §15.2） ----------
// 纯函数与 app.js 同一个 projects.js（node 单测 tests/test_projects.py 更细）；标题行用同一个 projectHeader() 画几组样例，
// 比对浅色 / 深色下的颜色、对比度、折叠、截断、「+」的显示。
async function projectChecks() {
  const nCalls = calls.length;
  const M = (sid, jobid, cwd, x = {}) => ({ sid, meta: { sid, jobid, cwd, created_at: 100, order: null, in_project: true, ...x } });
  const items = [M('a', 'local', '~/proj', { order: 2 }), M('b', '123', '/home/u/exp/', { order: 0 }), M('c', '456', '/home/u/exp', { order: 3 }),
    M('d', 'local', '/Users/u/proj/', { order: 1 }), M('e', 'local', '/Users/u/proj', { order: 4, in_project: false }), M('f', 'local', '/Users/u/x', { order: 5 })];
  delete items[5].meta.in_project;
  const L = PJ.buildLayout(items, { homes: { local: '/Users/u' } });
  check(JSON.stringify(L.projects.map((g) => [g.key, g.sids])) === JSON.stringify([['server:/home/u/exp', ['b', 'c']], ['local:/Users/u/proj', ['d', 'a']], ['local:/Users/u/x', ['f']]])
    && JSON.stringify(L.chats) === '["e"]', '分组：同一服务器路径不同作业 = 一个项目；~ 与绝对路径、末尾斜杠 = 同一个；in_project=false → 聊天；旧记录（没有字段）→ 项目');
  check(JSON.stringify(PJ.flatten(L)) === '["b","c","d","a","f","e"]' && PJ.moveSession(L, 'a', 'b', false) === null && PJ.moveSession(L, 'e', 'f', false) === null
    && JSON.stringify(PJ.moveSession(L, 'a', 'd', false)) === '["b","c","a","d","f","e"]', '拖动落地：组内照常、跨项目 / 项目↔聊天不接受');
  check(JSON.stringify(PJ.moveProject(L, 'local:/Users/u/x', 'server:/home/u/exp', false)) === '["f","b","c","d","a","e"]', '拖动项目标题行：整组移动、组内顺序不变');
  const L2 = PJ.buildLayout(items.concat([M('n', '789', '/home/u/exp', { created_at: 999 })]), { homes: { local: '/Users/u' } });
  check(L2.projects[0].key === 'server:/home/u/exp' && L2.projects[0].sids[0] === 'n' && L2.projects[0].jobid === '789', '已有项目里新建：在该项目最上面、项目不挪位置；「+」预填最新会话的作业');
  const P = ['pending', '待处理'], U = ['unread', '新回复'], B = ['busy', '运行中'], R = ['ready', '空闲'];
  check(PJ.aggregate([R, B, U]).top === 'unread' && PJ.aggregate([U, P]).top === 'pending' && PJ.aggregate([R, B]).top === 'busy' && PJ.aggregate([R]).top === null,
    '聚合状态：待处理 > 新回复 > 运行中');
  // ---- 标题行样例（与 app.js 相同的 projectHeader / 结构 / class）
  const side = h('div', { class: 'fx-side side-list' });
  Object.assign(side.style, { position: 'fixed', left: '0', top: '0', width: '288px', zIndex: '50', background: 'var(--bg-side)', overflow: 'visible', padding: '2px 8px 10px' });
  const blocks = {};
  const mk = (k, g, rowTitle) => {
    const hd = PJ.projectHeader({ key: 'k:' + k, name: g.name || k, path: g.path || '/Users/u/' + k, target: g.target || 'local', n: g.n || 1,
      agg: PJ.aggregate(g.sts || [R]), collapsed: !!g.collapsed, active: !!g.active, bodyId: 'fx-pb-' + k }, { onToggle: () => {}, onAdd: () => {} });
    const body = h('div', { class: 'proj-body', id: 'fx-pb-' + k, hidden: !!g.collapsed },
      h('div', { class: rowClass('ready', false) }, h('span', { class: 'dot st-ready' }), h('div', { class: 'sess-main' }, h('div', { class: 'sess-title' }, rowTitle || '会话'),
        h('div', { class: 'sess-sub' }, h('span', { class: 'st-label st-ready' }, '空闲')))));
    blocks[k] = h('div', { class: 'proj' + (g.deny ? ' drop-deny' : '') }, hd, body);
    return blocks[k];
  };
  side.append(PJ.sectionHead('项目', { addTitle: '新建项目', addLabel: '新建项目', onAdd: () => {} }),
    mk('open', { n: 2, sts: [R, B] }, '展开的项目'), mk('plain', { collapsed: true, target: 'server' }), mk('attn', { collapsed: true, n: 3, sts: [R, P, U], target: 'server' }),
    mk('unread', { collapsed: true, sts: [U, R] }), mk('act', { collapsed: true, active: true }), mk('actUnread', { collapsed: true, active: true, sts: [U] }),
    mk('actAttn', { collapsed: true, active: true, sts: [P] }), mk('busy', { collapsed: true, sts: [B] }),
    mk('long', { name: 'a-very-long-project-folder-name-that-must-be-truncated', n: 12, collapsed: true, target: 'server' }), mk('deny', { deny: true }));
  document.body.appendChild(side);
  await raf();
  const hd = (k) => blocks[k].querySelector('.proj-hd');
  const tg = (k) => blocks[k].querySelector('.proj-toggle');
  check(tg('open').getAttribute('aria-expanded') === 'true' && tg('plain').getAttribute('aria-expanded') === 'false' && tg('plain').getAttribute('aria-controls') === 'fx-pb-plain'
    && blocks.plain.querySelector('.proj-body').hidden, '折叠：button + aria-expanded / aria-controls，组内会话 hidden');
  check(/^项目 attn（服务器，3 个会话，1 个待处理，1 个新回复）$/.test(tg('attn').getAttribute('aria-label')) && tg('attn').title.startsWith('/Users/u/attn\n服务器 · 3 个会话 · 1 个待处理 · 1 个新回复'),
    'aria-label / tooltip：名称、位置、数量、状态、完整路径：' + tg('attn').getAttribute('aria-label'));
  check(hd('attn').className === 'proj-hd attn' && hd('unread').className === 'proj-hd unread' && hd('open').className === 'proj-hd' && hd('act').className === 'proj-hd active'
    && hd('actUnread').className === 'proj-hd unread active' && hd('busy').className === 'proj-hd', '标题行 class：折叠时 待处理 = attn、新回复 = unread、含当前会话 = active；展开 / 只在干活 = 不着色');
  check(blocks.attn.querySelector('.proj-dot').className === 'dot proj-dot st-pending' && blocks.busy.querySelector('.proj-dot').className === 'dot proj-dot st-busy'
    && !blocks.plain.querySelector('.proj-dot') && blocks.open.querySelector('.proj-dot.st-busy'), '状态点：待处理琥珀、在干活脉冲蓝点（与会话行同一套 .dot）；没事不画');
  const chevRot = (k) => getComputedStyle(blocks[k].querySelector('.proj-chev')).transform;
  check(chevRot('open') === 'none' && chevRot('plain') !== 'none', 'chevron：展开朝下、折叠转 -90°');
  const hb = hd('open').getBoundingClientRect();
  check(Math.round(hb.height) === 28 && Math.round(hb.width) === 272, '标题行紧凑：28px 高、与会话行同宽（' + Math.round(hb.width) + 'px）');
  const db = blocks.busy.querySelector('.proj-dot').getBoundingClientRect(), tb = tg('busy').getBoundingClientRect();
  check(Math.abs((db.top + db.bottom) / 2 - (tb.top + tb.bottom) / 2) <= 1.5, '状态点在标题行里竖直居中');
  check(getComputedStyle(blocks.open.querySelector('.proj-body .sess')).paddingLeft === '14px', '组内会话行缩进（左内边距 14px）');
  const ln = blocks.long.querySelector('.proj-name');
  check(ln.scrollWidth > ln.clientWidth && getComputedStyle(ln).textOverflow === 'ellipsis' && hd('long').scrollWidth <= hd('long').clientWidth + 1
    && blocks.long.querySelector('.proj-count').getBoundingClientRect().right <= hd('long').getBoundingClientRect().right && blocks.long.querySelector('.loc-tag').getBoundingClientRect().width > 0,
  '长文件夹名省略号截断，位置标签与数量仍完整、不撑宽');
  const add = blocks.open.querySelector('.proj-add');
  check(getComputedStyle(add).opacity === '0' && add.getAttribute('aria-label') === '在项目「open」中新建会话', '「+」平时隐藏、有 aria-label');
  add.focus(); await raf();
  check(getComputedStyle(add).opacity === '1' && getComputedStyle(tg('open')).paddingRight === '30px', '键盘聚焦「+」时显示，名称让出位置');
  add.blur();
  const tip = blocks.deny.querySelector('.deny-tip');
  check(getComputedStyle(tip).display !== 'none' && tip.textContent === '不同项目，不能放这里' && getComputedStyle(blocks.deny.querySelector('.proj-count')).display === 'none'
    && getComputedStyle(blocks.deny).outlineStyle === 'dashed' && getComputedStyle(blocks.open.querySelector('.deny-tip')).display === 'none', '不能放：虚线框 +「不同项目，不能放这里」（平时不显示）');
  const secAdd = side.querySelector('.side-sec-add');
  check(side.querySelector('.side-sec-t').textContent === '项目' && secAdd.getAttribute('aria-label') === '新建项目', '分区标题「项目」+「新建项目」按钮');
  const th = window.ccrTheme;
  const prevPref = th.pref();
  for (const theme of ['light', 'dark']) {
    th.set(theme, { persist: false }); await raf();
    const tag = theme === 'light' ? '浅色' : '深色';
    const sideBg = rgbaOf(getComputedStyle(side).backgroundColor);
    const bgOf = (k) => over(rgbaOf(getComputedStyle(hd(k)).backgroundColor), sideBg);
    const fmt = (c) => 'rgb(' + [c.r, c.g, c.b].map((v) => Math.round(v)).join(',') + ')';
    const redness = (c) => c.r - (c.g + c.b) / 2;
    const yellowness = (c) => (c.r + c.g) / 2 - c.b;
    const plain = bgOf('plain'), attn = bgOf('attn'), unread = bgOf('unread'), act = bgOf('act');
    check(yellowness(attn) > yellowness(plain) + 4 && redness(unread) > redness(plain) + 4 && fmt(attn) !== fmt(unread), tag + '：折叠时 待处理 偏黄（' + fmt(attn) + '）、新回复 偏红（' + fmt(unread) + '），与会话行同色');
    const rowAct = h('div', { class: 'sess active' }); side.appendChild(rowAct);
    check(fmt(act) === fmt(over(rgbaOf(getComputedStyle(rowAct).backgroundColor), sideBg)) && fmt(act) !== fmt(plain), tag + '：含当前会话的折叠项目 = 会话行的选中底色（' + fmt(act) + '）');
    rowAct.remove();
    check(/inset/.test(getComputedStyle(hd('actUnread')).boxShadow) && /inset/.test(getComputedStyle(hd('actAttn')).boxShadow) && redness(bgOf('actUnread')) > redness(act) + 4,
      tag + '：选中 + 新回复 / 待处理：更深一档同色底 + 左侧色条（与会话行一致）');
    const res = [];
    for (const k of ['plain', 'attn', 'unread', 'act', 'actUnread', 'actAttn', 'open']) {
      const bg = bgOf(k);
      res.push(['名称 ' + k, contrast(rgbaOf(getComputedStyle(blocks[k].querySelector('.proj-name')).color), bg), 4.5]);
      res.push(['数量 ' + k, contrast(rgbaOf(getComputedStyle(blocks[k].querySelector('.proj-count')).color), bg), 4.5]);
      res.push(['位置 ' + k, contrast(rgbaOf(getComputedStyle(blocks[k].querySelector('.loc-tag')).color), bg), 3]);
    }
    res.push(['分区标题', contrast(rgbaOf(getComputedStyle(side.querySelector('.side-sec')).color), sideBg), 4.5]);
    const worst = res.reduce((a, b) => (b[1] / b[2] < a[1] / a[2] ? b : a), res[0]);
    check(res.every((r) => r[1] >= r[2]), tag + '：标题行文字对比度（名称 / 数量 ≥ 4.5，加粗的位置标签 ≥ 3；最低 ' + worst[0] + ' ' + worst[1].toFixed(2) + '）');
  }
  th.set(prevPref, { persist: false }); await raf();
  side.remove();
  check(calls.length === nCalls, '分组、标题行的渲染与折叠不发任何请求');
}

// ---------- 输入框底栏 ----------
async function effortLockChecks() {
  // 纯函数：结果原文 / 提醒文字（null、缺字段都不显示成 "null"）
  const t0 = appliedToast({ ok: true, result: null }, 'session');
  check(t0.text === '仅本会话' && t0.level === 'info', 'result 为 null：提示只写「仅本会话」，不出现 null');
  const t1 = appliedToast({ ok: true, result: null, code: 'result_unknown', warning: '结果未知，请查看终端面板' }, 'session');
  check(t1.text === '结果未知，请查看终端面板' && t1.level === 'warn', '结果未知：warn 提示「结果未知，请查看终端面板」');
  const t2 = appliedToast({ ok: true, result: 'Set model to `Haiku 5.5` for this session only · effort stays max (set by CLAUDE_CODE_EFFORT_LEVEL); xhigh isn\'t saved', code: 'effort_env_locked', warning: 'effort 被环境变量 CLAUDE_CODE_EFFORT_LEVEL=max 锁定' }, 'session');
  check(t2.level === 'warn' && t2.text.includes('Haiku 5.5') && !t2.text.includes('`') && t2.text.includes('锁定'), '/model 结果 + 锁定提醒：原文去掉反引号，附锁定说明');
  check([null, undefined, {}, { result: 5 }, { result: '  ' }].every((r) => !/null|undefined/.test(appliedToast(r, 'save').text) && resultText(r) === ''), '各种缺失 / 非字符串结果都不显示 null / undefined');
  check(effortSaveBlocked('max') !== '' && effortSaveBlocked('high') === '' && effortLockText({}) === '' && effortLockText(null) === '', 'max 档不能保存为默认；没锁定时没有说明');

  const n0 = calls.length;
  show('stream:ready');
  pushMeta({ state: 'idle', link: 'ready', effort: 'max', effort_locked: { level: 'max', source: 'CLAUDE_CODE_EFFORT_LEVEL' } });
  await raf();
  const eb = $('fx-effort-btn');
  check(eb.classList.contains('is-locked') && eb.textContent.trim() === 'max' && !!eb.querySelector('svg.cb-lock'), '锁定时胶囊右半显示实际档位 max + 锁图标');
  check(/CLAUDE_CODE_EFFORT_LEVEL=max 锁定/.test(eb.title) && /实际仍是 max/.test(eb.title) && /重启会话/.test(eb.title), 'tooltip 说明被环境变量锁定、实际档位与解决办法');
  check(getComputedStyle(eb).color !== getComputedStyle($('fx-model-btn')).color, '锁定时 effort 用警示色');
  await click(eb, 'effort 按钮（锁定）');
  let dlg = document.querySelector('dialog.modal[open]');
  const note = dlg && dlg.querySelector('.effort-lock-note');
  check(!!note && note.textContent.includes('CLAUDE_CODE_EFFORT_LEVEL=max') && note.textContent.includes('~/.zshrc'), 'effort 弹窗里提示锁定与清除方法');
  check(!!dlg && dlg.textContent.includes('当前：max'), '弹窗当前档位 = 锁定的 max');
  const saveBtn = () => [...dlg.querySelectorAll('button')].find((b) => b.textContent.startsWith('保存为默认'));
  const why = () => dlg.querySelector('.save-why');
  check(saveBtn().disabled && /只能「仅本会话」/.test(saveBtn().title) && !why().hidden && why().textContent.includes('max'), 'max 档：「保存为默认」置灰并说明原因');
  await click([...dlg.querySelectorAll('.seg-item')].find((b) => b.textContent === 'high'), 'high');
  check(!saveBtn().disabled && why().hidden, '换到 high：「保存为默认」恢复可用');
  await click([...dlg.querySelectorAll('.seg-item')].find((b) => b.textContent === 'max'), 'max');
  check(saveBtn().disabled, '再选 max：重新置灰');
  await click([...dlg.querySelectorAll('.seg-item')].find((b) => b.textContent === 'xhigh'), 'xhigh');
  // 后端：CLI 回报 "CLAUDE_CODE_EFFORT_LEVEL=max overrides this session — …" → 409 effort_env_locked
  const origEffort = actions.applyEffort, origApplied = actions.onApplied;
  const LOCK_MSG = 'effort 被环境变量 CLAUDE_CODE_EFFORT_LEVEL=max 锁定：本会话实际仍是 max。清除该变量（例如 ~/.zshrc 里的 export）后重启会话即可调整';
  actions.applyEffort = async (_s, body) => { record('effort', body); const e = new Error(LOCK_MSG); e.code = 'effort_env_locked'; e.status = 409; throw e; };
  await click([...dlg.querySelectorAll('button')].find((b) => b.textContent === '仅本会话（s）'), '仅本会话（锁定）');
  await sleep(50);
  const errEl = dlg.querySelector('.field-err');
  check(dlg.open && errEl.textContent.startsWith('没有生效：') && errEl.textContent.includes('实际仍是 max') && !/null/.test(errEl.textContent), '锁定：弹窗留着，显示「没有生效」+ 中文说明（不显示 null）');
  let toastText = null;
  actions.applyEffort = async (_s, body) => { record('effort', body); return { ok: true, result: null, state: {}, code: 'result_unknown', warning: '结果未知，请查看终端面板' }; };
  actions.onApplied = (_s, r, scope) => { toastText = appliedToast(r, scope).text; };
  await click([...dlg.querySelectorAll('button')].find((b) => b.textContent === '仅本会话（s）'), '仅本会话（结果未知）');
  await sleep(50);
  check(toastText === '结果未知，请查看终端面板' && !document.querySelector('dialog.modal[open]'), '结果未知：弹窗关闭，提示「结果未知，请查看终端面板」，没有 null');
  actions.applyEffort = origEffort; actions.onApplied = origApplied;
  const ef = calls.slice(n0).filter((c) => c.op === 'effort');
  check(ef.length === 2 && ef.every((c) => c.body.level === 'xhigh' && c.body.scope === 'session' && c.gesture), '两次 POST effort 都来自点击（xhigh / 仅本会话）');
  // 切换模型弹窗里也提示锁定
  await click($('fx-model-btn'), '切换模型（锁定）');
  await sleep(600);
  dlg = document.querySelector('dialog.modal[open]');
  check(!!dlg && !!dlg.querySelector('.effort-lock-note') && dlg.querySelector('.effort-lock-note').textContent.includes('CLAUDE_CODE_EFFORT_LEVEL'), '切换模型弹窗里也提示 effort 被锁定');
  if (dlg) await click([...dlg.querySelectorAll('button')].find((b) => b.textContent.startsWith('取消')), '取消');
  await sleep(300);
  // 解除锁定（重启后）：恢复普通外观
  pushMeta({ effort: 'low', effort_locked: null });
  show('stream:ready');
  await raf();
  check(!$('fx-effort-btn').classList.contains('is-locked') && !$('fx-effort-btn').querySelector('svg.cb-lock') && $('fx-effort-btn').textContent.trim() === 'low', '没有锁定时胶囊恢复普通显示');
  check(calls.slice(n0).every((c) => c.gesture), '本节所有请求都来自点击');
}

async function composerBarChecks() {
  const bar = document.querySelector('.fx .composer-bar');
  const ta = $('fx-input');
  const vis = (el) => !!el && !el.hidden && getComputedStyle(el).display !== 'none' && el.getClientRects().length > 0;
  const part = (k) => statsBar.el.querySelector('.st-' + k);
  const menu = () => document.querySelector('.popover.mode-menu');
  const nCalls = calls.length;
  pushMeta({ state: 'idle', link: 'ready', stats: { context_used: 25000, context_limit: 1000000, context_pct: 3, context_source: 'status', cost_usd: 0.0035, cost_text: '$0.0035', cost_source: 'status', tok_s_last: 35.2 } });
  show('stream:ready');
  await raf();
  // 胶囊：一个外框里两个点击区（左 = 模型，右 = effort），中间细分隔线
  const cap = document.querySelector('#fx-ctl .cb-capsule');
  const kids = cap ? [...cap.children] : [];
  check(kids.length === 3 && kids[0] === $('fx-model-btn') && kids[1].classList.contains('cb-div') && kids[2] === $('fx-effort-btn'), '模型/effort 是一个胶囊、两个点击区（中间分隔线）');
  check($('fx-model-btn').textContent.includes('Haiku 5.5') && $('fx-effort-btn').textContent.trim() === 'low'
    && /切换模型/.test($('fx-model-btn').title) && /调整 effort/.test($('fx-effort-btn').title), '左半显示模型、右半显示 effort，各自 title');
  check(getComputedStyle($('fx-effort-btn')).color !== getComputedStyle($('fx-model-btn')).color, 'effort 用弱化色');
  check($('fx-mode').nextElementSibling === $('fx-cycle') && /Shift\+Tab/.test($('fx-cycle').textContent), '模式 chip 旁保留 Shift+Tab 小按钮');
  // 内容不变不重建（hover 不丢、不闪）
  const capEl = cap, modeEl = $('fx-mode');
  renderChrome(); renderChrome();
  check(document.querySelector('#fx-ctl .cb-capsule') === capEl && $('fx-mode') === modeEl, 'key 不变时底栏控件不重建');
  // 运行中：模型/effort 禁用（title 说原因），模式与 Shift+Tab 仍可用
  show('stream:busy_count');
  await raf();
  check($('fx-model-btn').disabled && $('fx-effort-btn').disabled && /运行中/.test($('fx-model-btn').title) && /运行中/.test($('fx-effort-btn').title), '运行中：胶囊两半都禁用，title 说明原因');
  check(!$('fx-mode').disabled && !$('fx-cycle').disabled, '运行中：权限模式与 Shift+Tab 仍可用');
  check(/运行中/.test(ta.placeholder) && !$('fx-stop').hidden, '运行中：提示写在占位文字里，底栏显示中断');
  check(document.querySelector('#fx-ctl .cb-capsule') !== capEl && $('fx-mode') === modeEl, '只重建变了的部分（胶囊重建、模式 chip 不动）');
  // 已退出：全部禁用，统计变淡
  pushMeta({ state: 'exited' });
  show('stream:ready');
  await raf();
  check($('fx-model-btn').disabled && $('fx-cycle').disabled && !$('fx-mode').disabled && /已退出/.test($('fx-model-btn').title) && /已退出/.test($('fx-cycle').title) && statsBar.el.classList.contains('dead'),
    '已退出：模型/effort/Shift+Tab 禁用（模式列表仍可打开选「需重启」），统计变淡');
  pushMeta({ state: 'idle', link: 'down' });
  await raf();
  check($('fx-model-btn').disabled && $('fx-cycle').disabled && /连接已断开/.test($('fx-cycle').title), '断线：禁用并说明原因');
  pushMeta({ state: 'idle', link: 'ready' });
  await raf();
  check(!$('fx-model-btn').disabled && !$('fx-effort-btn').disabled && !$('fx-cycle').disabled, '恢复后重新可用');
  // 没有会话：清空；再渲染时恢复
  ctl.update(null);
  check(!$('fx-ctl').children.length, '没有活动会话时左侧控件组清空');
  renderChrome();
  check(!!$('fx-model-btn') && !!$('fx-mode'), '重新渲染后恢复');

  // 模式菜单：从底栏向上弹，不出窗口；打开期间 chip 保持填充
  show('dialogs:final');
  await raf();
  await click($('fx-mode'), '模式 chip');
  let pop = menu();
  const cr = $('fx-mode').getBoundingClientRect();
  const pr = pop ? pop.getBoundingClientRect() : null;
  check(!!pop && pr.top >= 0 && pr.bottom <= innerHeight && pr.left >= 0 && pr.right <= innerWidth, '模式菜单完整在窗口内（' + (pr ? Math.round(pr.top) + '–' + Math.round(pr.bottom) : '-') + ' / ' + innerHeight + '）');
  check(!!pr && pr.bottom <= cr.top, '模式菜单在 chip 上方弹出');
  check($('fx-mode').getAttribute('aria-expanded') === 'true' && getComputedStyle($('fx-mode')).backgroundColor !== 'rgba(0, 0, 0, 0)', '菜单打开时 chip 保持填充');
  check(pop && pop.querySelector('.mode-row.on .mode-id')?.textContent === 'manual', '菜单标出当前 manual');
  check(calls.length === nCalls, '打开菜单不发请求');
  // 菜单开着时模式变了（终端里按了 Shift+Tab）：底栏重建，菜单挂到新 chip 上并刷新「当前」
  const oldChip = $('fx-mode');
  show('stream:ready');
  await raf();
  pop = menu();
  check($('fx-mode') !== oldChip && !!pop && $('fx-mode').getAttribute('aria-expanded') === 'true' && pop.querySelector('.mode-row.on .mode-id')?.textContent === 'auto',
    '菜单打开时模式变化：在新 chip 上重新打开，当前 = auto');
  check(document.querySelectorAll('.popover.mode-menu').length === 1, '始终只有一个模式菜单');
  await click([...pop.querySelectorAll('.mode-row')].find((b) => b.querySelector('.mode-id')?.textContent === 'plan'), '选 plan');
  check(calls.at(-1)?.op === 'mode' && calls.at(-1).body.mode === 'plan' && !menu() && $('fx-mode').getAttribute('aria-expanded') === 'false', '选循环里的模式 → mode {plan}，菜单关闭');
  await click($('fx-mode'), '模式 chip');
  await click($('fx-mode'), '模式 chip（再点一次）');
  check(!menu(), '再点一次 chip 收起菜单');
  await click($('fx-mode'), '模式 chip');
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  check(!menu() && $('fx-mode').getAttribute('aria-expanded') === 'false', 'Esc 关闭菜单');

  // 底栏：固定高度、不换行；右侧统计 + 中断 + 发送永远完整可见；按宽度分级折叠
  show('stream:busy_count');
  pushMeta({ stats: { ...s.meta.stats, tok_s_live: 46.3 } });
  await raf();
  const taW = ta.offsetWidth;
  const LEVELS = [
    { w: 720, stopText: true, rate: true, cost: true, pct: true, modeName: true },
    { w: 660, stopText: false, rate: true, cost: true, pct: true, modeName: true },
    { w: 580, stopText: false, rate: false, cost: true, pct: true, modeName: true },
    { w: 500, stopText: false, rate: false, cost: false, pct: true, modeName: true },
    { w: 380, stopText: false, rate: false, cost: false, pct: false, modeName: false },
  ];
  for (const L of LEVELS) {
    bar.style.width = L.w + 'px';
    await raf();
    const br = bar.getBoundingClientRect();
    const send = $('fx-send').getBoundingClientRect(), stop = $('fx-stop').getBoundingClientRect(), st = statsBar.el.getBoundingClientRect();
    const oneRow = [...bar.children].filter(vis).every((c) => { const r = c.getBoundingClientRect(); return r.top >= br.top - 0.5 && r.bottom <= br.bottom + 0.5; });
    check(getComputedStyle(bar).flexWrap === 'nowrap' && bar.offsetHeight === 32 && oneRow, L.w + 'px：底栏一行、高 32px（' + bar.offsetHeight + '）');
    check(send.right <= br.right + 0.5 && stop.right <= send.left && st.right <= stop.left + 0.5 && vis(part('ctx').querySelector('.st-ring')), L.w + 'px：圆环、中断、发送完整可见且不重叠');
    const got = { stopText: vis($('fx-stop').querySelector('.stop-text')), rate: vis(part('rate')), cost: vis(part('cost')), pct: vis(part('ctx').querySelector('.st-pct')), modeName: vis($('fx-mode').querySelector('.cb-label')) };
    check(Object.keys(got).every((k) => got[k] === L[k]), L.w + 'px：折叠 ' + JSON.stringify(got));
  }
  bar.style.width = '';
  await raf();
  check(ta.offsetWidth === taW, '底栏折叠不影响输入框宽度');
  // 很长的模型名：省略号截断，不撑宽输入框、不换行
  const box = bar.closest('.composer-box');
  const keepModel = s.modelInfo.model;
  s.modelInfo.model = 'claude-some-extremely-long-custom-model-name-for-testing[1m]';
  renderChrome();
  await raf();
  const lbl = $('fx-model-btn').querySelector('.cb-label');
  check(lbl.clientWidth <= 161 && lbl.scrollWidth > lbl.clientWidth && box.scrollWidth <= box.clientWidth && bar.offsetHeight === 32 && ta.offsetWidth === taW,
    '长模型名省略号截断（' + lbl.clientWidth + 'px），底栏不撑宽、不换行');
  s.modelInfo.model = keepModel;
  renderChrome();
  show('stream:interrupted');
  await raf();
  check(calls.length === nCalls + 1, '底栏检查期间只有用户点击的一次 mode 请求');
}

// ---------- 可视化（§10）：桩 api（stat/open）+ 桩 blob 取回，不连后端 ----------
const VCWD = '/Users/demo/proj';
const VFILES = {
  [VCWD + '/site/index.html']: { kind: 'html', url: '/static/dev/viz_demo.html', size: 420 },
  [VCWD + '/site/style.css']: { kind: 'text', url: '/static/dev/viz_demo.css', size: 180 },
  [VCWD + '/shot.png']: { kind: 'image', url: '/static/favicon.png', size: 2048 },
};
let vizStatCalls = 0;
const vizBatchSizes = [];
let vizPanel = null;
function ensureVizPanel() {
  if (!vizPanel) vizPanel = new PreviewPanel({ host: document.querySelector('.fx-main'), chgKey: FX_CHG_KEY, onState: () => fxChgSync() });
  return vizPanel;
}
const vizApi = {
  token: '',
  sessionPath: (sid, tail) => '/api/sessions/' + encodeURIComponent(sid) + tail,
  post: async (path, body) => {
    if (path.endsWith('/local/stat') || path.endsWith('/srvfile/stat')) {   // 「可视化示例」勾「服务器」时点 chip 走 srvfile/stat（§10.10）
      vizStatCalls++;
      vizBatchSizes.push((body.paths || []).length);
      if ((body.paths || []).length > STAT_BATCH) throw new Error('一次 stat 超过 ' + STAT_BATCH + ' 个路径');
      return { results: (body.paths || []).map((p) => {
        const abs = p.startsWith('/') ? p : VCWD + '/' + p;
        const f = VFILES[abs] || (abs.startsWith(VCWD + '/many/') ? { kind: 'text', url: '/static/dev/viz_demo.css', size: 1 } : null);
        return f ? { path: p, abs, real: abs, exists: true, type: 'file', ok: true, mtime: 1, ...f }
          : { path: p, abs, exists: false, ok: false, reason: '文件不存在' };
      }) };
    }
    if (path.endsWith('/local/open')) { record('local/open', body); return { ok: true }; }
    throw new Error('回放页不支持：' + path);
  },
};

function refPng(ref) {
  const c = document.createElement('canvas');
  c.width = 480; c.height = 300;
  const g = c.getContext('2d');
  let hsh = 0;
  for (const ch of ref) hsh = (hsh * 31 + ch.charCodeAt(0)) >>> 0;
  g.fillStyle = 'hsl(' + (hsh % 360) + ', 55%, 60%)';
  g.fillRect(0, 0, 480, 300);
  g.fillStyle = '#fff';
  g.font = 'bold 28px sans-serif';
  g.fillText('ccr_ref ' + ref.slice(-6), 30, 160);
  return c.toDataURL('image/png');
}

function vizEntries() {
  const U = (i) => '00000000-0000-4000-8000-' + String(i).padStart(12, '0');
  const ref = (n) => ({ type: 'image', source: { type: 'ccr_ref', media_type: 'image/png', ref: 'm.59dd443a-c637-4148-bf1c-fd720f2e5b1f.' + (1000 * n) + '.0-1.' + String(n).repeat(16).slice(0, 16), bytes: 150000 } });
  const base = (i, type, extra) => ({ type, uuid: U(i), cwd: VCWD, sessionId: '59dd443a-c637-4148-bf1c-fd720f2e5b1f', timestamp: new Date().toISOString(), ...extra });
  const asst = (i, content, stop) => base(i, 'assistant', { message: { id: 'msg_v' + i, role: 'assistant', content, stop_reason: stop || 'tool_use' } });
  const res = (i, id, content, tur) => base(i, 'user', { message: { role: 'user', content: [{ tool_use_id: id, type: 'tool_result', content }] }, ...(tur ? { toolUseResult: tur } : {}) });
  const html = '<!doctype html>\n<html>\n<head>\n<title>x</title>\n</head>\n<body>\n<p>hi</p>\n</body>\n</html>';
  return [
    base(1, 'user', { message: { role: 'user', content: [{ type: 'text', text: '做个网页并截图看看' }, ref(1)] }, origin: { kind: 'human' }, promptSource: 'typed' }),
    asst(2, [{ type: 'thinking', thinking: '先截图，再写页面。' }]),
    asst(3, [{ type: 'tool_use', id: 'tv1', name: 'mcp__chrome-devtools__take_screenshot', input: { pageId: 1 } }]),
    res(4, 'tv1', [{ type: 'text', text: 'Took a screenshot' }, { ...ref(2), source: { ...ref(2).source, ref: ref(2).source.ref.replace('.0-1.', '.0-1.') } }, { type: 'text', text: '[Image]' }],
      [{ type: 'text', text: 'Took a screenshot' }, { type: 'image', source: { type: 'ccr_dropped', media_type: 'image/png' } }]),
    asst(5, [{ type: 'tool_use', id: 'tv2', name: 'Write', input: { file_path: VCWD + '/site/index.html', content: html } }]),
    res(6, 'tv2', 'File created successfully', { type: 'create', filePath: VCWD + '/site/index.html', content: html, structuredPatch: [] }),
    asst(7, [{ type: 'tool_use', id: 'tv3', name: 'Edit', input: { file_path: VCWD + '/site/index.html', old_string: '<p>hi</p>', new_string: '<h1>标题</h1>\n<p>hi!</p>' } }]),
    res(8, 'tv3', 'The file has been updated', { filePath: VCWD + '/site/index.html', structuredPatch: [{ oldStart: 5, oldLines: 3, newStart: 5, newLines: 4, lines: [' </head>', ' <body>', '-<p>hi</p>', '+<h1>标题</h1>', '+<p>hi!</p>', ' </body>'] }] }),
    asst(9, [{ type: 'tool_use', id: 'tv4', name: 'Bash', input: { command: 'python3 tools/shot.py --out ' + VCWD + '/shot.png && echo ok' } }]),
    res(10, 'tv4', 'saved ' + VCWD + '/shot.png\nServing on http://localhost:5173/'),
    asst(11, [{ type: 'tool_use', id: 'tv5', name: 'Read', input: { file_path: VCWD + '/shot.png' } }]),
    res(12, 'tv5', [ref(3)], { type: 'image', file: { type: 'image/png', ccr_dropped: 'base64' } }),
    asst(13, [{ type: 'text', text: '完成：页面在 `site/index.html:5`，样式 [style](site/style.css)，缺少 `site/nope.txt`。\n\n预览 http://localhost:5173/ ，本程序 http://127.0.0.1:' + (location.port || '80') + '/ 不应可预览。\n\n![截图](shot.png)' }], 'end_turn'),
    base(14, 'system', { subtype: 'turn_duration', durationMs: 1234, content: '' }),
  ];
}

async function vizChecks() {
  ensureVizPanel();
  initViz({ api: vizApi, getConfig: () => ({}), toast: (t) => log('（toast）' + t), panel: vizPanel });
  _setBlobLoader((d) => refPng(d.ref));
  const nCalls = calls.length;
  s.localOverride = true;
  pushTranscript(vizEntries(), true);
  await raf(); flushNow(s);
  // 滚到各处，触发 IntersectionObserver 懒加载
  for (const el of qa('.cimg')) { el.scrollIntoView({ block: 'center' }); await raf(); }
  await sleep(300);
  const imgs = qa('.cimg');
  check(qa('.k-user .cimg').length === 1 && qa('.k-tool .viewed-imgs .cimg').length === 2, '用户粘贴图 + MCP 截图 + Read 缩略图条（ccr_ref）');
  check(imgs.filter((e) => e._img && e._img.ref).every((e) => e.classList.contains('ready')), 'ccr_ref 图片进入视口后加载完成（' + imgs.length + ' 张）');
  check(!!q('.k-thinking') && q('.k-thinking .think-label').textContent === '思考', '思考块标签为“思考”');
  check(/5 个工具调用：/.test((q('.tool-group-sum') || {}).textContent || '') && /读取 1 个文件/.test(q('.tool-group-sum').textContent), '工具组摘要：' + ((q('.tool-group-sum') || {}).textContent || ''));
  // 灯箱
  const readThumb = q('.k-tool.cat-read .viewed-imgs .cimg');
  readThumb.click();
  await sleep(120);
  const lb = document.querySelector('.lb');
  check(!!lb && /2 \/ 2|1 \/ 2/.test(lb.querySelector('.lb-count').textContent), '点击缩略图打开灯箱（同组 MCP 截图 + Read 图片可切换）');
  const before = lb.querySelector('.lb-count').textContent;
  const finder = () => !lb.querySelector('.lb-btn[title="在 Finder 中显示"]').hidden;
  check(finder() && !!lb.querySelector('.lb-btn[title="复制图片"]') && !!lb.querySelector('.lb-btn[title="下载"]'), '本机 Read 图片：复制 / 下载 / 在 Finder 中显示');
  lb.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
  await sleep(80);
  check(lb.querySelector('.lb-count').textContent !== before && !finder(), '← 切换到上一张（MCP 截图没有本机路径 → 不显示 Finder 按钮）：' + lb.querySelector('.lb-count').textContent);
  const z0 = lb.querySelector('.lb-zoom').textContent;
  lb.dispatchEvent(new KeyboardEvent('keydown', { key: '+', bubbles: true }));
  await raf();
  const wheel = new WheelEvent('wheel', { deltaY: -120, ctrlKey: true, bubbles: true, cancelable: true, clientX: 300, clientY: 300 });
  const notPrevented = lb.querySelector('.lb-view').dispatchEvent(wheel);
  check(lb.querySelector('.lb-zoom').textContent !== z0 && !notPrevented, 'Ctrl+滚轮/+ 缩放（拦住页面缩放）：' + z0 + ' → ' + lb.querySelector('.lb-zoom').textContent);
  lb.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await raf();
  check(!document.querySelector('.lb'), 'Esc 关闭灯箱');
  // 文件 chip（本机）
  await sleep(120);
  const chips = qa('.k-text .fchip');
  check(chips.length >= 3, '回复里的路径变成文件 chip（' + chips.map((c) => c.textContent).join(' / ') + '）');
  check(chips.some((c) => c.classList.contains('ok')) && chips.some((c) => c.classList.contains('missing')), '存在的文件可点、不存在的灰显');
  const pvBtns = qa('.k-text .url-pv');
  check(pvBtns.length === 1, 'localhost 链接可“预览”，本 app 端口不行');
  check(qa('.k-text .cimg.md-img').length === 1, 'markdown 本机图片显示缩略图');
  // 本轮改动 / 网页预览 / 本轮图片
  const tc = q('.k-turnsum .tc-changes');
  check(!!tc && /1 个文件/.test(tc.textContent) && /\+11/.test(tc.textContent) && /−1/.test(tc.textContent), '本轮改动：1 个文件 +11 −1（新建 9 行 + Edit +2/−1）');
  const web = q('.k-turnsum .web-card');
  await sleep(150);
  const fr = web && web.querySelector('iframe');
  check(!!fr && fr.getAttribute('sandbox') === 'allow-scripts allow-forms allow-popups allow-modals', '网页预览卡片：沙箱 iframe（无 allow-same-origin）');
  check(!!q('.k-turnsum .tc-images') && !q('.k-turnsum .tc-images').hidden, '本轮图片（Bash 输出里的 png 经 stat 确认）');
  // diff 面板
  await click(q('.tc-row'), '本轮改动文件行');
  await raf();
  const pv = document.querySelector('.pv');
  check(pv && !pv.hidden && !!pv.querySelector('.pv-diff .diff.patch'), '点击文件 → 右侧面板 diff 标签');
  const lns = [...pv.querySelectorAll('.pv-diff .diff.patch .diff-del .ln')].map((e) => e.textContent).filter(Boolean);
  check(lns.includes('7'), 'diff 带旧行号（删除行 = 7）');
  // 文件标签
  await click(chips.find((c) => c.classList.contains('ok')), '文件 chip');
  await sleep(200);
  check(pv.querySelectorAll('.pv-tab').length === 2, '文件 chip → 新标签（' + [...pv.querySelectorAll('.pv-tab')].map((t) => t.textContent).join(' | ') + '）');
  check(!!pv.querySelector('.pv-view iframe[sandbox]') || !!pv.querySelector('.pv-gutter'), 'HTML 用沙箱 iframe / 文本带行号');
  // 一次超过 200 个路径：分批查询，第 200 个以后的不能被当成"不存在"
  const many = Array.from({ length: 250 }, (_, k) => VCWD + '/many/f' + k + '.txt');
  const nb = vizBatchSizes.length;
  const manyRes = await Promise.all(many.map((p) => statPath(s, p)));
  const sizes = vizBatchSizes.slice(nb);
  check(manyRes.every((r) => r && r.exists) && sizes.length >= 2 && sizes.every((n) => n <= STAT_BATCH),
    'stat 250 个路径：分 ' + sizes.length + ' 批（' + sizes.join('+') + '），全部拿到真实结果');
  // 引用图片同一时间最多取 BLOB_CONCURRENCY 张
  let active = 0, peak = 0;
  await Promise.all(Array.from({ length: 6 }, () => limitBlob(async () => { active++; peak = Math.max(peak, active); await sleep(25); active--; })));
  check(peak === BLOB_CONCURRENCY && BLOB_CONCURRENCY === 2, '引用图片取回并发上限 = ' + peak);
  // 彩色区分
  const root = document.documentElement;
  const th = window.ccrTheme;
  const catColor = () => getComputedStyle(q('.k-tool.cat-edit')).borderLeftColor;
  const varOf = (v) => getComputedStyle(root).getPropertyValue(v).trim();
  th.setColorful(true, { persist: false });
  await raf();
  const onColor = catColor();
  th.setColorful(false, { persist: false });
  await raf();
  const offColor = catColor();
  th.setColorful(true, { persist: false });
  check(onColor !== offColor, '彩色区分开/关：编辑类工具色条 ' + onColor + ' / ' + offColor);
  const prevPref = th.pref();
  th.set('light', { persist: false }); await raf();
  const lightVars = ['--c-read', '--c-edit', '--c-think', '--c-user-bg'].map(varOf);
  const lum = (c) => { const m = c.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const probe = h('span', { class: 'muted' }, 'x');
  document.body.appendChild(probe);
  const mutedRatio = ratio(getComputedStyle(probe).color, getComputedStyle(document.body).backgroundColor);
  th.set('dark', { persist: false }); await raf();
  const darkVars = ['--c-read', '--c-edit', '--c-think', '--c-user-bg'].map(varOf);
  th.set(prevPref, { persist: false }); await raf();
  probe.remove();
  check(lightVars.every(Boolean) && darkVars.every(Boolean) && lightVars.join() !== darkVars.join(), '语义色在浅色/深色都有定义且不同');
  check(mutedRatio >= 4.5, '浅色弱化文字对比度 ' + mutedRatio.toFixed(2) + ':1 ≥ 4.5');
  // 服务器会话：只有改动卡片 + diff，文件 chip 点了预览服务器文件（14j 节），没有网页/图片预览
  vizPanel.closeAll();
  s.localOverride = false;
  pushTranscript(vizEntries(), true);
  await raf(); flushNow(s);
  await sleep(100);
  check(!q('.web-card') && !q('.tc-images') && !!q('.tc-changes'), '服务器会话：保留本轮改动，无网页预览 / 本轮图片');
  check(qa('.k-text .fchip').every((c) => c.classList.contains('remote')) && !q('.k-text .url-pv'), '服务器会话：chip 走服务器文件预览（remote，点了才查），不提供 localhost 预览');
  await click(q('.tc-row'), '服务器会话的改动行');
  await raf();
  check(!!document.querySelector('.pv:not([hidden]) .pv-diff'), '服务器会话也能打开 diff（数据来自 structuredPatch）');
  vizPanel.closeAll();
  s.localOverride = null;
  check(calls.length === nCalls, '可视化部分没有发出任何按键/操作请求（stat ' + vizStatCalls + ' 次，只读）');
}

// ---------- 右侧「代码改动」栏（changes.js，SPEC §10.4「改动」标签） ----------
// 构造的两轮编辑（形状照真实 CLI 的 toolUseResult：Edit / Write create / NotebookEdit / 被拒绝 / 失败 / 截断）+ 一个子代理的编辑。
const CCWD = '/Users/demo/calc';
const CHG_AGENT = 'agfx1';
function chgEntries() {
  const U = (i) => '00000000-0000-4000-9000-' + String(i).padStart(12, '0');
  const t0 = Date.parse('2026-11-24T06:00:00Z');
  const base = (i, type, extra) => ({ type, uuid: U(i), cwd: CCWD, sessionId: 'chg-fx', timestamp: new Date(t0 + i * 1000).toISOString(), ...extra });
  const say = (i, text) => base(i, 'user', { message: { role: 'user', content: [{ type: 'text', text }] }, origin: { kind: 'human' }, promptSource: 'typed' });
  const use = (i, id, name, input, extra) => base(i, 'assistant', { message: { id: 'msg_c' + i, role: 'assistant', content: [{ type: 'tool_use', id, name, input }], stop_reason: 'tool_use' }, ...extra });
  const res = (i, id, content, tur, extra = {}) => base(i, 'user', { message: { role: 'user', content: [{ tool_use_id: id, type: 'tool_result', content, ...(extra.isError ? { is_error: true } : {}) }] },
    ...(tur !== undefined ? { toolUseResult: tur } : {}), ...(extra.denied ? { toolDenialKind: 'user-rejected' } : {}), ...(extra.truncated ? { truncated: true } : {}) });
  const done = (i, text) => [base(i, 'assistant', { message: { id: 'msg_c' + i, role: 'assistant', content: [{ type: 'text', text }], stop_reason: 'end_turn' } }),
    base(i + 1, 'system', { subtype: 'turn_duration', durationMs: 3000, content: '' })];
  const calc = CCWD + '/calc.py', notes = CCWD + '/notes.txt', strs = CCWD + '/utils/strings.py';
  const p1 = [{ oldStart: 4, oldLines: 3, newStart: 4, newLines: 13, lines: [' ', ' def sub(a, b):', '     return a - b', '+', '+', '+def mul(a, b):', '+    return a * b',
    '+', '+', '+def div(a, b):', '+    if b == 0:', '+        raise ValueError("division by zero")', '+    return a / b'] }];
  const p2 = [{ oldStart: 1, oldLines: 2, newStart: 1, newLines: 3, lines: [' hello', '-world', '+there', '+bye'] }];
  const p3 = [{ oldStart: 14, oldLines: 3, newStart: 14, newLines: 3, lines: ['     if b == 0:', '         raise ValueError("division by zero")', '-    return a / b', '+    return a // b'] }];
  const shout = 'def shout(s):\n    return s.upper() + "!"\n';
  const big = Array.from({ length: 1200 }, (_, k) => k + ',' + (k * 7 % 13)).join('\n') + '\n';
  const nb = CCWD + '/nb/analysis.ipynb';
  return [
    say(1, '把 calc.py 加上 mul 和 div，notes.txt 的 world 改成 there 并加一行 bye，再新建 utils/strings.py'),
    use(2, 'c1', 'Edit', { file_path: calc, old_string: '    return a - b\n', new_string: '    return a - b\n\n\ndef mul…' }),
    res(3, 'c1', 'The file ' + calc + ' has been updated successfully.', { filePath: calc, oldString: '…', newString: '…', originalFile: 'x', structuredPatch: p1, userModified: false, replaceAll: false }),
    use(4, 'c2', 'Edit', { file_path: notes, old_string: 'world', new_string: 'there\nbye' }),
    res(5, 'c2', 'The file ' + notes + ' has been updated successfully.', { filePath: notes, structuredPatch: p2, userModified: false, replaceAll: false }),
    use(6, 'c3', 'Write', { file_path: strs, content: shout }),
    res(7, 'c3', 'File created successfully at: ' + strs, { type: 'create', filePath: strs, content: shout, structuredPatch: [], originalFile: null }),
    use(8, 'c4', 'Write', { file_path: '/tmp/denied.txt', content: 'no' }),
    res(9, 'c4', "The user doesn't want to proceed with this tool use. The tool use was rejected", 'User rejected tool use', { isError: true, denied: true }),
    use(10, 'c5', 'Edit', { file_path: notes, old_string: 'nope', new_string: 'x' }),
    res(11, 'c5', '<tool_use_error>String to replace not found in file.</tool_use_error>', 'Error: String to replace not found in file.', { isError: true }),
    ...done(12, '完成：calc.py、notes.txt、utils/strings.py。'),
    say(20, '再把 div 改成整除，更新笔记本，导出数据，并让子代理改配置'),
    use(21, 'c6', 'Edit', { file_path: calc, old_string: 'a / b', new_string: 'a // b' }),
    res(22, 'c6', 'The file ' + calc + ' has been updated successfully.', { filePath: calc, structuredPatch: p3, userModified: false, replaceAll: false }),
    use(23, 'c7', 'NotebookEdit', { notebook_path: nb, cell_id: 'c3', new_source: 'df = load()\ndf.describe()' }),
    res(24, 'c7', 'Updated cell c3 with df = load()…', { new_source: 'df = load()\ndf.describe()', old_source: 'df = load()', cell_id: 'c3', cell_type: 'code', notebook_path: nb, original_file: '{}', updated_file: '{}' }),
    use(25, 'c8', 'Write', { file_path: CCWD + '/data/big.csv', content: big }, { truncated: true }),
    res(26, 'c8', 'File created successfully at: ' + CCWD + '/data/big.csv', { type: 'create', filePath: CCWD + '/data/big.csv', content: big, structuredPatch: [], originalFile: null }, { truncated: true }),
    use(27, 'c9', 'Agent', { description: '改配置', prompt: '把 conf/app.toml 的端口改成 8080', subagent_type: 'general-purpose' }),
    res(28, 'c9', 'Async agent launched successfully.\nagentId: ' + CHG_AGENT, { isAsync: true, status: 'async_launched', agentId: CHG_AGENT, description: '改配置' }),
    ...done(29, '都改好了。'),
  ];
}
function chgSubagent(withEdit2) {
  const ent = (i, type, extra) => ({ type, isSidechain: true, agentId: CHG_AGENT, uuid: 'sa-' + i, timestamp: new Date(Date.parse('2026-11-24T06:00:30Z') + i * 1000).toISOString(), ...extra });
  const conf = CCWD + '/conf/app.toml';
  const entries = [
    ent(0, 'user', { parentUuid: null, message: { role: 'user', content: '把 conf/app.toml 的端口改成 8080' } }),
    ent(1, 'assistant', { message: { role: 'assistant', content: [{ type: 'tool_use', id: 'sx1', name: 'Write', input: { file_path: conf, content: 'port = 8080\n' } }] } }),
    ent(2, 'user', { toolUseResult: { type: 'create', filePath: conf, content: 'port = 8080\n', structuredPatch: [], originalFile: null },
      message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'sx1', content: 'File created successfully at: ' + conf }] } }),
  ];
  if (withEdit2) entries.push(
    ent(3, 'assistant', { message: { role: 'assistant', content: [{ type: 'tool_use', id: 'sx2', name: 'Edit', input: { file_path: CCWD + '/conf/extra.toml' } }] } }),
    ent(4, 'user', { toolUseResult: { filePath: CCWD + '/conf/extra.toml', structuredPatch: [{ oldStart: 1, oldLines: 1, newStart: 1, newLines: 1, lines: ['-a = 1', '+a = 2'] }] },
      message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'sx2', content: 'ok' }] } }));
  return { type: 'subagent', agent_id: CHG_AGENT, gen: 1, from: 0, total: entries.length, entries,
    summary: { tool_use_id: 'c9', agent_id: CHG_AGENT, description: '改配置', agent_type: 'general-purpose', status: 'completed', tool_uses: entries.length > 3 ? 2 : 1 } };
}

async function changesChecks() {
  const pv = ensureVizPanel();
  initViz({ api: vizApi, getConfig: () => ({}), toast: (t) => log('（toast）' + t), panel: pv });
  const nCalls = calls.length, nStat = vizStatCalls;
  let fetches = 0;
  const origFetch = window.fetch;
  window.fetch = (...a) => { fetches++; return origFetch(...a); };
  try {
    pv.closeAll();
    localStorage.removeItem(FX_CHG_KEY);
    s.localOverride = false;   // 先按服务器会话：数据只来自 transcript
    // 空状态：fixtures 里真实的 transcript 只有两个被拒绝的工具调用（Bash / Write）→ 不计入
    pushTranscript(transcriptEntries(), true);
    await raf(); flushNow(s);
    renderChrome();
    check(fxChgBtn.count.hidden && fxChgBtn.el.getAttribute('aria-pressed') === 'false', '会话头「改动」按钮：没有改动时不显示角标、未按下');
    await click(fxChgBtn.el, '会话头「改动」按钮');
    await raf();
    const root = pv.root;
    const tabs = () => [...root.querySelectorAll('.pv-tab')];
    const view = () => root.querySelector('.chg');
    check(!root.hidden && tabs().length === 1 && tabs()[0].classList.contains('is-chg') && !tabs()[0].querySelector('.pv-tab-x'), '打开右侧栏：「改动」标签在最左、不可关闭');
    check(!!view() && !view().querySelector('.chg-empty').hidden && /本会话还没有改动文件/.test(view().textContent), '空状态：真实 transcript 里被拒绝的 Write / Bash 不计入（' + (view() && view().querySelector('.chg-empty-t').textContent) + '）');
    check(fxChgBtn.el.getAttribute('aria-pressed') === 'true' && fxChgBtn.el.classList.contains('on'), '按钮显示按下状态');
    check(localStorage.getItem(FX_CHG_KEY) === '1', '开关状态记在 localStorage（回放页用自己的键）');
    check(pv.barEl.hidden && (view().querySelector('.chg-foot') || {}).textContent === NOTE_TEXT, '底部固定一行口径说明；文件标签的地址栏隐藏');
    // 两轮编辑 + 子代理
    pushTranscript(chgEntries(), true);
    applyEnvelope(state, env('subagent', chgSubagent(false)));
    await raf(); flushNow(s);
    await raf();
    const rows = () => [...view().querySelectorAll('.chg-file')];
    const rowOf = (rel) => rows().find((r) => r.querySelector('.chg-path').textContent === rel);
    const rels = rows().map((r) => r.querySelector('.chg-path').textContent);
    check(rels.join(',') === 'calc.py,conf/app.toml,data/big.csv,nb/analysis.ipynb,notes.txt,utils/strings.py', '文件列表（相对 cwd、按路径排序；被拒绝 / 失败的不算）：' + rels.join(', '));
    const sum = view().querySelector('.chg-sum').textContent;
    check(/6 个文件/.test(sum) && /\+1\.2k/.test(sum) && /−2/.test(sum), '汇总「6 个文件 +1.2k −2」（compact 计数）：' + sum);
    check(fxChgBtn.count.textContent === '6' && !fxChgBtn.count.hidden && (tabs()[0].querySelector('.pv-tab-n') || {}).textContent === '6', '会话头按钮与标签的角标 = 6');
    const calcRow = rowOf('calc.py');
    check(/2 次/.test(calcRow.textContent) && calcRow.querySelector('.chg-add').textContent === '+11' && calcRow.querySelector('.chg-del').textContent === '−1', 'calc.py：2 次编辑合并 +11 −1');
    const strRow = rowOf('utils/strings.py');
    check(!!strRow.querySelector('.chg-tag.is-new') && !strRow.querySelector('.chg-del') && strRow.querySelector('.chg-add').textContent === '+2', '新建文件标「新建」，0 的一侧不显示（+2）');
    check(!!rowOf('conf/app.toml').querySelector('.chg-tag.is-sub') && /子代理/.test(rowOf('conf/app.toml').textContent), '子代理改的文件标「子代理」');
    check(!!rowOf('data/big.csv').querySelector('.chg-tag.is-approx'), '截断的 Write 标「≈」（行数可能不准）');
    check(!!rowOf('nb/analysis.ipynb').querySelector('.chg-tag.is-nb') && rowOf('nb/analysis.ipynb').querySelector('.chg-add').textContent === '+1', 'NotebookEdit 计入并标「单元格」（old/new_source LCS：+1）');
    const dirEl = rowOf('utils/strings.py').querySelector('.chg-dir'), nameEl = rowOf('utils/strings.py').querySelector('.chg-name');
    check(dirEl.textContent === 'utils/' && nameEl.textContent === 'strings.py' && Number(getComputedStyle(nameEl).fontWeight) >= 600 && getComputedStyle(dirEl).color !== getComputedStyle(nameEl).color, '目录灰色、文件名加粗');
    // 按轮
    const sel = view().querySelector('.chg-scope');
    const opts = [...sel.options].map((o) => o.textContent);
    check(sel.options.length === 3 && sel.value === 'all' && opts[0] === '本会话（全部 2 轮）' && /^第 2 轮 · /.test(opts[1]) && /^第 1 轮 · /.test(opts[2]) && /（4 个文件）/.test(opts[1]),
      '范围选择：本会话 + 有改动的轮（最新在上）：' + opts.join(' | '));
    const q1 = '把 calc.py 加上 mul 和 div，notes.txt 的 world 改成 there 并加一行 bye，再新建 utils/strings.py';
    check(opts[2].includes(' · ' + q1.slice(0, 30) + '…（') && /\d\d:\d\d/.test(opts[2]), '轮的选项 = 第一句提问前 30 字 + 时间：' + opts[2]);
    sel.value = '1';
    sel.dispatchEvent(new Event('change'));
    await raf();
    check(rows().map((r) => r.querySelector('.chg-path').textContent).join(',') === 'calc.py,notes.txt,utils/strings.py' && /3 个文件/.test(view().querySelector('.chg-sum').textContent) && !/2 次/.test(rowOf('calc.py').textContent),
      '切到第 1 轮：3 个文件，calc.py 只算这一轮的 1 次');
    // 展开 diff（行号）
    await click(rowOf('calc.py').querySelector('.chg-row'), 'calc.py 行');
    const body = rowOf('calc.py').querySelector('.chg-body');
    const newNos = [...body.querySelectorAll('.diff.patch .diff-add .ln:nth-child(2)')].map((e) => e.textContent);
    check(!body.hidden && !!body.querySelector('.diff-hunk') && newNos[0] === '7' && newNos[newNos.length - 1] === '16', '展开 diff：hunk 头 + 新行号 7…16（' + newNos.length + ' 行新增）');
    check(rowOf('calc.py').querySelector('.chg-row').getAttribute('aria-expanded') === 'true', '展开状态 aria-expanded=true');
    sel.value = 'all';
    sel.dispatchEvent(new Event('change'));
    await raf();
    await click(rowOf('calc.py').querySelector('.chg-row'), 'calc.py 行（本会话）');
    const secs = [...rowOf('calc.py').querySelectorAll('.chg-sec')];
    check(secs.length === 2 && /第 1 次/.test(secs[0].textContent) && /第 1 轮/.test(secs[0].textContent) && /第 2 次/.test(secs[1].textContent) && /第 2 轮/.test(secs[1].textContent),
      '本会话范围：逐次编辑按时间顺序，每段标出第几轮');
    const delNos = [...secs[1].querySelectorAll('.diff-del .ln')].map((e) => e.textContent).filter(Boolean);
    check(delNos.includes('16'), '第 2 次编辑的旧行号（删除行 = 16）');
    // 懒渲染：1200 行的 Write 先放占位，进入视口才渲染
    gesture++; rowOf('data/big.csv').querySelector('.chg-row').click(); gesture--;
    const lazy0 = !!rowOf('data/big.csv').querySelector('.chg-lazy');   // 同步看：IntersectionObserver 回调之前还是占位
    for (let k = 0; k < 10 && rowOf('data/big.csv').querySelector('.chg-lazy'); k++) { rowOf('data/big.csv').scrollIntoView({ block: 'start' }); await sleep(50); }
    check(lazy0 && !rowOf('data/big.csv').querySelector('.chg-lazy') && rowOf('data/big.csv').querySelectorAll('.diff-add').length === 1200,
      '大 diff（1200 行 > ' + EAGER_LINES + '）先占位，进入视口才渲染');
    check(/已截断/.test(rowOf('data/big.csv').textContent), '截断的编辑在 diff 段头标「已截断，行数可能不准」');
    // 全部展开 / 折叠
    const allBtn = view().querySelector('.chg-all');
    await click(allBtn, '全部展开');
    check(rows().every((r) => r.classList.contains('open')) && /全部折叠/.test(allBtn.textContent), '全部展开');
    await click(allBtn, '全部折叠');
    check(rows().every((r) => !r.classList.contains('open')) && /全部展开/.test(allBtn.textContent), '全部折叠');
    // 不改变时间线：面板里的展开 / 子代理带来的新改动都不动时间线
    const tl = s.view.root;
    tl.scrollTop = Math.max(0, tl.scrollHeight / 2);
    s.view.stick = false;
    await raf();
    const before = [tl.scrollHeight, tl.scrollTop, s.view.col.getBoundingClientRect().width, $('fx-header').getBoundingClientRect().height];
    const nodeCalc = rowOf('calc.py');
    await click(rowOf('notes.txt').querySelector('.chg-row'), 'notes.txt 行');
    applyEnvelope(state, env('subagent', chgSubagent(true)));
    await raf(); flushNow(s); await raf();
    const after = [tl.scrollHeight, tl.scrollTop, s.view.col.getBoundingClientRect().width, $('fx-header').getBoundingClientRect().height];
    check(rows().length === 7 && fxChgBtn.count.textContent === '7' && !!rowOf('conf/extra.toml'), '实时：子代理的新编辑到达后自动更新（7 个文件，角标 7）');
    check(rowOf('calc.py') === nodeCalc && rowOf('notes.txt').classList.contains('open'), '实时更新复用未变化的文件节点、保留展开状态');
    check(after.join() === before.join(), '时间线 scrollHeight / scrollTop / 列宽、会话头高度都不变（' + before.join(',') + ' → ' + after.join(',') + '）');
    // 联动：「本轮改动」卡片 → 打开右侧栏、切到那一轮、展开并滚到那个文件
    sel.value = 'all';
    sel.dispatchEvent(new Event('change'));
    await click(fxChgBtn.el, '会话头「改动」按钮（关闭）');
    check(root.hidden && fxChgBtn.el.getAttribute('aria-pressed') === 'false' && localStorage.getItem(FX_CHG_KEY) === '0', '再点按钮关闭（没有其它标签时整个面板隐藏），记住关闭');
    const card = qa('.k-turnsum .tc-changes')[0];
    const notesBtn = card && [...card.querySelectorAll('.tc-row')].find((b) => b.textContent.includes('notes.txt'));
    await click(notesBtn, '第 1 轮「本轮改动」卡片的 notes.txt');
    await raf();
    const hit = rowOf('notes.txt');
    const sc = view().querySelector('.chg-scroll');
    const off = hit.getBoundingClientRect().top - sc.getBoundingClientRect().top;
    check(!root.hidden && view().querySelector('.chg-scope').value === '1' && hit.classList.contains('open') && hit.classList.contains('chg-hit') && off >= -1 && off < 40,
      '联动：切到第 1 轮、只展开 notes.txt（其它折叠）并滚到顶部（偏移 ' + Math.round(off) + 'px）');
    check(rows().filter((r) => r.classList.contains('open')).length === 1, '联动时这一轮的其它文件折叠');
    check([...hit.querySelectorAll('.diff-del .ln')].some((e) => e.textContent === '2'), '联动打开的 diff 带旧行号（删除行 = 2）');
    check(!!hit.querySelector('.chg-acts .btn[title^="在预览面板中打开"]') && !!hit.querySelector('.chg-acts .btn[title^="复制绝对路径"]'), '服务器会话：「打开文件」（经 agent 预览服务器文件，§10.10）+「复制路径」');
    // 文件标签 + 「改动」：关闭文件标签后回到「改动」
    s.localOverride = true;
    pv.open(s, { kind: 'diff', path: CCWD + '/calc.py', change: { path: CCWD + '/calc.py', edits: [], add: 0, del: 0 } });   // 不会发请求的标签
    await raf();
    check(tabs().length === 2 && tabs()[0].classList.contains('is-chg') && tabs()[1].classList.contains('on') && fxChgBtn.el.getAttribute('aria-pressed') === 'false', '另开标签时「改动」仍在最左，按钮不再按下');
    tabs()[1].querySelector('.pv-tab-x').click();
    await raf();
    check(tabs().length === 1 && tabs()[0].classList.contains('on') && !!view(), '关掉其它标签后回到「改动」');
    // 记忆：新面板实例读同一个键 → 打开
    const p2 = new PreviewPanel({ host: document.createElement('div'), chgKey: FX_CHG_KEY });
    const remembered = p2.chgOpen;
    clearInterval(p2.timer); p2.root.remove(); p2.pill.remove();
    check(remembered === true, '重新加载后记得「改动」栏是打开的');
    // 颜色：浅色 / 深色
    const th = window.ccrTheme;
    const prevPref = th.pref();
    const colorRes = [];
    for (const t of ['light', 'dark']) {
      th.set(t, { persist: false }); await raf();
      const bg = rgbaOf(getComputedStyle(view()).backgroundColor);
      const row = rowOf('notes.txt');   // 第 1 轮：+2 −1
      const add = rgbaOf(getComputedStyle(row.querySelector('.chg-add')).color), del = rgbaOf(getComputedStyle(row.querySelector('.chg-del')).color);
      const dir = rgbaOf(getComputedStyle(rowOf('utils/strings.py').querySelector('.chg-dir')).color);
      const foot = rgbaOf(getComputedStyle(view().querySelector('.chg-foot')).color), footBg = rgbaOf(getComputedStyle(view().querySelector('.chg-foot')).backgroundColor);
      colorRes.push([t, contrast(add, bg), contrast(del, bg), contrast(dir, bg), contrast(foot, over(footBg, bg))].map((x) => (typeof x === 'number' ? x.toFixed(1) : x)));
    }
    th.set(prevPref, { persist: false }); await raf();
    check(colorRes.every((r) => r.slice(1).every((v) => Number(v) >= 4.5)) && colorRes[0][1] !== colorRes[1][1], '浅色 / 深色：+ / − / 目录 / 口径说明对比度都 ≥ 4.5（' + colorRes.map((r) => r.join(' ')).join('；') + '）');
    // 关闭面板（×）= 「改动」也关掉
    pv.closeAll();
    await raf();
    check(root.hidden && !pv.chgOpen && localStorage.getItem(FX_CHG_KEY) === '0', '面板右上角 × 关闭所有标签（含「改动」）');
    check(trackerOf(s).turns.length === 3 && s.items.filter((it) => it.kind === 'user').length === 2, 'tracker 的轮 = 用户消息（+ 开头一段）');
  } finally {
    window.fetch = origFetch;
    s.localOverride = null;
    localStorage.removeItem(FX_CHG_KEY);
  }
  check(calls.length === nCalls && vizStatCalls === nStat && fetches === 0, '改动栏没有发出任何请求（按键 / stat / fetch 都是 0）');
}

// ---------- /context 卡片（ctxcard.js） ----------
// 真机 transcript（CLI 2.1.331，fixtures/transcript_context_*.jsonl）：<local-command-stdout> 带 ANSI 颜色码与 ⛁ ⛀ ⛶ ⛝ 网格。
async function contextChecks() {
  const nCalls = calls.length;
  let full = [], classic = [];
  try {
    const load = async (name) => (await fetchText('/fixtures/' + name)).split('\n').filter(Boolean).map((l) => JSON.parse(l));
    full = await load('transcript_context_fullscreen.jsonl');
    classic = await load('transcript_context_classic.jsonl');
  } catch (e) { log(String(e.message || e), 'bad'); }
  pushTranscript(full, true);
  await raf(); flushNow(s);
  const cmds = qa('.k-cmd');
  const cards = qa('.ctx-card');
  check(cmds.length === 2 && cards.length === 2 && cmds.every((c) => c.querySelector('.cmd-line').textContent.trim() === '/context'),
    '两次 /context：每次一条命令 + 一张卡片（isMeta 的 Markdown 版不显示）');
  check(!cmds.some((c) => /\x1b|\[\d+(;\d+)*m/.test(c.textContent)), '不再露出 ANSI 颜色码（"[38;5;245m" 之类）');
  check(!qa('.k-user').length && !qa('.k-hint').length, '没有多出用户消息 / 系统行');
  const card = cards[0];
  const t = (sel) => (card.querySelector(sel) || {}).textContent || '';
  check(t('.ctx-title') === '上下文用量' && t('.ctx-model') === 'Haiku 5.5' && t('.ctx-model-id') === 'claude-haiku-5-5',
    '卡片标题：上下文用量 · Haiku 5.5 · claude-haiku-5-5');
  check(t('.ctx-total-num') === '16k / 1m tokens' && t('.ctx-total-pct') === '2%' && cards[1].querySelector('.ctx-total-num').textContent === '16.6k / 1m tokens',
    '总量：16k / 1m tokens · 2%（第二次 16.6k）');
  const rows = [...card.querySelectorAll('.ctx-cat')].map((r) => [r.querySelector('.ctx-cat-name').textContent, r.querySelector('.ctx-cat-tok').textContent, r.querySelector('.ctx-cat-pct').textContent]);
  check(JSON.stringify(rows) === JSON.stringify([['System prompt', '3.1k', '0.3%'], ['System tools', '10.1k', '1%'], ['Custom agents', '73', '<0.1%'],
    ['Memory files', '130', '<0.1%'], ['Skills', '2.6k', '0.3%'], ['Messages', '10', '<0.1%'], ['Free space', '951k', '95%'], ['Autocompact buffer', '33k', '3%']]),
  '分类列表 8 行（名称 / tokens / 百分比与 CLI 一致）');
  // 三列对齐：每列的左/右边缘一致
  const colX = (sel, side) => [...card.querySelectorAll(sel)].map((e) => Math.round(e.getBoundingClientRect()[side]));
  const same = (a) => a.every((x) => Math.abs(x - a[0]) <= 1);
  check(same(colX('.ctx-cat-name', 'left')) && same(colX('.ctx-cat-tok', 'right')) && same(colX('.ctx-cat-pct', 'right')), '分类表：名称左对齐、数字右对齐');
  const dots = [...card.querySelectorAll('.ctx-dot')].map((d) => getComputedStyle(d).backgroundColor);
  check(dots[0] === 'rgb(138, 138, 138)' && dots[2] === 'rgb(95, 135, 255)' && dots[5] === 'rgb(175, 0, 175)', '分类圆点用 CLI 的颜色（245 / 69 / 127 号色）');
  const bar = card.querySelector('.ctx-bar');
  const segs = [...bar.querySelectorAll('.ctx-seg')];
  const bw = bar.getBoundingClientRect().width;
  const segW = (i) => segs[i].getBoundingClientRect().width / bw * 100;
  check(segs.length === 7 && segs.at(-1).classList.contains('ctx-buf') && Math.abs(segW(1) - 1.01) < 0.3 && Math.abs(segW(6) - 3.3) < 0.3
    && segs[1].style.background && bar.lastElementChild === segs.at(-1),
    '用量条：6 段已用（宽度按 tokens / 窗口，System tools ≈ 1%）+ 最右自动压缩预留 3.3%（' + segW(1).toFixed(2) + '% / ' + segW(6).toFixed(2) + '%）');
  check(/2%/.test(bar.getAttribute('aria-label')) && bar.getAttribute('role') === 'img', '用量条有 aria-label（已用 2%）');
  check(t('.ctx-meta') === '自动压缩窗口1m tokens', '自动压缩窗口：1m tokens');
  const secs = [...card.querySelectorAll('.ctx-sec')];
  check(secs.length === 3 && secs.every((x) => x.tagName === 'DIV') && secs[0].textContent === 'Custom agents.claude/agents/1 agent · 73 tokens',
    '小节（Custom agents / Memory files / Skills）各一行');
  // 原文：默认收起；展开后上色、网格对齐
  const raw = card.querySelector('.ctx-raw');
  check(raw && !raw.open && /\/context all to expand/.test(raw.querySelector('summary').textContent), '原文默认收起（摘要带 "/context all to expand"）');
  raw.open = true;
  await raf();
  const pre = raw.querySelector('pre.ansi-pre');
  const glyphs = [...pre.querySelectorAll('.ansi-g')];
  const gw = glyphs.map((g) => g.getBoundingClientRect().width);
  const probe = h('span', null, '0000000000');
  pre.appendChild(probe);
  const ch = probe.getBoundingClientRect().width / 10;
  probe.remove();
  check(glyphs.length === 10 * 20 + 8 && gw.every((w) => Math.abs(w - ch) < 0.05), '原文：' + glyphs.length + ' 个方块都固定 1ch 宽（' + ch.toFixed(2) + 'px）');
  const leftOf = (needle) => {
    const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const i = n.data.indexOf(needle);
      if (i >= 0) { const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + 1); return r.getBoundingClientRect().left; }
    }
    return NaN;
  };
  // 右列（第 42 列起）与分类名（第 44 列起：图例方块 + 空格之后）；网格下方的 Messages / Free space / Autocompact 行也要对上
  const xs = ['Haiku 5.5', 'claude-haiku-5-5', '16k/1m', 'Estimated usage'].map(leftOf);
  const xm = ['System prompt', 'Skills:', 'Messages', 'Free space', 'Autocompact buffer'].map(leftOf);
  check(same(xs) && same(xm) && Math.abs(xm[0] - xs[0] - 2 * ch) < 1, '原文：右列与网格下方的分类行对齐（右列 ' + xs.map(Math.round).join('/') + '，分类 ' + xm.map(Math.round).join('/') + '）');
  const g0 = glyphs[0], g7 = glyphs[7];
  check(getComputedStyle(g0).color === 'rgb(138, 138, 138)' && getComputedStyle(g7).color === 'rgb(98, 98, 98)', '原文：方块保留 CLI 颜色');
  const boldHead = [...pre.querySelectorAll('.ansi-b')].map((e) => e.textContent);
  check(boldHead.includes('Context usage') && boldHead.includes('Auto-compact window:'), '原文：粗体标题');
  check(getComputedStyle(pre).whiteSpace === 'pre', '原文不折行（宽了横向滚动）');
  raw.open = false;
  // 经典渲染器 / 展开版：小节有分组，可展开
  pushTranscript(classic, true);
  await raf(); flushNow(s);
  const c2 = q('.ctx-card');
  const skills = c2 && [...c2.querySelectorAll('.ctx-sec')].find((x) => x.textContent.startsWith('Skills'));
  check(!!skills && skills.tagName === 'DETAILS' && !skills.open && /18 项/.test(skills.querySelector('summary').textContent), '展开版：Skills 小节可展开（18 项）');
  skills.open = true;
  await raf();
  const groups = [...skills.querySelectorAll('.ctx-sec-group')].map((x) => x.textContent);
  check(JSON.stringify(groups) === JSON.stringify(['User', 'Plugin (claude-hud)', 'Built-in']) && skills.querySelectorAll('.ctx-sec-item').length === 18, '展开版：分组 User / Plugin / Built-in，共 18 项');
  // 其它带 ANSI 的本地命令输出：上色的等宽原文；不带 ANSI 的照旧
  pushTranscript([
    { type: 'system', subtype: 'local_command', uuid: 'fx-ansi-1', content: '<command-name>/status</command-name>\n<command-args></command-args>' },
    { type: 'system', subtype: 'local_command', uuid: 'fx-ansi-2', content: '<local-command-stdout>\x1b[1mVersion:\x1b[22m 2.1.331\n\x1b[31mError\x1b[39m: none \x1b[38;5;241mdim\x1b[39m</local-command-stdout>' },
    { type: 'system', subtype: 'local_command', uuid: 'fx-ansi-3', content: '<command-name>/model</command-name>\n<command-args></command-args>' },
    { type: 'system', subtype: 'local_command', uuid: 'fx-ansi-4', content: '<local-command-stdout>Kept model as Haiku 5.5</local-command-stdout>' },
  ], false);
  await raf(); flushNow(s);
  const st = qa('.k-cmd').find((c) => c.textContent.includes('/status'));
  const stPre = st && st.querySelector('pre.ansi-pre.cmd-ansi');
  check(!!stPre && stPre.textContent === 'Version: 2.1.331\nError: none dim' && !st.querySelector('.ctx-card'), '其它带 ANSI 的命令输出：去掉颜色码的等宽原文');
  const red = stPre && [...stPre.children].find((x) => x.textContent === 'Error');
  const dim = stPre && [...stPre.children].find((x) => x.textContent === 'dim');
  check(!!red && getComputedStyle(red).color === 'rgb(205, 49, 49)' && dim.classList.contains('ansi-grey'), '红色照 ANSI 上色；灰色（CLI 的 dim）改用主题弱化色');
  const md = qa('.k-cmd').find((c) => c.textContent.includes('/model'));
  check(!!md && md.querySelector('.cmd-out-text') && !md.querySelector('pre'), '不带 ANSI 的输出照旧（/model）');
  // 浅色 / 深色：卡片文字对比度
  pushTranscript(full, true);
  await raf(); flushNow(s);
  const th = window.ccrTheme;
  const prevPref = th ? th.pref() : 'system';
  const res = [];
  for (const tname of ['light', 'dark']) {
    if (th) th.set(tname, { persist: false });
    await raf();
    const cd = q('.ctx-card');
    const bg = rgbaOf(getComputedStyle(cd).backgroundColor);
    const ratio = (sel) => contrast(rgbaOf(getComputedStyle(cd.querySelector(sel)).color), bg);
    res.push([tname, ratio('.ctx-title'), ratio('.ctx-cat-pct'), ratio('.ctx-model-id'), ratio('.ctx-sec-note'),
      getComputedStyle(document.documentElement).getPropertyValue('--ctx-free').trim(), getComputedStyle(cd.querySelector('.ctx-bar')).backgroundColor]);
  }
  if (th) th.set(prevPref, { persist: false });
  await raf();
  check(res.every((r) => r.slice(1, 5).every((v) => v >= 4.5)) && res[0][5] && res[1][5] && res[0][6] !== res[1][6],
    '浅色 / 深色：卡片文字对比度 ≥ 4.5，用量条底色随主题（' + res.map((r) => r[0] + ' ' + r.slice(1, 5).map((v) => v.toFixed(1)).join('/')).join('；') + '）');
  check(calls.length === nCalls, '/context 卡片不发任何请求、不按任何键');
  pushTranscript(transcriptEntries(), true);
  await raf(); flushNow(s);
}

// ---------- 子代理被停止（subagents.js 托盘 + render.js 的 Agent 卡 / 通知行） ----------
// 形状照 fixtures/subagent_stop/absorbed（服务器真实样本脱敏）：主会话回复中途调 TaskStop，killed 通知被 CLI 吸收为
// attachment/queued_command（没有 user 条目）；agent 只转发精简版（node_agent.note_attachment）。摘要是后端给的（Python 单测覆盖判定）。
const SS_AID = 'a8d153a165a587509', SS_TU = 'toolu_01YBHwjpKR3o53b7jLGihQZd';
const SS_NOTE = '<task-notification>\n<task-id>' + SS_AID + '</task-id>\n<tool-use-id>' + SS_TU + '</tool-use-id>\n<output-file>/tmp/claude-1000/x/tasks/' + SS_AID + '.output</output-file>\n<status>killed</status>\n<summary>Agent "搭建沙盒" was stopped by Claude</summary>\n<note>A task-notification fires each time this agent stops with no live background children of its own.</note>\n</task-notification>';
function ssEntries() {
  const t0 = Date.parse('2026-11-25T02:40:33Z');
  const ts = (sec) => new Date(t0 + sec * 1000).toISOString();
  const launched = 'Async agent launched successfully. (This tool result is internal metadata — never quote or paste any part of it, including the agentId below, into a user-facing reply.)\nagentId: ' + SS_AID + ' (internal ID - do not mention to user.)\nThe agent is working in the background. You will be notified automatically when it completes.';
  return [
    { type: 'user', uuid: 'ss-u1', timestamp: ts(0), origin: { kind: 'human' }, promptSource: 'typed', message: { role: 'user', content: '派一个执行者搭建沙盒' } },
    { type: 'assistant', uuid: 'ss-a1', timestamp: ts(27), message: { role: 'assistant', content: [{ type: 'tool_use', id: SS_TU, name: 'Agent', input: { description: '搭建沙盒', prompt: '搭建一个小沙盒并汇报。', subagent_type: 'executor', run_in_background: true } }] } },
    { type: 'user', uuid: 'ss-u2', timestamp: ts(27.3), toolUseResult: { isAsync: true, status: 'async_launched', agentId: SS_AID, description: '搭建沙盒' }, message: { role: 'user', content: [{ tool_use_id: SS_TU, type: 'tool_result', content: [{ type: 'text', text: launched }] }] } },
    { type: 'user', uuid: 'ss-u3', timestamp: ts(230), origin: { kind: 'human' }, promptSource: 'typed', message: { role: 'user', content: '中断所有subagent' } },
    { type: 'assistant', uuid: 'ss-a2', timestamp: ts(235.5), message: { role: 'assistant', content: [{ type: 'tool_use', id: 'toolu_ss_stop', name: 'TaskStop', input: { task_id: SS_AID } }] } },
    { type: 'user', uuid: 'ss-u4', timestamp: ts(235.6), message: { role: 'user', content: [{ tool_use_id: 'toolu_ss_stop', type: 'tool_result', content: '{"message":"Successfully stopped task: ' + SS_AID + ' (搭建沙盒)","task_id":"' + SS_AID + '","task_type":"local_agent"}' }] } },
    { type: 'attachment', uuid: 'ss-at1', parentUuid: 'ss-u4', timestamp: ts(235.7), isSidechain: false, attachment: { type: 'queued_command', commandMode: 'task-notification', prompt: SS_NOTE, origin: { kind: 'task-notification' } } },
    { type: 'assistant', uuid: 'ss-a3', timestamp: ts(247), message: { role: 'assistant', content: [{ type: 'text', text: '执行者已中断，现在没有子代理在运行。' }] } },
  ];
}
function ssSummary(status) {
  const t0 = Date.parse('2026-11-25T02:41:00Z') / 1000;
  const sum = { agent_id: SS_AID, tool_use_id: SS_TU, description: '搭建沙盒', agent_type: 'executor', model: 'claude-opus-5-5', background: true,
    status, raw_status: status === 'killed' ? 'killed' : null, started_at: t0, last_activity: t0 + 208, tool_uses: 1,
    last_action: 'Bash: mkdir -p sandbox && ls sandbox', entries: 4, notifications: status === 'killed' ? 1 : 0, has_transcript: true };
  if (status !== 'running') Object.assign(sum, { ended_at: t0 + 208.7, duration_ms: 208700 });
  return { type: 'subagent', agent_id: SS_AID, gen: 1, from: 4, total: 4, entries: [], reset: false, summary: sum };
}
let fxSaUI = null;
async function subStopChecks() {
  const nCalls = calls.length;
  let fetches = 0;
  const origFetch = window.fetch;
  window.fetch = (...a) => { fetches++; return origFetch(...a); };
  const prevOn = state.onSubagent;
  try {
    if (!fxSaUI) {
      fxSaUI = new SubagentUI({ api: async () => { throw new Error('回放页不应请求子代理详情'); }, toast: (t) => log('（toast）' + t) });
      const inner = document.querySelector('.composer .composer-inner');
      inner.insertBefore(fxSaUI.tray, inner.firstChild);
    }
    state.onSubagent = (ss, aid, added, reset) => { if (prevOn) prevOn(ss, aid, added, reset); fxSaUI.onUpdate(ss, aid, added, reset); };
    s.subagents.clear();
    fxSaUI.setSession(s);
    const ents = ssEntries();
    const cut = ents.findIndex((e) => e.type === 'attachment');
    pushTranscript(ents.slice(0, cut - 2), true);   // 派出子代理、用户说"中断所有subagent"
    applyEnvelope(state, env('subagent', ssSummary('running')));
    await raf(); flushNow(s);
    const tray = fxSaUI.tray;
    const card = () => qa('.k-tool').find((el) => el.querySelector('.sa-card-row'));
    check(!tray.hidden && tray.querySelectorAll('.sa-item').length === 1 && /1 个运行中/.test(tray.textContent) && /搭建沙盒/.test(tray.textContent),
      '运行中：输入框上方托盘列出 1 个子代理');
    check(!!card() && card().querySelector('.sa-card-row').classList.contains('st-running') && /子代理运行中/.test(card().textContent), 'Agent 卡：子代理运行中');
    // TaskStop：子代理文件末尾的中断标记 → 后端摘要立即变 killed（通知还没到）
    pushTranscript(ents.slice(cut - 2, cut), false);
    applyEnvelope(state, env('subagent', ssSummary('killed')));
    await raf(); flushNow(s);
    check(tray.hidden && !tray.querySelector('.sa-item'), '被停止后托盘立即移除（没有运行中的子代理 → 托盘隐藏）');
    const row = card() && card().querySelector('.sa-card-row');
    check(!!row && row.classList.contains('st-killed') && card().classList.contains('st-interrupted') && /子代理已终止/.test(card().textContent),
      'Agent 卡：子代理已终止（' + (row ? row.textContent : '无') + '）');
    check(!/运行中/.test(card().textContent), 'Agent 卡不再显示运行中 / 最近动作');
    check(!qa('.k-subnote').length, '通知到达前没有结束通知行');
    // 回复中途吸收的 attachment 通知 → 结束通知行
    const nItems = s.items.length;
    pushTranscript(ents.slice(cut), false);
    await raf(); flushNow(s);
    const notes = qa('.k-subnote');
    check(notes.length === 1 && notes[0].classList.contains('st-killed') && /子代理「搭建沙盒」已终止/.test(notes[0].textContent),
      'attachment 通知渲染成结束通知行「子代理「搭建沙盒」已终止」：' + (notes[0] ? notes[0].textContent : '无'));
    check(s.items.length === nItems + 2 && !qa('.k-user').some((el) => /task-notification/.test(el.textContent)), '通知不显示成用户消息（只多了通知行与回复）');
    check(!!notes[0] && !notes[0].querySelector('.sa-note-tog') && !!notes[0].querySelector('.st-warn'), '通知行：停止图标、没有结果可展开（被停止的通知不带 <result>）');
    // 其它 attachment：未知类型不渲染（回复中发送的用户消息 commandMode=prompt 见第 14h 节）；纯文本通知 → 系统消息
    const n2 = s.items.length;
    pushTranscript([
      { type: 'attachment', uuid: 'ss-at3', timestamp: ents[cut].timestamp, attachment: { type: 'skill_listing', content: SS_NOTE } },
    ], false);
    await raf(); flushNow(s);
    check(s.items.length === n2, '非通知、非用户消息的 attachment 不渲染');
    pushTranscript([{ type: 'attachment', uuid: 'ss-at4', timestamp: ents[cut].timestamp, attachment: { type: 'queued_command', commandMode: 'task-notification', prompt: 'Background agent "第三次数数" was stopped by the user.' } }], false);
    await raf(); flushNow(s);
    check(s.items.length === n2 + 1 && qa('.k-hint').some((el) => /系统消息：Background agent "第三次数数" was stopped by the user\./.test(el.textContent)),
      '纯文本的停止通知（Ctrl+X Ctrl+K / Ctrl+C）显示为系统消息');
    // 侧栏：主会话空闲且没有运行中的子代理 → 不再「子代理运行中」
    const subRunning = [...s.subagents.values()].filter((a) => a.summary && a.summary.status === 'running').length;   // 同 app.js runningSubagents
    const idleMeta = { state: 'idle', link: 'ready' };
    check(subRunning === 0 && sessionStatus({ meta: idleMeta, subRunning })[1] === '空闲'
      && sessionStatus({ meta: idleMeta, subRunning: 1 })[1] === '子代理运行中',
      '侧栏：停止后不再「子代理运行中」（' + sessionStatus({ meta: idleMeta, subRunning })[1] + '）');
    check(calls.length === nCalls && fetches === 0, '整个过程不发任何请求 / 按键');
  } finally {
    window.fetch = origFetch;
    state.onSubagent = prevOn;
    if (fxSaUI) fxSaUI.setSession(null);
  }
}

// ---------- 回复进行中发出的消息（CLI 2.1.330–2.1.332：被这一轮中途吸收的消息只写成 attachment / queued_command，
// commandMode=prompt，不另写 user 条目；render.js onQueuedPrompt）。条目全部是合成的。 ----------
async function queuedPromptChecks() {
  const nCalls = calls.length;
  let fetches = 0;
  const origFetch = window.fetch;
  window.fetch = (...a) => { fetches++; return origFetch(...a); };
  const th = window.ccrTheme;
  const prevPref = th ? th.pref() : 'system';
  try {
    const QCWD = '/mnt/ssd/home/demouser/qproj';
    const t0 = Date.parse('2026-11-25T08:00:00Z');
    const U = (i) => '00000000-0000-4000-9000-' + String(i).padStart(12, '0');
    const base = (i, type, extra) => ({ type, uuid: U(i), parentUuid: i > 1 ? U(i - 1) : null, isSidechain: false, cwd: QCWD, timestamp: new Date(t0 + i * 1000).toISOString(), ...extra });
    const userE = (i, text) => base(i, 'user', { message: { role: 'user', content: text }, origin: { kind: 'human' }, promptSource: 'typed' });
    const asst = (i, content, stop) => base(i, 'assistant', { message: { id: 'msg_q' + i, role: 'assistant', content, stop_reason: stop || 'tool_use' } });
    const res = (i, id, content, tur) => base(i, 'user', { message: { role: 'user', content: [{ tool_use_id: id, type: 'tool_result', content }] }, ...(tur ? { toolUseResult: tur } : {}) });
    // 与 node_agent.note_attachment 转发的精简版同形
    const att = (i, prompt, extra = {}) => base(i, 'attachment', { attachment: { type: 'queued_command', commandMode: 'prompt', prompt, origin: { kind: 'human' }, source_uuid: 'qsrc-' + i, ...extra } });
    const file = QCWD + '/notes.txt';
    const MID = '顺便把第二行也改成 world';
    const ents = [
      userE(1, '把 notes.txt 第一行改成 hello'),
      asst(2, [{ type: 'tool_use', id: 'tq1', name: 'Edit', input: { file_path: file, old_string: 'hi', new_string: 'hello' } }]),
      res(3, 'tq1', 'The file has been updated', { filePath: file, structuredPatch: [{ oldStart: 1, oldLines: 1, newStart: 1, newLines: 1, lines: ['-hi', '+hello'] }] }),
      att(4, MID),
      asst(5, [{ type: 'tool_use', id: 'tq2', name: 'Edit', input: { file_path: file, old_string: 'x', new_string: 'world' } }]),
      res(6, 'tq2', 'The file has been updated', { filePath: file, structuredPatch: [{ oldStart: 2, oldLines: 1, newStart: 2, newLines: 1, lines: ['-x', '+world'] }] }),
      asst(7, [{ type: 'text', text: '两行都改好了。' }], 'end_turn'),
      base(8, 'system', { subtype: 'turn_duration', durationMs: 5000 }),
      userE(9, '谢谢'),
      asst(10, [{ type: 'text', text: '不客气。' }], 'end_turn'),
    ];
    pushTranscript(ents.slice(0, 3), true);
    await raf(); flushNow(s);
    // 回复进行中发送：前端先记成“待出现”（与 app.js send() 相同：n = 发送前时间线上的用户消息条数）
    const pend = [];
    addPending(pend, MID, promptsFromItems(s.items).length);
    check(mergePending(promptsFromItems(s.items), pend).pending.length === 1, '回复中发送后 transcript 里还没有：待出现 1 条');
    pushTranscript(ents.slice(3), false);
    await raf(); flushNow(s);
    const users = qa('.k-user');
    const mid = users[1];
    check(users.length === 3 && !!mid && mid.classList.contains('mid-turn') && !users[0].classList.contains('mid-turn') && !users[2].classList.contains('mid-turn'),
      'attachment（commandMode=prompt）渲染成用户气泡，只有它带 mid-turn（共 ' + users.length + ' 条用户消息）');
    check(!!mid && mid.querySelector('.user-text').textContent === MID && /回复中发送/.test(mid.querySelector('.user-tag').textContent),
      '气泡文字 = 原文，顶部小标注「回复中发送」');
    const meta = mid && mid.querySelector(':scope > .msg-meta.mm-user');
    check(!!meta && !!meta.querySelector('.mm-btn') && !!meta.querySelector('.mm-time'), '悬停操作照常：时间 + 复制');
    const kinds = s.items.map((it) => it.kind).join(',');
    check(kinds === 'user,tool,user,tool,text,turnsum,user,text', '位置：第一次工具结果之后、第二次工具调用之前（' + kinds + '）');
    const ts = s.items.find((it) => it.kind === 'turnsum');
    check(!!ts && ts.tools.length === 2 && ts.tools.every((t) => t.name === 'Edit') && qa('.k-turnsum').length === 1,
      '不结束这一轮：只有一张「本轮改动」卡片，包含前后两次 Edit');
    const tr = trackerOf(s);
    tr.update(s);
    check(tr.turns.length === 3 && tr.main.length === 2 && tr.main.every((e) => e.turn === 1), '「改动」栏不另起一轮：轮 = 开头 + 2 条普通用户消息，两次编辑都在第 1 轮');
    const prompts = promptsFromItems(s.items);
    check(prompts.join('|') === ['把 notes.txt 第一行改成 hello', MID, '谢谢'].join('|'), '↑/↓ 历史包含回复中发送的消息（按时间顺序）');
    check(mergePending(prompts, pend).pending.length === 0, '待出现对账：出现了对应 attachment 就算已出现，不再单独列出');
    check(minimapEntries(s.items).length === 3 && fxMinimap.entries.length === 3, '左侧小格子：每条用户消息一格（含回复中发送的，共 ' + fxMinimap.entries.length + ' 格）');
    // 只出现一次：同一条再推一遍（uuid 相同）、或压缩后 CLI 原样再写一遍（uuid 不同、source_uuid 相同）
    const n1 = s.items.length;
    pushTranscript([ents[3], { ...ents[3], uuid: U(99) }], false);
    await raf(); flushNow(s);
    check(s.items.length === n1 && qa('.k-user').length === 3, '只出现一次：重复的 uuid / 相同 source_uuid 都不再追加');
    // 带图片（CLI：prompt = [文字块, 图片块…]，开头的 [Image #1] 占位与 onUser 一样去掉）
    const SMALL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
    pushTranscript([att(11, [{ type: 'text', text: '[Image #1] 看看这张图' }, { type: 'image', source: { type: 'base64', media_type: 'image/png', data: SMALL } }])], false);
    await raf(); flushNow(s);
    const imgU = qa('.k-user').pop();
    check(!!imgU && imgU.classList.contains('mid-turn') && imgU.querySelectorAll('.user-imgs img').length === 1 && imgU.querySelector('.user-text').textContent === '看看这张图',
      '带图片的：气泡里显示图片，文字去掉 [Image #1] 占位');
    // 来自其它会话（peer，CLI 记为 isMeta）→ 系统行，不是用户气泡，不进 ↑/↓ 历史
    const nU = qa('.k-user').length, nP = promptsFromItems(s.items).length;
    pushTranscript([att(12, '<agent-message from="helper">\n后台任务的报告第一行\n第二行\n</agent-message>', { origin: { kind: 'peer', name: 'helper' } })], false);
    await raf(); flushNow(s);
    check(qa('.k-user').length === nU && promptsFromItems(s.items).length === nP
      && qa('.k-hint').some((el) => /来自其它会话「helper」：后台任务的报告第一行/.test(el.textContent)), '来自其它会话的消息 → 系统行「来自其它会话「helper」：…」');
    // 子代理通知仍是通知行；其它 attachment 不渲染
    const n2 = s.items.length;
    pushTranscript([base(13, 'attachment', { attachment: { type: 'date', date: '2026-11-25' } })], false);
    await raf(); flushNow(s);
    check(s.items.length === n2, '其它 attachment（date 等）不渲染');
    // 历史预览（/api/history 的 entries 同形）：同样渲染
    const hs = ensureSession(createState({ actions: {} }), 'squeuedhist01', { title: '历史', state: 'exited' });
    applyHistory(hs, ents);
    const hu = hs.items.filter((it) => it.kind === 'user');
    check(hu.length === 3 && hu[1].midTurn === true && hu[1].text === MID && hs.items.filter((it) => it.kind === 'turnsum').length === 1, '历史预览：同样渲染成回复中发送的用户消息，不另起一轮');
    // 浅色 / 深色：小标注的对比度
    const res2 = [];
    if (th) {
      for (const theme of ['light', 'dark']) {
        th.set(theme, { persist: false }); await raf();
        const tag = mid.querySelector('.user-tag');
        const bubbleBg = over(rgbaOf(getComputedStyle(mid.querySelector('.bubble')).backgroundColor), rgbaOf(getComputedStyle(document.body).backgroundColor));
        res2.push(contrast(rgbaOf(getComputedStyle(tag).color), bubbleBg).toFixed(2));
      }
      th.set(prevPref, { persist: false }); await raf();
    }
    check(res2.length === 2 && res2.every((v) => Number(v) >= 4.5), '浅色 / 深色：「回复中发送」小标注对比度 ≥ 4.5（' + res2.join(' / ') + '）');
    check(calls.length === nCalls && fetches === 0, '整个过程不发任何请求 / 按键');
  } finally {
    window.fetch = origFetch;
    if (th) th.set(prevPref, { persist: false });
  }
}

// ---------- 删除的会话不复活（render.js：applyEnvelope / forgetSession / createsSession，纯状态，不挂 DOM） ----------
function removedChecks() {
  const removedCalls = [];
  const st = createState({ actions: {}, onRemoved: (sid) => removedCalls.push(sid) });
  const SA_ = 'sdeleted00001', SB_ = 'ssametitle002';
  let g = 100;
  const ev = (sid, kind, data) => ({ gseq: ++g, sid, ts: Date.now() / 1000, kind, data });
  const meta = (sid, m, extra) => ev(sid, 'app', { type: 'meta', meta: { sid, ...m }, ...(extra || {}) });
  // 两个同名（同一句首条输入）、不同 key 的会话
  ensureSession(st, SA_, { title: '这是我导师想的一个idea', state: 'exited', api_key_name: 'demouser_d' });
  ensureSession(st, SB_, { title: '这是我导师想的一个idea', state: 'idle', api_key_name: 'demouser' });
  applyEnvelope(st, ev(SB_, 'subagent', { type: 'subagent', agent_id: 'aB', gen: 1, from: 0, total: 0, entries: [], summary: { agent_id: 'aB', status: 'running' } }));
  applyEnvelope(st, ev(SA_, 'transcript', { type: 'transcript', entries: [{ type: 'user', uuid: 'uA', timestamp: '2026-11-25T02:40:00Z', message: { role: 'user', content: 'A 的消息' } }], reset: true }));
  const a = st.sessions.get(SA_), b = st.sessions.get(SB_);
  check(a !== b && a.items.length === 1 && b.items.length === 0 && b.subagents.size === 1 && a.subagents.size === 0,
    '同名会话按 sid 各自接收事件（A 的条目不进 B、B 的子代理不进 A）');
  // 本页删除 A，之后才到的事件（exited meta / 子代理 / 屏幕 / transcript / exit）都不能把它加回来
  forgetSession(st, SA_);
  for (const e of [meta(SA_, { title: '这是我导师想的一个idea', state: 'exited' }),
    ev(SA_, 'subagent', { type: 'subagent', agent_id: 'aA', gen: 1, from: 0, total: 0, entries: [], summary: { agent_id: 'aA', status: 'killed' } }),
    ev(SA_, 'screen', { type: 'screen', state: { busy: false }, text: '' }),
    ev(SA_, 'transcript', { type: 'transcript', entries: [], reset: true }),
    ev(SA_, 'app', { type: 'exit', exit_status: 0 }),
    meta(SA_, { state: 'idle' }, { added: true })]) applyEnvelope(st, e);
  check(!st.sessions.has(SA_) && st.sessions.has(SB_) && st.sessions.get(SB_).meta.api_key_name === 'demouser',
    '删除后迟到的事件（含 added）都丢弃，不复活；同名的另一个会话不受影响');
  // 未知 sid：只有新建会话的那条 meta（data.added）能创建
  applyEnvelope(st, meta('sunknown00003', { state: 'exited', title: 'x' }));
  applyEnvelope(st, ev('sunknown00003', 'subagent', { type: 'subagent', agent_id: 'q', summary: { status: 'running' } }));
  check(!st.sessions.has('sunknown00003'), '未知 sid 的普通事件不创建会话');
  applyEnvelope(st, meta('snew000000004', { state: 'starting', title: '新会话' }, { added: true }));
  check(st.sessions.has('snew000000004') && st.sessions.get('snew000000004').meta.title === '新会话' && createsSession(meta('x', {}, { added: true })) && !createsSession(meta('x', {})),
    '新建会话（data.added，例如在别的窗口新建）照常出现');
  // 别的窗口删除：后端推 app/removed → 回调界面清理并记住，之后的事件也不复活；暂存（hold）中也立即生效
  const bs = st.sessions.get(SB_);
  bs.hold = true;
  applyEnvelope(st, ev(SB_, 'app', { type: 'removed' }));
  check(!st.sessions.has(SB_) && removedCalls.join() === SB_ && st.removed.has(SB_) && bs.held.length === 0, 'app/removed：立即移除（暂存中的会话也一样）并通知界面');
  applyEnvelope(st, meta(SB_, { state: 'idle' }));
  applyEnvelope(st, ev(SB_, 'subagent', { type: 'subagent', agent_id: 'aB', summary: { status: 'running' } }));
  check(!st.sessions.has(SB_), 'removed 之后再到的事件同样丢弃');
  applyEnvelope(st, ev('sother0000005', 'app', { type: 'removed' }));
  check(removedCalls.length === 1 && st.removed.has('sother0000005'), '未知 sid 的 removed：只记住，不回调');
  // 重连回放（同一批 gseq 再来一遍，含更早的 meta）：已删除的仍不复活；lastGseq 照常前进
  const before = st.lastGseq;
  applyEnvelope(st, { ...meta(SA_, { state: 'exited' }), gseq: 5 });
  applyEnvelope(st, meta(SA_, { state: 'exited' }));
  check(!st.sessions.has(SA_) && st.lastGseq > before && st.lastGseq === g, '重连回放不复活已删除的会话（lastGseq 照常前进）');
}

// ---------- dmg 版自动更新（update.js） ----------
async function updateChecks() {
  const base = { enabled: true, current: '2.12', auto_check: true, checking: false, last_check: null, available: null,
    phase: 'idle', progress: null, error: null, busy: [], manual: false, page: 'https://github.com/o/r/releases/latest', just_updated: null };
  const av = { version: '2.13', notes: '新增：自动更新\n修复：若干问题', page: 'https://github.com/o/r/releases/tag/v2.13', size: 40e6, snoozed: false };
  const st = (o) => ({ ...base, ...o });
  // 纯函数
  check(bannerModel(null) === null && bannerModel({ enabled: false }) === null && bannerModel(st({})) === null, '没开启 / 没有新版本：不显示');
  const mAv = bannerModel(st({ available: av }));
  check(mAv && mAv.text === '新版本 2.13 可用' && mAv.actions.map((a) => a.id).join() === 'install,later' && mAv.notes.includes('自动更新'), '新版本可用：立即更新 / 稍后 / 更新说明');
  check(bannerModel(st({ available: { ...av, snoozed: true } })) === null, '「稍后」之后 24 小时内不再提示同一版本');
  const mW = bannerModel(st({ available: av, phase: 'waiting', busy: ['会话 A', '会话 B'] }));
  check(mW && mW.text.includes('等所有会话空闲后自动开始更新到 2.13') && mW.text.includes('会话 A、会话 B') && mW.actions.map((a) => a.id).join() === 'cancel', '有会话在忙：等空闲（可取消）');
  const mD = bannerModel(st({ available: av, phase: 'downloading', progress: { done: 15e6, total: 40e6 } }));
  check(mD && mD.pct === 37 && mD.text.includes('37%') && pctOf({ done: 1, total: 0 }) === null, '下载进度 %');
  check(bannerModel(st({ available: av, phase: 'verifying' })).text.includes('校验') && bannerModel(st({ available: av, phase: 'installing' })).text.includes('安装')
    && bannerModel(st({ available: av, phase: 'relaunching' })).text.includes('即将重新打开窗口'), '校验 → 安装 → 即将重新打开窗口');
  const mM = bannerModel(st({ available: av, phase: 'manual', manual: true, error: '无法自动替换（…）' }));
  check(mM && mM.kind === 'warn' && mM.actions.map((a) => a.id).join() === 'open_dmg,retry,page,dismiss', '无法自动替换：打开已下载的安装包 / 重试 / 打开下载页 / 关闭');
  const mF = bannerModel(st({ available: av, phase: 'failed', error: '签名校验失败' }));
  check(mF && mF.kind === 'error' && mF.text === '更新失败：签名校验失败' && mF.actions.map((a) => a.id).join() === 'retry,page,dismiss', '失败：原因 + 重试 + 打开下载页');
  const mU = bannerModel(st({ just_updated: { version: '2.13', from: '2.12' } }));
  check(mU && mU.kind === 'ok' && mU.text === '已更新到 2.13' && mU.actions[0].id === 'ack', '更新完成后提示一次「已更新到 X」');
  const now = Date.now() / 1000;
  check(checkText(st({ checking: true })) === '正在检查…' && checkText(st({})) === '还没有检查过'
    && checkText(st({ last_check: { at: now, result: 'uptodate' } })).startsWith('已是最新版本')
    && checkText(st({ last_check: { at: now, result: 'none' } })).startsWith('还没有发布任何版本')
    && checkText(st({ last_check: { at: now, result: 'error', error: '网络错误：x' } })).startsWith('检查失败：网络错误'), '设置里的检查结果文字');
  // DOM：挂在会话头上方（与 app.js 相同位置）
  const store = new UpdateStore();
  const acts = [];
  const fakeApi = { post: async (path, body) => { record('update ' + path, body); acts.push(path); return null; } };
  const hd = $('fx-header');
  const banner = new UpdateBanner(store, { mount: (el) => hd.parentNode.insertBefore(el, hd), act: (id) => updateAction(fakeApi, store, id) });
  const nCalls = calls.length;
  store.set(st({}));
  check(!banner.el || banner.el.hidden, '没有新版本：不挂 / 隐藏');
  store.set(st({ available: av }));
  await raf();
  const el = banner.el;
  check(!!el && !el.hidden && el.nextElementSibling === hd && el.textContent.includes('新版本 2.13 可用'), '提示条在会话头上方');
  check(calls.length === nCalls, '显示提示条不发任何请求');
  const notesBtn = el.querySelector('.upd-link');
  await click(notesBtn, '更新说明');
  const pre = el.querySelector('.upd-notes pre');
  const ghLink = el.querySelector('.upd-notes a');
  check(!!pre && pre.textContent.includes('修复：若干问题') && ghLink && ghLink.target === '_blank' && ghLink.href === av.page && calls.length === nCalls, '更新说明：展开纯文本 + 「在 GitHub 上查看」新窗口链接，不发请求');
  check(el.querySelector('[data-act="later"]')?.textContent === '24 小时后再提醒', '「稍后」按钮文案：24 小时后再提醒');
  await click(el.querySelector('[data-act="later"]'), '24 小时后再提醒');
  check(calls.at(-1)?.op === 'update /api/update/snooze' && calls.at(-1).body.version === '2.13', '「稍后」→ POST /api/update/snooze {version}');
  await click(el.querySelector('[data-act="install"]'), '立即更新');
  check(calls.at(-1)?.op === 'update /api/update/install', '「立即更新」→ POST /api/update/install');
  store.set(st({ available: av, phase: 'downloading', progress: { done: 20e6, total: 40e6 } }));
  await raf();
  const bar = el.querySelector('.upd-bar > span');
  check(el.classList.contains('is-progress') && !!el.querySelector('.spin') && bar && bar.style.width === '50%', '下载中：spinner + 进度条 50%');
  await click(el.querySelector('[data-act="cancel"]'), '取消');
  check(calls.at(-1)?.op === 'update /api/update/cancel', '「取消」→ POST /api/update/cancel');
  store.set(st({ available: av, phase: 'manual', manual: true, error: '无法自动替换' }));
  await raf();
  const page = el.querySelector('a[data-act="page"]');
  check(!!page && page.target === '_blank' && page.href === base.page && page.rel.includes('noopener'), '「打开下载页」是新窗口链接（只在用户点击时打开）');
  await click(el.querySelector('[data-act="open_dmg"]'), '打开已下载的安装包');
  check(calls.at(-1)?.op === 'update /api/update/open_dmg', '「打开已下载的安装包」→ POST /api/update/open_dmg');
  // 颜色：各状态在浅色 / 深色下文字对比度
  const th = window.ccrTheme;
  const prevPref = th.pref();
  const res = [];
  const bodyBg = () => rgbaOf(getComputedStyle(document.querySelector('.fx-main .main')).backgroundColor);
  for (const t of ['light', 'dark']) {
    th.set(t, { persist: false });
    for (const [kind, s2] of [['info', st({ available: av })], ['progress', st({ available: av, phase: 'verifying' })],
      ['warn', st({ available: av, phase: 'manual', error: 'x' })], ['error', st({ available: av, phase: 'failed', error: 'x' })],
      ['ok', st({ just_updated: { version: '2.13', from: '2.12' } })]]) {
      store.set(s2); await raf();
      const cs = getComputedStyle(el.querySelector('.upd-text'));
      res.push([t, kind, contrast(rgbaOf(cs.color), over(rgbaOf(getComputedStyle(el).backgroundColor), bodyBg()))]);
    }
  }
  th.set(prevPref, { persist: false }); await raf();
  check(res.every((r) => r[2] >= 4.5), '浅色 / 深色：各状态提示文字对比度 ≥ 4.5（最低 ' + Math.min(...res.map((r) => r[2])).toFixed(1) + '）');
  await click(el.querySelector('[data-act="ack"]'), '知道了');
  check(calls.at(-1)?.op === 'update /api/update/ack', '「知道了」→ POST /api/update/ack');
  // 设置项
  store.set(st({ last_check: { at: now, result: 'uptodate' } }));
  const fld = updateSettingsField(fakeApi, store, {});
  document.body.appendChild(fld.el);
  check(fld.el.querySelector('input[name="update_auto_check"]').checked && fld.el.textContent.includes('当前版本 2.12') && fld.el.textContent.includes('已是最新版本'), '设置：自动检查开关（默认开）+ 当前版本 + 上次检查结果');
  const box = fld.el.querySelector('input[name="update_auto_check"]');
  gesture++; box.checked = false; box.dispatchEvent(new Event('change')); await sleep(50); gesture--;
  check(calls.at(-1)?.op === 'update /api/update/settings' && calls.at(-1).body.auto_check === false, '关掉开关 → POST /api/update/settings {auto_check:false}');
  await click(fld.el.querySelector('[data-act="check"]'), '立即检查');
  check(calls.at(-1)?.op === 'update /api/update/check', '「立即检查」→ POST /api/update/check');
  // 设置：有新版本时始终可以装（提示条点过「24 小时后再提醒」也一样）
  const snAv = { ...av, snoozed: true, snoozed_until: now + 3600 };
  const sm = settingsModel(st({ available: snAv }));
  check(bannerModel(st({ available: snAv })) === null && sm && sm.text === '发现新版本 2.13' && sm.actions.map((a) => a.id).join() === 'install,page' && sm.notes.includes('自动更新'),
    '设置：提示条被暂时隐藏时仍显示「发现新版本」+ 立即更新 + 打开下载页 + 更新说明');
  check(snoozeText(st({ available: snAv })).startsWith('已暂时隐藏窗口上方的提醒') && snoozeText(st({ available: av })) === '', '设置：「已暂时隐藏提醒」只在点过之后显示');
  check(netText(st({ network: { mode: 'proxy', proxy: '127.0.0.1:7890' } })).includes('经系统代理 127.0.0.1:7890')
    && netText(st({ network: { mode: 'direct' } })).includes('未设置系统代理')
    && netText(st({ network: { mode: 'unsupported', why: 'PAC 自动代理' } })).includes('PAC 自动代理，暂不支持，未检查更新')
    && hostsText(st({})).includes('github.com') && hostsText(st({})).includes('release-assets.githubusercontent.com'), '设置：经哪个系统代理 / 未设置 / 不支持；会访问的域名');
  for (const ph of ['downloading', 'failed', 'manual', 'waiting', 'verifying']) {
    const a = bannerModel(st({ available: av, phase: ph, error: 'x', progress: { done: 1, total: 2 } }));
    const b = settingsModel(st({ available: av, phase: ph, error: 'x', progress: { done: 1, total: 2 } }));
    if (!(a && b && a.text === b.text && a.actions.map((x) => x.id).join() === b.actions.map((x) => x.id).join())) { check(false, '设置与提示条状态一致：' + ph); }
  }
  check(true, '设置与提示条在下载 / 失败 / 手动 / 等待 / 校验时状态一致');
  store.set(st({ available: snAv, network: { mode: 'proxy', proxy: '127.0.0.1:7890' }, last_check: { at: now, result: 'available' } }));
  await raf();
  const sbox = fld.el.querySelector('.upd-set-box');
  check(el.hidden && !!sbox && !sbox.hidden && sbox.textContent.includes('发现新版本 2.13') && !!sbox.querySelector('[data-act="install"]')
    && sbox.querySelector('a[data-act="page"]')?.target === '_blank', '设置 DOM：提示条隐藏时，设置里仍有「立即更新」与「打开下载页」');
  check(!fld.el.querySelector('.upd-snooze-line').hidden && fld.el.querySelector('.upd-net-line').textContent.includes('经系统代理 127.0.0.1:7890')
    && fld.el.querySelector('.upd-hosts-line').textContent.includes('release-assets.githubusercontent.com'), '设置 DOM：已暂时隐藏提醒 / 网络 / 域名三行');
  await click(sbox.querySelector('.upd-link'), '设置里的更新说明');
  check(!!sbox.querySelector('.upd-notes pre') && sbox.querySelector('.upd-notes pre').textContent.includes('修复'), '设置里「更新说明」可展开');
  await click(sbox.querySelector('[data-act="install"]'), '设置里的立即更新');
  check(calls.at(-1)?.op === 'update /api/update/install', '设置里「立即更新」→ POST /api/update/install');
  store.set(st({ available: av, phase: 'downloading', progress: { done: 30e6, total: 40e6 } }));
  await raf();
  check(sbox.classList.contains('is-progress') && sbox.querySelector('.upd-bar > span')?.style.width === '75%' && !el.hidden && el.querySelector('.upd-bar > span')?.style.width === '75%',
    '下载中：设置与提示条同时显示 75%');
  await click(sbox.querySelector('[data-act="cancel"]'), '设置里的取消');
  check(calls.at(-1)?.op === 'update /api/update/cancel', '设置里「取消」→ POST /api/update/cancel');
  store.set(st({ available: av, phase: 'failed', error: '无法连接 github.com：超时（经系统代理 127.0.0.1:7890）' }));
  await raf();
  check(sbox.classList.contains('is-error') && sbox.textContent.includes('超时（经系统代理 127.0.0.1:7890）') && !!sbox.querySelector('[data-act="retry"]'), '失败：设置里显示原因 + 重试');
  await click(sbox.querySelector('[data-act="retry"]'), '设置里的重试');
  check(calls.at(-1)?.op === 'update /api/update/install', '设置里「重试」→ POST /api/update/install');
  store.set(st({ network: { mode: 'unsupported', why: 'PAC 自动代理' }, last_check: { at: now, result: 'error', error: '系统代理是 PAC 自动代理，暂不支持，未连接 GitHub' } }));
  await raf();
  check(sbox.hidden && fld.el.querySelector('.upd-check-line').textContent.includes('暂不支持') && fld.el.querySelector('.upd-net-line').textContent.includes('不会绕过代理直连'), 'PAC：设置里写明未检查、不直连');
  // 设置状态框对比度（浅色 / 深色）
  const th2 = window.ccrTheme, prev2 = th2.pref(), res2 = [];
  const bodyBg2 = () => rgbaOf(getComputedStyle(document.body).backgroundColor);
  for (const t of ['light', 'dark']) {
    th2.set(t, { persist: false });
    for (const s2 of [st({ available: av }), st({ available: av, phase: 'downloading', progress: { done: 1, total: 2 } }), st({ available: av, phase: 'failed', error: 'x' }), st({ available: av, phase: 'manual', error: 'x' })]) {
      store.set(s2); await raf();
      const bg = over(rgbaOf(getComputedStyle(sbox).backgroundColor), bodyBg2().a ? bodyBg2() : { r: 255, g: 255, b: 255, a: 1 });
      res2.push(contrast(rgbaOf(getComputedStyle(sbox.querySelector('.upd-text')).color), bg));
    }
  }
  th2.set(prev2, { persist: false }); await raf();
  check(res2.every((c) => c >= 4.5), '设置状态框：浅色 / 深色文字对比度 ≥ 4.5（最低 ' + Math.min(...res2).toFixed(1) + '）');
  fld.dispose(); fld.el.remove();
  store.set(st({}));
  check(el.hidden, '状态回到无新版本：提示条隐藏');
  el.remove();
  check(acts.length === calls.length - nCalls, '更新相关请求都来自点击（共 ' + acts.length + ' 个）');
}

// ---------- 重启 / 续接进度（opprogress.js） ----------
// 后端 meta.op 的样例（与 ccr/opprogress.py 的结构一致）；横幅挂在输入框上方（与 app.js 相同位置）。
let fxOpNow = 0, fxOpId = 0;
function sampleOp(over = {}) {
  const now = fxOpNow;
  const mk = (id, label, state, t0, t1, slow = 30, hint = '比平时慢，仍在进行') => ({ id, label, slow_after: slow, slow_hint: hint, state, t0, t1 });
  return {
    id: ++fxOpId, kind: 'resume_new_job', state: 'running', phase: 'connect', jobid: '421865', local: false, retries: 0,
    started_at: now - 12, phase_started_at: now - 4, finished_at: null, detail: null, error: null, error_code: null,
    phases: [mk('stop', '退出旧的 claude（/exit）', 'done', now - 12, now - 10), mk('probe', 'ssh gpu-cluster：查询作业 421865（squeue）', 'done', now - 10, now - 4, 15),
      mk('connect', 'srun --jobid=421865 --overlap：在作业上启动 agent', 'cur', now - 4, null, 30, '比平时慢，仍在进行（gpu-cluster 的 Slurm 偶尔不稳）'),
      mk('keeper', '确认 tmux 常驻进程（keeper）', 'todo', null, null), mk('start', '在 tmux 里启动 claude --resume 59dd443a', 'todo', null, null),
      mk('ready', '等待 claude 就绪（出现输入框）', 'todo', null, null)],
    ...over,
  };
}

async function opChecks() {
  const now = Date.now() / 1000;
  fxOpNow = now;
  // 纯函数
  check(opModel(null) === null && fmtSecs(5) === '5 秒' && fmtSecs(65) === '1 分 05 秒' && busyLabel('resume_new_job') === '正在续接…' && busyLabel('restart') === '正在重启…', '没有进度不显示；秒数格式；按钮文字');
  const pm = opModel(null, { now, pending: { kind: 'restart', started_at: now - 1 } });
  check(pm && pm.state === 'pending' && pm.title === '正在重启…' && pm.text === '正在提交请求…', '点下去、请求还没回来：本地"正在重启… 正在提交请求…"');
  const rm = opModel(sampleOp(), { now });
  check(rm.state === 'running' && rm.title === '正在续接…' && rm.text.startsWith('srun --jobid=421865') && rm.stepNo === '第 3/6 步' && Math.round(rm.elapsed) === 12 && !rm.slow, '进行中：当前阶段 + 第几步 + 已用秒数');
  check(rm.steps.filter((x) => x.status === 'done').length === 2 && Math.round(rm.steps[1].took) === 6, '已完成阶段打勾并显示用时');
  const sm = opModel(sampleOp({ phase_started_at: now - 31 }), { now });
  check(sm.slow && sm.slowText === '比平时慢，仍在进行（gpu-cluster 的 Slurm 偶尔不稳）', 'srun 超过 30 秒：比平时慢，仍在进行（Slurm 偶尔不稳）');
  check(opModel(sampleOp({ phase: 'probe', phase_started_at: now - 10 }), { now }).slow === false, 'ssh 查询 10 秒：未超过 15 秒，不提示慢');
  const fm = opModel(sampleOp({ state: 'failed', error: '连接作业 421865 超时（已等 90 秒）：ssh 连接 gpu-cluster 超时', finished_at: now }), { now });
  check(fm.state === 'failed' && fm.title === '续接失败' && fm.error.includes('ssh 连接 gpu-cluster 超时') && fm.actions.join() === 'retry,dismiss', '失败：原因 + 重试 / 关闭');
  const jm = opModel(sampleOp({ state: 'failed', error: 'srun 无法连接作业 421865：作业已结束（请在新作业上续接）', error_code: 'job_ended', finished_at: now }), { now });
  check(jm.actions.join() === 'newjob,dismiss', '作业已结束：按钮变成「在新作业上续接」（重试同一个作业没有意义）');
  const dm = opModel(sampleOp({ state: 'done', phase: 'ready', finished_at: now }), { now });
  check(dm.state === 'done' && dm.title === 'claude 已就绪' && dm.text === '用时 12 秒', '成功：短暂显示「已就绪」');
  const tm = opModel(sampleOp({ state: 'trust', phase: 'ready', detail: '等待你在上方确认信任目录', finished_at: now }), { now });
  check(tm.state === 'trust' && tm.text === '等待你在上方确认信任目录', '信任框：提示用户确认（不自动回答）并结束转圈');
  check(opModel(sampleOp({ state: 'stalled', phase: 'ready', detail: 'claude 已启动，但 90 秒后仍未出现输入框', finished_at: now }), { now }).actions.join() === 'term,dismiss', '长时间未就绪：建议打开终端面板');
  // 作业已结束时的 jobid 输入框（根因修复：以前预填已结束的旧作业，直接点下一步被静默忽略）
  const jp = jobPromptModel({ jobid: '123456' }, '421865', { ended: true, dead: true });
  check(jp.value === '421865' && jp.okText === '续接' && !jp.confirm && jp.title === '在新作业上续接', '作业已结束：预填当前作业 421865（与从历史续接一致），一步续接');
  check(jp.validate('123456').includes('作业 123456 已结束') && jp.validate('421865') === '' && jp.validate('abc') === 'jobid 格式不正确', '填回已结束的作业：框里说明原因（不再静默无反应）');
  check(jobPromptModel({ jobid: '123456' }, '123456', { ended: true }).value === '', '设置里的作业也是已结束的那个：不预填，让用户填新作业');
  const lp = jobPromptModel({ jobid: '421865' }, '421865', { ended: false, dead: false });
  check(lp.value === '421865' && lp.confirm && lp.validate('421865').includes('已经在作业 421865 上'), '运行中改作业：预填当前作业，填同一个时说明原因，仍需确认');

  // DOM：横幅挂在输入框上方
  const fxb = $('fx-banner');
  const banner = new OpBanner({ onAction: (id) => onAct(id) });
  fxb.parentNode.insertBefore(banner.el, fxb);
  let meta = { sid: SID, jobid: '123456', state: 'exited', op: null };
  let pend = null;   // 当前"后端请求"（测试里手动结束）
  const sent = [];
  const guard = new OpGuard(() => render());
  // 与 app.js 相同的规则：进行中时重启按钮禁用、转圈并写明在做什么
  const btn = h('button', { class: 'btn ghost small restart-btn', type: 'button', id: 'fx-op-restart' });
  fxb.parentNode.insertBefore(btn, banner.el);   // 回放页的会话头会随屏幕重建，按钮放在横幅旁边
  function render() {
    const kind = guard.busyKind(SID, meta);
    btn.disabled = !!kind;
    btn.replaceChildren(kind ? h('span', { class: 'spin' }) : icon('refresh'), h('span', null, kind ? busyLabel(kind) : '重连'));
    banner.update(guard.view(SID, meta));
  }
  async function restart(body) {
    const out = await guard.run(SID, body.jobid ? 'resume_new_job' : 'reconnect', meta, () => {
      record('restart', body); sent.push(body);
      return new Promise((res, rej) => { pend = { res, rej }; });
    }, body);
    if (out.skipped) log('（toast）正在进行中，进度见输入框上方');
    return out;
  }
  function onAct(id) {
    if (id === 'dismiss') guard.dismiss(SID, meta);
    if (id === 'retry') { const q = guard.last.get(SID) || {}; guard.dismiss(SID, meta); restart(q).catch((e) => guard.fail(SID, 'resume_new_job', e.message)); }
    if (id === 'newjob') record('open_job_prompt');
  }
  btn.addEventListener('click', () => { restart({ jobid: '421865' }).catch((e) => guard.fail(SID, 'resume_new_job', e.message, e.code)); });
  render();
  check(banner.el.hidden && !btn.disabled, '空闲：横幅隐藏、按钮可用');
  const n0 = calls.length;
  // 连点 5 次：只发一次请求，按钮立即进入"进行中"
  gesture++;
  for (let i = 0; i < 5; i++) btn.click();
  gesture--;
  await raf();
  check(sent.length === 1 && calls.length === n0 + 1, '连点 5 次只发 1 次请求');
  check(btn.disabled && btn.textContent === '正在续接…' && !!btn.querySelector('.spin'), '点下去立刻：按钮禁用 + 转圈 +「正在续接…」');
  check(!banner.el.hidden && banner.el.dataset.state === 'pending' && banner.el.nextElementSibling === fxb && !!banner.el.querySelector('.spin'), '横幅立刻出现（请求还没回来）：转圈 + 正在提交请求…');
  // 后端进度经 SSE 到达
  meta = { ...meta, state: 'starting', op: sampleOp() };
  render();
  const el = banner.el;
  check(el.dataset.state === 'running' && el.querySelector('.op-title').textContent === '正在续接…' && el.querySelector('.op-text').textContent.startsWith('srun --jobid=421865'), '后端进度：标题 + 当前阶段');
  check(el.querySelectorAll('.op-steps li').length === 6 && el.querySelectorAll('.op-steps li.op-s-done .op-s-ic').length === 2 && !!el.querySelector('.op-steps li.op-s-cur .spin'), '阶段列表：2 个打勾、当前阶段转圈、其余待办');
  check(/已用 12 秒/.test(el.querySelector('.op-time').textContent), '已用秒数');
  const head0 = el.querySelector('.op-head');
  meta = { ...meta, op: { ...meta.op, started_at: meta.op.started_at - 1 } };
  render();
  check(el.querySelector('.op-head') === head0 && /已用 13 秒/.test(el.querySelector('.op-time').textContent), '每秒计时原地更新（不重建、不闪）');
  meta = { ...meta, op: { ...meta.op, phase_started_at: now - 40, detail: '第 1 次连接失败（ssh 连接 gpu-cluster 超时），稍后自动重试', retries: 1 } };
  render();
  check(el.querySelector('.op-slow')?.textContent.includes('Slurm 偶尔不稳') && el.querySelector('.op-detail')?.textContent.includes('第 1 次连接失败'), '比平时慢 + 第几次失败、原因');
  // 请求返回（HTTP 在停止旧 claude 之后返回）：仍按后端进度显示，按钮仍禁用
  pend.res({ sid: SID }); await sleep(20); render();
  check(btn.disabled && el.dataset.state === 'running', '请求返回后：后端还在进行 → 按钮仍禁用');
  gesture++; btn.click(); gesture--; await raf();
  check(sent.length === 1, '后端进行中再点：不发请求');
  // 失败：按钮恢复，可重试
  meta = { ...meta, state: 'error', op: { ...meta.op, state: 'failed', error: '连接作业 421865 超时（已等 90 秒）：ssh 连接 gpu-cluster 超时', finished_at: now } };
  render();
  check(!btn.disabled && btn.textContent === '重连' && el.dataset.state === 'failed' && el.querySelector('.op-error').textContent.includes('ssh 连接 gpu-cluster 超时')
    && el.querySelector('.op-steps li.op-s-fail') && !el.querySelector('.spin'), '失败：写清原因、失败阶段打叉、不再转圈、按钮恢复');
  await click(el.querySelector('[data-act="retry"]'), '重试');
  check(sent.length === 2 && sent[1].jobid === '421865' && el.dataset.state === 'pending', '重试：同样的参数再发一次，横幅回到进行中');
  pend.rej(Object.assign(new Error('该会话正在执行其它操作（例如发送消息、切换模型），请稍候再试'), { code: 'op_in_progress' })); await sleep(20);
  meta = { ...meta, op: null };
  render();
  check(el.dataset.state === 'failed' && el.textContent.includes('该会话正在执行其它操作') && !btn.disabled, '请求本身失败：横幅显示原因（不弹错误框），按钮恢复');
  await click(el.querySelector('[data-act="dismiss"]'), '关闭');
  check(el.hidden, '关闭失败提示');
  // 成功
  meta = { ...meta, state: 'idle', op: sampleOp({ state: 'done', phase: 'ready', finished_at: now }) };
  render();
  check(!el.hidden && el.dataset.state === 'done' && el.textContent.includes('claude 已就绪') && !el.querySelector('.op-steps'), '成功：短暂显示「claude 已就绪」');
  meta = { ...meta, op: null }; render();
  check(el.hidden && !btn.disabled, '进度收起');
  // 时间线尾部的「正在启动 claude…」：有阶段进度时不重复显示
  pushMeta({ ...s.meta, state: 'starting', op: sampleOp() }); await raf(); refreshTailFx();
  const tailHidden = s.view.starting.hidden;
  pushMeta({ ...s.meta, state: 'starting', op: null }); await raf(); refreshTailFx();
  check(tailHidden && !s.view.starting.hidden, '时间线尾部「正在启动 claude…」：有进度横幅时隐藏，没有时照常显示');
  pushMeta({ ...s.meta, state: 'idle', op: null }); await raf();

  // 对比度（浅色 / 深色）：各状态的文字对横幅底色
  const th = window.ccrTheme, prev = th.pref(), res = [];
  const bodyBg = () => rgbaOf(getComputedStyle(document.querySelector('.fx-main .main')).backgroundColor);
  const states = [['running', sampleOp({ phase_started_at: now - 40, detail: '第 1 次连接失败（ssh 连接 gpu-cluster 超时），稍后自动重试' })],
    ['failed', sampleOp({ state: 'failed', error: 'srun 无法连接作业 421865：作业已结束', finished_at: now })],
    ['done', sampleOp({ state: 'done', phase: 'ready', finished_at: now })],
    ['trust', sampleOp({ state: 'trust', phase: 'ready', detail: '等待你在上方确认信任目录', finished_at: now })],
    ['stalled', sampleOp({ state: 'stalled', phase: 'ready', detail: 'claude 已启动，但 90 秒后仍未出现输入框', finished_at: now })]];
  for (const t of ['light', 'dark']) {
    th.set(t, { persist: false });
    for (const [k, op] of states) {
      meta = { ...meta, op }; render(); await raf();
      const bg = over(rgbaOf(getComputedStyle(el).backgroundColor), bodyBg());
      for (const sel of ['.op-title', '.op-text', '.op-error', '.op-detail', '.op-slow', '.op-time', '.op-step', '.op-steps li.op-s-todo', '.op-steps li.op-s-done', '.op-s-took']) {
        const n = el.querySelector(sel);
        if (n) res.push([t, k, sel, contrast(rgbaOf(getComputedStyle(n).color), bg)]);
      }
    }
  }
  th.set(prev, { persist: false }); await raf();
  const worst = res.reduce((a, b) => (b[3] < a[3] ? b : a), res[0]);
  check(res.length > 40 && res.every((r) => r[3] >= 4.5), '浅色 / 深色：各状态横幅文字对比度 ≥ 4.5（' + res.length + ' 项，最低 ' + worst[3].toFixed(1) + ' ' + worst.slice(0, 3).join(' ') + '）');
  meta = { ...meta, op: null }; render();
  el.remove(); btn.remove();
  check(calls.slice(n0).every((c) => c.gesture) && calls.length - n0 === 2, '进度相关请求都来自点击（共 ' + (calls.length - n0) + ' 个：续接 + 重试，没有其它）');
}
function refreshTailFx() { refreshTail(s); }

// ---------- 第 14s 节：系统通知与防休眠（sysnotify.js / keepawake.js） ----------
// 系统通知只在原生窗口里有 handler：这里 stub 掉 window.webkit.messageHandlers.ccrNotify（记录消息、按场景回复），
// 浏览器里没有 handler 时设置里显示为不可用。防休眠的开关是 config 的 keep_awake，状态是 GET /api/keepawake。
// 模块按需动态导入（不改本文件开头的 import 列表）。不发真的通知、不弹系统授权框。
async function sysChecks() {
  const SN = await import('/static/sysnotify.js');
  const KA = await import('/static/keepawake.js');
  const th = window.ccrTheme;
  const n0 = calls.length;
  let cfg = { keep_awake: true };
  let ka = { enabled: true, active: true, working: 2, release_in: null, dry_run: false, available: true };
  const gets = [];
  const fakeApi = {
    get: async (path) => {
      gets.push(path);
      if (path === '/api/notify') return { sounds: [], available: false };
      if (path === '/api/keepawake') return { ...ka, enabled: cfg.keep_awake !== false, active: cfg.keep_awake !== false && ka.active };
      return {};
    },
    post: async (path, body) => { record('sys ' + path, body); if (path === '/api/config') { cfg = { ...cfg, ...body }; return { ...cfg }; } return {}; },
  };
  const openSettings = async () => { const p = settingsDialog(fakeApi, { config: { ...cfg } }); await raf(); await sleep(40); return { p, sd: topDialog() }; };
  const closeSettings = async (sd, p) => { await click([...sd.querySelectorAll('.modal-actions button')].find((b) => b.textContent === '完成'), '设置 · 完成'); await p; };
  const bgOf = (el) => { let e = el; while (e && rgbaOf(getComputedStyle(e).backgroundColor).a === 0) e = e.parentElement; return rgbaOf(e ? getComputedStyle(e).backgroundColor : 'rgb(255,255,255)'); };
  const ratioOf = (el) => contrast(over(rgbaOf(getComputedStyle(el).color), bgOf(el)), bgOf(el));

  // 1) 浏览器（没有 handler）
  check(!window.webkit && !SN.sysNotify.available, '浏览器里没有原生 handler（window.webkit.messageHandlers.ccrNotify）：系统通知不可用');
  let { p, sd } = await openSettings();
  const sysBox = sd.querySelector('input[name=sys_notify]');
  check(sysBox && sysBox.disabled && sd.querySelector('.sysn-hint').textContent.includes('原生窗口'), '设置：系统通知在浏览器里显示为不可用（说明只在 Mac 的原生窗口里可用）');
  const kaBox = sd.querySelector('input[name=keep_awake]');
  const kaSt = sd.querySelector('.ka-status');
  check(kaBox && kaBox.checked && !kaBox.disabled, '设置：防休眠开关默认打开');
  const okDot = rgbaOf(getComputedStyle(kaSt, '::before').backgroundColor);
  const okVar = rgbaOf(getComputedStyle(kaSt).getPropertyValue('--ok').trim().replace(/^#(..)(..)(..)$/, (_, r, g, b) => `rgb(${parseInt(r, 16)},${parseInt(g, 16)},${parseInt(b, 16)})`));
  check(kaSt && kaSt.textContent === '正在阻止空闲睡眠：2 个会话在干活。' && kaSt.classList.contains('ok') && okDot.g === okVar.g && okDot.r === okVar.r,
    '防休眠状态行：正在阻止（2 个会话在干活，前面是绿点）');
  const kaText = sd.querySelector('.ka-field').textContent;
  check(kaText.includes('合上盖子') && kaText.includes('等你审批') && kaText.includes('caffeinate') && kaText.includes('60 秒'),
    '防休眠说明：合上盖子照样会睡、等审批不算在干活、全部空闲 60 秒后恢复、由 caffeinate 实现');
  const order = [...sd.querySelectorAll('.form-grid > .field > .field-label')].map((e) => e.textContent);
  check(order.indexOf('系统通知') > order.indexOf('声音') && order.indexOf('防休眠') > order.indexOf('系统通知') && order.indexOf('默认 base_url（API key）') > order.indexOf('防休眠'),
    '设置弹窗顺序：提示音 → 声音 → 系统通知 → 防休眠 → 默认 base_url');
  check(calls.length === n0, '打开设置不发写请求');
  // 浅色 / 深色对比度（状态行、说明）
  const prevTheme = th ? th.pref() : null;
  const rs = [];
  for (const t of ['light', 'dark']) {
    if (th) { th.set(t, { persist: false }); await raf(); }
    rs.push([t, 'ka-status', ratioOf(kaSt)], [t, 'sysn-hint', ratioOf(sd.querySelector('.sysn-hint'))], [t, 'ka-desc', ratioOf(sd.querySelector('.ka-field .muted.small-text'))]);
  }
  if (th && prevTheme) { th.set(prevTheme, { persist: false }); await raf(); }
  const worst = rs.reduce((a, b) => (b[2] < a[2] ? b : a), rs[0]);
  check(rs.every((r) => r[2] >= 4.5), '浅色 / 深色：防休眠状态行与两节说明文字对比度 ≥ 4.5（最低 ' + worst[2].toFixed(1) + ' ' + worst[0] + ' ' + worst[1] + '）');
  // 关掉防休眠：POST /api/config（点击触发），状态行随之变化
  ka = { ...ka, active: true };
  await click(kaBox, '防休眠 · 关');
  await sleep(60);
  const kc = calls.slice(n0).find((c) => c.op === 'sys /api/config');
  check(kc && kc.gesture && kc.body.keep_awake === false && Object.keys(kc.body).length === 1 && !kaBox.checked && kaSt.textContent.startsWith('已关闭'),
    '关掉防休眠：POST /api/config {keep_awake:false}（点击触发），状态行「已关闭」');
  await click(kaBox, '防休眠 · 开');
  await sleep(60);
  check(cfg.keep_awake === true && kaBox.checked && kaSt.textContent.startsWith('正在阻止'), '再打开：保存 keep_awake:true，状态行恢复');
  ka = { ...ka, working: 0, release_in: 37.2 };
  check(KA.keepAwakeText(ka, true) === '会话都空闲了，约 38 秒后恢复正常睡眠。' && KA.keepAwakeText({ ...ka, dry_run: true, working: 1, release_in: null }, true).includes('演示模式'),
    '状态文字：空闲倒计时 / 演示模式（fake 后端不真的运行 caffeinate）');
  await closeSettings(sd, p);
  const g1 = gets.filter((x) => x === '/api/keepawake').length;
  await sleep(KA.POLL_MS + 300);
  check(gets.filter((x) => x === '/api/keepawake').length === g1, '关掉设置弹窗后不再轮询防休眠状态');

  // 2) 原生窗口（stub 掉 handler）：打开设置只查状态；点开关才 op=enable（原生壳这时才请求授权）
  const msgs = [];
  let reply = { available: true, enabled: false, auth: 'notDetermined' };
  window.webkit = { messageHandlers: { ccrNotify: { postMessage: (m) => {
    msgs.push({ m: JSON.parse(JSON.stringify(m)), gesture: gesture > 0 });
    if (m.op === 'enable') reply = m.on ? { available: true, enabled: true, auth: 'authorized' } : { ...reply, enabled: false };
    return Promise.resolve(m.op === 'post' || m.op === 'clear' ? { ok: true } : { ...reply });
  } } } };
  try {
    ({ p, sd } = await openSettings());
    let box = sd.querySelector('input[name=sys_notify]');
    let hint = sd.querySelector('.sysn-hint');
    check(SN.sysNotify.available && box && !box.disabled && !box.checked && hint.textContent.includes('授权框'), '原生窗口：开关可用、默认关闭；说明第一次打开时 macOS 会弹授权框');
    check(msgs.length > 0 && msgs.every((x) => x.m.op === 'status'), '打开设置只查询状态（op=status），不请求授权');
    await click(box, '系统通知 · 开');
    await sleep(40);
    const en = msgs.find((x) => x.m.op === 'enable');
    check(en && en.gesture && en.m.on === true && box.checked && hint.textContent.includes('已授权'), '打开开关：op=enable（点击触发）→ 授权后开关保持打开，说明「已授权」');
    await click(box, '系统通知 · 关');
    await sleep(40);
    check(msgs.filter((x) => x.m.op === 'enable').map((x) => x.m.on).join() === 'true,false' && !box.checked, '关闭开关：op=enable on=false');
    await closeSettings(sd, p);
    reply = { available: true, enabled: false, auth: 'denied' };
    ({ p, sd } = await openSettings());
    box = sd.querySelector('input[name=sys_notify]');
    hint = sd.querySelector('.sysn-hint');
    check(!box.checked && !box.disabled && hint.textContent.includes('系统设置') && hint.classList.contains('warn'), '系统设置里拒绝过通知：开关关闭，提示去「系统设置 → 通知」打开');
    const wr = [];
    for (const t of ['light', 'dark']) { if (th) { th.set(t, { persist: false }); await raf(); } wr.push(ratioOf(hint)); }
    if (th && prevTheme) { th.set(prevTheme, { persist: false }); await raf(); }
    const warnDot = getComputedStyle(hint, '::before').backgroundColor;
    check(wr.every((r) => r >= 4.5) && rgbaOf(warnDot).a > 0, '浅色 / 深色：拒绝时的提示文字对比度 ≥ 4.5、前面是琥珀色圆点（' + wr.map((r) => r.toFixed(1)).join(' / ') + '）');
    await closeSettings(sd, p);

    // 3) 调用时机与内容：与 app.js 相同的 deps（前台 = 页面可见且有焦点）
    reply = { available: true, enabled: true, auth: 'authorized' };
    const ctl = new SN.SysNotify(window);
    const opened = [];
    let act = 'fxa';
    const fresh = (sid, m, x) => ({ sid, meta: { sid, ...m }, ...(x || {}) });
    const sess = new Map([['fxa', fresh('fxa', { display_title: '会话 A', state: 'idle', done_at: null })],
      ['fxb', fresh('fxb', { display_title: '会话 B', state: 'idle', done_at: 1.7e9, unread: true })]]);
    const fg = () => document.visibilityState === 'visible' && document.hasFocus();
    ctl.init({ activeSid: () => act, hasSession: (sid) => sess.has(sid), foreground: fg, dialog: (x) => x.dialog || null, onOpen: (sid) => opened.push(sid) });
    await sleep(20);
    ctl.seed(sess.values());
    msgs.length = 0;
    const posts = () => msgs.filter((x) => x.m.op === 'post').map((x) => x.m);
    const now = Date.now() / 1000;
    ctl.onMeta(fresh('fxb', { display_title: '会话 B', state: 'idle', done_at: 1.7e9, unread: true }));    // 基线里的旧完成
    check(posts().length === 0, '基线（bootstrap 时已有的新回复）不通知');
    ctl.onMeta(fresh('fxb', { display_title: '会话 B', state: 'idle', done_at: now, unread: true }));
    const p1 = posts()[0];
    check(posts().length === 1 && p1.sid === 'fxb' && p1.kind === 'done' && p1.title === '会话 B' && p1.body === '完成' && p1.current === false,
      '别的会话完成一轮 → {op:post, kind:done, title:会话名, body:「完成」, current:false}');
    ctl.onMeta(fresh('fxb', { display_title: '会话 B', state: 'idle', done_at: now + 0.5, unread: true }));
    check(posts().length === 1, '同一会话 20 秒内的同类通知合并');
    ctl.onMeta(fresh('fxb', { display_title: '会话 B', state: 'waiting', done_at: now + 0.5, attn_at: now + 1, unread: true }, { dialog: { kind: 'permission', tool: 'Bash command' } }));
    const p2 = posts()[1];
    check(p2 && p2.kind === 'attention' && p2.body === '需要审批：Bash', '需要审批（meta.attn_at 变大且仍在等）→ 正文「需要审批：Bash」');
    ctl.onMeta(fresh('fxa', { display_title: '会话 A', state: 'idle', done_at: now + 2, unread: true }));
    const isFg = fg();
    check(isFg ? posts().length === 2 : (posts().length === 3 && posts()[2].current === true),
      '当前会话完成：窗口在前台不弹，不在前台才弹（current:true，原生壳再核对一次）—— 本页前台 = ' + isFg);
    ctl.onMeta(fresh('fxb', { display_title: '会话 B', state: 'idle', done_at: now, unread: true }));
    check(posts().length === (isFg ? 2 : 3), 'SSE 回放的旧 meta 不重复通知');
    // 点通知 → 原生壳派发 ccr-native-open-session：页面接手（preventDefault）并打开会话
    const handled = !window.dispatchEvent(new CustomEvent(SN.OPEN_EVENT, { detail: { sid: 'fxb' }, cancelable: true }));
    check(handled && opened.join() === 'fxb', '点通知：页面收到 ccr-native-open-session，接手（preventDefault）并打开对应会话');
    window.dispatchEvent(new CustomEvent(SN.OPEN_EVENT, { detail: { sid: 'fxc' }, cancelable: true }));
    check(opened.length === 1, '会话还没加载出来：先记下');
    sess.set('fxc', fresh('fxc', { state: 'idle' }));
    ctl.seed(sess.values());
    check(opened.join() === 'fxb,fxc', '会话加载后（seed）再打开');
    window.dispatchEvent(new CustomEvent(SN.OPEN_EVENT, { detail: { sid: '../x"' }, cancelable: true }));
    check(opened.length === 2, '非法会话 id 忽略');
    msgs.length = 0;
    ctl.clear('fxb');
    check(msgs.length === 1 && msgs[0].m.op === 'clear' && msgs[0].m.sid === 'fxb', '打开会话时清掉通知中心里它那条（op=clear）');
    window.removeEventListener(SN.OPEN_EVENT, ctl._onOpenEvent);
  } finally {
    delete window.webkit;
  }
  check(!SN.sysNotify.available, '拿掉 stub 后恢复为不可用');
  check(calls.slice(n0).every((c) => c.gesture), '本节的 HTTP 写请求都发生在点击中（共 ' + (calls.length - n0) + ' 个）');
}

// ---------- 14k. 输入框：/ 命令与 @ 文件补全（complete.js / slashcmds.js）、置顶任务清单（todopin.js）、草稿（drafts.js） ----------
// 与 app.js 同一套模块：补全挂在本页输入框上（真实 textarea + 合成按键），列表数据由桩代替后端（只记录、不发请求、不按任何键）；
// 任务清单回放真机 CLI 2.1.332 的 transcript（fixtures/transcript_tasks_2_1_332.jsonl：TaskCreate ×3 + TaskUpdate）；
// 草稿只用本页专用的 sid 写 localStorage，结束时删掉。
async function composerChecks() {
  const nCalls = calls.length;
  const ta = $('fx-input');
  const inner = ta.closest('.composer-inner');
  const pop = h('div', { id: 'fx-slash-pop', class: 'slash-pop', hidden: true });
  inner.insertBefore(pop, inner.firstChild);
  const fetched = [];
  let failNext = '';
  const custom = { commands: [
    { name: 'deploy', src: 'project', kind: 'command', desc: '部署到测试环境' },
    { name: 'frontend:component', src: 'user', kind: 'command', desc: '生成一个前端组件', hint: '<名字>' },
    { name: 'demo-skill', src: 'user', kind: 'skill', desc: '演示 skill' },
    { name: 'linked', src: 'user', kind: 'skill', desc: '', link: true },
    { name: 'hud:setup', src: 'plugin', kind: 'command', desc: '配置插件', plugin: 'hud' },
  ] };
  const files = { root: '/tmp/proj', entries: ['README.md', 'src/', 'docs/', 'package.json', 'src/app.py', 'src/lib/', 'docs/my notes.md', 'src/lib/util.py'] };
  const cache = new CompleteCache(async (sess, kind) => {
    fetched.push(kind + ':' + sess.sid + ':' + sess.cwd);
    if (failNext === kind) { failNext = ''; throw new Error('ssh 连接失败（模拟）'); }
    return kind === 'commands' ? custom : files;
  }, { onChange: () => cmp.refresh() });
  let fxComposing = false;
  const sess = { sid: SID, cwd: '/tmp/proj', version: '2.1.332', local: true };
  const cmp = new Completer({ ta, pop, cache, session: () => sess, composing: () => fxComposing, onApply: () => fxSizer.update() });
  const onKey = (e) => { if (cmp.onKey(e)) e.stopImmediatePropagation(); };   // 与 app.js 一样：列表打开时按键先归它（不翻输入历史）
  const onInput = () => cmp.update();
  ta.addEventListener('keydown', onKey, true);
  ta.addEventListener('input', onInput);
  const wasDisabled = ta.disabled;
  ta.disabled = false;
  ta.focus();
  const settle = async () => { for (let i = 0; i < 5; i++) await sleep(0); await raf(); };
  const type = async (v) => { ta.value = ''; ta.setSelectionRange(0, 0); document.execCommand('insertText', false, v); await settle(); };
  const key = (k, o = {}) => !ta.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...o }));
  const rows = () => [...pop.querySelectorAll('.cmp-row')];
  const names = () => rows().map((r) => (r.querySelector('.cmp-name') || r.querySelector('.cmp-path')).textContent);
  const active = () => (pop.querySelector('.cmp-row.active .cmp-name') || pop.querySelector('.cmp-row.active .cmp-path') || {}).textContent;

  // ---- / 命令
  await type('/');
  check(!pop.hidden && rows().length === 85 && names()[0] === '/deploy' && names()[5] === '/add-dir',
    '/：自定义 5 个在前 + 内置 80 个（真机 2.1.332 的列表），共 ' + rows().length + ' 项');
  check(fetched.length === 1 && fetched[0] === 'commands:' + SID + ':/tmp/proj' && calls.length === nCalls, '打开 / 列表只读一次自定义命令（GET complete/commands），不发任何按键');
  const srcs = rows().slice(0, 5).map((r) => r.querySelector('.cmp-src').textContent);
  check(JSON.stringify(srcs) === JSON.stringify(['项目', '用户', '用户 skill', '用户 skill', '插件 hud']) && rows()[5].querySelector('.cmp-src').textContent === '内置',
    '来源标签：项目 / 用户 / 用户 skill / 插件 hud / 内置');
  check(rows()[3].textContent.includes('软链接') && rows()[1].querySelector('.cmp-hint').textContent === '<名字>' && /CLI 2\.1\.332 · 自定义 5 个/.test(pop.querySelector('.cmp-head').textContent),
    '软链接只列名字、参数提示、标题行（内置按 CLI 版本 · 自定义数量）');
  await type('/comp');
  const nm = names();
  check(nm[0] === '/compact' && nm.includes('/autocompact') && nm.includes('/frontend:component') && active() === '/compact', '/comp：前缀匹配排最前（再是某一段的前缀、包含），第一项高亮');
  check(key('ArrowDown') && active() === nm[1] && key('ArrowUp') && active() === '/compact' && key('ArrowUp') && active() === nm.at(-1) && key('ArrowDown'), '↑/↓ 在列表里移动（首尾循环），不翻输入历史');
  check(key('Tab') && ta.value === '/compact ' && ta.selectionStart === 9 && pop.hidden, 'Tab 插入 `/compact `（光标在后，列表关闭）');
  await type('/cost');
  check(names()[0] === '/usage' && rows()[0].querySelector('.cmp-alias').textContent === '别名 cost', '别名：/cost → /usage');
  check(key('Enter') && ta.value === '/usage ', 'Enter 插入正式名字');
  await type('/model');
  check(!key('Enter') && ta.value === '/model', '已经打完整个名字：Enter 不拦（照常发送）');
  await type('/compa 已有参数');
  ta.setSelectionRange(6, 6); cmp.update(); await settle();
  check(!pop.hidden && names()[0] === '/compact' && key('Tab') && ta.value === '/compact 已有参数', '光标回到命令上：替换命令、保留后面的参数');
  await type('/zzqqxx');
  check(!pop.hidden && rows().length === 0 && pop.querySelector('.cmp-empty').textContent.includes('由 claude 自己处理') && !key('ArrowDown'), '没有匹配：提示仍可直接发送，↑/↓ 不拦');
  sess.version = '2.1.400';
  await type('/');
  check(pop.querySelector('.cmp-meta').textContent.includes('内置按 CLI 2.1.332（本会话 2.1.400）'), '会话的 CLI 版本比表新：用最接近的表并注明');
  sess.version = '2.1.332';
  // 输入法组字中：不处理按键、不改列表
  check(!key('ArrowDown', { isComposing: true }) && !key('Enter', { keyCode: 229 }), '输入法组字中的 ↑/↓/回车交给输入法');
  fxComposing = true;
  const before = names().length;
  ta.value = '/c'; cmp.update();
  check(names().length === before, '组字中输入框变化：列表保持原样，组字结束再更新');
  fxComposing = false;
  // Esc：本次关闭、同一个词里继续打字也不再弹出；新的词重新弹出
  await type('/re');
  check(key('Escape') && pop.hidden, 'Esc 关闭列表');
  document.execCommand('insertText', false, 's'); await settle();
  check(pop.hidden, 'Esc 后同一个词继续打字不再弹出');
  await type('/');
  check(!pop.hidden, '删改了前面的字（这里整个换成 /）：重新弹出');
  // 刷新按钮 / 读取失败
  await type('/');
  failNext = 'commands';
  pop.querySelector('.cmp-refresh').click();
  await settle(); await sleep(10); await settle();
  check(fetched.length === 2 && /读取自定义命令失败：ssh 连接失败（模拟）/.test(pop.querySelector('.cmp-status.err')?.textContent || '') && rows().length === 85,
    '刷新 = 重新读取；失败时提示原因，仍显示内置命令（和上次读到的自定义命令）');
  pop.querySelector('.cmp-refresh').click();
  await settle(); await sleep(10); await settle();
  check(!pop.querySelector('.cmp-status.err') && fetched.length === 3, '再次刷新成功：错误提示消失');
  // 鼠标
  await type('/dep');
  rows()[0].dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  check(ta.value === '/deploy ' && pop.hidden && document.activeElement === ta, '鼠标点选：插入并保持输入框焦点');

  // ---- @ 文件
  await type('看看 @');
  await settle(); await sleep(10); await settle();
  check(!pop.hidden && pop.dataset.kind === 'at' && names()[0] === 'README.md' && fetched.at(-1) === 'files:' + SID + ':/tmp/proj',
    '@：列出 cwd 下的文件（GET complete/files，浅的在前）');
  check(pop.querySelector('.cmp-head').textContent.includes('proj · 8 项') && rows()[1].classList.contains('is-dir'), '标题行：目录名 · 项数；文件夹有标记');
  document.execCommand('insertText', false, 'rdme'); await settle();
  check(names()[0] === 'README.md' && key('Enter') && ta.value === '看看 @README.md ' && pop.hidden, '模糊匹配 rdme → README.md；Enter 插入 `@README.md `（补一个空格）');
  document.execCommand('insertText', false, '和 @sr'); await settle();
  check(names()[0] === 'src/' && key('Tab') && ta.value.endsWith('@src/') && !pop.hidden && names()[0] === 'src/app.py', '选文件夹：插入 `@src/` 并接着列它里面的内容');
  check(key('Tab') && ta.value === '看看 @README.md 和 @src/app.py ', '再选文件：路径完整');
  document.execCommand('insertText', false, '@note'); await settle();
  check(key('Enter') && ta.value.endsWith('@"docs/my notes.md" '), '带空格的路径插入 `@"…"`');
  const fetchedAt = fetched.length;
  document.execCommand('insertText', false, '@'); await settle();
  check(fetched.length === fetchedAt, '文件列表按会话缓存（2 分钟内不重复读取）');
  check(key('Escape') && pop.hidden, '@ 列表 Esc 关闭');
  await type('mail a@b.com');
  check(pop.hidden, '邮箱里的 @ 不触发');
  check(atSafeTail('看 @README.md') === '看 @README.md ' && atSafeTail('看 @README.md 好') === '看 @README.md 好',
    '发送前：以 @引用 结尾补一个空格（真机：终端里 claude 的 @ 补全会停在这个词上，回车可能被它吃掉）');
  // 列表打开时不翻历史；关闭后 ↑ 照常翻历史
  await type('/');
  check(key('ArrowUp') && ta.value === '/', '列表打开时 ↑ 不调出历史');
  key('Escape');
  ta.value = ''; ta.setSelectionRange(0, 0); cmp.update(); fxNav = new PromptNav();
  check(key('ArrowUp') && ta.value.length > 1 && pop.hidden, '列表关闭后 ↑ 照常调出上一条消息');

  // ---- 浅色 / 深色：弹层文字对比度
  await type('/');
  const th = window.ccrTheme, prevTheme = th.pref(), res = [];
  for (const t of ['light', 'dark']) {
    th.set(t, { persist: false }); await raf();
    for (const sel of ['.cmp-title', '.cmp-meta', '.cmp-foot', '.cmp-row.active .cmp-name', '.cmp-row:not(.active) .cmp-name', '.cmp-row:not(.active) .cmp-desc',
      '.src-project .cmp-src', '.src-user .cmp-src', '.src-plugin .cmp-src', '.src-builtin .cmp-src']) {
      const n = pop.querySelector(sel);
      if (n) res.push([t, sel, contrast(rgbaOf(getComputedStyle(n).color), effBg(n))]);
    }
  }
  const worstP = res.reduce((a, b) => (b[2] < a[2] ? b : a), res[0]);
  if (!res.every((r) => r[2] >= 4.5)) log(JSON.stringify(res.filter((r) => r[2] < 4.5).map((r) => [r[0], r[1], +r[2].toFixed(2)])), 'bad');
  check(res.length === 20 && res.every((r) => r[2] >= 4.5), '浅色 / 深色：补全弹层文字对比度 ≥ 4.5（' + res.length + ' 项，最低 ' + worstP[2].toFixed(1) + ' ' + worstP.slice(0, 2).join(' ') + '）');
  th.set(prevTheme, { persist: false });
  key('Escape');

  // ---- 置顶任务清单：真机 CLI 2.1.332 transcript 逐条回放
  let lines = [];
  try { lines = (await fetchText('/fixtures/transcript_tasks_2_1_332.jsonl')).split('\n').filter(Boolean).map((l) => JSON.parse(l)); } catch (e) { log(String(e.message || e), 'bad'); }
  const mem = new Map();
  const pinStore = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
  const pin = new TodoPin({ storage: pinStore });
  inner.insertBefore(pin.el, ta.closest('.composer-box'));
  pushTranscript([], true);
  pin.update(s);
  check(pin.el.hidden, '没有任务清单：不显示');
  const seen = [];
  for (const e of lines) {
    pushTranscript([e], false);
    pin.update(s);
    const t = pin.el.hidden ? '-' : pin.el.querySelector('.todo-pin-count').textContent;
    if (seen.at(-1) !== t) seen.push(t);
    if (t === '1/3' && !pin.__mid) pin.__mid = [...pin.el.querySelectorAll('.todo-pin-item')].map((x) => x.className.replace('todo-pin-item ', ''));
  }
  check(JSON.stringify(seen) === JSON.stringify(['-', '0/1', '0/2', '0/3', '1/3', '2/3', '3/3']), '真机 TaskCreate ×3 → TaskUpdate：0/1 → … → 3/3（参数错误被拒的那次 TaskUpdate 不算）：' + seen.join(' '));
  check(JSON.stringify(pin.__mid) === JSON.stringify(['st-completed', 'st-pending', 'st-pending']), '完成一项后：第一项打勾划线');
  check(pin.el.classList.contains('all-done') && pin.el.classList.contains('collapsed') && pin.list.hidden && pin.el.querySelector('.todo-pin-title').textContent.includes('全部完成'),
    '全部完成：显示「全部完成」并默认收起');
  pin.head.click();
  check(!pin.list.hidden && pin.el.querySelectorAll('.todo-pin-item.st-completed').length === 3 && pin.el.querySelectorAll('.todo-pin-item .todo-pin-ic').length === 3, '点标题展开：3 项都打勾');
  await raf(); flushNow(s);
  const qaTools = qa('.k-tool').map((x) => x.textContent);
  check(qaTools.some((x) => x.includes('新建任务') && x.includes('心算 1+1')) && qaTools.some((x) => x.includes('更新任务') && x.includes('#1 → 已完成')),
    '时间线工具卡：TaskCreate「新建任务 心算 1+1」、TaskUpdate「更新任务 #1 → 已完成」（tools.js 认得 Task*）');
  // TodoWrite（进行中的高亮、activeForm）、收起时标题行显示当前项、记住收起状态
  const tw = (id, todos) => ({ type: 'assistant', uuid: 'fx-tw-' + id, timestamp: '2026-11-28T01:20:0' + id + '.000Z', message: { id: 'm-tw-' + id, role: 'assistant', content: [{ type: 'tool_use', id: 'toolu_tw' + id, name: 'TodoWrite', input: { todos } }] } });
  pushTranscript([tw(1, [{ content: '读代码', status: 'completed', activeForm: '正在读代码' }, { content: '改 bug', status: 'in_progress', activeForm: '正在改 bug' }, { content: '跑测试', status: 'pending', activeForm: '正在跑测试' }])], false);
  pin.update(s);
  const cur = pin.el.querySelector('.todo-pin-item.st-in_progress');
  check(!pin.el.hidden && pin.el.dataset.source === 'todo' && pin.el.querySelector('.todo-pin-count').textContent === '1/3' && cur && cur.textContent === '正在改 bug' && !pin.list.hidden,
    'TodoWrite 更新后以它为准：1/3，进行中的一项高亮并显示「正在…」');
  pin.head.click();
  check(pin.list.hidden && pin.el.querySelector('.todo-pin-cur').textContent === '正在改 bug' && mem.get('ccr.todoPin.collapsed') === '1', '收起：只剩标题行（带当前进行中的项），收起状态记住');
  const pin2 = new TodoPin({ storage: pinStore });
  pin2.update(s);
  check(pin2.list.hidden, '新建（例如重新载入）时沿用收起状态');
  pin.head.click();
  // 对比度
  const resT = [];
  for (const t of ['light', 'dark']) {
    th.set(t, { persist: false }); await raf();
    for (const sel of ['.todo-pin-title', '.todo-pin-count', '.st-completed .todo-pin-text', '.st-in_progress .todo-pin-text', '.st-pending .todo-pin-text']) {
      const n = pin.el.querySelector(sel);
      if (n) resT.push([t, sel, contrast(rgbaOf(getComputedStyle(n).color), effBg(n))]);
    }
  }
  th.set(prevTheme, { persist: false }); await raf();
  const worstT = resT.reduce((a, b) => (b[2] < a[2] ? b : a), resT[0]);
  check(resT.length === 10 && resT.every((r) => r[2] >= 4.5), '浅色 / 深色：任务清单文字对比度 ≥ 4.5（' + resT.length + ' 项，最低 ' + worstT[2].toFixed(1) + ' ' + worstT.slice(0, 2).join(' ') + '）');
  pushTranscript([], true);
  pin.update(s);
  check(pin.el.hidden, 'transcript reset（/clear 等）：清单消失');

  // ---- 草稿：localStorage（本页专用 sid，结束时删掉）
  const ls = window.localStorage;
  const A = new DraftStore({ storage: ls, windowId: 'fxA' });
  const B = new DraftStore({ storage: ls, windowId: 'fxB' });
  A.queue('sfxdraft0001', '写到一半的草稿', 1000);
  const notYet = ls.getItem('ccr.draft.sfxdraft0001') === null;
  A.flush();
  const back = new DraftStore({ storage: ls }).load('sfxdraft0001');
  check(notYet && back && back.t === '写到一半的草稿', '草稿 400ms 内合并写出；页面隐藏 / 关闭时立即写出；重新载入后读回');
  B.queue('sfxdraft0001', '旧窗口很早以前的输入', 500); B.flush();
  check(new DraftStore({ storage: ls }).load('sfxdraft0001').t === '写到一半的草稿', '多窗口：旧窗口写出更早的输入不会盖掉别的窗口后打的字');
  A.remove('sfxdraft0001');
  check(ls.getItem('ccr.draft.sfxdraft0001') === null, '会话删除：草稿一起删');

  // 清理
  ta.removeEventListener('keydown', onKey, true);
  ta.removeEventListener('input', onInput);
  pop.remove(); pin.el.remove();
  ta.value = ''; ta.disabled = wasDisabled; fxSizer.update();
  check(calls.length === nCalls, '本节没有发出任何请求 / 按键（列表数据只读，来自桩）');
}
function effBg(el) {   // 元素实际的底色：从根到它逐层叠加
  const chain = [];
  for (let n = el; n; n = n.parentElement) chain.push(rgbaOf(getComputedStyle(n).backgroundColor));
  let bg = { r: 255, g: 255, b: 255, a: 1 };
  for (const c of chain.reverse()) if (c.a > 0) bg = over(c, bg);
  return bg;
}

// ---------- 14l. 时间线工具 ----------
// 查找：全部条目（含「显示更早」之前未渲染的）、忽略大小写、中英文、折叠的工具卡 / 思考 / 详情、被档位隐藏的条目、只画视口附近、
// <mark> 退路、流式新条目、⌘F / ⌘G / Esc；档位：四档逐条核对 + 阅读位置 + 贴底 + 菜单；引用：消息 / 文件 / diff 选区、引用条、
// 发送拼接、命令不带、气泡里的引用卡片；表格复制（桩）；聊天宽度。全程不发请求、不碰真实剪贴板。
function tlFindEntries(nTurns = 170) {
  const ts = new Date().toISOString();
  const out = [];
  const U = (k) => 'tl-' + k;
  for (let k = 0; k < nTurns; k++) {
    const extra = k === 2 ? '，请记住 宝藏ALPHA 这个词' : '';
    out.push({ type: 'user', uuid: U('u' + k), timestamp: ts, message: { role: 'user', content: `第 ${k} 轮提问：讲讲话题 ${k}${extra}` } });
    if (k === 120) {
      out.push({ type: 'assistant', uuid: U('th'), timestamp: ts, message: { id: 'tl-mth', role: 'assistant', content: [{ type: 'thinking', thinking: '先想一想：needle-think 在思考里' }] } });
      out.push({ type: 'assistant', uuid: U('tu'), timestamp: ts, message: { id: 'tl-mtu', role: 'assistant', content: [{ type: 'tool_use', id: 'tl-bash', name: 'Bash', input: { command: 'grep -rn needle-xyz src', description: '搜索' } }] } });
      out.push({ type: 'user', uuid: U('tr'), timestamp: ts, message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tl-bash', content: 'src/a.py:3: needle-xyz = 1\nsrc/b.py:9: needle-out 只在输出里' }] } });
      out.push({ type: 'system', uuid: U('sy'), timestamp: ts, level: 'warning', content: 'Hook 警告第一行\n详情第二行 detail-needle' });
    }
    out.push({ type: 'assistant', uuid: U('a' + k), timestamp: ts, message: { id: 'tl-m' + k, role: 'assistant', stop_reason: 'end_turn',
      content: [{ type: 'text', text: `第 ${k} 轮回答：关于**话题** ${k} 的说明。` }] } });
  }
  return out;
}

function tlTierEntries(rep = 1) {
  const ts = new Date().toISOString();
  const out = [];
  for (let r = 0; r < rep; r++) {
    const U = (k) => 'tv-' + r + '-' + k;
    const A = (k, content, stop) => ({ type: 'assistant', uuid: U(k), timestamp: ts, message: { id: 'tvm-' + r + '-' + k, role: 'assistant', content, stop_reason: stop || 'tool_use' } });
    const R = (k, id, content, extra) => ({ type: 'user', uuid: U(k), timestamp: ts, message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: id + r, content, ...(extra || {}) }] } });
    out.push({ type: 'user', uuid: U('ua'), timestamp: ts, message: { role: 'user', content: '层级测试 A' + r } });
    out.push(A('th', [{ type: 'thinking', thinking: '想一想' }]));
    out.push(A('t1', [{ type: 'text', text: '先看看文件（中间说明）' }]));
    out.push(A('r1', [{ type: 'tool_use', id: 'tvread' + r, name: 'Read', input: { file_path: '/tmp/a.py' } }]));
    out.push(R('r1r', 'tvread', 'print(1)'));
    out.push(A('b1', [{ type: 'tool_use', id: 'tvbad' + r, name: 'Bash', input: { command: 'false' } }]));
    out.push(R('b1r', 'tvbad', 'exit 1', { is_error: true }));
    out.push(A('w1', [{ type: 'tool_use', id: 'tvdeny' + r, name: 'Write', input: { file_path: '/tmp/b.py', content: 'x' } }]));
    out.push({ ...R('w1r', 'tvdeny', 'The user doesn\'t want to proceed', { is_error: true }), toolUseResult: 'User rejected tool use' });
    out.push(A('g1', [{ type: 'tool_use', id: 'tvrun' + r, name: 'Grep', input: { pattern: 'todo' } }]));
    out.push(A('t2', [{ type: 'text', text: 'A' + r + ' 轮的最终回复' }], 'end_turn'));
    out.push({ type: 'system', uuid: U('td'), timestamp: ts, subtype: 'turn_duration', durationMs: 1000, content: '' });
    out.push({ type: 'user', uuid: U('ub'), timestamp: ts, message: { role: 'user', content: '层级测试 B' + r } });
    out.push(A('t3', [{ type: 'text', text: 'B' + r + ' 回复前半段' }]));
    out.push({ type: 'attachment', uuid: U('q'), timestamp: ts, attachment: { type: 'queued_command', commandMode: 'prompt', prompt: '中途追加的消息 ' + r, source_uuid: U('qs') } });
    out.push(A('t4', [{ type: 'text', text: 'B' + r + ' 回复读到追加之后' }]));
    out.push(A('b2', [{ type: 'tool_use', id: 'tvok' + r, name: 'Bash', input: { command: 'echo ok' } }]));
    out.push(R('b2r', 'tvok', 'ok'));
    out.push(A('t5', [{ type: 'text', text: 'B' + r + ' 最终回复' }], 'end_turn'));
    out.push({ type: 'system', uuid: U('w'), timestamp: ts, level: 'warning', content: '警告：示例 ' + r });
    out.push({ type: 'system', uuid: U('i'), timestamp: ts, content: '普通系统信息 ' + r });
  }
  return out;
}

async function timelineChecks() {
  const n0 = calls.length;
  const root = s.view.root;
  const host = $('fx-host');
  const F = fxFind;
  const hl = (name) => { const x = CSS.highlights && CSS.highlights.get(name); return x ? x.size : 0; };
  const activeRange = () => { const x = CSS.highlights && CSS.highlights.get(FD.FIND_HL_ACTIVE); if (!x) return null; for (const r of x) return r; return null; };
  const activeText = () => { const r = activeRange(); return r ? r.toString() : ''; };
  const inView = (r) => { if (!r) return false; const a = r.getBoundingClientRect(), b = root.getBoundingClientRect(); return a.height > 0 && a.top >= b.top && a.bottom <= b.bottom; };
  const type = async (q, f = F) => { f.input.value = q; f.input.dispatchEvent(new Event('input', { bubbles: true })); await sleep(140); await raf(); await raf(); };
  const key = (k, opt = {}, target = F.input) => { const e = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...opt }); target.dispatchEvent(e); return e; };
  const step = async (k, opt) => { key(k, opt); await raf(); await raf(); };
  const toBottom = async () => { root.scrollTop = root.scrollHeight; s.view.stick = true; s.view.jump.hidden = true; await raf(); await raf(); };
  // 真实剪贴板：整节都换成会报错的计数桩（任何代码碰到都会被数到）
  let clipTouched = 0;
  const clip = navigator.clipboard;
  const origWT = clip && clip.writeText, origW = clip && clip.write, origRT = clip && clip.readText;
  if (clip) {
    clip.writeText = () => { clipTouched++; return Promise.reject(new Error('回放页不允许写剪贴板')); };
    clip.write = () => { clipTouched++; return Promise.reject(new Error('回放页不允许写剪贴板')); };
    clip.readText = () => { clipTouched++; return Promise.reject(new Error('回放页不允许读剪贴板')); };
  }
  const lsBefore = ['ccr.tools', 'ccr.chatw'].map((k) => localStorage.getItem(k));

  // ======== 会话内查找 ========
  log('— 14j-1 会话内查找');
  pushTranscript(tlFindEntries(), true); await raf(); flushNow(s);
  await toBottom();
  const target = s.items.find((it) => it.kind === 'user' && it.text.includes('宝藏ALPHA'));
  check(s.renderStart > 0 && target && s.items.indexOf(target) < s.renderStart && !target.el, '目标消息在「显示更早」之前、还没渲染（renderStart=' + s.renderStart + '）');
  check(!F.isOpen() && host.querySelectorAll('.find-bar').length === 1 && !root.querySelector('.find-bar'), '#chat-host 里有一个查找栏（覆盖层，不在时间线里），默认隐藏');
  const scrollH0 = root.scrollHeight, colW0 = s.view.col.getBoundingClientRect().width;
  const kd = new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true });
  document.dispatchEvent(kd);
  await raf();
  check(kd.defaultPrevented && F.isOpen() && document.activeElement === F.input, '⌘F：打开查找栏、焦点在输入框、拦下浏览器自己的查找');
  check(root.scrollHeight === scrollH0 && Math.abs(s.view.col.getBoundingClientRect().width - colW0) < 0.5, '打开查找栏不改变时间线的高度与列宽');
  await type('宝藏alpha');
  check(F.count.textContent === '1/1', '忽略大小写：「宝藏alpha」命中「宝藏ALPHA」（' + F.count.textContent + '）');
  check(!!target.el && target.el.isConnected && s.renderStart <= s.items.indexOf(target), '命中在未渲染区：先补渲染（renderStart=' + s.renderStart + '）');
  check(activeText() === '宝藏ALPHA' && inView(activeRange()), '当前那一处高亮（CSS Highlight API）且滚到视口里');
  check(!s.view.stick, '跳到上面的命中后不再贴底');
  await type('话题');
  const nTopic = F.matches.length;
  check(nTopic >= 340 && /^\d+\/\d+$/.test(F.count.textContent), '中文查询：「话题」' + nTopic + ' 处（用户消息 + 回答里的粗体）');
  const i0 = F.active;
  await step('Enter');
  check(F.active === (i0 + 1) % nTopic && F.count.textContent === (F.active + 1) + '/' + nTopic, 'Enter = 下一个（' + F.count.textContent + '）');
  await step('Enter', { shiftKey: true });
  check(F.active === i0, 'Shift+Enter = 上一个');
  const kg = new KeyboardEvent('keydown', { key: 'g', metaKey: true, bubbles: true, cancelable: true });
  document.dispatchEvent(kg); await raf(); await raf();
  check(kg.defaultPrevented && F.active === (i0 + 1) % nTopic, '⌘G = 下一个');
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', metaKey: true, shiftKey: true, bubbles: true, cancelable: true })); await raf(); await raf();
  check(F.active === i0, '⇧⌘G = 上一个');
  F.go(nTopic - 1); await raf();
  await step('Enter');
  check(F.active === 0 && activeText() === '话题' && inView(activeRange()) && s.renderStart === 0, '最后一处再按 Enter 回到第一处（循环；第一处在最前面，已补渲染）');
  await toBottom(); await raf(); await raf();
  F.paint(); await raf();
  const painted = hl(FD.FIND_HL) + hl(FD.FIND_HL_ACTIVE);
  check(painted > 0 && painted < nTopic / 2, '只画视口上下各一屏以内的命中（画了 ' + painted + ' 处，共 ' + nTopic + ' 处）');
  const ki = key('Enter', { isComposing: true });
  check(!ki.defaultPrevented, '输入法组字中的回车不算');
  await type('NEEDLE-XYZ');
  const tool = s.items.find((it) => it.kind === 'tool' && it.toolId === 'tl-bash');
  check(F.matches.length === 3 && F.matches[0].item === tool && F.matches[0].part === 0, '工具卡：标题（命令摘要）1 处 + 展开后的正文 2 处（命令 + 输出）= ' + F.matches.length);
  check(F.active === 2 && !tool.expanded && /按 Enter 展开/.test(F.note.textContent), '命中都在视口上方：从最近的一处（最后一处，在折叠的输出里）开始，不自动展开');
  F.go(0); await raf();
  check(!tool.expanded && activeText().toLowerCase() === 'needle-xyz' && !!activeRange() && !!activeRange().startContainer.parentElement.closest('.tool-head') && !F.note.textContent, '标题上的那一处不用展开');
  await step('Enter');
  check(tool.expanded && !!tool.el.querySelector('.tool-body') && /已展开/.test(F.note.textContent) && !!activeRange() && !!activeRange().startContainer.parentElement.closest('.tool-body') && inView(activeRange()),
    '下一处在折叠的工具卡正文里：展开并说明「' + F.note.textContent + '」');
  tool.expanded = false; s.dirty.add(tool); flushNow(s);
  await type('needle-out');
  check(F.count.textContent === '1/1' && !tool.expanded && /按 Enter 展开/.test(F.note.textContent), '边输入边找：命中只在折叠的输出里时不自动展开，提示「' + F.note.textContent + '」');
  await step('Enter');
  check(tool.expanded && F.active === 0 && F.count.textContent === '1/1' && activeText() === 'needle-out', '第一次 Enter 先展开这一处（不跳到下一处）');
  const think = s.items.find((it) => it.kind === 'thinking' && /needle-think/.test(it.text));
  await type('needle-think');
  check(!think.expanded && /思考/.test(F.note.textContent), '命中在折叠的思考里：提示');
  await step('Enter');
  check(think.expanded && activeText() === 'needle-think', 'Enter：展开思考并高亮');
  await type('detail-needle');
  await step('Enter');
  const hint = s.items.find((it) => it.kind === 'hint' && /detail-needle/.test(it.detail || ''));
  check(!!hint && hint.el.querySelector('details').open && activeText() === 'detail-needle', '命中在系统消息的详情里：打开 <details>');
  // 流式新条目：查找栏开着时到达，重新计数并保持当前那一处
  await type('话题');
  F.go(5); await raf();
  const keepItem = F.matches[F.active].item, keepN = F.matches.length;
  pushTranscript([{ type: 'user', uuid: 'tl-live', timestamp: new Date().toISOString(), message: { role: 'user', content: '新到达的话题消息' } }], false);
  await raf(); flushNow(s); await sleep(320); await raf();
  check(F.matches.length === keepN + 1 && F.matches[F.active].item === keepItem, '查找期间新到达的条目：计数 +1（' + keepN + ' → ' + F.matches.length + '），当前那一处不变');
  await type('没有这个词zzz');
  check(F.count.textContent === '0/0' && F.count.classList.contains('none') && F.nextBtn.disabled && hl(FD.FIND_HL_ACTIVE) === 0, '没有命中：0/0（红色）、上下按钮禁用、无高亮');
  await type('   ');
  check(F.count.textContent === '' && F.matches.length === 0, '全是空白 = 空查询');
  // Esc 关闭：焦点回到原来的输入框，高亮清掉
  F.close();
  $('fx-input').focus();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true }));
  await type('话题');
  const ke = key('Escape');
  await raf();
  check(ke.defaultPrevented && !F.isOpen() && document.activeElement === $('fx-input') && !CSS.highlights.has(FD.FIND_HL) && !CSS.highlights.has(FD.FIND_HL_ACTIVE), 'Esc：关闭、焦点回到输入框、清掉高亮');
  check(!document.querySelector('.find-bar:not([hidden])'), '关闭后查找栏隐藏');
  await click($('fx-find-btn'), '会话头「查找」');
  check(F.isOpen() && F.input.value === '话题' && F.matches.length > 0, '会话头「查找」按钮：打开并沿用上次的查询');
  F.close();

  // <mark> 退路（不支持 CSS Highlight API 时；WKWebView 旧版本）
  const MK = new FD.SessionFind(host, { reveal: revealItem, flush: flushNow, forceMark: true });
  MK.setSession(s);
  await toBottom();
  const lastUser = [...s.items].reverse().find((it) => it.kind === 'user' && /话题/.test(it.text));
  const txt0 = lastUser.el.textContent;
  MK.open();
  await type('话题', MK);
  const marks = root.querySelectorAll('mark.ccr-find-mark');
  const act = root.querySelectorAll('mark.ccr-find-mark.active');
  check(marks.length > 0 && act.length === 1 && act[0].textContent === '话题' && !CSS.highlights.has(FD.FIND_HL),
    '退路：<mark> 包住命中（' + marks.length + ' 处，当前 ' + act.length + ' 处「' + (act[0] && act[0].textContent) + '」，Highlight=' + CSS.highlights.has(FD.FIND_HL) + '）');
  const act0 = act[0];
  key('Enter', {}, MK.input); await raf(); await raf();
  const act1 = root.querySelectorAll('mark.ccr-find-mark.active');
  check(act1.length === 1 && act1[0] !== act0 && act1[0].textContent === '话题' && !act0.isConnected, '退路：下一个之后仍只有一处是当前（旧的 mark 先拆掉再包：' + act1.length + '）');
  MK.close();
  check(!root.querySelector('mark.ccr-find-mark') && lastUser.el.textContent === txt0, '退路：关闭后 mark 全部拆掉，文字原样');
  MK.setSession(null); MK.el.remove();

  // 浅色 / 深色：高亮颜色的对比度
  {
    const th = window.ccrTheme, prev = th.pref();
    const probe = h('span', { style: { position: 'fixed', left: '-9999px' } });
    document.body.appendChild(probe);
    const colorOf = (v, prop = 'backgroundColor') => { probe.style.backgroundColor = ''; probe.style.color = ''; probe.style[prop] = 'var(' + v + ')'; return rgbaOf(getComputedStyle(probe)[prop]); };
    const res = [];
    for (const theme of ['light', 'dark']) {
      th.set(theme, { persist: false }); await raf();
      const bg = colorOf('--bg'), text = colorOf('--text', 'color');
      const findBg = over(colorOf('--find-bg'), bg), actBg = over(colorOf('--find-active-bg'), bg), actFg = colorOf('--find-active-fg', 'color');
      res.push([theme, contrast(text, findBg), contrast(actFg, actBg), contrast(actBg, findBg)]);
    }
    th.set(prev, { persist: false }); await raf();
    probe.remove();
    check(res.every((r) => r[1] >= 4.5 && r[2] >= 4.5), '浅色 / 深色：命中底色上正文、当前那一处的文字对比度 ≥ 4.5（' + res.map((r) => r[0] + ' ' + r[1].toFixed(1) + '/' + r[2].toFixed(1)).join('，') + '）');
    check(res.every((r) => r[3] >= 1.25), '当前那一处与其它命中的底色区分得开（' + res.map((r) => r[3].toFixed(2)).join(' / ') + '）');
  }

  // ======== 工具调用显示档位 ========
  log('— 14j-2 工具调用显示档位');
  pushTranscript(tlTierEntries(1), true); await raf(); flushNow(s);
  await toBottom();
  // 只核对「全部」档里本来就显示的条目（没有可显示内容的本轮卡片本来就是空的）
  const base = new Set(s.items.filter((it) => it.el && it.el.getClientRects().length > 0));
  const tiersOk = (tier) => {
    const keep = TV.zenKeep(s.items);
    const bad = [];
    for (const it of s.items) {
      if (!it.el || !base.has(it)) continue;
      const shown = it.el.getClientRects().length > 0;
      if (shown !== TV.itemShown(it, tier, { st: TV.renderedState(it), keep })) bad.push(it.kind + ':' + (it.text || it.name || ''));
    }
    return bad;
  };
  const shownTexts = () => s.items.filter((it) => it.el && it.el.getClientRects().length > 0).map((it) => it.kind === 'tool' ? 'tool:' + it.name + ':' + TV.renderedState(it) : it.kind + ':' + (it.text || ''));
  check(fxTiers.tier === 'all' && !root.dataset.tools && tiersOk('all').length === 0, '默认「全部」：所有条目都显示');
  fxTiers.set('hideok'); await raf();
  const okShown = shownTexts();
  check(root.dataset.tools === 'hideok' && tiersOk('hideok').length === 0, '「隐藏成功的」：每条条目的显示与 itemShown 一致（' + tiersOk('hideok').join('，') + '）');
  check(!okShown.includes('tool:Read:done') && !okShown.includes('tool:Bash:done') && okShown.includes('tool:Bash:error') && okShown.includes('tool:Write:denied') && okShown.includes('tool:Grep:running'),
    '「隐藏成功的」：成功的 Read / Bash 隐藏；失败、被拒绝、运行中（含等待审批）的仍显示');
  check(okShown.includes('thinking:想一想') && okShown.includes('text:先看看文件（中间说明）'), '「隐藏成功的」：思考与中间说明照常');
  fxTiers.set('hideall'); await raf();
  const allShown = shownTexts();
  check(tiersOk('hideall').length === 0 && !allShown.some((t) => t.startsWith('tool:')) && allShown.includes('text:先看看文件（中间说明）'), '「全部隐藏」：没有工具卡；Claude 的中间说明照常');
  fxTiers.set('zen'); await raf();
  const zen = shownTexts();
  check(tiersOk('zen').length === 0, '「只看对话」：每条条目的显示与 itemShown 一致（' + tiersOk('zen').join('，') + '）');
  check(JSON.stringify(zen.filter((t) => !t.startsWith('hint'))) === JSON.stringify(['user:层级测试 A0', 'text:A0 轮的最终回复', 'user:层级测试 B0', 'text:B0 回复前半段', 'user:中途追加的消息 0', 'text:B0 最终回复']),
    '「只看对话」：你的消息 + 每轮 Claude 最后一段回复（回复中途追加消息之前的那段也留下）：' + zen.join(' | '));
  check(zen.includes('hint:警告：示例 0') && !zen.includes('hint:普通系统信息 0'), '「只看对话」：警告仍显示，普通系统信息隐藏');
  const keepEl = s.items.find((it) => it.text === 'B0 最终回复').el;
  check(keepEl.dataset.zen === 'keep' && getComputedStyle(keepEl.querySelector('.mm-text')).display === 'flex', '保留的回复下方仍有时间 / 复制');
  // 流式：同一轮里新的一段回复到达 → 上一段收起，新的一段显示
  pushTranscript([{ type: 'assistant', uuid: 'tv-live', timestamp: new Date().toISOString(), message: { id: 'tv-live', role: 'assistant', content: [{ type: 'text', text: '又一段新回复' }] } }], false);
  await raf(); flushNow(s); await raf();
  check(keepEl.getClientRects().length === 0 && s.items[s.items.length - 1].el.getClientRects().length > 0, '「只看对话」流式：新的一段到达后只显示最新一段');
  // 阅读位置
  pushTranscript(tlTierEntries(14), true); await raf(); flushNow(s);
  fxTiers.set('all'); await raf();
  const posCheck = async (to) => {
    const rr = root.getBoundingClientRect();
    const before = s.items.filter((it) => it.el && it.el.getClientRects().length).map((it) => ({ it, top: it.el.getBoundingClientRect().top - rr.top, bottom: it.el.getBoundingClientRect().bottom - rr.top }))
      .filter((x) => x.bottom > 0 && x.top < root.clientHeight);
    fxTiers.set(to); await raf();
    const keep = TV.zenKeep(s.items);
    const a = before.find((x) => TV.itemShown(x.it, to, { st: TV.renderedState(x.it), keep }));
    if (!a) return 'no-anchor';
    const now = a.it.el.getBoundingClientRect().top - root.getBoundingClientRect().top;
    return Math.abs(now - a.top);
  };
  root.scrollTop = Math.round(root.scrollHeight * 0.45); s.view.stick = false; await raf(); await raf();
  const d1 = await posCheck('hideok'), d2 = await posCheck('zen'), d3 = await posCheck('hideall'), d4 = await posCheck('all');
  check([d1, d2, d3, d4].every((d) => typeof d === 'number' && d <= 1), '切换档位保持阅读位置：视口里第一条仍显示的条目不动（偏差 ' + [d1, d2, d3, d4].map((d) => typeof d === 'number' ? d.toFixed(1) : d).join(' / ') + ' px）');
  // 视口顶端正好是一张工具卡（切换后被隐藏）：锚点换成下一条
  const tcard = s.items.find((it, k) => k > s.items.length / 2 && it.kind === 'tool' && it.name === 'Read');
  root.scrollTop += tcard.el.getBoundingClientRect().top - root.getBoundingClientRect().top - 2; await raf();
  const d5 = await posCheck('hideall');
  check(typeof d5 === 'number' && d5 <= 1, '视口顶端的工具卡被隐藏时，以其后第一条仍显示的条目为锚（偏差 ' + (typeof d5 === 'number' ? d5.toFixed(1) : d5) + ' px）');
  fxTiers.set('all'); await raf();
  await toBottom();
  fxTiers.set('zen'); await raf();
  check(root.scrollHeight - root.scrollTop - root.clientHeight < 2, '贴底时切换档位：继续贴底');
  fxTiers.set('all'); await raf();
  // 菜单
  await click(fxTiers.el, '工具调用显示按钮');
  const menu = document.querySelector('.popover.tier-menu');
  check(!!menu && menu.querySelectorAll('.tier-row').length === 4 && menu.querySelector('.tier-row.on').dataset.tier === 'all', '会话头「工具」：四档菜单，当前档打勾');
  await click(menu.querySelector('.tier-row[data-tier="hideok"]'), '隐藏成功的');
  check(!document.querySelector('.popover.tier-menu') && fxTiers.tier === 'hideok' && fxTiers.el.classList.contains('on') && fxTiers.el.textContent.includes('隐藏成功'), '选「隐藏成功的」：菜单关闭、按钮高亮并显示档位');
  await click(fxTiers.el, '工具调用显示按钮'); await raf();
  key('Escape', {}, document);
  await raf();
  check(!document.querySelector('.popover.tier-menu'), 'Esc 关闭菜单');
  // 查找命中被隐藏的条目：查找期间临时显示
  fxTiers.set('hideall'); await raf();
  root.scrollTop = Math.round(root.scrollHeight * 0.3); s.view.stick = false; await raf();
  F.open();
  await type('echo ok');
  const hidTool = F.matches.length ? F.matches[F.active].item : null;
  check(!!hidTool && hidTool.kind === 'tool' && hidTool.el.getClientRects().length === 0 && /按 Enter 临时显示/.test(F.note.textContent), '命中在被档位隐藏的工具卡上：边输入边找时只提示');
  await step('Enter');
  check(!!hidTool && hidTool.el.dataset.findShow === '1' && hidTool.el.getClientRects().length > 0 && /临时显示/.test(F.note.textContent) && inView(activeRange()), 'Enter：临时显示那一条并高亮');
  const rr0 = root.getBoundingClientRect();
  const atTop = s.items.find((it) => it.el && it !== hidTool && it.el.getClientRects().length && it.el.getBoundingClientRect().bottom - rr0.top > 0);
  const atTop0 = atTop.el.getBoundingClientRect().top;
  F.close(); await raf();
  check(!hidTool.el.dataset.findShow && hidTool.el.getClientRects().length === 0, '关闭查找：恢复隐藏');
  check(Math.abs(atTop.el.getBoundingClientRect().top - atTop0) <= 1, '恢复隐藏时保持阅读位置（视口顶端的条目不动）');
  fxTiers.set('all'); await raf();
  check(localStorage.getItem('ccr.tools') === lsBefore[0], '回放页切换档位不写 localStorage');
  check(TV.normTier('x') === 'all' && TV.hiddenToolCount(s.items, 'hideok') > 0, '纯函数：非法档位 = 全部；隐藏计数');

  // ======== 引用回复 ========
  log('— 14j-3 引用回复');
  const qts = new Date().toISOString();
  pushTranscript([
    { type: 'user', uuid: 'qt-u1', timestamp: qts, message: { role: 'user', content: '> 引用的话\n> 第二行\n\n我的回复正文' } },
    { type: 'assistant', uuid: 'qt-a1', timestamp: qts, message: { id: 'qt-m1', role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text', text: '这是 Claude 的回答，包含**粗体**和 `code`。\n\n第二段文字。\n\n```py\nprint(1)\n```' }] } },
    { type: 'user', uuid: 'qt-u2', timestamp: qts, message: { role: 'user', content: '普通的一条消息' } },
  ], true);
  await raf(); flushNow(s); await toBottom();
  const u1 = s.items.find((it) => it.kind === 'user' && /引用的话/.test(it.text)).el;
  check(!!u1.querySelector('.user-quote') && u1.querySelector('.user-quote').textContent.includes('第二行') && u1.querySelector('.user-text').textContent === '我的回复正文',
    '发出的消息开头的引用 = 气泡里的内嵌卡片，正文照旧');
  const u2 = s.items.find((it) => it.kind === 'user' && it.text === '普通的一条消息').el;
  check(!u2.querySelector('.user-quote') && u2.querySelector('.user-text').textContent === '普通的一条消息', '没有引用的消息与以前完全一样');
  const a1 = s.items.find((it) => it.kind === 'text' && /Claude 的回答/.test(it.text)).el;
  const ps = a1.querySelectorAll('p');
  const sel = window.getSelection();
  const selectRange = (a, ao, b, bo) => { const r = document.createRange(); r.setStart(a, ao); r.setEnd(b, bo); sel.removeAllRanges(); sel.addRange(r); };
  selectRange(ps[0].firstChild, 0, ps[1].lastChild, ps[1].lastChild.length);
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  await sleep(20); await raf();
  const pop = document.querySelector('.quote-pop');
  const pr = pop.getBoundingClientRect();
  check(!pop.hidden && pr.width > 0 && pr.top >= 0 && pr.bottom <= innerHeight, '选中 Claude 回复里的文字：浮出「引用」按钮');
  await click(pop.querySelector('button'), '引用');
  let ql = fxQuotes.list(SID);
  check(ql.length === 1 && ql[0].kind === 'chat' && ql[0].label === 'Claude 的回复' && ql[0].text === '这是 Claude 的回答，包含粗体和 code。\n\n第二段文字。', '点「引用」：加进引用条（纯文字，段落之间空一行）：' + JSON.stringify(ql[0] && ql[0].text));
  check(sel.isCollapsed && pop.hidden && !fxQuoteBar.el.hidden && fxQuoteBar.el.querySelectorAll('.quote-chip').length === 1 && document.activeElement === $('fx-input'), '选区清掉、按钮收起、输入框上方出现引用条、焦点到输入框');
  const ut = u2.querySelector('.user-text').firstChild;
  selectRange(ut, 0, ut, 4);
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  await sleep(20); await raf();
  await click(pop.querySelector('button'), '引用');
  ql = fxQuotes.list(SID);
  check(ql.length === 2 && ql[1].text === '普通的一' && ql[1].label === '你的消息' && fxQuoteBar.el.querySelectorAll('.quote-chip').length === 2 && !!fxQuoteBar.el.querySelector('.quote-clear'), '可以多条：第二条来自你的消息；≥2 条时有「全部清除」');
  // 代码块里的选区：保留换行（white-space: pre）
  const code = a1.querySelector('.md-code pre code');
  selectRange(code.firstChild || code, 0, code.lastChild || code, (code.lastChild || code).textContent.length);
  const cq = QT.selectionQuote(sel);
  check(!!cq && cq.text === 'print(1)', '代码块选区：只要代码，不带语言标签和「复制」：' + JSON.stringify(cq && cq.text));
  sel.removeAllRanges();
  // 跨两条消息：标签「时间线」
  selectRange(u1.querySelector('.user-text').firstChild, 0, ps[0].firstChild, 4);
  const xq = QT.selectionQuote(sel);
  check(!!xq && xq.label === '时间线' && /我的回复正文/.test(xq.text) && /这是 C/.test(xq.text), '跨两条消息的选区也能引用（标签「时间线」）');
  sel.removeAllRanges();
  // 输入框里的选区不算
  $('fx-input').value = 'abc'; $('fx-input').focus(); $('fx-input').select();
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  await sleep(20); await raf();
  check(pop.hidden, '输入框里选中文字不出「引用」按钮');
  $('fx-input').value = '';
  // 删除一条
  await click(fxQuoteBar.el.querySelector('.quote-chip .quote-x'), '删除第一条引用');
  ql = fxQuotes.list(SID);
  check(ql.length === 1 && ql[0].text === '普通的一' && fxQuoteBar.el.querySelectorAll('.quote-chip').length === 1 && !fxQuoteBar.el.querySelector('.quote-clear'), '× 只删这一条');
  // 发送：拼成 Markdown 引用放在文字前面
  $('fx-input').value = '我的问题';
  const nMsg = calls.length;
  await click($('fx-send'), '发送');
  const sent = calls.slice(nMsg).find((c) => c.op === 'message');
  check(!!sent && sent.body.text === '> 普通的一\n\n我的问题' && fxQuotes.list(SID).length === 0 && fxQuoteBar.el.hidden, '发送：「> 引用」+ 空行 + 用户文字；发出后引用条清空：' + JSON.stringify(sent && sent.body.text));
  fxQuotes.add(SID, { kind: 'chat', text: '第一段\n\n第二段', label: 'Claude 的回复' });
  $('fx-input').value = '/compact';
  const nMsg2 = calls.length;
  await click($('fx-send'), '发送命令');
  const sent2 = calls.slice(nMsg2).find((c) => c.op === 'message');
  check(!!sent2 && sent2.body.text === '/compact' && fxQuotes.list(SID).length === 1, '以 / 开头的命令不带引用，引用保留到下一条');
  fxQuotes.clear(SID);
  check(fxQuoteBar.el.hidden, '清空后引用条隐藏');
  // 纯函数：多条引用之间空一行、文件引用带 路径:行号 与代码围栏
  const md2 = QT.composeMessage([{ kind: 'chat', text: 'a\n\nb' }, { kind: 'file', path: VCWD + '/src/x.py', line: 3, endLine: 4, text: 'def f():\n    return 1' }], '问题', VCWD).text;
  check(md2 === '> a\n>\n> b\n\n> src/x.py:3-4\n> ```python\n> def f():\n>     return 1\n> ```\n\n问题', '拼接：多条引用之间空一行；文件引用 = 路径:行号 + 代码围栏（缩进保留）');
  // 文件预览里的选区：带上路径与行号
  ensureVizPanel();
  initViz({ api: vizApi, getConfig: () => ({}), toast: (t) => log('（toast）' + t), panel: vizPanel });
  const wasLocal = s.localOverride;
  s.localOverride = true;
  vizPanel.setSession(s);
  vizPanel.open(s, { kind: 'file', path: VCWD + '/site/style.css' });
  let pre = null;
  for (let k = 0; k < 100 && !(pre = document.querySelector('.pv .pv-text .pv-lines')); k++) await sleep(30);
  if (pre) {
    const all = pre.textContent;
    const lines = all.split('\n');
    const off = (ln) => lines.slice(0, ln - 1).reduce((a, l) => a + l.length + 1, 0);
    const nodeAt = (o) => {
      const w = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
      let n, acc = 0;
      while ((n = w.nextNode())) { if (acc + n.length >= o) return [n, o - acc]; acc += n.length; }
      return [pre, 0];
    };
    const [sn, so] = nodeAt(off(2)), [en, eo] = nodeAt(off(3) + lines[2].length);
    selectRange(sn, so, en, eo);
    const fq = QT.selectionQuote(sel);
    check(!!fq && fq.kind === 'file' && fq.path === VCWD + '/site/style.css' && fq.line === 2 && fq.endLine === 3 && fq.text === lines[1] + '\n' + lines[2],
      '右侧预览面板的文件里选中第 2–3 行：引用带 路径:行号（' + (fq ? QT.quoteSource(fq, VCWD) : '') + '）');
    check(!!fq && QT.quoteMarkdown(fq, VCWD).startsWith('> site/style.css:2-3\n> ```css\n'), '文件引用拼成 `> site/style.css:2-3` + ```css 围栏');
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    await sleep(20); await raf();
    check(!pop.hidden, '预览面板里的选区也浮出「引用」按钮');
    sel.removeAllRanges(); fxPicker.hide();
  } else check(false, '右侧预览面板没有打开文本文件');
  vizPanel.closeAll();
  s.localOverride = wasLocal;
  // diff（代码改动 / diff 标签 / 工具卡里的 structuredPatch）
  const box = h('div', { class: 'pv', style: { position: 'fixed', left: '0', top: '0', width: '600px' } },
    h('div', { class: 'chg-file', dataset: { path: VCWD + '/calc.py' } },
      renderPatch([{ oldStart: 10, oldLines: 3, newStart: 10, newLines: 4, lines: [' a = 1', '-b = 2', '+b = 3', '+c = 4', ' d = 5'] }])));
  document.body.appendChild(box);
  const rows = box.querySelectorAll('.diff-del, .diff-add');
  selectRange(rows[0].lastChild, 0, rows[2].lastChild, rows[2].lastChild.length);
  const dq = QT.selectionQuote(sel);
  check(!!dq && dq.kind === 'diff' && dq.path === VCWD + '/calc.py' && dq.text === '-b = 2\n+b = 3\n+c = 4' && dq.line === 11 && dq.endLine === 12,
    'diff 里的选区：带 +/- 号、路径与新文件行号（' + (dq ? QT.quoteSource(dq, VCWD) + ' ' + JSON.stringify(dq.text) : '') + '）');
  sel.removeAllRanges();
  box.remove();
  check(QT.splitLeadingQuotes('> a\n>\n> b\n\n> c\n\n正文\n> 不是开头').quotes.length === 2 && QT.splitLeadingQuotes('正文 > x').quotes.length === 0, '纯函数：开头的引用块拆分');

  // ======== 表格复制 ========
  log('— 14j-4 表格复制（复制实现是桩）');
  pushTranscript([
    { type: 'assistant', uuid: 'tb-a1', timestamp: qts, message: { id: 'tb-m1', role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text',
      text: '结果如下：\n\n| 名称 | 值 | 说明 |\n|:---|---:|:---:|\n| a\\|b | 1,000 | 含 "引号" |\n| `x|y` | **2** | 多  空格 |\n\n完毕。' }] } },
  ], true);
  await raf(); flushNow(s);
  const tbox = root.querySelector('.md-table-box');
  const acts = tbox && tbox.querySelector('.tbl-acts');
  check(!!tbox && !!tbox.querySelector('.md-table-wrap > table') && !!acts && acts.querySelectorAll('button.tbl-copy').length === 2, '渲染出的表格下方有「复制为 Markdown / CSV」');
  check(getComputedStyle(acts).opacity === '0', '平时不显示（悬停 / 键盘聚焦才出现，位置照占）');
  acts.querySelector('button').focus(); await sleep(220);
  check(getComputedStyle(acts).opacity === '1', '键盘聚焦时出现');
  const nCopy = fxCopied.length;
  await click(acts.querySelectorAll('button.tbl-copy')[0], '复制为 Markdown');
  check(fxCopied[nCopy] === '| 名称 | 值 | 说明 |\n| :--- | ---: | :---: |\n| a\\|b | 1,000 | 含 "引号" |\n| `x\\|y` | **2** | 多 空格 |',
    'Markdown：保留对齐与单元格里的记号，| 转义：' + JSON.stringify(fxCopied[nCopy]));
  check(acts.querySelectorAll('button.tbl-copy')[0].textContent.includes('已复制'), '复制后按钮显示「已复制」');
  await click(acts.querySelectorAll('button.tbl-copy')[1], '复制为 CSV');
  check(fxCopied[nCopy + 1] === '名称,值,说明\na|b,"1,000","含 ""引号"""\nx|y,2,多 空格', 'CSV：显示的文字、含逗号 / 引号的加引号并转义：' + JSON.stringify(fxCopied[nCopy + 1]));
  check(MT.tableToCSV([['a\nb', ' x '], ['p,q', 'say "hi"']]) === 'a b,x\n"p,q","say ""hi"""' && MT.mdCell('a|b') === 'a\\|b', '纯函数：换行折叠成空格、首尾空白、逗号 / 引号、竖线');

  // ======== 聊天宽度 ========
  log('— 14j-5 聊天宽度');
  pushTranscript(tlFindEntries(8), true); await raf(); flushNow(s); await toBottom();
  const col = s.view.col, inner = document.querySelector('.fx .composer-inner');
  let cwEvents = 0;
  const onCw = () => { cwEvents++; fxMinimap.measure(); };
  window.addEventListener('ccr:chatw', onCw);
  const mx = (el) => getComputedStyle(el).maxWidth;
  const center = (el) => { const r = el.getBoundingClientRect(); return r.left + r.width / 2; };
  check(CW.currentWidth() === 'std' && mx(col) === '800px' && mx(inner) === '800px', '标准：消息列与输入框都是 800px');
  const g0 = fxMinimap.gutter;
  CW.applyWidth('wide', { persist: false }); await raf();
  check(mx(col) === '1000px' && mx(inner) === '1000px' && Math.abs(center(col) - center(inner)) <= 8, '宽：1000px，消息列与输入框同宽同中线');
  CW.applyWidth('full', { persist: false }); await raf(); await raf();
  const hostW = host.getBoundingClientRect().width;
  const cr = col.getBoundingClientRect(), ir = inner.getBoundingClientRect();
  check(mx(inner) === 'none' && cr.width >= hostW - 60 && ir.width >= hostW - 60 && Math.abs(cr.left - ir.left) <= 1,
    '全宽：占满时间线（' + Math.round(cr.width) + ' / ' + Math.round(hostW) + 'px），输入框跟着变宽（' + Math.round(ir.width) + 'px），消息与输入框左缘对齐');
  check(cwEvents === 2 && fxMinimap.gutter !== g0 && fxMinimap.root.dataset.persistent === 'true', '派发 ccr:chatw，小格子按新的留白重新测量（' + Math.round(g0) + ' → ' + Math.round(fxMinimap.gutter) + 'px，全宽时仍常驻）');
  const field = CW.chatWidthField({ persist: false });
  document.body.appendChild(field.el);
  check(field.el.querySelectorAll('.cw-item').length === 4 && field.el.querySelector('.cw-item.on').dataset.cw === 'full', '设置里的「聊天宽度」：四档分段按钮，当前档选中');
  await click(field.el.querySelector('[data-cw="wider"]'), '很宽');
  check(CW.currentWidth() === 'wider' && mx(col) === '1280px' && field.el.querySelector('[data-cw="wider"]').getAttribute('aria-checked') === 'true', '点「很宽」立即生效（1280px）');
  // 在设置里切换时保持阅读位置（app.js 用同样的包装：toolview.keepReading）
  CW.applyWidth('std', { persist: false }); await raf();
  pushTranscript(tlFindEntries(60), true); await raf(); flushNow(s);
  root.scrollTop = Math.round(root.scrollHeight * 0.5); s.view.stick = false; await raf(); await raf();
  const rrw = root.getBoundingClientRect();
  const anc = s.items.find((it) => it.el && it.el.getBoundingClientRect().bottom - rrw.top > 0);
  const ancTop = anc.el.getBoundingClientRect().top;
  CW.setWidthWrapper((apply) => TV.keepReading(s, apply));
  await click(field.el.querySelector('[data-cw="full"]'), '全宽');
  CW.setWidthWrapper(null);
  check(CW.currentWidth() === 'full' && Math.abs(anc.el.getBoundingClientRect().top - ancTop) <= 1, '在设置里切换宽度：文字重排后视口顶端的条目不动（偏差 ' + Math.abs(anc.el.getBoundingClientRect().top - ancTop).toFixed(1) + 'px）');
  field.el.remove();
  CW.applyWidth('std', { persist: false }); await raf();
  window.removeEventListener('ccr:chatw', onCw);
  fxMinimap.measure();
  check(mx(col) === '800px' && !document.documentElement.hasAttribute('data-chatw') && CW.colWidth('x') === '800px', '恢复标准：不写 data-chatw（与以前完全相同）');
  check(localStorage.getItem('ccr.chatw') === lsBefore[1], '回放页切换宽度不写 localStorage');

  // ======== 红线 ========
  if (clip) { clip.writeText = origWT; clip.write = origW; clip.readText = origRT; }
  check(clipTouched === 0, '整节没有读写真实剪贴板（复制走桩、引用只改自己的输入框）');
  check(calls.slice(n0).every((c) => c.gesture && c.op === 'message'), '本节只有点击「发送」产生的请求（' + calls.slice(n0).length + ' 个），查找 / 档位 / 引用 / 复制 / 宽度都不发请求');
  F.close();
  pushTranscript(transcriptEntries(), true); await raf(); flushNow(s); await toBottom();
}

// ---------- 绑定 ----------
$('fx-show').addEventListener('click', () => show($('fx-scene').value));
$('fx-stream').addEventListener('click', () => playStream(Number($('fx-speed').value) || 2));
$('fx-transcript').addEventListener('click', () => { pushTranscript(transcriptEntries(), true); log('transcript：' + s.items.length + ' 个条目'); });
$('fx-viz').addEventListener('click', () => {
  ensureVizPanel();
  initViz({ api: vizApi, getConfig: () => ({}), toast: (t) => log('（toast）' + t), panel: vizPanel });
  _setBlobLoader((d) => refPng(d.ref));
  s.localOverride = $('fx-viz-remote').checked ? false : true;
  pushTranscript(vizEntries(), true);
  log('可视化示例：' + (s.localOverride ? '本机会话' : '服务器会话'));
});
$('fx-auto').addEventListener('click', () => { autoTest().catch((e) => { errors++; log('自测异常：' + (e && e.stack || e), 'bad'); }); });
// 主题：用 theme.js（回放页只在本页生效、不写 localStorage）；?theme=light|dark|system 可直接指定
{
  const th = window.ccrTheme, sel = $('fx-theme');
  const q = new URLSearchParams(location.search).get('theme');
  if (th && q) th.set(q, { persist: false });
  if (th) sel.value = th.pref() === 'system' ? '' : th.pref();
  sel.addEventListener('change', (e) => th && th.set(e.target.value || 'system', { persist: false }));
}
$('fx-send').addEventListener('click', () => {
  // 与 app.js send() 相同：引用条拼成 Markdown 引用放在文字前面（/ 或 ! 开头的命令不带，引用保留）
  const comp = QT.composeMessage(fxQuotes.list(SID), $('fx-input').value, (s && (s.absCwd || s.meta.cwd)) || '');
  gesture++; record('message', { text: comp.text }); gesture--;
  if (comp.used) fxQuotes.clear(SID);
  $('fx-input').value = ''; fxSizer.update();
});
$('fx-stop').addEventListener('click', () => { gesture++; record('interrupt'); gesture--; });
$('fx-file').addEventListener('change', async (e) => {
  for (const f of e.target.files) ingest(f.name, await f.text());
  fillScenes();
});

setup();
loadFixtures().then(() => { if (new URLSearchParams(location.search).get('auto') === '1') $('fx-auto').click(); });

// ---------- 14j. 服务器会话的文件预览与下载、GB18030 文本（srvfiles.js / textdecode.js，SPEC §10.10） ----------
// 桩 api（POST …/srvfile/stat、…/local/stat）+ 桩 fetch（/fxsrv/… 的文件内容），不连后端；下载用 _setDownloader 截住，
// 不真的下载。真实链路（agent op、/preview 路由、缓存、分块下载）由 tests/test_srvfiles.py 与 fake 端到端覆盖。
const SCWD = '/home/demo/proj';
const SGBK = [0xd6, 0xd0, 0xce, 0xc4, 0xb2, 0xe2, 0xca, 0xd4, 0xa3, 0xac, 0x47, 0x42, 0x4b, 0x20, 0xb1, 0xe0, 0xc2, 0xeb, 0xa1, 0xa3, 0x0a,
  0xb5, 0xda, 0xb6, 0xfe, 0xd0, 0xd0, 0x0a];   // "中文测试，GBK 编码。\n第二行\n" 的 GB18030 字节
const SF_ENC = new TextEncoder();
function srvFiles() {
  const big = 25 * 1024 * 1024;
  return {
    'notes.txt': { kind: 'text', body: SF_ENC.encode('hello\nworld 世界\n第三行\n') },
    'gbk.txt': { kind: 'text', body: Uint8Array.from(SGBK), charset: 'gb18030' },
    'blob.txt': { kind: 'text', body: Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]) },
    'big.log': { kind: 'text', size: big, too_large: true, reason: '文件较大（25MB），超过服务器文件预览上限 20MB' },
    'part.log': { kind: 'text', body: SF_ENC.encode('line 1\nline 2\n'), size: 3 * 1024 * 1024, partial: true },
    'pic.png': { kind: 'image', blobUrl: true },
    'site/index.html': { kind: 'html', url: '/static/dev/viz_demo.html' },
    'data.bin': { kind: 'binary', body: Uint8Array.from([0, 1, 2, 3]) },
    'huge.bin': { kind: 'binary', size: 600 * 1024 * 1024, too_large: true, downloadable: false, reason: '文件较大（600MB），超过服务器文件预览上限 20MB' },
    'slow.txt': { kind: 'text', body: SF_ENC.encode('slow ok\n') },
    'flaky.txt': { kind: 'text', body: SF_ENC.encode('flaky ok\n'), failFetch: 1 },
  };
}

async function srvFileChecks() {
  ensureVizPanel();
  vizPanel.closeAll();
  const nCalls = calls.length;
  const files = srvFiles();
  const st = { stat: [], fetches: [], dl: [], toasts: [], failStat: 0, hold: null, gb: null };
  const pngUrl = refPng('srvfile-pic');
  const pngBytes = Uint8Array.from(atob(pngUrl.split(',')[1]), (c) => c.charCodeAt(0));
  const rel = (p) => (p.startsWith(SCWD + '/') ? p.slice(SCWD.length + 1) : p);
  const entry = (p, local) => {
    const r = rel(p);
    if (r === 'src') return { path: p, abs: p, real: p, exists: true, type: 'dir', ok: false, remote: !local };
    const f = files[r];
    if (!f) return { path: p, abs: p, exists: false, ok: false, reason: '文件不存在', remote: !local };
    const size = f.size || (f.body ? f.body.length : pngBytes.length);
    const url = f.url || '/fxsrv/' + (local ? 'l/' : 's/') + r;
    return { path: p, abs: p, real: SCWD + '/' + r, exists: true, type: 'file', ok: true, kind: f.kind, size, mtime: 1, url,
      download_url: f.downloadable === false ? null : url + '?download=1', too_large: !!f.too_large, downloadable: f.downloadable !== false,
      reason: f.reason || null, remote: !local, openable: false, readable: true };
  };
  const srvApi = {
    token: '',
    sessionPath: (sid, tail) => '/api/sessions/' + encodeURIComponent(sid) + tail,
    post: async (path, body) => {
      const local = path.endsWith('/local/stat');
      if (!local && !path.endsWith('/srvfile/stat')) throw new Error('回放页不支持：' + path);
      st.stat.push({ path, paths: body.paths, gesture: gesture > 0 });
      if (st.hold) await st.hold.p;
      if (!local && st.failStat > 0) { st.failStat--; const e = new Error('与计算节点的连接已断开，正在重连…'); e.status = 503; throw e; }
      return { results: (body.paths || []).map((p) => entry(p, local)) };
    },
  };
  const origFetch = window.fetch;
  window.fetch = async (url, opts) => {
    const u = String(url);
    if (!u.startsWith('/fxsrv/')) return origFetch(url, opts);
    st.fetches.push(u);
    const [path, qs] = u.slice('/fxsrv/'.length).split('?');
    const local = path.startsWith('l/');
    const f = files[path.slice(2)];
    const q = new URLSearchParams(qs || '');
    if (!f) return new Response(JSON.stringify({ error: '文件不存在' }), { status: 404 });
    if (f.failFetch > 0) { f.failFetch--; return new Response(JSON.stringify({ error: '远端操作超时（file_read）' }), { status: 504 }); }
    const hd = {};
    let body = f.blobUrl ? pngBytes : f.body;
    if (f.charset) hd['X-CCR-Charset'] = f.charset;
    if (f.charset && q.get('ccr_utf8') === '1') body = SF_ENC.encode(new TextDecoder('gb18030').decode(Uint8Array.from(SGBK)));
    if (f.partial && !local) { hd['X-CCR-Partial'] = '1'; hd['X-CCR-Size'] = String(f.size); }
    return new Response(body, { status: 200, headers: { 'Content-Type': f.blobUrl ? 'image/png' : 'text/plain; charset=utf-8', ...hd } });
  };
  _setDownloader((url, name) => st.dl.push({ url, name }));
  initViz({ api: srvApi, getConfig: () => ({}), toast: (t, lv) => { st.toasts.push({ t, lv }); log('（toast）' + t); }, panel: vizPanel });
  const waitFor = async (pred, ms = 2000) => { const t0 = performance.now(); while (performance.now() - t0 < ms) { if (pred()) return true; await sleep(20); } return !!pred(); };
  const pv = () => document.querySelector('.pv');
  const body = () => pv().querySelector('.pv-body');
  const bar = () => pv().querySelector('.pv-bar');
  const barBtn = (label) => [...bar().querySelectorAll('.pv-act')].find((b) => b.textContent.trim() === label);
  const chip = (name) => qa('.fchip').find((c) => c.dataset.path === SCWD + '/' + name);
  try {
    // ---- 服务器会话的聊天记录：渲染时不发任何请求
    s.localOverride = false;
    const U = (i) => '00000000-0000-4000-8000-1' + String(i).padStart(11, '0');
    const base = (i, type, extra) => ({ type, uuid: U(i), cwd: SCWD, sessionId: '59dd443a-c637-4148-bf1c-fd720f2e5b1f', timestamp: new Date().toISOString(), ...extra });
    const asst = (i, content, stop) => base(i, 'assistant', { message: { id: 'msg_sf' + i, role: 'assistant', content, stop_reason: stop || 'tool_use' } });
    const names = ['notes.txt', 'gbk.txt', 'blob.txt', 'big.log', 'part.log', 'pic.png', 'site/index.html', 'data.bin', 'huge.bin', 'nope.txt', 'slow.txt', 'flaky.txt'];
    pushTranscript([
      base(1, 'user', { message: { role: 'user', content: [{ type: 'text', text: '看看服务器上的这些文件' }] }, origin: { kind: 'human' }, promptSource: 'typed' }),
      asst(2, [{ type: 'tool_use', id: 'sf1', name: 'Edit', input: { file_path: SCWD + '/notes.txt', old_string: 'world', new_string: 'world 世界' } }]),
      base(3, 'user', { message: { role: 'user', content: [{ tool_use_id: 'sf1', type: 'tool_result', content: 'The file has been updated' }] },
        toolUseResult: { filePath: SCWD + '/notes.txt', structuredPatch: [{ oldStart: 2, oldLines: 1, newStart: 2, newLines: 1, lines: ['-world', '+world 世界'] }] } }),
      asst(4, [{ type: 'text', text: '文件：' + names.map((n) => '`' + SCWD + '/' + n + '`').join('、') + '。' }], 'end_turn'),
      base(5, 'system', { subtype: 'turn_duration', durationMs: 900, content: '' }),
    ], true);
    await raf(); flushNow(s);
    await sleep(150);
    const chips = names.map(chip);
    check(chips.every((c) => c && c.classList.contains('srv') && c.classList.contains('remote')) && isSrv(s),
      '服务器会话：' + chips.filter(Boolean).length + ' 个文件 chip 都可点（srv，在右侧面板预览服务器文件）');
    const tchip = q('.k-tool .tool-fchip');
    check(!!tchip && tchip.classList.contains('srv') && tchip.textContent.trim() === '预览', '工具卡标题行的小 chip：服务器会话也是「预览」（不再只是复制路径）');
    check(st.stat.length === 0 && st.fetches.length === 0, '渲染聊天记录时不查、不取任何服务器文件（点了才查）');
    // ---- 文本：加载中 → 内容；预览头：服务器标签、下载、浏览器打开、复制路径，没有 Finder / 默认应用
    st.hold = {}; st.hold.p = new Promise((r) => { st.hold.r = r; });
    await click(chips[0], 'notes.txt chip');
    check(!!pv() && !pv().hidden && /正在从服务器读取/.test(body().textContent) && !!body().querySelector('.spin'), '点击 chip → 面板立即显示「正在从服务器读取文件信息…」（远端慢时也有反馈）');
    check(st.stat.length === 1 && st.stat[0].gesture && st.stat[0].path.endsWith('/srvfile/stat'), '只在点击时查一次（POST …/srvfile/stat）');
    st.hold.r(); st.hold = null;
    await waitFor(() => body().querySelector('.pv-gutter'));
    check(!!body().querySelector('.pv-gutter') && /world 世界/.test(body().textContent), '文本：行号 + 内容（UTF-8）');
    check(!!bar().querySelector('.pv-srv-tag') && !!barBtn('下载') && !!barBtn('在浏览器中打开') && !!barBtn('复制路径') && !barBtn('在 Finder 中显示') && !barBtn('用默认应用打开'),
      '预览头：「服务器」标签 + 刷新 / 下载 / 在浏览器中打开 / 复制路径（没有 Finder、默认应用）：' + [...bar().querySelectorAll('.pv-act')].map((b) => b.textContent.trim()).join(' / '));
    check(!bar().querySelector('.pv-enc'), 'UTF-8 文件不标编码');
    // ---- 下载：只在点击时；交给 <a download>（这里截住）
    await click(barBtn('下载'), '下载');
    await waitFor(() => st.dl.length === 1);
    check(st.dl.length === 1 && st.dl[0].url === '/fxsrv/s/notes.txt?download=1' && st.dl[0].name === 'notes.txt' && st.toasts.some((x) => /开始下载 notes\.txt/.test(x.t)),
      '「下载」：先重新查一次再交给浏览器 / 原生窗口下载（download=1 → Content-Disposition: attachment）');
    // ---- GB18030
    await click(chip('gbk.txt'), 'gbk.txt chip');
    await waitFor(() => /中文测试/.test(body().textContent));
    const enc = bar().querySelector('.pv-enc');
    check(/中文测试，GBK 编码。/.test(body().textContent) && /第二行/.test(body().textContent) && !!enc && enc.textContent === 'GB18030',
      'GBK 文件：UTF-8 解不了 → 按 GB18030 解码，预览头标「GB18030」');
    _setGbSupport(false);
    const nf = st.fetches.length;
    await click(barBtn('刷新'), '刷新（模拟不支持 TextDecoder(gb18030)）');
    await waitFor(() => st.fetches.length >= nf + 2 && /中文测试/.test(body().textContent));
    check(st.fetches.slice(nf).some((u) => u.includes('ccr_utf8=1')) && /中文测试/.test(body().textContent) && (bar().querySelector('.pv-enc') || {}).textContent === 'GB18030',
      '不支持 TextDecoder(gb18030) 时改用 ?ccr_utf8=1 让后端转码，仍标「GB18030」');
    _setGbSupport(null);
    // ---- 二进制内容（扩展名像文本）不当文本；超过预览上限；只取开头
    await click(chip('blob.txt'), 'blob.txt chip');
    await waitFor(() => /二进制/.test(body().textContent));
    check(/看起来是二进制文件/.test(body().textContent) && !body().querySelector('.pv-gutter') && !!body().querySelector('.pv-dl'), '扩展名是 .txt 但内容是二进制：不显示乱码，给「下载到本机」');
    const nf2 = st.fetches.length;
    await click(chip('big.log'), 'big.log chip');
    await waitFor(() => /预览上限/.test(body().textContent));
    check(/预览上限 20MB/.test(body().textContent) && !!body().querySelector('.pv-dl') && st.fetches.length === nf2, '超过预览上限（25MB）：说明 + 下载按钮，不去取内容');
    await click(chip('part.log'), 'part.log chip');
    await waitFor(() => body().querySelector('.pv-gutter'));
    check(st.fetches.some((u) => u.startsWith('/fxsrv/s/part.log?ccr_head=' + SRV_TEXT_HEAD)) && /只显示前/.test(body().textContent), '大文本只取开头 2MB（ccr_head），提示「只显示前 2MB」');
    await click(chip('data.bin'), 'data.bin chip');
    await waitFor(() => /二进制文件/.test(body().textContent));
    check(/二进制文件，无法在这里预览/.test(body().textContent) && !!body().querySelector('.pv-dl'), '二进制文件：给「下载到本机」');
    const ndl = st.dl.length;
    await click(chip('huge.bin'), 'huge.bin chip');
    await waitFor(() => /预览上限/.test(body().textContent));
    check(!body().querySelector('.pv-dl') && /scp/.test(body().textContent) && !barBtn('下载'), '超过下载上限（600MB）：不给下载，提示用 scp / rsync');
    check(st.dl.length === ndl, '没有点下载就不会下载');
    // ---- 图片 / 网页
    await click(chip('pic.png'), 'pic.png chip');
    await waitFor(() => body().querySelector('.pv-img .cimg.ready'));
    const im = body().querySelector('.pv-img img');
    check(!!im && im.src.startsWith('blob:') && !!body().querySelector('.cimg.ready'), '图片：取回后用 objectURL 显示（失败能说明原因，而不是一张破图）');
    await click(chip('site/index.html'), 'index.html chip');
    await waitFor(() => body().querySelector('iframe'));
    const fr = body().querySelector('iframe');
    check(!!fr && fr.getAttribute('sandbox') === 'allow-scripts allow-forms allow-popups allow-modals' && fr.getAttribute('src') === '/static/dev/viz_demo.html',
      'HTML：先确认读得到，再用同一个 /preview 地址放进沙箱 iframe（无 allow-same-origin）');
    // ---- 失败与重试：链路断开（stat 503）、读取超时（504）、不存在、目录
    st.failStat = 1;
    await click(chip('slow.txt'), 'slow.txt chip（链路断开）');
    await waitFor(() => body().querySelector('.pv-srv-err'));
    const retry = body().querySelector('.pv-retry');
    check(/与计算节点的连接已断开/.test(body().textContent) && !!retry, '链路断开：显示原因（与计算节点的连接已断开…）+「重试」');
    await click(retry, '重试');
    await waitFor(() => /slow ok/.test(body().textContent));
    check(/slow ok/.test(body().textContent), '点「重试」后正常显示');
    await click(chip('flaky.txt'), 'flaky.txt chip（读取超时）');
    await waitFor(() => body().querySelector('.pv-srv-err'));
    check(/读取失败：远端操作超时/.test(body().textContent) && !!body().querySelector('.pv-retry'), '读取超时：「读取失败：远端操作超时（file_read）」+「重试」');
    await click(body().querySelector('.pv-retry'), '重试');
    await waitFor(() => /flaky ok/.test(body().textContent));
    check(/flaky ok/.test(body().textContent), '重试成功');
    await click(chip('nope.txt'), 'nope.txt chip');
    await waitFor(() => /文件不存在/.test(body().textContent));
    check(/文件不存在/.test(body().textContent) && !!body().querySelector('.pv-retry') && [...body().querySelectorAll('button')].some((b) => /复制路径/.test(b.textContent)), '不存在：说明 + 重试 + 复制路径');
    vizPanel.open(s, { kind: 'file', path: SCWD + '/src' });
    await waitFor(() => /目录/.test(body().textContent));
    check(/这是服务器上的目录/.test(body().textContent), '目录：说明（不列目录）');
    // ---- 「改动」栏：服务器会话也有「打开文件」
    vizPanel.openChanges(s, { path: SCWD + '/notes.txt' });
    await raf(); await sleep(60);
    const open = [...pv().querySelectorAll('.chg-file.open .pv-act')].find((b) => /打开文件/.test(b.textContent));
    check(!!open, '「改动」栏：服务器会话的文件也有「打开文件」');
    if (open) {
      await click(open, '改动栏 · 打开文件');
      await waitFor(() => body().querySelector('.pv-gutter') && /world 世界/.test(body().textContent));
      check(/world 世界/.test(body().textContent) && pv().querySelector('.pv-tab.on .pv-tab-t').textContent === 'notes.txt', '「打开文件」→ 服务器文件标签');
    }
    // ---- 浅色 / 深色：编码标签、「服务器」标签、chip 文字对比度
    const th = window.ccrTheme;
    const prevPref = th.pref();
    await click(chip('gbk.txt'), 'gbk.txt chip');
    await waitFor(() => bar().querySelector('.pv-enc'));
    const ratios = [];
    // color-mix() 的计算值是 color(srgb r g b) 形式（0–1），rgbaOf 只认 rgb()/rgba()
    const rgbOf = (c) => { const m = String(c).match(/^color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)(?: \/ ([\d.]+))?\)$/); return m ? { r: m[1] * 255, g: m[2] * 255, b: m[3] * 255, a: m[4] != null ? +m[4] : 1 } : rgbaOf(c); };
    for (const mode of ['light', 'dark']) {
      th.set(mode, { persist: false }); await raf();
      const pageBg = rgbaOf(getComputedStyle(document.body).backgroundColor);
      const pvBg = over(rgbaOf(getComputedStyle(pv()).backgroundColor), pageBg);
      for (const el of [bar().querySelector('.pv-enc'), bar().querySelector('.pv-srv-tag'), chip('gbk.txt')]) {
        const cs = getComputedStyle(el);
        const bg = over(rgbOf(cs.backgroundColor), el.closest('.pv') ? pvBg : pageBg);
        ratios.push([mode, el.className.split(' ')[0], contrast(rgbOf(cs.color), bg)]);
      }
    }
    th.set(prevPref, { persist: false }); await raf();
    const worst = ratios.reduce((a, b) => (b[2] < a[2] ? b : a));
    check(ratios.every((r) => r[2] >= 4.5), '浅色 / 深色：编码标签、「服务器」标签、服务器 chip 文字对比度 ≥ 4.5（最低 ' + worst[2].toFixed(2) + ' ' + worst[0] + ' ' + worst[1] + '）');
    // ---- 本机会话的 GBK 文本也按 GB18030 显示
    vizPanel.closeAll();
    s.localOverride = true;
    vizPanel.open(s, { kind: 'file', path: SCWD + '/gbk.txt' });
    await waitFor(() => /中文测试/.test(body().textContent));
    check(/中文测试/.test(body().textContent) && (bar().querySelector('.pv-enc') || {}).textContent === 'GB18030' && !bar().querySelector('.pv-srv-tag') && !!barBtn('在 Finder 中显示'),
      '本机会话的 GBK 文本同样按 GB18030 显示并标出（本机按钮不变）');
    check(st.stat.some((x) => x.path.endsWith('/local/stat')) && !barBtn('下载'), '本机文件走 /local/stat，不显示「下载」');
    const dt = decodeText(Uint8Array.from([0xe4, 0xb8, 0xad, 0xe6]), { partial: true });
    check(dt.encoding === 'utf-8' && dt.text === '中', '只拿到开头时截在多字节字符中间仍按 UTF-8（不误判成 GB18030）');
    const bg = st.stat.filter((x) => !x.gesture);
    check(bg.every((x) => x.paths.length === 1), '服务器文件的查询只来自点击（' + (st.stat.length - bg.length) + ' 次）、直接打开标签与打开着的标签的轮询（' + bg.length + ' 次，每次只查这一个文件）');
  } finally {
    window.fetch = origFetch;
    _setGbSupport(null);
    _setDownloader(null);
    vizPanel.closeAll();
    s.localOverride = null;
    initViz({ api: vizApi, getConfig: () => ({}), toast: (t) => log('（toast）' + t), panel: vizPanel });
  }
  check(calls.length === nCalls, '服务器文件预览没有发出任何按键 / 操作请求');
}
