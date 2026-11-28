// 聊天宽度（设置弹窗「聊天宽度」；docs/SPEC_V2_TUI.md §7「聊天宽度」）：标准 800px（原来的 --col-w）/ 宽 / 很宽 / 全宽。
// 存 localStorage「ccr.chatw」，写成 <html data-chatw>（theme.js 在首帧前就写好，这里负责切换与跨标签页同步），
// CSS 按它改 --col-w：消息列（.chat-col）、输入框（.composer-inner：图片条、引用条、子代理托盘、快捷键提示都在里面）同宽同中线；
// 左侧小格子按新的留白重新测量（app.js / 回放页监听 'ccr:chatw'）。纯前端，不发请求。

import { h } from './dom.js';

export const CW_KEY = 'ccr.chatw';
export const CHAT_WIDTHS = [
  { id: 'std', name: '标准', px: 800, desc: '800px（默认）' },
  { id: 'wide', name: '宽', px: 1000, desc: '1000px' },
  { id: 'wider', name: '很宽', px: 1280, desc: '1280px' },
  { id: 'full', name: '全宽', px: null, desc: '占满窗口（左右各留 28px）' },
];

/** 任意值 → 合法的档位 id（缺省 / 非法 = 标准） */
export function normWidth(v) {
  return CHAT_WIDTHS.some((w) => w.id === v) ? v : 'std';
}

/** 档位 → --col-w 的取值（全宽 = none：max-width 不限制） */
export function colWidth(id) {
  const w = CHAT_WIDTHS.find((x) => x.id === normWidth(id));
  return w.px ? w.px + 'px' : 'none';
}

function store() { try { return window.localStorage; } catch (_) { return null; } }

export function loadWidth() {
  const ls = store();
  try { return normWidth(ls && ls.getItem(CW_KEY)); } catch (_) { return 'std'; }
}

/** 写 <html data-chatw> 并派发 'ccr:chatw'（标准档不写属性，与以前完全相同） */
export function applyWidth(id, { persist = true } = {}) {
  const v = normWidth(id);
  const root = document.documentElement;
  const prev = root.getAttribute('data-chatw') || 'std';
  if (v === 'std') root.removeAttribute('data-chatw'); else root.setAttribute('data-chatw', v);
  if (persist) {
    const ls = store();
    try { if (ls) { if (v === 'std') ls.removeItem(CW_KEY); else ls.setItem(CW_KEY, v); } } catch (_) { /* 隐私模式：只在本页生效 */ }
  }
  if (prev !== v) window.dispatchEvent(new CustomEvent('ccr:chatw', { detail: { width: v } }));
  return v;
}

export function currentWidth() { return normWidth(document.documentElement.getAttribute('data-chatw') || 'std'); }

// 用户在设置里切换时的包装（app.js：保持阅读位置——WebKit 没有滚动锚定，列宽一变文字重排，时间线会跳）
let wrapper = null;
export function setWidthWrapper(fn) { wrapper = typeof fn === 'function' ? fn : null; }

// 其它标签页改了宽度：跟着变
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('storage', (e) => { if (e.key === CW_KEY) applyWidth(e.newValue, { persist: false }); });
}

/** 设置弹窗里的一节：分段按钮（role=radiogroup），点了立即生效 */
export function chatWidthField({ persist = true } = {}) {
  const items = CHAT_WIDTHS.map((w) => {
    const b = h('button', { class: 'seg-item cw-item', type: 'button', role: 'radio', dataset: { cw: w.id }, title: w.name + '：' + w.desc }, w.name);
    b.addEventListener('click', () => {
      const run = () => applyWidth(w.id, { persist });
      if (wrapper) wrapper(run); else run();
      sync();
    });
    return b;
  });
  const seg = h('div', { class: 'seg cw-seg', role: 'radiogroup', 'aria-label': '聊天宽度' }, items);
  const sync = () => {
    const cur = currentWidth();
    for (const b of items) {
      const on = b.dataset.cw === cur;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', String(on));
    }
  };
  sync();
  const el = h('div', { class: 'field cw-field' }, h('label', { class: 'field-label' }, '聊天宽度'), seg,
    h('div', { class: 'field-hint' }, '消息列与输入框的最大宽度（标准 800px · 宽 1000px · 很宽 1280px · 全宽）；只保存在这台设备的这个窗口里。'));
  return { el, sync };
}
