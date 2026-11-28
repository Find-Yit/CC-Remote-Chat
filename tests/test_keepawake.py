"""防休眠（ccr/keepawake.py，SPEC §12.2）：「在干活」的判定、caffeinate 的启动 / 放开 / 不重复起、60 秒全空闲才放开、
关开关立即放开、后端退出时结束、意外退出后的重起间隔、找不到可执行文件、演示模式（--fake）不起进程、设置项校验、
GET /api/keepawake 与 POST /api/config 的挂钩。

**绝不**真的运行 caffeinate：Popen 一律是假的（FakePopen 记录 argv / kwargs），时钟可注入。"""
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from ccr import config as C, keepawake as K
from tests.test_server import Base as ServerBase

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeProc:
    _next = 4000

    def __init__(self, argv, **kw):
        FakeProc._next += 1
        self.pid = FakeProc._next
        self.argv = argv
        self.kw = kw
        self.code = None
        self.terminated = 0
        self.killed = 0
        self.hang = False   # 收到 SIGTERM 也不退出（测 SIGKILL 兜底）

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated += 1
        if not self.hang:
            self.code = -15

    def kill(self):
        self.killed += 1
        self.code = -9

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("caffeinate", timeout)
        return self.code


class FakePopen:
    def __init__(self):
        self.procs = []
        self.fail = None

    def __call__(self, argv, **kw):
        if self.fail is not None:
            raise self.fail
        p = FakeProc(argv, **kw)
        self.procs.append(p)
        return p

    def alive(self):
        return [p for p in self.procs if p.code is None]


def meta(state="busy", working=None, link="ready", **kw):
    m = {"sid": kw.pop("sid", "s1"), "state": state, "link": link,
         "working": (state == "busy") if working is None else working}
    m.update(kw)
    return m


class TestIsWorking(unittest.TestCase):
    def test_same_as_sidebar(self):
        self.assertTrue(K.is_working(meta("busy")))
        self.assertTrue(K.is_working(meta("idle", working=True)))          # 主会话回复完、子代理还在跑
        self.assertFalse(K.is_working(meta("idle")))
        self.assertFalse(K.is_working(meta("waiting", working=False)))     # 等审批 / 等回答
        self.assertFalse(K.is_working(meta("waiting", working=True)))      # 防御：后端不会这样给，但也不算
        self.assertFalse(K.is_working(meta("trust", working=False)))
        for st in ("starting", "exited", "error"):
            self.assertFalse(K.is_working(meta(st, working=True)), st)
        self.assertFalse(K.is_working(meta("busy", link="down")))          # 侧栏「连接断开」
        self.assertFalse(K.is_working(meta("busy", link="ended")))         # 「作业已结束」
        self.assertTrue(K.is_working(meta("busy", link="connecting")))
        self.assertTrue(K.is_working(meta("busy", jobid="local")))         # 本机会话
        self.assertTrue(K.is_working(meta("busy", jobid="123456")))        # 服务器会话
        self.assertFalse(K.is_working(meta("busy", working="yes")))
        self.assertFalse(K.is_working(None))
        self.assertFalse(K.is_working("busy"))
        self.assertEqual(K.working_count([meta("busy"), meta("idle"), meta("idle", working=True), None]), 2)
        self.assertEqual(K.working_count(None), 0)

    def test_matches_session_meta(self):
        """与 _Session.working / meta() 的真实输出一致（主会话 busy；idle + 子代理在跑；waiting 不算）。"""
        from ccr import session as S
        s = S._Session("s1", 10)
        s.link = "ready"
        for state, running, want in (("busy", 0, True), ("idle", 0, False), ("idle", 2, True),
                                     ("waiting", 0, False), ("waiting", 1, False), ("trust", 0, False)):
            s.state = state
            with mock.patch.object(s.subs, "counts", return_value={"running": running, "done": 0, "total": running}):
                self.assertEqual(K.is_working(s.meta()), want, (state, running))


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = {}
        self.metas = []
        self.clock = Clock()
        self.popen = FakePopen()
        self.exe = tempfile.NamedTemporaryFile(prefix="fake-caffeinate", dir="/tmp", delete=False)
        self.exe.close()
        pathlib.Path(self.exe.name).chmod(0o700)   # 假的可执行文件：只用来让 os.access 通过，永远不会被运行
        self.addCleanup(pathlib.Path(self.exe.name).unlink)
        self.ka = self.make()

    def make(self, **kw):
        args = dict(exe=self.exe.name, popen=self.popen, clock=self.clock, pid=4242)
        args.update(kw)
        return K.KeepAwake(lambda: dict(self.cfg), lambda: list(self.metas), **args)

    def at(self, dt):
        self.clock.t += dt
        self.ka.tick()


class TestKeepAwake(Base):
    def test_start_on_work_and_argv(self):
        self.ka.tick()
        self.assertEqual(self.popen.procs, [])                       # 没有会话在干活：不起
        self.metas = [meta("idle", sid="a"), meta("busy", sid="b")]
        self.ka.tick()
        self.assertEqual(len(self.popen.procs), 1)
        p = self.popen.procs[0]
        self.assertEqual(p.argv, [self.exe.name, "-i", "-w", "4242"])  # 只阻止空闲睡眠；后端退出它也退出
        self.assertIs(p.kw["shell"], False)
        self.assertIs(p.kw["start_new_session"], True)
        self.assertIs(p.kw["close_fds"], True)
        for k in ("stdin", "stdout", "stderr"):
            self.assertEqual(p.kw[k], subprocess.DEVNULL)
        self.assertNotIn("env", p.kw)                                  # 不改环境变量
        st = self.ka.status()
        self.assertEqual((st["active"], st["working"], st["enabled"], st["dry_run"], st["available"]),
                         (True, 1, True, False, True))
        self.assertIsNone(st["release_in"])

    def test_default_pid_is_backend(self):
        import os
        self.assertEqual(K.KeepAwake(dict, list).pid, os.getpid())
        self.assertEqual(K.KeepAwake(dict, list).exe, "/usr/bin/caffeinate")

    def test_release_after_60s_all_idle(self):
        self.metas = [meta("busy")]
        self.ka.tick()
        p = self.popen.procs[0]
        self.metas = [meta("idle")]
        self.at(1)                                   # 开始计时
        self.at(30)
        self.assertIsNone(p.poll())
        st = self.ka.status()
        self.assertTrue(st["active"])
        self.assertAlmostEqual(st["release_in"], 30.0, delta=0.2)
        self.at(29.9)
        self.assertIsNone(p.poll())                  # 还差 0.1 秒
        self.at(0.2)
        self.assertEqual(p.terminated, 1)            # 全部空闲满 60 秒：结束 caffeinate
        self.assertFalse(self.ka.status()["active"])
        self.at(100)
        self.assertEqual(len(self.popen.procs), 1)   # 空闲时不再起
        self.assertEqual([h["what"] for h in self.ka.history], ["start", "stop"])

    def test_waiting_does_not_count(self):
        self.metas = [meta("busy")]
        self.ka.tick()
        p = self.popen.procs[0]
        self.metas = [meta("waiting", working=False)]   # 等审批：不算在干活
        self.at(1)
        self.at(61)
        self.assertEqual(p.terminated, 1)
        self.metas = [meta("trust", working=False), meta("idle", sid="s2")]
        self.at(5)
        self.assertEqual(len(self.popen.procs), 1)

    def test_toggling_never_spawns_twice(self):
        self.metas = [meta("busy")]
        self.ka.tick()
        for _ in range(20):                          # 反复 busy / idle / 子代理 / 对话框，间隔都小于 60 秒
            self.metas = [meta("idle")]
            self.at(20)
            self.metas = [meta("waiting", working=False)]
            self.at(20)
            self.metas = [meta("idle", working=True)]
            self.at(15)
            self.metas = [meta("busy"), meta("busy", sid="s2")]
            self.at(10)
        self.assertEqual(len(self.popen.procs), 1)
        self.assertIsNone(self.popen.procs[0].poll())
        self.assertEqual(self.ka.spawns, 1)
        # 空闲计时在重新干活时清零：之后要再空闲满 60 秒
        self.metas = []
        self.at(1)
        self.at(59)
        self.assertIsNone(self.popen.procs[0].poll())
        self.at(1)
        self.assertEqual(self.popen.procs[0].terminated, 1)
        # 放开之后再干活 = 第二个进程
        self.metas = [meta("busy")]
        self.at(1)
        self.assertEqual(len(self.popen.procs), 2)

    def test_disable_releases_immediately(self):
        self.metas = [meta("busy")]
        self.ka.tick()
        p = self.popen.procs[0]
        self.cfg = {"keep_awake": False}
        self.ka.poke()                               # 没有后台线程时 poke = 立即 tick
        self.assertEqual(p.terminated, 1)
        st = self.ka.status()
        self.assertEqual((st["enabled"], st["active"]), (False, False))
        self.at(5)
        self.assertEqual(len(self.popen.procs), 1)   # 关着就不起
        self.cfg = {"keep_awake": True}
        self.at(1)
        self.assertEqual(len(self.popen.procs), 2)   # 重新打开、仍在干活 → 马上起

    def test_disabled_does_not_read_sessions(self):
        self.cfg = {"keep_awake": False}
        called = []
        ka = K.KeepAwake(lambda: dict(self.cfg), lambda: called.append(1) or [], exe=self.exe.name,
                         popen=self.popen, clock=self.clock)
        ka.tick()
        self.assertEqual(called, [])

    def test_stop_ends_process(self):
        self.metas = [meta("busy")]
        self.ka.tick()
        p = self.popen.procs[0]
        self.ka.stop()
        self.assertEqual(p.terminated, 1)
        self.assertFalse(self.ka.status()["active"])
        self.ka.tick()                               # 退出之后不再起
        self.assertEqual(len(self.popen.procs), 1)
        self.ka.stop()                               # 再次 stop 无事
        self.assertEqual(p.terminated, 1)

    def test_kill_if_term_ignored(self):
        self.metas = [meta("busy")]
        self.ka.tick()
        p = self.popen.procs[0]
        p.hang = True
        self.ka.stop()
        self.assertEqual((p.terminated, p.killed), (1, 1))

    def test_died_respawn_gap(self):
        self.metas = [meta("busy")]
        self.ka.tick()
        self.popen.procs[0].code = 1                 # 被别人结束了
        with self.assertLogs("ccr.keepawake", "WARNING"):
            self.at(5)
        self.assertEqual(len(self.popen.procs), 1)   # 30 秒内不重起
        self.assertFalse(self.ka.status()["active"])
        self.at(20)
        self.assertEqual(len(self.popen.procs), 1)
        self.at(11)
        self.assertEqual(len(self.popen.procs), 2)
        self.assertTrue(self.ka.status()["active"])

    def test_spawn_failure_and_missing_exe(self):
        self.popen.fail = OSError("boom")
        self.metas = [meta("busy")]
        with self.assertLogs("ccr.keepawake", "WARNING"):
            self.ka.tick()
        self.assertFalse(self.ka.status()["active"])
        self.popen.fail = None
        self.at(10)
        self.assertEqual(self.popen.procs, [])       # 失败后隔 30 秒再试
        self.at(25)
        self.assertEqual(len(self.popen.procs), 1)
        # 找不到可执行文件：不调用 Popen，只警告一次
        pop = FakePopen()
        ka = K.KeepAwake(lambda: {}, lambda: [meta("busy")], exe="/nonexistent/caffeinate", popen=pop, clock=self.clock)
        with self.assertLogs("ccr.keepawake", "WARNING") as cm:
            ka.tick()
            self.clock.t += 100
            ka.tick()
        self.assertEqual(pop.procs, [])
        self.assertEqual(len([r for r in cm.records if "找不到" in r.getMessage()]), 1)
        st = ka.status()
        self.assertEqual((st["available"], st["active"]), (False, False))

    def test_dry_run_never_spawns(self):
        pop = mock.Mock(side_effect=AssertionError("演示模式不能起进程"))
        ka = self.make(popen=pop, dry_run=True, exe="/nonexistent/caffeinate")
        self.metas = [meta("busy")]
        with mock.patch.object(K.subprocess, "Popen", side_effect=AssertionError("不能起真进程")):
            ka.tick()
            st = ka.status()
            self.assertEqual((st["active"], st["dry_run"], st["available"]), (True, True, True))
            self.metas = []
            self.clock.t += 1
            ka.tick()
            self.clock.t += 61
            ka.tick()
            self.assertFalse(ka.status()["active"])
            ka.stop()
        pop.assert_not_called()
        self.assertEqual([h["what"] for h in ka.history], ["start", "stop"])

    def test_list_sessions_error_is_idle(self):
        self.metas = [meta("busy")]
        self.ka.tick()

        def boom():
            raise RuntimeError("x")
        self.ka._list = boom
        with self.assertLogs("ccr.keepawake", "ERROR"):
            self.at(1)
        self.assertTrue(self.ka.status()["active"])   # 读失败当空闲：照样要满 60 秒才放开

    def test_background_thread_and_poke(self):
        ka = K.KeepAwake(lambda: dict(self.cfg), lambda: list(self.metas), exe=self.exe.name, popen=self.popen,
                         interval=30.0)
        ka.start()
        self.addCleanup(ka.stop)
        self.metas = [meta("busy")]
        ka.poke()                                     # 不用等 30 秒
        deadline = time.monotonic() + 3
        while not self.popen.procs and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.popen.procs), 1)
        self.cfg = {"keep_awake": False}
        ka.poke()
        deadline = time.monotonic() + 3
        while self.popen.procs[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.popen.procs[0].terminated, 1)
        ka.stop()
        self.assertFalse(ka._thread.is_alive())
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn("ccr-keepawake", names)

    def test_never_runs_real_caffeinate_in_tests(self):
        """防御：本文件所有 KeepAwake 都注入了假 Popen；模块默认用 subprocess.Popen（app.py 里才是真的）。"""
        src = (ROOT / "ccr" / "keepawake.py").read_text(encoding="utf-8")
        self.assertIn('CAFFEINATE = "/usr/bin/caffeinate"', src)
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)


class TestConfigAndWiring(unittest.TestCase):
    def test_config_default_and_validate(self):
        self.assertIs(C.DEFAULT_CONFIG["keep_awake"], True)
        self.assertEqual(C.validate_partial({"keep_awake": False}), {"keep_awake": False})
        for bad in ("false", 0, 1, None, []):
            with self.assertRaises(ValueError):
                C.validate_partial({"keep_awake": bad})
        self.assertIn("keep_awake", C.public_config(dict(C.DEFAULT_CONFIG)))
        with tempfile.TemporaryDirectory() as d:
            st = C.ConfigStore(pathlib.Path(d) / "config.json")
            self.assertIs(st.get()["keep_awake"], True)
            st.update({"keep_awake": False})
            self.assertIs(C.ConfigStore(pathlib.Path(d) / "config.json").get()["keep_awake"], False)
        self.assertTrue(K.enabled_in({}))
        self.assertTrue(K.enabled_in(None))
        self.assertFalse(K.enabled_in({"keep_awake": False}))

    def test_app_wiring(self):
        """app.py：fake 模式默认不起真进程（dry_run=args.fake）；启动后 start、退出时 stop；传给 server。"""
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("KeepAwake(store.get, manager.list_sessions, dry_run=args.fake)", src)
        self.assertIn("keepawake=keepawake", src)
        self.assertLess(src.index("keepawake.start()"), src.index("open_window(server, data_dir, args.no_browser)"))
        fin = src[src.index("    finally:\n        log.info(\"正在退出"):]
        self.assertIn("keepawake.stop()", fin)


class StubKA:
    def __init__(self):
        self.pokes = 0

    def status(self):
        return {"enabled": True, "active": True, "working": 2, "since": 3.0, "release_in": None, "grace": 60.0,
                "dry_run": False, "available": True}

    def poke(self):
        self.pokes += 1


class TestServerRoutes(ServerBase):
    def test_status_route(self):
        st, js, _, _ = self.req("GET", "/api/keepawake")
        self.assertEqual((st, js), (200, {"available": False, "active": False}))   # 没有传入 = 不可用
        self.srv.keepawake = StubKA()
        st, js, _, _ = self.req("GET", "/api/keepawake")
        self.assertEqual(st, 200)
        self.assertEqual((js["active"], js["working"]), (True, 2))
        self.assertEqual(self.req("GET", "/api/keepawake", auth=False)[0], 401)

    def test_config_pokes(self):
        ka = StubKA()
        self.srv.keepawake = ka
        st, js, _, _ = self.req("POST", "/api/config", {"keep_awake": False})
        self.assertEqual(st, 200)
        self.assertIs(js["keep_awake"], False)
        self.assertEqual(ka.pokes, 1)                 # 关掉 = 立即放开
        self.assertIs(self.store.get()["keep_awake"], False)
        self.req("POST", "/api/config", {"notify_on_done": False})
        self.assertEqual(ka.pokes, 1)                 # 别的设置不 poke
        self.assertErr(self.req("POST", "/api/config", {"keep_awake": "no"}), 400)
        self.assertEqual(ka.pokes, 1)
        st, js, _, _ = self.req("GET", "/api/config")
        self.assertIs(js["keep_awake"], False)


if __name__ == "__main__":
    unittest.main()
