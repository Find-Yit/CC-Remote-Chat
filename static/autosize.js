// 输入框自适应高度（不抖动版）。
//
// 旧做法是每次 input 先把 textarea 设成 height:auto 再读 scrollHeight：这一缩一撑会让输入框所在的 flex 列瞬间变矮，
// 时间线滚动容器的可视高度随之变大、scrollTop 被浏览器夹到新的最大值；撑回原高度后 scrollTop 却不会回来，
// 于是贴底时每次按键（两次按键之间有输出把时间线滚回底部时）内容都往下掉一截、右侧滚动条跟着跳。
// Chrome 的滚动锚定能掩盖一部分，WebKit（原生应用的 WKWebView）不支持滚动锚定，所以抖得明显。
//
// 现在的做法：
// - 用一个隐藏的“镜像”textarea（同一套 CSS、同样宽度、同样内容）量内容高度，真正的输入框从不被临时缩小；
// - 高度没变就不写 style（打字时绝大多数按键不改任何布局）；
// - 没到最大高度（40% 视口）时 overflow-y: hidden，到了才 auto，滚动条不会一闪；
// - 输入框宽度变化（窗口缩放、pageZoom、侧栏/抽屉开合、从隐藏变为显示）由 ResizeObserver 触发重新测量。
// 高度确实变化时（多/少一行、发送后清空、恢复草稿）由调用方在 onChange 里处理时间线（贴底就继续贴底）。

const MAX_RATIO = 0.4;   // 与 style.css 的 max-height: 40vh 一致

export class AutoSize {
  /** ta：textarea；onChange(height)：高度真正改变后调用（同步，在浏览器绘制之前） */
  constructor(ta, { onChange } = {}) {
    this.ta = ta;
    this.onChange = onChange || null;
    this.height = -1;      // 上次写入的像素高度（-1 = 还没写过）
    this.width = -1;       // 上次测量时的宽度
    this.mirror = null;
    this.retry = 0;
    if (typeof ResizeObserver === 'function') {
      // 只关心宽度：高度变化是我们自己写的，不能再触发测量（避免循环）
      this.ro = new ResizeObserver(() => {
        const w = this.measureWidth();
        if (!w) this.width = 0;               // 被隐藏：记下来，重新显示时（宽度从 0 变回来）补测
        else if (w !== this.width) this.update();
      });
      this.ro.observe(ta);
    }
    // 字体晚到会改变行高 / 换行
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => this.update(true)).catch(() => {});
  }

  /** 输入框的边框盒宽度（CSS px，可带小数）。用计算样式而不是 getBoundingClientRect：
   *  祖先有 CSS zoom 时后者是缩放后的视觉宽度，写回镜像的 style.width 会被再缩放一次；隐藏时返回 0 */
  measureWidth() {
    // 不显示（祖先 hidden / display:none）时计算样式给的是 "100%" 之类，不能当像素用
    if (!this.ta.isConnected || !this.ta.getClientRects().length) return 0;
    const cw = getComputedStyle(this.ta).width;
    const w = cw.endsWith('px') ? parseFloat(cw) : 0;
    return Number.isFinite(w) && w > 0 ? w : 0;
  }

  ensureMirror() {
    if (this.mirror && this.mirror.parentNode === this.ta.parentNode) return this.mirror;
    const m = document.createElement('textarea');
    m.setAttribute('aria-hidden', 'true');
    m.tabIndex = -1;
    m.readOnly = true;
    m.className = this.ta.className;
    m.rows = 1;
    // 与输入框同一个父元素 → 命中同一套 CSS（字体、行高、内边距、换行）；绝对定位 + 不可见，不参与布局
    const st = m.style;
    st.position = 'absolute';
    st.visibility = 'hidden';
    st.pointerEvents = 'none';
    st.left = '0';
    st.top = '0';
    st.zIndex = '-1';
    st.height = '0';
    st.minHeight = '0';
    st.maxHeight = 'none';
    st.overflow = 'hidden';
    st.boxSizing = 'border-box';
    this.ta.after(m);
    this.mirror = m;
    return m;
  }

  /** 光标（位置 pos）是否在第一个 / 最后一个“视觉行”（自动折行也算行；输入框 ↑/↓ 历史用，见 prompthist.js）。
   *  用同一个镜像量：从开头到光标所在词的末尾（拉丁字母的词可能整个被折到下一行）的高度，与一行的高度 / 全文的高度比较。
   *  输入框隐藏时只按换行符判断 */
  caretEdges(pos) {
    const v = this.ta.value;
    const before = v.slice(0, pos);
    const after = v.slice(pos);
    let first = !before.includes('\n');
    let last = !after.includes('\n');
    if (!first && !last) return { first, last };
    const w = this.measureWidth();
    if (!w) return { first, last };
    const m = this.ensureMirror();
    m.style.width = w + 'px';
    m.placeholder = '';
    const hOf = (t) => { m.value = t; return m.scrollHeight; };
    // 只把拉丁字母这类“词”的剩余部分算进来；中日韩文字可以在任意两字之间折行，遇到就停（否则整段中文都被当成一个词）
    const upTo = hOf(before + after.match(/^[^\s\u2E80-\uFFFF]*/)[0]);
    if (first) first = upTo <= hOf('');
    if (last) last = hOf(v) <= upTo;
    // 还原镜像（下次 update() 也会同步，这里还原是为了不留下与输入框不一致的中间状态）
    m.value = v;
    m.placeholder = v ? '' : this.ta.placeholder;
    return { first, last };
  }

  /** 按当前内容/宽度调整高度。force：即使宽度与高度都没变也重新测量并通知 */
  update(force = false) {
    const ta = this.ta;
    const w = this.measureWidth();
    // 输入框隐藏着（页面刚载入、恢复草稿时）量不准：保持原样，等它显示出来时由 ResizeObserver 补测
    if (!w) {
      this.width = 0;
      // 隐藏又在同一帧里显示出来时 ResizeObserver 看不到尺寸变化，下一帧再补测一次
      if (!this.retry) this.retry = requestAnimationFrame(() => { this.retry = 0; if (this.measureWidth()) this.update(); });
      return;
    }
    this.width = w;
    const m = this.ensureMirror();
    m.style.width = w + 'px';
    if (m.value !== ta.value) m.value = ta.value;
    const ph = ta.value ? '' : ta.placeholder;   // 空输入框连同占位文字一起量，与浏览器给真实输入框的排版一致
    if (m.placeholder !== ph) m.placeholder = ph;
    const content = m.scrollHeight;
    if (!content) return;   // 镜像没有排版（不该发生）：宁可不动，也不把输入框写成 0 高
    const max = Math.round(window.innerHeight * MAX_RATIO);
    const capped = content > max;
    const hgt = capped ? max : content;
    const ov = capped ? 'auto' : 'hidden';
    if (ta.style.overflowY !== ov) ta.style.overflowY = ov;
    if (!capped && ta.scrollTop) ta.scrollTop = 0;   // 不滚动的输入框里不应残留半行的内部偏移
    if (hgt === this.height && !force) return;
    const changed = hgt !== this.height;
    this.height = hgt;
    if (changed) ta.style.height = hgt + 'px';
    if (this.onChange) this.onChange(hgt);
  }
}
