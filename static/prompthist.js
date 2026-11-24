// 输入框 ↑/↓ 历史（SPEC §7「输入框」）：把这个会话里之前发过的文字消息放回输入框，行为参考终端里 claude 的 ↑/↓。
// - 来源 = 当前会话 transcript 里的用户文字消息（时间线上的用户气泡，按时间顺序）+ 本页刚发出、transcript 里还没出现的
//   （运行中排队的消息要等 claude 处理时才写进 transcript）。只在前端，不发任何请求、不按任何键。
// - ↑：光标在第一个“视觉行”（自动折行也算行）且没有选中文字 → 上一条，光标放在开头（连按 ↑ 继续往前）；
//   ↓：正在浏览历史且光标在最后一个视觉行 → 下一条，光标放在末尾；翻过最新一条 → 恢复开始浏览前输入框里的草稿。
// - 改过调出来的内容 → 改后的文字成为新草稿、退出浏览（再按 ↑ 从最新一条重新开始，↓ 能回到改后的文字），不会丢字。
// 纯逻辑（无 DOM）：promptsFromItems / mergePending / PromptNav；光标在哪一行由 AutoSize.caretEdges 量。

const STALE_AFTER = 3;    // 本页发出的消息之后 transcript 又来了这么多条仍没对上（被 CLI 改写等）→ 不再单独列出
const MAX_PENDING = 20;

/** 时间线条目（render.js 的 s.items）→ 用户发过的文字消息（按时间顺序）。
 *  跳过被截断的（放回去再发就不是原文了）和只有图片的；本地命令（/model 等）、系统注入消息不是 user 条目，本来就不在里面 */
export function promptsFromItems(items) {
  const out = [];
  for (const it of items || []) {
    if (!it || it.kind !== 'user' || it.truncated) continue;
    const t = typeof it.text === 'string' ? it.text.trim() : '';
    if (t) out.push(t);
  }
  return out;
}

/** 本页发出的消息要不要记成“待出现”：/ 命令、! shell 在 transcript 里不是用户消息，等不到，不记 */
export function isRecallable(text) {
  const t = String(text || '').trim();
  return !!t && !t.startsWith('/') && !t.startsWith('!');
}

/** 记一条本页发出的消息（pending 原地修改）。n = 发送时 transcript 里已有的消息条数 */
export function addPending(pending, text, n) {
  if (!isRecallable(text)) return;
  pending.push({ text: String(text).trim(), n: Math.max(0, n | 0) });
  if (pending.length > MAX_PENDING) pending.splice(0, pending.length - MAX_PENDING);
}

/** prompts：transcript 里的消息；pending：本页发出的 [{text, n}]。
 *  返回 {list, pending}：list = prompts + 仍未出现的 pending（相邻重复只留一条）；
 *  pending = 还要继续等的（已经在 transcript 里出现、或之后又来了 STALE_AFTER 条仍没对上的都去掉） */
export function mergePending(prompts, pending) {
  const keep = [];
  let from = 0;   // 两条一样的待出现消息要对上两次不同的出现
  for (const p of pending || []) {
    const start = Math.max(Math.min(p.n, prompts.length), from);
    const hit = prompts.indexOf(p.text, start);
    if (hit >= 0) { from = hit + 1; continue; }
    if (prompts.length - p.n >= STALE_AFTER) continue;
    keep.push(p);
  }
  const list = [];
  for (const t of prompts.concat(keep.map((p) => p.text))) if (t !== list[list.length - 1]) list.push(t);
  return { list, pending: keep };
}

/** 一个会话的浏览状态（挂在该会话的草稿上，切换会话再回来可以接着翻） */
export class PromptNav {
  constructor() { this.reset(); }

  reset() {
    this.list = null;     // 开始浏览时的快照（浏览期间新到的消息不打乱顺序）
    this.idx = -1;        // -1 = 没在浏览；否则 = list 里的下标
    this.saved = '';      // 开始浏览前输入框里的草稿
    this.shown = null;    // 我们放进输入框的文字
  }

  get active() { return this.idx >= 0; }

  /** 输入框内容不是我们放进去的了（用户改过 / 发送了 / 被别处改了）→ 退出浏览，当前内容就是新草稿 */
  sync(value) { if (this.idx >= 0 && value !== this.shown) this.reset(); }

  /** dir = -1 更早 / +1 更新。返回要放进输入框的文字；null = 不处理（交给浏览器默认的光标移动） */
  step(dir, value, getList) {
    this.sync(value);
    if (dir < 0) {
      let list = this.list;
      let i = this.idx;
      if (i < 0) {
        list = getList() || [];
        i = list.length;
        if (i && list[i - 1] === value) i--;   // 最新一条就是输入框里现在的文字：跳过，免得第一下 ↑ 看起来没反应
      }
      if (i <= 0) return null;                 // 已经是最早一条 / 没有历史
      if (this.idx < 0) { this.list = list; this.saved = value; }
      this.idx = i - 1;
    } else {
      if (this.idx < 0) return null;
      if (this.idx + 1 >= this.list.length) {  // 翻过最新一条：回到草稿
        const t = this.saved;
        this.reset();
        return t;
      }
      this.idx++;
    }
    this.shown = this.list[this.idx];
    return this.shown;
  }
}

/** 键盘事件是不是“纯” ↑/↓（不带修饰键、不在输入法组字中）。返回 -1 / +1 / 0 */
export function historyDir(e, composing) {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return 0;
  if (e.shiftKey || e.altKey || e.metaKey || e.ctrlKey) return 0;
  if (e.isComposing || composing || e.keyCode === 229) return 0;   // 中文输入法：↑/↓ 是选候选词
  return e.key === 'ArrowUp' ? -1 : 1;
}

/** 处理一次 ↑/↓：ta = 输入框，sizer = 它的 AutoSize（量光标在第几个视觉行），nav = PromptNav，getList() = 历史列表。
 *  返回放进输入框的文字（调用方负责 preventDefault、更新草稿、重新测高）；null = 不处理 */
export function recallInto(ta, dir, sizer, nav, getList) {
  if (!dir || ta.disabled || ta.readOnly) return null;
  const pos = ta.selectionStart;
  if (pos !== ta.selectionEnd) return null;   // 选中了文字：交给浏览器
  nav.sync(ta.value);
  if (dir > 0 && !nav.active) return null;    // 没在浏览时 ↓ 就是普通的光标移动
  const edge = sizer && typeof sizer.caretEdges === 'function' ? sizer.caretEdges(pos) : null;
  const first = edge ? edge.first : !ta.value.slice(0, pos).includes('\n');
  const last = edge ? edge.last : !ta.value.slice(pos).includes('\n');
  if (dir < 0 ? !first : !last) return null;  // 多行内容里先在行间移动光标
  const text = nav.step(dir, ta.value, getList);
  if (text == null) return null;
  ta.value = text;
  const at = dir < 0 ? 0 : text.length;
  ta.setSelectionRange(at, at);
  return text;
}
