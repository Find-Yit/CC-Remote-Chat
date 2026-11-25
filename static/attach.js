// 本机会话发图片（SPEC §4.2「图片」、§7「输入框」）：与用户在终端里的操作完全等价，本程序不读写剪贴板、不另存文件。
// - ⌘V 粘贴截图：输入框的 paste 事件里有图片 → POST /image/paste，后端在终端里按 Ctrl+V，由 claude 自己读剪贴板；
//   有文字时文字照常粘进我们的输入框（只拦图片）。
// - 拖入图片：原生窗口（tools/macapp/main.swift）拿到被拖入文件的路径，以 `ccr-native-drag` / `ccr-native-drop`
//   事件交给页面 → 图片 POST /image/paths（后端按终端拖放的规则转义后粘进 claude 输入框，claude 自己变成 [Image #N]）；
//   非图片文件 = 终端拖放：转义后的路径插到我们的输入框光标处。浏览器里拿不到路径，只提示用 ⌘V。
// - 输入框上方的图片条：显示 claude 输入框里此刻有哪些 [Image #N]（来自屏幕解析 state.input.images），发送时一起发出。
//   每张一个小缩略图（⌘V 的 = 浏览器给的图片，只在本页内存里；拖入的 = 后端只读通道 image/file/<token>）：点了用灯箱放大；
//   × = 只从 claude 的输入框里删掉这一张（后端把光标挪到它后面按退格，见 SessionManager.remove_image）。
// 纯函数可在 node 里测（tests/test_attach.py）；DOM 部分（ImageTray / DropOverlay）在回放自测页第 1e 节。
import { h, icon, spinner } from './dom.js';

export const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
export const MAX_DROP_PATHS = 10;
// 与 ccr/attach.py 的 _UNSAFE_RE 相同：安全字符原样保留，其余 ASCII 字符前加 "\"（终端.app / iTerm2 拖放的写法）；非 ASCII 不转义
const UNSAFE_RE = /([^A-Za-z0-9_./+,:@%=\-\u0080-\u{10ffff}])/gu;
const CTRL_RE = /[\x00-\x1f\x7f]/;

/** paste / drop 事件的 DataTransfer：{ images: 图片文件数, files: 文件总数, text: 有没有纯文字, file: 第一个图片 File|null } */
export function transferInfo(dt) {
  const out = { images: 0, files: 0, text: false, file: null };
  if (!dt) return out;
  const items = dt.items ? Array.from(dt.items) : [];
  for (const it of items) {
    if (!it) continue;
    if (it.kind === 'file') {
      out.files++;
      if (/^image\//i.test(it.type || '')) {
        out.images++;
        if (!out.file && typeof it.getAsFile === 'function') out.file = it.getAsFile() || null;
      }
    } else if (it.kind === 'string' && /^text\/plain\b/i.test(it.type || '')) out.text = true;
  }
  if (!items.length && dt.files) {   // 旧实现没有 items：退回 files
    for (const f of Array.from(dt.files)) {
      out.files++;
      if (f && /^image\//i.test(f.type || '')) { out.images++; if (!out.file) out.file = f; }
    }
  }
  if (!out.text && typeof dt.getData === 'function') {
    try { out.text = !!dt.getData('text/plain'); } catch (e) { /* dragover 时读不了内容 */ }
  }
  return out;
}

/** DOM 拖动是否带文件（dragenter / dragover 时只能看 types） */
export function dragHasFiles(dt) {
  if (!dt || !dt.types) return false;
  return Array.from(dt.types).includes('Files');
}

export function isImagePath(p) {
  if (typeof p !== 'string') return false;
  const l = p.toLowerCase();
  return IMAGE_EXTS.some((e) => l.endsWith(e));
}

/** 终端拖放式转义：/a/my shot (1).png → /a/my\ shot\ \(1\).png */
export function escapeDropPath(p) { return String(p).replace(UNSAFE_RE, '\\$1'); }

/** 原生窗口交来的路径 → { images: [图片路径], others: [其它路径], bad: 不合格的个数 }（绝对路径、无控制字符） */
export function splitDropPaths(paths) {
  const out = { images: [], others: [], bad: 0 };
  for (const p of Array.isArray(paths) ? paths : []) {
    if (typeof p !== 'string' || !p.startsWith('/') || p.length > 4096 || CTRL_RE.test(p)) { out.bad++; continue; }
    (isImagePath(p) ? out.images : out.others).push(p);
  }
  return out;
}

/** 把（非图片）路径按终端拖放的样子插到输入框的选区：路径之间、前后用空格隔开。返回 { value, caret } */
export function insertPaths(value, start, end, paths) {
  const v = String(value || '');
  const a = Math.max(0, Math.min(start == null ? v.length : start, v.length));
  const b = Math.max(a, Math.min(end == null ? a : end, v.length));
  const before = v.slice(0, a);
  const after = v.slice(b);
  let ins = paths.map(escapeDropPath).join(' ');
  if (before && !/\s$/.test(before)) ins = ' ' + ins;
  ins += after && /^\s/.test(after) ? '' : ' ';
  return { value: before + ins + after, caret: a + ins.length };
}

/** 屏幕解析结果里 claude 输入框的图片编号（[Image #N] 的 N）；输入框不可见 / 没有 → [] */
export function inputImages(st) {
  const inp = st && st.input;
  if (!inp || !inp.visible || !Array.isArray(inp.images)) return [];
  return inp.images.filter((n) => Number.isInteger(n) && n >= 0);
}

/** claude 输入框里除了图片占位还有没有别的文字 */
export function inputHasOther(st) {
  const inp = st && st.input;
  if (!inp || !inp.visible) return false;
  return !!String(inp.text || '').replace(/\[Image #\d+\]/g, ' ').trim();
}

/**
 * 能不能往这个会话发图片：null = 可以；否则返回中文原因（toast 用）。
 * sess = { local, dead, dialog, linkDown, remote(手机远程口) }
 */
export function imageBlocked(sess, how) {
  if (!sess) return '请先选择一个会话';
  if (sess.remote) return how === 'drop' ? '手机上拖入的文件在手机上，Mac 上的 claude 读不到' : '手机上粘贴的图片在手机上，Mac 上的 claude 读不到；请在 Mac 上粘贴截图';
  if (!sess.local) return how === 'drop' ? '服务器会话暂不支持拖入文件：文件在这台 Mac 上，远端的 claude 读不到'
    : '服务器会话暂不支持直接发图片：远端的 claude 读不到这台 Mac 的剪贴板（文字已照常粘贴）';
  if (sess.dead) return '会话已退出，请先点击“重连”';
  if (sess.dialog) return '请先处理上方对话框';
  if (sess.linkDown) return '与本机 agent 的连接已断开，请稍后再试';
  return null;
}

/**
 * 拖动经过窗口时覆盖层的文字：{ ok, title, sub }。
 * native = 原生窗口交来的拖动（知道路径）；paths = 原生拖动的路径（没有 = DOM 拖动，拿不到路径）
 */
export function dropHint({ native, paths, sess }) {
  if (!native) {
    return { ok: false, title: '这里拿不到文件路径', sub: '截图请在输入框里按 ⌘V 粘贴（截屏时加按 Control 会直接存进剪贴板）' };
  }
  const sp = splitDropPaths(paths);
  const why = imageBlocked(sess, 'drop');
  if (why) return { ok: false, title: '不能放在这里', sub: why };
  if (!sp.images.length && !sp.others.length) return { ok: false, title: '不能放在这里', sub: '没有可用的文件路径' };
  if (sp.images.length > MAX_DROP_PATHS) return { ok: false, title: '图片太多', sub: '一次最多拖入 ' + MAX_DROP_PATHS + ' 张图片' };
  const parts = [];
  if (sp.images.length) parts.push(sp.images.length + ' 张图片放进 claude 的输入框（和拖进终端一样）');
  if (sp.others.length) parts.push(sp.others.length + ' 个文件以路径插入输入框');
  return { ok: true, title: '松开即可', sub: parts.join('；') };
}

/**
 * 图片条的数据（纯函数）：null = 不显示。
 * images：claude 输入框里的图片编号；pending：{ kind:'paste'|'paths'|'clear'|'remove', n } | null（remove 时 n = 正在删的编号）；
 * other：输入框里还有别的文字；thumbs：Map(N -> { url, name, kind:'paste'|'drop', loading, failed })；
 * blocked：不能操作的原因（已退出 / 对话框…）；removable：能不能逐张删（只有本机会话，默认 true）
 */
export function trayModel({ images, pending, other, thumbs, blocked, removable = true }) {
  const ids = Array.isArray(images) ? images : [];
  if (!ids.length && !pending) return null;
  const chips = ids.map((n) => {
    const t = thumbs && thumbs.get ? thumbs.get(n) : null;
    const url = (t && t.url) || '';
    return {
      n, url, name: (t && t.name) || '', kind: (t && t.kind) || '',
      loading: !url && !!(t && t.loading), failed: !url && !!(t && t.failed),
      removing: !!(pending && pending.kind === 'remove' && pending.n === n),
    };
  });
  let note;
  if (pending) note = pending.kind === 'paste' ? '正在让 claude 读取剪贴板里的图片…'
    : pending.kind === 'clear' ? '正在清除 claude 输入框里的图片…'
      : pending.kind === 'remove' ? '正在从 claude 的输入框里删掉 [Image #' + pending.n + ']…' : '正在把图片放进 claude 的输入框…';
  else if (other) note = '终端输入框里还有其它文字：发送时会先确认清空（图片也会一起清掉）';
  else note = '在 claude 的输入框里，发送时和文字一起发出';
  const canRemove = !!ids.length && !pending && !blocked && !!removable;
  return {
    chips, note, pending: !!pending, warn: !pending && !!other, canRemove,
    canClear: !!ids.length && !other && !pending && !blocked,
    key: JSON.stringify([chips, note, !!blocked]),
  };
}

/** 图片条上的图片 → 灯箱的描述列表（只有能显示的：⌘V 的内存缩略图、拖入且读到了的）与点中那张的下标 */
export function lightboxList(model, n, extra) {
  const list = [];
  let index = 0;
  for (const c of (model && model.chips) || []) {
    if (!c.url) continue;
    if (c.n === n) index = list.length;
    list.push({ url: c.url, name: (c.name ? c.name + ' · ' : '') + '[Image #' + c.n + ']', ...(extra ? extra(c) : {}) });
  }
  return { list, index };
}

function chipTitle(c) {
  const src = c.kind === 'paste' ? '剪贴板里的图片' : c.name || '';
  let t = '[Image #' + c.n + ']' + (src ? ' · ' + src : '') + '（在 claude 的输入框里）';
  if (c.url) t += '，点击放大';
  else if (c.loading) t += '，缩略图加载中…';
  else if (c.failed) t += '，没能读到这个文件，不显示缩略图';
  else if (c.kind !== 'drop') t += '，页面刷新后不再有缩略图（图片仍在）';
  return t;
}

/** 输入框（composer-box）顶部的图片条。update(model) 只在内容变化时重建 */
export class ImageTray {
  constructor({ onClear, onRemove, onOpen } = {}) {
    this.onClear = onClear || (() => {});
    this.onRemove = onRemove || (() => {});
    this.onOpen = onOpen || (() => {});
    this.el = h('div', { class: 'img-tray', role: 'group', 'aria-label': 'claude 输入框里的图片', hidden: true });
    this.key = '';
    this.model = null;
  }

  update(model) {
    const key = model ? model.key + (model.canRemove ? '|x' : '') : '';
    this.model = model;
    if (key === this.key) return false;
    this.key = key;
    if (!model) { this.el.hidden = true; this.el.replaceChildren(); return true; }
    this.el.hidden = false;
    this.el.classList.toggle('warn', model.warn);
    const chips = model.chips.map((c) => {
      const title = chipTitle(c);
      const inner = c.url ? h('img', { src: c.url, alt: '', draggable: 'false' })
        : c.loading ? h('span', { class: 'img-chip-ph' }, spinner()) : h('span', { class: 'img-chip-ph' }, icon('image'));
      // 有图才是按钮（点了用灯箱放大）；没有缩略图时只是占位
      const thumb = c.url ? h('button', { class: 'img-thumb', type: 'button', title, 'aria-label': '放大查看 [Image #' + c.n + ']' }, inner)
        : h('span', { class: 'img-thumb', title }, inner);
      if (c.url) thumb.addEventListener('click', (e) => { e.stopPropagation(); this.onOpen(c.n); });
      const kids = [thumb, h('span', { class: 'img-chip-n', 'aria-hidden': 'true' }, '#' + c.n)];
      if (model.canRemove) {
        const x = h('button', { class: 'img-x', type: 'button', title: '从 claude 的输入框里删掉这一张（其它图片和文字不动）',
          'aria-label': '删除 [Image #' + c.n + ']' }, icon('x'));
        x.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); this.onRemove(c.n); });
        kids.push(x);
      }
      return h('span', { class: 'img-chip' + (c.removing ? ' removing' : '') + (c.url ? ' has-img' : ''), 'data-n': String(c.n) }, ...kids);
    });
    const kids = [h('div', { class: 'img-chips' }, chips)];
    kids.push(h('span', { class: 'img-note' }, model.pending ? spinner() : null, model.note));
    if (model.canClear) {
      const b = h('button', { class: 'img-clear', type: 'button', title: '在终端里按 Ctrl+U 清掉 claude 输入框里的这些图片' }, '清除');
      b.addEventListener('click', () => this.onClear());
      kids.push(b);
    }
    this.el.replaceChildren(...kids);
    return true;
  }
}

/** 拖动经过时的覆盖层（盖住聊天区与输入框，不接收指针） */
export class DropOverlay {
  constructor() {
    this.el = h('div', { class: 'drop-overlay', hidden: true, 'aria-hidden': 'true' });
    this.key = '';
  }

  show(hint) {
    const key = JSON.stringify(hint);
    this.el.hidden = false;
    if (key === this.key) return;
    this.key = key;
    this.el.classList.toggle('bad', !hint.ok);
    this.el.replaceChildren(h('div', { class: 'drop-card' },
      icon(hint.ok ? 'image' : 'ban'), h('div', { class: 'drop-title' }, hint.title), h('div', { class: 'drop-sub' }, hint.sub)));
  }

  hide() { this.el.hidden = true; this.key = ''; }
}
