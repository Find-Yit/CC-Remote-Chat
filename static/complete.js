// 输入框的 / 命令与 @ 文件补全（SPEC §7「输入框：/ 命令与 @ 文件补全」）。
// - `/`（只在输入框开头）：CLI 内置命令（slashcmds.js，按会话的 CLI 版本）+ 后端只读列出的自定义命令 / skills / 插件命令
//   （GET /api/sessions/<sid>/complete/commands）。选中 → 输入框里变成 `/名字 `，发送后仍由 claude 自己处理。
// - `@`（行首或空白之后）：会话 cwd 下的文件 / 文件夹（GET …/complete/files，只读、有上限），模糊匹配；选中文件 → 插入
//   `@相对路径 `（有空格的路径用 `@"…"`），选中文件夹 → `@文件夹/` 并继续列它里面的内容。路径由 claude 自己解析。
// - 键盘：↑/↓ 选择、Tab / Enter 插入、Esc 关闭（这个词接着往后打不再弹出；删改了前面的字、换了词再弹出）；输入法组字中一律不处理；
//   正好打完整个名字时 Enter 照常发送。列表打开时 ↑/↓ 不翻输入历史（prompthist.js），关闭后照常。
// - 列表按会话缓存（会话 + cwd；命令 10 分钟、文件 2 分钟，过期先用旧的再后台刷新），弹层右上角可手动刷新。
// 纯函数（detectTrigger / rankCommands / rankFiles / applyPick / atSafeTail …）+ CompleteCache + Completer（DOM）。
import { h, icon, spinner } from './dom.js';
import { builtinCommands, tableVersion } from './slashcmds.js';

export const TTL = { commands: 10 * 60 * 1000, files: 2 * 60 * 1000 };
export const FILE_LIMIT = 60;
export const ERR_RETRY_MS = 15000;
const SRC_LABEL = { builtin: '内置', project: '项目', user: '用户', plugin: '插件' };

/** 光标处的补全触发：{kind:'slash'|'at', query, start, end, quoted?}；没有 → null。
 *  slash：输入框开头到光标是 `/xxx`（不含空白和 /，避免 /usr/bin 这类路径）；
 *  at：光标前的词以 @ 开头、@ 在行首或空白之后（与 CLI 的 `(?:^|\s)@(\S*)$` 一致；邮箱 a@b 不触发）；`@"…` 是带空格的路径 */
export function detectTrigger(value, caret) {
  const v = String(value || '');
  const pos = Math.max(0, Math.min(caret == null ? v.length : caret, v.length));
  const before = v.slice(0, pos);
  let m = /^\/([^\s/]*)$/.exec(before);
  if (m) return { kind: 'slash', query: m[1], start: 0, end: pos };
  m = /(^|\s)@"([^"\n]*)$/.exec(before);
  if (m) return { kind: 'at', query: m[2], start: m.index + m[1].length, end: pos, quoted: true };
  m = /(^|\s)@((?:[^\s\\"]|\\ )*)$/.exec(before);
  if (m) return { kind: 'at', query: m[2].replace(/\\ /g, ' '), start: m.index + m[1].length, end: pos, quoted: false };
  return null;
}

/** 子序列模糊匹配：全部字符按顺序出现 → 分数（连续、词首加分，间隔扣分）；否则 -1。都按小写比较 */
export function fuzzyScore(text, q) {
  if (!q) return 0;
  const t = text.toLowerCase();
  let score = 0, ti = 0, prev = -2;
  for (const ch of q.toLowerCase()) {
    const k = t.indexOf(ch, ti);
    if (k < 0) return -1;
    score += 10;
    if (k === prev + 1) score += 15;
    if (k === 0 || '/-_.: '.includes(t[k - 1])) score += 20;
    score -= Math.min(k - ti, 20);
    prev = k;
    ti = k + 1;
  }
  return score;
}

function nameScore(name, q) {
  const n = name.toLowerCase();
  if (n === q) return 10000;
  if (n.startsWith(q)) return 8000 - n.length;
  if (n.split(/[:-]/).some((seg) => seg && seg.startsWith(q))) return 6500 - n.length;
  const k = n.indexOf(q);
  if (k >= 0) return 5000 - k;
  const f = fuzzyScore(n, q);
  return f >= 0 ? 2000 + f : -1;
}

/** 命令按查询排序：名字 / 别名 精确 > 前缀 > 某一段的前缀 > 包含 > 模糊；查询 ≥ 2 个字时说明里包含也算（排最后）。
 *  空查询 = 原顺序全部。返回 [{...命令, via: 命中的别名|undefined, exact}] */
export function rankCommands(cmds, query) {
  const q = String(query || '').toLowerCase();
  const out = [];
  (cmds || []).forEach((c, i) => {
    if (!c || !c.name) return;
    if (!q) { out.push({ c, s: 0, i, via: undefined }); return; }
    let best = nameScore(c.name, q), via;
    for (const a of c.aliases || []) {
      const s = nameScore(a, q) - 1;   // 同分时正式名字优先
      if (s > best) { best = s; via = a; }
    }
    if (best < 0 && q.length >= 2 && ((c.desc || '').toLowerCase().includes(q) || (c.en || '').toLowerCase().includes(q))) best = 500;
    if (best >= 0) out.push({ c, s: best, i, via });
  });
  out.sort((a, b) => b.s - a.s || a.i - b.i);
  return out.map((r) => ({ ...r.c, via: r.via, exact: !!q && (r.c.name.toLowerCase() === q) }));
}

/** 内置 + 自定义合并（自定义在前；同名只留自定义那一个） */
export function mergeCommands(custom, builtins) {
  const seen = new Set();
  const out = [];
  for (const c of (custom || []).concat(builtins || [])) {
    if (!c || typeof c.name !== 'string' || !c.name || seen.has(c.name)) continue;
    seen.add(c.name);
    out.push(c);
  }
  return out;
}

const depthOf = (p) => (p.replace(/\/$/, '').match(/\//g) || []).length;
const baseOf = (p) => { const x = p.replace(/\/$/, ''); return x.slice(x.lastIndexOf('/') + 1); };

/** 文件按查询排序（entries = 后端的相对路径，文件夹以 / 结尾，广度优先顺序）。
 *  空查询 = 前 limit 项（浅的在前）；以 / 结尾且正好是某个文件夹 = 先列它的直接子项；
 *  查询里有 / → 按整条路径匹配；否则主要按文件名匹配（再看路径、模糊）。返回 [{path, dir}] */
export function rankFiles(entries, query, limit = FILE_LIMIT) {
  const list = Array.isArray(entries) ? entries.filter((e) => typeof e === 'string' && e) : [];
  const q = String(query || '').toLowerCase();
  const wrap = (p) => ({ path: p, dir: p.endsWith('/') });
  if (!q) return list.slice(0, limit).map(wrap);
  const scored = [];
  const hasSlash = q.includes('/');
  list.forEach((p, i) => {
    const pl = p.toLowerCase();
    const d = depthOf(p);
    let s = -1;
    if (hasSlash) {
      if (q.endsWith('/') && pl.startsWith(q) && pl !== q && !pl.slice(q.length).replace(/\/$/, '').includes('/')) s = 9500 - i / 1e5;   // 文件夹的直接子项
      else if (pl === q) s = 9000;
      else if (pl.startsWith(q)) s = 8000 - d * 10 - pl.length / 100;
      else {
        const k = pl.indexOf(q);
        if (k >= 0) s = 6000 - k - d;
        else { const f = fuzzyScore(pl, q); if (f >= 0) s = 2000 + f - d; }
      }
    } else {
      const n = baseOf(pl);
      if (n === q) s = 10000 - d;
      else if (n.startsWith(q)) s = 8000 - n.length - d * 5;
      else {
        const k = n.indexOf(q);
        if (k >= 0) s = 6000 - k - d * 5;
        else if (pl.includes(q)) s = 4000 - d;
        else {
          const f1 = fuzzyScore(n, q);
          if (f1 >= 0) s = 3000 + f1 - d;
          else { const f2 = fuzzyScore(pl, q); if (f2 >= 0) s = 1000 + f2 - d; }
        }
      }
    }
    if (s >= 0) scored.push([s, i, p]);
  });
  scored.sort((a, b) => b[0] - a[0] || a[2].length - b[2].length || a[1] - b[1]);
  return scored.slice(0, limit).map((x) => wrap(x[2]));
}

/** 文件路径 → 输入框里的 @ 引用（claude 自己解析）：有空白 → `@"…"`（路径里还有 " 时用 `\ ` 转义空格）。
 *  open=true：文件夹，引号先不闭合，方便继续往里补全 */
export function atToken(path, open = false) {
  const p = String(path || '');
  if (!/\s/.test(p)) return '@' + p;
  if (!p.includes('"')) return '@"' + p + (open ? '' : '"');
  return '@' + p.replace(/ /g, '\\ ');
}

/** 选中一项后的输入框内容：{value, caret, keepOpen}。
 *  slash：开头第一个词换成 `/名字 `，后面已有的参数保留；at：从 @ 到光标后这个词的结尾换成引用（文件后面补一个空格；
 *  文件夹不补，继续列它里面的内容） */
export function applyPick(value, trig, item) {
  const v = String(value || '');
  if (!trig || !item) return { value: v, caret: v.length, keepOpen: false };
  if (trig.kind === 'slash') {
    const rest = v.slice(trig.end).replace(/^\S*/, '').replace(/^[ \t]+/, '');
    const head = '/' + item.name + ' ';
    return { value: head + rest, caret: head.length, keepOpen: false };
  }
  let tail = v.slice(trig.end);
  if (trig.quoted) {
    const q = tail.indexOf('"');
    const nl = tail.search(/\s/);
    tail = q >= 0 && (nl < 0 || q < nl) ? tail.slice(q + 1) : tail.replace(/^\S*/, '');
  } else tail = tail.replace(/^\S*/, '');
  // 文件夹：继续往里补全；但选的正好是已经打完的这个文件夹 = 就引用它本身（补空格、关闭）
  const dir = !!item.dir && item.path !== trig.query;
  const tok = atToken(item.path, dir);
  const sep = dir || /^\s/.test(tail) ? '' : ' ';
  const head = v.slice(0, trig.start) + tok + sep;
  const caret = head.length + (!dir && sep === '' && /^[ \t]/.test(tail) ? 1 : 0);
  return { value: head + tail, caret, keepOpen: dir };
}

/** 发送前：消息以一个 @ 引用结尾时补一个空格——否则终端里 claude 的 @ 文件补全列表会停在那个词上，
 *  回车可能被补全吃掉（实测 2.1.332：粘贴 `看看 @a.txt` 后出现 `+ a.txt` 补全，末尾加空格后消失） */
export function atSafeTail(text) {
  const t = String(text ?? '');
  return /(^|\s)@(?:"[^"\n]*"?|(?:[^\s\\]|\\ )+)$/.test(t) ? t + ' ' : t;
}

// ====================== 缓存 ======================

/** 会话级缓存：key = sid + cwd（/cd 换了目录就是另一份）。过期的先照用、后台刷新；同一份同时只请求一次 */
export class CompleteCache {
  constructor(fetcher, { ttl = TTL, now = () => Date.now(), onChange = null } = {}) {
    this.fetcher = fetcher;
    this.ttl = ttl;
    this.now = now;
    this.onChange = onChange;
    this.map = new Map();   // key -> {data, at, err, promise}
  }

  static key(sess, kind) { return kind + '\n' + sess.sid + '\n' + (sess.cwd || ''); }

  peek(sess, kind) { return sess ? this.map.get(CompleteCache.key(sess, kind)) || null : null; }

  /** 需要时发请求（没有 / 过期 / force）；返回该条目 */
  ensure(sess, kind, force = false) {
    const k = CompleteCache.key(sess, kind);
    let e = this.map.get(k);
    if (!e) { e = { data: null, at: 0, err: '', promise: null }; this.map.set(k, e); }
    // 读取失败的 15 秒后才自动重试（服务器连不上时不会每打一个字就 ssh 一次）；刷新按钮随时可以重读
    const stale = !e.at || this.now() - e.at > (e.err ? ERR_RETRY_MS : (this.ttl[kind] || 60000));
    if (!e.promise && (force || stale)) {
      if (force) e.err = '';
      e.promise = Promise.resolve().then(() => this.fetcher(sess, kind)).then((d) => {
        e.data = d || {};
        e.err = '';
        e.at = this.now();
      }, (err) => {
        e.err = (err && err.message) || String(err);
        e.at = this.now();
      }).finally(() => {
        e.promise = null;
        if (this.onChange) this.onChange(sess, kind);
      });
    }
    return e;
  }

  /** 会话删除 / 关闭：丢掉它的全部缓存 */
  drop(sid) {
    for (const k of [...this.map.keys()]) if (k.split('\n')[1] === sid) this.map.delete(k);
  }
}

// ====================== 弹层 ======================

function srcText(c) {
  const base = SRC_LABEL[c.src] || '';
  if (c.src === 'plugin') return '插件' + (c.plugin ? ' ' + c.plugin : '');
  return base + (c.kind === 'skill' ? ' skill' : '');
}

/** 输入框补全：挂在 #slash-pop 上。opts:
 *  ta, pop：输入框与弹层容器；cache：CompleteCache；session()：当前会话 {sid, cwd, version, local} 或 null；
 *  composing()：是否在输入法组字中；onApply(value)：选中后输入框内容变了（调用方更新草稿、测高） */
export class Completer {
  constructor({ ta, pop, cache, session, composing = () => false, onApply = () => {} }) {
    this.ta = ta;
    this.pop = pop;
    this.cache = cache;
    this.session = session;
    this.composing = composing;
    this.onApply = onApply;
    this.trig = null;
    this.items = [];
    this.index = 0;
    this.lastQuery = null;
    this.dismissed = null;   // {key:'slash:0'|'at:12', query}：Esc 关掉的那一次；同一个词接着往后打不再弹出，改了前面的字再弹出
    this.sess = null;
    pop.setAttribute('role', 'listbox');
    pop.addEventListener('mousedown', (e) => e.preventDefault());   // 点弹层不让输入框失焦
  }

  get isOpen() { return !this.pop.hidden; }

  close() {
    this.pop.hidden = true;
    this.trig = null;
    this.items = [];
    this.index = 0;
    this.lastQuery = null;
    this.ta.removeAttribute('aria-activedescendant');
    this.ta.setAttribute('aria-expanded', 'false');
  }

  /** 输入 / 光标变化后调用 */
  update() {
    const ta = this.ta;
    const sess = this.session();
    if (!sess || ta.disabled || ta.readOnly || ta.selectionStart !== ta.selectionEnd) { this.close(); return; }
    if (this.composing()) return;   // 组字中：保持原样，组字结束（compositionend 后的 input）再更新
    const trig = detectTrigger(ta.value, ta.selectionStart);
    if (!trig) { this.dismissed = null; this.close(); return; }
    const dkey = trig.kind + ':' + trig.start;
    const dm = this.dismissed;
    if (dm && dm.key === dkey && trig.query.startsWith(dm.query)) { this.close(); return; }
    this.dismissed = null;
    this.sess = sess;
    const qkey = dkey + ':' + trig.query;
    if (qkey !== this.lastQuery) this.index = 0;
    this.lastQuery = qkey;
    this.trig = trig;
    this.cache.ensure(sess, trig.kind === 'slash' ? 'commands' : 'files');
    this.compute();
    this.render();
  }

  /** 缓存有了新数据：打开着就重算 */
  refresh() { if (this.isOpen && this.trig) { this.compute(); this.render(); } }

  compute() {
    const sess = this.sess, trig = this.trig;
    if (trig.kind === 'slash') {
      const e = this.cache.peek(sess, 'commands');
      const custom = e && e.data && Array.isArray(e.data.commands) ? e.data.commands : [];
      this.items = rankCommands(mergeCommands(custom, builtinCommands(sess.version)), trig.query).map((c) => ({ type: 'cmd', ...c }));
    } else {
      const e = this.cache.peek(sess, 'files');
      const entries = e && e.data && Array.isArray(e.data.entries) ? e.data.entries : [];
      this.items = rankFiles(entries, trig.query).map((f) => ({ type: 'file', ...f }));
    }
    if (this.index >= this.items.length) this.index = Math.max(0, this.items.length - 1);
  }

  /** keydown：处理了返回 true（调用方不再继续处理这个键） */
  onKey(e) {
    if (!this.isOpen) return false;
    if (e.isComposing || e.keyCode === 229 || this.composing()) return false;   // 输入法：↑/↓/回车是选候选词
    if (e.altKey || e.metaKey || e.ctrlKey) return false;
    const n = this.items.length;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      if (this.trig) this.dismissed = { key: this.trig.kind + ':' + this.trig.start, query: this.trig.query };
      this.close();
      return true;
    }
    if (!n) return false;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (e.shiftKey) return false;
      e.preventDefault();
      this.index = (this.index + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
      this.render();
      return true;
    }
    if (e.key === 'PageDown' || e.key === 'PageUp') {
      e.preventDefault();
      this.index = Math.max(0, Math.min(n - 1, this.index + (e.key === 'PageDown' ? 8 : -8)));
      this.render();
      return true;
    }
    if ((e.key === 'Tab' || e.key === 'Enter') && !e.shiftKey) {
      const it = this.items[this.index];
      // 已经完整打出了这个名字：Enter 照常发送（Tab 仍然插入，补上空格）
      if (e.key === 'Enter' && it && this.isExact(it)) return false;
      e.preventDefault();
      this.pick(this.index);
      return true;
    }
    return false;
  }

  isExact(it) {
    const q = this.trig ? this.trig.query : '';
    if (!q) return false;
    if (it.type === 'cmd') return it.name === q;
    return !it.dir && it.path === q;
  }

  pick(i) {
    const it = this.items[i];
    if (!it || !this.trig) return;
    const r = applyPick(this.ta.value, this.trig, it);
    this.ta.value = r.value;
    this.ta.setSelectionRange(r.caret, r.caret);
    this.close();
    this.onApply(r.value);
    if (r.keepOpen) this.update();
  }

  render() {
    const pop = this.pop;
    const trig = this.trig;
    if (!trig) { this.close(); return; }
    const sess = this.sess;
    const slash = trig.kind === 'slash';
    const kind = slash ? 'commands' : 'files';
    const e = this.cache.peek(sess, kind) || {};
    const kids = [];
    // 标题行：哪一类、数量、来源说明、刷新
    let meta = '';
    if (slash) {
      const tv = tableVersion(sess.version);
      const custom = e.data && Array.isArray(e.data.commands) ? e.data.commands.length : 0;
      meta = '内置按 CLI ' + tv.version + (sess.version && !tv.exact ? '（本会话 ' + sess.version + '）' : '') + (e.data ? ' · 自定义 ' + custom + ' 个' : '');
    } else {
      const root = e.data && typeof e.data.root === 'string' ? e.data.root : (sess.cwd || '');
      const cnt = e.data && Array.isArray(e.data.entries) ? e.data.entries.length : 0;
      meta = (root ? baseOf(root) || root : '') + (e.data ? ' · ' + cnt + ' 项' + (e.data.truncated ? '（已截断）' : '') : '');
    }
    const rf = h('button', { class: 'cmp-refresh', type: 'button', title: slash ? '重新读取自定义命令 / skills' : '重新读取文件列表' }, icon('refresh'));
    rf.addEventListener('click', () => { this.cache.ensure(sess, kind, true); this.render(); });
    kids.push(h('div', { class: 'cmp-head' },
      h('span', { class: 'cmp-title' }, slash ? '命令' : '文件'),
      h('span', { class: 'cmp-meta', title: slash ? '内置命令是按 CLI 版本整理的静态表；自定义命令 / skills / 插件命令从磁盘只读列出' : (e.data && e.data.root) || '' }, meta),
      h('span', { class: 'spacer' }), rf));
    if (e.promise && !e.data) kids.push(h('div', { class: 'cmp-status' }, spinner(), slash ? '正在读取自定义命令…' : '正在读取文件列表…'));
    else if (e.err) kids.push(h('div', { class: 'cmp-status err' }, (slash ? '读取自定义命令失败：' : '读取文件列表失败：') + e.err + (slash ? '（下面只有内置命令）' : '')));
    if (slash && e.data && Array.isArray(e.data.notes)) for (const n of e.data.notes) kids.push(h('div', { class: 'cmp-status' }, String(n)));
    const list = h('div', { class: 'cmp-list' });
    this.items.forEach((it, k) => {
      const row = it.type === 'cmd' ? this.cmdRow(it) : this.fileRow(it);
      row.id = 'cmp-opt-' + k;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', k === this.index ? 'true' : 'false');
      if (k === this.index) row.classList.add('active');
      row.addEventListener('mousedown', (ev) => { ev.preventDefault(); this.pick(k); });
      list.appendChild(row);
    });
    if (!this.items.length && !(e.promise && !e.data)) {
      list.appendChild(h('div', { class: 'cmp-empty' }, slash ? '没有匹配的命令（仍可直接发送，由 claude 自己处理）' : (e.data ? '没有匹配的文件' : '')));
    }
    kids.push(list);
    kids.push(h('div', { class: 'cmp-foot' }, '↑↓ 选择 · Tab / Enter 插入 · Esc 关闭'));
    pop.replaceChildren(...kids);
    pop.hidden = false;
    pop.dataset.kind = trig.kind;
    this.ta.setAttribute('aria-expanded', 'true');
    this.ta.setAttribute('aria-controls', pop.id || '');
    const act = list.children[this.index];
    if (act && act.id) {
      this.ta.setAttribute('aria-activedescendant', act.id);
      if (typeof act.scrollIntoView === 'function') act.scrollIntoView({ block: 'nearest' });
    } else this.ta.removeAttribute('aria-activedescendant');
  }

  cmdRow(c) {
    const tip = [c.en, c.link ? '软链接（不跟随软链接，没有读取说明）' : '', c.hint ? '参数：' + c.hint : ''].filter(Boolean).join('\n');
    return h('div', { class: 'cmp-row cmp-cmd src-' + (c.src || 'builtin'), title: tip || null },
      h('span', { class: 'cmp-name' }, '/' + c.name),
      c.via ? h('span', { class: 'cmp-alias' }, '别名 ' + c.via) : null,
      c.hint ? h('span', { class: 'cmp-hint' }, c.hint) : null,
      h('span', { class: 'cmp-desc' }, c.link ? '（软链接，未读取说明）' : (c.desc || '')),
      h('span', { class: 'cmp-src' }, srcText(c)));
  }

  fileRow(f) {
    const p = f.path.replace(/\/$/, '');
    const k = p.lastIndexOf('/');
    return h('div', { class: 'cmp-row cmp-file' + (f.dir ? ' is-dir' : ''), title: f.path },
      icon(f.dir ? 'folder' : 'file', 'cmp-ic'),
      h('span', { class: 'cmp-path' },
        k >= 0 ? h('span', { class: 'cmp-dirpart' }, p.slice(0, k + 1)) : null,
        h('span', { class: 'cmp-base' }, p.slice(k + 1) + (f.dir ? '/' : ''))));
  }
}
