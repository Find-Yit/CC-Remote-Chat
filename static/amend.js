// 权限卡片的「附说明…」（CLI 2.1.332 权限框的 "Tab to amend"，SPEC §7「审批时附带说明」）：
// 第一项 Yes / 最后一项 No 旁边一个小按钮 → 卡片里展开一个输入框（我们的 UI）→ 确认后由后端在终端里
// 选中该项、按 Tab、粘贴说明、核对、回车（每一步都带 guard，见 ccr/tuiact.py）。说明随审批一起发给 Claude：
// Yes = 照常执行，并告诉 Claude 接下来怎么做；No = 不执行，并告诉 Claude 该怎么改（这一轮不会被中断）。
// 纯函数 + DOM，只在用户点击时调用 onSubmit；app 与回放页共用。
import { h, icon } from './dom.js';

export const AMEND_MAX = 2000;
export const AMEND_PLACEHOLDERS = { Yes: 'and tell Claude what to do next', No: 'and tell Claude what to do differently' };
const SAID_RE = /To tell you how to proceed, the user said:\n([\s\S]+)$/;

/** 能附带说明的选项 index（与 ccr/tui_parse.py 的 amendable 一致：第一项正好是 "Yes"、最后一项正好是 "No"）。
 *  终端里已经在输入说明（d.amend）时不提供（请在终端面板里处理）。 */
export function amendIndexes(d) {
  if (!d || d.kind !== 'permission' || d.amend) return [];
  const opts = Array.isArray(d.options) ? d.options : [];
  if (Array.isArray(d.amendable)) return d.amendable.filter((i) => Number.isInteger(i) && i >= 0 && i < opts.length);
  const out = [];
  if (opts.length && String(opts[0].label) === 'Yes') out.push(0);
  if (opts.length > 1 && String(opts[opts.length - 1].label) === 'No') out.push(opts.length - 1);
  return out;
}

export function amendBase(d, index) {
  const opts = (d && Array.isArray(d.options)) ? d.options : [];
  return index === 0 ? 'Yes' : index === opts.length - 1 ? 'No' : '';
}

/** 说明文字：换行 / 制表符换成空格（终端里的说明是一行），首尾空白去掉 */
export function cleanAmend(text) { return String(text == null ? '' : text).replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/ {2,}/g, ' ').trim(); }

/** 不能提交的原因（'' = 可以） */
export function amendError(text) {
  const t = cleanAmend(text);
  if (!t) return '请先写说明';
  if (t.length > AMEND_MAX) return `说明太长（最多 ${AMEND_MAX} 字）`;
  if (Object.values(AMEND_PLACEHOLDERS).includes(t)) return '说明不能和终端里的占位文字完全相同';
  return '';
}

/** 被拒绝的工具结果里用户附带的说明（CLI：拒绝文字末尾 "To tell you how to proceed, the user said:\n<说明>"）；没有 → '' */
export function rejectNote(content) {
  let t = content;
  if (Array.isArray(content)) t = content.map((b) => (b && b.type === 'text' ? String(b.text || '') : '')).join('\n');
  const m = typeof t === 'string' ? t.match(SAID_RE) : null;
  return m ? m[1].trim() : '';
}

/** 用户气泡上的标记：批准 / 拒绝时附带的说明（null = 不是） */
export function amendTag(item) {
  if (!item || (item.amend !== 'yes' && item.amend !== 'no')) return null;
  const yes = item.amend === 'yes';
  return h('div', { class: 'user-tag amend-tag ' + (yes ? 'yes' : 'no'),
    title: yes ? '在权限确认框上批准时附带的说明（终端里的 Tab to amend）：工具照常执行，Claude 随后读到这段话'
      : '在权限确认框上拒绝时附带的说明（终端里的 Tab to amend）：工具没有执行，Claude 读到这段话后接着回复' },
  icon('shield'), yes ? '批准时附带的说明' : '拒绝时附带的说明');
}

/**
 * 权限卡片里的附说明控件。ui = s.dialogUi（amendIndex / amendText 跨卡片重建保留）；onSubmit(index, text) 只在用户点击 /
 * 回车时调用。返回 { opts(buttons) → 选项区（可附带说明的项旁边多一个按钮）, box（输入区，没有展开时隐藏）, note（终端里已经
 * 在输入说明时的提示）}。
 */
export function amendControls(d, ui, onSubmit) {
  const idx = amendIndexes(d);
  const opts = Array.isArray(d.options) ? d.options : [];
  const editing = d && d.kind === 'permission' && d.amend && typeof d.amend === 'object' ? d.amend : null;
  const note = editing ? h('div', { class: 'dlg-amend-note' }, icon('edit'),
    `终端里正在给「${editing.base}」写说明${editing.text ? '：' + editing.text : '（还没输入）'}。可以在终端面板里继续；Tab 收起，Esc 会拒绝本次操作。`) : null;
  if (!idx.length) return { opts: (buttons) => h('div', { class: 'dlg-opts' }, buttons), box: null, note };
  if (ui.amendIndex != null && !idx.includes(ui.amendIndex)) ui.amendIndex = null;
  const box = h('div', { class: 'dlg-amend', hidden: ui.amendIndex == null });
  const label = h('div', { class: 'dlg-amend-head' });
  const input = h('input', { class: 'input dlg-amend-input', type: 'text', name: 'amend', maxlength: String(AMEND_MAX),
    autocomplete: 'off', spellcheck: 'false', 'aria-label': '附带的说明' });
  input.value = ui.amendText || '';
  const err = h('span', { class: 'dlg-amend-err' });
  const submit = h('button', { class: 'btn small dlg-act dlg-amend-go', type: 'button' });
  const fold = h('button', { class: 'btn ghost small dlg-act', type: 'button' }, '收起');
  const hint = h('span', { class: 'muted small-text dlg-amend-how' });
  const fill = () => {
    const i = ui.amendIndex;
    if (i == null) return;
    const yes = amendBase(d, i) === 'Yes';
    const o = opts.find((x) => x && x.index === i) || {};
    label.replaceChildren(icon('edit'), `随「${o.n != null ? o.n + '. ' : ''}${o.label || ''}」附带说明`);
    input.placeholder = yes ? '告诉 Claude 接下来怎么做（例如：执行完再跑一遍测试）' : '告诉 Claude 应该怎么改（例如：别动 /tmp，改在项目目录里建）';
    submit.replaceChildren(yes ? '批准并附说明' : '拒绝并附说明');
    submit.classList.toggle('primary', yes);
    submit.classList.toggle('danger', !yes);
    hint.textContent = yes ? '= 在终端里选中「Yes」、按 Tab、输入说明并回车：照常执行，Claude 随后读到这段说明'
      : '= 在终端里选中「No」、按 Tab、输入说明并回车：不执行，Claude 读到说明后接着回复（本轮不中断）';
  };
  const go = () => {
    const e = amendError(input.value);
    err.textContent = e;
    if (e || ui.amendIndex == null) { input.focus(); return; }
    onSubmit(ui.amendIndex, cleanAmend(input.value));
  };
  const close = () => { ui.amendIndex = null; box.hidden = true; err.textContent = ''; };
  input.addEventListener('input', () => { ui.amendText = input.value; if (err.textContent) err.textContent = ''; });
  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); go(); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }   // 只收起我们的输入框，不碰终端
  });
  submit.addEventListener('click', go);
  fold.addEventListener('click', close);
  box.append(label, input, h('div', { class: 'card-actions' }, submit, fold, err), hint);
  fill();
  const open = (i) => {
    ui.amendIndex = i;
    fill();
    box.hidden = false;
    err.textContent = '';
    input.focus();
  };
  return {
    opts(buttons) {
      return h('div', { class: 'dlg-opts' }, buttons.map((b) => {
        const o = opts.find((x) => x && String(x.index) === (b.dataset && b.dataset.optIndex));
        if (!o || !idx.includes(o.index)) return b;
        const yes = amendBase(d, o.index) === 'Yes';
        const btn = h('button', { class: 'btn ghost small dlg-act dlg-amend-btn', type: 'button', dataset: { amendIndex: String(o.index) },
          title: yes ? '批准，并附带一段说明告诉 Claude 接下来怎么做（终端里的 Tab to amend）'
            : '拒绝，并附带一段说明告诉 Claude 应该怎么改（终端里的 Tab to amend）' }, icon('edit'), '附说明…');
        btn.addEventListener('click', () => open(o.index));
        return h('div', { class: 'dlg-opt-row' }, b, btn);
      }));
    },
    box,
    note,
  };
}
