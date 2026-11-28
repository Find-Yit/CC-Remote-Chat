"""输入框的三个前端模块（node 跑纯逻辑；DOM 部分在回放自测页「14j」节）：
- static/complete.js + slashcmds.js：/ 命令与 @ 文件补全（触发、排序、插入、发送前补空格、缓存）；
- static/todopin.js：置顶任务清单（真机 2.1.332 transcript 的 TaskCreate / TaskUpdate、TodoWrite、TaskList）；
- static/drafts.js：草稿存 localStorage（防抖、多窗口以最后输入为准、大小上限、删除、清理、旧 sessionStorage 迁移）。"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_js(body, module):
    url = (ROOT / "static" / module).as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestComplete(unittest.TestCase):
    def test_detect_trigger(self):
        out = run_js(r"""
const t = (v, c) => m.detectTrigger(v, c);
console.log(JSON.stringify([
  t('/'), t('/co'), t('/claude-hud:se'), t('/usr/bin'), t(' /co'), t('/co x'), t('/co x', 3),
  t('@'), t('看看 @src/a'), t('a@b.com'), t('看看@x'), t('x @"my d'), t('x @a\\ b'), t('x @a b'), t('第一行\n@do'),
  t('', 0), t('@x', 0),
]));
""", "complete.js")
        self.assertEqual(out[0], {"kind": "slash", "query": "", "start": 0, "end": 1})
        self.assertEqual(out[1]["query"], "co")
        self.assertEqual(out[2]["query"], "claude-hud:se")
        self.assertIsNone(out[3])                    # 路径不触发
        self.assertIsNone(out[4])                    # 只在输入框开头
        self.assertIsNone(out[5])                    # 已经有参数了
        self.assertEqual(out[6]["query"], "co")      # 光标回到命令里
        self.assertEqual(out[7], {"kind": "at", "query": "", "start": 0, "end": 1, "quoted": False})
        self.assertEqual((out[8]["query"], out[8]["start"]), ("src/a", 3))
        self.assertIsNone(out[9])                    # 邮箱
        self.assertIsNone(out[10])                   # @ 前面不是空白（claude 也不认）
        self.assertEqual((out[11]["query"], out[11]["quoted"]), ("my d", True))
        self.assertEqual(out[12]["query"], "a b")
        self.assertIsNone(out[13])
        self.assertEqual(out[14]["query"], "do")
        self.assertIsNone(out[15])
        self.assertIsNone(out[16])

    def test_rank_commands_and_builtins(self):
        out = run_js(r"""
const S = await import(%s);
const b = S.builtinCommands('2.1.332');
const custom = [{ name: 'deploy', src: 'project', kind: 'command', desc: '部署' }, { name: 'compact', src: 'user', kind: 'command', desc: '我的同名' }];
const all = m.mergeCommands(custom, b);
const names = (q) => m.rankCommands(all, q).map((c) => c.name + (c.via ? '@' + c.via : ''));
console.log(JSON.stringify({
  n: b.length, skills: b.filter((c) => c.kind === 'skill').length,
  hasAll: ['add-dir', 'clear', 'compact', 'context', 'model', 'effort', 'exit', 'resume', 'rewind', 'usage', 'init', 'code-review', 'loop'].every((x) => b.some((c) => c.name === x)),
  zh: b.every((c) => /[一-鿿]/.test(c.desc) && c.en),
  dupCompact: all.filter((c) => c.name === 'compact').map((c) => c.src),
  first: m.rankCommands(all, '').slice(0, 2).map((c) => c.name),
  comp: names('comp').slice(0, 2), cost: names('cost')[0], quit: names('quit')[0], rev: names('rev').slice(0, 2),
  exact: m.rankCommands(all, 'model')[0].exact, fuzzy: names('cdrv')[0], none: names('zzzzqqq'),
  v: [S.tableVersion('2.1.332'), S.tableVersion('2.1.400'), S.tableVersion('2.0.1'), S.tableVersion(''), S.cmpVersion('2.1.10', '2.1.9')],
}));
""" % json.dumps((ROOT / "static" / "slashcmds.js").as_uri()), "complete.js")
        self.assertEqual(out["n"], 80)                       # 真机 2.1.332：85 项 - 用户 3 个 skill - 2 个插件命令
        self.assertEqual(out["skills"], 21)
        self.assertTrue(out["hasAll"])
        self.assertTrue(out["zh"])
        self.assertEqual(out["dupCompact"], ["user"])        # 同名只留自定义
        self.assertEqual(out["first"], ["deploy", "compact"])   # 空查询：自定义在前
        self.assertEqual(out["comp"], ["compact", "autocompact"])
        self.assertEqual(out["cost"], "usage@cost")          # 别名
        self.assertEqual(out["quit"], "exit@quit")
        self.assertEqual(out["rev"], ["code-review", "security-review"])
        self.assertTrue(out["exact"])
        self.assertEqual(out["fuzzy"], "code-review")
        self.assertEqual(out["none"], [])
        self.assertEqual(out["v"][0], {"version": "2.1.332", "exact": True})
        self.assertEqual(out["v"][1], {"version": "2.1.332", "exact": False})
        self.assertEqual(out["v"][2], {"version": "2.1.332", "exact": False})   # 比已知最早的还旧：用最新表
        self.assertEqual(out["v"][3], {"version": "2.1.332", "exact": False})
        self.assertEqual(out["v"][4], 1)

    def test_rank_files(self):
        out = run_js(r"""
const E = ['README.md', 'src/', 'tests/', 'docs/', 'package.json', 'src/app.py', 'src/lib/', 'tests/test_app.py', 'docs/my notes.md', 'src/lib/util.py', 'src/lib/app_helpers.py'];
const r = (q, n) => m.rankFiles(E, q, n).map((f) => f.path + (f.dir ? '' : ''));
console.log(JSON.stringify({
  empty: r('', 3), app: r('app'), src: r('src/'), lib: r('src/lib/'), rdme: r('rdme'), path: r('lib/ut'), up: r('README'),
  dirflag: m.rankFiles(E, 'src')[0], none: r('zzz'), bad: m.rankFiles(null, 'x'),
}));
""", "complete.js")
        self.assertEqual(out["empty"], ["README.md", "src/", "tests/"])   # 空查询：浅的在前（广度优先顺序）
        self.assertEqual(out["app"][:2], ["src/app.py", "src/lib/app_helpers.py"])
        self.assertEqual(out["src"][:2], ["src/app.py", "src/lib/"])     # 以 / 结尾：先列直接子项
        self.assertEqual(out["lib"][:2], ["src/lib/util.py", "src/lib/app_helpers.py"])
        self.assertEqual(out["rdme"], ["README.md"])
        self.assertEqual(out["path"][0], "src/lib/util.py")
        self.assertEqual(out["up"][0], "README.md")                       # 不分大小写
        self.assertEqual(out["dirflag"], {"path": "src/", "dir": True})
        self.assertEqual(out["none"], [])
        self.assertEqual(out["bad"], [])

    def test_apply_pick_and_tail(self):
        out = run_js(r"""
const P = (v, c, it) => m.applyPick(v, m.detectTrigger(v, c), it);
console.log(JSON.stringify({
  slash: P('/co', 3, { name: 'compact' }), slashArgs: P('/co 保留参数', 3, { name: 'compact' }),
  file: P('看 @src/a', 8, { path: 'src/app.py', dir: false }),
  mid: P('看 @sr 后面', 5, { path: 'src/app.py', dir: false }),
  dir: P('看 @sr', 5, { path: 'src/', dir: true }),
  sameDir: P('看 @src/', 7, { path: 'src/', dir: true }),
  space: P('x @do', 5, { path: 'docs/my notes.md', dir: false }),
  spaceDir: P('x @do', 5, { path: 'my docs/', dir: true }),
  quotedClose: P('x @"my docs/n', 13, { path: 'my docs/n 1.md', dir: false }),
  quoteInPath: m.atToken('a "b" c.md'),
  tail: ['看 @a.txt', '看 @a.txt 好', 'a@b', '@"a b"', '@a\\ b', '/compact', '  @x  ', ''].map(m.atSafeTail),
}));
""", "complete.js")
        self.assertEqual(out["slash"], {"value": "/compact ", "caret": 9, "keepOpen": False})
        self.assertEqual(out["slashArgs"]["value"], "/compact 保留参数")
        self.assertEqual(out["file"], {"value": "看 @src/app.py ", "caret": 14, "keepOpen": False})
        self.assertEqual(out["mid"]["value"], "看 @src/app.py 后面")
        self.assertEqual(out["mid"]["caret"], len("看 @src/app.py "))
        self.assertEqual(out["dir"], {"value": "看 @src/", "caret": 7, "keepOpen": True})
        self.assertEqual(out["sameDir"], {"value": "看 @src/ ", "caret": 8, "keepOpen": False})   # 就是这个文件夹本身
        self.assertEqual(out["space"]["value"], 'x @"docs/my notes.md" ')
        self.assertEqual(out["spaceDir"], {"value": 'x @"my docs/', "caret": 12, "keepOpen": True})
        self.assertEqual(out["quotedClose"]["value"], 'x @"my docs/n 1.md" ')
        self.assertEqual(out["quoteInPath"], '@a\\ "b"\\ c.md')
        self.assertEqual(out["tail"], ["看 @a.txt ", "看 @a.txt 好", "a@b", '@"a b" ', "@a\\ b ", "/compact", "  @x  ", ""])

    def test_cache(self):
        out = run_js(r"""
let t = 1000, calls = [], changes = 0, fail = false;
const c = new m.CompleteCache(async (sess, kind) => { calls.push(kind + ':' + sess.sid + ':' + sess.cwd); if (fail) throw new Error('断线'); return { n: calls.length }; },
  { now: () => t, onChange: () => changes++ });
const a = { sid: 's1', cwd: '/p' };
const e1 = c.ensure(a, 'files'); const e2 = c.ensure(a, 'files');
const same = e1 === e2 && !!e1.promise && e1.promise === e2.promise;   // 同时只请求一次
await e1.promise; await new Promise((r) => setTimeout(r, 0));
const got = c.peek(a, 'files').data.n;
c.ensure(a, 'files'); const cached = calls.length === 1;          // 没过期：不再请求
t += 3 * 60 * 1000; const e3 = c.ensure(a, 'files');              // 过期：先照用旧的，后台刷新
const staleKept = e3.data.n === 1 && !!e3.promise;
await e3.promise; await new Promise((r) => setTimeout(r, 0));
c.ensure({ sid: 's1', cwd: '/other' }, 'files');                  // /cd 换了目录：另一份
fail = true; const e4 = c.ensure(a, 'commands', true); await e4.promise.catch(() => {}); await new Promise((r) => setTimeout(r, 0));
const err = c.peek(a, 'commands').err;
const n0 = calls.length; c.ensure(a, 'commands'); t += 5000; c.ensure(a, 'commands');
const noSpam = !c.peek(a, 'commands').promise;                  // 失败后 15 秒内不自动重试
t += 15000; const e5 = c.ensure(a, 'commands'); await e5.promise; await new Promise((r) => setTimeout(r, 0));
const retried = calls.length === n0 + 1;
c.drop('s1');
console.log(JSON.stringify({ same, got, cached, staleKept, calls: calls.slice(0, 4), err, noSpam, retried, dropped: c.peek(a, 'files'), changes }));
""", "complete.js")
        self.assertTrue(out["same"])
        self.assertEqual(out["got"], 1)
        self.assertTrue(out["cached"])
        self.assertTrue(out["staleKept"])
        self.assertEqual(out["calls"], ["files:s1:/p", "files:s1:/p", "files:s1:/other", "commands:s1:/p"])
        self.assertEqual(out["err"], "断线")
        self.assertTrue(out["noSpam"])
        self.assertTrue(out["retried"])
        self.assertIsNone(out["dropped"])
        self.assertGreaterEqual(out["changes"], 3)


def tools_from_transcript(path):
    """transcript → render.js 那样的工具卡（kind/name/input/result/isError/done/structured），按时间顺序"""
    tools, by_id = [], {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        c = (e.get("message") or {}).get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if b.get("type") == "tool_use":
                t = {"kind": "tool", "name": b["name"], "input": b.get("input"), "toolId": b["id"], "done": False,
                     "isError": False}
                tools.append(t)
                by_id[b["id"]] = t
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                t = by_id[b["tool_use_id"]]
                t.update(done=True, isError=bool(b.get("is_error")), result=b.get("content"))
                if isinstance(e.get("toolUseResult"), dict):
                    t["structured"] = e["toolUseResult"]
    return tools


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestTodoPin(unittest.TestCase):
    def model_steps(self, tools):
        """每多一个工具卡（及其结果）后的清单：[(done,total,[状态…])]"""
        return run_js(r"""
const tools = %s;
const out = [];
for (let i = 1; i <= tools.length; i++) {
  const md = m.todoModel(tools.slice(0, i));
  out.push(md ? [md.source, md.done, md.total, md.items.map((x) => x.id + ':' + x.status + ':' + m.itemText(x))] : null);
}
console.log(JSON.stringify(out));
""" % json.dumps(tools, ensure_ascii=False), "todopin.js")

    def test_real_cli_2_1_332_tasks(self):
        tools = tools_from_transcript(ROOT / "fixtures" / "transcript_tasks_2_1_332.jsonl")
        self.assertEqual([t["name"] for t in tools], ["TaskCreate"] * 3 + ["TaskUpdate", "ToolSearch"] + ["TaskUpdate"] * 3)
        self.assertEqual(tools[0]["input"], {"title": "心算 1+1", "description": "计算 1+1 的结果"})   # 模型写的是 title
        steps = self.model_steps(tools)
        self.assertEqual(steps[2], ["tasks", 0, 3, ["1:pending:心算 1+1", "2:pending:心算 2+2", "3:pending:心算 3+3"]])
        self.assertEqual(steps[3], steps[2])          # 参数不对（status: done、多了 result）被 CLI 拒绝：不算
        self.assertEqual(steps[5][1:3], [1, 3])
        self.assertEqual(steps[-1], ["tasks", 3, 3, ["1:completed:心算 1+1", "2:completed:心算 2+2", "3:completed:心算 3+3"]])

    def test_todowrite_pending_and_switching(self):
        tw = lambda todos, **k: {"kind": "tool", "name": "TodoWrite", "input": {"todos": todos}, "done": True, **k}
        steps = self.model_steps([
            {"kind": "tool", "name": "TaskCreate", "input": {"subject": "未出结果"}, "toolId": "t1", "done": False},
            tw([{"content": "写代码", "status": "completed", "activeForm": "正在写代码"},
                {"content": "跑测试", "status": "in_progress", "activeForm": "正在跑测试"},
                {"content": "提交", "status": "weird"}]),
            {"kind": "tool", "name": "TaskUpdate", "input": {"taskId": "9", "status": "in_progress", "activeForm": "正在做"},
             "done": False},
            {"kind": "tool", "name": "TaskUpdate", "input": {"task_id": "9", "status": "deleted"}, "done": True},
            {"kind": "tool", "name": "TaskList", "done": True, "result": "#1 [completed] 甲\n#2 [in_progress] 乙 (agent-1)\n#3 [pending] 丙 [blocked by #2]"},
            tw([], isError=True),
            tw([]),
        ])
        self.assertEqual(steps[0], ["tasks", 0, 1, ["new:t1:pending:未出结果"]])   # 还没出结果：先显示
        self.assertEqual(steps[1], ["todo", 1, 3, ["1:completed:写代码", "2:in_progress:正在跑测试", "3:pending:提交"]])
        self.assertEqual(steps[2][0], "tasks")         # 以最后更新的那一种为准
        self.assertEqual(steps[2][3], ["9:in_progress:正在做", "new:t1:pending:未出结果"])   # 编号的在前，没出结果的在后
        self.assertNotIn("9:", json.dumps(steps[3]))   # deleted
        self.assertEqual(steps[4], ["tasks", 1, 3, ["1:completed:甲", "2:in_progress:乙", "3:pending:丙"]])   # TaskList 快照
        self.assertEqual(steps[5], steps[4])           # 出错的 TodoWrite 不算
        self.assertIsNone(steps[6])                    # 清空 → 不显示

    def test_incremental_scan(self):
        out = run_js(r"""
const s = { items: [{ kind: 'user' }, { kind: 'tool', name: 'Bash' }] };
const a = m.taskTools(s).length;
s.items.push({ kind: 'tool', name: 'TaskCreate', input: { subject: 'x' }, done: true, structured: { task: { id: '1', subject: 'x' } } });
const b = m.taskTools(s).length;
s.items = [];                                  // transcript reset
const c = m.taskTools(s).length;
const md = m.todoModel([{ kind: 'tool', name: 'TaskCreate', input: { subject: 'x' }, done: true, result: 'Task #7 created successfully: x' }]);
console.log(JSON.stringify([a, b, c, md.items[0].id, m.modelKey(md), m.modelKey(null)]));
""", "todopin.js")
        self.assertEqual(out[:4], [0, 1, 0, "7"])
        self.assertEqual(out[4], "tasks|7:pending:x")
        self.assertEqual(out[5], "")


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestDrafts(unittest.TestCase):
    PRE = r"""
class Mem { constructor() { this.m = new Map(); this.quota = Infinity; }
  get length() { return this.m.size; } key(i) { return [...this.m.keys()][i] ?? null; }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { const size = [...this.m.entries()].reduce((n, [a, b]) => n + a.length + b.length, 0) + k.length + v.length;
    if (size > this.quota) throw new Error('QuotaExceededError'); this.m.set(k, String(v)); }
  removeItem(k) { this.m.delete(k); } }
let t = 1000;
const timers = [];
const opts = (o = {}) => ({ now: () => t, setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: (id) => { timers[id - 1] = null; }, ...o });
const runTimers = () => { const fs = timers.splice(0); for (const f of fs) if (f) f(); };
"""

    def test_basic_and_limits(self):
        out = run_js(self.PRE + r"""
const ls = new Mem();
const tooBig = [];
const d = new m.DraftStore({ storage: ls, windowId: 'A', maxChars: 10, onTooBig: (sid, n) => tooBig.push([sid, n]), ...opts() });
d.queue('s1', '你好'); const before = ls.getItem('ccr.draft.s1'); const pend = d.isPending('s1');
runTimers();
const saved = d.load('s1');
t = 2000; d.queue('s1', ''); d.write('s1');
const cleared = ls.getItem('ccr.draft.s1');
d.queue('s2', '12345678901'); d.flush(); d.queue('s2', '12345678901'); d.flush();   // 超过上限：不存、只提示一次
d.queue('s3', 'x'); d.flush(); d.remove('s3');
ls.setItem('ccr.draft.bad', '{not json');
console.log(JSON.stringify({ before, pend, saved, cleared, tooBig, s2: ls.getItem('ccr.draft.s2'), s3: ls.getItem('ccr.draft.s3'), bad: d.load('bad'), pendAfter: d.isPending('s1') }));
""", "drafts.js")
        self.assertIsNone(out["before"])                # 防抖：还没写
        self.assertTrue(out["pend"])
        self.assertEqual(out["saved"], {"t": "你好", "at": 1000, "w": "A"})
        self.assertIsNone(out["cleared"])               # 空草稿 = 删键
        self.assertEqual(out["tooBig"], [["s2", 11]])
        self.assertIsNone(out["s2"])
        self.assertIsNone(out["s3"])                    # 会话删除
        self.assertIsNone(out["bad"])
        self.assertFalse(out["pendAfter"])

    def test_multi_window_last_input_wins(self):
        out = run_js(self.PRE + r"""
const ls = new Mem();
const A = new m.DraftStore({ storage: ls, windowId: 'A', ...opts() });
const B = new m.DraftStore({ storage: ls, windowId: 'B', ...opts() });
// 两个窗口都打开了同一会话；B 先输入（t=1000），A 后输入（t=2000）
B.queue('s', 'B 的字', 1000); A.queue('s', 'A 后打的字', 2000);
A.flush(); B.flush();                                  // B 晚写出、但输入更早：不覆盖 A
const r1 = A.load('s').t;
// 旧窗口关闭时写出很久以前的输入：不覆盖
B.queue('s', '很旧的', 500); B.flush();
const r2 = A.load('s').t;
// storage 事件：只通知别的窗口写的
const got = [];
const win = { fns: [], addEventListener(n, f) { this.fns.push(f); }, removeEventListener() {} };
const off = A.onExternal((sid, d) => got.push([sid, d && d.t]), win);
const fire = (key, nv) => win.fns.forEach((f) => f({ key, newValue: nv, storageArea: ls }));
fire('ccr.draft.s', JSON.stringify({ t: 'B 新写的', at: 3000, w: 'B' }));
fire('ccr.draft.s', JSON.stringify({ t: '自己写的', at: 3000, w: 'A' }));
fire('ccr.draft.x', null);
fire('other.key', 'x');
console.log(JSON.stringify({ r1, r2, got }));
""", "drafts.js")
        self.assertEqual(out["r1"], "A 后打的字")
        self.assertEqual(out["r2"], "A 后打的字")
        self.assertEqual(out["got"], [["s", "B 新写的"], ["x", None]])

    def test_gc_prune_quota_migrate(self):
        out = run_js(self.PRE + r"""
const ls = new Mem();
const d = new m.DraftStore({ storage: ls, windowId: 'A', maxEntries: 3, ...opts() });
for (const [i, sid] of ['a', 'b', 'c', 'e'].entries()) { d.queue(sid, sid + '的草稿', 100 + i); d.flush(); }
const afterPrune = d.entries().map((e) => e.sid);   // 最多 3 条：最旧的 a 被删
t = 100 + 20 * 60 * 1000;
d.queue('z', '新的', t); d.flush();
d.gc(['c']);                                        // 不在列表里、10 分钟没动过的删掉；刚写的 z 保留
const afterGc = d.entries().map((e) => e.sid);
// 存储满：先删最旧的再写
const ls2 = new Mem(); ls2.quota = 120;
const d2 = new m.DraftStore({ storage: ls2, windowId: 'A', ...opts() });
d2.queue('old', 'x'.repeat(40), 1); d2.flush();
d2.queue('new', 'y'.repeat(40), 2); d2.flush();
const quota = d2.entries().map((e) => e.sid);
// 旧版 sessionStorage 草稿：搬过来（已有的不覆盖），旧键删掉
const ss = new Mem(); ss.setItem('ccr.drafts', JSON.stringify({ m1: '旧草稿', z: '不覆盖' }));
const ls3 = new Mem(); ls3.setItem('ccr.draft.z', JSON.stringify({ t: '新的', at: 5, w: 'X' }));
const d3 = new m.DraftStore({ storage: ls3, legacy: ss, windowId: 'A', ...opts() });
console.log(JSON.stringify({ afterPrune, afterGc, quota, m1: d3.load('m1') && d3.load('m1').t, z: d3.load('z').t, legacy: ss.getItem('ccr.drafts'),
  none: new m.DraftStore({ storage: null }).load('x') }));
""", "drafts.js")
        self.assertEqual(out["afterPrune"], ["b", "c", "e"])
        self.assertEqual(out["afterGc"], ["c", "z"])
        self.assertEqual(out["quota"], ["new"])
        self.assertEqual(out["m1"], "旧草稿")
        self.assertEqual(out["z"], "新的")
        self.assertIsNone(out["legacy"])
        self.assertIsNone(out["none"])


if __name__ == "__main__":
    unittest.main()
