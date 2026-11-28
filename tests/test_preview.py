"""可视化：本机文件预览（ccr.preview + /preview/<cap>/… 路由 + /local/* API）与图片引用 blob 路由（ccr.blobs）。

预览服务的安全检查：dot 段、`..`、软链接逃逸、cap 错误、非本机会话、大小上限、CSP 头、方法限制、特殊文件、数据目录。
"""
import base64
import os
import struct
import tempfile
import time
import unittest
import zlib
from unittest import mock

from ccr import blobs as B
from ccr import preview as P
from ccr import session as SM
from tests.test_server import Base


def png_bytes(w=4, h=3):
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class PolicyTests(unittest.TestCase):
    """PreviewService 的路径策略（不经 HTTP）。"""

    def setUp(self):
        self.td = os.path.realpath(tempfile.mkdtemp(prefix="ccrpv"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))
        self.home = os.path.join(self.td, "home")
        self.data = os.path.join(self.td, "data")
        self.proj = os.path.join(self.home, "proj")
        for d in (self.proj, os.path.join(self.home, ".ssh"), os.path.join(self.home, "Library", "Keychains"),
                  self.data, os.path.join(self.proj, ".git")):
            os.makedirs(d, exist_ok=True)
        self.w("proj/index.html", "<p>hi</p>")
        self.w(".ssh/id_ed25519", "SECRET")
        self.w("Library/Keychains/login.keychain-db", "K")
        self.w("proj/.git/config", "[core]")
        self.w("proj/.env", "KEY=1")
        with open(os.path.join(self.data, "api_keys.json"), "w") as f:
            f.write("{}")
        self.cfg = {"fake": False}
        self.pv = P.PreviewService(lambda: self.cfg, data_dir=self.data, home=self.home, runner=mock.Mock())

    def w(self, rel, text):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def err(self, path, status, want="file"):
        with self.assertRaises(P.PreviewError) as cm:
            self.pv.resolve(path, want)
        self.assertEqual(cm.exception.status, status, (path, cm.exception.message))
        return cm.exception

    def test_dot_segments_and_sensitive_dirs(self):
        real, st = self.pv.resolve(os.path.join(self.proj, "index.html"))
        self.assertEqual(real, os.path.join(self.proj, "index.html"))
        for rel in (".ssh/id_ed25519", "proj/.git/config", "proj/.env", "Library/Keychains/login.keychain-db"):
            self.err(os.path.join(self.home, rel), 403)
        self.err(os.path.join(self.data, "api_keys.json"), 403)          # 本程序数据目录
        self.err(self.proj, 404)                                         # 目录：不列目录
        self.assertEqual(self.pv.resolve(self.proj, "any")[0], self.proj)
        self.err("relative/x.html", 400)
        self.err(self.proj + "/a\x00b", 400)
        self.err(os.path.join(self.proj, "nope.html"), 404)
        self.err("/dev/null", 403)

    def test_symlink_escape_and_special_files(self):
        os.symlink(os.path.join(self.home, ".ssh", "id_ed25519"), os.path.join(self.proj, "key.txt"))
        self.err(os.path.join(self.proj, "key.txt"), 403)                # realpath 落进 .ssh
        os.symlink(os.path.join(self.home, ".ssh"), os.path.join(self.proj, "sshdir"))
        self.err(os.path.join(self.proj, "sshdir", "id_ed25519"), 403)
        fifo = os.path.join(self.proj, "pipe")
        os.mkfifo(fifo)
        self.err(fifo, 403)
        big = os.path.join(self.proj, "big.bin")
        with open(big, "wb") as f:
            f.truncate(P.MAX_FILE + 1)
        self.err(big, 413)

    def test_fake_home_allowed_inside_data_dir(self):
        fh = os.path.join(self.data, "fake", "home")
        os.makedirs(fh)
        with open(os.path.join(fh, "a.png"), "wb") as f:
            f.write(png_bytes())
        self.err(os.path.join(fh, "a.png"), 403)                         # 非 fake 模式：数据目录一律拒绝
        self.cfg = {"fake": True, "fake_home": fh}
        self.assertEqual(self.pv.resolve(os.path.join(fh, "a.png"))[0], os.path.join(fh, "a.png"))
        self.err(os.path.join(self.data, "api_keys.json"), 403)          # 数据目录其它部分仍拒绝
        self.assertEqual(self.pv.expand("~/a.png", None), os.path.join(fh, "a.png"))

    def test_expand_and_root_scoping(self):
        self.assertEqual(self.pv.expand("index.html", self.proj), os.path.join(self.proj, "index.html"))
        self.assertEqual(self.pv.expand("../proj/./index.html", self.proj), os.path.join(self.proj, "index.html"))
        self.assertEqual(self.pv.expand("~/proj/index.html", None), os.path.join(self.proj, "index.html"))
        with self.assertRaises(P.PreviewError):
            self.pv.expand("rel.txt", None)
        # 根目录：cwd（非宽泛目录）；cwd = home 时只授权文件所在目录；home 下的文件只授权文件本身
        f = os.path.join(self.proj, "index.html")
        self.assertEqual(self.pv.root_for(f, self.proj), self.proj)
        self.assertEqual(self.pv.root_for(f, self.home), self.proj)
        self.assertEqual(self.pv.root_for(os.path.join(self.home, "x.png"), self.home), os.path.join(self.home, "x.png"))
        u1 = self.pv.url_for("s1", f, self.proj)
        u2 = self.pv.url_for("s1", os.path.join(self.proj, "sub", "b.css"), self.proj)
        self.assertEqual(u1.split("/")[2], u2.split("/")[2])             # 同一根目录复用 cap
        self.assertNotEqual(u1.split("/")[2], self.pv.url_for("s2", f, self.proj).split("/")[2])
        self.assertEqual(len(u1.split("/")[2]), P.CAP_LEN)
        self.assertTrue(u1.endswith(f))

    def test_content_types_and_kinds(self):
        self.assertEqual(P.content_type("/a/x.html"), "text/html; charset=utf-8")
        self.assertEqual(P.content_type("/a/x.py"), "text/plain; charset=utf-8")
        self.assertEqual(P.content_type("/a/x.unknownext", b"plain words"), "text/plain; charset=utf-8")
        self.assertEqual(P.content_type("/a/x.unknownext", b"\x00\x01"), "application/octet-stream")
        self.assertEqual([P.kind_of(x) for x in ("a.png", "a.svg", "a.HTML", "a.md", "a.pdf", "a.ts", "a.zip")],
                         ["image", "svg", "html", "markdown", "pdf", "text", "binary"])

    def test_open_actions(self):
        meta = {"jobid": "local", "cwd": self.proj}
        runner = self.pv.runner
        runner.return_value = mock.Mock(returncode=0, stderr=b"")
        self.assertEqual(self.pv.open(meta, "index.html", "open")["path"], os.path.join(self.proj, "index.html"))
        self.assertEqual(runner.call_args[0][0], ["open", os.path.join(self.proj, "index.html")])
        self.assertIs(runner.call_args[1]["shell"], False)
        self.pv.open(meta, self.proj, "reveal")
        self.assertEqual(runner.call_args[0][0], ["open", "-R", self.proj])
        script = self.w("proj/run.command", "echo pwned")
        exe = self.w("proj/tool", "#!/bin/sh")
        os.chmod(exe, 0o755)
        for p in (script, exe):
            with self.assertRaises(P.PreviewError) as cm:
                self.pv.open(meta, p, "open")
            self.assertEqual(cm.exception.status, 403)
        self.pv.open(meta, exe, "reveal")                                 # 在 Finder 中显示可以
        n = runner.call_count
        for bad in ((os.path.join(self.home, ".ssh", "id_ed25519"), "reveal"), (self.proj, "open"),
                    ("index.html", "rm"), ("index.html", None)):
            with self.assertRaises(P.PreviewError):
                self.pv.open(meta, *bad)
        self.assertEqual(runner.call_count, n)

    def test_open_whitelist(self):
        """「用默认应用打开」改为白名单：其余类型（.tcl/.jnlp/.vncloc/.webarchive/脚本…）只能在 Finder 中显示。"""
        meta = {"jobid": "local", "cwd": self.proj}
        runner = self.pv.runner
        runner.return_value = mock.Mock(returncode=0, stderr=b"")
        for name in ("a.tcl", "a.jnlp", "a.vncloc", "a.webarchive", "a.py", "a.sh", "a.rb", "a.app2", "a.terminal",
                     "a.inetloc", "a.zip", "noext"):
            p = self.w("proj/" + name, "x")
            with self.assertRaises(P.PreviewError, msg=name) as cm:
                self.pv.open(meta, p, "open")
            self.assertEqual(cm.exception.status, 403)
            self.pv.open(meta, p, "reveal")                               # Finder 里显示总是可以
            self.assertEqual(runner.call_args[0][0], ["open", "-R", p])
        for name in ("a.png", "a.PDF", "a.md", "a.txt", "a.html", "a.mp4", "a.docx", "a.xlsx", "a.csv", "a.json", "a.ts"):
            p = self.w("proj/" + name, "x")
            self.pv.open(meta, p, "open")
            self.assertEqual(runner.call_args[0][0], ["open", p], name)
        res = {r["path"]: r for r in self.pv.stat("s1", meta, ["a.png", "a.tcl", "a.html"])["results"]}
        self.assertEqual([res[k]["openable"] for k in ("a.png", "a.tcl", "a.html")], [True, False, True])

    def _case_insensitive(self):
        probe = os.path.join(self.td, "CaseProbe")
        open(probe, "w").close()
        try:
            return os.path.exists(os.path.join(self.td, "caseprobe"))
        finally:
            os.remove(probe)

    def test_case_and_firmlink_variants_cannot_bypass(self):
        """APFS 不区分大小写、realpath 不规范大小写、/System/Volumes/Data firmlink：换种写法也不能绕过拒绝名单。"""
        if not self._case_insensitive():
            self.skipTest("文件系统区分大小写")
        self.w("Library/Preferences/x.plist", "PREF")
        fh = os.path.join(self.data, "fake", "home")
        os.makedirs(fh)
        with open(os.path.join(fh, "ok.txt"), "w") as f:
            f.write("ok")
        variants = [os.path.join(self.home, "library", "Preferences", "x.plist"),
                    os.path.join(self.home, "LIBRARY", "Preferences", "x.plist"),
                    os.path.join(self.home.upper(), "Library", "Preferences", "x.plist"),
                    os.path.join(self.data.upper(), "api_keys.json"),
                    os.path.join(self.data.swapcase(), "API_KEYS.JSON")]
        fl = "/System/Volumes/Data"
        if os.path.exists(fl + self.td):
            variants += [fl + os.path.join(self.home, "Library", "Preferences", "x.plist"),
                         fl + os.path.join(self.home, "library", "Preferences", "x.plist"),
                         fl + os.path.join(self.data, "api_keys.json")]
        for v in variants:
            self.assertTrue(os.path.exists(v), v)                         # 这些写法在磁盘上确实能打开
            self.err(v, 403)
            # 只靠身份比对（关掉字符串预检）也必须拒绝
            with mock.patch.object(self.pv, "denied", return_value=None):
                with self.assertRaises(P.PreviewError, msg=v):
                    self.pv.resolve(v)
        # fake 模式：假 HOME 里的文件换种写法仍允许，数据目录其它部分仍拒绝
        self.cfg = {"fake": True, "fake_home": fh}
        self.assertTrue(self.pv.resolve(os.path.join(fh.upper(), "OK.TXT"))[0])
        with mock.patch.object(self.pv, "denied", return_value=None):
            self.assertTrue(self.pv.resolve(os.path.join(fh.swapcase(), "ok.txt"))[0])
            with self.assertRaises(P.PreviewError):
                self.pv.resolve(os.path.join(self.data.upper(), "api_keys.json"))
        # 根目录按身份判断：cwd 换了大小写也认得出（不会因为字符串不同而扩大到上一级目录）
        f = os.path.join(self.proj, "index.html")
        self.assertEqual(self.pv.root_for(f, self.proj.upper()), self.proj.upper())
        self.assertEqual(self.pv.root_for(f, self.home.upper()), self.proj)       # cwd = home（宽泛）换写法也认得出
        self.assertEqual(self.pv.root_for(os.path.join(self.home, "x.png"), self.home.swapcase()),
                         os.path.join(self.home, "x.png"))

    def test_check_after_open_uses_fd_path(self):
        """打开后按 F_GETPATH 的当前路径复核：路径落进拒绝目录（模拟检查后被换掉）→ 拒绝。"""
        real, st = self.pv.resolve(os.path.join(self.proj, "index.html"))
        self.assertEqual(self.pv.read_file(real, st), b"<p>hi</p>")
        with mock.patch.object(P, "_fd_path", return_value=os.path.join(self.home, ".ssh", "id_ed25519")):
            with self.assertRaises(P.PreviewError) as cm:
                self.pv.read_file(real, st)
            self.assertEqual(cm.exception.status, 403)
        other = P._ident(self.home)
        with self.assertRaises(P.PreviewError):
            self.pv.read_file(real, st, root_ident=(other[0], other[1] + 999999))   # 不在授权根目录内
        self.assertEqual(self.pv.read_file(real, st, root_ident=P._ident(self.proj)), b"<p>hi</p>")

    def test_forget(self):
        u = self.pv.url_for("s1", os.path.join(self.proj, "index.html"), self.proj)
        cap = u.split("/")[2]
        self.assertIsNotNone(self.pv.lookup(cap))
        self.pv.forget("s1")
        self.assertIsNone(self.pv.lookup(cap))
        self.assertNotEqual(self.pv.url_for("s1", os.path.join(self.proj, "index.html"), self.proj).split("/")[2], cap)

    def test_is_local(self):
        self.assertTrue(P.is_local({"jobid": P.LOCAL_JOBID}, {}))
        self.assertFalse(P.is_local({"jobid": "276403"}, {}))
        # fake 模式里 jobid 是作业号的会话按服务器会话处理（文件经 agent 只读取回，ccr/srvfiles.py）
        self.assertFalse(P.is_local({"jobid": "276403"}, {"fake": True}))
        self.assertTrue(P.is_local({"jobid": P.LOCAL_JOBID}, {"fake": True}))
        self.assertFalse(P.is_local(None, {"fake": True}))


class PreviewHttpTests(Base):
    """经 HTTP：/preview/<cap>/… 与 /api/sessions/<sid>/local/*。"""

    def setUp(self):
        super().setUp()
        self.proj = os.path.realpath(os.path.join(self.tmp.name, "proj"))
        os.makedirs(os.path.join(self.proj, "css"))
        os.makedirs(os.path.join(self.proj, ".hidden"))
        self.files = {
            "index.html": b"<link rel=stylesheet href=css/a.css><script src=app.js></script><p>hi</p>",
            "css/a.css": b"p{color:red}", "app.js": b"console.log(1)", "pic.png": png_bytes(),
            "doc.pdf": b"%PDF-1.4\n", "notes.md": "# 标题".encode(), ".hidden/s.txt": b"secret",
            "data.json": b'{"k":1}',
        }
        for rel, data in self.files.items():
            with open(os.path.join(self.proj, rel), "wb") as f:
                f.write(data)
        self.outside = os.path.realpath(os.path.join(self.tmp.name, "outside"))
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "o.txt"), "w") as f:
            f.write("OUTSIDE")
        os.symlink(os.path.join(self.outside, "o.txt"), os.path.join(self.proj, "link.txt"))
        now = time.time()
        base = {"title": "t", "cwd": self.proj, "state": "idle", "created_at": now, "last_activity": now,
                "link": "ready", "launch": {}, "claude_session_id": None}
        self.mgr.metas["sloc"] = dict(base, sid="sloc", jobid="local")
        self.mgr.metas["srem"] = dict(base, sid="srem", jobid="276403")

    def stat(self, sid, paths):
        return self.req("POST", "/api/sessions/%s/local/stat" % sid, {"paths": paths})

    def url(self, rel):
        st, js, _, raw = self.stat("sloc", [rel])
        self.assertEqual(st, 200, raw)
        r = js["results"][0]
        self.assertTrue(r["ok"], r)
        return r["url"]

    def get(self, url, headers=None, method="GET", host=None):
        return self.req(method, url, auth=False, headers=headers, host=host)

    def test_serve_types_csp_and_relative_resources(self):
        u = self.url("index.html")
        st, _, r, raw = self.get(u)
        self.assertEqual(st, 200)
        self.assertEqual(raw, self.files["index.html"])
        self.assertEqual(r.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(r.getheader("Content-Security-Policy"), P.HTML_CSP)
        self.assertNotIn("allow-same-origin", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        self.assertIsNone(r.getheader("X-Frame-Options"))                # 要能被面板的 iframe 嵌入
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        # 相对资源：同一个 cap 下按路径拼接即可
        cap_prefix = u[:u.rindex("/") + 1]
        st, _, r, raw = self.get(cap_prefix + "css/a.css")
        self.assertEqual((st, raw, r.getheader("Content-Type")), (200, b"p{color:red}", "text/css; charset=utf-8"))
        self.assertEqual(r.getheader("Content-Security-Policy"), P.STRICT_CSP)
        st, _, r, raw = self.get(self.url("pic.png"))
        self.assertEqual((st, r.getheader("Content-Type")), (200, "image/png"))
        st, _, r, _ = self.get(self.url("doc.pdf"))
        self.assertEqual((st, r.getheader("Content-Type")), (200, "application/pdf"))
        self.assertIsNone(r.getheader("Content-Security-Policy"))        # Chrome 的 PDF 查看器不能在沙箱里运行
        st, _, r, raw = self.get(u, method="HEAD")
        self.assertEqual((st, raw, r.getheader("Content-Length")), (200, b"", str(len(self.files["index.html"]))))

    def test_cors_only_for_sandboxed_script_style_font(self):
        prefix = self.url("index.html").rsplit("/", 1)[0] + "/"
        cases = [("app.js", "script", "null"), ("css/a.css", "style", "null"), ("app.js", "empty", None),
                 ("data.json", "script", None), ("app.js", "script", None)]
        for rel, dest, want in cases:
            origin = {"Origin": "null"} if (rel, dest, want) != ("app.js", "script", None) else {"Origin": "http://evil.example"}
            st, _, r, _ = self.get(prefix + rel, headers=dict(origin, **{"Sec-Fetch-Dest": dest, "Sec-Fetch-Site": "cross-site"}))
            self.assertEqual(st, 200)
            self.assertEqual(r.getheader("Access-Control-Allow-Origin"), want, (rel, dest))

    def test_rejections(self):
        u = self.url("index.html")
        cap = u.split("/")[2]
        prefix = "/preview/" + cap
        # dot 段 / .. / 软链接逃逸 / 根目录以外 / 目录
        for path, status in ((prefix + self.proj + "/.hidden/s.txt", 403),
                             (prefix + self.proj + "/css/../.hidden/s.txt", 403),
                             (prefix + self.proj + "/%2e%2e/outside/o.txt", 403),
                             (prefix + self.proj + "/link.txt", 403),
                             (prefix + self.outside + "/o.txt", 403),
                             (prefix + self.proj + "/", 404),
                             (prefix + self.proj + "/nope.html", 404),
                             (prefix + self.proj + "/%ff%fe.html", 400)):
            st, js, _, raw = self.get(path)
            self.assertEqual(st, status, (path, raw[:200]))
            self.assertNotIn(b"OUTSIDE", raw)
            self.assertNotIn(b"secret", raw)
        # cap 错误 / 伪造
        for bad in ("/preview/" + "A" * 32 + self.proj + "/index.html", "/preview/short" + self.proj + "/index.html",
                    "/preview/" + self.proj + "/index.html"):
            st, _, _, raw = self.get(bad)
            self.assertEqual(st, 404, bad)
        # 方法限制
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            st, _, _, _ = self.req(m, u, auth=False, raw_body=b"")
            self.assertEqual(st, 405, m)
        # Host 校验保留
        st, _, _, _ = self.get(u, host="evil.example:%d" % self.port)
        self.assertEqual(st, 403)
        # 会话变成非本机 / 被删除 → 旧 cap 失效
        self.mgr.metas["sloc"]["jobid"] = "276403"
        self.assertEqual(self.get(u)[0], 404)
        self.mgr.metas["sloc"]["jobid"] = "local"
        del self.mgr.metas["sloc"]
        self.assertEqual(self.get(u)[0], 404)
        self.assertIsNone(self.srv.preview.lookup(cap))                   # 顺手清掉了已删会话的 cap

    def test_case_variant_urls_follow_identity(self):
        """cap 根目录按身份判断：同一目录换大小写/firmlink 写法仍在授权内；根目录以外换写法仍拒绝。"""
        probe = os.path.join(self.proj, "CaseProbe")
        open(probe, "w").close()
        ci = os.path.exists(os.path.join(self.proj, "caseprobe"))
        os.remove(probe)
        if not ci:
            self.skipTest("文件系统区分大小写")
        u = self.url("index.html")
        prefix = "/preview/" + u.split("/")[2]
        st, _, _, raw = self.get(prefix + self.proj.upper() + "/INDEX.HTML")
        self.assertEqual((st, raw), (200, self.files["index.html"]))
        st, _, _, raw = self.get(prefix + self.outside.upper() + "/o.txt")
        self.assertEqual(st, 403)
        self.assertNotIn(b"OUTSIDE", raw)
        fl = "/System/Volumes/Data"
        if os.path.exists(fl + self.proj):
            self.assertEqual(self.get(prefix + fl + self.proj + "/index.html")[0], 200)
            self.assertEqual(self.get(prefix + fl + self.outside + "/o.txt")[0], 403)

    def test_caps_forgotten_on_remove_and_restart(self):
        cap = self.url("index.html").split("/")[2]
        self.assertEqual(self.req("POST", "/api/sessions/sloc/restart", {})[0], 200)
        self.assertIsNone(self.srv.preview.lookup(cap))                   # 重启：旧 cap 作废
        u2 = self.url("index.html")
        self.assertNotEqual(u2.split("/")[2], cap)
        self.assertEqual(self.get(u2)[0], 200)
        self.assertEqual(self.req("DELETE", "/api/sessions/sloc")[0], 200)
        self.assertIsNone(self.srv.preview.lookup(u2.split("/")[2]))      # 删除：cap 作废
        self.assertEqual(self.get(u2)[0], 404)

    def test_local_api_only_for_local_sessions(self):
        st, js, _, _ = self.stat("srem", ["index.html"])
        self.assertEqual((st, js.get("code")), (403, "not_local"))
        st, js, _, _ = self.req("POST", "/api/sessions/srem/local/open", {"path": "index.html", "action": "reveal"})
        self.assertEqual(st, 403)
        st, js, _, _ = self.req("GET", "/api/sessions/srem/local/info")
        self.assertEqual((st, js["local"]), (200, False))
        st, js, _, _ = self.req("GET", "/api/sessions/sloc/local/info")
        self.assertEqual((st, js["local"], js["cwd"]), (200, True, self.proj))
        # stat：被策略拒绝的路径不透露是否存在
        st, js, _, _ = self.stat("sloc", ["index.html", ".hidden/s.txt", "nope.png", "css", "link.txt", 5])
        res = js["results"]
        self.assertEqual([r["exists"] for r in res], [True, False, False, True, True, False])
        self.assertEqual((res[0]["kind"], res[0]["type"], res[3]["type"]), ("html", "file", "dir"))
        # 软链接指向 cwd 以外：按目标文件授权，cap 只覆盖目标所在目录，和 cwd 的 cap 不同
        self.assertEqual(res[4]["real"], os.path.join(self.outside, "o.txt"))
        self.assertNotEqual(res[4]["url"].split("/")[2], res[0]["url"].split("/")[2])
        self.assertIsNone(res[1]["url"])
        st, _, _, _ = self.stat("sloc", ["a"] * 201)
        self.assertEqual(st, 400)
        # 不带 token 一律 401
        st, _, _, _ = self.req("POST", "/api/sessions/sloc/local/stat", {"paths": []}, auth=False)
        self.assertEqual(st, 401)
        # open：argv、不经 shell（runner 打桩，不真的打开 Finder）
        with mock.patch.object(self.srv.preview, "runner", return_value=mock.Mock(returncode=0, stderr=b"")) as run:
            st, js, _, _ = self.req("POST", "/api/sessions/sloc/local/open", {"path": "pic.png", "action": "reveal"})
            self.assertEqual(st, 200)
            self.assertEqual(run.call_args[0][0], ["open", "-R", os.path.join(self.proj, "pic.png")])
            st, _, _, _ = self.req("POST", "/api/sessions/sloc/local/open", {"path": ".hidden/s.txt", "action": "open"})
            self.assertEqual(st, 403)
            self.assertEqual(run.call_count, 1)


class BlobRouteTests(Base):
    def setUp(self):
        super().setUp()
        self.png = png_bytes()
        self.ref = "m.285bd6a9-08c3-46e0-b111-3fe228694c59.123.0-1.0123456789abcdef"
        self.calls = []

        def get_blob(sid, ref):
            self.mgr._need(sid)
            self.calls.append((sid, ref))
            if ref.endswith("ffffffffffffffff"):
                return {"media_type": "image/png", "data": base64.b64encode(b"<svg onload=alert(1)>").decode()}
            if ref.endswith("eeeeeeeeeeeeeeee"):
                raise SM.OpError("该图片不属于这个会话", code="forbidden", status=403)
            return {"media_type": "image/jpeg", "data": base64.b64encode(self.png).decode()}
        self.mgr.get_blob = get_blob
        self.mgr.metas["s1"] = {"sid": "s1", "jobid": "276403", "cwd": "/x", "created_at": 1}

    def test_blob_bytes_type_and_cache(self):
        st, _, r, raw = self.req("GET", "/api/sessions/s1/blob/" + self.ref)
        self.assertEqual(st, 200)
        self.assertEqual(raw, self.png)
        self.assertEqual(r.getheader("Content-Type"), "image/png")       # 按文件头，不信 media_type
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("max-age", r.getheader("Cache-Control"))
        st, _, _, raw2 = self.req("GET", "/api/sessions/s1/blob/" + self.ref)
        self.assertEqual((st, raw2), (200, self.png))
        self.assertEqual(len(self.calls), 1)                             # 第二次命中服务端 LRU

    def test_blob_errors(self):
        self.assertEqual(self.req("GET", "/api/sessions/s1/blob/" + self.ref, auth=False)[0], 401)
        for bad in ("x", "m.nope.1.0.0123456789abcdef", self.ref + "0", "m.285bd6a9-08c3-46e0-b111-3fe228694c59.1.0.ZZ"):
            self.assertEqual(self.req("GET", "/api/sessions/s1/blob/" + bad)[0], 404, bad)
        st, js, _, _ = self.req("GET", "/api/sessions/s1/blob/" + self.ref[:-16] + "f" * 16)
        self.assertEqual(st, 415)                                        # 不是 png/jpeg/gif/webp
        st, js, _, _ = self.req("GET", "/api/sessions/s1/blob/" + self.ref[:-16] + "e" * 16)
        self.assertEqual((st, js.get("code")), (403, "forbidden"))
        self.assertEqual(self.req("GET", "/api/sessions/nope/blob/" + self.ref)[0], 404)
        self.assertEqual(len(B.BlobCache()), 0)


class BlobUnitTests(unittest.TestCase):
    def test_sniff_decode_and_lru(self):
        self.assertEqual(B.sniff_image(png_bytes()), "image/png")
        self.assertEqual(B.sniff_image(b"\xff\xd8\xff\xe0xx"), "image/jpeg")
        self.assertEqual(B.sniff_image(b"GIF89a.."), "image/gif")
        self.assertEqual(B.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(B.sniff_image(b"<svg/>"))
        with self.assertRaises(ValueError):
            B.decode_image("not base64!!")
        c = B.BlobCache(max_bytes=40, max_items=10)
        c.put("s", "a", "image/png", b"x" * 8)
        c.put("s", "b", "image/png", b"y" * 8)
        c.get("s", "a")
        for k in "cdef":
            c.put("s", k, "image/png", b"z" * 8)
        self.assertIsNotNone(c.get("s", "a"))                           # 最近用过的保留
        self.assertIsNone(c.get("s", "b"))
        self.assertTrue(B.valid_ref("a.285bd6a9-08c3-46e0-b111-3fe228694c59.ag_1-x.5.3.0123456789abcdef"))
        self.assertFalse(B.valid_ref("a.285bd6a9-08c3-46e0-b111-3fe228694c59.5.3.0123456789abcdef"))

    def test_session_get_blob_checks_owner(self):
        mgr = SM.SessionManager.__new__(SM.SessionManager)
        s = SM._Session("s0123456789ab", 10)
        s.claude_session_id = "285bd6a9-08c3-46e0-b111-3fe228694c59"
        s.jobid = "276403"
        mgr._get = lambda sid: s
        mgr._req = mock.Mock(return_value={"data": "x"})
        with self.assertRaises(SM.OpError) as cm:
            mgr.get_blob(s.sid, "m.11111111-2222-3333-4444-555555555555.1.0.0123456789abcdef")
        self.assertEqual(cm.exception.http_status, 403)
        mgr._req.assert_not_called()
        mgr.get_blob(s.sid, "m.285bd6a9-08c3-46e0-b111-3fe228694c59.1.0.0123456789abcdef")
        mgr._req.assert_called_once_with("276403", "blob", timeout=SM.BLOB_TIMEOUT, sid=s.sid,
                                         ref="m.285bd6a9-08c3-46e0-b111-3fe228694c59.1.0.0123456789abcdef",
                                         session_id="285bd6a9-08c3-46e0-b111-3fe228694c59")


if __name__ == "__main__":
    unittest.main()
