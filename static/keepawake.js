// 设置弹窗「防休眠」一节（SPEC §12.2）：开关 = config.json 的 keep_awake（POST /api/config，默认开启）；
// 状态 = GET /api/keepawake（后端 ccr/keepawake.py：有会话在干活时运行 /usr/bin/caffeinate -i -w <后端 pid>）。
// 手机远程访问：这是 Mac 本机的设置，只能看、不能改（后端远程口对 keep_awake 返回 403）。
import { h } from './dom.js';
import { REMOTE } from './api.js';

export const POLL_MS = 3000;

/** 状态行文字（纯函数）：st = GET /api/keepawake 的结果，on = 开关是否打开 */
export function keepAwakeText(st, on) {
  if (!on) return '已关闭：Mac 按系统设置正常睡眠。';
  if (!st) return '正在读取状态…';
  if (st.available === false) return '这台 Mac 上找不到 /usr/bin/caffeinate，无法阻止睡眠。';
  const demo = st.dry_run ? '（演示模式：不会真的运行 caffeinate）' : '';
  if (st.active) {
    if (typeof st.release_in === 'number' && (st.working || 0) === 0) {
      return '会话都空闲了，约 ' + Math.max(1, Math.ceil(st.release_in)) + ' 秒后恢复正常睡眠。' + demo;
    }
    return '正在阻止空闲睡眠：' + (st.working || 0) + ' 个会话在干活。' + demo;
  }
  return '没有会话在干活，Mac 正常睡眠。' + demo;
}

/** 返回 { el, dispose }。save(partial) = settings.js 的保存函数（返回 Promise，保存后 cfg 更新） */
export function keepAwakeSection(api, { config, save, getConfig } = {}) {
  const cfg0 = config || {};
  const box = h('input', { type: 'checkbox', name: 'keep_awake', checked: cfg0.keep_awake !== false, disabled: REMOTE });
  const row = h('label', { class: 'check settings-check' + (REMOTE ? ' disabled' : '') }, box,
    h('span', null, h('b', null, '防休眠'), h('span', { class: 'muted' }, ' — 有会话在干活时不让这台 Mac 空闲睡眠')));
  const state = h('div', { class: 'field-hint ka-status', role: 'status' });
  const el = h('div', { class: 'field ka-field' }, h('label', { class: 'field-label' }, '防休眠'), row,
    h('div', { class: 'muted small-text' }, '「在干活」与侧栏一致：Claude 正在回复，或子代理还在跑；等你审批 / 回答的会话不算。'
      + '本机与服务器会话都算（Mac 睡着了 ssh 会断）。全部空闲 60 秒后恢复正常睡眠。'
      + '只影响空闲睡眠：合上盖子、手动选「睡眠」照样会睡，显示器也照常熄灭。由本机后端运行 /usr/bin/caffeinate -i。'),
    REMOTE ? h('div', { class: 'field-hint warn' }, '这是 Mac 本机的设置，只能在 Mac 上修改。') : null,
    state);
  let st = null;
  let timer = null;
  let gone = false;
  const on = () => box.checked;
  const render = () => {
    state.textContent = keepAwakeText(st, on());
    state.classList.toggle('ok', !!(on() && st && st.active));
  };
  const load = async () => {
    try { st = await api.get('/api/keepawake'); } catch (_) { st = null; }
    if (!gone) render();
  };
  box.addEventListener('change', async () => {
    const want = box.checked;
    render();
    if (save) await save({ keep_awake: want });
    const c = getConfig ? getConfig() : null;
    if (c && typeof c.keep_awake === 'boolean' && c.keep_awake !== box.checked) box.checked = c.keep_awake;   // 保存失败：回到实际值
    await load();
  });
  render();
  load();
  timer = setInterval(load, POLL_MS);
  return { el, dispose() { gone = true; clearInterval(timer); } };
}
