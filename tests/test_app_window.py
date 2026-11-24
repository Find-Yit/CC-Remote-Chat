"""原生 Mac 应用取登录链接（SPEC §14）：app.py 的 window_request / window_login_url 交接。

- 单测：take_window_request（存在/不存在/过期/软链接/目录）、write_window_login_url（0600、原子、不跟随软链接）、
  open_window 的三种去向（原生窗口 / --no-browser 打印 / 浏览器，浏览器用 mock，绝不真的打开 Chrome）。
- 端到端：临时数据目录里以 fake 模式启动 app.py（--no-browser --no-sound --port 0），验证"新启动"与
  "已有实例 → SIGUSR1 → reopen"两条路径都把链接写进 window_login_url；没有请求时仍按原行为打印。
  全程只用临时目录，不碰 ~/.cc-remote-chat、不碰 8765 端口上的实例。
"""
import io
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import app as ccr_app

ROOT = pathlib.Path(__file__).resolve().parent.parent


class StubServer:
    def __init__(self):
        self.n = 0

    def new_login_url(self):
        self.n += 1
        return "http://127.0.0.1:18999/#code=CODE%d" % self.n


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ccr-appwin-")).resolve()
        os.chmod(self.dir, 0o700)
        self.req = self.dir / ccr_app.WINDOW_REQUEST
        self.out = self.dir / ccr_app.WINDOW_LOGIN_URL

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_request(self, age=0.0):
        fd = os.open(self.req, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        if age:
            t = time.time() - age
            os.utime(self.req, (t, t))


class TestTakeWindowRequest(TempDir):
    def test_absent(self):
        self.assertFalse(ccr_app.take_window_request(self.dir))

    def test_fresh_request_consumed(self):
        self.make_request()
        self.assertTrue(ccr_app.take_window_request(self.dir))
        self.assertFalse(self.req.exists())
        self.assertFalse(ccr_app.take_window_request(self.dir))   # 只生效一次

    def test_stale_request_removed_but_ignored(self):
        self.make_request(age=ccr_app.WINDOW_REQUEST_MAX_AGE + 30)
        self.assertFalse(ccr_app.take_window_request(self.dir))
        self.assertFalse(self.req.exists())

    def test_future_mtime_beyond_skew_ignored(self):
        self.make_request(age=-60)
        self.assertFalse(ccr_app.take_window_request(self.dir))
        self.assertFalse(self.req.exists())

    def test_symlink_removed_not_followed(self):
        target = self.dir / "target"
        target.write_text("keep")
        os.symlink(target, self.req)
        self.assertFalse(ccr_app.take_window_request(self.dir))
        self.assertFalse(os.path.lexists(self.req))
        self.assertEqual(target.read_text(), "keep")

    def test_directory_ignored(self):
        os.mkdir(self.req)
        self.assertFalse(ccr_app.take_window_request(self.dir))
        self.assertTrue(self.req.is_dir())   # 删不掉目录：保持原样，按原行为开窗


class TestWriteLoginUrl(TempDir):
    def test_atomic_0600(self):
        ccr_app.write_window_login_url(self.dir, "http://127.0.0.1:1/#code=abc")
        self.assertEqual(self.out.read_text(), "http://127.0.0.1:1/#code=abc\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
        # 不留临时文件
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [ccr_app.WINDOW_LOGIN_URL])

    def test_overwrite(self):
        ccr_app.write_window_login_url(self.dir, "http://127.0.0.1:1/#code=a")
        ccr_app.write_window_login_url(self.dir, "http://127.0.0.1:1/#code=b")
        self.assertEqual(self.out.read_text(), "http://127.0.0.1:1/#code=b\n")

    def test_symlink_replaced_not_followed(self):
        target = self.dir / "target"
        target.write_text("keep")
        os.symlink(target, self.out)
        ccr_app.write_window_login_url(self.dir, "http://127.0.0.1:1/#code=c")
        self.assertFalse(os.path.islink(self.out))
        self.assertEqual(self.out.read_text(), "http://127.0.0.1:1/#code=c\n")
        self.assertEqual(target.read_text(), "keep")

    def test_failure_cleans_tmp(self):
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                ccr_app.write_window_login_url(self.dir, "http://127.0.0.1:1/#code=d")
        self.assertEqual(list(self.dir.iterdir()), [])


class TestOpenWindow(TempDir):
    def test_native_when_requested(self):
        """有请求：写文件、不开浏览器、不打印（--no-browser 也一样：请求优先）。"""
        for no_browser in (False, True):
            self.make_request()
            opener = mock.Mock()
            buf = io.StringIO()
            with redirect_stdout(buf):
                r = ccr_app.open_window(StubServer(), self.dir, no_browser, opener=opener)
            self.assertEqual(r, "native")
            opener.assert_not_called()
            self.assertEqual(buf.getvalue(), "")
            self.assertTrue(self.out.read_text().startswith("http://127.0.0.1:18999/#code=CODE"))
            self.assertFalse(self.req.exists())
            self.out.unlink()

    def test_each_call_new_code(self):
        srv = StubServer()
        self.make_request()
        ccr_app.open_window(srv, self.dir, True)
        a = self.out.read_text()
        self.make_request()
        ccr_app.open_window(srv, self.dir, True)
        self.assertNotEqual(a, self.out.read_text())

    def test_no_request_no_browser_prints(self):
        opener = mock.Mock()
        buf = io.StringIO()
        with redirect_stdout(buf):
            r = ccr_app.open_window(StubServer(), self.dir, True, opener=opener)
        self.assertEqual(r, "printed")
        self.assertIn("登录链接（120 秒内有效", buf.getvalue())
        opener.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_no_request_opens_browser(self):
        called = threading.Event()
        got = []
        opener = lambda url: (got.append(url), called.set())   # noqa: E731
        r = ccr_app.open_window(StubServer(), self.dir, False, opener=opener)
        self.assertEqual(r, "browser")
        self.assertTrue(called.wait(5))
        self.assertEqual(got, ["http://127.0.0.1:18999/#code=CODE1"])
        self.assertFalse(self.out.exists())

    def test_stale_request_falls_back(self):
        self.make_request(age=ccr_app.WINDOW_REQUEST_MAX_AGE + 30)
        buf = io.StringIO()
        with redirect_stdout(buf):
            r = ccr_app.open_window(StubServer(), self.dir, True)
        self.assertEqual(r, "printed")
        self.assertFalse(self.out.exists())
        self.assertFalse(self.req.exists())

    def test_write_failure_reports_error(self):
        self.make_request()
        opener = mock.Mock()
        with mock.patch.object(ccr_app, "write_window_login_url", side_effect=OSError("disk")):
            r = ccr_app.open_window(StubServer(), self.dir, False, opener=opener)
        self.assertEqual(r, "error")
        opener.assert_not_called()   # 原生应用在等：不退回去开 Chrome


class TestEndToEnd(TempDir):
    """真实运行 app.py（fake 模式、临时数据目录、系统分配端口）。"""

    def setUp(self):
        super().setUp()
        self.proc = None
        self.lines = []
        self.lines_lock = threading.Lock()

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.run_app("--stop")
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        super().tearDown()

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "ANTHROPIC_", "CCR_", "TMUX"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def run_app(self, *extra):
        return subprocess.run([sys.executable, str(ROOT / "app.py"), "--data-dir", str(self.dir), "--no-browser",
                               "--no-sound"] + list(extra), cwd=str(ROOT), env=self.env(), capture_output=True,
                              text=True, timeout=30)

    def start_instance(self):
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--fake", "--no-browser", "--no-sound",
                                      "--port", "0", "--data-dir", str(self.dir)],
                                     cwd=str(ROOT), env=self.env(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def reader():
            for line in self.proc.stdout:
                with self.lines_lock:
                    self.lines.append(line)
        threading.Thread(target=reader, daemon=True).start()

    def wait_for(self, pred, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                return pred()
            time.sleep(0.05)
        return pred()

    def output(self):
        with self.lines_lock:
            return "".join(self.lines)

    def take_url(self):
        self.assertTrue(self.wait_for(self.out.exists), "没有写出 window_login_url；输出：\n" + self.output())
        url = self.out.read_text()
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
        self.out.unlink()
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/#code=[A-Za-z0-9_-]{40,}\n\Z")
        self.assertFalse(self.req.exists())
        return url

    def test_startup_and_reopen_paths(self):
        # 1) 新启动：先有请求再启动 → 写文件，不打印登录链接
        self.make_request()
        self.start_instance()
        url1 = self.take_url()
        self.assertTrue(self.wait_for(lambda: "已启动" in self.output()))
        self.assertNotIn("登录链接（", self.output())
        # 2) 已有实例：再运行一次 app.py → SIGUSR1 → reopen → 同样写文件（新登录码）
        self.make_request()
        r = self.run_app()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已让它重新打开窗口", r.stdout)
        url2 = self.take_url()
        self.assertNotEqual(url1, url2)
        self.assertEqual(url1.split("#")[0], url2.split("#")[0])   # 同一个实例、同一个端口
        self.assertNotIn("登录链接（", self.output())
        # 3) 没有请求：保持原行为（实例以 --no-browser 运行 → 打印登录链接），不写文件
        r = self.run_app()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.wait_for(lambda: "登录链接（" in self.output()), self.output())
        self.assertFalse(self.out.exists())
        # 4) 停止
        r = self.run_app("--stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.proc.wait(15)


if __name__ == "__main__":
    unittest.main()
