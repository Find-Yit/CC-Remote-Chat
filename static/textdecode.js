// 文本预览的解码（docs/SPEC_V2_TUI.md §10.10）：本机与服务器文件共用。纯函数，node 单测见 tests/test_textdecode.py。
//   1. BOM：UTF-8 / UTF-16LE / UTF-16BE；
//   2. 像二进制（有 NUL、开头 64KB 里控制字符过多）→ binary，绝不把二进制当文本显示成乱码；
//   3. 严格按 UTF-8 解码；失败再严格按 GB18030（GBK 的超集）解码 → 预览头标「GB18030」；
//   4. 两种都不行 → 按 UTF-8 宽松解码（坏字节显示为 �），lossy=true。
// partial=true：只拿到了文件开头（截断在多字节字符中间不算错）。
// 规则与后端 ccr/textenc.py 一致；后端据此给 /preview 响应标 charset，前端不支持 TextDecoder('gb18030') 时
// 改用 ?ccr_utf8=1 让后端转成 UTF-8（响应头 X-CCR-Charset 仍说明原编码）。

const SAMPLE = 64 * 1024;
const OK_CTRL = new Set([9, 10, 11, 12, 13, 27]);   // \t \n \v \f \r ESC

export const ENCODING_LABEL = { 'gb18030': 'GB18030', 'utf-16le': 'UTF-16LE', 'utf-16be': 'UTF-16BE', 'utf-8-bom': 'UTF-8 BOM' };

let gbOverride = null;
/** 测试用：模拟不支持 / 支持 TextDecoder('gb18030')（null = 按实际） */
export function _setGbSupport(v) { gbOverride = v; }

export function supportsEncoding(label) {
  if (label === 'gb18030' && gbOverride != null) return gbOverride;
  try { new TextDecoder(label); return true; } catch (_) { return false; }   // eslint-disable-line no-new
}

/** 有 NUL，或开头 64KB 里的控制字符（不含 \t \n \v \f \r ESC）超过 1%（至少 8 个） */
export function looksBinary(bytes) {
  const n = Math.min(bytes.length, SAMPLE);
  let ctrl = 0;
  for (let i = 0; i < n; i++) {
    const b = bytes[i];
    if (b === 0) return true;
    if ((b < 32 && !OK_CTRL.has(b)) || b === 127) ctrl++;
  }
  return ctrl > Math.max(8, Math.floor(n / 100));
}

function tryDecode(label, bytes, partial) {
  try {
    return new TextDecoder(label, { fatal: true }).decode(bytes, { stream: !!partial });
  } catch (_) { return null; }
}

/**
 * bytes（Uint8Array / ArrayBuffer）→ {text, encoding, label, binary, lossy}
 * encoding：'utf-8' | 'utf-8-bom' | 'utf-16le' | 'utf-16be' | 'gb18030' | null（binary）；label 是预览头上显示的文字（UTF-8 为空）
 */
export function decodeText(input, { partial = false } = {}) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input || new ArrayBuffer(0));
  const out = (text, encoding, extra = {}) => ({ text, encoding, label: ENCODING_LABEL[encoding] || '', binary: false, lossy: false, ...extra });
  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    return out(new TextDecoder('utf-8').decode(bytes.subarray(3), { stream: partial }), 'utf-8-bom');
  }
  if (bytes.length >= 2 && ((bytes[0] === 0xff && bytes[1] === 0xfe) || (bytes[0] === 0xfe && bytes[1] === 0xff))) {
    const enc = bytes[0] === 0xff ? 'utf-16le' : 'utf-16be';
    const t = tryDecode(enc, bytes.subarray(2), partial);
    if (t != null) return out(t, enc);
  }
  if (looksBinary(bytes)) return { text: '', encoding: null, label: '', binary: true, lossy: false };
  const u = tryDecode('utf-8', bytes, partial);
  if (u != null) return out(u, 'utf-8');
  if (supportsEncoding('gb18030')) {
    const g = tryDecode('gb18030', bytes, partial);
    if (g != null) return out(g, 'gb18030');
  }
  return out(new TextDecoder('utf-8').decode(bytes, { stream: partial }), 'utf-8', { lossy: true });
}

/**
 * 取一个文本文件并解码（本机 / 服务器预览共用）：fetch → 字节 → decodeText。
 * 后端说原文件是 GB18030（X-CCR-Charset）但这里的 TextDecoder 不支持 GB18030 时，带 ?ccr_utf8=1 重取一次（后端转码）。
 * 返回 {text, encoding, label, binary, lossy, partial, size}；HTTP 失败抛 Error（message 用后端的中文说明）。
 */
export async function fetchText(url, { credentials = 'omit', fetchFn = null } = {}) {
  const f = fetchFn || ((u, o) => fetch(u, o));
  const res = await f(url, { credentials, cache: 'no-store' });
  if (!res.ok) throw new Error(await errorText(res));
  const partial = res.headers.get('X-CCR-Partial') === '1';
  const size = Number(res.headers.get('X-CCR-Size')) || null;
  const orig = (res.headers.get('X-CCR-Charset') || '').toLowerCase();
  const buf = await res.arrayBuffer();
  if (orig === 'gb18030' && !supportsEncoding('gb18030')) {
    const res2 = await f(url + (url.includes('?') ? '&' : '?') + 'ccr_utf8=1', { credentials, cache: 'no-store' });
    if (!res2.ok) throw new Error(await errorText(res2));
    const r = decodeText(await res2.arrayBuffer(), { partial });
    return { ...r, encoding: 'gb18030', label: ENCODING_LABEL.gb18030, transcoded: true, partial, size };
  }
  return { ...decodeText(buf, { partial }), partial, size };
}

/** HTTP 错误响应 → 中文说明（后端 JSON 的 error 字段，没有就用状态码） */
export async function errorText(res) {
  let msg = 'HTTP ' + res.status;
  try { const j = await res.json(); if (j && typeof j.error === 'string' && j.error) msg = j.error; } catch (_) { /* 忽略 */ }
  return msg;
}
