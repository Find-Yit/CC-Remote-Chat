"""时间线工具的纯函数（node 直接 import 前端模块；DOM 部分在回放自测页第 14j 节）：
- static/finder.js：会话内查找（忽略大小写、中英文、不重叠计数、条目的可搜内容、命中列表、上一个 / 下一个、计数文字、起点、重新计数后找回、位置映射）
- static/toolview.js：工具调用显示档位（四档过滤、「只看对话」每轮最后一段回复、阅读位置锚点）
- static/quote.js：引用回复（Markdown 引用拼接、文件 路径:行号 + 围栏、命令消息不带、开头引用拆分、diff 选区、去重与上限）
- static/mdtable.js：表格转 Markdown / CSV（含转义）
- static/chatwidth.js：聊天宽度档位"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_js(module, body):
    url = (ROOT / "static" / module).as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestFinder(unittest.TestCase):
    def test_fold_and_occurrences(self):
        out = run_js("finder.js", r"""
console.log(JSON.stringify({
  fold: [m.foldCase('ABC 中文 Xy'), m.foldCase('İstanbul').length, 'İstanbul'.length],
  occ: [m.occurrences('aaaa', 'aa'), m.occurrences('话题与话题', '话题'), m.occurrences('abc', ''), m.occurrences('abcabc', 'abc', 1)],
  q: [m.normQuery('  '), m.normQuery(' x '), m.normQuery('a'.repeat(300)).length, m.normQuery(null)],
}));
""")
        self.assertEqual(out["fold"], ["abc 中文 xy", 8, 8])            # 长度不变：位置能对回原文
        self.assertEqual(out["occ"], [[0, 2], [0, 3], [], [0]])          # 不重叠
        self.assertEqual(out["q"], ["", " x ", 200, ""])

    def test_item_parts(self):
        out = run_js("finder.js", r"""
const env = { md: (t) => ['MD:' + t], user: (t) => ['U:' + t] };
const P = (it) => { const p = m.itemParts(it, env); return p && p.map((x) => [x.scope, x.blocks, x.fold]); };
console.log(JSON.stringify([
  P({ kind: 'user', text: '你好' }),
  P({ kind: 'user', text: '' }),
  P({ kind: 'text', text: '**粗**' }),
  P({ kind: 'thinking', text: '想', expanded: false }),
  P({ kind: 'thinking', text: '想', redacted: true }),
  P({ kind: 'tool', name: 'Bash', input: { command: 'ls -la\nwc', description: '列目录' }, result: [{ type: 'text', text: '输出' }, { type: 'image' }] }),
  P({ kind: 'tool', name: 'Agent', input: { description: 'd', prompt: 'p' }, note: { result: '子代理结果' }, structured: { answers: { q: 'a' } } }),
  P({ kind: 'cmd', command: '/model', output: '\u001b[1mSet model\u001b[0m to `Haiku`' }),
  P({ kind: 'hint', text: '系统消息', detail: '详情' }),
  P({ kind: 'notice', text: '已中断' }),
  P({ kind: 'subnote', desc: '查资料', note: { result: '结果' } }),
  P({ kind: 'turnsum' }), P(null),
]));
""")
        self.assertEqual(out[0], [[".user-quote, .user-text", ["U:你好"], None]])
        self.assertIsNone(out[1])
        self.assertEqual(out[2], [[":scope", ["MD:**粗**"], None]])
        self.assertEqual(out[3], [[".think-body", ["想"], "thinking"]])
        self.assertIsNone(out[4])
        self.assertEqual(out[5], [[".tool-name, .tool-sum", ["Bash", "ls -la …"], None],
                                  [".tool-body", ["ls -la\nwc", "列目录", "输出"], "tool"]])
        self.assertEqual(out[6][1], [".tool-body", ["d", "p", "子代理结果", "a"], "tool"])
        self.assertEqual(out[7], [[".cmd-line", ["/model"], None], [".cmd-out", ["Set model to Haiku"], None]])
        self.assertEqual(out[8], [["summary, :scope > span", ["系统消息"], None], ["details > pre", ["详情"], "details"]])
        self.assertEqual(out[9], [[":scope > span", ["已中断"], None]])
        self.assertEqual(out[10], [[".sa-note-text", ["查资料"], None], [".sa-note-result", ["MD:结果"], "subnote"]])
        self.assertEqual(out[11:], [None, None])

    def test_matches_navigation(self):
        out = run_js("finder.js", r"""
const items = [
  { kind: 'user', id: 'a', text: 'Hello 世界 hello' },
  { kind: 'turnsum' },
  { kind: 'text', id: 'b', text: 'HELLO again' },
  { kind: 'tool', id: 'c', name: 'Bash', input: { command: 'echo hello' }, result: 'hello\nhello' },
];
const cache = new Map();
const partsOf = (it) => { if (!cache.has(it)) { const p = m.itemParts(it); if (p) for (const x of p) x.folded = x.blocks.map(m.foldCase); cache.set(it, p); } return cache.get(it); };
const r = m.buildMatches(items, 'hello', partsOf);
const capped = m.buildMatches(items, 'hello', partsOf, 3);
const list = r.matches.map((x) => [x.index, x.part, x.occ]);
const nav = [m.stepIndex(0, 5, 1), m.stepIndex(4, 5, 1), m.stepIndex(0, 5, -1), m.stepIndex(-1, 5, 1), m.stepIndex(-1, 5, -1), m.stepIndex(2, 0, 1)];
const cnt = [m.formatCount(0, 7), m.formatCount(6, 7), m.formatCount(-1, 0), m.formatCount(2, 9999, true), m.formatCount(99, 3)];
const start = [m.startIndex(r.matches, 0), m.startIndex(r.matches, 2), m.startIndex(r.matches, 3), m.startIndex(r.matches, 9), m.startIndex([], 0)];
// 重新计数后找回：同一处；那一处没了 → 其后最近的一处；都没了 → 最后一处
const prev = r.matches[3];
const again = m.buildMatches(items, 'hello', partsOf).matches;
const shorter = again.filter((x) => !(x.index === 2));
console.log(JSON.stringify({ list, n: r.matches.length, capped: [capped.matches.length, capped.capped, r.capped], nav, cnt, start,
  rel: [m.relocate(again, prev), m.relocate(shorter, prev), m.relocate(again.slice(0, 2), again[5]), m.relocate(again, null), m.relocate([], prev)],
  empty: m.buildMatches(items, '  ', partsOf).matches.length }));
""")
        # 用户消息 2 处；text 1 处；工具：标题（Bash / echo hello）1 处 + 正文（命令 1 + 输出 2）3 处
        self.assertEqual(out["list"], [[0, 0, 0], [0, 0, 1], [2, 0, 0], [3, 0, 0], [3, 1, 0], [3, 1, 1], [3, 1, 2]])
        self.assertEqual(out["capped"], [3, True, False])
        self.assertEqual(out["nav"], [1, 0, 4, 0, 4, -1])
        self.assertEqual(out["cnt"], ["1/7", "7/7", "0/0", "3/9999+", "3/3"])
        self.assertEqual(out["start"], [0, 2, 3, 6, -1])
        self.assertEqual(out["rel"], [3, 2, 1, -1, -1])
        self.assertEqual(out["empty"], 0)

    def test_locate(self):
        out = run_js("finder.js", r"""
const nodes = [{ start: 0, end: 5 }, { start: 5, end: 7 }, { start: 7, end: 12 }];   // 'Hello' '**' 'World'
console.log(JSON.stringify([m.locate(nodes, 1, 3), m.locate(nodes, 3, 6), m.locate(nodes, 5, 2), m.locate(nodes, 11, 1), m.locate(nodes, 11, 2), m.locate([], 0, 1)]));
""")
        self.assertEqual(out[0], {"si": 0, "so": 1, "ei": 0, "eo": 4})
        self.assertEqual(out[1], {"si": 0, "so": 3, "ei": 2, "eo": 2})      # 跨三个文本节点
        self.assertEqual(out[2], {"si": 1, "so": 0, "ei": 1, "eo": 2})
        self.assertEqual(out[3], {"si": 2, "so": 4, "ei": 2, "eo": 5})
        self.assertIsNone(out[4])
        self.assertIsNone(out[5])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestToolTiers(unittest.TestCase):
    def test_filter(self):
        out = run_js("toolview.js", r"""
const items = [
  { kind: 'user', id: 'u1', text: 'A' },
  { kind: 'thinking', id: 'th' },
  { kind: 'text', id: 't1', text: '中间说明' },
  { kind: 'tool', id: 'ok', done: true },
  { kind: 'tool', id: 'err', done: true, isError: true },
  { kind: 'tool', id: 'deny', done: true, isError: true, denied: true },
  { kind: 'tool', id: 'run' },
  { kind: 'tool', id: 'intr', interrupted: true },
  { kind: 'text', id: 't2', text: 'A 的最终回复' },
  { kind: 'turnsum', id: 'ts' },
  { kind: 'cmd', id: 'cmd' },
  { kind: 'user', id: 'u2', text: 'B' },
  { kind: 'text', id: 't3', text: '前半段' },
  { kind: 'user', id: 'mid', text: '中途', midTurn: true },
  { kind: 'text', id: 't4', text: '后半段' },
  { kind: 'text', id: 'err2', text: 'API Error', apiError: true },
  { kind: 'text', id: 't5', text: '最终' },
  { kind: 'hint', id: 'hi', level: 'info' }, { kind: 'hint', id: 'hw', level: 'warn' }, { kind: 'hint', id: 'he', level: 'error' },
  { kind: 'notice', id: 'no' }, { kind: 'divider', id: 'dv' }, { kind: 'subnote', id: 'sn' }, { kind: 'block', id: 'bk' },
];
const keep = m.zenKeep(items);
const shown = {};
for (const t of ['all', 'hideok', 'hideall', 'zen', 'bogus']) shown[t] = items.filter((it) => m.itemShown(it, t, { keep })).map((it) => it.id);
// Agent 卡：子代理还在跑时渲染成 running（调用方给 st）
const agentDone = { kind: 'tool', done: true };
console.log(JSON.stringify({ shown, keep: [...keep].map((x) => x.id),
  agent: [m.itemShown(agentDone, 'hideok', { st: 'running' }), m.itemShown(agentDone, 'hideok')],
  st: [m.toolState({ done: true }), m.toolState({ done: true, isError: true }), m.toolState({ denied: true }), m.toolState({ interrupted: true }), m.toolState({}), m.toolState(null)],
  hidden: [m.hiddenToolCount(items, 'hideok'), m.hiddenToolCount(items, 'hideall'), m.hiddenToolCount(items, 'all')],
  norm: [m.normTier('zen'), m.normTier('x'), m.normTier(undefined), m.TIERS.map((t) => t.id), m.tierInfo('nope').name],
  null: m.itemShown(null, 'all') }));
""")
        sh = out["shown"]
        self.assertEqual(len(sh["all"]), 24)
        self.assertEqual(sh["bogus"], sh["all"])
        self.assertNotIn("ok", sh["hideok"])
        for k in ("err", "deny", "run", "intr", "th", "t1", "ts", "cmd"):
            self.assertIn(k, sh["hideok"])
        self.assertEqual([x for x in sh["hideall"] if x in ("ok", "err", "deny", "run", "intr")], [])
        self.assertIn("t1", sh["hideall"])
        # 只看对话：你的消息（含中途发的）+ 每轮最后一段回复（中途消息之前那段也留）+ 错误 / 警告 / 中断 / 压缩
        self.assertEqual(sh["zen"], ["u1", "t2", "u2", "t3", "mid", "err2", "t5", "hw", "he", "no", "dv"])
        self.assertEqual(out["keep"], ["t2", "t3", "t5"])
        self.assertEqual(out["agent"], [True, False])
        self.assertEqual(out["st"], ["done", "error", "denied", "interrupted", "running", "running"])
        self.assertEqual(out["hidden"], [1, 5, 0])
        self.assertEqual(out["norm"], ["zen", "all", "all", ["all", "hideok", "hideall", "zen"], "全部"])
        self.assertFalse(out["null"])

    def test_anchor(self):
        out = run_js("toolview.js", r"""
const A = (top, bottom, keep) => ({ top, bottom, keep });
console.log(JSON.stringify([
  m.pickAnchor([A(-80, -10, true), A(-10, 40, false), A(40, 90, true), A(90, 200, true)]),   // 视口顶端那条会被隐藏 → 下一条
  m.pickAnchor([A(-30, 20, true), A(20, 60, true)]),                                           // 跨视口顶端、仍显示的那条
  m.pickAnchor([A(-50, -5, true), A(0, 900, false), A(900, 1400, true)]),                      // 视口里都被隐藏 → 其后第一条
  m.pickAnchor([A(-90, -40, true), A(-40, 10, false)]),                                        // 后面没有 → 之前最后一条
  m.pickAnchor([A(0, 10, false)]), m.pickAnchor([]), m.pickAnchor(null),
]));
""")
        self.assertEqual(out, [2, 0, 2, 0, -1, -1, -1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestQuote(unittest.TestCase):
    def test_markdown(self):
        out = run_js("quote.js", r"""
const cwd = '/Users/me/proj';
console.log(JSON.stringify({
  chat: m.quoteMarkdown({ kind: 'chat', text: '第一行\n\n第三行  \n' }),
  file: m.quoteMarkdown({ kind: 'file', path: cwd + '/src/a.py', line: 12, endLine: 14, text: 'def f():\n    return 1\n' }, cwd),
  one: m.quoteMarkdown({ kind: 'file', path: '/etc/hosts', line: 3, text: '127.0.0.1 localhost' }, cwd),
  noline: m.quoteMarkdown({ kind: 'file', path: cwd + '/README.md', text: '# 标题\n正文' }, cwd),
  diff: m.quoteMarkdown({ kind: 'diff', path: cwd + '/x.js', line: 5, endLine: 6, text: '-a\n+b' }, cwd),
  fence: m.quoteMarkdown({ kind: 'file', path: cwd + '/doc.md', line: 1, text: 'x\n````\ny' }, cwd),
  fenceFor: [m.fenceFor('a'), m.fenceFor('```'), m.fenceFor('`````x')],
  rel: [m.relPath(cwd + '/a/b', cwd), m.relPath(cwd + 'x/a', cwd), m.relPath('/a/b', '/'), m.relPath('/a', ''), m.relPath(cwd + '/a', cwd + '/')],
  range: [m.lineRange(3, 0), m.lineRange(3, 3), m.lineRange(3, 7), m.lineRange(0, 5)],
  lang: [m.langOf('a.PY'), m.langOf('b.tsx'), m.langOf('Makefile'), m.langOf('c.unknown')],
  src: [m.quoteSource({ path: cwd + '/a.py', line: 2, endLine: 4 }, cwd), m.quoteSource({ label: 'Claude 的回复' }), m.quoteSource({}), m.quoteSource(null)],
}));
""")
        self.assertEqual(out["chat"], "> 第一行\n>\n> 第三行")
        self.assertEqual(out["file"], "> src/a.py:12-14\n> ```python\n> def f():\n>     return 1\n> ```")
        self.assertEqual(out["one"], "> /etc/hosts:3\n> ```\n> 127.0.0.1 localhost\n> ```")
        self.assertEqual(out["noline"], "> README.md\n> # 标题\n> 正文")
        self.assertEqual(out["diff"], "> x.js:5-6\n> ```diff\n> -a\n> +b\n> ```")
        self.assertEqual(out["fence"], "> doc.md:1\n> `````markdown\n> x\n> ````\n> y\n> `````")
        self.assertEqual(out["fenceFor"], ["```", "````", "``````"])
        self.assertEqual(out["rel"], ["a/b", "/Users/me/projx/a", "/a/b", "/a", "a"])
        self.assertEqual(out["range"], [":3", ":3", ":3-7", ""])
        self.assertEqual(out["lang"], ["python", "tsx", "", ""])
        self.assertEqual(out["src"], ["a.py:2-4", "Claude 的回复", "引用", ""])

    def test_compose(self):
        out = run_js("quote.js", r"""
const qs = [{ kind: 'chat', text: '甲' }, { kind: 'chat', text: '乙\n丙' }];
console.log(JSON.stringify([
  m.composeMessage(qs, '我的问题'),
  m.composeMessage(qs, '\n\n  问题\n第二行'),
  m.composeMessage(qs, ''),
  m.composeMessage([], '只有文字'),
  m.composeMessage(qs, '/compact'),
  m.composeMessage(qs, '  !ls'),
  m.composeMessage([{ kind: 'chat', text: '   ' }], '问'),
  m.composeMessage(null, 'x'),
  [m.isCommandText('/x'), m.isCommandText(' !x'), m.isCommandText('a/b'), m.isCommandText('')],
]));
""")
        self.assertEqual(out[0], {"text": "> 甲\n\n> 乙\n> 丙\n\n我的问题", "used": True})
        self.assertEqual(out[1], {"text": "> 甲\n\n> 乙\n> 丙\n\n  问题\n第二行", "used": True})   # 只去掉开头的空行
        self.assertEqual(out[2], {"text": "> 甲\n\n> 乙\n> 丙", "used": True})                      # 只有引用也能发
        self.assertEqual(out[3], {"text": "只有文字", "used": False})
        self.assertEqual(out[4], {"text": "/compact", "used": False})                              # 命令不带引用
        self.assertEqual(out[5], {"text": "  !ls", "used": False})
        self.assertEqual(out[6], {"text": "问", "used": False})                                     # 空引用不算
        self.assertEqual(out[7], {"text": "x", "used": False})
        self.assertEqual(out[8], [True, True, False, False])

    def test_split_clean_diff_add(self):
        out = run_js("quote.js", r"""
const S = (t) => m.splitLeadingQuotes(t);
const long = 'x'.repeat(20) + '\n' + 'y'.repeat(20);
let list = [];
for (let k = 0; k < 12; k++) list = m.addQuote(list, { text: 't' + k });
const dup = m.addQuote([{ text: 'a', path: '/p', line: 1 }, { text: 'b' }], { text: 'a', path: '/p', line: 1 });
console.log(JSON.stringify({
  split: [S('> a\n> b\n\n正文'), S('> a\n>\n> b\n\n> c\n\n正文\n> 不是开头'), S('正文 > x'), S('\n> a'), S('>a\n>  b'), S('> 只有引用'), S('')],
  clean: [m.cleanQuote(' a  \r\n\r\nb\t\n\n'), m.cleanQuote(long, 25), m.cleanQuote(null)],
  diff: [m.diffQuoteText([{ sign: '-', text: 'a', oldNo: 10, newNo: 0 }, { sign: '+', text: 'b', oldNo: 0, newNo: 10 }, { sign: '', text: 'c', oldNo: 11, newNo: 11 }]),
         m.diffQuoteText([{ sign: '+', text: 'x\ny', newNo: 0, oldNo: 0 }]), m.diffQuoteText([])],
  add: [list.length, list[0].text, list[11 - 2].text, dup.map((q) => q.text)],
}));
""")
        sp = out["split"]
        self.assertEqual(sp[0], {"quotes": ["a\nb"], "rest": "正文"})
        self.assertEqual(sp[1], {"quotes": ["a\n\nb", "c"], "rest": "正文\n> 不是开头"})
        self.assertEqual(sp[2], {"quotes": [], "rest": "正文 > x"})
        self.assertEqual(sp[3], {"quotes": ["a"], "rest": ""})
        self.assertEqual(sp[4], {"quotes": ["a\n b"], "rest": ""})
        self.assertEqual(sp[5], {"quotes": ["只有引用"], "rest": ""})
        self.assertEqual(sp[6], {"quotes": [], "rest": ""})
        self.assertEqual(out["clean"][0], " a\n\nb")
        self.assertEqual(out["clean"][1], "x" * 20 + "\n…（已截断）")
        self.assertEqual(out["clean"][2], "")
        self.assertEqual(out["diff"][0], {"text": "-a\n+b\n c", "line": 10, "endLine": 11})
        self.assertEqual(out["diff"][1], {"text": "+x y", "line": 0, "endLine": 0})
        self.assertEqual(out["diff"][2], {"text": "", "line": 0, "endLine": 0})
        self.assertEqual(out["add"], [10, "t2", "t11", ["b", "a"]])   # 最多 10 条（丢最早的）；相同的去重、移到最后


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestTableCopy(unittest.TestCase):
    def test_markdown_csv(self):
        out = run_js("mdtable.js", r"""
console.log(JSON.stringify({
  md: m.tableToMarkdown(['名称', '值', '说明', '空'], [['a|b', '1,000', '含 "引号"'], ['`x|y`', '**2**', '多  空格\n换行', '', '多余']], ['left', 'right', 'center', '']),
  md2: [m.tableToMarkdown([], [['a']]), m.tableToMarkdown(['h'], null), m.mdCell('a\\|b'), m.mdCell(null), m.mdCell(' a\tb ')],
  csv: m.tableToCSV([['名称', '值'], ['a,b', 'say "hi"'], ['多\n行', ' 首尾 '], ['=1+1', ''], ['x\ry', 'z']]),
  csv2: [m.tableToCSV([]), m.tableToCSV(null), m.csvCell(3), m.csvCell(undefined), m.csvCell('"')],
}));
""")
        self.assertEqual(out["md"], "| 名称 | 值 | 说明 | 空 |\n| :--- | ---: | :---: | --- |\n"
                                    "| a\\|b | 1,000 | 含 \"引号\" |  |\n| `x\\|y` | **2** | 多 空格 换行 |  |")
        self.assertEqual(out["md2"], ["", "| h |\n| --- |", "a\\|b", "", "a b"])
        self.assertEqual(out["csv"], '名称,值\n"a,b","say ""hi"""\n多 行,首尾\n=1+1,\nx y,z')
        self.assertEqual(out["csv2"], ["", "", "3", "", '""""'])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestChatWidth(unittest.TestCase):
    def test_widths(self):
        out = run_js("chatwidth.js", r"""
console.log(JSON.stringify([m.CHAT_WIDTHS.map((w) => [w.id, w.name, w.px]), ['std', 'wide', 'wider', 'full', 'x', null].map(m.normWidth),
  ['std', 'wide', 'wider', 'full', 'bogus'].map(m.colWidth), m.CW_KEY]));
""")
        self.assertEqual(out[0], [["std", "标准", 800], ["wide", "宽", 1000], ["wider", "很宽", 1280], ["full", "全宽", None]])
        self.assertEqual(out[1], ["std", "wide", "wider", "full", "std", "std"])
        self.assertEqual(out[2], ["800px", "1000px", "1280px", "none", "800px"])
        self.assertEqual(out[3], "ccr.chatw")

    def test_theme_sync(self):
        # theme.js 首帧前写 data-chatw 的白名单与 chatwidth.js 一致
        src = (ROOT / "static" / "theme.js").read_text(encoding="utf-8")
        self.assertIn("localStorage.getItem('ccr.chatw')", src)
        for v in ("wide", "wider", "full"):
            self.assertIn("v === '%s'" % v, src)
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-chatw="wide"] { --col-w: 1000px; }', css)
        self.assertIn(':root[data-chatw="wider"] { --col-w: 1280px; }', css)
        self.assertIn(':root[data-chatw="full"] { --col-w: none; }', css)


if __name__ == "__main__":
    unittest.main()
