"""右侧「代码改动」栏（static/changes.js，SPEC §10.4「改动」标签）：用 node 跑纯函数与 ChangeTracker。
数据只来自前端已有的 transcript 条目（render.js 的 s.items / s.subagents）；DOM 部分（面板、联动、懒渲染、颜色）在回放自测页第 14b 节。"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 与 render.js 的工具条目同形的构造器（items 里 kind:'user' 分轮、kind:'tool' 是工具调用）
PRELUDE = r"""
let seq = 0;
const user = (text, ts) => ({ kind: 'user', text, ts: ts ?? null, id: 'u' + (++seq) });
const tool = (name, input, structured, extra = {}) => ({ kind: 'tool', toolId: 't' + (++seq), name, input, structured: structured ?? undefined,
  result: extra.result ?? 'ok', done: extra.done ?? true, isError: !!extra.isError, denied: !!extra.denied, interrupted: !!extra.interrupted,
  truncated: !!extra.truncated, ts: extra.ts ?? null });
const patch = (oldStart, newStart, lines) => {
  const o = lines.filter((l) => l[0] !== '+').length, n = lines.filter((l) => l[0] !== '-').length;
  return [{ oldStart, oldLines: o, newStart, newLines: n, lines }];
};
const pick = (f) => ({ rel: f.rel, dir: f.dir, name: f.name, n: f.edits.length, add: f.add, del: f.del, created: f.created, sub: f.sub, main: f.main, approx: f.approx, notebook: f.notebook });
"""


def run_js(body):
    url = (ROOT / "static" / "changes.js").as_uri()
    js = "const m = await import(%s);\n%s\n%s" % (json.dumps(url), PRELUDE, body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestChangesPure(unittest.TestCase):
    def test_compact_and_text(self):
        out = run_js(r"""
console.log(JSON.stringify({
  c: [0, 7, 999, 1000, 1234, 9950, 15400, 999499, 999500, 2500000, 3e9, -3, 'x'].map(m.compactCount),
  st: [m.statText(12, 1), m.statText(0, 4), m.statText(3, 0), m.statText(0, 0), m.statText(1234, 0)],
  lbl: [m.turnLabel({ n: 0 }), m.turnLabel({ n: 3, text: '一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十多出来的字', ts: null }),
        m.turnLabel({ n: 2, text: 'short', ts: null })],
}));
""")
        self.assertEqual(out["c"], ["0", "7", "999", "1k", "1.2k", "9.9k", "15k", "999k", "1m", "2.5m", "3b", "0", "0"])
        self.assertEqual(out["st"], ["+12 −1", "−4", "+3", "", "+1.2k"])
        self.assertEqual(out["lbl"][0], "第一条消息之前")
        self.assertEqual(out["lbl"][1], "第 3 轮 · " + "一二三四五六七八九十" * 3 + "…")
        self.assertEqual(out["lbl"][2], "第 2 轮 · short")

    def test_paths(self):
        out = run_js(r"""
console.log(JSON.stringify({
  abs: [m.absPath('/a/b/../c.py', '/x'), m.absPath('src/x.js', '/repo/'), m.absPath('./y.md', '/repo'), m.absPath('~/n.txt', '/repo'),
        m.absPath('rel.txt', '~/proj'), m.absPath('rel.txt', ''), m.absPath('', '/repo')],
  rel: [m.relPath('/repo/src/x.js', '/repo'), m.relPath('/repo/src/x.js', '/repo/'), m.relPath('/repox/y', '/repo'),
        m.relPath('/etc/hosts', '/repo'), m.relPath('/a/b', '/'), m.relPath('/repo/x', '')],
  split: [m.splitPath('src/ui/app.js'), m.splitPath('README.md'), m.splitPath('/abs/f')],
}));
""")
        self.assertEqual(out["abs"], ["/a/c.py", "/repo/src/x.js", "/repo/y.md", "~/n.txt", "~/proj/rel.txt", "rel.txt", ""])
        self.assertEqual(out["rel"], ["src/x.js", "src/x.js", "/repox/y", "/etc/hosts", "/a/b", "/repo/x"])
        self.assertEqual(out["split"], [{"dir": "src/ui/", "name": "app.js"}, {"dir": "", "name": "README.md"}, {"dir": "/abs/", "name": "f"}])

    def test_edit_of(self):
        out = run_js(r"""
const r = {};
const sp = patch(5, 5, [' </head>', '-<p>hi</p>', '+<h1>t</h1>', '+<p>hi!</p>']);
const keep = (e) => ({ label: e.label, add: e.add, del: e.del, created: e.created, approx: e.approx, lineNos: e.lineNos, hunks: !!e.hunks, content: e.content != null, strings: e.old != null });
// Edit：structuredPatch（带行号）
r.edit = keep(m.editOf('Edit', { file_path: '/r/a.html', old_string: '<p>hi</p>', new_string: 'x' }, { filePath: '/r/a.html', structuredPatch: sp }));
// Write 新建：structuredPatch 为空、originalFile null → 用 content 生成全新增
r.create = keep(m.editOf('Write', { file_path: '/r/n.py', content: 'a\nb\nc\n' }, { type: 'create', filePath: '/r/n.py', content: 'a\nb\nc\n', structuredPatch: [], originalFile: null }));
// Write 覆盖：有 structuredPatch
r.update = keep(m.editOf('Write', { file_path: '/r/n.py', content: 'z' }, { type: 'update', structuredPatch: patch(1, 1, ['-a', '+z']), originalFile: 'a' }));
// Write 覆盖、内容未变（structuredPatch 为空）→ 0
r.same = keep(m.editOf('Write', { file_path: '/r/n.py', content: 'a' }, { type: 'update', structuredPatch: [], originalFile: 'a' }));
// Write 覆盖、没有 patch 但有 originalFile → LCS
r.updOrig = keep(m.editOf('Write', { file_path: '/r/n.py', content: 'a\nB\nc' }, { type: 'update', structuredPatch: null, originalFile: 'a\nb\nc' }));
// 没有结构化结果：按结果文字（Orca 规则）判断新建 / 覆盖（修正：以前没有 structured 一律当新建）
r.noStCreated = keep(m.editOf('Write', { file_path: '/r/q', content: 'x\ny' }, null, { resultText: 'File created successfully at: /r/q' }));
r.noStUpdated = keep(m.editOf('Write', { file_path: '/r/q', content: 'x\ny' }, null, { resultText: 'The file /r/q has been updated successfully.' }));
// 截断：Write 内容超过 2 万字被截断 → 行数可能不准
r.trunc = keep(m.editOf('Write', { file_path: '/r/big', content: 'l\n'.repeat(100) }, { type: 'create', content: 'l\n'.repeat(100), structuredPatch: [], originalFile: null }, { truncated: true }));
// 有 structuredPatch 时即使条目被截断（originalFile 等），行数仍准确
r.truncPatch = keep(m.editOf('Edit', { file_path: '/r/a' }, { structuredPatch: sp }, { truncated: true }));
// Edit 没有 structuredPatch（旧 transcript）→ LCS，无行号
r.lcs = keep(m.editOf('Edit', { file_path: '/r/a', old_string: 'a\nb\nc', new_string: 'a\nB\nc\nd' }, null));
// MultiEdit 没有 structuredPatch
r.multi = keep(m.editOf('MultiEdit', { file_path: '/r/a', edits: [{ old_string: 'x', new_string: 'y' }, { old_string: 'p', new_string: 'p\nq' }] }, null));
// NotebookEdit：toolUseResult 的 old_source / new_source
r.nb = m.editOf('NotebookEdit', { notebook_path: '/r/n.ipynb', cell_id: 'c1', new_source: 'print(2)\nx = 1' },
  { new_source: 'print(2)\nx = 1', old_source: 'print(1)', cell_id: 'c1', cell_type: 'code', notebook_path: '/r/n.ipynb', original_file: '{}', updated_file: '{}' });
r.nbInsert = keep(m.editOf('NotebookEdit', { notebook_path: '/r/n.ipynb', edit_mode: 'insert', new_source: 'a\nb' }, { new_source: 'a\nb', cell_type: 'markdown' }));
r.nbDelete = keep(m.editOf('NotebookEdit', { notebook_path: '/r/n.ipynb', edit_mode: 'delete', cell_id: 'c2' }, { old_source: 'x\ny\nz', new_source: '' }));
r.nbNoOld = keep(m.editOf('NotebookEdit', { notebook_path: '/r/n.ipynb', new_source: 'q' }, null));
r.path = [m.editPath({ file_path: '/a' }, null), m.editPath({ notebook_path: '/n.ipynb' }, null), m.editPath({}, { filePath: '/st' }), m.editPath(null, null)];
r.lines = [m.editLines(m.editOf('Edit', {}, { structuredPatch: sp })), m.editLines(m.editOf('Write', { content: 'a\nb' }, { type: 'create', structuredPatch: [] }))];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["edit"], {"label": "Edit", "add": 2, "del": 1, "created": False, "approx": False, "lineNos": True, "hunks": True, "content": False, "strings": False})
        self.assertEqual(out["create"], {"label": "Write（新建）", "add": 3, "del": 0, "created": True, "approx": False, "lineNos": True, "hunks": False, "content": True, "strings": False})
        self.assertEqual(out["update"], {"label": "Write（覆盖）", "add": 1, "del": 1, "created": False, "approx": False, "lineNos": True, "hunks": True, "content": False, "strings": False})
        self.assertEqual((out["same"]["add"], out["same"]["del"], out["same"]["created"]), (0, 0, False))
        self.assertEqual((out["updOrig"]["add"], out["updOrig"]["del"], out["updOrig"]["strings"], out["updOrig"]["approx"]), (1, 1, True, False))
        self.assertTrue(out["noStCreated"]["created"])
        self.assertEqual((out["noStCreated"]["add"], out["noStCreated"]["approx"]), (2, False))
        self.assertFalse(out["noStUpdated"]["created"])
        self.assertEqual((out["noStUpdated"]["label"], out["noStUpdated"]["approx"]), ("Write（覆盖，旧内容未知）", True))
        self.assertEqual((out["trunc"]["add"], out["trunc"]["approx"]), (100, True))
        self.assertEqual((out["truncPatch"]["add"], out["truncPatch"]["approx"]), (2, False))
        self.assertEqual((out["lcs"]["add"], out["lcs"]["del"], out["lcs"]["lineNos"], out["lcs"]["strings"]), (2, 1, False, True))
        self.assertEqual((out["multi"]["add"], out["multi"]["del"]), (2, 1))
        nb = out["nb"]
        self.assertEqual((nb["add"], nb["del"], nb["notebook"], nb["approx"]), (2, 1, {"cell": "c1", "type": "code", "mode": "replace"}, False))
        self.assertEqual((out["nbInsert"]["add"], out["nbInsert"]["del"], out["nbInsert"]["label"]), (2, 0, "NotebookEdit（插入单元格）"))
        self.assertEqual((out["nbDelete"]["add"], out["nbDelete"]["del"]), (0, 3))
        self.assertTrue(out["nbNoOld"]["approx"])
        self.assertEqual(out["path"], ["/a", "/n.ipynb", "/st", ""])
        self.assertEqual(out["lines"], [5, 3])

    def test_file_changes_turn_card(self):
        out = run_js(r"""
const ts = [
  tool('Edit', { file_path: '/r/a.py' }, { structuredPatch: patch(1, 1, ['-a', '+b']) }),
  tool('Read', { file_path: '/r/a.py' }, null),
  tool('Edit', { file_path: '/r/a.py' }, { structuredPatch: patch(3, 3, ['+c', '+d']) }),
  tool('Write', { file_path: '/r/x.txt', content: 'hi' }, 'User rejected tool use', { denied: true, isError: true }),
  tool('Write', { file_path: '/r/y.txt', content: 'hi' }, null, { isError: true, result: 'Error: permission' }),
  tool('Edit', { file_path: '/r/z.txt', old_string: 'a', new_string: 'b' }, null, { done: false }),
  tool('Write', { file_path: '/r/w.txt', content: 'one\ntwo' }, null, { result: 'File created successfully at: /r/w.txt' }),
];
const fc = m.fileChanges(ts);
console.log(JSON.stringify(fc.map((c) => [c.path, c.edits.length, c.add, c.del, c.edits.some((e) => e.created), c.toolIds.length])));
""")
        self.assertEqual(out, [["/r/a.py", 2, 3, 1, False, 2], ["/r/w.txt", 1, 2, 0, True, 1]])

    def test_sidechain_tools(self):
        out = run_js(r"""
const E = [
  { type: 'user', message: { role: 'user', content: '子代理任务' }, isSidechain: true },
  { type: 'assistant', timestamp: '2026-11-24T06:00:00Z', isSidechain: true, message: { content: [
    { type: 'tool_use', id: 's1', name: 'Write', input: { file_path: '/r/sub.py', content: 'x\ny\n' } },
    { type: 'tool_use', id: 's2', name: 'Bash', input: { command: 'ls' } },
    { type: 'tool_use', id: 's3', name: 'Edit', input: { file_path: '/r/a.py' } } ] } },
  { type: 'user', isSidechain: true, toolUseResult: { type: 'create', filePath: '/r/sub.py', content: 'x\ny\n', structuredPatch: [] },
    message: { content: [{ type: 'tool_result', tool_use_id: 's1', content: 'File created successfully at: /r/sub.py' }] } },
  { type: 'user', isSidechain: true, toolUseResult: 'User rejected tool use', toolDenialKind: 'user-rejected',
    message: { content: [{ type: 'tool_result', tool_use_id: 's3', content: 'rejected', is_error: true }] } },
  { type: 'user', isSidechain: true, message: { content: [{ type: 'tool_result', tool_use_id: 's2', content: 'a' }] } },
];
console.log(JSON.stringify(m.sidechainTools(E).map((t) => [t.toolId, t.name, t.done, t.isError, t.denied, m.toolOk(t), t.ts, !!t.structured])));
""")
        self.assertEqual(out, [["s1", "Write", True, False, False, True, 1795500000, True],
                               ["s3", "Edit", True, True, True, False, 1795500000, False]])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestChangeTracker(unittest.TestCase):
    def test_turns_incremental_and_aggregate(self):
        out = run_js(r"""
const r = {};
const s = { items: [], subagents: new Map(), absCwd: '/repo' };
const T = new m.ChangeTracker();
const e1 = tool('Write', { file_path: '/repo/src/new.py', content: 'a\nb\nc' }, { type: 'create', structuredPatch: [], originalFile: null, content: 'a\nb\nc' }, { ts: 100 });
s.items.push(user('第一轮：新建文件', 90), e1);
r.v0 = T.version; r.c1 = T.update(s); r.v1 = T.version;
r.c1b = T.update(s); r.v1b = T.version;           // 没变化：版本不变
// 第二轮：一个还没结果的 Edit（pending），一个失败的，一个 Bash
const e2 = tool('Edit', { file_path: 'src/new.py' }, undefined, { done: false, ts: 200 });
const e3 = tool('Edit', { file_path: '/repo/README.md' }, undefined, { isError: true, ts: 201 });
s.items.push({ kind: 'text', text: '好的' }, { kind: 'turnsum', tools: [e1] }, user('第二轮：改一下', 190), e2, e3, tool('Bash', { command: 'ls' }, null));
r.c2 = T.update(s);
r.pending = T.pending.length;
r.files2 = T.aggregate('all', '/repo').files.map(pick);
// 结果到达（render.js 的 applyToolResult 原地改条目）→ 下一次 update 计入，不用重扫
e2.done = true; e2.structured = { structuredPatch: patch(2, 2, ['-b', '+B', '+b2']) };
r.c3 = T.update(s);
r.pending3 = T.pending.length;
const all = T.aggregate('all', '/repo');
r.all = { files: all.files.map(pick), add: all.add, del: all.del, edits: all.edits };
r.same = T.aggregate('all', '/repo') === all;    // 缓存
r.t1 = T.aggregate(1, '/repo').files.map(pick);
r.t2 = T.aggregate(2, '/repo').files.map(pick);
r.turns = T.turnsWithEdits().map((x) => [x.turn.n, x.turn.text, x.files, x.add, x.del]);
r.order = all.files[0].edits.map((e) => [e.turn, e.label]);
r.turnOf = [T.turnOfTool(e1.toolId), T.turnOfTool(e2.toolId), T.turnOfTool('nope')];
// 中断且没有结果的调用不会一直挂在 pending 里
const e4 = tool('Edit', { file_path: '/repo/x' }, undefined, { done: false });
s.items.push(e4); T.update(s); e4.interrupted = true; T.update(s);
r.pending4 = T.pending.length;
// reset（/clear 或重新载入）：items 整体换成新数组 → 从头重建
const before = T.version;
s.items = [user('新会话', 500), tool('Edit', { file_path: '/repo/only.txt' }, { structuredPatch: patch(1, 1, ['+z']) })];
r.c5 = T.update(s); r.reset = T.version > before;
r.afterReset = T.aggregate('all', '/repo').files.map((f) => f.rel);
r.turnsAfter = T.turns.length;
console.log(JSON.stringify(r));
""")
        self.assertEqual((out["v0"] < out["v1"], out["c1"], out["c1b"], out["v1b"]), (True, True, False, out["v1"]))
        self.assertTrue(out["c2"])
        self.assertEqual(out["pending"], 1)
        self.assertEqual([f["rel"] for f in out["files2"]], ["src/new.py"])
        self.assertTrue(out["c3"])
        self.assertEqual(out["pending3"], 0)
        self.assertEqual(out["all"]["files"], [{"rel": "src/new.py", "dir": "src/", "name": "new.py", "n": 2, "add": 5, "del": 1,
                                                "created": True, "sub": False, "main": True, "approx": False, "notebook": False}])
        self.assertEqual((out["all"]["add"], out["all"]["del"], out["all"]["edits"]), (5, 1, 2))
        self.assertTrue(out["same"])
        self.assertEqual([(f["n"], f["add"], f["created"]) for f in out["t1"]], [(1, 3, True)])
        self.assertEqual([(f["n"], f["add"], f["del"], f["created"]) for f in out["t2"]], [(1, 2, 1, False)])
        self.assertEqual(out["turns"], [[2, "第二轮：改一下", 1, 2, 1], [1, "第一轮：新建文件", 1, 3, 0]])
        self.assertEqual(out["order"], [[1, "Write（新建）"], [2, "Edit"]])
        self.assertEqual(out["turnOf"], [1, 2, None])
        self.assertEqual(out["pending4"], 0)
        self.assertTrue(out["c5"] and out["reset"])
        self.assertEqual(out["afterReset"], ["only.txt"])
        self.assertEqual(out["turnsAfter"], 2)

    def test_subagents(self):
        out = run_js(r"""
const r = {};
const agentCall = tool('Agent', { description: '改配置', prompt: 'x' }, { status: 'async_launched', agentId: 'ag1' });
const s = { items: [user('第一轮', 10), user('第二轮：派子代理', 20), agentCall,
  tool('Edit', { file_path: '/repo/shared.py' }, { structuredPatch: patch(1, 1, ['-a', '+b']) }, { ts: 21 })], subagents: new Map(), absCwd: '/repo' };
const T = new m.ChangeTracker();
T.update(s);
const ent = (i, type, extra) => ({ type, isSidechain: true, uuid: 'e' + i, timestamp: new Date((30 + i) * 1000).toISOString(), ...extra });
const a = { id: 'ag1', gen: 1, summary: { tool_use_id: agentCall.toolId, description: '改配置', agent_type: 'general-purpose', status: 'running' },
  entries: [ent(0, 'user', { message: { content: '任务' } }),
    ent(1, 'assistant', { message: { content: [{ type: 'tool_use', id: 'x1', name: 'Write', input: { file_path: '/repo/conf/new.toml', content: 'a = 1\n' } }] } })],
  base: 0, total: 2, serverBase: null, stale: false };
s.subagents.set('ag1', a);
r.c1 = T.update(s);
r.n1 = T.aggregate('all', '/repo').files.length;           // 还没有结果：不算
a.entries.push(ent(2, 'user', { toolUseResult: { type: 'create', structuredPatch: [], content: 'a = 1\n', originalFile: null },
  message: { content: [{ type: 'tool_result', tool_use_id: 'x1', content: 'File created successfully at: /repo/conf/new.toml' }] } }),
  ent(3, 'assistant', { message: { content: [{ type: 'tool_use', id: 'x2', name: 'Edit', input: { file_path: '/repo/shared.py' } }] } }),
  ent(4, 'user', { toolUseResult: { structuredPatch: patch(5, 5, ['+c']) }, message: { content: [{ type: 'tool_result', tool_use_id: 'x2', content: 'ok' }] } }));
a.total = 5;
r.c2 = T.update(s);
r.c2b = T.update(s);
const all = T.aggregate('all', '/repo');
r.files = all.files.map(pick);
r.shared = all.files.find((f) => f.rel === 'shared.py').edits.map((e) => [e.turn, e.sub ? e.sub.label : null]);
r.t2 = T.aggregate(2, '/repo').files.map((f) => f.rel);    // 子代理的改动归到派它的那一轮
r.incomplete0 = T.incompleteAgents();
// 页面打开时子代理已经在跑：条目从中间开始（base > 0）→ 提示可能没计入
s.subagents.set('ag2', { id: 'ag2', gen: 1, summary: { tool_use_id: 'unknown', description: '后来的' }, entries: [], base: 7, total: 9, serverBase: null, stale: false });
T.update(s);
r.incomplete1 = T.incompleteAgents();
// 子代理被移除 → 它的改动也去掉
s.subagents.delete('ag1');
r.c3 = T.update(s);
r.afterRemove = T.aggregate('all', '/repo').files.map((f) => [f.rel, f.sub]);
// 找不到 Agent 调用时按时间归轮
const T2 = new m.ChangeTracker();
const s2 = { items: [user('一', 10), user('二', 100)], subagents: new Map([['z', { id: 'z', gen: 1, summary: { tool_use_id: 'gone' }, base: 0, total: 2, stale: false,
  entries: [ent(0, 'assistant', { message: { content: [{ type: 'tool_use', id: 'w', name: 'Write', input: { file_path: '/p/f' } }] } }),
    ent(1, 'user', { message: { content: [{ type: 'tool_result', tool_use_id: 'w', content: 'File created successfully at: /p/f' }] } })] }]]) };
T2.update(s2);
r.byTs = T2.allEdits().map((e) => e.turn);
console.log(JSON.stringify(r));
""")
        self.assertTrue(out["c1"])
        self.assertEqual(out["n1"], 1)
        self.assertTrue(out["c2"])
        self.assertFalse(out["c2b"])
        self.assertEqual(out["files"], [
            {"rel": "conf/new.toml", "dir": "conf/", "name": "new.toml", "n": 1, "add": 1, "del": 0, "created": True, "sub": True, "main": False, "approx": False, "notebook": False},
            {"rel": "shared.py", "dir": "", "name": "shared.py", "n": 2, "add": 2, "del": 1, "created": False, "sub": True, "main": True, "approx": False, "notebook": False},
        ])
        self.assertEqual(out["shared"], [[2, None], [2, "改配置"]])
        self.assertEqual(out["t2"], ["conf/new.toml", "shared.py"])
        self.assertEqual(out["incomplete0"], 0)
        self.assertEqual(out["incomplete1"], 1)
        self.assertTrue(out["c3"])
        self.assertEqual(out["afterRemove"], [["shared.py", False]])
        self.assertEqual(out["byTs"], [1])   # 30s 在第一轮（10s）与第二轮（100s）之间

    def test_notebook_and_relative_cwd(self):
        out = run_js(r"""
const s = { items: [user('改笔记本', 1),
  tool('NotebookEdit', { notebook_path: 'nb/analysis.ipynb', cell_id: 'c3', new_source: 'x = 2\ny = 3' }, { old_source: 'x = 1', new_source: 'x = 2\ny = 3', cell_id: 'c3', cell_type: 'code' }),
  tool('Edit', { file_path: '/elsewhere/z.py' }, { structuredPatch: patch(1, 1, ['-q', '+r']) })], subagents: new Map() };
const T = m.trackerOf(s);
T.update(s);
const same = m.trackerOf(s) === T;
const agg = T.aggregate('all', '/home/u/proj');
console.log(JSON.stringify({ same, files: agg.files.map(pick) }));
""")
        self.assertTrue(out["same"])
        self.assertEqual(out["files"], [
            {"rel": "/elsewhere/z.py", "dir": "/elsewhere/", "name": "z.py", "n": 1, "add": 1, "del": 1, "created": False, "sub": False, "main": True, "approx": False, "notebook": False},
            {"rel": "nb/analysis.ipynb", "dir": "nb/", "name": "analysis.ipynb", "n": 1, "add": 2, "del": 1, "created": False, "sub": False, "main": True, "approx": False, "notebook": True},
        ])

    def test_incremental_is_cheap(self):
        # 长会话：4000 轮、每轮 1 个编辑 + 若干其它条目；之后每次 flush 只追加几条，update 不应重扫全部条目
        out = run_js(r"""
const s = { items: [], subagents: new Map(), absCwd: '/repo' };
for (let i = 0; i < 4000; i++) {
  s.items.push(user('第 ' + i + ' 轮', i), { kind: 'text', text: 'x' },
    tool('Edit', { file_path: '/repo/f' + (i % 50) + '.py' }, { structuredPatch: patch(i + 1, i + 1, ['-a', '+b', '+c']) }), tool('Bash', { command: 'ls' }, null));
}
const T = new m.ChangeTracker();
let t0 = performance.now();
T.update(s);
const agg = T.aggregate('all', '/repo');
const full = performance.now() - t0;
let scanned = 0;
const orig = T.update.bind(T);
t0 = performance.now();
for (let k = 0; k < 200; k++) {
  s.items.push({ kind: 'text', text: 'streaming' });
  T.update(s);
}
const inc = (performance.now() - t0) / 200;
console.log(JSON.stringify({ files: agg.files.length, add: agg.add, del: agg.del, full, inc, scanned: T.scanned, turns: T.turnsWithEdits().length }));
""")
        self.assertEqual((out["files"], out["add"], out["del"], out["turns"]), (50, 8000, 4000, 4000))
        self.assertEqual(out["scanned"], 16200)
        self.assertLess(out["full"], 1500)     # 首次全量（含 4000 次 patchStats）
        self.assertLess(out["inc"], 2)         # 之后每次追加只看新条目


if __name__ == "__main__":
    unittest.main()
