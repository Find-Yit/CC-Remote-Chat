"""侧栏：项目与聊天（static/projects.js，SPEC §15.2）的纯函数：分组 key 与路径规范化、显示名、布局与顺序、
拖动 / 移动落地后的全局顺序、跨组拒绝、聚合状态、折叠状态与 home 的记忆。DOM（标题行、颜色、截断）在回放自测页第 14i 节。"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_js(body):
    url = (ROOT / "static" / "projects.js").as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


# 会话样例：本机两个目录（含 ~ 形式与末尾斜杠）、服务器同一路径两个作业、一个聊天、一个旧记录（没有 in_project）
SESSIONS = r"""
const S = (sid, jobid, cwd, extra = {}) => ({ sid, meta: { sid, jobid, cwd, created_at: 1000, order: null, in_project: true, ...extra } });
const items = [
  S('a', 'local', '/Users/u/proj', { order: 3 }),
  S('b', '123', '/home/u/exp/', { order: 1 }),
  S('c', '456', '/home/u/exp', { order: 4 }),
  S('d', 'local', '/Users/u/proj/', { order: 0 }),
  S('e', 'local', '/Users/u/other', { order: 2, in_project: false }),
  S('f', 'local', '/Users/u/notes', { order: 5 }),
];
delete items[5].meta.in_project;   // 旧记录：没有这个字段 = 在项目里
"""


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestProjects(unittest.TestCase):
    def test_norm_and_key(self):
        out = run_js(r"""
const r = {};
r.norm = ['/a/b/', '/a//b///', '/', '///', ' /x ', '~', '~/', '~/p/', 'rel/x', '', null, 5].map((x) => m.normPath(x));
r.home = ['~', '~/p', '/abs', '~x'].map((x) => m.normPath(x, '/Users/u/'));
r.rootHome = m.normPath('~/p', '/');
r.keys = [
  m.projectKey({ jobid: 'local', cwd: '/Users/u/p/' }),
  m.projectKey({ jobid: '123', cwd: '/home/u/p' }),
  m.projectKey({ jobid: '456', cwd: '/home/u/p/' }),
  m.projectKey({ jobid: 'local', cwd: '~/p' }, { local: '/Users/u' }),
  m.projectKey({ jobid: '123', cwd: '~/p' }, { local: '/Users/u' }),
  m.projectKey({ jobid: 'local', cwd: '' }),
  m.projectKey({ jobid: 'local' }),
];
r.names = [m.projectName('/Users/u/proj'), m.projectName('/'), m.projectName('~'), m.projectName('/Users/u', '/Users/u/'),
  m.projectName('~/a/b'), m.projectName(''), m.projectName('/Users/u')];
r.inProject = [m.inProject({ in_project: true }), m.inProject({ in_project: false }), m.inProject({}), m.inProject(null)];
r.target = [m.targetOf({ jobid: 'local' }), m.targetOf({ jobid: '1' }), m.targetOf(null)];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["norm"], ["/a/b", "/a/b", "/", "/", "/x", "~", "~", "~/p", "", "", "", ""])
        self.assertEqual(out["home"], ["/Users/u", "/Users/u/p", "/abs", ""])
        self.assertEqual(out["rootHome"], "/p")
        self.assertEqual(out["keys"], ["local:/Users/u/p", "server:/home/u/p", "server:/home/u/p", "local:/Users/u/p",
                                       "server:~/p", "?", "?"])
        self.assertEqual(out["names"], ["proj", "/", "~", "~", "b", "未知目录", "u"])
        self.assertEqual(out["inProject"], [True, False, True, True])
        self.assertEqual(out["target"], ["local", "server", "server"])

    def test_layout(self):
        out = run_js(SESSIONS + r"""
const L = m.buildLayout(items);
console.log(JSON.stringify({ projects: L.projects.map((g) => [g.key, g.name, g.target, g.sids, g.jobid]), chats: L.chats,
  flat: m.flatten(L) }));
""")
        # 项目顺序 = 组内最靠前的会话：proj(d=0) < exp(b=1) < notes(f=5)；聊天单独一区
        self.assertEqual(out["projects"], [
            ["local:/Users/u/proj", "proj", "local", ["d", "a"], ""],
            ["server:/home/u/exp", "exp", "server", ["b", "c"], "123"],
            ["local:/Users/u/notes", "notes", "local", ["f"], ""],
        ])
        self.assertEqual(out["chats"], ["e"])
        self.assertEqual(out["flat"], ["d", "a", "b", "c", "f", "e"])

    def test_new_sessions_and_projects(self):
        out = run_js(SESSIONS + r"""
const r = {};
// 已有项目里新建（没排过序）：只出现在该项目最上面，项目不挪位置
const a = items.concat([S('n1', '789', '/home/u/exp', { created_at: 2000 })]);
r.inExisting = m.buildLayout(a).projects.map((g) => g.sids);
// 新项目（组内全是没排过序的）：排在所有项目最前面；多个新项目按最新的会话、新的在上
const b = a.concat([S('n2', 'local', '/Users/u/new', { created_at: 3000 }), S('n3', 'local', '/Users/u/newer', { created_at: 4000 })]);
r.newProject = m.buildLayout(b).projects.map((g) => g.key);
// 新聊天：聊天区最上面
r.newChat = m.buildLayout(b.concat([S('n4', 'local', '/Users/u/proj', { created_at: 5000, in_project: false })])).chats;
// 一个都没排过（从没拖过）：项目按建立时间（组里最早的会话）、新项目在上；会话按新的在上；
// 往已有项目里再加会话（比 /q 的还新）也不挪位置
const fresh = [S('x', 'local', '/p', { created_at: 1 }), S('y', 'local', '/q', { created_at: 3 }), S('z', 'local', '/p', { created_at: 2 })];
r.fresh = m.buildLayout(fresh).projects.map((g) => [g.key, g.sids]);
r.freshAdd = m.buildLayout(fresh.concat([S('w', 'local', '/p', { created_at: 9 })])).projects.map((g) => [g.key, g.sids]);
// 服务器项目的「+」预填：组里最新那个会话的作业号
r.jobid = m.buildLayout(a).projects[1].jobid;
// 同名文件夹：同一运行位置下往上多带一级；本机与服务器同名不算重名（有位置标签）
const dup = [S('p', 'local', '/a/src', { order: 0 }), S('q', 'local', '/b/src', { order: 1 }), S('w', '1', '/c/src', { order: 2 }),
  S('h', 'local', '/x/y/z', { order: 3 }), S('i', 'local', '/w/y/z', { order: 4 })];
r.dup = m.buildLayout(dup).projects.map((g) => g.name);
// ~ 形式：知道 home 时与绝对路径同一个项目；home 本身显示 "~"
const tl = [S('t1', 'local', '~/proj', { order: 0 }), S('t2', 'local', '/Users/u/proj', { order: 1 }), S('t3', 'local', '~', { order: 2 }),
  S('t4', 'local', '/Users/u/', { order: 3 })];
r.tilde = m.buildLayout(tl, { homes: { local: '/Users/u' } }).projects.map((g) => [g.key, g.name, g.sids]);
r.tildeUnknown = m.buildLayout(tl).projects.map((g) => g.key);
r.unknown = m.buildLayout([S('u1', 'local', ''), S('u2', '1', null)]).projects.map((g) => [g.key, g.name, g.target, g.sids.length]);
r.empty = m.buildLayout([]);
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["inExisting"], [["d", "a"], ["n1", "b", "c"], ["f"]])
        self.assertEqual(out["newProject"], ["local:/Users/u/newer", "local:/Users/u/new", "local:/Users/u/proj",
                                             "server:/home/u/exp", "local:/Users/u/notes"])
        self.assertEqual(out["newChat"], ["n4", "e"])
        self.assertEqual(out["fresh"], [["local:/q", ["y"]], ["local:/p", ["z", "x"]]])
        self.assertEqual(out["freshAdd"], [["local:/q", ["y"]], ["local:/p", ["w", "z", "x"]]])
        self.assertEqual(out["jobid"], "789")
        self.assertEqual(out["dup"], ["a/src", "b/src", "src", "x/y/z", "w/y/z"])
        self.assertEqual(out["tilde"], [["local:/Users/u/proj", "proj", ["t1", "t2"]], ["local:/Users/u", "~", ["t3", "t4"]]])
        self.assertEqual(out["tildeUnknown"], ["local:~/proj", "local:/Users/u/proj", "local:~", "local:/Users/u"])
        self.assertEqual(out["unknown"], [["?", "未知目录", "", 2]])
        self.assertEqual(out["empty"], {"projects": [], "chats": []})

    def test_moves(self):
        out = run_js(SESSIONS + r"""
const L = m.buildLayout(items.concat([S('g', 'local', '/x', { order: 6, in_project: false })]));
const r = {};
r.base = m.flatten(L);
r.within = m.moveSession(L, 'a', 'd', false);          // 组内：a 移到 d 之前
r.withinAfter = m.moveSession(L, 'b', 'c', true);
r.chatWithin = m.moveSession(L, 'g', 'e', false);
r.cross = m.moveSession(L, 'a', 'b', false);           // 跨项目：拒绝
r.toChat = m.moveSession(L, 'a', 'e', true);           // 项目 → 聊天：拒绝
r.fromChat = m.moveSession(L, 'e', 'f', false);
r.self = m.moveSession(L, 'a', 'a', true);
r.noop = m.moveSession(L, 'd', 'a', false);            // 落在原位：不调整
r.unknown = m.moveSession(L, 'zz', 'a', false);
r.edge = [m.moveSessionEdge(L, 'a', 'top'), m.moveSessionEdge(L, 'a', 'bottom'), m.moveSessionEdge(L, 'g', 'top'), m.moveSessionEdge(L, 'zz', 'top')];
r.step = [m.moveSessionStep(L, 'a', -1), m.moveSessionStep(L, 'd', -1), m.moveSessionStep(L, 'a', 1), m.moveSessionStep(L, 'f', 1)];
r.can = [m.canDropSession(L, 'a', 'local:/Users/u/proj'), m.canDropSession(L, 'a', 'server:/home/u/exp'), m.canDropSession(L, 'e', m.CHAT_KEY),
  m.canDropSession(L, 'a', m.CHAT_KEY), m.canDropSession(L, 'zz', m.CHAT_KEY)];
r.proj = [
  m.moveProject(L, 'local:/Users/u/notes', 'local:/Users/u/proj', false),
  m.moveProject(L, 'local:/Users/u/proj', 'server:/home/u/exp', true),
  m.moveProject(L, 'local:/Users/u/proj', 'server:/home/u/exp', false),   // 原位
  m.moveProject(L, 'local:/Users/u/proj', 'local:/Users/u/proj', true),
  m.moveProject(L, 'nope', 'local:/Users/u/proj', true),
];
r.projEdge = [m.moveProjectEdge(L, 'server:/home/u/exp', 'top'), m.moveProjectEdge(L, 'server:/home/u/exp', 'bottom'), m.moveProjectEdge(L, 'local:/Users/u/proj', 'top')];
r.projStep = [m.moveProjectStep(L, 'server:/home/u/exp', 1), m.moveProjectStep(L, 'local:/Users/u/proj', -1)];
// 落地后的全局顺序再分组：显示与预期一致（组内顺序 / 项目顺序都保留）
const apply = (flat) => m.buildLayout(L.projects.flatMap((g) => g.sids).concat(L.chats).map((sid, _) => {
  const it = items.concat([S('g', 'local', '/x', { in_project: false })]).find((x) => x.sid === sid);
  return { sid, meta: { ...it.meta, order: flat.indexOf(sid) } };
}));
const after = apply(r.proj[0]);
r.reapplied = [after.projects.map((g) => [g.key, g.sids]), after.chats];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["base"], ["d", "a", "b", "c", "f", "e", "g"])
        self.assertEqual(out["within"], ["a", "d", "b", "c", "f", "e", "g"])
        self.assertEqual(out["withinAfter"], ["d", "a", "c", "b", "f", "e", "g"])
        self.assertEqual(out["chatWithin"], ["d", "a", "b", "c", "f", "g", "e"])
        for k in ("cross", "toChat", "fromChat", "self", "noop", "unknown"):
            self.assertIsNone(out[k], k)
        self.assertEqual(out["edge"], [["a", "d", "b", "c", "f", "e", "g"], None, ["d", "a", "b", "c", "f", "g", "e"], None])
        self.assertEqual(out["step"], [["a", "d", "b", "c", "f", "e", "g"], None, None, None])
        self.assertEqual(out["can"], [True, False, True, False, False])
        self.assertEqual(out["proj"], [["f", "d", "a", "b", "c", "e", "g"], ["b", "c", "d", "a", "f", "e", "g"], None, None, None])
        self.assertEqual(out["projEdge"], [["b", "c", "d", "a", "f", "e", "g"], ["d", "a", "f", "b", "c", "e", "g"], None])
        self.assertEqual(out["projStep"], [["d", "a", "f", "b", "c", "e", "g"], None])
        self.assertEqual(out["reapplied"], [[["local:/Users/u/notes", ["f"]], ["local:/Users/u/proj", ["d", "a"]],
                                             ["server:/home/u/exp", ["b", "c"]]], ["e", "g"]])

    def test_aggregate(self):
        out = run_js(r"""
const P = ['pending', '待处理'], U = ['unread', '新回复'], B = ['busy', '运行中'], SB = ['busy', '子代理运行中'], E = ['error', '错误'], R = ['ready', '空闲'], X = ['exited', '已退出'];
const cases = [[R, X], [R, B], [B, SB, U], [U, P, B], [E, R], [E, B], [], [null, R]];
console.log(JSON.stringify({
  agg: cases.map((c) => { const a = m.aggregate(c); return [a.top, a.n, m.aggregateText(a)]; }),
  cls: [
    m.headerClass(m.aggregate([P]), true, false), m.headerClass(m.aggregate([P]), false, false),
    m.headerClass(m.aggregate([U, B]), true, true), m.headerClass(m.aggregate([R]), true, true), m.headerClass(m.aggregate([R]), false, true),
    m.headerClass(m.aggregate([B]), true, false), m.headerClass(null, true, false),
  ],
}));
""")
        self.assertEqual(out["agg"], [
            [None, 2, ""], ["busy", 2, "1 个运行中"], ["unread", 3, "1 个新回复 · 2 个运行中"],
            ["pending", 3, "1 个待处理 · 1 个新回复 · 1 个运行中"], ["error", 2, "1 个出错"], ["busy", 2, "1 个运行中 · 1 个出错"],
            [None, 0, ""], [None, 2, ""]])
        # 折叠时：待处理 = 浅黄、新回复 = 浅红、当前会话在里面 = 选中底色；展开时标题行不着色（会话行自己有）
        self.assertEqual(out["cls"], ["proj-hd attn", "proj-hd", "proj-hd unread active", "proj-hd active", "proj-hd",
                                      "proj-hd", "proj-hd"])

    def test_collapse_and_homes(self):
        out = run_js(r"""
const mem = () => { const d = new Map(); return { getItem: (k) => (d.has(k) ? d.get(k) : null), setItem: (k, v) => d.set(k, String(v)), d }; };
const r = {};
const st = mem();
const c = new m.CollapseStore(st);
r.c1 = [c.has('local:/p'), c.set('local:/p', true), c.set('local:/p', true), c.has('local:/p'), c.toggle('server:/q'), c.toggle('local:/p')];
r.saved = JSON.parse(st.getItem(m.COLLAPSE_KEY));
r.reload = new m.CollapseStore(st).has('server:/q');
const many = new m.CollapseStore(mem());
for (let i = 0; i < 320; i++) many.set('k' + i, true);
r.cap = [many.has('k0'), many.has('k319'), [...many.set_].length];
const bad = mem(); bad.setItem(m.COLLAPSE_KEY, '{oops'); r.bad = new m.CollapseStore(bad).has('x');
const broken = { getItem: () => { throw new Error('x'); }, setItem: () => { throw new Error('x'); } };
const cb = new m.CollapseStore(broken); r.broken = [cb.set('a', true), cb.has('a')];
r.infer = [m.inferHome('~', '/Users/u'), m.inferHome('~/proj', '/Users/u/proj'), m.inferHome('~/proj/', '/private/tmp/h/proj'),
  m.inferHome('~/proj', '/elsewhere/other'), m.inferHome('/abs', '/abs'), m.inferHome('~/p', '~/p'), m.inferHome('~/p', '/p')];
const hs = mem();
const hb = new m.HomeBook(hs);
r.obs = [hb.observe('s1', 'local', '~/proj'), hb.observe('s1', 'local', '~/proj'), hb.observe('s1', 'local', '/Users/u/proj'),
  hb.observe('s1', 'local', '/Users/u/proj'), hb.observe('s2', 'server', '/home/x/a'), hb.observe('s3', 'server', '~'), hb.observe('s3', 'server', '/home/x')];
r.homes = hb.homes;
r.homesReload = new m.HomeBook(hs).homes;
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["c1"], [False, True, False, True, True, False])
        self.assertEqual(out["saved"], ["server:/q"])
        self.assertTrue(out["reload"])
        self.assertEqual(out["cap"], [False, True, 300])
        self.assertFalse(out["bad"])
        self.assertEqual(out["broken"], [True, True])             # 存不进去也照常在本页生效
        self.assertEqual(out["infer"], ["/Users/u", "/Users/u", "/private/tmp/h", "", "", "", ""])
        self.assertEqual(out["obs"], [False, False, True, False, False, False, True])
        self.assertEqual(out["homes"], {"local": "/Users/u", "server": "/home/x"})
        self.assertEqual(out["homesReload"], out["homes"])


if __name__ == "__main__":
    unittest.main()
