// 子代理托盘的「停止」/「全部停止」（SPEC §7「子代理：停止」）：
// 每个运行中的子代理旁边一个「停止」（点一下变成「确认停止」，4 秒内再点才发请求）；托盘标题处「全部停止」（弹窗二次确认）。
// 后端在终端底部的子代理列表里选中它按 x / 在输入框里按两次 Ctrl+X Ctrl+K（ccr/tuiact.py，每一步带 guard）；
// 子代理随后由 transcript / meta 变成「已终止」（ccr/subagents.py），托盘自动移除。只在用户点击时发请求。
import { h, icon, spinner } from './dom.js';

const ARM_MS = 4000;

/** 现在能不能停（不能 → 原因，'' = 可以）：已退出 / 只读 / 断线 / 有对话框 / 终端输入框里有文字 */
export function stopBlockReason(s) {
  if (!s || s.readOnly) return '只读';
  const m = s.meta || {};
  if (m.state === 'exited' || m.state === 'starting' || m.state === 'error') return '会话没有在运行';
  if (m.link && m.link !== 'ready') return '与终端的连接未就绪';
  const st = (s.screen && s.screen.state) || {};
  if (st.dialog || m.state === 'waiting' || m.state === 'trust') return '终端里有对话框，请先处理';
  const inp = st.input || {};
  if ((inp.text || '').trim() || inp.placeholder || (Array.isArray(inp.images) && inp.images.length)) {
    return '终端输入框里有文字（或排队的消息），为了不误操作，请先发送或清空';
  }
  return '';
}

export class AgentStopper {
  /** api：Api 实例；toast(text, level)；confirm(title, message, opts) → Promise<bool>；onChange()：状态变了，请重画托盘 */
  constructor({ api, toast, confirm, onChange }) {
    this.api = api;
    this.toast = toast || (() => {});
    this.confirm = confirm || (async () => false);
    this.onChange = onChange || (() => {});
    this.state = new Map();   // 'sid:aid' | 'sid:*' -> { phase: 'armed' | 'pending', timer }
  }

  key(s, aid) { return s.sid + ':' + aid; }

  set(k, v) {
    const old = this.state.get(k);
    if (old && old.timer) clearTimeout(old.timer);
    if (v) this.state.set(k, v); else this.state.delete(k);
    this.onChange();
  }

  /** 托盘需要重画的依据：各按钮状态 + 现在能不能停 */
  stateKey(s) {
    const pre = s ? s.sid + ':' : '';
    return JSON.stringify([stopBlockReason(s), [...this.state].filter(([k]) => k.startsWith(pre)).map(([k, v]) => [k, v.phase])]);
  }

  /** 托盘某一项：原来的按钮 + 「停止」 */
  wrapItem(s, a, itemBtn) {
    const k = this.key(s, a.id);
    const cur = this.state.get(k);
    const why = stopBlockReason(s);
    const desc = (a.summary && a.summary.description) || a.id;
    let btn;
    if (cur && cur.phase === 'pending') {
      btn = h('button', { class: 'sa-stop pending', type: 'button', disabled: true, title: '正在终端里停止这个子代理…' }, spinner(), '停止中');
    } else {
      const armed = !!cur && cur.phase === 'armed';
      btn = h('button', { class: 'sa-stop' + (armed ? ' armed' : ''), type: 'button', disabled: !!why,
        'aria-label': '停止子代理「' + desc + '」',
        title: why || (armed ? '再点一次确认：在终端的子代理列表里选中它并按 x（和你在终端里操作一样，无法恢复）'
          : '停止这个子代理（终端子代理列表里的 x；点了之后需要再点一次确认）') },
      icon('stop'), armed ? '确认停止' : '停止');
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (stopBlockReason(s)) return;
        const now = this.state.get(k);
        if (!now) { this.set(k, { phase: 'armed', timer: setTimeout(() => { if ((this.state.get(k) || {}).phase === 'armed') this.set(k, null); }, ARM_MS) }); return; }
        if (now.phase === 'armed') this.stopOne(s, a.id, desc);
      });
    }
    itemBtn.removeAttribute('role');
    return h('div', { class: 'sa-row', role: 'listitem' }, itemBtn, btn);
  }

  /** 托盘标题行：原来的折叠按钮 + 「全部停止」 */
  wrapHead(s, headBtn, running) {
    const k = this.key(s, '*');
    const pending = (this.state.get(k) || {}).phase === 'pending';
    const why = stopBlockReason(s);
    const btn = h('button', { class: 'sa-stopall' + (pending ? ' pending' : ''), type: 'button', disabled: pending || !!why,
      title: why || '停止全部后台子代理（终端里按两次 Ctrl+X Ctrl+K；会先让你确认）' },
    pending ? spinner() : icon('stop'), pending ? '停止中' : '全部停止');
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (pending || stopBlockReason(s)) return;
      const n = running.length;
      const ok = await this.confirm('停止全部后台子代理？',
        `会在终端里按两次 Ctrl+X Ctrl+K（和你在终端里操作一样），正在运行的 ${n} 个子代理${n > 1 ? '都' : ''}会被停止，已经做了一半的工作不会继续，无法恢复。`,
        { okText: '全部停止', danger: true });
      if (ok) this.stopAll(s);
    });
    return h('div', { class: 'sa-tray-headrow' }, headBtn, btn);
  }

  async stopOne(s, aid, desc) {
    const k = this.key(s, aid);
    this.set(k, { phase: 'pending' });
    try {
      const r = await this.api.post(this.api.sessionPath(s.sid, '/agents/stop'), { agent_id: aid });
      if (r && r.warning) this.toast(r.warning, 'warn');
      else this.toast('已停止子代理「' + desc + '」', 'info');
    } catch (e) {
      this.toast('没有停止：' + ((e && e.message) || e), e && e.status === 409 ? 'warn' : 'error');
    } finally {
      this.set(k, null);
    }
  }

  async stopAll(s) {
    const k = this.key(s, '*');
    this.set(k, { phase: 'pending' });
    try {
      const r = await this.api.post(this.api.sessionPath(s.sid, '/agents/stop_all'), {});
      if (r && r.warning) this.toast(r.warning, 'warn');
      else this.toast('已停止全部后台子代理', 'info');
    } catch (e) {
      this.toast('没有停止：' + ((e && e.message) || e), e && e.status === 409 ? 'warn' : 'error');
    } finally {
      this.set(k, null);
    }
  }
}
