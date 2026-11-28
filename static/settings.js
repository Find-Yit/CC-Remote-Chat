// 设置弹窗（侧栏品牌行的齿轮按钮）：服务器 SSH 主机、提示音、默认 base_url（API key 用量）。
// 声音由本地后端用 /usr/bin/afplay 播放（与 Claude Code CLI 的 Stop hook 同一个声音，默认 Blow）；浏览器不播放。
// 设置存 app 的 config.json（POST /api/config，后端白名单校验）；「试听」= POST /api/notify/test。
import { h, icon, spinner } from './dom.js';
import { modal, toast } from './dialogs.js';
import { updateSettingsField } from './update.js';
import { BASE_URL_RE, setDefaultBaseUrl, loadUsage } from './apikeys.js';
import { sysNotifySection } from './sysnotify.js';   // 系统通知（只在原生窗口里可用，SPEC §12.1）
import { keepAwakeSection } from './keepawake.js';   // 防休眠（SPEC §12.2）
import { chatWidthField } from './chatwidth.js';   // 聊天宽度（只存在本机 localStorage，立即生效）

// dmg 版自动更新：app.js 在后端开启了自动更新时调用（之后设置弹窗多一节「更新」）；没调用时与以前完全相同
let updateStore = null;
export function setUpdateStore(store) { updateStore = store || null; }
// 额外的一节（私有版的手机远程访问）：fn(api) → { el, dispose }；没设置时与以前完全相同
let extraSection = null;
export function setExtraSection(fn) { extraSection = typeof fn === 'function' ? fn : null; }

export function settingsDialog(api, { config, onSaved }) {
  const update = updateStore;
  return new Promise((resolve) => {
    let cfg = { ...(config || {}) };
    const status = h('span', { class: 'muted small-text settings-status' });
    const mk = (name, label, desc) => {
      const box = h('input', { type: 'checkbox', name, checked: cfg[name] !== false });
      const row = h('label', { class: 'check settings-check' }, box, h('span', null, h('b', null, label), desc ? h('span', { class: 'muted' }, ' — ' + desc) : null));
      return { box, row };
    };
    const enabled = mk('notify_enabled', '提示音', '总开关');
    const onDone = mk('notify_on_done', '完成时', 'Claude 做完一轮回复（用户中断的不响）');
    const onAttn = mk('notify_on_attention', '需要我操作时', '权限审批、提问（AskUserQuestion）、计划审批、信任目录等对话框');
    const sound = h('select', { class: 'input', name: 'notify_sound', 'aria-label': '提示音', disabled: true },
      h('option', { value: cfg.notify_sound || 'Blow' }, cfg.notify_sound || 'Blow'));
    const test = h('button', { class: 'btn ghost', type: 'button', title: '用所选声音播放一次' }, icon('bell'), '试听');
    const localNote = h('div', { class: 'muted small-text' });
    // 服务器：ssh 主机别名（~/.ssh/config 的 Host）。留空 = 只用本机模式。回车或离开输入框时保存。
    const hostInput = h('input', { class: 'input mono', type: 'text', name: 'host', 'aria-label': 'SSH 主机别名',
      value: cfg.host || '', placeholder: '例如 my-cluster（留空 = 只用本机）', maxlength: '253', autocomplete: 'off', spellcheck: 'false' });
    const saveHost = () => {
      const v = hostInput.value.trim();
      if (v === (cfg.host || '')) return;
      save({ host: v });
    };
    hostInput.addEventListener('keydown', (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') { e.preventDefault(); saveHost(); }
    });
    hostInput.addEventListener('change', saveHost);
    const done = h('button', { class: 'btn primary', type: 'button' }, '完成');
    // 默认 base_url：只存在本程序的 config.json（不读写 ~/.claude）；没有单独 base_url 的已保存 API key 用它
    const baseUrl = h('input', { class: 'input mono', type: 'text', name: 'default_base_url', 'aria-label': '默认 base_url', value: cfg.default_base_url || '',
      placeholder: 'https://…', autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false' });
    const baseSave = h('button', { class: 'btn ghost', type: 'button' }, '保存');
    const baseErr = h('div', { class: 'field-err' });

    const syncDisabled = () => {
      const off = !enabled.box.checked;
      onDone.box.disabled = off;
      onAttn.box.disabled = off;
      onDone.row.classList.toggle('disabled', off);
      onAttn.row.classList.toggle('disabled', off);
    };
    let saveSeq = 0;
    const save = async (partial) => {
      const my = ++saveSeq;
      status.replaceChildren(spinner(), ' 保存中…');
      try {
        const r = await api.post('/api/config', partial);
        cfg = r && typeof r === 'object' ? r : { ...cfg, ...partial };
        if (onSaved) onSaved(cfg);
        if (my === saveSeq) status.textContent = '已保存';
      } catch (e) {
        if (my === saveSeq) status.textContent = '保存失败：' + e.message;
        toast('保存设置失败：' + e.message, 'error');
      }
    };
    for (const c of [enabled, onDone, onAttn]) {
      c.box.addEventListener('change', () => { syncDisabled(); save({ [c.box.name]: c.box.checked }); });
    }
    sound.addEventListener('change', () => save({ notify_sound: sound.value }));
    const saveBase = async () => {
      const v = baseUrl.value.trim();
      baseErr.textContent = '';
      if (v === (cfg.default_base_url || '')) return;
      if (v && (v.length > 200 || !BASE_URL_RE.test(v))) { baseErr.textContent = 'base_url 格式不正确（形如 http://host:port 或 https://host/path）'; return; }
      await save({ default_base_url: v });
      if ((cfg.default_base_url || '') === v) {
        setDefaultBaseUrl(v);
        loadUsage(api).catch(() => {});   // 地址变了，后端用量缓存自动失效
      }
    };
    baseSave.addEventListener('click', saveBase);
    baseUrl.addEventListener('change', saveBase);
    baseUrl.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); saveBase(); } });
    test.addEventListener('click', async () => {
      test.disabled = true;
      try {
        const r = await api.post('/api/notify/test', { sound: sound.value });
        status.textContent = '正在播放 ' + (r && r.sound ? r.sound : sound.value);
      } catch (e) { toast('试听失败：' + e.message, 'error'); }
      setTimeout(() => { test.disabled = false; }, 600);
    });
    done.addEventListener('click', () => dlg.close());

    const sysn = sysNotifySection();
    const kawake = keepAwakeSection(api, { config: cfg, save, getConfig: () => cfg });
    const body = h('div', { class: 'form-grid' },
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '服务器（SSH 主机别名）'), hostInput,
        h('div', { class: 'field-hint' }, '服务器会话 = ssh <主机> → srun --jobid=<作业> --overlap（需要 Slurm 集群，且这台 Mac 能免密 ssh 登录）。只用本机模式可以留空。')),
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '提示音'),
        enabled.row, onDone.row, onAttn.row),
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '声音'),
        h('div', { class: 'row' }, sound, test),
        h('div', { class: 'field-hint' }, '/System/Library/Sounds 里的系统声音；默认 Blow')),
      h('div', { class: 'muted small-text' }, '声音由本机后端用 /usr/bin/afplay 播放（服务器会话也在这台 Mac 上响）。app 启动或重连后的首次状态同步不响；同一时刻多个会话最多响一次。'),
      localNote,
      sysn ? sysn.el : null, kawake.el,
      h('div', { class: 'field' }, h('label', { class: 'field-label' }, '默认 base_url（API key）'),
        h('div', { class: 'row' }, baseUrl, baseSave),
        h('div', { class: 'field-hint' }, '已保存的 API key 没有单独填写 base_url 时用这个地址：查询近 7 天用量（GET <base_url>/v1/usage），'
          + '以及选这样的 key 启动会话时写进 key 文件（ANTHROPIC_BASE_URL）。留空 = 不设置：没有地址的 key 沿用 settings.json 的 ANTHROPIC_BASE_URL。'
          + '只保存在本程序的 config.json，不读写 ~/.claude；选「默认（settings.json）」启动的会话不受影响。'),
        baseErr),
      h('div', { class: 'row' }, status));
    body.prepend(chatWidthField().el);   // 聊天宽度：标准 / 宽 / 很宽 / 全宽（纯前端）
    // dmg 版自动更新（只有后端开启时 update 才不为 null）
    const upd = update ? updateSettingsField(api, update, { onError: (e) => toast('操作失败：' + e.message, 'error') }) : null;
    if (upd) body.appendChild(upd.el);
    const extra = extraSection ? extraSection(api) : null;
    if (extra) body.appendChild(extra.el);
    if (extra && extra.hideTest) test.hidden = true;
    const dlg = modal({ title: '设置', body, actions: [done], onClose: () => { if (upd) upd.dispose(); if (extra) extra.dispose(); if (sysn) sysn.dispose(); kawake.dispose(); resolve(cfg); } });
    syncDisabled();
    api.get('/api/notify').then((info) => {
      const names = Array.isArray(info.sounds) ? info.sounds : [];
      const cur = cfg.notify_sound || info.default_sound || 'Blow';
      const opts = names.length ? names : [cur];
      sound.replaceChildren(...opts.map((n) => h('option', { value: n, selected: n === cur }, n + (n === info.default_sound ? '（默认）' : ''))));
      if (!opts.includes(cur)) sound.prepend(h('option', { value: cur, selected: true }, cur + '（不存在）'));
      sound.disabled = !names.length;
      test.disabled = !info.available || !names.length;
      if (!info.available) test.title = '提示音不可用';
      localNote.textContent = '本机会话：如果本机 ~/.claude/settings.json 的 Stop hook 已经会播放声音（afplay），“完成时”不再重复播放，“需要我操作时”照常。'
        + (info.local_stop_hook ? '（已检测到本机 Stop hook 配置了 afplay）' : '');
    }, (e) => { localNote.textContent = '读取可选声音失败：' + e.message; });
  });
}
