// 图片灯箱（docs/SPEC_V2_TUI.md §10.2；缩放算法移植自 T3 Code 的 ZoomableImage，MIT）：
// - 适应窗口显示；单击在「适应窗口」与「放大（原始尺寸，已是原尺寸时 2×）」之间切换；
// - Ctrl+滚轮 / 触控板捏合缩放（passive:false 拦住页面缩放；WebKit 的捏合是 gesture* 事件），以鼠标位置为锚点；
//   + / - / 0 键缩放；拖拽平移；
// - 同组多张图片 ← / → 切换，Esc 关闭；
// - 按钮：复制图片（clipboard API，统一转 png）、下载；本机会话且知道文件路径时另有「在 Finder 中显示」「用默认应用打开」。
import { h, icon } from './dom.js';
import { isLocal, localOpen, toast } from './viz.js';
import { loadImage } from './blobimg.js';

const MAX_ZOOM = 8;
let current = null;

export function lightboxOpen() { return !!current; }
export function closeLightbox() { if (current) current.close(); }

export function openLightbox(descs, index = 0) {
  if (current) current.close();
  const list = (descs || []).filter(Boolean);
  if (!list.length) return null;
  const lb = new Lightbox(list, Math.min(Math.max(0, index), list.length - 1));
  current = lb;
  return lb;
}

class Lightbox {
  constructor(list, index) {
    this.list = list;
    this.index = index;
    this.zoom = 1;
    this.nat = { w: 0, h: 0 };
    this.url = '';
    this.drag = null;
    this.suppressClick = false;
    this.prevFocus = document.activeElement;
    this.build();
    this.show(index);
  }

  build() {
    const btn = (ic, label, fn, extra = '') => {
      const b = h('button', { class: 'lb-btn' + extra, type: 'button', title: label, 'aria-label': label }, icon(ic), h('span', { class: 'lb-btn-t' }, label));
      b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
      return b;
    };
    this.title = h('span', { class: 'lb-title' });
    this.count = h('span', { class: 'lb-count' });
    this.zoomLbl = h('span', { class: 'lb-zoom', title: '当前缩放（相对原始像素）' });
    this.btnOut = btn('minus', '缩小（-）', () => this.setZoom(this.zoom / 1.25));
    this.btnIn = btn('plus', '放大（+）', () => this.setZoom(this.zoom * 1.25));
    this.btnFit = btn('fit', '适应窗口（0）', () => this.setZoom(1));
    this.btnCopy = btn('copy', '复制图片', () => this.copy());
    this.btnDown = btn('download', '下载', () => this.download());
    this.btnReveal = btn('folder', '在 Finder 中显示', () => this.reveal('reveal'));
    this.btnOpen = btn('external', '用默认应用打开', () => this.reveal('open'));
    const closeB = btn('x', '关闭（Esc）', () => this.close(), ' lb-close');
    this.bar = h('div', { class: 'lb-bar' },
      h('div', { class: 'lb-info' }, this.title, this.count),
      h('div', { class: 'lb-tools' }, this.btnOut, this.zoomLbl, this.btnIn, this.btnFit,
        h('span', { class: 'lb-sep' }), this.btnCopy, this.btnDown, this.btnReveal, this.btnOpen, closeB));
    this.img = h('img', { class: 'lb-img', alt: '', draggable: 'false' });
    this.msg = h('div', { class: 'lb-msg' });
    this.view = h('div', { class: 'lb-view', tabindex: '0', role: 'region', 'aria-label': '可缩放的图片：单击放大/还原，Ctrl+滚轮或捏合缩放，拖拽平移' }, this.img);
    this.prev = h('button', { class: 'lb-nav prev', type: 'button', title: '上一张（←）', 'aria-label': '上一张' }, icon('chevleft'));
    this.next = h('button', { class: 'lb-nav next', type: 'button', title: '下一张（→）', 'aria-label': '下一张' }, icon('chevron'));
    this.prev.addEventListener('click', (e) => { e.stopPropagation(); this.go(-1); });
    this.next.addEventListener('click', (e) => { e.stopPropagation(); this.go(1); });
    this.stage = h('div', { class: 'lb-stage' }, this.view, this.msg, this.prev, this.next);
    this.root = h('div', { class: 'lb', role: 'dialog', 'aria-modal': 'true', 'aria-label': '图片查看器', tabindex: '-1' }, this.bar, this.stage);
    // 点击背景关闭（点图片 = 缩放）
    this.stage.addEventListener('click', (e) => { if (e.target === this.stage) this.close(); });
    this.img.addEventListener('load', () => {
      this.nat = { w: this.img.naturalWidth || 1, h: this.img.naturalHeight || 1 };
      this.msg.hidden = true;
      this.zoom = 1;
      this.layout();
      this.view.scrollLeft = 0; this.view.scrollTop = 0;
    });
    this.img.addEventListener('error', () => { if (this.img.getAttribute('src')) this.message('图片无法显示'); });
    this.view.addEventListener('click', (e) => {
      if (this.suppressClick || e.detail > 1) { this.suppressClick = false; return; }
      this.toggleZoom({ x: e.clientX, y: e.clientY });
    });
    this.onWheel = (e) => {
      if (!e.ctrlKey && !e.metaKey) return;          // 普通滚动 = 平移
      e.preventDefault();                             // 拦住页面缩放（触控板捏合 = ctrl+wheel）
      if (!e.deltaY) return;
      const delta = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? this.view.clientHeight : 1);
      this.setZoom(this.zoom * Math.exp(-delta * 0.01), { x: e.clientX, y: e.clientY });
    };
    this.root.addEventListener('wheel', this.onWheel, { passive: false });
    // Safari / WKWebView（原生 Mac 应用）的触控板捏合不发 ctrl+wheel，而是非标准的 gesturestart/gesturechange（e.scale
    // 是相对捏合开始时的倍数）；Chrome 没有这些事件，不受影响
    this.gestureBase = 0;
    this.root.addEventListener('gesturestart', (e) => { e.preventDefault(); this.gestureBase = this.zoom; });
    this.root.addEventListener('gesturechange', (e) => {
      e.preventDefault();
      if (this.gestureBase && e.scale > 0) this.setZoom(this.gestureBase * e.scale, { x: e.clientX, y: e.clientY });
    });
    this.root.addEventListener('gestureend', (e) => { e.preventDefault(); this.gestureBase = 0; });
    this.view.addEventListener('pointerdown', (e) => this.onDown(e));
    this.view.addEventListener('pointermove', (e) => this.onMove(e));
    const up = (e) => this.onUp(e);
    this.view.addEventListener('pointerup', up);
    this.view.addEventListener('pointercancel', up);
    this.onKey = (e) => this.key(e);
    this.root.addEventListener('keydown', this.onKey);
    this.onResize = () => this.layout();
    window.addEventListener('resize', this.onResize);
    document.body.appendChild(this.root);
    this.root.focus();
  }

  show(i) {
    this.index = (i + this.list.length) % this.list.length;
    const d = this.list[this.index];
    this.title.textContent = d.name || '图片';
    this.count.textContent = this.list.length > 1 ? `${this.index + 1} / ${this.list.length}` : '';
    this.prev.hidden = this.next.hidden = this.list.length < 2;
    const local = !!(d.path && d.s && isLocal(d.s));
    this.btnReveal.hidden = !local;
    // 「用默认应用打开」只对白名单类型（后端 ccr.preview.OPEN_ALLOW_EXTS 的图片部分）
    this.btnOpen.hidden = !(local && /\.(png|jpe?g|gif|webp|avif|bmp|tiff?|heic|heif|ico|svg)$/i.test(d.path));
    this.nat = { w: 0, h: 0 };
    this.img.removeAttribute('src');
    this.img.style.width = this.img.style.height = '';
    this.message('加载中…');
    const my = this.index;
    loadImage(d).then((url) => {
      if (this.index !== my || current !== this) return;
      this.url = url;
      this.img.alt = d.name || '图片';
      this.img.src = url;
    }, (e) => { if (this.index === my) this.message('图片加载失败：' + ((e && e.message) || e)); });
  }

  message(t) { this.msg.textContent = t; this.msg.hidden = !t; this.zoomLbl.textContent = ''; }

  go(step) { if (this.list.length > 1) this.show(this.index + step); }

  fit() {
    const sw = this.stage.clientWidth, sh = this.stage.clientHeight;
    if (!this.nat.w || !sw || !sh) return 1;
    return Math.min(1, (sw - 24) / this.nat.w, (sh - 24) / this.nat.h);
  }

  layout() {
    if (!this.nat.w) return;
    const f = this.fit() * this.zoom;
    const w = Math.max(1, Math.round(this.nat.w * f)), hh = Math.max(1, Math.round(this.nat.h * f));
    this.img.style.width = w + 'px';
    this.img.style.height = hh + 'px';
    this.view.classList.toggle('zoomed', this.zoom > 1.001);
    this.zoomLbl.textContent = Math.round(f * 100) + '%';
  }

  setZoom(next, point) {
    const z = Math.min(MAX_ZOOM / Math.max(this.fit(), 1e-6), Math.max(1, next));
    if (!this.nat.w || Math.abs(z - this.zoom) < 1e-4) { this.layout(); return; }
    const v = this.view;
    const b = v.getBoundingClientRect();
    const x = point ? point.x - b.left : v.clientWidth / 2;
    const y = point ? point.y - b.top : v.clientHeight / 2;
    const ax = (v.scrollLeft + x) / this.zoom, ay = (v.scrollTop + y) / this.zoom;
    const cx = b.left + x, cy = b.top + y;
    this.zoom = z;
    this.layout();
    const nb = v.getBoundingClientRect();
    v.scrollLeft = ax * z - (cx - nb.left);
    v.scrollTop = ay * z - (cy - nb.top);
  }

  /** 单击：适应窗口 ↔ 原始尺寸（图片本就不大于窗口时 ↔ 2×） */
  toggleZoom(point) {
    if (this.zoom > 1.001) { this.setZoom(1, point); return; }
    const f = this.fit();
    this.setZoom(f < 0.99 ? 1 / f : 2, point);
  }

  onDown(e) {
    this.suppressClick = false;
    if (e.pointerType !== 'mouse' || e.button !== 0 || this.zoom <= 1.001) return;
    this.drag = { id: e.pointerId, x: e.clientX, y: e.clientY, l: this.view.scrollLeft, t: this.view.scrollTop };
    this.view.setPointerCapture(e.pointerId);
    this.view.classList.add('dragging');
  }

  onMove(e) {
    const d = this.drag;
    if (!d || d.id !== e.pointerId) return;
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > 4) this.suppressClick = true;
    this.view.scrollLeft = d.l - (e.clientX - d.x);
    this.view.scrollTop = d.t - (e.clientY - d.y);
  }

  onUp(e) {
    if (!this.drag || this.drag.id !== e.pointerId) return;
    if (this.view.hasPointerCapture(e.pointerId)) this.view.releasePointerCapture(e.pointerId);
    this.drag = null;
    this.view.classList.remove('dragging');
  }

  key(e) {
    if (e.isComposing) return;
    const k = e.key;
    let done = true;
    if (k === 'Escape') this.close();
    else if (k === 'ArrowLeft' && !e.metaKey && !e.ctrlKey) this.go(-1);
    else if (k === 'ArrowRight' && !e.metaKey && !e.ctrlKey) this.go(1);
    else if ((k === '+' || k === '=') && !e.metaKey && !e.ctrlKey) this.setZoom(this.zoom * 1.25);
    else if (k === '-' && !e.metaKey && !e.ctrlKey) this.setZoom(this.zoom / 1.25);
    else if (k === '0' && !e.metaKey && !e.ctrlKey) this.setZoom(1);
    else if ((k === 'Enter' || k === ' ') && e.target === this.view) this.toggleZoom();
    else if (k === 'Tab') { done = false; this.trapTab(e); }
    else done = false;
    if (done) { e.preventDefault(); e.stopPropagation(); }   // 不让 Esc 再关掉子代理抽屉等
  }

  trapTab(e) {
    const f = [...this.root.querySelectorAll('button:not([hidden]), [tabindex="0"]')].filter((x) => x.offsetParent !== null);
    if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
  }

  async pngBlob() {
    const res = await fetch(this.url);
    const blob = await res.blob();
    if (blob.type === 'image/png') return blob;
    const bmp = await createImageBitmap(blob);
    const c = document.createElement('canvas');
    c.width = bmp.width; c.height = bmp.height;
    c.getContext('2d').drawImage(bmp, 0, 0);
    return await new Promise((resolve, reject) => c.toBlob((b) => (b ? resolve(b) : reject(new Error('转换失败'))), 'image/png'));
  }

  async copy() {
    if (!this.url) return;
    try {
      if (!navigator.clipboard || typeof ClipboardItem === 'undefined') throw new Error('浏览器不支持复制图片');
      const blob = await this.pngBlob();
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      toast('已复制图片');
    } catch (e) { toast('复制图片失败：' + ((e && e.message) || e), 'error'); }
  }

  download() {
    if (!this.url) return;
    const d = this.list[this.index];
    const ext = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' }[d.mediaType] || 'png';
    const name = d.name && /\.[a-z0-9]{2,5}$/i.test(d.name) ? d.name : (d.name || 'image') + '.' + ext;
    const a = h('a', { href: this.url, download: name.replace(/[\\/:*?"<>|]/g, '_') });
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  reveal(action) {
    const d = this.list[this.index];
    if (d && d.path) localOpen(d.s, d.path, action);   // 用户点击触发
  }

  close() {
    if (current === this) current = null;
    window.removeEventListener('resize', this.onResize);
    this.root.remove();
    if (this.prevFocus && this.prevFocus.isConnected && this.prevFocus.focus) {
      try { this.prevFocus.focus({ preventScroll: true }); } catch (_) { /* 忽略 */ }
    }
  }
}
