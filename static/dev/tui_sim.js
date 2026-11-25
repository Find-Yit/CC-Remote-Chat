// 仅供 static/dev 回放页使用：§4.1 parse_screen 的简化 JS 版（正式解析器是后端 ccr/tui_parse.py）。
// 目的只是把 fixtures 的屏幕原文变成同结构的 state 喂给前端渲染器，不追求覆盖所有画面。

const RULE_RE = /^─{20,}\s*$/;
const TOP_RE = /^▔{20,}\s*$/;
const HINTS_RE = /(Esc to cancel|Enter to confirm|Enter to select|to adjust)/;
const ECHO_RE = /^❯ (.*)$/;
const SPINNER_RE = /^([✻✽✶✢*·✳✺⏺]) (\S.*….*)$/;
const DONE_RE = /^[✻✽✶✢*·✳✺] \S+ for \d/;
const TIP_RE = /^\s*⎿\s+Tip:/;
const LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];

function stripEdges(arr) {
  let a = 0; let b = arr.length;
  while (a < b && !arr[a].trim()) a++;
  while (b > a && !arr[b - 1].trim()) b--;
  return arr.slice(a, b);
}

function modeOf(l) {
  if (/auto mode on/.test(l)) return 'auto';
  if (/accept edits on/i.test(l)) return 'acceptEdits';
  if (/plan mode on/i.test(l)) return 'plan';
  if (/bypass permissions on/i.test(l)) return 'bypassPermissions';
  if (/manual mode on/i.test(l)) return 'manual';
  return null;
}

function findInputBox(lines) {
  for (let i = lines.length - 2; i >= 0; i--) {
    if (RULE_RE.test(lines[i]) && lines[i + 1].startsWith('❯')) {
      for (let j = i + 2; j < Math.min(lines.length, i + 14); j++) if (RULE_RE.test(lines[j])) return [i, j];
    }
  }
  return null;
}

function findDialogStart(lines) {
  for (let i = 0; i < lines.length; i++) {
    if ((RULE_RE.test(lines[i]) || TOP_RE.test(lines[i])) && lines.slice(i + 1).some((l) => HINTS_RE.test(l))) return i;
  }
  return null;
}

function indentOf(l) { return l.length - l.replace(/^\s+/, '').length; }

function numbered(body, contInto) {
  const out = [];
  let cur = null;
  for (const l of body) {
    const m = l.match(/^(\s*)(❯\s*)?(\d+)\.\s+(.+)$/);
    if (m) {
      cur = { index: out.length, n: Number(m[3]), label: m[4].trim(), desc: '', selected: !!m[2], _ind: m[1].length + (m[2] ? m[2].length : 0) };
      out.push(cur);
      continue;
    }
    if (!l.trim() || RULE_RE.test(l)) { if (RULE_RE.test(l)) cur = null; continue; }
    if (cur && indentOf(l) > cur._ind) {
      const t = l.trim();
      if (contInto === 'label') cur.label += ' ' + t; else cur.desc = cur.desc ? cur.desc + ' ' + t : t;
    } else cur = null;
  }
  for (const o of out) delete o._ind;
  return out;
}

function parseDialog(region) {
  const lines = region.slice();
  let hints = '';
  let hi = lines.length;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].trim()) continue;
    if (HINTS_RE.test(lines[i])) { hints = lines[i].trim(); hi = i; }
    break;
  }
  const body = stripEdges(lines.slice(0, hi));
  const d = { kind: 'generic', title: (body.find((l) => l.trim()) || '').trim(), lines: body, options: [], hints };
  const text = body.join('\n');
  if (/Yes, I trust this folder/.test(text)) {
    d.kind = 'trust';
    const p = body.find((l) => /^\s*\//.test(l));
    if (p) d.path = p.trim();
    for (const l of body) {
      const m = l.match(/^\s*(❯\s+)?(No, exit|Yes, I trust this folder)\s*$/);
      if (m) d.options.push({ index: d.options.length, n: null, label: m[2], desc: '', selected: !!m[1] });
    }
  } else if (/Select model/.test(text)) {
    d.kind = 'model';
    d.title = 'Select model';
    d.rows = [];
    let cur = null;
    for (const l of body) {
      const m = l.match(/^\s*(❯\s*)?(\d+)\.\s+(.+?)\s{2,}(.*)$/);
      if (m) {
        let name = m[3].trim();
        const current = /✔$/.test(name);
        name = name.replace(/\s*✔$/, '');
        cur = { n: Number(m[2]), name, desc: m[4].trim(), current, selected: !!m[1] };
        d.rows.push(cur);
        continue;
      }
      const up = l.match(/↑ (\d+) more/); if (up) { d.more_above = Number(up[1]); cur = null; continue; }
      const dn = l.match(/↓ (\d+) more/); if (dn) { d.more_below = Number(dn[1]); cur = null; continue; }
      const ef = l.match(/([○◐●◑◒◓])\s+(\w+) effort(\s*\(default\))?/);
      if (ef) { d.effort = { level: ef[2].toLowerCase(), label: ef[2] + ' effort', is_default: !!ef[3] }; cur = null; continue; }
      if (cur && l.trim() && indentOf(l) > 10) cur.desc += ' ' + l.trim();
      else cur = null;
    }
    d.more_above = d.more_above || 0;
    d.more_below = d.more_below || 0;
    d.options = d.rows.map((r, k) => ({ index: k, n: r.n, label: r.name, desc: r.desc, selected: r.selected }));
  } else if (body.some((l) => /^\s*Effort\s*$/.test(l)) && /Faster/.test(text)) {
    d.kind = 'effort';
    d.title = 'Effort';
    const caret = body.find((l) => l.includes('▲'));
    const labels = body.find((l) => /low\s+medium/.test(l));
    if (caret && labels) {
      const col = caret.indexOf('▲');
      let best = null; let bestDist = 1e9;
      for (const lv of LEVELS) {
        const re = new RegExp('\\b' + lv + '\\b');
        const m = labels.match(re);
        if (!m) continue;
        const start = m.index; const end = start + lv.length;
        const dist = col >= start && col <= end ? 0 : Math.min(Math.abs(col - start), Math.abs(col - end));
        if (dist < bestDist) { best = lv; bestDist = dist; }
      }
      d.level = best;
    } else d.level = null;
    const u = text.match(/Ultracode\s+(on|off)/);
    d.ultracode = u ? u[1] : null;
  } else if (/Do you want to /.test(text)) {
    d.kind = 'permission';
    d.tool = d.title;
    const q = body.find((l) => /Do you want to /.test(l));
    d.question = q ? q.trim() : '';
    const r1 = body.findIndex((l) => /^╌{3,}/.test(l));
    const r2 = r1 >= 0 ? body.findIndex((l, k) => k > r1 && /^╌{3,}/.test(l)) : -1;
    d.preview = r1 >= 0 && r2 > r1 ? body.slice(r1 + 1, r2).map((l) => l.replace(/^ /, '')) : [];
    const head = body.slice(body.findIndex((l) => l.trim()) + 1, r1 >= 0 ? r1 : body.length);
    const tip = []; const desc = [];
    let inTip = false;
    for (const l of head) {
      const t = l.trim();
      if (!t) continue;
      if (/^Tip:/.test(t)) { inTip = true; tip.push(t); continue; }
      if (inTip && tip.length === 1 && !/[.。]$/.test(tip[0]) && t.split(/\s+/).length <= 2) { tip[0] += ' ' + t; continue; }
      inTip = false;
      desc.push(t);
    }
    d.description = desc.join('\n');
    d.tip = tip.join(' ');
    d.options = numbered(body.slice(body.findIndex((l) => /Do you want to /.test(l)) + 1), 'label');
  } else if (/^\s*[☐☒✔]/.test(body.find((l) => l.trim()) || '')) {
    d.kind = 'question';
    const ne = body.filter((l) => l.trim());
    d.header = ne[0].replace(/^\s*[☐☒✔]\s*/, '').trim();
    d.title = d.header;
    d.question = (ne[1] || '').trim();
    d.options = numbered(body, 'desc').map((o) => ({ ...o, free_text: /^Type something/.test(o.label) }));
  } else {
    d.options = numbered(body, 'desc');
  }
  return d;
}

export function parseScreen(text) {
  let lines = String(text || '').replace(/\r/g, '').split('\n').map((l) => l.replace(/\s+$/, ''));
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  const res = { busy: false, spinner: null, mode: 'manual', status_line: null, input: { visible: false, text: '' }, live: null, dialog: null };
  const box = findInputBox(lines);
  let footer = [];
  let content;
  if (box) {
    const [lo, hi] = box;
    const inp = lines.slice(lo + 1, hi);
    const first = (inp[0] || '').replace(/^❯[  ]?/, '');
    res.input = { visible: true, text: [first, ...inp.slice(1).map((l) => l.replace(/^ {2}/, ''))].join('\n').trimEnd() };
    // 输入框里的图片占位（与 ccr/tui_parse.py 的 split_input_images 一致：有才给 images）
    const imgs = [...res.input.text.matchAll(/\[Image #(\d{1,6})\]/g)].map((m) => Number(m[1]));
    if (imgs.length) res.input.images = imgs;
    footer = lines.slice(hi + 1);
    content = lines.slice(0, lo);
  } else {
    const ds = findDialogStart(lines);
    if (ds != null) { res.dialog = parseDialog(lines.slice(ds + 1)); content = lines.slice(0, ds); }
    else content = lines;
  }
  const status = [];
  for (const l of footer) {
    if (!l.trim()) continue;
    const m = modeOf(l);
    if (m || /shift\+tab/.test(l) || /^\s*esc to interrupt/.test(l)) { if (m) res.mode = m; }
    else status.push(l.trim());
  }
  if (status.length) res.status_line = status.join('\n');
  const tail = footer.length ? footer : lines.filter((l) => l.trim()).slice(-2);
  res.busy = tail.some((l) => l.includes('esc to interrupt'));
  content = content.slice();
  while (content.length && !content[content.length - 1].trim()) content.pop();
  if (box) {
    if (content.length && /ctrl\+[a-z] to /.test(content[content.length - 1])) content.pop();
    while (content.length && /^\s{2,4}(❯\s)?\s*\/\S+\s{2,}/.test(content[content.length - 1])) content.pop();
  }
  let echo = -1;
  for (let i = content.length - 1; i >= 0; i--) if (ECHO_RE.test(content[i])) { echo = i; break; }
  let spin = -1; let tip = -1;
  for (let i = content.length - 1; i > echo; i--) {
    const m = content[i].match(SPINNER_RE);
    if (m && !DONE_RE.test(content[i])) {
      spin = i; res.spinner = m[2].trim();
      if (TIP_RE.test(content[i + 1] || '')) tip = i + 1;
      break;
    }
  }
  let body; let base;
  if (echo >= 0) {
    let j = echo + 1;
    while (j < content.length && content[j].startsWith('  ') && content[j].trim() && !content[j].trim().startsWith('⎿')) j++;
    while (j < content.length && /^\s+⎿\s+\[Image #\d{1,6}\]\s*$/.test(content[j])) j++;   // 带图片的回显（与 tui_parse 一致）
    body = content.slice(j); base = j;
  } else { body = res.busy ? content : []; base = 0; }
  const kept = [];
  body.forEach((l, k) => {
    const idx = base + k;
    if (idx === spin || idx === tip || DONE_RE.test(l)) return;
    kept.push(l);
  });
  const live = stripEdges(kept);
  res.live = live.length ? live.join('\n') : null;
  return res;
}
