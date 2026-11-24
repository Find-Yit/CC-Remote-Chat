// 主题：跟随系统（默认）/ 浅色 / 深色。
// 普通脚本（非 module），在 <head> 里于样式表之后同步加载：首帧渲染前就给 <html> 写好 data-theme，避免闪白。
// CSP 是 script-src 'self'（禁内联脚本），所以单独成文件。
// - 偏好存 localStorage「ccr.theme」：'light' | 'dark'；缺省 / 非法值 = 'system'（跟随系统）
// - <html data-theme> 永远是解析后的实际主题 light | dark（CSS 只认它）；<html data-theme-pref> 是偏好
// - 跟随系统时监听 prefers-color-scheme 变化实时切换；其它标签页改了偏好经 storage 事件同步
// - 页面上任意带 data-theme-toggle 的按钮：点击在 跟随系统 → 浅色 → 深色 间循环（图标 + tooltip 自动维护）
// - 对外：window.ccrTheme = { pref(), resolved(), set(pref, {persist}), cycle() }；变化时在 window 上派发 'ccr:theme'
(function () {
  'use strict';
  var KEY = 'ccr.theme';
  var ORDER = ['system', 'light', 'dark'];
  var LABELS = { system: '跟随系统', light: '浅色', dark: '深色' };
  // 16×16 描边图标，风格同 dom.js 的 ICONS
  var ICONS = {
    system: 'M2.5 3h11v7.5h-11z M6 13.5h4 M8 10.5v3',
    light: 'M8 5.3a2.7 2.7 0 1 0 0 5.4a2.7 2.7 0 1 0 0-5.4z M8 1.6v1.5 M8 12.9v1.5 M1.6 8h1.5 M12.9 8h1.5 M3.5 3.5l1 1 M11.5 11.5l1 1 M3.5 12.5l1-1 M11.5 4.5l1-1',
    dark: 'M13.2 9.7A5.4 5.4 0 1 1 6.3 2.8a4.3 4.3 0 0 0 6.9 6.9z',
  };
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  var pref = load();
  var ephemeral = false; // set(..., {persist:false}) 之后不再跟随 storage 事件（回放页用）

  function valid(p) { return ORDER.indexOf(p) >= 0 ? p : 'system'; }
  function load() { try { return valid(localStorage.getItem(KEY)); } catch (e) { return 'system'; } }
  function save(p) {
    try { if (p === 'system') localStorage.removeItem(KEY); else localStorage.setItem(KEY, p); } catch (e) { /* 隐私模式等：只在本页生效 */ }
  }
  function resolve(p) { return p === 'system' ? (mq && mq.matches ? 'dark' : 'light') : p; }

  function apply() {
    var t = resolve(pref);
    var prev = root.getAttribute('data-theme');
    if (prev && prev !== t) {
      // 切换那一帧关掉过渡，避免各元素以不同速度变色
      root.classList.add('theme-switching');
      var raf = window.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
      raf(function () { raf(function () { root.classList.remove('theme-switching'); }); });
    }
    root.setAttribute('data-theme', t);
    root.setAttribute('data-theme-pref', pref);
    var meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.setAttribute('content', t);
    renderToggles();
    try { window.dispatchEvent(new CustomEvent('ccr:theme', { detail: { pref: pref, theme: t } })); } catch (e) { /* 老浏览器 */ }
  }

  function renderToggle(btn) {
    var next = ORDER[(ORDER.indexOf(pref) + 1) % ORDER.length];
    var cur = pref === 'system' ? '跟随系统（当前' + LABELS[resolve(pref)] + '）' : LABELS[pref];
    var tip = '主题：' + cur + ' · 点击改为「' + LABELS[next] + '」';
    btn.title = tip;
    btn.setAttribute('aria-label', tip);
    btn.setAttribute('data-pref', pref);
    var svg = btn.querySelector('svg.ic');
    if (!svg) {
      svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', '0 0 16 16');
      svg.setAttribute('class', 'ic');
      svg.setAttribute('aria-hidden', 'true');
      svg.appendChild(document.createElementNS(SVG_NS, 'path'));
      btn.appendChild(svg);
    }
    svg.firstChild.setAttribute('d', ICONS[pref]);
  }
  function renderToggles() {
    if (!document.body) return; // <head> 阶段：等 DOMContentLoaded 再画按钮
    var list = document.querySelectorAll('[data-theme-toggle]');
    for (var i = 0; i < list.length; i++) renderToggle(list[i]);
  }

  function set(p, opts) {
    pref = valid(p);
    if (opts && opts.persist === false) ephemeral = true;
    else { ephemeral = false; save(pref); }
    apply();
  }
  function cycle() { set(ORDER[(ORDER.indexOf(pref) + 1) % ORDER.length]); }

  if (mq) {
    var onSystem = function () { if (pref === 'system') apply(); };
    if (mq.addEventListener) mq.addEventListener('change', onSystem); else if (mq.addListener) mq.addListener(onSystem);
  }
  window.addEventListener('storage', function (e) {
    if (ephemeral || (e.key !== KEY && e.key !== null)) return;
    pref = load();
    apply();
  });
  document.addEventListener('click', function (e) {
    var t = e.target;
    var btn = t && t.closest ? t.closest('[data-theme-toggle]') : null;
    if (btn) { e.preventDefault(); cycle(); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderToggles);

  // ---------------- 彩色区分（时间线不同部分的语义色，§10.6）：默认开；存 localStorage「ccr.colorful」（'0' = 关）
  // <html data-colorful="on|off">；带 data-colorful-toggle 的按钮点击切换。CSS 的彩色规则都挂在 [data-colorful="on"] 下。
  var CKEY = 'ccr.colorful';
  var CICON = 'M8 2a6 6 0 1 0 0 12c.9 0 1.3-.6 1.3-1.2 0-.8-.6-1-.6-1.8 0-.7.6-1.2 1.4-1.2H12a2 2 0 0 0 2-2C14 4.6 11.3 2 8 2z M5 7.2h.01 M6.8 4.8h.01 M9.8 4.8h.01';
  var colorEph = false;
  function loadColorful() { try { return localStorage.getItem(CKEY) !== '0'; } catch (e) { return true; } }
  var colorful = loadColorful();
  function applyColorful() {
    root.setAttribute('data-colorful', colorful ? 'on' : 'off');
    if (!document.body) return;
    var list = document.querySelectorAll('[data-colorful-toggle]');
    for (var i = 0; i < list.length; i++) {
      var btn = list[i];
      var tip = '彩色区分：' + (colorful ? '开' : '关') + ' · 点击' + (colorful ? '关闭（单色）' : '开启');
      btn.title = tip;
      btn.setAttribute('aria-label', tip);
      btn.setAttribute('aria-pressed', colorful ? 'true' : 'false');
      var svg = btn.querySelector('svg.ic');
      if (!svg) {
        svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', '0 0 16 16');
        svg.setAttribute('class', 'ic');
        svg.setAttribute('aria-hidden', 'true');
        var path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', CICON);
        svg.appendChild(path);
        btn.appendChild(svg);
      }
    }
    try { window.dispatchEvent(new CustomEvent('ccr:colorful', { detail: { on: colorful } })); } catch (e) { /* 老浏览器 */ }
  }
  function setColorful(on, opts) {
    colorful = !!on;
    if (opts && opts.persist === false) colorEph = true;
    else {
      colorEph = false;
      try { if (colorful) localStorage.removeItem(CKEY); else localStorage.setItem(CKEY, '0'); } catch (e) { /* 只在本页生效 */ }
    }
    applyColorful();
  }
  window.addEventListener('storage', function (e) {
    if (colorEph || (e.key !== CKEY && e.key !== null)) return;
    colorful = loadColorful();
    applyColorful();
  });
  document.addEventListener('click', function (e) {
    var t = e.target;
    var btn = t && t.closest ? t.closest('[data-colorful-toggle]') : null;
    if (btn) { e.preventDefault(); setColorful(!colorful); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyColorful);

  window.ccrTheme = {
    pref: function () { return pref; },
    resolved: function () { return resolve(pref); },
    set: set,
    cycle: cycle,
    colorful: function () { return colorful; },
    setColorful: setColorful,
  };
  apply();
  applyColorful();
})();
