// 输入框底栏的左侧控件组（SPEC §7「输入框底栏」，参考 T3 Code 的 composer footer）：
//   [ ✦ 模型 │ effort ]  [ 🛡 权限模式 ▾ ]  [ Shift+Tab ]
// - 模型和 effort 是「一个胶囊、两个点击区」：左半 = 切换模型，右半 = 调整 effort；各自 hover / title / 禁用。
// - 模式 chip 点开权限模式列表（modes.js，向上弹）；旁边保留 Shift+Tab 小按钮（mode/cycle）。
// 只在用户点击时调用回调；本模块自己不发任何请求。app.js 与回放自测页（static/dev/fixture.js）共用。
// 两部分各有自己的 key：内容不变不重建（不闪、不丢 hover）；模式部分重建时若列表正打开，就在新 chip 上原样重新打开。
import { h, icon } from './dom.js';
import { modeMenu, modeName, closeModeMenu, modeMenuAnchor } from './modes.js';

export class ComposerControls {
  /** host：底栏里的容器；ids：可选的元素 id（回放页按 id 找按钮） */
  constructor(host, { ids = {} } = {}) {
    this.host = host;
    this.ids = ids;
    this.capsule = null;
    this.modeGroup = null;   // [modeChip, cycleBtn]
    this.capKey = '';
    this.modeKey = '';
    this.opts = null;
  }

  /**
   * o = null（没有活动会话）或 {
   *   sid, model, effort,                    // 胶囊上显示的文字
   *   why,                                   // 模型/effort 不可用的原因（'' = 可用）
   *   mode, cycle, modeBlocked,              // 当前模式、本次启动的 Shift+Tab 循环、模式不能切换的原因
   *   onModel(), onEffort(), onPickMode(id, needsRestart), onCycle()
   * }
   */
  update(o) {
    this.opts = o;
    if (!o) {
      if (this.capKey || this.modeKey) {
        if (this.ownsMenu()) closeModeMenu();
        this.host.replaceChildren();
        this.capsule = null; this.modeGroup = null; this.capKey = ''; this.modeKey = '';
      }
      return;
    }
    const capKey = JSON.stringify([o.sid, o.model, o.effort, o.why || '']);
    const modeKey = JSON.stringify([o.sid, o.mode || '', (o.cycle || []).join(','), o.modeBlocked || '']);
    if (capKey !== this.capKey) {
      this.capKey = capKey;
      const el = this.buildCapsule(o);
      if (this.capsule && this.capsule.isConnected) this.capsule.replaceWith(el);
      else this.host.prepend(el);
      this.capsule = el;
    }
    if (modeKey !== this.modeKey) {
      const sameSid = this.modeKey && JSON.parse(this.modeKey)[0] === o.sid;
      const reopen = this.ownsMenu();
      if (reopen) closeModeMenu();
      this.modeKey = modeKey;
      const els = this.buildMode(o);
      if (this.modeGroup && this.modeGroup[0].isConnected) {
        this.modeGroup[0].replaceWith(els[0]);
        this.modeGroup[1].replaceWith(els[1]);
      } else this.host.append(...els);
      this.modeGroup = els;
      // 列表开着时模式/循环/可用性变了（例如在终端里按了 Shift+Tab）：在新 chip 上重新打开，内容随之刷新；换了会话就关掉
      if (reopen && sameSid) this.openMenu(els[0]);
    }
  }

  ownsMenu() {
    const a = modeMenuAnchor();
    return !!a && this.host.contains(a);
  }

  buildCapsule(o) {
    const why = o.why || '';
    const isDefaultEffort = o.effort === '默认';
    const model = h('button', { class: 'cb-btn cb-model', type: 'button', id: this.ids.model || null, disabled: !!why,
      title: why || '模型：' + o.model + '\n点击切换模型（在终端里打开 /model 面板）', 'aria-label': '模型 ' + o.model + '，切换模型' },
    icon('spark'), h('span', { class: 'cb-label' }, o.model));
    model.addEventListener('click', () => { const c = this.opts; if (c && c.onModel) c.onModel(); });
    const effort = h('button', { class: 'cb-btn cb-effort', type: 'button', id: this.ids.effort || null, disabled: !!why,
      title: why || 'effort：' + o.effort + '\n点击调整 effort（在终端里打开 /effort 面板）', 'aria-label': 'effort ' + o.effort + '，调整 effort' },
    h('span', { class: 'cb-label' }, isDefaultEffort ? 'effort 默认' : o.effort));
    effort.addEventListener('click', () => { const c = this.opts; if (c && c.onEffort) c.onEffort(); });
    return h('span', { class: 'cb-capsule' + (why ? ' is-disabled' : ''), role: 'group', 'aria-label': '模型与 effort' },
      model, h('span', { class: 'cb-div', 'aria-hidden': 'true' }), effort);
  }

  buildMode(o) {
    const mode = o.mode || '';
    const blocked = o.modeBlocked || '';
    const chip = h('button', { class: 'cb-btn cb-mode', type: 'button', id: this.ids.mode || null, 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      'data-mode': mode, title: '权限模式：' + modeName(mode) + (mode ? '（' + mode + '）' : '') + '\n来自终端模式行；点击选择模式' },
    icon('shield'), h('span', { class: 'cb-label' }, modeName(mode)), icon('chevdown', 'cb-caret'));
    chip.addEventListener('click', () => {
      if (modeMenuAnchor() === chip) { closeModeMenu(); return; }   // 再点一次 = 收起
      this.openMenu(chip);
    });
    const cycle = h('button', { class: 'cb-btn cb-btab', type: 'button', id: this.ids.cycle || null, disabled: !!blocked,
      title: blocked || '在终端里按一次 Shift+Tab 切换到下一个权限模式', 'aria-label': 'Shift+Tab：切换到下一个权限模式' },
    h('span', { class: 'cb-btab-long' }, 'Shift+Tab'), h('span', { class: 'cb-btab-short', 'aria-hidden': 'true' }, '⇧Tab'));
    cycle.addEventListener('click', () => { const c = this.opts; if (c && c.onCycle) c.onCycle(); });
    return [chip, cycle];
  }

  openMenu(chip) {
    const o = this.opts;
    if (!o) return;
    modeMenu(chip, {
      current: o.mode || '', cycle: o.cycle || [], blocked: o.modeBlocked || '',
      onPick: (id, needsRestart) => { const c = this.opts; if (c && c.onPickMode) c.onPickMode(id, needsRestart); },
      onCycle: () => { const c = this.opts; if (c && c.onCycle) c.onCycle(); },
    });
  }
}
