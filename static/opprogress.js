// 重启 / 续接 / 新建 / 换 API 重启的进度横幅（数据 = 后端 meta.op，见 ccr/opprogress.py；app 与回放页共用）。
// 点下去立刻反馈：请求还没回来时用本地的 pending（"正在提交请求…"）；后端 op 经 SSE 到达后按阶段显示：
// 当前阶段 + 转圈 + 已用秒数 + 已完成阶段打勾；某阶段超过正常时长显示"比平时慢，仍在进行"。
// 防连点：OpGuard 在同一会话有进行中的操作时不再发请求（后端也幂等，重复请求直接返回当前进度）。
import { h, icon, spinner } from './dom.js';
import { JOBID_RE } from './dialogs.js';

export const OP_TITLE = {
  create: '正在启动 claude', restart: '正在重启', reconnect: '正在重连', resume_new_job: '正在续接',
  switch_api: '正在换 API 重启', mode: '正在重启（切换权限模式）',
};
const FAIL_TITLE = {
  create: '启动失败', restart: '重启失败', reconnect: '重连失败', resume_new_job: '续接失败',
  switch_api: '换 API 重启失败', mode: '重启失败',
};

export function opRunning(op) { return !!op && op.state === 'running'; }

export function fmtSecs(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  return s < 60 ? s + ' 秒' : Math.floor(s / 60) + ' 分 ' + String(s % 60).padStart(2, '0') + ' 秒';
}

/** 按钮上的短文字（进行中）："正在重启…" / "正在续接…" … */
export function busyLabel(kind) { return (OP_TITLE[kind] || '正在处理') + '…'; }

/**
 * 横幅的显示模型（纯函数）。op = 后端 meta.op；pending = 本地"已点击、请求还没回来" {kind, started_at}；
 * failed = 本地记录的请求失败 {kind, message, code}（后端没有 op 时才用）。now = 秒。
 * 返回 null（不显示）或 {state:'pending|running|done|failed|trust|stalled', kind, title, text, detail, steps, stepNo,
 *   elapsed, phaseElapsed, slow, slowText, error, actions:[…]}
 */
export function opModel(op, { now = Date.now() / 1000, pending = null, failed = null } = {}) {
  if (pending && !opRunning(op)) {
    return { state: 'pending', kind: pending.kind, title: busyLabel(pending.kind), text: '正在提交请求…', detail: '',
      steps: [], stepNo: '', elapsed: now - (pending.started_at || now), phaseElapsed: 0, slow: false, slowText: '', error: '', actions: [] };
  }
  if (!op) {
    if (!failed) return null;
    return { state: 'failed', kind: failed.kind, title: FAIL_TITLE[failed.kind] || '操作失败', text: failed.message || '', detail: '',
      steps: [], stepNo: '', elapsed: 0, phaseElapsed: 0, slow: false, slowText: '', error: failed.message || '',
      actions: failed.code === 'job_ended' ? ['newjob', 'dismiss'] : ['retry', 'dismiss'] };
  }
  const phases = Array.isArray(op.phases) ? op.phases : [];
  const cur = phases.find((p) => p.id === op.phase) || null;
  const idx = cur ? phases.indexOf(cur) : -1;
  // 失败时当前阶段打叉、成功时全部打勾（以 op.state 为准，阶段自己的 state 只是补充）
  const status = (p) => (op.state === 'failed' && p.id === op.phase ? 'fail' : op.state === 'done' ? 'done' : p.state);
  const steps = phases.map((p) => ({ id: p.id, label: p.label, status: status(p),
    took: p.state === 'done' && p.t0 && p.t1 ? Math.max(0, p.t1 - p.t0) : null }));
  const end = op.finished_at || now;
  const base = { kind: op.kind, steps, stepNo: idx >= 0 && phases.length ? `第 ${idx + 1}/${phases.length} 步` : '',
    elapsed: end - (op.started_at || end), phaseElapsed: now - (op.phase_started_at || now), slow: false, slowText: '',
    detail: op.detail || '', error: '', actions: [] };
  if (op.state === 'running') {
    const slow = !!cur && base.phaseElapsed > (Number(cur.slow_after) || 30);
    return { ...base, state: 'running', title: busyLabel(op.kind), text: cur ? cur.label : '', slow,
      slowText: slow ? (cur.slow_hint || '比平时慢，仍在进行') : '' };
  }
  if (op.state === 'done') return { ...base, state: 'done', title: 'claude 已就绪', text: op.detail || '用时 ' + fmtSecs(base.elapsed), detail: '' };
  if (op.state === 'trust') return { ...base, state: 'trust', title: 'claude 已启动', text: op.detail || '等待你在上方确认信任目录', detail: '', actions: ['dismiss'] };
  if (op.state === 'stalled') return { ...base, state: 'stalled', title: 'claude 已启动，但还没就绪', text: op.detail || '', detail: '', actions: ['term', 'dismiss'] };
  // failed
  return { ...base, state: 'failed', title: FAIL_TITLE[op.kind] || '操作失败', text: cur ? '卡在：' + cur.label : '', detail: '',
    error: op.error || '未知错误', actions: op.error_code === 'job_ended' ? ['newjob', 'dismiss'] : ['retry', 'dismiss'] };
}

const ACT_TEXT = { retry: '重试', newjob: '在新作业上续接', term: '打开终端面板', dismiss: '关闭' };

/** 进度横幅（挂在输入框上方，和"在新作业上续接"按钮同一位置）。update(model) 只在内容变化时重建，计时每秒原地更新。 */
export class OpBanner {
  constructor({ onAction } = {}) {
    this.onAction = onAction || (() => {});
    this.el = h('div', { class: 'op-banner', role: 'status', 'aria-live': 'polite', hidden: true });
    this.key = '';
    this.model = null;
  }

  update(m) {
    this.model = m;
    if (!m) { this.el.hidden = true; this.key = ''; this.el.replaceChildren(); return; }
    const key = JSON.stringify([m.state, m.kind, m.title, m.text, m.detail, m.error, m.slow, m.slowText, m.stepNo, m.actions,
      m.steps.map((x) => [x.id, x.label, x.status])]);
    if (key !== this.key) { this.key = key; this.render(m); }
    this.tick(m);
    this.el.hidden = false;
  }

  render(m) {
    const el = this.el;
    el.className = 'op-banner is-' + m.state;
    el.dataset.state = m.state;
    const lead = m.state === 'running' || m.state === 'pending' ? spinner()
      : icon(m.state === 'done' || m.state === 'trust' ? 'check' : m.state === 'failed' ? 'x' : 'clock', 'op-ic');
    const acts = m.actions.map((id) => {
      const b = h('button', { class: 'btn small ' + (id === 'retry' || id === 'newjob' ? 'primary' : 'ghost'), type: 'button', 'data-act': id },
        id === 'retry' || id === 'newjob' ? icon('refresh') : null, ACT_TEXT[id] || id);
      b.addEventListener('click', () => this.onAction(id, this.model));
      return b;
    });
    const head = h('div', { class: 'op-head' }, lead,
      h('strong', { class: 'op-title' }, m.title),
      m.text ? h('span', { class: 'op-text' }, m.text) : null,
      m.stepNo && (m.state === 'running' || m.state === 'failed') ? h('span', { class: 'op-step' }, m.stepNo) : null,
      m.state === 'running' || m.state === 'pending' ? h('span', { class: 'op-time', 'data-op-time': '' }) : null,
      acts.length ? h('span', { class: 'op-acts' }, acts) : null);
    const rows = [head];
    if (m.error) rows.push(h('div', { class: 'op-error' }, m.error));
    if (m.detail) rows.push(h('div', { class: 'op-detail' }, m.detail));
    if (m.slow) rows.push(h('div', { class: 'op-slow' }, icon('clock'), m.slowText));
    if (m.steps.length && m.state !== 'done' && m.state !== 'trust') {   // 成功 / 等信任：不再列阶段，横幅保持紧凑
      rows.push(h('ol', { class: 'op-steps' }, m.steps.map((x) => h('li', { class: 'op-s-' + x.status, 'data-phase': x.id },
        x.status === 'done' ? icon('check', 'op-s-ic') : x.status === 'cur' ? (m.state === 'running' ? spinner() : icon('clock', 'op-s-ic'))
          : x.status === 'fail' ? icon('x', 'op-s-ic') : h('span', { class: 'op-s-dot' }),
        h('span', { class: 'op-s-label' }, x.label),
        x.took != null ? h('span', { class: 'op-s-took' }, fmtSecs(x.took)) : x.status === 'cur' && m.state === 'running' ? h('span', { class: 'op-s-took', 'data-op-phase-time': '' }) : null))));
    }
    el.replaceChildren(...rows);
  }

  /** 每秒调用：只改计时文字（不重建、不闪） */
  tick(m = this.model) {
    if (!m) return;
    const t = this.el.querySelector('[data-op-time]');
    if (t) t.textContent = '已用 ' + fmtSecs(m.elapsed);
    const p = this.el.querySelector('[data-op-phase-time]');
    if (p) p.textContent = fmtSecs(m.phaseElapsed);
  }
}

/**
 * 前端防连点 + 本地状态：同一会话有进行中的操作（本地请求未返回，或后端 op 在进行）时 run() 直接返回 {skipped:true}，不发请求。
 * 失败（HTTP 错误、且不是会接着弹选择框的那几种）记成本地 failed，横幅显示原因 + 重试；后端 op 到达后以后端为准。
 */
export class OpGuard {
  constructor(onChange) {
    this.onChange = onChange || (() => {});
    this.pending = new Map();   // sid -> {kind, started_at}
    this.failed = new Map();    // sid -> {kind, message, code}
    this.dismissed = new Set(); // 用户关掉的（已结束的）op.id
    this.last = new Map();      // sid -> 最近一次请求参数（重试用）
  }

  busy(sid, meta) { return this.pending.has(sid) || opRunning(meta && meta.op); }

  /** 进行中的种类（按钮文字用）：本地 pending 优先，其次后端 op */
  busyKind(sid, meta) {
    const p = this.pending.get(sid);
    if (p) return p.kind;
    return opRunning(meta && meta.op) ? meta.op.kind : '';
  }

  async run(sid, kind, meta, fn, req) {
    if (this.busy(sid, meta)) return { skipped: true };
    this.pending.set(sid, { kind, started_at: Date.now() / 1000 });
    this.failed.delete(sid);
    if (req !== undefined) this.last.set(sid, req);
    this.onChange();
    try {
      return { result: await fn() };
    } finally {
      this.pending.delete(sid);
      this.onChange();
    }
  }

  fail(sid, kind, message, code = '') { this.failed.set(sid, { kind, message, code }); this.onChange(); }

  view(sid, meta, now = Date.now() / 1000) {
    let op = meta && meta.op;
    if (op && !opRunning(op) && this.dismissed.has(op.id)) op = null;
    return opModel(op, { now, pending: this.pending.get(sid) || null, failed: op ? null : this.failed.get(sid) || null });
  }

  dismiss(sid, meta) {
    const op = meta && meta.op;
    if (op && !opRunning(op)) this.dismissed.add(op.id);
    this.failed.delete(sid);
    this.onChange();
  }
}

/**
 * 「在新作业上续接」/ 改 jobid 的输入框（纯函数）。
 * 作业已结束时预填**当前设置里的作业**（与"从历史续接"一致），不再预填已结束的旧作业——以前预填旧作业，
 * 用户直接点下一步时 j === 旧 jobid 被静默忽略，界面毫无反应（"点了卡住"）。填回旧作业 / 当前作业时在框里提示原因。
 */
export function jobPromptModel(meta, cfgJob, { ended = false, dead = false } = {}) {
  const cur = String((meta && meta.jobid) || '');
  const cfg = String(cfgJob || '');
  const value = ended ? (cfg && cfg !== cur ? cfg : '') : (cur || cfg);
  return {
    title: ended ? '在新作业上续接' : '修改此会话的 jobid',
    label: ended ? `作业 ${cur} 已结束。新的作业 jobid（将在该作业上 --resume 续接本会话，对话历史不丢）`
      : '新的作业 jobid（将在该作业上 --resume 续接本会话）',
    value,
    placeholder: '例如 ' + (cfg && cfg !== cur ? cfg : '123456'),
    okText: ended || dead ? '续接' : '下一步',
    validate: (v) => (!JOBID_RE.test(v) ? 'jobid 格式不正确'
      : v === cur ? (ended ? `作业 ${cur} 已结束，请填写新的作业 jobid（可点「我的作业」选择）` : `会话已经在作业 ${cur} 上；要换作业请填写新的 jobid`) : ''),
    // 正在运行：还要确认（会退出当前 claude）；已退出：输入框本身已说明会发生什么，直接续接
    confirm: !(ended || dead),
  };
}
