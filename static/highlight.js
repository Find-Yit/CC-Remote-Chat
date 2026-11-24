// 极简正则语法高亮（docs/SPEC_V2_TUI.md §10.7，P2）：js/ts、py、sh、json、html/xml、css。无第三方库；
// 只生成 <span class="hl-*"> + 文本节点（textContent），绝不拼 innerHTML。颜色走 CSS 变量（--hl-*，浅色/深色两套），
// 只在「彩色区分」开启时上色。
import { h } from './dom.js';

const MAX = 300000;
const STR_D = String.raw`"(?:[^"\\\n]|\\.)*"`;
const STR_S = String.raw`'(?:[^'\\\n]|\\.)*'`;
const STR_B = String.raw`\x60(?:[^\x60\\]|\\[\s\S])*\x60`;
const NUM = String.raw`\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b`;

function kw(words) { return String.raw`\b(?:` + words.trim().split(/\s+/).join('|') + String.raw`)\b`; }

const DEFS = {
  js: [
    ['c', String.raw`\/\/[^\n]*|\/\*[\s\S]*?\*\/`],
    ['s', STR_B + '|' + STR_D + '|' + STR_S],
    ['k', kw('break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new return super switch this throw try typeof var void while with yield async await of static get set from as interface type enum implements private public protected readonly declare namespace abstract')],
    ['l', kw('true false null undefined NaN Infinity')],
    ['n', NUM],
    ['f', String.raw`\b[A-Za-z_$][\w$]*(?=\s*\()`],
  ],
  py: [
    ['c', String.raw`#[^\n]*`],
    ['s', String.raw`[rRbBfFuU]{0,2}(?:"""[\s\S]*?"""|'''[\s\S]*?'''|` + STR_D + '|' + STR_S + ')'],
    ['k', kw('and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case')],
    ['l', kw('None True False self cls')],
    ['n', NUM],
    ['f', String.raw`\b[A-Za-z_]\w*(?=\s*\()`],
  ],
  sh: [
    ['c', String.raw`(?:^|(?<=\s))#[^\n]*`],
    ['s', STR_D + '|' + STR_S],
    ['v', String.raw`\$\{[^}\n]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@#?*$!-]`],
    ['k', kw('if then else elif fi for while until do done case esac function in return export local readonly set unset echo cd exit source sudo')],
    ['n', NUM],
  ],
  json: [
    ['p', STR_D + String.raw`(?=\s*:)`],
    ['s', STR_D],
    ['l', kw('true false null')],
    ['n', String.raw`-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b`],
  ],
  css: [
    ['c', String.raw`\/\*[\s\S]*?\*\/`],
    ['s', STR_D + '|' + STR_S],
    ['k', String.raw`@[\w-]+|!important`],
    ['p', String.raw`[\w-]+(?=\s*:(?!:))`],
    ['n', String.raw`#[0-9a-fA-F]{3,8}\b|-?\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg|fr|ch)?\b`],
  ],
  html: [
    ['c', String.raw`<!--[\s\S]*?-->`],
    ['t', String.raw`<\/?[A-Za-z][\w:-]*|\/?>`],
    ['a', String.raw`(?<=\s)[A-Za-z_:@][\w:.-]*(?==)`],
    ['s', STR_D + '|' + STR_S],
  ],
};

const ALIAS = {
  js: 'js', javascript: 'js', jsx: 'js', mjs: 'js', cjs: 'js', ts: 'js', typescript: 'js', tsx: 'js', node: 'js',
  py: 'py', python: 'py', python3: 'py', pyi: 'py',
  sh: 'sh', bash: 'sh', zsh: 'sh', shell: 'sh', console: 'sh', shellscript: 'sh',
  json: 'json', jsonc: 'json', jsonl: 'json', ndjson: 'json', map: 'json',
  css: 'css', scss: 'css', less: 'css', sass: 'css',
  html: 'html', htm: 'html', xml: 'html', svg: 'html', xhtml: 'html', vue: 'html', svelte: 'html',
};

const compiled = new Map();
function regexFor(lang) {
  if (compiled.has(lang)) return compiled.get(lang);
  const parts = DEFS[lang];
  const re = new RegExp(parts.map(([k, src]) => '(?<' + k + '>' + src + ')').join('|'), 'gm');
  const v = { re, keys: parts.map((p) => p[0]) };
  compiled.set(lang, v);
  return v;
}

export function langOf(name) {
  const k = String(name || '').toLowerCase().replace(/^\./, '');
  return ALIAS[k] || null;
}

/** 把 code 元素的内容换成高亮后的 span；语言不认识 / 太大时什么都不做 */
export function highlightInto(codeEl, text, lang) {
  const L = langOf(lang);
  if (!L || !codeEl || typeof text !== 'string' || !text || text.length > MAX) return false;
  const { re, keys } = regexFor(L);
  const frag = document.createDocumentFragment();
  let last = 0;
  re.lastIndex = 0;
  let m;
  let guard = 0;
  while ((m = re.exec(text)) && guard++ < 200000) {
    if (m[0] === '') { re.lastIndex++; continue; }
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    const g = m.groups || {};
    const k = keys.find((x) => g[x] !== undefined) || '';
    frag.appendChild(h('span', { class: 'hl-' + k }, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  codeEl.replaceChildren(frag);
  return true;
}
