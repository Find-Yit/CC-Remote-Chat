"""dmg 版自动更新（ccr/updater.py，SPEC §14.11）测试。不联网：本机 127.0.0.1 起 https 服务（openssl 生成的临时证书）。

- 版本比较、清单校验（坏 JSON、非 https、主机不在白名单、版本不大于当前、超大）、UPDATE.json 读取；
- 检查：404 安静处理、关闭开关后不检查；
- 安装：sha256 不符、签名钉死不符（ad-hoc / 另一张测试证书 → 拒绝）、正确证书 → 换装（带空格路径的假 bundle）、
  回滚、EPERM/EACCES 退回手动方案（open 已下载的 dmg 只记录、不执行）、有会话忙时等待 / 取消、
  SIGTERM 假的原生壳（拷到 Contents/MacOS/ 的 sleep）+ 注入的启动器收到 bundle 路径、新版本启动后清理旧 .app；
- 服务器：没有 updater 时 /api/update 不存在、bootstrap 没有 update 键（私有版不变）。

测试证书：tools/release_key.sh init --dir <临时目录>（单独的钥匙串，不进搜索列表；前后核对搜索列表不变）。
红线：不碰 ~/.cc-remote-chat*、不运行 open、不联网、不碰真实 tmux。
"""
import hashlib
import http.server
import json
import os
import pathlib
import plistlib
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ccr import updater as U
from tests.test_session import wait_until

ROOT = pathlib.Path(__file__).resolve().parent.parent
OPENSSL = "/usr/bin/openssl"
HAVE_TOOLS = all(os.path.exists(p) for p in (OPENSSL, "/usr/bin/codesign", "/usr/bin/hdiutil", "/usr/bin/ditto",
                                              "/usr/bin/security"))
TMP = None
KEYS = {}        # "a" / "b" → {"dir", "sha1", "sha256"}
SERVER_CERT = None


def _snap_keychains():
    a = subprocess.run(["/usr/bin/security", "list-keychains", "-d", "user"], capture_output=True, text=True).stdout
    b = subprocess.run(["/usr/bin/security", "default-keychain", "-d", "user"], capture_output=True, text=True).stdout
    return a + "|" + b


_KC_BEFORE = None


def setUpModule():
    global TMP, SERVER_CERT, _KC_BEFORE
    if not HAVE_TOOLS:
        return
    TMP = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="ccr-updt.", dir="/tmp")))
    _KC_BEFORE = _snap_keychains()
    for k in ("a", "b"):
        d = TMP / ("key " + k)
        r = subprocess.run(["/bin/bash", str(ROOT / "tools" / "release_key.sh"), "init", "--dir", str(d),
                            "--cn", "CCR Test %s" % k.upper()], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("release_key.sh init 失败：" + r.stdout + r.stderr)
        fp = dict(line.split() for line in (d / "fingerprints.txt").read_text().splitlines())
        KEYS[k] = {"dir": d, "sha1": fp["SHA-1"], "sha256": fp["SHA-256"]}
    # https 服务的证书（127.0.0.1）
    cert, key = TMP / "srv.pem", TMP / "srv.key"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "2", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=60)
    SERVER_CERT = (str(cert), str(key))


def tearDownModule():
    if TMP is not None:
        shutil.rmtree(TMP, True)   # 钥匙串文件直接删（不用 security delete-keychain，免得动搜索列表）
        if _KC_BEFORE is not None and _snap_keychains() != _KC_BEFORE:
            raise AssertionError("钥匙串搜索列表 / 默认钥匙串在测试前后不一致")


# ============================================================================ 小工具
class Https:
    """本机 https 服务：routes[path] = (status, headers, body) 或 callable(handler) → 同样的三元组。"""

    def __init__(self, routes):
        self.routes = routes
        self.hits = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append((self.path, dict(self.headers)))
                r = outer.routes.get(self.path)
                if callable(r):
                    r = r(self)
                if r is None:
                    r = (404, {}, b"not found")
                st, hd, body = r
                self.send_response(st)
                for k, v in hd.items():
                    self.send_header(k, v)
                if "Content-Length" not in hd:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*SERVER_CERT)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.base = "https://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def source(key="a", **kw):
    k = KEYS[key]
    args = dict(base=kw.pop("base"), allowed_hosts=kw.pop("allowed_hosts", {"127.0.0.1"}), cafile=SERVER_CERT[0])
    return U.Source("Owner/CC-Remote-Chat", k["sha1"], k["sha256"], **args)


def make_bundle(parent: pathlib.Path, version: str, build: str, sign="a", name="CC Remote Chat.app",
                bundle_id=U.BUNDLE_ID) -> pathlib.Path:
    app = parent / name
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "app").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / "CC Remote Chat"
    shutil.copyfile("/bin/sleep", exe)   # 不能 copy2：/bin/sleep 带 restricted 标志
    os.chmod(exe, 0o755)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "CC Remote Chat", "CFBundleName": "CC Remote Chat",
                       "CFBundlePackageType": "APPL", "CCRBundled": True, "CCRVersion": version}, f)
    (app / "Contents" / "Resources" / "app" / "BUILD").write_text(build + "\n")
    (app / "Contents" / "Resources" / "app" / "app.py").write_text("# %s\n" % version)
    if sign == "adhoc":
        subprocess.run(["/usr/bin/codesign", "-f", "-s", "-", str(exe)], check=True, capture_output=True)
        subprocess.run(["/usr/bin/codesign", "-f", "-s", "-", str(app)], check=True, capture_output=True)
    else:
        k = KEYS[sign]
        pw = (k["dir"] / "keychain-password").read_text().strip()
        kc = str(k["dir"] / "release.keychain-db")
        subprocess.run(["/usr/bin/security", "unlock-keychain", "-p", pw, kc], check=True, capture_output=True)
        for t in (exe, app):
            subprocess.run(["/usr/bin/codesign", "-f", "-s", k["sha1"], "--keychain", kc, "--timestamp=none", str(t)],
                           check=True, capture_output=True, timeout=60)
        subprocess.run(["/usr/bin/security", "lock-keychain", kc], capture_output=True)
    return app


def make_dmg(app: pathlib.Path, out: pathlib.Path) -> bytes:
    src = out.parent / (out.stem + "-src")
    src.mkdir()
    subprocess.run(["/usr/bin/ditto", str(app), str(src / app.name)], check=True)
    subprocess.run(["/usr/bin/hdiutil", "create", "-quiet", "-volname", "CC Remote Chat", "-srcfolder", str(src),
                    "-fs", "HFS+", "-format", "UDZO", "-ov", str(out)], check=True, timeout=120)
    shutil.rmtree(src)
    return out.read_bytes()


def manifest(version, build, dmg_bytes, name="CC-Remote-Chat-%s.dmg", **over):
    m = {"version": version, "build": build, "notes": "测试更新说明\n- 第二行",
         "min_macos": "12.0", "dmg": {"name": name % version, "sha256": hashlib.sha256(dmg_bytes).hexdigest(),
                                      "size": len(dmg_bytes)}}
    m.update(over)
    return m


# ============================================================================ 纯函数
class TestVersions(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(U.parse_version("2.12"), (2, 12))
        self.assertEqual(U.parse_version("2.12.1-test"), (2, 12, 1))
        self.assertEqual(U.parse_version("2.12.0"), (2, 12))
        for bad in ("", "v2.12", "2.12 ", "2..1", "2.12-", "a.b", None, 2.12, "1.22.333.4444.5", "2.12-a b"):
            self.assertIsNone(U.parse_version(bad), repr(bad))
        self.assertTrue(U.is_newer("2.12.1-test", "2.12-test"))
        self.assertTrue(U.is_newer("2.13", "2.12.9"))
        self.assertTrue(U.is_newer("10.0", "9.99"))
        self.assertFalse(U.is_newer("2.12", "2.12"))
        self.assertFalse(U.is_newer("2.12.0", "2.12"))
        self.assertFalse(U.is_newer("2.12-b", "2.12-a"))
        self.assertFalse(U.is_newer("2.11", "2.12"))
        self.assertFalse(U.is_newer("garbage", "2.12"))


class TestManifest(unittest.TestCase):
    def good(self, **over):
        m = {"version": "2.13", "build": "2.13 20261201T000000Z abcdef", "notes": "x",
             "min_macos": "12.0", "dmg": {"name": "CC-Remote-Chat-2.13.dmg", "sha256": "a" * 64, "size": 1234}}
        m.update(over)
        return m

    def test_valid(self):
        r = U.validate_manifest(self.good(), "2.12", macos="14.5")
        self.assertTrue(r["newer"] and r["supported"])
        self.assertFalse(U.validate_manifest(self.good(version="2.12"), "2.12", macos="14.5")["newer"])
        self.assertFalse(U.validate_manifest(self.good(version="2.11"), "2.12", macos="14.5")["newer"])
        self.assertFalse(U.validate_manifest(self.good(min_macos="15.0"), "2.12", macos="14.5")["supported"])

    def test_invalid(self):
        bads = [[], "x", self.good(version="v2.13"), self.good(version=213), self.good(dmg="x"),
                self.good(dmg={"name": "../x.dmg", "sha256": "a" * 64, "size": 1}),
                self.good(dmg={"name": "CC Remote Chat 2.13.dmg", "sha256": "a" * 64, "size": 1}),
                self.good(dmg={"name": "x.zip", "sha256": "a" * 64, "size": 1}),
                self.good(dmg={"name": "x.dmg", "sha256": "A" * 64, "size": 1}),
                self.good(dmg={"name": "x.dmg", "sha256": "a" * 63, "size": 1}),
                self.good(dmg={"name": "x.dmg", "sha256": "a" * 64, "size": 0}),
                self.good(dmg={"name": "x.dmg", "sha256": "a" * 64, "size": U.DMG_MAX + 1}),
                self.good(dmg={"name": "x.dmg", "sha256": "a" * 64, "size": True}),
                self.good(notes="x" * (U.NOTES_MAX + 1)), self.good(notes=5), self.good(build="a\nb"),
                self.good(min_macos="twelve")]
        for b in bads:
            with self.assertRaises(U.UpdateError, msg=repr(b)[:120]):
                U.validate_manifest(b, "2.12", macos="14.5")

    def test_urls_built_from_repo_not_manifest(self):
        s = U.Source("Owner/CC-Remote-Chat", "a" * 40, "b" * 64)
        self.assertEqual(s.manifest_url(), "https://github.com/Owner/CC-Remote-Chat/releases/latest/download/update.json")
        self.assertEqual(s.dmg_url("2.13", "CC-Remote-Chat-2.13.dmg"),
                         "https://github.com/Owner/CC-Remote-Chat/releases/download/v2.13/CC-Remote-Chat-2.13.dmg")
        self.assertEqual(s.requirement(), 'identifier "local.cc-remote-chat.bundled" and certificate leaf = H"%s"' % ("a" * 40))
        for bad in ("x", "a/b/c", "../x", "a/..", "-a/b", "a/b c"):
            with self.assertRaises(ValueError, msg=bad):
                U.Source(bad, "a" * 40, "b" * 64)
        with self.assertRaises(ValueError):
            U.Source("a/b", "A" * 40, "b" * 64)

    def test_load_source(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(U.load_source(td))
            p = pathlib.Path(td) / "UPDATE.json"
            p.write_text(json.dumps({"repo": "Owner/CC-Remote-Chat", "cert_sha1": "a" * 40, "cert_sha256": "b" * 64}))
            self.assertEqual(U.load_source(td).repo, "Owner/CC-Remote-Chat")
            for bad in ("{", "[]", json.dumps({"repo": "x", "cert_sha1": "a" * 40, "cert_sha256": "b" * 64}),
                        json.dumps({"repo": "a/b", "cert_sha1": "a" * 40}), "x" * 5000):
                p.write_text(bad)
                self.assertIsNone(U.load_source(td), bad[:40])
        self.assertIsNone(U.load_source(ROOT))   # 仓库里没有 UPDATE.json：从源码运行永远不启用

    def test_create_disabled_cases(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(U.create(td, td, "2.12 x y"))   # 没有 UPDATE.json
            (pathlib.Path(td) / "UPDATE.json").write_text(json.dumps(
                {"repo": "a/b", "cert_sha1": "a" * 40, "cert_sha256": "b" * 64}))
            self.assertIsNone(U.create(td, td, None))          # 没有 BUILD
            self.assertIsNone(U.create(td, td, "bad x y"))     # BUILD 里的版本不合法
            self.assertIsNone(U.create(td, td, "2.12 x y", fake=True))   # --fake 没给测试源 → 关闭（绝不访问 GitHub）
            u = U.create(td, td, "2.12 x y")
            self.assertEqual((u.current, u.source.base, u.source.manifest_url()[:19]), ("2.12", U.GITHUB, "https://github.com/"))

    def test_busy_titles(self):
        ss = [{"sid": "1", "display_title": "空闲", "state": "idle", "subagents": {"running": 0}},
              {"sid": "2", "display_title": "回复中", "state": "busy", "working": True},
              {"sid": "3", "display_title": "审批", "state": "waiting"},
              {"sid": "4", "display_title": "启动", "state": "starting"},
              {"sid": "5", "display_title": "子代理", "state": "idle", "subagents": {"running": 2}},
              {"sid": "6", "display_title": "退出", "state": "exited"},
              {"sid": "7", "display_title": "信任", "state": "trust"}]
        self.assertEqual(U.busy_titles(ss), ["回复中", "审批", "启动", "子代理", "信任"])


# ============================================================================ 检查（https）
@unittest.skipUnless(HAVE_TOOLS, "需要 openssl / codesign / hdiutil / security")
class TestCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="ccr-updc.", dir=str(TMP)))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = Https({})
        self.addCleanup(self.srv.close)
        self.mpath = "/Owner/CC-Remote-Chat/releases/latest/download/update.json"

    def upd(self, **kw):
        u = U.Updater(self.tmp / "data", self.tmp / "app", source(base=self.srv.base), "2.12", "2.12 t h",
                      first_delay=kw.pop("first_delay", 3600), **kw)
        self.addCleanup(u.stop)
        return u

    def test_available_and_headers(self):
        self.srv.routes[self.mpath] = (200, {"Content-Type": "application/json"},
                                       json.dumps(manifest("2.13", "b", b"x" * 10)).encode())
        u = self.upd()
        self.assertEqual(u.check(), "available")
        st = u.status()
        self.assertEqual(st["available"]["version"], "2.13")
        self.assertFalse(st["available"]["snoozed"])
        self.assertEqual(st["last_check"]["result"], "available")
        hdrs = {k.lower(): v for k, v in self.srv.hits[0][1].items()}
        self.assertEqual(hdrs["user-agent"], "CC-Remote-Chat/2.12")
        self.assertFalse({"cookie", "authorization"} & set(hdrs))
        u.snooze("2.13")
        self.assertTrue(u.status()["available"]["snoozed"])
        u2 = self.upd(now=lambda: time.time() + U.SNOOZE_SECONDS + 5)   # 24 小时后不再算「稍后」
        u2.check()
        self.assertFalse(u2.status()["available"]["snoozed"])

    def test_404_quiet(self):
        u = self.upd()
        self.assertEqual(u.check(), "none")
        st = u.status()
        self.assertIsNone(st["available"])
        self.assertIsNone(st["last_check"]["error"])
        self.assertEqual(st["phase"], "idle")

    def test_not_newer_bad_json_oversize(self):
        u = self.upd()
        self.srv.routes[self.mpath] = (200, {}, json.dumps(manifest("2.12", "b", b"x")).encode())
        self.assertEqual(u.check(), "uptodate")
        self.srv.routes[self.mpath] = (200, {}, b"{not json")
        self.assertEqual(u.check(), "error")
        self.assertIn("JSON", u.status()["last_check"]["error"])
        self.srv.routes[self.mpath] = (200, {}, b" " * (U.MANIFEST_MAX + 10))
        self.assertEqual(u.check(), "error")
        self.assertIn("太大", u.status()["last_check"]["error"])
        self.srv.routes[self.mpath] = (500, {}, b"")
        self.assertEqual(u.check(), "error")

    def test_redirect_rules(self):
        u = self.upd()
        self.srv.routes[self.mpath] = (302, {"Location": "http://127.0.0.1:%d/x" % self.srv.port}, b"")
        self.assertEqual(u.check(), "error")
        self.assertIn("不在允许范围", u.status()["last_check"]["error"])
        self.srv.routes[self.mpath] = (302, {"Location": "https://localhost:%d/x" % self.srv.port}, b"")
        self.assertEqual(u.check(), "error")
        self.assertIn("不在允许范围", u.status()["last_check"]["error"])
        self.srv.routes["/ok"] = (200, {}, json.dumps(manifest("2.13", "b", b"x")).encode())
        self.srv.routes[self.mpath] = (302, {"Location": "https://127.0.0.1:%d/ok" % self.srv.port}, b"")
        self.assertEqual(u.check(), "available")
        # 起始地址本身不是 https / 不在白名单
        u.source.base = "http://127.0.0.1:%d" % self.srv.port
        self.assertEqual(u.check(), "error")

    def test_untrusted_tls_rejected(self):
        src = U.Source("Owner/CC-Remote-Chat", KEYS["a"]["sha1"], KEYS["a"]["sha256"], base=self.srv.base,
                       allowed_hosts={"127.0.0.1"}, cafile=None)   # 系统 CA 不认测试证书
        u = U.Updater(self.tmp / "d2", self.tmp / "app", src, "2.12", "2.12", first_delay=3600)
        self.assertEqual(u.check(), "error")
        self.assertIn("网络错误", u.status()["last_check"]["error"])

    def test_auto_check_loop_and_switch(self):
        self.srv.routes[self.mpath] = (200, {}, json.dumps(manifest("2.13", "b", b"x")).encode())
        u = self.upd(first_delay=0.05, interval=3600)
        u.set_auto(False)
        u.start()
        time.sleep(0.6)
        self.assertEqual(self.srv.hits, [], "关闭自动检查后不应发出请求")
        u.set_auto(True)   # 打开开关会叫醒循环
        self.assertTrue(wait_until(lambda: len(self.srv.hits) == 1, 5))
        self.assertTrue(wait_until(lambda: u.status()["available"] is not None, 5))
        changes = []
        u.on_change = lambda: changes.append(1)
        u.check_now()   # 「立即检查」不看开关
        self.assertTrue(wait_until(lambda: len(self.srv.hits) == 2 and not u.status()["checking"], 5))
        self.assertTrue(changes)
        state = json.loads((self.tmp / "data" / "updates" / "state.json").read_text())
        self.assertIs(state["auto_check"], True)
        self.assertEqual(os.stat(self.tmp / "data" / "updates").st_mode & 0o777, 0o700)


# ============================================================================ 安装
@unittest.skipUnless(HAVE_TOOLS, "需要 openssl / codesign / hdiutil / security")
class TestInstall(unittest.TestCase):
    """当前 = 「Apps With Space/CC Remote Chat.app」2.12-test（测试证书 A 签名）；新版 dmg 由本机 https 提供。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="ccr-updi.", dir=str(TMP)))
        self.shell = None
        self.addCleanup(self._cleanup)
        self.apps = self.tmp / "Apps With Space"
        self.apps.mkdir()
        self.cur = make_bundle(self.apps, "2.12-test", "2.12-test t1 aaaa", sign="a")
        self.srv = Https({})
        self.addCleanup(self.srv.close)
        self.launched = []
        self.shell = None

    def _cleanup(self):
        if self.shell is not None and self.shell.poll() is None:
            self.shell.kill()
            self.shell.wait(5)
        for p in self.tmp.rglob("*"):   # 解除 chmod 0555，便于删除
            try:
                if p.is_dir() and not p.is_symlink():
                    os.chmod(p, 0o755)
            except OSError:
                pass
        os.chmod(self.tmp, 0o755)
        shutil.rmtree(self.tmp, True)

    def publish(self, sign="a", version="2.12.1-test", build="2.12.1-test t2 bbbb", tamper=None, plist_version=None):
        bdir = self.tmp / ("build-" + sign + "-" + version)
        bdir.mkdir()
        app = make_bundle(bdir, plist_version or version, build, sign=sign)
        data = make_dmg(app, self.tmp / ("%s-%s.dmg" % (sign, version)))
        m = manifest(version, build, data)
        if tamper:
            tamper(m)
        self.srv.routes["/Owner/CC-Remote-Chat/releases/latest/download/update.json"] = (200, {}, json.dumps(m).encode())
        self.srv.routes["/Owner/CC-Remote-Chat/releases/download/v%s/%s" % (version, m["dmg"]["name"])] = (200, {}, data)
        return m

    def upd(self, current="2.12-test", **kw):
        u = U.Updater(self.tmp / "data", self.cur / "Contents" / "Resources" / "app", source(base=self.srv.base),
                      current, current + " t1 aaaa", launcher=self.launched.append, first_delay=3600,
                      shell_exit_timeout=5, busy_poll=0.05, **kw)
        self.addCleanup(u.stop)
        return u

    def run_install(self, u, final=("relaunching", "failed", "manual", "idle"), timeout=120):
        self.assertEqual(u.check(), "available")
        u.install()
        self.assertTrue(wait_until(lambda: u.status()["phase"] in final, timeout), u.status())
        return u.status()

    def cur_version(self):
        return plistlib.loads((self.cur / "Contents" / "Info.plist").read_bytes()).get("CCRVersion")

    def start_shell(self):
        exe = self.cur / "Contents" / "MacOS" / "CC Remote Chat"
        self.shell = subprocess.Popen([str(exe), "600"])
        self.assertTrue(wait_until(lambda: U.find_processes(str(exe)) == [self.shell.pid], 5))

    def test_happy_path_swap_relaunch_and_cleanup(self):
        self.publish()
        self.start_shell()
        u = self.upd()
        u.note_shell_pid(self.shell.pid)
        st = self.run_install(u)
        self.assertEqual(st["phase"], "relaunching", st)
        # 原生壳被 SIGTERM、启动器收到 bundle 路径；bundle 已是新版本，旧版本在隐藏名下
        self.assertEqual(self.shell.wait(5), -signal.SIGTERM)
        self.assertTrue(wait_until(lambda: self.launched, 5))
        self.assertEqual(self.launched, [["/usr/bin/open", str(self.cur)]])
        self.assertEqual(self.cur_version(), "2.12.1-test")
        old = self.apps / ".CC Remote Chat.app.old-2.12-test"
        self.assertTrue(old.is_dir())
        self.assertEqual(sorted(p.name for p in self.apps.iterdir()), [".CC Remote Chat.app.old-2.12-test", "CC Remote Chat.app"])
        r = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", "=" + source(base="https://x").requirement(),
                            str(self.cur)], capture_output=True)
        self.assertEqual(r.returncode, 0)
        dl = self.tmp / "data" / "updates" / "2.12.1-test"
        self.assertEqual(os.stat(dl).st_mode & 0o777, 0o700)
        self.assertTrue((dl / "CC-Remote-Chat-2.12.1-test.dmg").is_file())
        self.assertEqual([p for p in pathlib.Path("/tmp").glob("ccr-upd-mnt.*") if os.path.ismount(p)], [])   # 已 detach
        u.stop()
        # 新版本启动：「已更新到 X」一次，删掉旧 .app 与下载件
        u2 = self.upd(current="2.12.1-test")
        st2 = u2.status()
        self.assertEqual(st2["just_updated"], {"version": "2.12.1-test", "from": "2.12-test"})
        self.assertFalse(old.exists())
        self.assertFalse(dl.exists())
        u2.ack()
        self.assertIsNone(self.upd(current="2.12.1-test").status()["just_updated"])

    def test_sha256_mismatch(self):
        def bad(m):
            m["dmg"]["sha256"] = "0" * 64
        self.publish(tamper=bad)
        st = self.run_install(self.upd())
        self.assertEqual(st["phase"], "failed")
        self.assertIn("sha256", st["error"])
        self.assertEqual(self.cur_version(), "2.12-test")
        self.assertEqual(self.launched, [])

    def test_wrong_signature_rejected(self):
        for sign in ("adhoc", "b"):
            with self.subTest(sign=sign):
                self.publish(sign=sign, version="2.12.%d-test" % (2 if sign == "adhoc" else 3))
                u = self.upd()
                st = self.run_install(u)
                self.assertEqual(st["phase"], "failed", st)
                self.assertIn("签名", st["error"])
                self.assertEqual(self.cur_version(), "2.12-test")
                self.assertEqual([p.name for p in self.apps.iterdir()], ["CC Remote Chat.app"])   # 没有留下临时副本
                self.assertEqual(self.launched, [])
                u.stop()

    def test_plist_version_mismatch_rejected(self):
        self.publish(plist_version="2.12.9-test")
        st = self.run_install(self.upd())
        self.assertEqual(st["phase"], "failed")
        self.assertIn("版本", st["error"])

    def test_permission_denied_falls_back_to_manual(self):
        self.publish()
        os.chmod(self.apps, 0o555)   # 「应用程序」不可写（非管理员）
        u = self.upd()
        st = self.run_install(u)
        self.assertEqual(st["phase"], "manual", st)
        self.assertIn("App 管理", st["error"])
        self.assertTrue(st["manual"])
        self.assertEqual(self.cur_version(), "2.12-test")
        u.open_dmg()   # 用户点「打开已下载的安装包」：只交给注入的启动器
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0][0], "/usr/bin/open")
        self.assertTrue(self.launched[0][1].endswith("/updates/2.12.1-test/CC-Remote-Chat-2.12.1-test.dmg"))
        os.chmod(self.apps, 0o755)

    def test_swap_eperm_rolls_back(self):
        """macOS「App 管理」拦截时 rename 报 EPERM：当前 .app 原样保留、临时副本删除、退回手动方案。"""
        self.publish()
        real = U.swap_bundles

        def deny(cur, new, old):
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(U, "swap_bundles", deny):
            st = self.run_install(self.upd())
        self.assertEqual(st["phase"], "manual", st)
        self.assertEqual(self.cur_version(), "2.12-test")
        self.assertEqual([p.name for p in self.apps.iterdir()], ["CC Remote Chat.app"])
        del real

    def test_swap_rollback_on_second_rename_failure(self):
        cur = self.cur
        old = self.apps / ".old"
        with self.assertRaises(OSError):
            U.swap_bundles(cur, self.apps / "does not exist.app", old)
        self.assertTrue(cur.is_dir())
        self.assertFalse(old.exists())
        self.assertEqual(self.cur_version(), "2.12-test")

    def test_waits_for_busy_sessions_and_cancel(self):
        self.publish()
        busy = [["会话 A"]]
        u = self.upd(busy_fn=lambda: busy[0])
        self.assertEqual(u.check(), "available")
        u.install()
        self.assertTrue(wait_until(lambda: u.status()["phase"] == "waiting" and u.status()["busy"] == ["会话 A"], 5))
        time.sleep(0.3)
        self.assertEqual(self.srv.hits[-1][0].endswith("update.json"), True, "等待期间不应开始下载")
        u.cancel()
        self.assertTrue(wait_until(lambda: u.status()["phase"] == "idle", 5))
        self.assertEqual(self.cur_version(), "2.12-test")
        # 再点一次：会话空闲后自动开始
        u.install()
        self.assertTrue(wait_until(lambda: u.status()["phase"] == "waiting", 5))
        busy[0] = []
        self.assertTrue(wait_until(lambda: u.status()["phase"] in ("relaunching", "failed", "manual"), 120), u.status())
        self.assertEqual(u.status()["phase"], "relaunching", u.status())
        self.assertEqual(self.cur_version(), "2.12.1-test")

    def test_shell_must_be_this_bundle(self):
        """只 SIGTERM 可执行路径正好是本 bundle 的壳：window_request 里的 pid 是别的进程时不发信号。"""
        self.publish()
        other = subprocess.Popen(["/bin/sleep", "600"])
        self.addCleanup(lambda: (other.kill(), other.wait(5)))
        u = self.upd()
        u.note_shell_pid(other.pid)
        st = self.run_install(u)
        self.assertEqual(st["phase"], "relaunching", st)
        self.assertTrue(wait_until(lambda: self.launched, 5))
        self.assertIsNone(other.poll())
        self.assertEqual(self.launched, [["/usr/bin/open", str(self.cur)]])


# ============================================================================ 服务器
class TestServerRoutes(unittest.TestCase):
    def test_no_updater_means_no_api(self):
        from tests.test_server import Base

        class T(Base):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        try:
            st, js, _, _ = t.req("GET", "/api/bootstrap")
            self.assertEqual(st, 200)
            self.assertNotIn("update", js)
            for m, p in (("GET", "/api/update"), ("POST", "/api/update/install"), ("POST", "/api/update/check")):
                self.assertEqual(t.req(m, p, {} if m == "POST" else None)[0], 404, p)
        finally:
            t.doCleanups()

    def test_with_updater(self):
        from tests.test_server import Base

        class Stub:
            def __init__(self):
                self.calls = []

            def status(self):
                return {"enabled": True, "phase": "idle"}

            def __getattr__(self, name):
                if name in ("check_now", "set_auto", "snooze", "install", "cancel", "dismiss", "ack", "open_dmg"):
                    return lambda *a: (self.calls.append((name, a)), {"enabled": True, "phase": name})[1]
                raise AttributeError(name)

        class T(Base):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        stub = Stub()
        t.srv.updater = stub
        try:
            self.assertEqual(t.req("GET", "/api/bootstrap")[1]["update"], {"enabled": True, "phase": "idle"})
            self.assertEqual(t.req("GET", "/api/update")[1]["phase"], "idle")
            self.assertEqual(t.req("POST", "/api/update/install", {})[1]["phase"], "install")
            self.assertEqual(t.req("POST", "/api/update/settings", {"auto_check": False})[1]["phase"], "set_auto")
            self.assertEqual(t.req("POST", "/api/update/settings", {"auto_check": "no"})[0], 400)
            self.assertEqual(t.req("POST", "/api/update/snooze", {"version": "2.13"})[1]["phase"], "snooze")
            self.assertEqual(t.req("POST", "/api/update/nope", {})[0], 404)
            self.assertEqual(t.req("GET", "/api/update", auth=False)[0], 401)
            self.assertEqual([c[0] for c in stub.calls], ["install", "set_auto", "snooze"])
        finally:
            t.doCleanups()


# ============================================================================ 端到端（真实 app.py、--bundled --fake、全临时目录）
def _api(base, method, path, tok=None, body=None):
    import urllib.request
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"X-CCR-Token": tok} if tok else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


@unittest.skipUnless(HAVE_TOOLS and shutil.which("tmux"), "需要 openssl / codesign / hdiutil / security / tmux")
class TestEndToEnd(unittest.TestCase):
    """A（2.12-test）装在「Apps With Space/CC Remote Chat.app」并起后台、建 fake 会话；本机 https 提供 B（2.12.1-test）；
    API 触发检查 → 下载 → 校验 → 换装；Contents/MacOS/ 上的假壳被 SIGTERM，注入的启动器收到 bundle 路径；
    再按原生壳的方式运行新 app.py → 接管 → 会话还在、能收发、tmux 里的 fake claude 没被杀。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="ccr-upde.", dir=str(TMP)))
        self.data = self.tmp / "data"
        self.data.mkdir(mode=0o700)
        (self.data / "logs").mkdir(mode=0o700)
        (self.tmp / "home").mkdir()
        (self.tmp / "tmux").mkdir()
        self.procs = []
        self.shell = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.shell is not None and self.shell.poll() is None:
            self.shell.kill()
            self.shell.wait(5)
        for app_py in [self.tmp / "Apps With Space" / "CC Remote Chat.app" / "Contents/Resources/app/app.py"]:
            if app_py.exists():
                subprocess.run([sys.executable, "-I", "-B", str(app_py), "--data-dir", str(self.data), "--stop"],
                               env=self.env(), capture_output=True, timeout=30)
        for p in self.procs:
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
            if p.stdout:
                p.stdout.close()
        tmux = shutil.which("tmux")
        for sock in self.tmp.glob("**/tmux-*/*"):   # 只用 -S 明确的临时套接字
            if sock.is_socket():
                subprocess.run([tmux, "-S", str(sock), "kill-server"], capture_output=True, timeout=10)
        shutil.rmtree(self.tmp, True)

    def env(self):
        return {"HOME": str(self.tmp / "home"), "USER": os.environ.get("USER", "u"), "LANG": "zh_CN.UTF-8",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + os.path.dirname(shutil.which("tmux")),
                "TMUX_TMPDIR": str(self.tmp / "tmux")}

    def make_real_bundle(self, parent, version, build):
        """与 make_dmg.sh 同样布局的 .app：app 代码 + BUILD + UPDATE.json + bin/tmux，测试证书 A 签名；
        Python 用当前解释器（bundle 里不放 python/）。Contents/MacOS/ 是假壳（重新签名的 sleep）。"""
        app = parent / "CC Remote Chat.app"
        res = app / "Contents" / "Resources"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (res / "app").mkdir(parents=True)
        (res / "bin").mkdir()
        shutil.copyfile("/bin/sleep", app / "Contents" / "MacOS" / "CC Remote Chat")
        shutil.copyfile(shutil.which("tmux"), res / "bin" / "tmux")
        for x in (app / "Contents" / "MacOS" / "CC Remote Chat", res / "bin" / "tmux"):
            os.chmod(x, 0o755)
        shutil.copyfile(ROOT / "app.py", res / "app" / "app.py")
        ign = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        shutil.copytree(ROOT / "ccr", res / "app" / "ccr", ignore=ign)
        shutil.copytree(ROOT / "static", res / "app" / "static", ignore=shutil.ignore_patterns("__pycache__", "dev"))
        shutil.copytree(ROOT / "assets", res / "app" / "assets", ignore=ign)
        (res / "app" / "BUILD").write_text(build + "\n")
        (res / "app" / "UPDATE.json").write_text(json.dumps(
            {"repo": "Owner/CC-Remote-Chat", "cert_sha1": KEYS["a"]["sha1"], "cert_sha256": KEYS["a"]["sha256"]}))
        with open(app / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({"CFBundleIdentifier": U.BUNDLE_ID, "CFBundleExecutable": "CC Remote Chat",
                           "CFBundleName": "CC Remote Chat", "CFBundlePackageType": "APPL", "CCRBundled": True,
                           "CCRVersion": version, "CCRDataDir": "~/.cc-remote-chat-app"}, f)
        k = KEYS["a"]
        kc = str(k["dir"] / "release.keychain-db")
        subprocess.run(["/usr/bin/security", "unlock-keychain", "-p", (k["dir"] / "keychain-password").read_text().strip(), kc],
                       check=True, capture_output=True)
        for t in (app / "Contents" / "MacOS" / "CC Remote Chat", res / "bin" / "tmux", app):
            subprocess.run(["/usr/bin/codesign", "-f", "-s", k["sha1"], "--keychain", kc, "--timestamp=none", str(t)],
                           check=True, capture_output=True, timeout=120)
        subprocess.run(["/usr/bin/security", "lock-keychain", kc], capture_output=True)
        return app

    def app_argv(self, app):
        return [sys.executable, "-I", "-B", str(app / "Contents/Resources/app/app.py"), "--bundled", "--fake",
                "--no-browser", "--no-sound", "--port", "0", "--data-dir", str(self.data), "--update-test", str(self.cfg)]

    def spawn(self, app, request_pid):
        (self.data / "window_request").write_text("%d\n" % request_pid)
        log = open(self.data / "logs" / "launcher.log", "a")
        p = subprocess.Popen(self.app_argv(app), env=self.env(), cwd=str(self.tmp / "home"), stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.procs.append(p)
        return p

    def take_url(self):
        f = self.data / "window_login_url"
        self.assertTrue(wait_until(f.exists, 40), (self.data / "logs" / "launcher.log").read_text()[-3000:])
        u = f.read_text().strip()
        f.unlink()
        return u

    def login(self, url):
        base, code = url.split("/#code=")
        return base, _api(base, "POST", "/api/login", body={"code": code})["token"]

    def send(self, base, tok, sid, text):
        import urllib.error
        end = time.time() + 40
        while True:
            try:
                return _api(base, "POST", "/api/sessions/%s/message" % sid, tok, {"text": text})
            except urllib.error.HTTPError:
                if time.time() > end:
                    raise
                time.sleep(0.4)

    def screen_has(self, base, tok, sid, text):
        return wait_until(lambda: text in json.dumps(_api(base, "GET", "/api/sessions/%s/screen" % sid, tok),
                                                     ensure_ascii=False), 40, 0.3)

    def panes(self):
        out = []
        for sock in sorted(self.tmp.glob("data/**/tmux-*/*")):
            if sock.is_socket():
                r = subprocess.run([shutil.which("tmux"), "-S", str(sock), "list-panes", "-a", "-F",
                                    "#{session_name} #{pane_pid}"], capture_output=True, text=True)
                out += [l for l in r.stdout.split("\n") if l]
        return sorted(out)

    def test_update_end_to_end(self):
        apps = self.tmp / "Apps With Space"
        apps.mkdir()
        cur = self.make_real_bundle(apps, "2.12-test", "2.12-test 20261124T000000Z aaaa")
        bdir = self.tmp / "build-b"
        bdir.mkdir()
        newer = self.make_real_bundle(bdir, "2.12.1-test", "2.12.1-test 20261124T000001Z bbbb")
        dmg = make_dmg(newer, self.tmp / "b.dmg")
        m = manifest("2.12.1-test", "2.12.1-test 20261124T000001Z bbbb", dmg)
        srv = Https({"/Owner/CC-Remote-Chat/releases/latest/download/update.json": (200, {}, json.dumps(m).encode()),
                     "/Owner/CC-Remote-Chat/releases/download/v2.12.1-test/" + m["dmg"]["name"]: (200, {}, dmg)})
        self.addCleanup(srv.close)
        self.launch_log = self.tmp / "launch.log"
        self.cfg = self.tmp / "update-test.json"
        self.cfg.write_text(json.dumps({"base": srv.base, "cafile": SERVER_CERT[0], "allowed_hosts": ["127.0.0.1"],
                                        "launch_log": str(self.launch_log), "first_delay": 3600}))
        # 1) A 起后台（像原生壳一样先写 window_request），建 fake 会话并收发
        shell_exe = cur / "Contents" / "MacOS" / "CC Remote Chat"
        self.shell = subprocess.Popen([str(shell_exe), "600"])   # 假的原生壳
        old = self.spawn(cur, self.shell.pid)
        base, tok = self.login(self.take_url())
        sid = _api(base, "POST", "/api/sessions", tok, {"jobid": "local", "cwd": "~"})["sid"]
        self.assertTrue(wait_until(lambda: _api(base, "GET", "/api/sessions/" + sid, tok).get("dialog_kind") == "trust", 30))
        _api(base, "POST", "/api/sessions/%s/choose" % sid, tok, {"index": 1})   # fake claude 的信任目录：测试里由"用户"选
        self.send(base, tok, sid, "更新前的消息")
        self.assertTrue(self.screen_has(base, tok, sid, "收到：更新前的消息"))
        panes1 = self.panes()
        self.assertTrue(any(l.startswith("ccr-" + sid + " ") for l in panes1), panes1)
        boot = _api(base, "GET", "/api/bootstrap", tok)
        self.assertEqual((boot["update"]["enabled"], boot["update"]["current"]), (True, "2.12-test"))
        # 2) 检查 → 有新版本
        _api(base, "POST", "/api/update/check", tok, {})
        self.assertTrue(wait_until(lambda: (_api(base, "GET", "/api/update", tok).get("available") or {}).get("version")
                                   == "2.12.1-test", 20))
        # 3) 一键更新：下载 → 校验 → 换装 → SIGTERM 壳 → 启动器收到 bundle 路径
        _api(base, "POST", "/api/update/install", tok, {})
        self.assertTrue(wait_until(lambda: self.launch_log.exists(), 120), _api(base, "GET", "/api/update", tok))
        self.assertEqual(self.shell.wait(5), -signal.SIGTERM)
        self.assertEqual([json.loads(l) for l in self.launch_log.read_text().splitlines()], [["/usr/bin/open", str(cur)]])
        self.assertEqual(_api(base, "GET", "/api/update", tok)["phase"], "relaunching")
        self.assertEqual(plistlib.loads((cur / "Contents/Info.plist").read_bytes())["CCRVersion"], "2.12.1-test")
        self.assertTrue((apps / ".CC Remote Chat.app.old-2.12-test").is_dir())
        # 4) 按原生壳的方式运行新 app.py → 接管旧后台
        self.shell = subprocess.Popen([str(shell_exe), "600"])   # 新的壳（open 启动的那个）
        new = self.spawn(cur, self.shell.pid)
        base2, tok2 = self.login(self.take_url())
        self.assertEqual(old.wait(20), 0)
        self.assertIsNone(new.poll())
        self.assertEqual(json.loads((self.data / "app.pid").read_text()),
                         {"pid": new.pid, "build": "2.12.1-test 20261124T000001Z bbbb"})
        sessions = _api(base2, "GET", "/api/sessions", tok2)["sessions"]
        self.assertEqual([s_["sid"] for s_ in sessions], [sid])
        self.assertEqual(self.panes(), panes1)   # tmux 里的 fake claude 没被杀
        self.send(base2, tok2, sid, "更新后的消息")
        self.assertTrue(self.screen_has(base2, tok2, sid, "收到：更新后的消息"))
        st = _api(base2, "GET", "/api/update", tok2)
        self.assertEqual((st["current"], st["just_updated"]), ("2.12.1-test", {"version": "2.12.1-test", "from": "2.12-test"}))
        self.assertFalse((apps / ".CC Remote Chat.app.old-2.12-test").exists())   # 新版本启动后清理旧 .app
        self.assertEqual(sorted(p.name for p in apps.iterdir()), ["CC Remote Chat.app"])
        _api(base2, "POST", "/api/update/ack", tok2, {})
        self.assertIsNone(_api(base2, "GET", "/api/update", tok2)["just_updated"])
        log = (self.data / "logs" / "app.log").read_text(encoding="utf-8")
        self.assertIn("已停止旧版本的后台", log)
        self.assertIn("已安装 2.12.1-test", log)


if __name__ == "__main__":
    unittest.main()
