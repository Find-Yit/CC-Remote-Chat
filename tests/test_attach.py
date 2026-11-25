"""static/attach.js 的纯函数（node 跑）：剪贴板 / 拖放内容判断、终端拖放式转义（与 ccr/attach.py 逐字一致）、
路径分类与插入、屏幕里的图片编号、能否发图片、覆盖层文字、图片条数据。DOM 部分在回放自测页第 1e 节。"""
import json
import pathlib
import shutil
import subprocess
import unittest

from ccr import attach

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_js(body):
    url = (ROOT / "static" / "attach.js").as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestAttachJs(unittest.TestCase):
    def test_escape_same_as_python(self):
        paths = ["/tmp/a.png", "/tmp/my shot.png", "/tmp/a (1)&b.png", "/x/截屏2026-11-25 21.00.00.png",
                 "/x/it's \"q\".png", "/x/a$b`c\\d.png", "/x/~u/a+b,c:d@e%f=g-h_i.png", "/a/ x.png",
                 "/x/[1]{2}<3>|4;5*6?7!8#9.png", "/x/emoji😀 名.png", "/x/tab\there.png"]
        out = run_js("console.log(JSON.stringify(%s.map((p) => m.escapeDropPath(p))));" % json.dumps(paths))
        self.assertEqual(out, [attach.escape_drop_path(p) for p in paths])

    def test_transfer_info(self):
        out = run_js(r"""
const file = (type) => ({ kind: 'file', type, getAsFile: () => ({ name: 'f', type }) });
const str = (type) => ({ kind: 'string', type });
const dt = (items, text) => ({ items, getData: (t) => (t === 'text/plain' ? (text || '') : '') });
const r = {};
r.shot = m.transferInfo(dt([file('image/png')]));
r.mixed = m.transferInfo(dt([str('text/plain'), str('text/html'), file('image/png')], 'hi'));
r.text = m.transferInfo(dt([str('text/plain')], 'hi'));
r.pdf = m.transferInfo(dt([file('application/pdf')]));
r.files = m.transferInfo({ files: [{ type: 'image/jpeg' }, { type: 'text/plain' }] });
r.none = m.transferInfo(null);
for (const k of Object.keys(r)) r[k] = { ...r[k], file: !!r[k].file };
r.drag = [m.dragHasFiles({ types: ['Files', 'text/uri-list'] }), m.dragHasFiles({ types: ['text/plain'] }), m.dragHasFiles(null)];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["shot"], {"images": 1, "files": 1, "text": False, "file": True})
        self.assertEqual(out["mixed"], {"images": 1, "files": 1, "text": True, "file": True})
        self.assertEqual(out["text"], {"images": 0, "files": 0, "text": True, "file": False})
        self.assertEqual(out["pdf"], {"images": 0, "files": 1, "text": False, "file": False})
        self.assertEqual(out["files"], {"images": 1, "files": 2, "text": False, "file": True})
        self.assertEqual(out["none"], {"images": 0, "files": 0, "text": False, "file": False})
        self.assertEqual(out["drag"], [True, False, False])

    def test_paths(self):
        out = run_js(r"""
const r = {};
r.split = m.splitDropPaths(['/a/x.PNG', '/a/y.jpeg', '/a/doc.pdf', 'rel.png', '/a/b\n.png', 5, '/a/dir']);
r.notArray = m.splitDropPaths(null);
r.ins1 = m.insertPaths('', 0, 0, ['/a b/c.txt']);
r.ins2 = m.insertPaths('看看', 2, 2, ['/a/c.txt', '/d/e f.py']);
r.ins3 = m.insertPaths('前后', 1, 1, ['/a/c.txt']);
r.ins4 = m.insertPaths('abc xyz', 0, 3, ['/p']);
r.isImg = ['/a.webp', '/a.GIF', '/a.png.txt', '', null].map(m.isImagePath);
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["split"], {"images": ["/a/x.PNG", "/a/y.jpeg"], "others": ["/a/doc.pdf", "/a/dir"], "bad": 3})
        self.assertEqual(out["notArray"], {"images": [], "others": [], "bad": 0})
        self.assertEqual(out["ins1"], {"value": "/a\\ b/c.txt ", "caret": 12})
        self.assertEqual(out["ins2"], {"value": "看看 /a/c.txt /d/e\\ f.py ", "caret": 23})
        self.assertEqual(out["ins3"], {"value": "前 /a/c.txt 后", "caret": 11})
        self.assertEqual(out["ins4"], {"value": "/p xyz", "caret": 2})
        self.assertEqual(out["isImg"], [True, True, False, False, False])

    def test_screen_and_blocked(self):
        out = run_js(r"""
const st = (text, images, visible = true) => ({ input: { visible, text, ...(images ? { images } : {}) } });
const r = {};
r.imgs = [m.inputImages(st('[Image #3] [Image #4]', [3, 4])), m.inputImages(st('x')), m.inputImages(st('[Image #1]', [1], false)),
  m.inputImages(null), m.inputImages(st('', [1, 'x', -1, 2.5]))];
r.other = [m.inputHasOther(st('[Image #3] [Image #4]')), m.inputHasOther(st('[Image #3] hi')), m.inputHasOther(st('')),
  m.inputHasOther(null), m.inputHasOther(st('hi', null, false))];
const ok = { local: true };
r.blocked = [m.imageBlocked(ok, 'paste'), m.imageBlocked(null), m.imageBlocked({ local: false }, 'paste'),
  m.imageBlocked({ local: false }, 'drop'), m.imageBlocked({ local: true, dead: true }), m.imageBlocked({ local: true, dialog: true }),
  m.imageBlocked({ local: true, linkDown: true }), m.imageBlocked({ local: true, remote: true }, 'paste')];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["imgs"], [[3, 4], [], [], [], [1]])
        self.assertEqual(out["other"], [False, True, False, False, False])
        b = out["blocked"]
        self.assertIsNone(b[0])
        self.assertEqual(b[1], "请先选择一个会话")
        self.assertIn("服务器会话暂不支持直接发图片", b[2])
        self.assertIn("文字已照常粘贴", b[2])
        self.assertIn("服务器会话暂不支持拖入文件", b[3])
        self.assertIn("已退出", b[4])
        self.assertIn("对话框", b[5])
        self.assertIn("断开", b[6])
        self.assertIn("手机", b[7])

    def test_drop_hint_and_tray(self):
        out = run_js(r"""
const r = {};
r.dom = m.dropHint({ native: false });
r.img = m.dropHint({ native: true, paths: ['/a/x.png', '/a/y.png', '/a/z.txt'], sess: { local: true } });
r.server = m.dropHint({ native: true, paths: ['/a/x.png'], sess: { local: false } });
r.none = m.dropHint({ native: true, paths: [], sess: { local: true } });
r.many = m.dropHint({ native: true, paths: Array.from({ length: 11 }, (_, i) => '/a/' + i + '.png'), sess: { local: true } });
const thumbs = new Map([[3, { url: 'blob:x', name: '剪贴板里的图片' }]]);
r.t0 = m.trayModel({ images: [], pending: null });
r.t1 = m.trayModel({ images: [3, 4], pending: null, other: false, thumbs });
r.t2 = m.trayModel({ images: [], pending: { kind: 'paste', n: 1 } });
r.t3 = m.trayModel({ images: [3], pending: null, other: true, thumbs });
r.t4 = m.trayModel({ images: [3], pending: null, other: false, blocked: 'blocked' });
r.t5 = m.trayModel({ images: [3], pending: { kind: 'paths', n: 2 } });
r.t6 = m.trayModel({ images: [3, 4], pending: { kind: 'remove', n: 4 }, thumbs });
r.t7 = m.trayModel({ images: [3, 4], pending: null, other: false, thumbs, removable: false });
const th2 = new Map([[3, { url: 'blob:a', name: '剪贴板里的图片', kind: 'paste' }], [4, { url: '', name: 'b.png', kind: 'drop', loading: true }],
  [5, { url: 'blob:c', name: 'c.png', kind: 'drop' }], [6, { url: '', name: 'd.png', kind: 'drop', failed: true }]]);
r.t8 = m.trayModel({ images: [3, 4, 5, 6], pending: null, other: true, thumbs: th2 });
r.lb = m.lightboxList(r.t8, 5, (c) => (c.kind === 'drop' ? { path: '/x/' + c.name } : {}));
r.lb0 = m.lightboxList(r.t8, 4);
for (const k of ['t1', 't2', 't3', 't4', 't5', 't6', 't7', 't8']) delete r[k].key;
console.log(JSON.stringify(r));
""")
        self.assertFalse(out["dom"]["ok"])
        self.assertIn("⌘V", out["dom"]["sub"])
        self.assertTrue(out["img"]["ok"])
        self.assertIn("2 张图片", out["img"]["sub"])
        self.assertIn("1 个文件以路径插入", out["img"]["sub"])
        self.assertFalse(out["server"]["ok"])
        self.assertIn("服务器会话", out["server"]["sub"])
        self.assertFalse(out["none"]["ok"])
        self.assertFalse(out["many"]["ok"])
        self.assertIsNone(out["t0"])
        chip = {"kind": "", "loading": False, "failed": False, "removing": False}
        self.assertEqual(out["t1"], {"chips": [dict(chip, n=3, url="blob:x", name="剪贴板里的图片"), dict(chip, n=4, url="", name="")],
                                     "note": "在 claude 的输入框里，发送时和文字一起发出", "pending": False, "warn": False,
                                     "canClear": True, "canRemove": True})
        self.assertTrue(out["t2"]["pending"])
        self.assertIn("剪贴板", out["t2"]["note"])
        self.assertFalse(out["t2"]["canClear"])
        self.assertFalse(out["t2"]["canRemove"])
        self.assertTrue(out["t3"]["warn"])
        self.assertFalse(out["t3"]["canClear"])
        self.assertTrue(out["t3"]["canRemove"])    # 还有别的文字时也能逐张删（后端按光标定位，其它文字不动）
        self.assertFalse(out["t4"]["canClear"])
        self.assertFalse(out["t4"]["canRemove"])   # 已退出 / 对话框
        self.assertIn("放进 claude 的输入框", out["t5"]["note"])
        # 正在删 #4：这一张标 removing，说明换成"正在删"，× 都不显示（一次一张）
        self.assertEqual([c["removing"] for c in out["t6"]["chips"]], [False, True])
        self.assertIn("[Image #4]", out["t6"]["note"])
        self.assertFalse(out["t6"]["canRemove"])
        self.assertFalse(out["t7"]["canRemove"])   # 服务器会话：不能逐张删
        self.assertTrue(out["t7"]["canClear"])
        c8 = out["t8"]["chips"]
        self.assertEqual([(c["kind"], bool(c["url"]), c["loading"], c["failed"]) for c in c8],
                         [("paste", True, False, False), ("drop", False, True, False), ("drop", True, False, False),
                          ("drop", False, False, True)])
        # 灯箱：只有有图的几张，按图片条顺序；点中的那张是 index；拖入的带路径（「在 Finder 中显示」）
        self.assertEqual(out["lb"], {"list": [{"url": "blob:a", "name": "剪贴板里的图片 · [Image #3]"},
                                              {"url": "blob:c", "name": "c.png · [Image #5]", "path": "/x/c.png"}],
                                     "index": 1})
        self.assertEqual(out["lb0"]["index"], 0)   # 点的那张没有图（不会发生：没有图时不是按钮）→ 从第一张开始


if __name__ == "__main__":
    unittest.main()
