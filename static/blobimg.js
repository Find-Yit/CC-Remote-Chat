// 聊天里的图片（docs/SPEC_V2_TUI.md §10.1）：内联 base64（小图）直接显示；ccr_ref 引用（大图，node agent 换掉了
// base64）进入视口时才带 token 头 fetch /api/sessions/<sid>/blob/<ref>，转成 objectURL 显示，失败显示占位（可重试）。
// 点击任意图片打开灯箱（lightbox.js），同一条消息 / 同一组连续工具卡里的图片可以 ←/→ 切换。
import { h, icon, safeImageSrc, fmtSize, basename } from './dom.js';
import { blobUrl, vizCtx } from './viz.js';
import { openLightbox } from './lightbox.js';

let io = null;
function observer() {
  if (io || typeof IntersectionObserver === 'undefined') return io;
  io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      startLoad(e.target);
    }
  }, { rootMargin: '600px 0px' });
  return io;
}

/** 图片描述（灯箱与缩略图共用）：{s, url?|ref?, name, mediaType, bytes, path?} */
export function imageDesc(s, block, extra = {}) {
  const src = block && block.source;
  if (!src || typeof src !== 'object') return null;
  if (src.type === 'base64') {
    const url = safeImageSrc(src.media_type, src.data);
    return url ? { s, url, mediaType: src.media_type, ...extra } : null;
  }
  if (src.type === 'ccr_ref' && typeof src.ref === 'string') {
    return { s, ref: src.ref, mediaType: src.media_type || '', bytes: src.bytes, ...extra };
  }
  return null;
}

/** 描述 → 可显示的 URL（data: / blob: / 同源 /preview/…） */
export function loadImage(d) {
  if (!d) return Promise.reject(new Error('没有图片'));
  if (d.url) return Promise.resolve(d.url);
  if (d.ref) {
    const ld = vizCtx().blobLoader;
    return ld ? Promise.resolve(ld(d)) : blobUrl(d.s, d.ref);
  }
  return Promise.reject(new Error('没有图片'));
}

function startLoad(btn) {
  const d = btn._img;
  const img = btn.querySelector('img');
  if (!d || !img) return;
  btn.classList.add('loading');
  btn.classList.remove('failed');
  loadImage(d).then((url) => {
    d.loaded = url;
    img.src = url;
  }, (e) => fail(btn, e));
}

function fail(btn, e) {
  btn.classList.remove('loading');
  btn.classList.add('failed');
  btn.title = '图片加载失败：' + ((e && e.message) || e) + '（点击重试）';
  const why = btn.querySelector('.cimg-why');
  if (why) why.textContent = '图片加载失败 · 点击重试';
}

/**
 * 一张聊天图片（缩略图按钮）。block = transcript 的 image 块；opts.path = 对应的本机文件（Read 的 file_path），
 * opts.url = 直接可用的地址（本机预览 /preview/…）。
 */
export function chatImage(s, block, opts = {}) {
  const d = opts.desc || imageDesc(s, block, { name: opts.name || (opts.path ? basename(opts.path) : ''), path: opts.path || null });
  if (!d) {
    return h('span', { class: 'chip cimg-none' }, icon('image'), block && block.source && block.source.type === 'ccr_dropped' ? '图片（重复，已省略）' : '[图片]');
  }
  const img = h('img', { alt: opts.alt || '图片', decoding: 'async', draggable: 'false' });
  const why = h('span', { class: 'cimg-why' }, d.bytes ? '图片 · ' + fmtSize(d.bytes) : '图片');
  const btn = h('button', { class: 'cimg' + (opts.cls ? ' ' + opts.cls : ''), type: 'button', title: '点击放大（灯箱）' }, img, why);
  btn._img = d;
  img.addEventListener('load', () => { btn.classList.remove('loading', 'failed', 'pending'); btn.classList.add('ready'); });
  img.addEventListener('error', () => { if (img.getAttribute('src')) fail(btn, new Error('无法解码')); });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (btn.classList.contains('failed')) { startLoad(btn); return; }
    openFromElement(btn);
  });
  if (d.url) { img.src = d.url; }
  else {
    btn.classList.add('pending');
    const o = observer();
    if (o) o.observe(btn); else startLoad(btn);
  }
  return btn;
}

/** 点击某张缩略图：同一条目（消息 / 工具卡）及相邻的连续工具卡里的图片组成一组 */
export function openFromElement(btn) {
  const item = btn.closest('.item');
  let scope = item ? [item] : [];
  if (item && item.classList.contains('k-tool')) {
    for (let p = item.previousElementSibling; p && p.classList.contains('k-tool'); p = p.previousElementSibling) scope.unshift(p);
    for (let n = item.nextElementSibling; n && n.classList.contains('k-tool'); n = n.nextElementSibling) scope.push(n);
  }
  let els = scope.flatMap((el) => [...el.querySelectorAll('.cimg')]).filter((el) => el._img && !el.classList.contains('failed'));
  if (!els.includes(btn)) els = [btn];
  openLightbox(els.map((el) => el._img), Math.max(0, els.indexOf(btn)));
}

/** Read 读图片时工具卡上的缩略图条（不展开也能看到，参考 t3 的 ViewedImagesTimelineRow） */
export function viewedImagesRow(s, blocks, path) {
  const list = blocks.filter((b) => b && b.type === 'image');
  if (!list.length) return null;
  return h('div', { class: 'viewed-imgs' }, list.map((b, k) => chatImage(s, b, { cls: 'thumb', path,
    alt: path ? basename(path) : '图片', name: path ? basename(path) : '截图' + (list.length > 1 ? ' ' + (k + 1) : '') })));
}
