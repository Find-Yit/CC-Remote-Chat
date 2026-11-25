// 对话框 / 弹出层 / 提示条（全部受控 DOM，基于原生 <dialog>）
import { h, icon, spinner, basename } from './dom.js';
import { apiKeySelect, SWITCH_COST_HINT } from './apikeys.js';
import { initialPerm, savePerm, PERM_SKIP } from './launchprefs.js';

export const JOBID_RE = /^\d{1,12}(_\d{1,8})?$/;
// 运行位置：本机 = 特殊 jobid "local"（后端 remote.LOCAL_JOBID）；其余 = 服务器上的 Slurm 作业
export const LOCAL_JOBID = 'local';
export const isLocalJob = (jobid) => jobid === LOCAL_JOBID;
export const isLocalMeta = (m) => !!m && m.jobid === LOCAL_JOBID;
const TARGET_KEY = 'ccr.target';   // 新建会话对话框记住上次选的运行位置（'server' | 'local'）
export function lastTarget() { try { return localStorage.getItem(TARGET_KEY) === 'local' ? 'local' : 'server'; } catch (_) { return 'server'; } }
function saveTarget(t) { try { localStorage.setItem(TARGET_KEY, t === 'local' ? 'local' : 'server'); } catch (_) { /* 忽略 */ } }
/** /api/dirs、/api/history 的 ?target= 参数（服务器 = 不带，与以前完全一致） */
export function targetQuery(target, sep = '&') { return target === 'local' ? sep + 'target=local' : ''; }
export const MODEL_RE = /^[A-Za-z0-9._:\-\[\]]{1,80}$/;
export const MODEL_SUGGESTIONS = ['opus', 'sonnet', 'haiku', 'opus[1m]', 'sonnet[1m]'];

export const MODE_LABELS = {
  manual: '手动审批', default: '手动审批（default）', acceptEdits: '自动接受编辑', auto: '自动', plan: '计划模式',
  bypassPermissions: '跳过权限', dontAsk: '不询问（拒绝需审批项）',
};
export function modeLabel(m) { return MODE_LABELS[m] ? `${MODE_LABELS[m]} · ${m}` : String(m || ''); }

// ---------- toast ----------
let toastHost = null;
export function toast(text, level = 'info', ms = 4200) {
  if (!toastHost) { toastHost = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.appendChild(toastHost); }
  const t = h('div', { class: 'toast lv-' + level }, String(text));
  toastHost.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, ms);
}

// ---------- 通用 modal ----------
/** 最上层的已打开对话框（嵌套的对话框挂在下层对话框里面，文档顺序在后） */
export function topDialog() {
  const ds = document.querySelectorAll('dialog.modal[open]');
  return ds.length ? ds[ds.length - 1] : null;
}

/**
 * 同一时刻只有一个真正的模态对话框（showModal）。已有对话框打开时（如在「新建会话」里打开「管理 API key」，
 * 再在里面打开「编辑」），新对话框挂在最上层对话框**里面**、用非模态 show() 打开，下层对话框的内容设 inert。
 * 原因：原生窗口（WKWebView）里叠放多个 showModal() 的对话框时，上层对话框的输入框可能打不进字
 * （朋友在 dmg 版里遇到：API key 管理弹窗的 base_url 等输入框无法输入）。嵌套的对话框是当前模态对话框的后代，
 * 不受"模态对话框之外都是 inert"影响；Esc 只关闭最上层；下层关闭时先关掉嵌套在里面的（各自的 onClose 照常调用）。
 */
export function modal({ title, body, actions, wide, onClose }) {
  const dlg = h('dialog', { class: 'modal' + (wide ? ' wide' : '') });
  const close = h('button', { class: 'icon-btn', type: 'button', title: '关闭（Esc）', tabindex: '-1' }, icon('x'));
  const form = h('form', { method: 'dialog', class: 'modal-form' },
    h('div', { class: 'modal-head' }, h('h2', null, title), close),
    h('div', { class: 'modal-body' }, body),
    actions ? h('div', { class: 'modal-actions' }, actions) : null);
  dlg.appendChild(form);
  const host = topDialog();
  const hostForm = host ? host.querySelector(':scope > .modal-form') : null;
  close.addEventListener('click', () => dlg.close(''));
  dlg.addEventListener('close', () => {
    for (const inner of [...dlg.querySelectorAll(':scope > dialog.modal[open]')]) inner.close('');
    if (hostForm) hostForm.inert = false;
    if (onClose) onClose(dlg.returnValue);
    dlg.remove();
    // 回到下层对话框：焦点还没在它里面时放回它的第一个可用控件（不会自动回去）
    const a = document.activeElement;
    if (host && host.open && !(a && a !== document.body && host.contains(a))) {
      const f = hostForm && hostForm.querySelector('input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]):not([tabindex="-1"])');
      if (f) f.focus();
    }
  });
  // 阻止 <form method=dialog> 的默认提交（由各对话框自行处理）
  form.addEventListener('submit', (e) => e.preventDefault());
  if (host) {
    dlg.classList.add('nested');
    host.appendChild(dlg);
    if (hostForm) hostForm.inert = true;
    dlg.show();
  } else {
    // Esc（浏览器把它交给唯一的模态对话框 = 最底层）：有嵌套的对话框时只关最上层那个。
    // keydown 里处理并 preventDefault；万一浏览器仍发 cancel，同一次按键不再多关一层
    let escAt = -1;
    dlg.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || e.isComposing || e.keyCode === 229) return;
      const top = topDialog();
      if (!top || top === dlg) return;
      e.preventDefault();
      escAt = e.timeStamp;
      top.close('');
    });
    dlg.addEventListener('cancel', (e) => {
      const top = topDialog();
      if (escAt >= 0 && e.timeStamp - escAt < 100) { e.preventDefault(); escAt = -1; return; }
      if (top && top !== dlg) { e.preventDefault(); top.close(''); }
    });
    document.body.appendChild(dlg);
    dlg.showModal();
  }
  return dlg;
}

export function confirmDialog(title, message, { okText = '确定', danger = false } = {}) {
  return new Promise((resolve) => {
    let result = false;
    const ok = h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), type: 'button' }, okText);
    const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消');
    const dlg = modal({ title, body: h('p', { class: 'modal-text' }, message), actions: [cancel, ok], onClose: () => resolve(result) });
    ok.addEventListener('click', () => { result = true; dlg.close(); });
    cancel.addEventListener('click', () => dlg.close());
    ok.focus();
  });
}

export function promptDialog(title, { label = '', value = '', placeholder = '', validate, okText = '确定', extra } = {}) {
  return new Promise((resolve) => {
    let result = null;
    const input = h('input', { class: 'input', type: 'text', name: 'value', 'aria-label': label || title, value, placeholder, autocomplete: 'off', spellcheck: 'false' });
    const err = h('div', { class: 'field-err' });
    const ok = h('button', { class: 'btn primary', type: 'button' }, okText);
    const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消');
    const submit = () => {
      const v = input.value.trim();
      const e = validate ? validate(v) : '';
      if (e) { err.textContent = e; input.focus(); return; }
      result = v;
      dlg.close();
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); }
    });
    ok.addEventListener('click', submit);
    cancel.addEventListener('click', () => dlg.close());
    const body = h('div', { class: 'field' }, label ? h('label', { class: 'field-label' }, label) : null,
      extra ? h('div', { class: 'row' }, input, extra(input)) : input, err);
    const dlg = modal({ title, body, actions: [cancel, ok], onClose: () => resolve(result) });
    input.focus();
    input.select();
  });
}

// ---------- 我的作业 ----------
/** 在 anchor 下方弹出 squeue 列表；选择后调用 onPick(jobid) */
export function jobsPopover(api, anchor, onPick) {
  document.querySelectorAll('.popover.jobs').forEach((p) => p.remove());
  const pop = h('div', { class: 'popover jobs', role: 'dialog' },
    h('div', { class: 'pop-head' }, '我的作业（squeue）'), h('div', { class: 'pop-body' }, spinner(), ' 正在查询…'));
  // 模态对话框之外的元素是 inert 的，所以弹出层要挂在所在 dialog 内
  (anchor.closest('dialog') || document.body).appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 380)) + 'px';
  pop.style.top = (r.bottom + 6) + 'px';
  const dismiss = (e) => {
    if (e && (pop.contains(e.target) || anchor.contains(e.target))) return;
    pop.remove();
    document.removeEventListener('mousedown', dismiss, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const onKey = (e) => { if (e.key === 'Escape') dismiss(); };
  document.addEventListener('mousedown', dismiss, true);
  document.addEventListener('keydown', onKey, true);
  const body = pop.querySelector('.pop-body');
  api.get('/api/jobs').then((res) => {
    const jobs = Array.isArray(res.jobs) ? res.jobs : [];
    if (!jobs.length) { body.replaceChildren(h('div', { class: 'muted pad' }, '当前没有作业')); return; }
    body.replaceChildren(...jobs.map((j) => {
      const running = String(j.state).toUpperCase() === 'RUNNING';
      const row = h('button', { class: 'job-row-btn', type: 'button' },
        h('span', { class: 'job-id' }, String(j.jobid ?? '')),
        h('span', { class: 'job-state ' + (running ? 'run' : 'other') }, String(j.state ?? '')),
        h('span', { class: 'job-name' }, String(j.name ?? '')),
        h('span', { class: 'job-meta' }, [j.nodes, j.elapsed && '已运行 ' + j.elapsed, j.left && '剩余 ' + j.left].filter(Boolean).join(' · ')));
      row.addEventListener('click', () => { dismiss(); onPick(String(j.jobid)); });
      return row;
    }));
  }, (e) => {
    body.replaceChildren(h('div', { class: 'card-error pad' }, '查询失败：' + e.message));
  });
  return pop;
}

// ---------- 本机：系统"选择文件夹"对话框（Finder 同款，SPEC §13） ----------
let folderBusy = false;
/**
 * 让本地后端弹出 macOS 自带的选择文件夹对话框（osascript choose folder），阻塞到用户选择 / 取消。
 * resolve(路径) / resolve(null)（取消）；不可用（演示模式等）或出错时 toast 并 resolve(null)。
 */
export async function chooseLocalFolder(api, current) {
  if (folderBusy) { toast('已经打开了一个选择文件夹对话框（可能在其它窗口后面）', 'warn'); return null; }
  folderBusy = true;
  try {
    const r = await api.post('/api/local/choose_folder', current ? { default: current } : {});
    if (r && typeof r.path === 'string' && r.path.startsWith('/')) return r.path;
    return null;   // 取消
  } catch (e) {
    toast((e && e.status === 501 ? '' : '打开选择文件夹对话框失败：') + e.message + (e && e.status === 501 ? '（可用“应用内浏览”）' : ''),
      e && e.status === 409 ? 'warn' : 'error');
    return null;
  } finally { folderBusy = false; }
}

// ---------- 目录选择器 ----------
/** target = 'local' 时列本机目录（/api/dirs?target=local），否则列登录节点 */
export function pickDirectory(api, startPath, target = 'server') {
  return new Promise((resolve) => {
    let result = null;
    let cur = startPath || '~';
    let hidden = false;
    let curAbs = '';
    let parent = null;
    const crumbs = h('div', { class: 'crumbs' });
    const list = h('div', { class: 'dir-list' });
    const pathInput = h('input', { class: 'input mono', type: 'text', name: 'path', 'aria-label': '路径', value: cur, spellcheck: 'false', autocomplete: 'off' });
    const hiddenBox = h('input', { type: 'checkbox', name: 'hidden' });
    const upBtn = h('button', { class: 'btn ghost small', type: 'button' }, icon('up'), '上级');
    const homeBtn = h('button', { class: 'btn ghost small', type: 'button' }, icon('home'), '主目录');
    const status = h('div', { class: 'muted small-text' });
    const ok = h('button', { class: 'btn primary', type: 'button', disabled: true }, '选择此目录');
    const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消');

    let reqId = 0;
    const load = async (p) => {
      const my = ++reqId;
      list.replaceChildren(h('div', { class: 'muted pad' }, spinner(), ' 正在读取…'));
      ok.disabled = true;
      try {
        const r = await api.get('/api/dirs?path=' + encodeURIComponent(p) + '&hidden=' + (hidden ? 1 : 0) + targetQuery(target));
        if (my !== reqId) return;
        curAbs = String(r.path || p);
        parent = r.parent || null;
        pathInput.value = curAbs;
        upBtn.disabled = !parent;
        ok.disabled = false;
        renderCrumbs();
        const dirs = Array.isArray(r.dirs) ? r.dirs : [];
        status.textContent = `${dirs.length} 个子目录`;
        if (!dirs.length) { list.replaceChildren(h('div', { class: 'muted pad' }, '（没有子目录）')); return; }
        list.replaceChildren(...dirs.map((name) => {
          const b = h('button', { class: 'dir-item', type: 'button' }, icon('folder'), h('span', null, String(name)));
          b.addEventListener('click', () => load(joinPath(curAbs, String(name))));
          return b;
        }));
      } catch (e) {
        if (my !== reqId) return;
        list.replaceChildren(h('div', { class: 'card-error pad' }, '无法读取：' + e.message));
      }
    };
    const renderCrumbs = () => {
      const parts = curAbs.split('/').filter(Boolean);
      const nodes = [];
      const root = h('button', { class: 'crumb', type: 'button' }, '/');
      root.addEventListener('click', () => load('/'));
      nodes.push(root);
      parts.forEach((seg, k) => {
        const path = '/' + parts.slice(0, k + 1).join('/');
        const b = h('button', { class: 'crumb', type: 'button' }, seg);
        b.addEventListener('click', () => load(path));
        if (k > 0) nodes.push(h('span', { class: 'crumb-sep' }, '/'));
        nodes.push(b);
      });
      crumbs.replaceChildren(...nodes);
    };
    upBtn.addEventListener('click', () => { if (parent) load(parent); });
    homeBtn.addEventListener('click', () => load('~'));
    hiddenBox.addEventListener('change', () => { hidden = hiddenBox.checked; load(curAbs || cur); });
    pathInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); load(pathInput.value.trim() || '~'); }
    });
    ok.addEventListener('click', () => { result = curAbs; dlg.close(); });
    cancel.addEventListener('click', () => dlg.close());

    const body = h('div', { class: 'dirpicker' },
      h('div', { class: 'row' }, pathInput),
      h('div', { class: 'row tools-row' }, upBtn, homeBtn, h('label', { class: 'check' }, hiddenBox, '显示隐藏目录'), h('span', { class: 'spacer' }), status),
      crumbs, list);
    const dlg = modal({ title: target === 'local' ? '选择工作目录（本机）' : '选择工作目录（登录节点）', body, actions: [cancel, ok], wide: true, onClose: () => resolve(result) });
    pathInput.focus();
    load(cur);
  });
}

function joinPath(a, b) { return (a.endsWith('/') ? a : a + '/') + b; }

// ---------- 新建会话 ----------
export const LAUNCH_MODES = ['acceptEdits', 'auto', 'bypassPermissions', 'manual', 'dontAsk', 'plan'];
export const LAUNCH_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];

/**
 * preset: {jobid, cwd, model, permission_mode, skip_permissions, effort, title, resume, target}
 *   target：'server' | 'local'（运行位置；不给 = 上次的选择）。续接历史会话时固定为该历史所在的机器（不能切换）。
 * modelHints: 额外的模型候选（来自最近一次 model_list）
 * 内部 POST /api/sessions（字段见 SPEC §4.2 create；本机 = jobid "local"，SPEC §9），成功后 resolve(meta)；取消 resolve(null)
 */
export function newSessionDialog(api, { config, preset = {}, modelHints = [] }) {
  return new Promise((resolve) => {
    let result = null;
    const cfg = config || {};
    const locked = !!preset.resume;   // 续接：claude 的对话记录只在原来那台机器上
    let target = preset.target === 'local' || preset.target === 'server' ? preset.target
      : (isLocalJob(preset.jobid) || !cfg.host ? 'local' : lastTarget());   // 还没设置服务器（设置里的 SSH 主机）时默认本机
    const jobid = h('input', { class: 'input mono', type: 'text', name: 'jobid', 'aria-label': '作业 jobid', value: isLocalJob(preset.jobid) ? (cfg.jobid ?? '') : (preset.jobid ?? cfg.jobid ?? ''), placeholder: '例如 123456', autocomplete: 'off', spellcheck: 'false' });
    const jobsBtn = h('button', { class: 'btn ghost', type: 'button' }, '我的作业');
    jobsBtn.addEventListener('click', () => jobsPopover(api, jobsBtn, (j) => { jobid.value = j; renderTarget(); }));
    // 工作目录：服务器 / 本机各记各的（切换运行位置时互不覆盖）；本机默认 ~
    const cwdBy = {
      server: preset.cwd ?? cfg.default_cwd ?? '~',
      local: preset.cwd ?? '~',
    };
    const cwd = h('input', { class: 'input mono', type: 'text', name: 'cwd', 'aria-label': '工作目录', value: cwdBy[target], placeholder: '~ 或绝对路径', autocomplete: 'off', spellcheck: 'false' });
    // 浏览：本机 = macOS 自带的"选择文件夹"对话框（Finder 同款，用户要求）；服务器 = 应用内目录浏览（登录节点）。
    // 本机仍保留应用内浏览作为次要入口（小链接）。
    const browse = h('button', { class: 'btn ghost', type: 'button' }, icon('folder'), '浏览…');
    browse.addEventListener('click', async () => {
      if (target === 'local') {
        browse.disabled = true;
        browse.replaceChildren(spinner(), ' 在 Finder 对话框中选择…');
        try {
          const p = await chooseLocalFolder(api, cwd.value.trim() || '~');
          if (p) cwd.value = p;
        } finally {
          browse.disabled = false;
          renderBrowse();
          cwd.focus();
        }
        return;
      }
      const p = await pickDirectory(api, cwd.value.trim() || '~', target);
      if (p) cwd.value = p;
    });
    const inApp = h('button', { class: 'linklike small-text', type: 'button', title: '在应用内浏览本机目录（不弹系统对话框）' }, '应用内浏览');
    inApp.addEventListener('click', async () => {
      const p = await pickDirectory(api, cwd.value.trim() || '~', target);
      if (p) cwd.value = p;
    });
    const renderBrowse = () => {
      const local = target === 'local';
      browse.replaceChildren(icon('folder'), local ? '选择文件夹…' : '浏览…');
      browse.title = local ? '打开 macOS 自带的“选择文件夹”对话框（与 Finder 相同）' : '浏览登录节点上的目录';
      inApp.hidden = !local;
    };
    // 运行位置：服务器（作业 J）/ 本机
    const targetHint = h('div', { class: 'field-hint' });
    const segServer = h('button', { class: 'seg-item target-item', type: 'button', role: 'radio', disabled: locked && target !== 'server' });
    const segLocal = h('button', { class: 'seg-item target-item', type: 'button', role: 'radio', disabled: locked && target !== 'local' });
    const seg = h('div', { class: 'seg target-seg', role: 'radiogroup', 'aria-label': '运行位置' }, segServer, segLocal);
    const jobField = h('div', { class: 'field' }, h('label', { class: 'field-label' }, '作业 jobid'), h('div', { class: 'row' }, jobid, jobsBtn));
    // 权限：按运行位置分别记住上次的选择（launchprefs.js）。初始 = 默认（不加参数，等同直接敲 claude）。preset 明确给了权限参数时以 preset 为准。
    const permBy = { server: initialPerm('server', preset), local: initialPerm('local', preset) };
    const perm = h('select', { class: 'input', name: 'permission', 'aria-label': '权限' },
      h('option', { value: '' }, '默认（不加参数）'),
      LAUNCH_MODES.map((m) => h('option', { value: m }, '--permission-mode ' + m + (MODE_LABELS[m] ? '（' + MODE_LABELS[m] + '）' : ''))),
      h('option', { value: PERM_SKIP }, '跳过权限（--dangerously-skip-permissions）'));
    perm.value = permBy[target];
    const effort = h('select', { class: 'input', name: 'effort', 'aria-label': 'effort' },
      h('option', { value: '', selected: !preset.effort }, '默认（不加参数）'),
      LAUNCH_EFFORTS.map((e) => h('option', { value: e, selected: e === preset.effort }, '--effort ' + e)));
    const listId = 'models-' + Math.random().toString(36).slice(2);
    const model = h('input', { class: 'input mono', type: 'text', name: 'model', 'aria-label': '模型', value: preset.model ?? '', placeholder: '留空 = 不加 --model（使用你的默认模型）', list: listId, autocomplete: 'off', spellcheck: 'false' });
    const hints = [...new Set([...MODEL_SUGGESTIONS, ...modelHints.filter((m) => MODEL_RE.test(m))])];
    const dl = h('datalist', { id: listId }, hints.map((m) => h('option', { value: m })));
    const title = h('input', { class: 'input', type: 'text', name: 'title', 'aria-label': '标题', value: preset.title ?? '', placeholder: '可留空（默认显示 Claude Code 里的会话标题）' });
    // API：默认 = 那台机器 settings.json 里的 key（不加 --settings）；选已保存的 key = agent 写 0600 文件后 --settings。
    // 从历史续接：默认选中该 claude 会话上次用的 key（preset.api_key_id / api_note / api_missing_text 由 app.js 给出）
    const apiPick = apiKeySelect(api, {
      current: preset.api_key_id || '', currentName: preset.api_key_name || '', note: preset.api_note || '',
      missingText: preset.api_missing_text || '', changeHint: preset.resume && preset.api_note ? SWITCH_COST_HINT : '',
      local: target === 'local',
    });
    const note = h('div', { class: 'muted small-text' });
    const err = h('div', { class: 'field-err' });
    const ok = h('button', { class: 'btn primary', type: 'button' }, preset.resume ? '续接会话' : '创建会话');
    const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消');

    const renderTarget = () => {
      const local = target === 'local';
      const j = jobid.value.trim();
      segServer.replaceChildren(icon('server'), '服务器', h('small', { class: 'target-sub' }, j ? '作业 ' + j : '作业 —'));
      segLocal.replaceChildren(icon('laptop'), '本机', h('small', { class: 'target-sub' }, '这台 Mac'));
      for (const [b, on] of [[segServer, !local], [segLocal, local]]) {
        b.classList.toggle('on', on);
        b.setAttribute('aria-checked', String(on));
      }
      jobField.hidden = local;
      targetHint.textContent = locked
        ? '续接只能在这段对话原来所在的' + (local ? '本机' : '服务器') + '上进行（claude 的对话记录不在另一台机器上）'
        : local ? '在这台 Mac 的私有 tmux（-L ' + (cfg.tmux_label || 'ccr') + '）里运行交互式 claude，用你自己的登录 shell 与 ~/.claude 配置（本程序不改任何配置）'
          : '经 ssh ' + (cfg.host || '登录节点') + ' → srun --overlap 在该作业的计算节点上运行';
      note.textContent = local
        ? '启动命令 = 你的登录 shell（$SHELL -l -i，与在终端里一样）执行 claude + 上面明确选择的参数；全部留默认就等同于你在终端里直接敲 claude。选了 API key 时额外加 --settings <本机 ~/.cc-remote-chat/keys/ 下的 0600 key 文件>（只覆盖 ANTHROPIC_AUTH_TOKEN 及该 key 的 base_url）。'
        : '启动命令 = claude + 上面明确选择的参数；全部留默认就等同于你直接敲 claude。选了 API key 时额外加 --settings <计算节点上的 0600 key 文件>（只覆盖 ANTHROPIC_AUTH_TOKEN 及该 key 的 base_url）。';
      apiPick.setLocal(local);
      renderBrowse();
    };
    const setTarget = (t) => {
      if (locked || t === target) return;
      cwdBy[target] = cwd.value;
      permBy[target] = perm.value;
      target = t;
      cwd.value = cwdBy[target];
      perm.value = permBy[target];
      err.textContent = '';
      renderTarget();
      (target === 'server' && !jobid.value.trim() ? jobid : cwd).focus();
    };
    segServer.addEventListener('click', () => setTarget('server'));
    segLocal.addEventListener('click', () => setTarget('local'));
    jobid.addEventListener('input', renderTarget);

    const field = (label, ...ctl) => h('div', { class: 'field' }, h('label', { class: 'field-label' }, label), h('div', { class: 'row' }, ...ctl));
    const body = h('div', { class: 'form-grid' },
      preset.resume ? h('div', { class: 'resume-note' }, icon('history'), '将以 --resume 续接历史会话 ', h('code', null, String(preset.resume))) : null,
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '运行位置'), seg, targetHint),
      jobField,
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '工作目录'), h('div', { class: 'row' }, cwd, browse), inApp),
      field('模型（可选）', model, dl),
      field('权限', perm),
      field('Effort', effort),
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'API'), h('div', { class: 'row' }, apiPick.el), apiPick.hint),
      field('标题', title),
      note,
      err);
    renderTarget();

    const submit = async () => {
      err.textContent = '';
      const local = target === 'local';
      const j = jobid.value.trim();
      if (!local && !JOBID_RE.test(j)) { err.textContent = 'jobid 格式不正确（应为数字，或 数字_数字）'; jobid.focus(); return; }
      const c = cwd.value.trim() || '~';
      if (!(c === '~' || c.startsWith('~/') || c.startsWith('/'))) { err.textContent = '工作目录必须是绝对路径或以 ~ 开头'; cwd.focus(); return; }
      const m = model.value.trim();
      if (m && !MODEL_RE.test(m)) { err.textContent = '模型名称包含非法字符'; model.focus(); return; }
      const req = { jobid: local ? LOCAL_JOBID : j, cwd: c };
      if (m) req.model = m;
      if (perm.value === PERM_SKIP) req.skip_permissions = true;
      else if (perm.value) req.permission_mode = perm.value;
      if (effort.value) req.effort = effort.value;
      if (apiPick.missing()) { err.textContent = preset.api_missing_text || '所选 API key 已被删除，请改选'; apiPick.el.focus(); return; }
      if (apiPick.value()) req.api_key_id = apiPick.value();
      const t = title.value.trim();
      if (t) req.title = t;
      if (preset.resume) req.resume = preset.resume;
      ok.disabled = true;
      ok.replaceChildren(spinner(), ' 创建中…');
      try {
        result = await api.post('/api/sessions', req);
        if (!locked) saveTarget(target);   // 记住这次选的运行位置（续接是被历史决定的，不算）
        savePerm(target, perm.value);      // 按运行位置记住这次的权限选择
        dlg.close();
      } catch (e) {
        err.textContent = e.message;
        ok.disabled = false;
        ok.textContent = preset.resume ? '续接会话' : '创建会话';
      }
    };
    ok.addEventListener('click', submit);
    cancel.addEventListener('click', () => dlg.close());
    for (const inp of [jobid, cwd, model, title]) {
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); } });
    }
    const dlg = modal({ title: preset.resume ? (target === 'local' ? '续接历史会话（本机）' : '续接历史会话') : '新建会话', body, actions: [cancel, ok], onClose: () => resolve(result) });
    (target === 'server' && !jobid.value ? jobid : cwd).focus();
  });
}

export { basename };
