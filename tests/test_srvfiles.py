"""服务器会话的文件预览 / 下载（SPEC §10.10）与文本编码检测（GB18030）。

- agent 的只读 op file_stat / file_read：路径策略（dot 段、`.`/`..`、软链接逃逸、目录、设备 / FIFO、敏感目录、超限、
  权限拒绝、授权根目录、打开后复核）、分块、签名不变时不传内容、在独立线程里执行；
- 后端 ccr/srvfiles.py + /preview/<cap>/… 路由（经真正的 node_agent 子进程）：响应头 / CSP 与本机预览一致、charset、
  只取开头、转码兜底、下载（attachment、分块、中途变化断开）、缓存与签名校验、链路断开 / 超时、并发上限、cap 作废；
- ccr/textenc.py 与 static/textdecode.js（node）。
"""
import base64
import http.client
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ccr import node_agent
from ccr import preview as P
from ccr import session as SM
from ccr import srvfiles as SF
from ccr import textenc as TE
from tests.test_node_agent import AgentProc
from tests.test_preview import png_bytes
from tests.test_server import Base

GBK_TEXT = "中文测试，GBK 编码。第二行：编码检测\n"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_tree(base):
    """home/proj 下的各种文件 + 隐藏 / 敏感 / 外部文件与软链接。返回 (home, proj, outside)。"""
    home = os.path.join(base, "home")
    proj = os.path.join(home, "proj")
    outside = os.path.join(base, "outside")
    for d in (os.path.join(proj, "css"), os.path.join(proj, "sub"), os.path.join(home, ".ssh"),
              os.path.join(home, "Library", "Keychains"), os.path.join(proj, ".git"), outside):
        os.makedirs(d, exist_ok=True)
    files = {
        "proj/a.txt": "hello 世界\n".encode(), "proj/gbk.txt": GBK_TEXT.encode("gb18030"),
        "proj/index.html": b"<link rel=stylesheet href=css/a.css><p>hi</p>", "proj/css/a.css": b"p{color:red}",
        "proj/app.js": b"console.log(1)", "proj/pic.png": png_bytes(), "proj/doc.pdf": b"%PDF-1.4\n",
        "proj/.env": b"KEY=1", "proj/.git/config": b"[core]", ".ssh/id_ed25519": b"SECRET",
        "Library/Keychains/login.keychain-db": b"K", "top.txt": b"in home",
    }
    for rel, data in files.items():
        with open(os.path.join(home, rel), "wb") as f:
            f.write(data)
    with open(os.path.join(outside, "o.txt"), "wb") as f:
        f.write(b"OUTSIDE")
    os.symlink(os.path.join(outside, "o.txt"), os.path.join(proj, "link_out.txt"))
    os.symlink(os.path.join(home, ".ssh", "id_ed25519"), os.path.join(proj, "link_key.txt"))
    os.symlink(os.path.join(proj, "a.txt"), os.path.join(proj, "link_in.txt"))
    os.symlink(os.path.join(home, ".ssh"), os.path.join(proj, "sshdir"))
    os.mkfifo(os.path.join(proj, "pipe"))
    with open(os.path.join(proj, "big.log"), "wb") as f:
        f.truncate(SF.PREVIEW_MAX + 1)
    return home, proj, outside


class AgentFileOpTests(unittest.TestCase):
    """node_agent.op_file_stat / op_file_read（直接调用，HOME 指向临时目录）。"""

    def setUp(self):
        self.td = os.path.realpath(tempfile.mkdtemp(prefix="ccrsf"))
        self.addCleanup(shutil.rmtree, self.td, True)
        self.home, self.proj, self.outside = make_tree(self.td)
        p = mock.patch.object(node_agent, "home_dir", lambda: self.home)
        p.start()
        self.addCleanup(p.stop)

    def stat1(self, path, cwd=None):
        return node_agent.op_file_stat({"paths": [path], "cwd": cwd})["results"][0]

    def read(self, path, root, **kw):
        req = dict({"path": path, "root": root, "max_size": SF.PREVIEW_MAX, "offset": 0, "length": 1 << 20}, **kw)
        return node_agent.op_file_read(req)

    def test_stat_types_roots_and_expansion(self):
        r = self.stat1("a.txt", self.proj)
        self.assertEqual((r["exists"], r["type"], r["real"], r["root"]), (True, "file", self.proj + "/a.txt", self.proj))
        self.assertEqual(r["size"], len("hello 世界\n".encode()))
        self.assertTrue(r["readable"])
        self.assertEqual(len(r["sig"]), 4)
        self.assertEqual(self.stat1("~/proj/a.txt")["real"], self.proj + "/a.txt")
        self.assertEqual(self.stat1("a.txt", "~/proj")["real"], self.proj + "/a.txt")
        self.assertEqual(self.stat1("sub", self.proj)["type"], "dir")
        # cwd 是宽泛目录（home）：只授权文件所在目录；home 下的文件只授权它本身
        self.assertEqual(self.stat1(self.proj + "/a.txt", self.home)["root"], self.proj)
        self.assertEqual(self.stat1(self.home + "/top.txt", self.home)["root"], self.home + "/top.txt")
        # 软链接：cwd 内部 → 按目标授权；指向 cwd 以外 → 根目录是目标所在目录（与本机预览一致）
        self.assertEqual(self.stat1("link_in.txt", self.proj)["real"], self.proj + "/a.txt")
        out = self.stat1("link_out.txt", self.proj)
        self.assertEqual((out["real"], out["root"]), (self.outside + "/o.txt", self.outside))
        res = node_agent.op_file_stat({"paths": ["x"], "cwd": None})["results"][0]
        self.assertIn("相对路径", res["reason"])
        with self.assertRaises(node_agent.OpError):
            node_agent.op_file_stat({"paths": ["a"] * 201})

    def test_stat_policy_hides_existence(self):
        for p in (".env", ".git/config", "link_key.txt", "sshdir/id_ed25519", "pipe", "../outside/o.txt", "./a.txt",
                  "nope.txt", "/dev/null", self.home + "/Library/Keychains/login.keychain-db", "a\x00b", "a\nb"):
            r = self.stat1(p, self.proj)
            self.assertFalse(r["exists"], (p, r))
            self.assertTrue(r["reason"], p)
            self.assertNotIn("root", r)
        self.assertIn("隐藏", self.stat1(".env", self.proj)["reason"])
        self.assertIn("..", self.stat1("../outside/o.txt", self.proj)["reason"])
        self.assertIn("特殊文件", self.stat1("pipe", self.proj)["reason"])
        self.assertIn("敏感", self.stat1("/dev/null")["reason"])

    def test_read_chunks_sig_and_limits(self):
        p = self.proj + "/a.txt"
        r = self.read(p, self.proj)
        self.assertEqual(base64.b64decode(r["data"]), "hello 世界\n".encode())
        self.assertEqual((r["offset"], r["real"]), (0, p))
        part = self.read(p, self.proj, offset=6, length=3)
        self.assertEqual(base64.b64decode(part["data"]), "世".encode())
        same = self.read(p, self.proj, sig=r["sig"])
        self.assertTrue(same["same"])
        self.assertNotIn("data", same)
        self.assertEqual(self.read(self.proj + "/big.log", self.proj)["status"], 413)
        head = self.read(self.proj + "/big.log", self.proj, max_size=1 << 40, length=10)
        self.assertEqual(base64.b64decode(head["data"]), b"\x00" * 10)
        big = self.read(p, self.proj, length=1 << 30)                    # 每次最多 4MB（这里文件本身更小）
        self.assertEqual(len(base64.b64decode(big["data"])), r["size"])
        self.assertEqual(self.read(p, self.proj, offset=-1)["status"], 400)
        self.assertEqual(self.read(p, self.proj, max_size=True)["status"], 400)

    def test_read_policy(self):
        cases = [
            (self.proj + "/.env", self.proj, 403), (self.proj + "/link_key.txt", self.proj, 403),
            (self.proj + "/sshdir/id_ed25519", self.proj, 403), (self.proj + "/link_out.txt", self.proj, 403),
            (self.outside + "/o.txt", self.proj, 403),                     # 根目录以外
            (self.proj + "/a.txt", self.home, 403),                        # 根目录是宽泛目录（home）
            (self.proj + "/a.txt", self.td, 403),
            (self.proj + "/../outside/o.txt", self.td, 403), (self.proj + "/./a.txt", self.proj, 403),
            (self.proj + "/sub", self.proj, 404), (self.proj + "/pipe", self.proj, 403),
            (self.proj + "/nope", self.proj, 404), ("/dev/null", "/dev", 403), ("rel.txt", self.proj, 400),
            (self.proj + "/a.txt", "rel", 400), (self.proj + "/a.txt", self.proj + "/.git", 403),
        ]
        for path, root, want in cases:
            r = self.read(path, root)
            self.assertEqual(r.get("status"), want, (path, root, r))
            self.assertNotIn("data", r)
        self.assertIn("data", self.read(self.outside + "/o.txt", self.outside))   # stat 给的根目录（目标所在目录）
        self.assertIn("data", self.read(self.home + "/top.txt", self.home + "/top.txt"))   # 文件本身

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_permission_denied(self):
        p = self.proj + "/secret.txt"
        with open(p, "wb") as f:
            f.write(b"x")
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o600)
        self.assertFalse(self.stat1(p)["readable"])
        r = self.read(p, self.proj)
        self.assertEqual((r["status"], r["reason"]), (403, "没有读取权限"))

    def test_rechecks_after_open(self):
        p = self.proj + "/a.txt"
        with mock.patch.object(node_agent, "_fd_path", lambda fd: self.home + "/.ssh/id_ed25519"):
            self.assertEqual(self.read(p, self.proj)["status"], 403)          # 打开后的真实路径落进 .ssh
        with mock.patch.object(node_agent, "_fd_path", lambda fd: self.outside + "/o.txt"):
            self.assertEqual(self.read(p, self.proj)["status"], 403)          # 打开后不在根目录内
        stale = os.lstat(self.proj + "/css/a.css")
        orig = node_agent.FileCtx.resolve
        with mock.patch.object(node_agent.FileCtx, "resolve", lambda s, x, c=None: orig(s, x, c)[:2] + (stale,)):
            self.assertEqual(self.read(p, self.proj)["status"], 409)          # 检查之后文件被换掉
        self.assertEqual(node_agent._fd_path(-1), None)

    def test_registered_async_and_separate_pool(self):
        self.assertIs(node_agent.OPS["file_stat"], node_agent.op_file_stat)
        self.assertIs(node_agent.OPS["file_read"], node_agent.op_file_read)
        self.assertTrue({"file_stat", "file_read", "blob"} <= node_agent.ASYNC_OPS)
        self.assertIsNot(node_agent._file_sem, node_agent._async_sem)

    def test_source_has_no_writes_in_file_ops(self):
        with open(os.path.join(ROOT, "ccr", "node_agent.py"), encoding="utf-8") as f:
            src = f.read()
        a = src.index("服务器文件预览（只读")
        b = src.index("OPS = {", a)
        seg = src[a:b]
        for bad in ("O_CREAT", "O_WRONLY", "O_RDWR", "O_TRUNC", "mkdir", "unlink", "rename", "chmod", "write("):
            self.assertNotIn(bad, seg)
        self.assertIn("O_NOFOLLOW", seg)
        self.assertNotIn("'", seg)   # 源码要经两层 shlex.quote 传给远端：单引号会膨胀命令长度


class TextEncTests(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(TE.detect("你好".encode()), "utf-8")
        self.assertEqual(TE.detect(GBK_TEXT.encode("gb18030")), "gb18030")
        self.assertEqual(TE.detect(GBK_TEXT.encode("gbk")), "gb18030")
        self.assertIsNone(TE.detect(png_bytes()))                           # 二进制
        self.assertIsNone(TE.detect(b"abc\x00def"))
        self.assertIsNone(TE.detect(bytes(range(1, 9)) * 20))               # 控制字符太多
        self.assertEqual(TE.detect(b"\x1b[31mred\x1b[0m\n\tok\r\n"), "utf-8")   # ANSI 日志不算二进制
        u = "中文".encode()
        self.assertIsNone(TE.detect(u[:-1]))                                 # 不是 partial：截断的 UTF-8 不算
        self.assertEqual(TE.detect(u + u[:2], partial=True), "utf-8")       # 截断在多字节字符中间
        self.assertEqual(TE.detect(b"\xef\xbb\xbfabc"), "utf-8")
        self.assertEqual(TE.detect(b"a" * ((1 << 20) - 1) + "中".encode()), "utf-8")   # 多字节字符跨 1MB 分块边界
        self.assertEqual(TE.detect(b"a" * ((1 << 20) - 1) + "中".encode("gb18030")), "gb18030")

    def test_with_charset(self):
        g = GBK_TEXT.encode("gb18030")
        self.assertEqual(TE.with_charset("text/plain; charset=utf-8", g), ("text/plain; charset=gb18030", "gb18030"))
        self.assertEqual(TE.with_charset("text/plain; charset=utf-8", b"ok"), ("text/plain; charset=utf-8", None))
        self.assertEqual(TE.with_charset("text/html; charset=utf-8", g), ("text/html", "gb18030"))   # 交给页面的 <meta charset>
        self.assertEqual(TE.with_charset("image/png", g), ("image/png", None))
        self.assertEqual(TE.to_utf8(g, "gb18030").decode(), GBK_TEXT)

    def test_preview_content_type_and_text_body(self):
        g = GBK_TEXT.encode("gb18030")
        self.assertEqual(P.content_type("/x/README", g), P.TEXT)            # 未知 / 无扩展名的 GBK 文本不再当二进制下载
        self.assertEqual(P.content_type("/x/a.unknownext", b"\x00\x01"), "application/octet-stream")
        ct, body, extra = P.text_body("/x/a.txt", g)
        self.assertEqual((ct, body, extra), ("text/plain; charset=gb18030", g, [("X-CCR-Charset", "gb18030")]))
        ct, body, extra = P.text_body("/x/a.txt", g, utf8=True)
        self.assertEqual((ct, body.decode(), extra), ("text/plain; charset=utf-8", GBK_TEXT, [("X-CCR-Charset", "gb18030")]))
        self.assertEqual(P.text_body("/x/a.png", png_bytes())[0], "image/png")


class SessionFileOpTests(unittest.TestCase):
    def test_file_op_only_whitelisted_ops_to_session_job(self):
        mgr = SM.SessionManager.__new__(SM.SessionManager)
        s = SM._Session("s0123456789ab", 10)
        s.jobid = "276403"
        mgr._get = lambda sid: s
        mgr._req = mock.Mock(return_value={"results": []})
        mgr.file_op(s.sid, "file_stat", timeout=5, paths=["a"])
        mgr._req.assert_called_once_with("276403", "file_stat", timeout=5, paths=["a"])
        for bad in ("blob", "keys", "paste", "start"):
            with self.assertRaises(ValueError):
                mgr.file_op(s.sid, bad)


# ---------------------------------------------------------------- 后端 + 真正的 agent 子进程
class AgentStub:
    """把 StubManager.file_op 接到真正的 node_agent 子进程（与 AgentLink + SessionManager._req 相同的错误语义）。"""

    def __init__(self, home):
        env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "en_US.UTF-8"}
        self.proc = AgentProc(env)
        self.calls = []
        self.down = None
        self.lock = threading.Lock()

    def file_op(self, sid, op, timeout=30.0, **params):
        with self.lock:
            self.calls.append((op, dict(params)))
        if self.down is not None:
            raise self.down
        with self.lock:   # AgentProc.call 不是线程安全的（按顺序配对响应）
            r = self.proc.call(op, timeout=timeout, **params)
        if not r["ok"]:
            raise SM.OpError("远端操作失败：%s" % r["error"], code="remote_error", status=502)
        return r["result"]


class ServerFilesHttpTests(Base):
    def setUp(self):
        super().setUp()
        self.home, self.proj, self.outside = make_tree(os.path.realpath(self.tmp.name))
        self.agent = AgentStub(self.home)
        self.addCleanup(self.agent.proc.close)
        self.mgr.file_op = self.agent.file_op
        now = time.time()
        base = {"title": "t", "cwd": self.proj, "state": "idle", "created_at": now, "last_activity": now,
                "link": "ready", "launch": {}, "claude_session_id": None}
        self.mgr.metas["srem"] = dict(base, sid="srem", jobid="276403")
        self.mgr.metas["sloc"] = dict(base, sid="sloc", jobid="local")

    def stat(self, paths, sid="srem"):
        st, js, _, raw = self.req("POST", "/api/sessions/%s/srvfile/stat" % sid, {"paths": paths})
        return st, js, raw

    def entry(self, path):
        st, js, raw = self.stat([path])
        self.assertEqual(st, 200, raw)
        return js["results"][0]

    def get(self, url, headers=None, method="GET"):
        return self.req(method, url, auth=False, headers=headers)

    def test_stat_entries(self):
        st, js, raw = self.stat(["a.txt", "gbk.txt", "sub", ".env", "nope.txt", "big.log", "pic.png", "link_out.txt", 3])
        self.assertEqual(st, 200, raw)
        self.assertEqual((js["preview_max"], js["download_max"], js["remote"]), (SF.PREVIEW_MAX, SF.DOWNLOAD_MAX, True))
        a, g, d, hid, no, big, pic, lo, bad = js["results"]
        self.assertTrue(a["ok"] and a["remote"] and not a["openable"])
        self.assertEqual((a["kind"], a["type"], a["real"]), ("text", "file", self.proj + "/a.txt"))
        self.assertTrue(a["url"].startswith("/preview/") and a["url"].endswith(self.proj + "/a.txt"))
        self.assertEqual(a["download_url"], a["url"] + "?download=1")
        self.assertEqual(a["url"].split("/")[2], g["url"].split("/")[2])        # 同一根目录同一个 cap
        self.assertEqual((d["exists"], d["type"], d["url"]), (True, "dir", None))
        for e in (hid, no, bad):
            self.assertFalse(e["exists"])
            self.assertIsNone(e["url"])
        self.assertTrue(big["ok"] and big["too_large"] and big["downloadable"])
        self.assertIn("预览上限", big["reason"])
        self.assertEqual(pic["kind"], "image")
        self.assertNotEqual(lo["url"].split("/")[2], a["url"].split("/")[2])   # cwd 以外：另一个 cap
        # 只对服务器会话；要 token；最多 200 个
        st, js, _ = self.stat(["a.txt"], sid="sloc")
        self.assertEqual(st, 403)
        self.assertEqual(self.req("POST", "/api/sessions/srem/srvfile/stat", {"paths": []}, auth=False)[0], 401)
        self.assertEqual(self.stat(["a"] * 201)[0], 400)
        self.assertEqual(self.stat(["a"], sid="nosuch")[0], 404)

    def test_serve_headers_same_as_local_preview(self):
        u = self.entry("index.html")["url"]
        st, _, r, raw = self.get(u)
        self.assertEqual((st, raw), (200, b"<link rel=stylesheet href=css/a.css><p>hi</p>"))
        self.assertEqual(r.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(r.getheader("Content-Security-Policy"), P.HTML_CSP)
        for k, v in (("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("Cache-Control", "no-store"), ("Cross-Origin-Resource-Policy", "cross-origin")):
            self.assertEqual(r.getheader(k), v, k)
        self.assertIsNone(r.getheader("X-Frame-Options"))
        prefix = u[:u.rindex("/") + 1]
        st, _, r, raw = self.get(prefix + "css/a.css", headers={"Origin": "null", "Sec-Fetch-Dest": "style"})
        self.assertEqual((st, raw, r.getheader("Content-Type")), (200, b"p{color:red}", "text/css; charset=utf-8"))
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "null")
        self.assertEqual(r.getheader("Content-Security-Policy"), P.STRICT_CSP)
        st, _, r, raw = self.get(prefix + "app.js", headers={"Origin": "null", "Sec-Fetch-Dest": "empty"})
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))      # fetch/XHR 读不到
        st, _, r, raw = self.get(self.entry("pic.png")["url"])
        self.assertEqual((st, r.getheader("Content-Type"), raw), (200, "image/png", png_bytes()))
        st, _, r, _ = self.get(self.entry("doc.pdf")["url"])
        self.assertEqual((st, r.getheader("Content-Type")), (200, "application/pdf"))
        self.assertIsNone(r.getheader("Content-Security-Policy"))
        st, _, r, raw = self.get(u, method="HEAD")
        self.assertEqual((st, raw, r.getheader("Content-Length")), (200, b"", str(len(b"<link rel=stylesheet href=css/a.css><p>hi</p>"))))

    def test_gbk_charset_transcode_and_head(self):
        u = self.entry("gbk.txt")["url"]
        st, _, r, raw = self.get(u)
        self.assertEqual((st, raw), (200, GBK_TEXT.encode("gb18030")))
        self.assertEqual((r.getheader("Content-Type"), r.getheader("X-CCR-Charset")), ("text/plain; charset=gb18030", "gb18030"))
        st, _, r, raw = self.get(u + "?ccr_utf8=1")
        self.assertEqual((raw.decode("utf-8"), r.getheader("Content-Type")), (GBK_TEXT, "text/plain; charset=utf-8"))
        st, _, r, raw = self.get(self.entry("a.txt")["url"] + "?ccr_head=7")
        self.assertEqual((st, raw, r.getheader("X-CCR-Partial"), r.getheader("X-CCR-Size")),
                         (200, "hello 世".encode()[:7], "1", str(len("hello 世界\n".encode()))))
        self.assertEqual(r.getheader("Content-Type"), "text/plain; charset=utf-8")   # 截在多字节字符中间仍是 UTF-8
        st, _, r, raw = self.get(self.entry("big.log")["url"] + "?ccr_head=16")      # 超过预览上限的文件也能看开头
        self.assertEqual((st, len(raw), r.getheader("X-CCR-Partial")), (200, 16, "1"))
        self.assertEqual(self.get(u + "?ccr_head=x")[0], 400)

    def test_local_preview_gbk_charset_too(self):
        """本机文件预览同样按内容标 charset、支持 ?ccr_utf8=1（GBK 预览本机与服务器共用）。"""
        st, js, _, raw = self.req("POST", "/api/sessions/sloc/local/stat", {"paths": ["gbk.txt"]})
        self.assertEqual(st, 200, raw)
        u = js["results"][0]["url"]
        self.assertFalse(self.srv.srvfiles.owns(u.split("/")[2]))         # 本机 cap 不归 srvfiles 管
        st, _, r, raw = self.get(u)
        self.assertEqual((st, raw, r.getheader("Content-Type"), r.getheader("X-CCR-Charset")),
                         (200, GBK_TEXT.encode("gb18030"), "text/plain; charset=gb18030", "gb18030"))
        st, _, r, raw = self.get(u + "?ccr_utf8=1")
        self.assertEqual((raw.decode(), r.getheader("Content-Type")), (GBK_TEXT, "text/plain; charset=utf-8"))
        self.assertEqual(self.agent.calls, [])                             # 本机文件不经 agent

    def test_rejections(self):
        u = self.entry("a.txt")["url"]
        cap = u.split("/")[2]
        prefix = "/preview/" + cap
        for path, status in ((prefix + self.proj + "/.env", 403), (prefix + self.proj + "/css/../.env", 403),
                             (prefix + self.proj + "/%2e%2e/outside/o.txt", 403), (prefix + self.proj + "/link_out.txt", 403),
                             (prefix + self.outside + "/o.txt", 403), (prefix + self.proj + "/sub", 404),
                             (prefix + self.proj + "/nope.txt", 404), (prefix + self.proj + "/%ff.txt", 400),
                             (prefix + self.proj + "/big.log", 413), (prefix + self.proj + "/pipe", 403)):
            st, js, _, raw = self.get(path)
            self.assertEqual(st, status, (path, raw[:200]))
            self.assertNotIn(b"OUTSIDE", raw)
            self.assertNotIn(b"KEY=1", raw)
        self.assertIn("下载", self.get(prefix + self.proj + "/big.log")[1]["error"])
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req(m, u, auth=False, raw_body=b"")[0], 405)
        self.assertEqual(self.req("GET", u, auth=False, host="evil.example:%d" % self.port)[0], 403)
        # 本机会话的 cap 与服务器会话的 cap 互不通用；会话变成本机 / 被删 → 旧 cap 失效
        self.mgr.metas["srem"]["jobid"] = "local"
        self.assertEqual(self.get(u)[0], 404)
        self.mgr.metas["srem"]["jobid"] = "276403"
        self.assertEqual(self.get(u)[0], 200)
        self.assertEqual(self.req("DELETE", "/api/sessions/srem")[0], 200)
        self.assertIsNone(self.srv.preview.lookup(cap))
        self.assertEqual(self.get(u)[0], 404)

    def test_cache_reuses_until_file_changes(self):
        u = self.entry("a.txt")["url"]
        self.assertEqual(self.get(u)[3], "hello 世界\n".encode())
        n = len(self.agent.calls)
        self.assertEqual(self.get(u)[3], "hello 世界\n".encode())
        op, params = self.agent.calls[n]
        self.assertEqual((op, len(params["sig"])), ("file_read", 4))      # 第二次带签名，agent 只回 same
        with open(self.proj + "/a.txt", "wb") as f:
            f.write(b"changed!")
        os.utime(self.proj + "/a.txt", (time.time() + 5, time.time() + 5))
        self.assertEqual(self.get(u)[3], b"changed!")

    def test_multi_chunk_read_and_change_detection(self):
        data = os.urandom(5000)
        with open(self.proj + "/blob.bin", "wb") as f:
            f.write(data)
        u = self.entry("blob.bin")["url"]
        with mock.patch.object(SF, "CHUNK", 1024):
            st, _, r, raw = self.get(u)
            self.assertEqual((st, raw, r.getheader("Content-Type")), (200, data, "application/octet-stream"))
            self.assertEqual(sum(1 for c in self.agent.calls if c[0] == "file_read"), 5)
            # 读到一半文件变了：重读一次；一直在变 → 409
            sf = self.srv.srvfiles
            real = sf._read_chunk
            flips = {"n": 0}

            def flaky(*a, **kw):
                r = real(*a, **kw)
                if a[3] > 0:
                    flips["n"] += 1
                    r = dict(r, sig=r["sig"][:1] + [flips["n"]] + r["sig"][2:])
                return r
            sf._cache.clear()
            with mock.patch.object(sf, "_read_chunk", flaky):
                st, js, _, _ = self.get(u)
            self.assertEqual(st, 409)
            self.assertIn("变化", js["error"])

    def test_download_attachment_streaming(self):
        data = os.urandom(70000)
        name = "结果 (1).bin"
        with open(os.path.join(self.proj, name), "wb") as f:
            f.write(data)
        e = self.entry(name)
        with mock.patch.object(SF, "CHUNK", 16384):
            st, _, r, raw = self.get(e["download_url"])
        self.assertEqual((st, raw), (200, data))
        cd = r.getheader("Content-Disposition")
        self.assertTrue(cd.startswith('attachment; filename="'), cd)
        self.assertIn("filename*=UTF-8''%E7%BB%93%E6%9E%9C%20%281%29.bin", cd)
        self.assertEqual((r.getheader("Content-Type"), r.getheader("Content-Length")), ("application/octet-stream", str(len(data))))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        # 超过预览上限但在下载上限内：可以下载（这里只看响应头，不把 20MB 读完）
        big = self.entry("big.log")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", big["download_url"], headers={"Host": self.host})
        resp = conn.getresponse()
        self.assertEqual((resp.status, resp.getheader("Content-Length")), (200, str(SF.PREVIEW_MAX + 1)))
        self.assertEqual(resp.read(10), b"\x00" * 10)
        conn.close()

    def test_download_aborts_when_file_changes(self):
        data = os.urandom(50000)
        with open(self.proj + "/grow.bin", "wb") as f:
            f.write(data)
        e = self.entry("grow.bin")
        sf = self.srv.srvfiles
        real = sf._read_chunk

        def changing(*a, **kw):
            r = real(*a, **kw)
            return dict(r, sig=[0, 0, 0, 0]) if a[3] > 0 else r
        with mock.patch.object(SF, "CHUNK", 16384), mock.patch.object(sf, "_read_chunk", changing), \
                self.assertLogs("ccr.srvfiles", "WARNING"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", e["download_url"], headers={"Host": self.host})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            with self.assertRaises(http.client.IncompleteRead):
                resp.read()
            conn.close()

    def test_link_down_busy_and_errors(self):
        u = self.entry("a.txt")["url"]
        self.agent.down = SM.LinkNotReady("与计算节点的连接已断开，正在重连…")
        st, js, _ = self.stat(["a.txt"])
        self.assertEqual((st, js["code"]), (503, "link_not_ready"))
        st, js, _, _ = self.get(u)
        self.assertEqual(st, 503)
        self.assertIn("连接已断开", js["error"])
        self.agent.down = SM.OpError("远端操作超时（file_read）", code="remote_timeout", status=504)
        self.assertEqual(self.get(u)[0], 504)
        self.agent.down = SM.OpError("作业 276403 已结束", code="job_ended")
        st, js, _, _ = self.get(u)
        self.assertEqual((st, js["error"]), (409, "作业 276403 已结束"))
        self.agent.down = None
        self.assertEqual(self.get(u)[0], 200)
        # 排队上限：等待中的读取太多 → 503（不会无限堆积在作业链路上）
        sf = self.srv.srvfiles
        with mock.patch.object(sf, "_waiting", SF.MAX_WAITING):
            st, js, _, _ = self.get(u)
        self.assertEqual(st, 503)
        self.assertIn("过多", js["error"])
        # 同时最多 2 个下载
        for _ in range(SF.MAX_DOWNLOADS):
            sf._downloads.acquire()
        try:
            st, js, _, _ = self.get(u + "?download=1")
            self.assertEqual(st, 503)
        finally:
            for _ in range(SF.MAX_DOWNLOADS):
                sf._downloads.release()

    def test_fake_mode_still_refuses_app_data_dir(self):
        """fake 模式（agent 就在本机）：本地再按身份查一遍本程序的数据目录。"""
        data = os.path.realpath(os.path.join(self.tmp.name, "data"))
        os.makedirs(data, exist_ok=True)
        with open(os.path.join(data, "api_keys.json"), "w") as f:
            f.write("{}")
        with mock.patch.object(self.srv.preview, "cfg", lambda: {"fake": True, "fake_home": self.home}), \
                mock.patch.object(self.srv.preview, "data_dir", data):
            e = self.entry(os.path.join(data, "api_keys.json"))
            self.assertFalse(e["exists"])
            self.assertIn("数据目录", e["reason"])
            self.assertTrue(self.entry("a.txt")["ok"])                     # 假 HOME 里的文件照常


class TextDecodeJsTests(unittest.TestCase):
    """static/textdecode.js（node 跑纯函数）。"""

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_decode(self):
        js = r"""
import { decodeText, looksBinary, _setGbSupport, fetchText } from '%s/static/textdecode.js';
const enc = (s) => new TextEncoder().encode(s);
const gbk = Uint8Array.from(%s);
const out = {};
out.utf8 = decodeText(enc('你好 world'));
out.gbk = decodeText(gbk);
out.bom = decodeText(Uint8Array.from([0xef, 0xbb, 0xbf, 0x61]));
out.u16 = decodeText(Uint8Array.from([0xff, 0xfe, 0x2d, 0x4e, 0x87, 0x65]));
out.bin = decodeText(Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0, 1, 2]));
out.ctrl = looksBinary(Uint8Array.from(Array(200).fill(1)));
out.ansi = decodeText(enc('\x1b[31mred\x1b[0m\n')).encoding;
const u = enc('中文');
out.partial = decodeText(Uint8Array.from([...u, ...u.slice(0, 2)]), { partial: true });
out.lossy = decodeText(Uint8Array.from([0x61, 0xff, 0x62]));
_setGbSupport(false);
out.nogb = decodeText(gbk);
const calls = [];
const resp = (body, headers) => ({ ok: true, status: 200, headers: new Map(Object.entries(headers || {})), arrayBuffer: async () => body.buffer, json: async () => ({}) });
const fetchFn = async (url) => { calls.push(url); return url.includes('ccr_utf8=1') ? resp(enc('中文测试'), { 'X-CCR-Charset': 'gb18030' }) : resp(gbk, { 'X-CCR-Charset': 'gb18030', 'X-CCR-Partial': '1', 'X-CCR-Size': '99' }); };
out.fallback = await fetchText('/preview/x/a.txt?ccr_head=5', { fetchFn });
out.calls = calls;
_setGbSupport(null);
const bad = async () => ({ ok: false, status: 503, headers: new Map(), json: async () => ({ error: '与计算节点的连接已断开' }) });
try { await fetchText('/x', { fetchFn: bad }); } catch (e) { out.err = e.message; }
console.log(JSON.stringify(out));
""" % (ROOT, json.dumps(list(GBK_TEXT.encode("gb18030"))))
        r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout)
        self.assertEqual((o["utf8"]["text"], o["utf8"]["encoding"], o["utf8"]["label"]), ("你好 world", "utf-8", ""))
        self.assertEqual((o["gbk"]["text"], o["gbk"]["encoding"], o["gbk"]["label"]), (GBK_TEXT, "gb18030", "GB18030"))
        self.assertEqual((o["bom"]["text"], o["bom"]["encoding"]), ("a", "utf-8-bom"))
        self.assertEqual((o["u16"]["text"], o["u16"]["encoding"]), ("中文", "utf-16le"))
        self.assertTrue(o["bin"]["binary"] and o["ctrl"])
        self.assertEqual(o["ansi"], "utf-8")
        self.assertEqual((o["partial"]["text"], o["partial"]["encoding"]), ("中文", "utf-8"))
        self.assertTrue(o["lossy"]["lossy"])
        self.assertEqual((o["nogb"]["encoding"], o["nogb"]["lossy"]), ("utf-8", True))   # 不支持 GB18030 时不会误报
        fb = o["fallback"]
        self.assertEqual((fb["text"], fb["label"], fb["transcoded"], fb["partial"], fb["size"]), ("中文测试", "GB18030", True, True, 99))
        self.assertEqual(o["calls"], ["/preview/x/a.txt?ccr_head=5", "/preview/x/a.txt?ccr_head=5&ccr_utf8=1"])
        self.assertEqual(o["err"], "与计算节点的连接已断开")


if __name__ == "__main__":
    unittest.main()
