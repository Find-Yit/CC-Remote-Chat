// 会话统计条：上下文 / 会话总花费 / token/s。数据只来自后端 meta.stats（docs/SPEC_V2_TUI.md §4.2）：
//   上下文 = Claude Code 状态栏（用户的 statusline）里的 "25k/1M · 3%"，没有时用 transcript 最近一条回复的 usage（标"约"；
//            分母 = /context 给出的窗口 > 按模型（不在目录里的模型按 CLI 的默认 1M）/ [1m]/1M 提示 > 曾超过 200k → 1M
//            > 默认 200k，来源写在 tooltip 里）；
//   花费   = 状态栏里的 "$x"（Claude Code 自己算的），没有就显示 "—"，本程序不估价；
//   tok/s  = spinner 的 token 计数（生成中 = 最近约 4 秒的增长速度；空闲 = 上一轮平均）。
// 纯展示：没有任何按钮、不发任何请求。只建一次元素、挂在输入框底栏右侧的固定槽位里，之后原地更新（不随任何重建移动或闪烁）。
// 上下文画成圆环 + 百分比（参考 T3 Code），「上下文」与 25k / 1M 等详情在 tooltip 里；空闲 tok/s 的「上一轮」也只在 tooltip / aria-label 里。
import { h } from './dom.js';

const WARN_PCT = 80;
const CRIT_PCT = 95;

/** 25000 → 25k、1000000 → 1M、28303 → 28.3k、950 → 950 */
export function fmtTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—';
  const a = Math.abs(n);
  const trim = (v) => String(v).replace(/\.0$/, '');
  if (a >= 1e6) return trim((n / 1e6).toFixed(a >= 1e7 ? 0 : 1)) + 'M';
  if (a >= 1e3) return trim((n / 1e3).toFixed(a >= 1e5 ? 0 : 1)) + 'k';
  return String(Math.round(n));
}

/** 3 → "3%"、2.8 → "2.8%"、45.25 → "45%"（≥10 取整） */
export function fmtPct(p) {
  if (typeof p !== 'number' || !Number.isFinite(p)) return '—';
  if (p >= 10 || Number.isInteger(p)) return Math.round(p) + '%';
  return p.toFixed(1).replace(/\.0$/, '') + '%';
}

/** tok/s：< 10 保留一位小数，否则取整 */
export function fmtRate(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  return v < 10 ? v.toFixed(1).replace(/\.0$/, '') : String(Math.round(v));
}

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const group = (n) => Math.round(n).toLocaleString('en-US');

/** transcript 后备时上下文窗口（分母）的来源说明（后端 meta.stats.context_limit_source，见 ccr/stats.py） */
export function windowSourceText(limit, src, model) {
  const w = '窗口 ' + fmtTokens(limit) + '：';
  const m = typeof model === 'string' && model ? model : '';
  switch (src) {
    case 'context_cmd': return w + '来自 /context' + (m ? '（' + m + '）' : '') + '，Claude Code 自己给出的窗口';
    case 'model': return w + '按模型' + (m ? ' ' + m : '') + ' 推断（Claude Code 2.1.331 内置模型目录）';
    case 'hint': return w + '模型名 / 状态栏 / /model 面板里有 [1m] 或 1M';
    case 'observed': return w + '本会话' + (m ? ' ' + m + ' ' : '该模型') + '的上下文曾超过 200k，按 1M 计';
    case 'unknown_model': return w + '模型' + (m ? ' ' + m + ' ' : '') + '不在 Claude Code 2.1.331 内置模型目录里，按 Claude Code 的默认 1M 计；运行一次 /context 可得到准确值';
    case 'default': return w + '默认估算' + (m ? '（' + m + '）' : '') + '；运行一次 /context 可得到准确值';
    default: return w + '按模型推断';
  }
}

/** meta.stats → 要显示的内容（纯函数，回放页也用它做断言） */
export function describeStats(st, opts = {}) {
  if (!st || typeof st !== 'object') return null;
  const busy = !!opts.busy && !opts.dead;
  const used = num(st.context_used), limit = num(st.context_limit);
  let pct = num(st.context_pct);
  // transcript 后备：后端的百分比已四舍五入到 0.1，按原始值再算一次（214,632 / 1M = 21.46% 显示 21%，与 CLI 一致，不会二次进位成 22%）
  if ((pct == null || st.context_source === 'transcript') && used != null && limit) pct = Math.min(100, (used * 100) / limit);
  const approx = !!st.context_approx;
  const out = { ctx: null, cost: null, rate: null };

  if (used != null || pct != null) {
    let value = '';
    if (used != null && limit) value = fmtTokens(used) + ' / ' + fmtTokens(limit);
    else if (used != null) value = fmtTokens(used);
    const level = pct == null ? '' : pct >= CRIT_PCT ? 'crit' : pct > WARN_PCT ? 'warn' : '';
    let tip;
    if (st.context_source === 'transcript') {
      tip = '上下文（约）：' + (used != null ? group(used) + ' tokens' : '—') +
        (limit ? ' / ' + group(limit) : '') + (pct != null ? '（' + fmtPct(pct) + '）' : '') +
        (limit ? '\n' + windowSourceText(limit, st.context_limit_source, st.context_model) : '') +
        '\n已用来自 transcript 最近一条回复的 usage（input + cache_creation + cache_read）；状态栏里没有上下文信息';
    } else {
      tip = '上下文：' + (used != null ? group(used) + (limit ? ' / ' + group(limit) : '') + ' tokens' : '') +
        (pct != null ? (used != null ? '（' + fmtPct(pct) + '）' : fmtPct(pct)) : '') +
        '\n来自 Claude Code 状态栏（你的 statusline 输出）';
    }
    const pctText = pct == null ? '' : fmtPct(pct);
    // tooltip 第一行是紧凑写法（底栏只显示圆环 + 百分比，25k / 1M 在这里看）
    const head = (pct == null ? '上下文 ' : '上下文已用 ') + (approx ? '约 ' : '') + [pctText, value].filter(Boolean).join(' · ');
    out.ctx = {
      approx, value, pct, pctText, level,
      text: (approx ? '约 ' : '') + [value, pctText].filter(Boolean).join(' · '),
      tip: head + '\n' + tip,
    };
  }

  const cost = num(st.cost_usd);
  if (cost != null) {
    const text = typeof st.cost_text === 'string' && st.cost_text.startsWith('$') ? st.cost_text
      : '$' + (cost < 1 ? cost.toFixed(4) : cost.toFixed(2));
    out.cost = { text, tip: '会话总花费：' + text + '\n来自 Claude Code 状态栏（Claude Code 自己计算的数字，本程序不做估算）' };
  } else {
    out.cost = { text: '—', empty: true, tip: '会话总花费：状态栏里没有 $ 金额（未配置 statusline，或格式里不含花费）\n本程序不自行按价格估算，以免与官方数字对不上' };
  }

  const live = num(st.tok_s_live), last = num(st.tok_s_last);
  if (busy) {
    out.rate = live != null
      ? { live: true, text: fmtRate(live) + ' tok/s', tip: '生成中：spinner 上的 token 计数在最近约 4 秒内的增长速度\n（计数含 thinking token；按屏幕实际显示计算，工具执行/等待首个 token 时为 0）' }
      : { live: true, pending: true, text: '— tok/s', tip: '生成中：spinner 上还没有 token 计数' };
  } else if (last != null) {
    const lt = num(st.last_turn_tokens), le = num(st.last_turn_elapsed_s);
    out.rate = {
      live: false, text: fmtRate(last) + ' tok/s', prefix: '上一轮',
      tip: '上一轮平均：' + (lt != null && le != null ? 'spinner 最终显示 ' + group(lt) + ' tokens ÷ 用时 ' + Math.round(le) + 's ≈ ' : '') +
        fmtRate(last) + ' tok/s\n（含 thinking token 与等待首个 token 的时间）',
    };
  }
  return out;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
export const RING_R = 9.75;                         // 圆环半径（viewBox 24×24，描边 3）
export const RING_C = 2 * Math.PI * RING_R;          // 周长 = stroke-dasharray

/** 上下文圆环（参考 T3 Code ContextWindowMeter）：底槽 + 进度弧，旋转 -90° 从 12 点钟方向开始；颜色走 CSS 变量 */
function ring() {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'st-ring');
  svg.setAttribute('aria-hidden', 'true');
  const circle = (cls) => {
    const c = document.createElementNS(SVG_NS, 'circle');
    c.setAttribute('cx', '12');
    c.setAttribute('cy', '12');
    c.setAttribute('r', String(RING_R));
    c.setAttribute('fill', 'none');
    c.setAttribute('stroke-width', '3');
    c.setAttribute('class', cls);
    return c;
  };
  const fill = circle('st-ring-fill');
  fill.setAttribute('stroke-linecap', 'round');
  fill.setAttribute('stroke-dasharray', RING_C.toFixed(3));
  fill.style.strokeDashoffset = RING_C.toFixed(3);   // CSSOM 赋值（CSP 允许；不写 style 属性字符串）
  svg.append(circle('st-ring-track'), fill);
  return svg;
}

export class StatsBar {
  constructor() {
    this.parts = {
      // 圆环 + 百分比；「上下文」字样与 25k / 1M 在 tooltip 里（没有百分比时才显示 token 数）
      ctx: h('span', { class: 'st-item st-ctx' }, ring(), h('span', { class: 'st-approx' }, '约'), h('span', { class: 'st-pct' })),
      cost: h('span', { class: 'st-item st-cost' }, h('span', { class: 'st-val' })),
      rate: h('span', { class: 'st-item st-rate' }, h('span', { class: 'st-dot', 'aria-hidden': 'true' }), h('span', { class: 'st-val' })),
    };
    this.el = h('div', { class: 'stats-bar', role: 'group', 'aria-label': '会话统计', hidden: true },
      this.parts.ctx, this.parts.cost, this.parts.rate);
    this.key = '';
  }

  /** stats = meta.stats；opts = {busy, dead} */
  update(stats, opts = {}) {
    const d = describeStats(stats, opts);
    const key = JSON.stringify([d, !!opts.dead]);
    if (key === this.key) return;
    this.key = key;
    if (!d) { this.el.hidden = true; return; }
    this.el.hidden = false;
    this.el.classList.toggle('dead', !!opts.dead);
    const { ctx, cost, rate } = this.parts;

    ctx.hidden = !d.ctx;
    if (d.ctx) {
      const c = d.ctx;
      ctx.title = c.tip;
      ctx.classList.toggle('warn', c.level === 'warn');
      ctx.classList.toggle('crit', c.level === 'crit');
      ctx.classList.toggle('approx', c.approx);
      ctx.classList.toggle('no-pct', c.pct == null);
      ctx.querySelector('.st-approx').hidden = !c.approx;
      ctx.querySelector('.st-pct').textContent = c.pctText || c.value;
      ctx.querySelector('.st-ring').classList.toggle('empty', c.pct == null);
      const p = c.pct == null ? 0 : Math.max(0, Math.min(100, c.pct));
      ctx.querySelector('.st-ring-fill').style.strokeDashoffset = (RING_C * (1 - p / 100)).toFixed(3);
      ctx.setAttribute('aria-label', c.pct == null ? '上下文 ' + c.text : '上下文已用 ' + (c.approx ? '约 ' : '') + c.pctText + (c.value ? '（' + c.value + '）' : ''));
    }

    cost.title = d.cost.tip;
    cost.classList.toggle('empty', !!d.cost.empty);
    cost.querySelector('.st-val').textContent = d.cost.text;
    cost.setAttribute('aria-label', '花费 ' + d.cost.text);

    rate.hidden = !d.rate;
    if (d.rate) {
      rate.title = d.rate.tip;
      rate.classList.toggle('live', !!d.rate.live);
      rate.classList.toggle('pending', !!d.rate.pending);
      rate.querySelector('.st-dot').hidden = !d.rate.live;
      rate.querySelector('.st-val').textContent = d.rate.text;
      rate.setAttribute('aria-label', (d.rate.live ? '生成中 ' : (d.rate.prefix || '') + ' ') + d.rate.text);
    }
  }
}
