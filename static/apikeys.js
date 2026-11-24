// API key 选择（--settings）：共享的 key/用量缓存、「API」下拉框、管理弹窗、"换 API 重启续接"弹窗。
// 明文 key 只出现在"新增"的密码框里，提交后立即清空；其它地方只显示名称、掩码（sk-ab12…cd34）与 base_url。
// claude 只在启动时读取 key：换 API = 以 --resume 续接重启（同一个本地会话、同一段对话）。
import { h, icon, spinner, fmtRelTime } from './dom.js';
import { modal, toast, confirmDialog } from './dialogs.js';

export const API_KEY_RE = /^[A-Za-z0-9._-]{8,256}$/;
export const BASE_URL_RE = /^https?:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~/-]*)?$/;
export const DEFAULT_API_LABEL = '服务器默认（settings.json）';
// 本机会话的"默认" = 本机 ~/.claude/settings.json 里的 key（不加 --settings）；与"服务器默认"条目无关
export const LOCAL_DEFAULT_API_LABEL = '默认（本机 settings.json）';
const isLocalMeta = (m) => !!m && m.jobid === 'local';
export const SWITCH_COST_HINT = '换 key 后提示缓存可能失效：下一条消息会在新 key 上重新读取整段历史，可能更慢、更贵一点。';
const MANAGE = '__manage__';

// ====================== 共享缓存（app.js 订阅变化以刷新会话头/列表标签） ======================
const store = { keys: [], loaded: false, usage: {}, listeners: new Set() };

export function onApiKeysChange(cb) { store.listeners.add(cb); return () => store.listeners.delete(cb); }
function emit() { for (const cb of [...store.listeners]) { try { cb(); } catch (_) { /* 忽略 */ } } }

export async function loadApiKeys(api) {
  const r = await api.get('/api/api_keys');
  store.keys = Array.isArray(r.keys) ? r.keys : [];
  store.loaded = true;
  emit();
  return store.keys;
}

/** 用量：后端缓存 5 分钟（过期的自动重查）；refresh=true 强制重查 */
export async function loadUsage(api, refresh = false) {
  const r = await api.get('/api/api_keys/usage' + (refresh ? '?refresh=1' : ''));
  store.usage = (r && typeof r.usage === 'object' && r.usage) || {};
  emit();
  return store.usage;
}

export function cachedKeys() { return store.keys; }
export function serverDefaultKey() { return store.keys.find((k) => k.is_server_default) || null; }
function selectableKeys() { return store.keys.filter((k) => !k.is_server_default); }
function usageOf(id) { return (id && store.usage[id]) || null; }

export function fmtUsd(v) {
  if (v == null || !isFinite(v)) return '—';
  const n = Number(v);
  if (n === 0) return '$0';
  if (Math.abs(n) < 0.01) return '$' + n.toFixed(4);
  return '$' + n.toFixed(2);
}

/** 「近 7 天 $12.34 · 今日 $0.01」；查询失败 → 「查询失败」；没有可查的地址 → 「—」 */
export function usageText(u, { short = false } = {}) {
  if (!u) return '';
  if (u.error) return u.base_url ? '查询失败' : '—';
  return short ? `近7天 ${fmtUsd(u.week_usd)}` : `近 7 天 ${fmtUsd(u.week_usd)} · 今日 ${fmtUsd(u.today_usd)}`;
}

export function usageTitle(u) {
  if (!u) return '';
  if (u.error) return (u.base_url ? '查询失败：' : '') + u.error;
  const parts = [`近 7 天 ${fmtUsd(u.week_usd)}`, `今日 ${fmtUsd(u.today_usd)}`];
  if (u.remaining_usd != null) parts.push(`余额 ${fmtUsd(u.remaining_usd)}`);
  if (u.fetched_at) parts.push('更新于 ' + fmtRelTime(u.fetched_at));
  return parts.join(' · ');
}

/** 该会话所用 API 的显示名：选了 key → 名称（已删除标出来）；服务器默认 → 服务器默认条目的名称或"默认"；
 *  本机会话（meta.jobid === 'local'）的默认 → "本机默认"（本机 settings.json，不套用服务器默认条目的名称） */
export function apiLabel(meta, { short = false } = {}) {
  const m = meta || {};
  const kid = m.launch && m.launch.api_key_id;
  if (!kid && isLocalMeta(m)) return short ? '默认' : '本机默认';
  if (!kid) { const sd = serverDefaultKey(); return sd ? sd.name : (short ? '默认' : '默认 API'); }
  return (m.api_key_name || kid) + (m.api_key_missing ? '（已删除）' : '');
}

/** 该会话所用 API 的用量 tooltip */
export function apiUsageTitle(meta) {
  const m = meta || {};
  const kid = m.launch && m.launch.api_key_id;
  if (!kid && isLocalMeta(m)) return 'API：本机默认（本机 ~/.claude/settings.json，不加 --settings）';
  const k = kid ? store.keys.find((x) => x.id === kid) : serverDefaultKey();
  const head = kid ? `API：${apiLabel(m)}` : 'API：服务器默认（settings.json，不加 --settings）';
  const addr = k ? ` · ${k.base_url || '默认地址'}` : '';
  const u = k ? usageOf(k.id) : null;
  return head + addr + (u ? '\n' + usageTitle(u) : '');
}

function keyOptionText(k) {
  const u = usageText(usageOf(k.id), { short: true });
  return `${k.name} · ${k.masked} · ${k.base_url || '默认地址'}` + (u ? ` · ${u}` : '');
}

function defaultOptionText(local) {
  if (local) return LOCAL_DEFAULT_API_LABEL;
  const sd = serverDefaultKey();
  if (!sd) return DEFAULT_API_LABEL;
  const u = usageText(usageOf(sd.id), { short: true });
  return `${DEFAULT_API_LABEL} · ${sd.name}` + (u ? ` · ${u}` : '');
}

// ====================== 「API」下拉框 ======================
/**
 * 服务器默认 + 已保存的 key（名称 · 掩码 · 地址 · 近7天用量）+「管理 API key…」。
 * current = 当前 api_key_id（'' / null = 服务器默认）；currentName = 已删除时用于显示的旧名称；
 * note = 下方常驻说明（如"该会话上次使用：xxx"）；missingText = current 已被删除时的提示；
 * changeHint = 选择与 current 不同时显示的提示（换 key 的代价）。
 * local = 本机会话："默认"选项显示为本机 settings.json（可用 setLocal() 随"运行位置"切换）。
 * 返回 {el, hint, value(), missing(), refresh(), setLocal(bool)}；value() 为 '' 表示默认（不加 --settings）。
 */
export function apiKeySelect(api, { current = '', currentName = '', note = '', missingText = '', changeHint = '', local = false } = {}) {
  const sel = h('select', { class: 'input', name: 'api_key', 'aria-label': 'API' });
  const hint = h('div', { class: 'field-hint api-hint' });
  const initial = current || '';
  let cur = initial;
  let failed = '';
  let isLocal = !!local;
  const missing = () => !!cur && store.loaded && !store.keys.some((k) => k.id === cur && !k.is_server_default);
  const render = () => {
    const keys = selectableKeys();
    const opts = [h('option', { value: '' }, defaultOptionText(isLocal))];
    for (const k of keys) opts.push(h('option', { value: k.id }, keyOptionText(k)));
    if (cur && !keys.some((k) => k.id === cur)) {
      opts.push(h('option', { value: cur }, store.loaded ? `${currentName || cur}（已删除，请改选）` : (currentName || cur)));
    }
    opts.push(h('option', { value: MANAGE }, '管理 API key…'));
    sel.replaceChildren(...opts);
    sel.value = cur;
    const lines = [];
    if (failed) lines.push(failed);
    else if (missing()) lines.push(missingText || ('这个 API key 已被删除，请改选其它 key 或' + (isLocal ? '默认' : '服务器默认')));
    else if (note) lines.push(note);
    if (!missing() && changeHint && cur !== initial) lines.push(changeHint);
    hint.textContent = lines.join(' ');
    hint.classList.toggle('err', !!failed || missing());
    hint.classList.toggle('warn', !failed && !missing() && !!changeHint && cur !== initial);
  };
  const off = onApiKeysChange(() => { if (!sel.isConnected && store.loaded) { off(); return; } render(); });
  const refresh = async () => {
    try { await loadApiKeys(api); failed = ''; } catch (e) { failed = '读取 API key 列表失败：' + e.message; render(); return; }
    loadUsage(api).catch(() => {});   // 用量后到：不阻塞下拉框
  };
  sel.addEventListener('change', async () => {
    if (sel.value !== MANAGE) { cur = sel.value; render(); return; }
    sel.value = cur;
    const added = await manageApiKeysDialog(api);
    if (added) cur = added;
    await refresh();
    render();
  });
  render();
  refresh();
  const setLocal = (v) => { if (isLocal !== !!v) { isLocal = !!v; render(); } };
  return { el: sel, hint, value: () => cur, missing, refresh, setLocal };
}

// ====================== 管理弹窗 ======================
function editKeyDialog(api, k) {
  return new Promise((resolve) => {
    let changed = false;
    const name = h('input', { class: 'input', type: 'text', 'aria-label': '名称', value: k.name, maxlength: '64', autocomplete: 'off', spellcheck: 'false' });
    const base = h('input', { class: 'input mono', type: 'text', 'aria-label': 'base_url', value: k.base_url || '', placeholder: '留空 = 默认地址（settings.json 的 ANTHROPIC_BASE_URL）', autocomplete: 'off', spellcheck: 'false' });
    const err = h('div', { class: 'field-err' });
    const ok = h('button', { class: 'btn primary', type: 'button' }, '保存');
    const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消');
    const submit = async () => {
      err.textContent = '';
      const n = name.value.trim();
      const b = base.value.trim();
      if (!n || n.length > 64) { err.textContent = '名称不能为空，最多 64 字'; name.focus(); return; }
      if (b && (b.length > 200 || !BASE_URL_RE.test(b))) { err.textContent = 'base_url 格式不正确（形如 http://host:port 或 https://host/path）'; base.focus(); return; }
      ok.disabled = true;
      try {
        await api.patch('/api/api_keys/' + encodeURIComponent(k.id), { name: n, base_url: b || null });
        changed = true;
        dlg.close();
      } catch (e) { err.textContent = e.message; ok.disabled = false; }
    };
    ok.addEventListener('click', submit);
    cancel.addEventListener('click', () => dlg.close());
    for (const inp of [name, base]) inp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); } });
    const body = h('div', { class: 'form-grid' },
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '名称'), name),
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'base_url（可选）'), base,
        h('div', { class: 'field-hint' }, k.is_server_default ? '服务器默认 key 的地址只用于查询用量（启动会话时不加 --settings）。' : '会写进 claude 所在机器（计算节点，或本机会话时的本机）上的 key 文件（ANTHROPIC_BASE_URL）。')),
      h('div', { class: 'muted small-text' }, 'key 本身不能修改；要换 key 请删除后重新添加。'),
      err);
    const dlg = modal({ title: '编辑「' + k.name + '」', body, actions: [cancel, ok], onClose: () => resolve(changed) });
    name.focus();
  });
}

/** 列表（名称、掩码、地址、近 7 天用量、在用会话数、编辑、删除）+ 新增。resolve(最后新增的可选 key id | null) */
export function manageApiKeysDialog(api) {
  return new Promise((resolve) => {
    let lastAdded = null;
    const list = h('div', { class: 'apikey-list' }, h('div', { class: 'muted pad' }, spinner(), ' 正在读取…'));
    const name = h('input', { class: 'input', type: 'text', name: 'api_key_name', 'aria-label': '名称', placeholder: '名称，例如「work」', maxlength: '64', autocomplete: 'off', spellcheck: 'false' });
    const key = h('input', { class: 'input mono', type: 'password', name: 'api_key_value', 'aria-label': 'API key', placeholder: 'sk-…（只保存在本机：本程序数据目录里的 api_keys.json）', autocomplete: 'new-password', spellcheck: 'false' });
    const base = h('input', { class: 'input mono', type: 'text', name: 'api_key_base', 'aria-label': 'base_url', placeholder: 'base_url（可选，留空 = 默认地址）', autocomplete: 'off', spellcheck: 'false' });
    const isDefault = h('input', { type: 'checkbox', name: 'api_key_is_default' });
    const add = h('button', { class: 'btn primary', type: 'button' }, icon('plus'), '保存');
    const err = h('div', { class: 'field-err' });
    const done = h('button', { class: 'btn ghost', type: 'button' }, '完成');
    const refreshBtn = h('button', { class: 'btn ghost small', type: 'button', title: '重新查询各 key 的网关用量（GET <base_url>/v1/usage）' }, icon('refresh'), '刷新用量');
    const usageNote = h('span', { class: 'muted small-text' });

    const render = () => {
      const keys = store.keys;
      const sd = serverDefaultKey();
      isDefault.disabled = !!sd;
      isDefault.parentElement && (isDefault.parentElement.title = sd ? '已经有一条服务器默认 key（' + sd.name + '）' : '');
      if (!keys.length) { list.replaceChildren(h('div', { class: 'muted pad' }, '还没有保存的 API key（不选 = 使用 settings.json 里的 key：服务器会话用服务器的，本机会话用本机的）')); return; }
      list.replaceChildren(...keys.map((k) => {
        const ed = h('button', { class: 'btn ghost small', type: 'button', title: '修改名称 / base_url' }, icon('edit'), '编辑');
        const del = h('button', { class: 'btn danger-ghost small', type: 'button', title: '删除' }, icon('trash'), '删除');
        ed.addEventListener('click', async () => { if (await editKeyDialog(api, k)) { await reload(); loadUsage(api).catch(() => {}); } });
        del.addEventListener('click', async () => {
          if (!(await confirmDialog('删除 API key', `从本机删除「${k.name}」（${k.masked}）？`, { okText: '删除', danger: true }))) return;
          const path = '/api/api_keys/' + encodeURIComponent(k.id);
          try {
            await api.del(path);
          } catch (e) {
            if (!(e && e.status === 409 && e.code === 'api_key_in_use')) { toast('删除失败：' + e.message, 'error'); return; }
            if (!(await confirmDialog('仍有会话在用', e.message + '\n\n仍要删除吗？', { okText: '仍然删除', danger: true }))) return;
            try { await api.del(path + '?force=1'); } catch (e2) { toast('删除失败：' + e2.message, 'error'); return; }
          }
          toast('已删除「' + k.name + '」');
          reload();
        });
        const u = usageOf(k.id);
        return h('div', { class: 'apikey-row' + (k.is_server_default ? ' is-default' : '') },
          icon('key'),
          h('div', { class: 'apikey-main' },
            h('div', { class: 'apikey-line' },
              h('span', { class: 'apikey-name', title: k.name }, k.name),
              k.is_server_default ? h('span', { class: 'badge', title: '代表服务器 settings.json 里的 key：只用于显示名称与查询用量，选择"服务器默认"时不加 --settings' }, '服务器默认') : null,
              h('code', { class: 'apikey-mask' }, k.masked)),
            h('div', { class: 'apikey-line sub' },
              h('span', { class: 'apikey-base mono', title: k.base_url || '服务器 settings.json 的 ANTHROPIC_BASE_URL' }, k.base_url || '默认地址'),
              h('span', { class: 'apikey-usage' + (u && u.error ? ' err' : ''), title: usageTitle(u) }, u ? usageText(u) : '用量查询中…'),
              k.in_use ? h('span', { class: 'chip compact', title: '引用这个 key 的本地会话（含已退出的）' }, k.in_use + ' 个会话在用') : null)),
          ed, del);
      }));
    };
    const reload = async () => {
      try { await loadApiKeys(api); } catch (e) { list.replaceChildren(h('div', { class: 'card-error pad' }, '读取失败：' + e.message)); }
    };
    const fetchUsage = async (refresh) => {
      refreshBtn.disabled = true;
      usageNote.textContent = refresh ? '正在刷新用量…' : '';
      try { await loadUsage(api, refresh); usageNote.textContent = ''; } catch (e) { usageNote.textContent = '用量查询失败：' + e.message; }
      refreshBtn.disabled = false;
    };
    const off = onApiKeysChange(() => render());

    const submit = async () => {
      err.textContent = '';
      const n = name.value.trim();
      const v = key.value.trim();
      const b = base.value.trim();
      if (!n) { err.textContent = '请填写名称'; name.focus(); return; }
      if (n.length > 64) { err.textContent = '名称最多 64 字'; name.focus(); return; }
      if (!API_KEY_RE.test(v)) { err.textContent = 'API key 格式不正确（只能包含字母、数字和 . _ -，长度 8–256）'; key.focus(); return; }
      if (b && (b.length > 200 || !BASE_URL_RE.test(b))) { err.textContent = 'base_url 格式不正确（形如 http://host:port 或 https://host/path）'; base.focus(); return; }
      add.disabled = true;
      try {
        const body = { name: n, key: v };
        if (b) body.base_url = b;
        if (isDefault.checked) body.is_server_default = true;
        const r = await api.post('/api/api_keys', body);
        if (r && r.api_key && !r.api_key.is_server_default) lastAdded = r.api_key.id;
        name.value = ''; key.value = ''; base.value = ''; isDefault.checked = false;
        toast('已保存「' + n + '」');
        await reload();
        fetchUsage(false);
      } catch (e) {
        err.textContent = e.message;
      } finally {
        add.disabled = false;
      }
    };
    add.addEventListener('click', submit);
    refreshBtn.addEventListener('click', () => fetchUsage(true));
    for (const inp of [name, key, base]) {
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); } });
    }
    done.addEventListener('click', () => dlg.close());
    const body = h('div', { class: 'form-grid' },
      h('div', { class: 'row tools-row' }, h('span', { class: 'field-label' }, '已保存'), h('span', { class: 'spacer' }), usageNote, refreshBtn),
      list,
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '新增 API key'),
        h('div', { class: 'row' }, name), h('div', { class: 'row' }, key), h('div', { class: 'row' }, base, add),
        h('label', { class: 'check' }, isDefault, '这是服务器默认 key（settings.json 里那个；只用于显示名称与查询用量，选择时不加 --settings）'),
        err),
      h('div', { class: 'muted small-text' }, '选择某个 key 启动时，程序把它（及 base_url）写到 claude 所在机器（计算节点；本机会话 = 本机）的 ~/.cc-remote-chat/keys/（0600）并以 --settings 传给 claude，只覆盖 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL；其它设置仍取那台机器的 settings.json。用量由本机直接查询网关（GET <base_url>/v1/usage），缓存 5 分钟。'));
    const dlg = modal({ title: '管理 API key', body, actions: [done], wide: true, onClose: () => { key.value = ''; off(); resolve(lastAdded); } });
    name.focus();
    reload().then(() => fetchUsage(false));
  });
}

// ====================== 重启 / 换 API 弹窗 ======================
/**
 * 重启/续接前选 API 的确认弹窗：message（说明会发生什么）+ warn（会中断什么）+「API」下拉。
 * resolve({api_key_id: string|null}) 或 null（取消）；sameOk=false 时选了与当前相同的 API 会提示"没有变化"。
 */
export function restartApiDialog(api, { title, message, warn = '', okText = '重启', current = '', currentName = '', sameOk = true, local = false }) {
  return new Promise((resolve) => {
    let result = null;
    const pick = apiKeySelect(api, { current, currentName, changeHint: SWITCH_COST_HINT, local });
    const err = h('div', { class: 'field-err' });
    const ok = h('button', { class: 'btn primary', type: 'button' }, okText);
    const cancel = h('button', { class: 'btn ghost', type: 'button' }, '取消');
    ok.addEventListener('click', () => {
      err.textContent = '';
      if (pick.missing()) { err.textContent = '这个 API key 已被删除，请改选'; pick.el.focus(); return; }
      if (!sameOk && (pick.value() || '') === (current || '')) { err.textContent = 'API 没有变化（要换 key 请先在上面改选）'; pick.el.focus(); return; }
      result = { api_key_id: pick.value() || null };
      dlg.close();
    });
    cancel.addEventListener('click', () => dlg.close());
    const body = h('div', { class: 'form-grid' },
      h('p', { class: 'modal-text' }, message),
      warn ? h('div', { class: 'api-warn' }, warn) : null,
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, 'API'), h('div', { class: 'row' }, pick.el), pick.hint),
      err);
    const dlg = modal({ title, body, actions: [cancel, ok], onClose: () => resolve(result) });
    ok.focus();
  });
}
