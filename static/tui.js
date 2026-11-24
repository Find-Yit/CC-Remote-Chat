// 交互式 TUI 相关的界面组件：模型/effort 弹窗、终端面板、头部小部件。
// 只消费后端的屏幕解析结构（§4.1）与 model_list；所有请求都经调用方传入的 actions 发出，
// 且只在用户点击时发出（红线 §0.5）。
import { h, icon, spinner } from './dom.js';
import { modal } from './dialogs.js';
import { EFFORT_LEVELS, currentDialog } from './render.js';

export const MODE_TEXT = {
  auto: '自动', acceptEdits: '自动接受编辑', plan: '计划模式', bypassPermissions: '跳过权限', manual: '手动审批', dontAsk: '不询问',
};

export function modeText(m) { return m ? (MODE_TEXT[m] ? `${MODE_TEXT[m]} · ${m}` : String(m)) : '—'; }

// 保存为默认 = claude 自己写它所在机器的 ~/.claude/settings.json（本机会话 = 本机；本程序不写）
const saveNote = (s) => '会写入' + (s && s.meta && s.meta.jobid === 'local' ? '本机' : '服务器') + ' ~/.claude/settings.json，和你在终端里按 Enter 一样';

function effortSeg(levels, value, onPick, defaultLevel) {
  const wrap = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'effort' });
  const render = (v) => {
    wrap.replaceChildren(...levels.map((l) => {
      const b = h('button', { class: 'seg-item' + (l === v ? ' on' : ''), type: 'button', role: 'radio', 'aria-checked': String(l === v) },
        l, l === defaultLevel ? h('small', { class: 'muted' }, ' 默认') : null);
      b.addEventListener('click', () => { onPick(l); render(l); });
      return b;
    }));
  };
  render(value);
  return wrap;
}

// ====================== 模型选择弹窗 ======================

/**
 * 打开模型选择弹窗。
 * opts: { s, actions:{applyModel(s,{index,effort,scope}), cancelDialog(s), keys(s,keys)}, pending:Promise|null }
 *   pending：由“切换模型”按钮打开时，POST model_picker 的 Promise（返回 {rows,cursor,effort}）。
 * 返回控制器 {update(), close(silent), isOpen()}。
 */
export function openModelModal({ s, actions, pending = null, onClosed }) {
  let full = null;           // 完整列表 {rows,cursor,effort}
  let loading = !!pending;
  let loadErr = '';
  let sel = null;            // 用户选中的行 index
  let eff = null;            // 用户选中的 effort
  let initEff = null;
  let busy = false;
  let closedSilently = false;
  let open = true;

  const list = h('div', { class: 'model-list', role: 'listbox', 'aria-label': '模型列表' });
  const note = h('div', { class: 'muted small-text model-note' });
  const effWrap = h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Effort'));
  const effSlot = h('div');
  effWrap.appendChild(effSlot);
  const err = h('div', { class: 'field-err' });
  const status = h('span', { class: 'muted small-text' });
  const primary = h('button', { class: 'btn primary', type: 'button' }, '仅本会话（s）');
  const save = h('button', { class: 'btn', type: 'button' }, '保存为默认（Enter）');
  const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消（Esc）');
  const scrollUp = h('button', { class: 'btn ghost small', type: 'button', title: '在终端面板里按 ↑（滚动可见列表）' }, icon('up'), '光标上移（看更多）');
  const scrollDown = h('button', { class: 'btn ghost small', type: 'button', title: '在终端面板里按 ↓（滚动可见列表）' }, icon('down'), '光标下移（看更多）');
  const scrollRow = h('div', { class: 'row model-scroll', hidden: true }, scrollUp, scrollDown, h('span', { class: 'muted small-text' }, '列表来自终端可见窗口；移动终端里的光标可滚出更多项（最终以你在这里选中的为准）'));
  const body = h('div', { class: 'form-grid model-modal' },
    list, scrollRow, note, effWrap,
    h('div', { class: 'save-note' }, icon('shield'), h('span', null, '“保存为默认（Enter）”', saveNote(s), '。“仅本会话（s）”不写任何设置文件。')),
    err);

  const dlg = modal({
    title: '切换模型', body, wide: true, actions: [status, cancel, save, primary],
    onClose: () => {
      open = false;
      if (!closedSilently) {
        // 用户关闭（X / Esc / 取消）= 在终端里按 Esc 关闭面板
        if (loading && pending) pending.then(() => actions.cancelDialog(s)).catch(() => {});
        else if (currentDialog(s) && currentDialog(s).kind === 'model') Promise.resolve(actions.cancelDialog(s)).catch(() => {});
      }
      if (onClosed) onClosed(closedSilently);
    },
  });

  const rows = () => {
    if (full && Array.isArray(full.rows) && full.rows.length) return full.rows.map(normRow);
    return [...s.screenModelRows.values()].sort((a, b) => a.n - b.n).map(normRow);
  };

  function normRow(r) {
    const index = typeof r.index === 'number' ? r.index : (typeof r.n === 'number' ? r.n - 1 : 0);
    return { index, n: r.n ?? index + 1, name: String(r.name ?? ''), desc: String(r.desc ?? ''), current: !!r.current, selected: !!r.selected };
  }

  function effortInfo() {
    if (full && full.effort) return full.effort;
    const d = currentDialog(s);
    return d && d.kind === 'model' ? d.effort || null : null;
  }

  function render() {
    const rs = rows();
    if (sel == null) {
      const cur = full && typeof full.cursor === 'number' ? rs.find((r) => r.index === full.cursor) : null;
      const pick = cur || rs.find((r) => r.selected) || rs.find((r) => r.current);
      if (pick) sel = pick.index;
    }
    const e = effortInfo();
    if (eff == null && e && e.level) { eff = String(e.level); initEff = eff; }
    if (loading && !rs.length) list.replaceChildren(h('div', { class: 'muted pad' }, spinner(), ' 正在读取完整模型列表…'));
    else if (!rs.length) list.replaceChildren(h('div', { class: 'muted pad' }, loadErr || '没有读到模型列表'));
    else {
      list.replaceChildren(...rs.map((r) => {
        const b = h('button', { class: 'model-row' + (r.index === sel ? ' on' : ''), type: 'button', role: 'option', 'aria-selected': String(r.index === sel) },
          h('span', { class: 'model-radio' }),
          h('span', { class: 'model-main' },
            h('span', { class: 'model-name' }, r.name, r.current ? h('span', { class: 'model-cur', title: '当前模型' }, ' ✔ 当前') : null),
            r.desc ? h('span', { class: 'model-desc' }, r.desc) : null));
        b.addEventListener('click', () => { sel = r.index; render(); });
        return b;
      }));
    }
    const d = currentDialog(s);
    const partial = !full && d && d.kind === 'model';
    scrollRow.hidden = !partial;
    if (partial) {
      scrollUp.disabled = busy || !d.more_above;
      scrollDown.disabled = busy || !d.more_below;
    }
    note.textContent = loading ? '正在读取完整列表（在终端面板里逐行滚动收集）…'
      : loadErr ? loadErr
        : partial ? `可见窗口${d.more_above ? `上方还有 ${d.more_above} 项` : ''}${d.more_above && d.more_below ? '，' : ''}${d.more_below ? `下方还有 ${d.more_below} 项` : ''}`
          : '';
    note.hidden = !note.textContent;
    const def = e && e.is_default && e.level ? String(e.level) : null;
    effSlot.replaceChildren(effortSeg(EFFORT_LEVELS, eff, (l) => { eff = l; }, def),
      e && e.label ? h('div', { class: 'muted small-text' }, '终端显示：' + String(e.label)) : null);
    const can = !busy && !loading && sel != null && !!(d && d.kind === 'model');
    primary.disabled = !can;
    save.disabled = !can;
    status.replaceChildren(...(busy ? [spinner(), ' 正在切换…'] : loading ? [spinner(), ' 读取中…'] : !d || d.kind !== 'model' ? ['终端面板尚未打开'] : []));
  }

  const apply = (scope) => {
    if (sel == null) return;
    busy = true;
    err.textContent = '';
    render();
    const effort = eff && eff !== initEff ? eff : null;
    Promise.resolve(actions.applyModel(s, { index: sel, effort, scope })).then((r) => {
      busy = false;
      ctrl.close(true);
      if (actions.onApplied) actions.onApplied(s, r, scope);
    }, (e) => {
      busy = false;
      err.textContent = '切换失败：' + ((e && e.message) || e);
      render();
    });
  };
  primary.addEventListener('click', () => apply('session'));
  save.addEventListener('click', () => apply('save'));
  cancel.addEventListener('click', () => dlg.close());
  scrollUp.addEventListener('click', () => Promise.resolve(actions.keys(s, ['Up'])).catch((e) => { err.textContent = e.message; }));
  scrollDown.addEventListener('click', () => Promise.resolve(actions.keys(s, ['Down'])).catch((e) => { err.textContent = e.message; }));

  if (pending) {
    pending.then((r) => {
      loading = false;
      if (r && Array.isArray(r.rows)) full = { rows: r.rows, cursor: r.cursor, effort: r.effort || null };
      if (open) render();
    }, (e) => {
      loading = false;
      loadErr = '读取模型列表失败：' + ((e && e.message) || e);
      if (open) render();
    });
  }
  // 用户自己打开的 /model 面板：只用屏幕上看到的行（旧的完整列表里“当前”标记可能已过期）

  const ctrl = {
    update() { if (open) render(); },
    setFull(d) { if (d && Array.isArray(d.rows)) { full = d; loading = false; if (open) render(); } },
    close(silent) { if (!open) return; closedSilently = !!silent; dlg.close(); },
    isOpen() { return open; },
    get busy() { return busy || loading; },
  };
  render();
  primary.focus();
  return ctrl;
}

// ====================== effort 弹窗 ======================

/** actions.applyEffort(s, {level, scope}) */
export function openEffortModal({ s, actions, current }) {
  let lv = EFFORT_LEVELS.includes(current) ? current : 'medium';
  let busy = false;
  const err = h('div', { class: 'field-err' });
  const status = h('span', { class: 'muted small-text' });
  const primary = h('button', { class: 'btn primary', type: 'button' }, '仅本会话（s）');
  const save = h('button', { class: 'btn', type: 'button' }, '保存为默认（Enter）');
  const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消');
  const body = h('div', { class: 'form-grid' },
    h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'Effort（当前：' + (current || '未知') + '）'),
      effortSeg(EFFORT_LEVELS, lv, (l) => { lv = l; })),
    h('div', { class: 'muted small-text' }, '确认后会在终端输入 /effort，用 ←/→ 调到所选档位，再按 s 或 Enter。'),
    h('div', { class: 'save-note' }, icon('shield'), h('span', null, '“保存为默认（Enter）”', saveNote(s), '。')),
    err);
  const dlg = modal({ title: '调整 effort', body, actions: [status, cancel, save, primary] });
  const go = (scope) => {
    if (busy) return;
    busy = true;
    primary.disabled = save.disabled = true;
    status.replaceChildren(spinner(), ' 正在调整…');
    err.textContent = '';
    Promise.resolve(actions.applyEffort(s, { level: lv, scope })).then((r) => {
      dlg.close();
      if (actions.onApplied) actions.onApplied(s, r, scope);
    }, (e) => {
      busy = false;
      primary.disabled = save.disabled = false;
      status.replaceChildren();
      err.textContent = '调整失败：' + ((e && e.message) || e);
    });
  };
  primary.addEventListener('click', () => go('session'));
  save.addEventListener('click', () => go('save'));
  cancel.addEventListener('click', () => dlg.close());
  primary.focus();
  return dlg;
}

// ====================== 终端面板 ======================

export const TERM_KEYS = [
  ['↑', 'Up'], ['↓', 'Down'], ['←', 'Left'], ['→', 'Right'], ['Enter', 'Enter'], ['Esc', 'Escape'], ['Tab', 'Tab'],
  ['Shift+Tab', 'BTab'], ['1', '1'], ['2', '2'], ['3', '3'], ['4', '4'], ['s', 's'], ['y', 'y'], ['n', 'n'], ['Ctrl+C', 'C-c'],
];

/** 终端面板：最新屏幕原文 + 键盘区（兜底） */
export class TermPanel {
  /** root: 容器元素；opts: {onKeys(keys) → Promise, onRefresh() → Promise, onClose()} */
  constructor(root, opts) {
    this.root = root;
    this.opts = opts;
    this.pre = h('pre', { class: 'term-screen', tabindex: '0', 'aria-label': '终端屏幕' });
    this.info = h('span', { class: 'muted small-text term-info' });
    this.err = h('span', { class: 'term-err' });
    const refresh = h('button', { class: 'icon-btn', type: 'button', title: '重新读取屏幕' }, icon('refresh'));
    refresh.addEventListener('click', () => this.refresh());
    const close = h('button', { class: 'icon-btn', type: 'button', title: '收起终端面板' }, icon('x'));
    close.addEventListener('click', () => opts.onClose && opts.onClose());
    this.keyBtns = TERM_KEYS.map(([label, key]) => {
      const b = h('button', { class: 'key', type: 'button', title: '发送按键 ' + key }, label);
      b.addEventListener('click', () => this.send([key]));
      return b;
    });
    root.replaceChildren(
      h('div', { class: 'term-head' }, icon('terminal'), h('span', { class: 'term-title' }, '终端'), this.info, h('span', { class: 'spacer' }), this.err, refresh, close),
      this.pre,
      h('div', { class: 'keypad term-keys' }, this.keyBtns,
        h('span', { class: 'muted small-text term-hint' }, '按键直接发送到 tmux 里的 claude，用于未识别的界面')));
    this.text = null;
    this.sid = null;
  }

  setSession(s) {
    const changed = (s && s.sid) !== this.sid;
    this.sid = s ? s.sid : null;
    if (changed) { this.text = null; this.err.textContent = ''; this.firstPaint = true; }
    this.update(s);
  }

  update(s) {
    const scr = s && s.screen;
    const raw = scr && typeof scr.text === 'string' ? scr.text : '';
    const text = raw.replace(/[ \t]+$/gm, '').replace(/\n+$/, '');
    if (text !== this.text) {
      const el = this.pre;
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
      el.textContent = text || (s ? '（还没有屏幕内容）' : '（未选择会话）');
      this.text = text;
      if (this.firstPaint || atBottom) {
        el.scrollTop = el.scrollHeight;
        if (el.clientHeight > 0) this.firstPaint = false; // 面板尚未显示时，等真正可见后再滚到底
      }
    } else if (this.firstPaint && this.pre.clientHeight > 0) {
      this.pre.scrollTop = this.pre.scrollHeight;
      this.firstPaint = false;
    }
    const dead = !s || s.readOnly || s.meta.state === 'exited' || s.meta.state === 'error';
    for (const b of this.keyBtns) b.disabled = dead;
    this.info.textContent = scr ? '更新于 ' + new Date(scr.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) + (scr.stale ? '（进程已结束）' : '') : '';
  }

  async refresh() {
    if (!this.opts.onRefresh) return;
    try { this.err.textContent = ''; await this.opts.onRefresh(); }
    catch (e) { this.err.textContent = '读取失败：' + e.message; }
  }

  async send(keys) {
    try { this.err.textContent = ''; await this.opts.onKeys(keys); }
    catch (e) { this.err.textContent = '发送失败：' + e.message; }
  }
}
