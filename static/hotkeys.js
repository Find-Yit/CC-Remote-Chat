// CLI 快捷键提示（Claude Code v2.1.330 实测）：
//   正在运行的 Bash 命令下方 "(ctrl+b ctrl+b (twice) to run in background)" → 「转到后台运行」
//   排队的消息下方 "⎿  Queued · ctrl+x ctrl+s to send now"              → 「立即发送排队消息」
// 数据来自屏幕解析 state.hints = {background, send_now, queued:[…], shells}（ccr/tui_parse.py）。
// 按钮与键盘和弦都只在**对应提示此刻出现、且没有对话框**时生效；按键由后端核对最新屏幕并带 guard 发送（§0.5）。
import { h, icon, truncate } from './dom.js';

export const CHORD_MS = 1500;   // 和弦两次按键的最大间隔

/** 会话当前屏幕的快捷键提示；没有屏幕 / 屏幕过期 / 只读 → null */
export function screenHints(s) {
  if (!s || s.readOnly || !s.screen || s.screen.stale) return null;
  const st = s.screen.state || {};
  const hn = st.hints;
  return hn && typeof hn === 'object' ? hn : null;
}

/** 输入框上方的提示条：排队中的消息 + 两个按钮 */
export class HintBar {
  constructor(el, { onBackground, onSendNow }) {
    this.el = el;
    this.onBackground = onBackground;
    this.onSendNow = onSendNow;
    this.key = '';
    this.busy = new Set();   // 正在发送的动作（防连点）
  }

  /** hints 为 null 或 blocked（有对话框 / 已退出）时只显示排队列表（若有）；按钮隐藏 */
  update(hints, { blocked = false } = {}) {
    const hn = hints || {};
    const queued = Array.isArray(hn.queued) ? hn.queued.filter((q) => typeof q === 'string') : [];
    const bg = !blocked && !!hn.background;
    const now = !blocked && !!hn.send_now;
    const key = JSON.stringify([queued, bg, now, [...this.busy]]);
    if (key === this.key) return;
    this.key = key;
    if (!queued.length && !bg && !now) { this.el.hidden = true; this.el.replaceChildren(); return; }
    const rows = queued.map((q) => h('div', { class: 'queued-row', title: q },
      h('span', { class: 'queued-tag' }, '排队中'), h('span', { class: 'queued-text' }, truncate(q.replace(/\s+/g, ' '), 160))));
    const acts = [];
    if (bg) {
      const b = h('button', { class: 'btn small hint-btn', type: 'button', disabled: this.busy.has('bg'),
        title: 'Claude Code 提示：ctrl+b ctrl+b (twice) to run in background\n把正在运行的命令转到后台继续跑（Claude 可以接着做别的）' },
      icon('down'), '转到后台运行', h('kbd', null, 'Ctrl+B Ctrl+B'));
      b.addEventListener('click', () => this.run('bg', this.onBackground));
      acts.push(b);
    }
    if (now) {
      const b = h('button', { class: 'btn small hint-btn', type: 'button', disabled: this.busy.has('now'),
        title: 'Claude Code 提示：ctrl+x ctrl+s to send now\n会中断当前这一轮生成，立即把排队的消息发给 Claude' },
      icon('send'), '立即发送排队消息', h('kbd', null, 'Ctrl+X Ctrl+S'));
      b.addEventListener('click', () => this.run('now', this.onSendNow));
      acts.push(b);
    }
    this.el.replaceChildren(...rows, acts.length ? h('div', { class: 'hint-acts' }, acts) : null);
    this.el.hidden = false;
  }

  async run(id, fn) {
    if (this.busy.has(id)) return;
    this.busy.add(id);
    this.key = '';
    try { await fn(); } finally { this.busy.delete(id); this.key = ''; }
  }
}

/**
 * 键盘和弦：Ctrl+B Ctrl+B（转到后台运行）、Ctrl+X Ctrl+S（立即发送排队消息），两次按键间隔 ≤ 1.5 秒。
 * 只在对应提示出现时拦截并 preventDefault；其余时候完全不碰（macOS 上这些 Ctrl 组合在 textarea 里是 emacs 光标键）。
 * getHints() → 当前会话可用的提示（有对话框 / 弹窗打开时应返回 null）。
 */
export function installChords({ getHints, onBackground, onSendNow }) {
  let armed = null;   // {key:'b'|'x', t}
  document.addEventListener('keydown', (e) => {
    if (!e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || e.isComposing || e.keyCode === 229) return;
    const k = String(e.key || '').toLowerCase();
    if (k !== 'b' && k !== 'x' && k !== 's') { armed = null; return; }
    const hn = getHints();
    const now = performance.now();
    const within = armed && now - armed.t <= CHORD_MS;
    if (k === 'b' && hn && hn.background) {
      e.preventDefault();
      if (within && armed.key === 'b') { armed = null; onBackground(); } else armed = { key: 'b', t: now };
      return;
    }
    if (k === 'x' && hn && hn.send_now) {
      e.preventDefault();
      armed = { key: 'x', t: now };
      return;
    }
    if (k === 's' && hn && hn.send_now && within && armed.key === 'x') {
      e.preventDefault();
      armed = null;
      onSendNow();
      return;
    }
    armed = null;
  }, true);
}
