// 新建会话对话框的「权限」默认值：按运行位置分别记住用户上次的选择（localStorage）。
// 初始值 = 默认（不加参数，等同直接敲 claude，需要时会弹权限审批）。想让本机会话默认跳过权限
// （--dangerously-skip-permissions；注意本机会话不带它启动的话，之后无法再切到 bypass），在新建对话框里选一次即被记住。纯函数 + 可注入的 storage，便于单测（tests/test_modes.py 用 node 跑）。
export const PERM_SKIP = '__skip__';
export const PERM_KEYS = { local: 'ccr.perm.local', server: 'ccr.perm.server' };
export const PERM_DEFAULTS = { local: '', server: '' };
const VALID = new Set(['', PERM_SKIP, 'acceptEdits', 'auto', 'bypassPermissions', 'manual', 'dontAsk', 'plan']);

function store(storage) {
  if (storage) return storage;
  try { return globalThis.localStorage || null; } catch (_) { return null; }
}

/** 该运行位置的权限下拉初始值：'' = 不加参数、'__skip__' = 跳过权限、其它 = --permission-mode 取值 */
export function loadPerm(target, storage) {
  const t = target === 'local' ? 'local' : 'server';
  const st = store(storage);
  try {
    const v = st ? st.getItem(PERM_KEYS[t]) : null;
    if (v !== null && v !== undefined && VALID.has(v)) return v;
  } catch (_) { /* 隐私模式等 */ }
  return PERM_DEFAULTS[t];
}

export function savePerm(target, value, storage) {
  const t = target === 'local' ? 'local' : 'server';
  if (!VALID.has(value)) return;
  const st = store(storage);
  try { if (st) st.setItem(PERM_KEYS[t], value); } catch (_) { /* 忽略 */ }
}

/** preset（从历史续接 / 调用方明确给的启动参数）优先，其次该运行位置记住的值 */
export function initialPerm(target, preset, storage) {
  const p = preset || {};
  if (p.skip_permissions) return PERM_SKIP;
  if (p.permission_mode && VALID.has(p.permission_mode)) return p.permission_mode;
  return loadPerm(target, storage);
}
