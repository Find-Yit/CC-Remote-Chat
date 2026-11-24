"""输入框 ↑/↓ 历史（static/prompthist.js，SPEC §7「输入框」）：历史列表、本页发出还没写进 transcript 的消息、浏览状态。
DOM 部分（光标在第几个视觉行、真实 textarea 上的按键）在回放自测页第 1c 节。"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_js(body):
    url = (ROOT / "static" / "prompthist.js").as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestPromptHistory(unittest.TestCase):
    def test_prompts_from_items(self):
        out = run_js(r"""
const items = [
  { kind: 'user', text: '  第一条  ' }, { kind: 'text', text: '助手' }, { kind: 'cmd', command: '/model' },
  { kind: 'user', text: '' }, { kind: 'user', text: '太长被截断', truncated: true }, { kind: 'hint', text: '系统消息' },
  { kind: 'user', text: '第二条\n两行' }, null, { kind: 'user' },
];
console.log(JSON.stringify([m.promptsFromItems(items), m.promptsFromItems(null)]));
""")
        self.assertEqual(out, [["第一条", "第二条\n两行"], []])

    def test_pending(self):
        out = run_js(r"""
const r = {};
const pend = [];
m.addPending(pend, '  /model  ', 0); m.addPending(pend, '! ls', 0); m.addPending(pend, '   ', 0);
r.skipped = pend.length;
m.addPending(pend, ' 排队 A ', 2); m.addPending(pend, '排队 A', 2);
r.added = pend.map((p) => [p.text, p.n]);
// 还没写进 transcript：排在最后（相邻重复只留一条）
r.notYet = m.mergePending(['x', 'y'], pend);
// 写进了一次：第二条（同样的文字）还在等
r.once = m.mergePending(['x', 'y', '排队 A'], pend);
// 两次都写进了
r.both = m.mergePending(['x', 'y', '排队 A', 'z', '排队 A'], pend);
// 发送前 transcript 里就有同样的文字：不算对上
r.older = m.mergePending(['排队 A', 'x'], [{ text: '排队 A', n: 2 }]);
// /clear 之后 transcript 变短：仍在等；之后又来了 3 条仍没对上 → 不再列出
r.cleared = m.mergePending(['新'], [{ text: 'q', n: 5 }]);
r.stale = m.mergePending(['a', 'b', 'c', 'd', 'e'], [{ text: 'q', n: 2 }]);
r.dups = m.mergePending(['a', 'a', 'b', 'a'], []).list;
const many = [];
for (let i = 0; i < 30; i++) m.addPending(many, 'm' + i, 0);
r.cap = [many.length, many[0].text];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["skipped"], 0)                       # / 命令、! shell、空白不记
        self.assertEqual(out["added"], [["排队 A", 2], ["排队 A", 2]])
        self.assertEqual(out["notYet"]["list"], ["x", "y", "排队 A"])
        self.assertEqual(len(out["notYet"]["pending"]), 2)
        self.assertEqual(out["once"]["list"], ["x", "y", "排队 A"])
        self.assertEqual(len(out["once"]["pending"]), 1)
        self.assertEqual(out["both"]["list"], ["x", "y", "排队 A", "z", "排队 A"])
        self.assertEqual(out["both"]["pending"], [])
        self.assertEqual(out["older"]["list"], ["排队 A", "x", "排队 A"])
        self.assertEqual(out["cleared"]["list"], ["新", "q"])
        self.assertEqual(out["stale"], {"list": ["a", "b", "c", "d", "e"], "pending": []})
        self.assertEqual(out["dups"], ["a", "b", "a"])
        self.assertEqual(out["cap"], [20, "m10"])

    def test_nav(self):
        out = run_js(r"""
const r = {};
const list = ['A', 'B', 'C'];
let calls = 0;
const get = () => { calls++; return list.slice(); };
const n = new m.PromptNav();
let v = '草稿';
const up = () => { const t = n.step(-1, v, get); if (t != null) v = t; return t; };
const down = () => { const t = n.step(1, v, get); if (t != null) v = t; return t; };
r.seq = [down(), up(), up(), up(), up(), down(), down(), down(), down()];
r.calls = calls;                                      // 一次浏览只取一次列表（快照）
r.final = [v, n.active];
// 改过调出来的内容 → 退出浏览，改后的文字是新草稿
v = ''; up(); v = 'C!';
r.edited = [down(), n.active, up(), down(), v];
// 输入框里正好是最新一条：跳过它
v = 'C';
r.skipSame = [up(), down(), v];
// 没有历史
const n2 = new m.PromptNav();
r.empty = [n2.step(-1, 'x', () => []), n2.active];
// 只有一条且就是输入框里的文字
r.onlySame = [n2.step(-1, 'A', () => ['A']), n2.active];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["seq"], [None, "C", "B", "A", None, "B", "C", "草稿", None])
        self.assertEqual(out["calls"], 1)
        self.assertEqual(out["final"], ["草稿", False])
        self.assertEqual(out["edited"], [None, False, "C", "C!", "C!"])
        self.assertEqual(out["skipSame"], ["B", "C", "C"])
        self.assertEqual(out["empty"], [None, False])
        self.assertEqual(out["onlySame"], [None, False])

    def test_keys(self):
        out = run_js(r"""
const ev = (key, o = {}) => ({ key, shiftKey: false, altKey: false, metaKey: false, ctrlKey: false, isComposing: false, keyCode: 0, ...o });
console.log(JSON.stringify([
  m.historyDir(ev('ArrowUp'), false), m.historyDir(ev('ArrowDown'), false), m.historyDir(ev('Enter'), false),
  m.historyDir(ev('ArrowUp', { shiftKey: true }), false), m.historyDir(ev('ArrowUp', { altKey: true }), false),
  m.historyDir(ev('ArrowUp', { metaKey: true }), false), m.historyDir(ev('ArrowDown', { ctrlKey: true }), false),
  m.historyDir(ev('ArrowUp', { isComposing: true }), false), m.historyDir(ev('ArrowUp', { keyCode: 229 }), false),
  m.historyDir(ev('ArrowUp'), true),
]));
""")
        self.assertEqual(out, [-1, 1, 0, 0, 0, 0, 0, 0, 0, 0])

    def test_recall_into_fallback(self):
        # 没有 sizer（或输入框隐藏）时按换行符判断第一 / 最后一行
        out = run_js(r"""
const ta = (value, pos, end = pos) => ({ value, selectionStart: pos, selectionEnd: end, disabled: false, readOnly: false,
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; } });
const get = () => ['A', 'B'];
const r = [];
let t = ta('x\ny', 3), n = new m.PromptNav();
r.push(m.recallInto(t, -1, null, n, get));             // 光标在第二行
t = ta('x\ny', 1);
r.push(m.recallInto(t, -1, null, n, get), t.value, t.selectionStart);
t.selectionStart = 0; t.selectionEnd = 1;
r.push(m.recallInto(t, -1, null, n, get));             // 有选区
t.selectionEnd = 0;
r.push(m.recallInto(t, 1, null, n, get), t.selectionStart);
r.push(m.recallInto(t, 1, null, n, get), t.value);
t = ta('', 0); t.disabled = true;
r.push(m.recallInto(t, -1, null, new m.PromptNav(), get));
const sizer = { caretEdges: () => ({ first: false, last: true }) };
t = ta('一行但折行了', 6);
r.push(m.recallInto(t, -1, sizer, new m.PromptNav(), get));
console.log(JSON.stringify(r));
""")
        # 光标在第二行 → 不处理；第一行 → 最近一条 B、光标在开头；有选区 → 不处理；
        # B 已是最新一条、↓ → 回到草稿（光标在末尾）；不在浏览时 ↓ → 不处理；禁用 → 不处理；sizer 说不在第一视觉行 → 不处理
        self.assertEqual(out, [None, "B", "B", 0, None, "x\ny", 3, None, "x\ny", None, None])


if __name__ == "__main__":
    unittest.main()
