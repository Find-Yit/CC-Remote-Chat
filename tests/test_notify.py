"""提示音（ccr/notify.py）：声音文件校验、afplay 调用（mock，不真的出声）、状态转移与去抖、本机 Stop hook
只读检查、设置校验（config）、server 接口、SessionManager 集成（StubLink + SimTUI）。"""
import json
import logging
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from ccr import config as C, notify as N, session as S
from tests.test_server import Base as ServerBase
from tests.test_session import Base as SessionBase, wait_until

logging.getLogger("ccr.notify").setLevel(logging.ERROR)


class FakeSched:
    """收集 (delay, fn)；run() 手动执行，确定性地测试"确认延迟"。"""

    def __init__(self):
        self.calls = []

    def __call__(self, delay, fn):
        self.calls.append((delay, fn))

    def run(self):
        calls, self.calls = self.calls, []
        for _d, fn in calls:
            fn()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def sound_dir():
    d = tempfile.mkdtemp(prefix="ccrsnd", dir="/tmp")
    for n in ("Blow", "Glass", "Ping"):
        pathlib.Path(d, n + ".aiff").write_bytes(b"FORM")
    pathlib.Path(d, "readme.txt").write_text("x")
    os.symlink(os.path.join(d, "Blow.aiff"), os.path.join(d, "Link.aiff"))
    os.mkdir(os.path.join(d, "Dir.aiff"))
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.sd = sound_dir()
        self.addCleanup(shutil.rmtree, self.sd, True)
        self.cfg = {}
        self.played = []
        self.sched = FakeSched()
        self.clock = Clock()
        self.local_hook = mock.Mock(return_value=False)
        self.n = N.Notifier(lambda: dict(self.cfg), play=self.played.append, clock=self.clock,
                            schedule=self.sched, local_hook=self.local_hook, sound_dir=self.sd)

    def obs(self, state, kind=None, sig=None, interrupted=False, sid="s1", jobid="123"):
        self.n.observe(sid, jobid, state, kind, sig, interrupted)

    def settle(self, dt=5.0):
        """过了确认延迟（且离开 reset 的安静期 / 全局间隔）后执行定时器。"""
        self.clock.t += dt
        self.sched.run()


# ============================================================================ 声音文件 / afplay
class TestSounds(Base):
    def test_list_and_validate(self):
        self.assertEqual(N.list_sounds(self.sd), ["Blow", "Glass", "Ping"])   # 不含软链接、目录、非 aiff
        self.assertEqual(N.validate_sound("Glass", self.sd), "Glass")
        for bad in ("Link", "Dir", "Nope", "../Blow", "Blow.aiff", "", None, 3, "a/b", "Blow\n", "-x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                N.validate_sound(bad, self.sd)
        self.assertEqual(N.sound_path("Ping", self.sd), os.path.join(self.sd, "Ping.aiff"))
        self.assertEqual(N.list_sounds("/definitely/not/here"), [])

    def test_play_file_argv(self):
        with mock.patch.object(N.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.assertTrue(N.play_file("/System/Library/Sounds/Blow.aiff"))
        args, kw = popen.call_args
        self.assertEqual(args[0], ["/usr/bin/afplay", "/System/Library/Sounds/Blow.aiff"])   # argv，不经 shell
        self.assertFalse(kw.get("shell"))
        self.assertTrue(kw.get("start_new_session"))
        for k in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw.get(k), N.subprocess.DEVNULL)
        with mock.patch.object(N.subprocess, "Popen", side_effect=OSError("no afplay")):
            self.assertFalse(N.play_file("/x.aiff"))

    @unittest.skipUnless(os.path.isdir("/System/Library/Sounds"), "需要 macOS 系统声音目录")
    def test_real_sound_dir_has_default(self):
        self.assertIn("Blow", N.list_sounds())
        N.validate_sound("Blow")


# ============================================================================ 状态转移 / 去抖
class TestTransitions(Base):
    def baseline(self, state="idle", **kw):
        self.n.reset(kw.get("sid", "s1"))
        self.obs(state, **kw)
        self.clock.t += 3   # 离开 reset 后的安静期

    def test_turn_done(self):
        self.baseline("idle")
        self.obs("busy")
        self.obs("idle")
        self.assertEqual(self.played, [])     # 还在确认延迟里
        self.settle()
        self.assertEqual(self.played, [os.path.join(self.sd, "Blow.aiff")])

    def test_first_sync_after_reset_silent(self):
        # 重连前屏幕是 busy，重连后首帧 idle：只是基线，不响
        self.n.reset("s1")
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.played, [])
        # 启动：starting → trust（信任框）也只是基线
        self.n.reset("s2")
        self.obs("starting", sid="s2")
        self.obs("trust", kind="trust", sig=("trust",), sid="s2")
        self.settle()
        self.assertEqual(self.played, [])

    def test_baseline_busy_then_idle_without_seen_busy(self):
        self.n.reset("s1")
        self.obs("busy")          # 基线（安静期内）
        self.clock.t += 3
        self.obs("idle")          # 基线之后没有真正看到 busy → 不响
        self.settle()
        self.assertEqual(self.played, [])

    def test_interrupt_suppressed(self):
        self.baseline("idle")
        self.obs("busy")
        self.n.suppress_done("s1")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.played, [])
        # 下一轮正常响
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(len(self.played), 1)

    def test_screen_interrupted_suppressed(self):
        self.baseline("idle")
        self.obs("busy")
        self.obs("idle", interrupted=True)
        self.settle()
        self.assertEqual(self.played, [])

    def test_suppress_after_pending_cancels(self):
        self.baseline("idle")
        self.obs("busy")
        self.obs("idle")
        self.n.suppress_done("s1")   # 确认之前用户点了中断/立即发送
        self.settle()
        self.assertEqual(self.played, [])

    def test_flicker_cancelled(self):
        self.baseline("idle")
        self.obs("busy")
        self.obs("idle")
        self.obs("busy")      # 确认延迟内又忙起来（屏幕抖动）
        self.settle()
        self.assertEqual(self.played, [])
        self.obs("idle")
        self.settle()
        self.assertEqual(len(self.played), 1)

    def test_attention(self):
        self.baseline("busy")
        sig = ("permission", "Bash command", "Do you want to proceed?", ("touch /tmp/x",), ("Yes", "No"))
        self.obs("waiting", "permission", sig)
        self.settle()
        self.assertEqual(len(self.played), 1)
        # 同一个对话框抖动（用户没操作）：不再响
        self.obs("busy")
        self.obs("waiting", "permission", sig)
        self.settle()
        self.assertEqual(len(self.played), 1)
        # 用户回答后同样的对话框再次出现：算新的一次
        self.n.user_acted("s1")
        self.obs("busy")
        self.obs("waiting", "permission", sig)
        self.settle()
        self.assertEqual(len(self.played), 2)
        # 直接换成另一个对话框（中间没有 busy 帧）且用户操作过：也响
        self.n.user_acted("s1")
        self.obs("waiting", "question", ("question", "颜色", "?", (), ("红", "蓝")))
        self.settle()
        self.assertEqual(len(self.played), 3)
        # 对话框重绘时指纹变化但用户没操作：不响
        self.obs("waiting", "question", ("question", "颜色", "?", (), ("红", "蓝", "绿")))
        self.settle()
        self.assertEqual(len(self.played), 3)

    def test_attention_flicker(self):
        """真机：waiting(X) → 0.3s 后闪成 busy → 0.2s 后回到 waiting(X) 并停住：响一次且只响一次。"""
        self.baseline("busy")
        x = ("permission", "Bash command", "Do you want to proceed?", ("rm -i x",), ("Yes", "No"))
        self.obs("waiting", "permission", x)
        self.clock.t += 0.3
        self.obs("busy")
        self.clock.t += 0.2
        self.obs("waiting", "permission", x)
        self.settle()
        self.assertEqual(len(self.played), 1)
        self.assertEqual([h["reason"] for h in self.n.history if h["what"] == "skip"], ["duplicate"])
        self.settle()
        self.assertEqual(len(self.played), 1)

    def test_second_permission_after_flicker(self):
        """真机：第一个权限框选 Yes → busy → 另一个权限框（命令不同），出现时闪烁 → 停在 waiting：要响。"""
        self.baseline("idle")
        a = ("permission", "Bash command", "Do you want to proceed?", ("python3 loop.py",), ("Yes", "No"))
        b = ("permission", "Bash command", "Do you want to proceed?", ("kill 1234",), ("Yes", "No"))
        self.obs("busy")
        self.obs("waiting", "permission", a)
        self.settle()
        self.assertEqual(len(self.played), 1)
        self.n.user_acted("s1")            # 选 Yes
        self.obs("busy")
        self.obs("waiting", "permission", b)
        self.clock.t += 0.3
        self.obs("busy")                   # 闪烁
        self.clock.t += 0.2
        self.obs("waiting", "permission", b)
        self.settle()
        self.assertEqual(len(self.played), 2)
        self.settle()
        self.assertEqual(len(self.played), 2)

    def test_attention_gone_before_confirm(self):
        """确认时已经不在该对话框（用户秒答 / 换成别的）：不响；之后再出现同一个对话框仍会提醒（候选阶段不记"已提醒"）。"""
        self.baseline("busy")
        x = ("permission", "t", "q", (), ("Yes", "No"))
        self.obs("waiting", "permission", x)
        self.obs("busy")
        self.settle()
        self.assertEqual(self.played, [])
        self.obs("waiting", "permission", x)
        self.settle()
        self.assertEqual(len(self.played), 1)

    def test_panels_and_trust(self):
        self.baseline("idle")
        self.obs("waiting", "model", ("model",))
        self.obs("idle")
        self.obs("waiting", "effort", ("effort",))
        self.settle()
        self.assertEqual(self.played, [])
        self.obs("idle")
        self.obs("trust", "trust", ("trust", "/x"))
        self.settle()
        self.assertEqual(len(self.played), 1)
        # status=waiting 但没解析出对话框：照样提醒
        self.obs("busy")
        self.obs("waiting", None, None)
        self.settle()
        self.assertEqual(len(self.played), 2)

    def test_global_gap(self):
        self.baseline("idle", sid="a")
        self.baseline("idle", sid="b")
        for sid in ("a", "b"):
            self.obs("busy", sid=sid)
            self.obs("idle", sid=sid)
        self.clock.t += 1.0
        self.sched.run()
        self.assertEqual(len(self.played), 1)   # 1.5 秒内最多一次
        self.assertEqual([h["reason"] for h in self.n.history if h["what"] == "skip"], ["global_gap"])

    def test_same_event_once(self):
        self.baseline("idle")
        self.obs("busy")
        self.obs("idle")
        self.obs("idle")   # 同一状态重复观察
        self.settle()
        self.settle()
        self.assertEqual(len(self.played), 1)

    def test_settings(self):
        self.baseline("idle")
        self.cfg.update(notify_on_done=False)
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.played, [])
        self.cfg.update(notify_on_done=True, notify_on_attention=False)
        self.obs("waiting", "permission", ("p",))
        self.settle()
        self.assertEqual(self.played, [])
        self.cfg.update(notify_enabled=False, notify_on_attention=True)
        self.n.user_acted("s1")
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.played, [])
        self.cfg.update(notify_enabled=True, notify_sound="Glass")
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.played, [os.path.join(self.sd, "Glass.aiff")])
        # 设置里的声音文件不在了：退回默认 Blow
        self.cfg.update(notify_sound="Gone")
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.played[-1], os.path.join(self.sd, "Blow.aiff"))

    def test_dry_run(self):
        self.cfg.update(notify_dry_run=True)
        self.baseline("idle")
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.played, [])
        self.assertEqual(self.n.history[-1]["what"], "dry_run")

    def test_local_session_stop_hook(self):
        """本机会话（jobid == "local"）：本机 CLI 的 Stop hook 已经 afplay → "完成时"不重复；"需要操作时"照常。"""
        self.local_hook.return_value = True
        self.baseline("idle", jobid="local")
        self.obs("busy", jobid="local")
        self.obs("idle", jobid="local")
        self.settle()
        self.assertEqual(self.played, [])
        self.assertEqual(self.n.history[-1]["reason"], "local_stop_hook")
        self.obs("waiting", "permission", ("p",), jobid="local")
        self.settle()
        self.assertEqual(len(self.played), 1)
        # 服务器会话不看本机 hook
        self.baseline("idle", sid="s9")
        self.obs("busy", sid="s9")
        self.obs("idle", sid="s9")
        self.settle()
        self.assertEqual(len(self.played), 2)
        self.local_hook.return_value = False
        self.obs("busy", jobid="local")
        self.obs("idle", jobid="local")
        self.settle()
        self.assertEqual(len(self.played), 3)

    def test_test_button(self):
        self.assertEqual(self.n.test("Ping"), "Ping.aiff")
        self.assertEqual(self.played, [os.path.join(self.sd, "Ping.aiff")])
        with self.assertRaises(ValueError):
            self.n.test("Ping")          # 点得太快
        self.clock.t += 1
        with self.assertRaises(ValueError):
            self.n.test("../etc/passwd")
        self.clock.t += 1
        self.cfg.update(notify_enabled=False)   # 试听不看开关
        self.assertEqual(self.n.test(), "Blow.aiff")

    def test_exited_resets(self):
        self.baseline("busy")
        self.obs("exited")
        self.obs("idle")    # exited 之后（重连/重启）= 基线
        self.settle()
        self.assertEqual(self.played, [])


class TestLocalStopHook(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="ccrhk", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.td, True)
        self.p = os.path.join(self.td, "settings.json")

    def write(self, obj):
        with open(self.p, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        st = os.stat(self.p)
        os.utime(self.p, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))   # 保证 mtime 变化（缓存失效）

    def test_detect(self):
        self.assertFalse(N.local_stop_hook_has_afplay(self.p))   # 不存在
        self.write({"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                   "command": "/usr/bin/afplay /System/Library/Sounds/Blow.aiff"}]}]}})
        self.assertTrue(N.local_stop_hook_has_afplay(self.p))
        self.write({"hooks": {"Notification": [{"hooks": [{"type": "command", "command": "afplay x"}]}]}})
        self.assertFalse(N.local_stop_hook_has_afplay(self.p))
        self.write("{bad json")
        self.assertFalse(N.local_stop_hook_has_afplay(self.p))
        self.write({"hooks": {"Stop": "weird"}})
        self.assertFalse(N.local_stop_hook_has_afplay(self.p))
        before = os.stat(self.p)
        N.local_stop_hook_has_afplay(self.p)
        after = os.stat(self.p)
        self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))   # 只读


# ============================================================================ 设置校验
class TestConfig(unittest.TestCase):
    def setUp(self):
        self.sd = sound_dir()
        self.addCleanup(shutil.rmtree, self.sd, True)
        p = mock.patch.object(N, "SOUND_DIR", self.sd)
        p.start()
        self.addCleanup(p.stop)
        self.td = tempfile.mkdtemp(prefix="ccrcf", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.td, True)

    def test_defaults_and_validate(self):
        self.assertEqual({k: C.DEFAULT_CONFIG[k] for k in ("notify_enabled", "notify_on_done", "notify_on_attention",
                                                           "notify_sound")},
                         {"notify_enabled": True, "notify_on_done": True, "notify_on_attention": True,
                          "notify_sound": "Blow"})
        self.assertEqual(C.validate_partial({"notify_sound": "Glass", "notify_enabled": False}),
                         {"notify_sound": "Glass", "notify_enabled": False})
        for bad in ({"notify_sound": "Nope"}, {"notify_sound": "../x"}, {"notify_sound": 1},
                    {"notify_enabled": "yes"}, {"notify_on_done": 1}, {"notify_dry_run": True}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                C.validate_partial(bad)

    def test_store_roundtrip_and_public(self):
        store = C.ConfigStore(pathlib.Path(self.td, "config.json"), overrides={"notify_dry_run": True})
        store.update({"notify_sound": "Ping", "notify_on_attention": False})
        self.assertEqual(C.ConfigStore(pathlib.Path(self.td, "config.json")).get()["notify_sound"], "Ping")
        pub = C.public_config(store.get())
        self.assertEqual(pub["notify_sound"], "Ping")
        self.assertNotIn("notify_dry_run", pub)   # 运行时覆盖项不给前端
        # 文件里的声音后来不存在了：读取时退回默认
        os.unlink(os.path.join(self.sd, "Ping.aiff"))
        self.assertEqual(C.ConfigStore(pathlib.Path(self.td, "config.json")).get()["notify_sound"], "Blow")


# ============================================================================ server
class TestServerNotify(ServerBase):
    def setUp(self):
        super().setUp()
        self.sd = sound_dir()
        self.addCleanup(shutil.rmtree, self.sd, True)
        p = mock.patch.object(N, "SOUND_DIR", self.sd)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(N, "local_stop_hook_has_afplay", return_value=True)
        p2.start()
        self.addCleanup(p2.stop)

    def test_info_and_test(self):
        st, js, _, _ = self.req("GET", "/api/notify")
        self.assertEqual(st, 200, js)
        self.assertEqual(js["sounds"], ["Blow", "Glass", "Ping"])
        self.assertEqual(js["default_sound"], "Blow")
        self.assertIs(js["local_stop_hook"], True)
        self.assertIs(js["available"], False)    # 桩 manager 没有 notifier
        self.assertErr(self.req("POST", "/api/notify/test", {}), 503)
        played = []
        self.mgr.notifier = N.Notifier(lambda: {}, play=played.append, sound_dir=self.sd)
        st, js, _, _ = self.req("POST", "/api/notify/test", {"sound": "Glass"})
        self.assertEqual((st, js), (200, {"ok": True, "sound": "Glass.aiff"}))
        self.assertEqual(played, [os.path.join(self.sd, "Glass.aiff")])
        self.assertErr(self.req("POST", "/api/notify/test", {"sound": "../../etc/passwd"}), 400)
        self.assertErr(self.req("POST", "/api/notify/test", {"sound": 5}), 400)
        self.assertEqual(len(played), 1)
        # 设置经 /api/config 保存（白名单校验）
        st, js, _, _ = self.req("POST", "/api/config", {"notify_sound": "Ping", "notify_on_done": False})
        self.assertEqual(st, 200, js)
        self.assertEqual((js["notify_sound"], js["notify_on_done"]), ("Ping", False))
        self.assertErr(self.req("POST", "/api/config", {"notify_sound": "Nope"}), 400)
        self.assertErr(self.req("POST", "/api/config", {"notify_dry_run": True}), 400)


# ============================================================================ 完成回调（侧栏"新回复"）
class TestDoneCallback(Base):
    """完成确认后回调 on_event(sid, "done")：与提示音同一个判定，但不受"响不响"影响。"""

    def setUp(self):
        super().setUp()
        self.events = []
        self.n.add_listener(lambda sid, kind: self.events.append((sid, kind)))

    def turn(self, sid="s1", jobid="123", interrupted=False):
        self.obs("busy", sid=sid, jobid=jobid)
        self.obs("idle", sid=sid, jobid=jobid, interrupted=interrupted)

    def baseline(self, sid="s1", jobid="123"):
        self.n.reset(sid)
        self.obs("idle", sid=sid, jobid=jobid)
        self.clock.t += 3

    def test_done_calls_back_once(self):
        self.baseline()
        self.turn()
        self.assertEqual(self.events, [])          # 确认延迟里还不算
        self.settle()
        self.assertEqual(self.events, [("s1", "done")])
        self.assertEqual(len(self.played), 1)

    def test_called_even_when_sound_skipped(self):
        # 声音被设置关掉
        self.cfg = {"notify_enabled": False}
        self.baseline()
        self.turn()
        self.settle()
        self.cfg = {"notify_on_done": False}
        self.turn()
        self.settle()
        self.assertEqual(self.events, [("s1", "done")] * 2)
        self.assertEqual(self.played, [])
        self.assertEqual([h.get("reason") for h in self.n.history[-2:]], ["disabled", "disabled"])
        # 本机 CLI 自己的 Stop hook 会响
        self.cfg = {}
        self.local_hook.return_value = True
        self.baseline("s2", N.LOCAL_JOBID)
        self.turn("s2", N.LOCAL_JOBID)
        self.settle()
        self.assertEqual(self.events[-1], ("s2", "done"))
        self.assertEqual(self.n.history[-1]["reason"], "local_stop_hook")
        self.assertEqual(self.played, [])
        # 全局间隔内第二个会话完成：只响一次，但两个都回调
        self.local_hook.return_value = False
        self.baseline("s3")
        self.baseline("s4")
        self.turn("s3")
        self.turn("s4")
        self.settle()
        self.assertEqual(self.events[-2:], [("s3", "done"), ("s4", "done")])
        self.assertEqual(len(self.played), 1)
        self.assertEqual(self.n.history[-1]["reason"], "global_gap")

    def test_not_called(self):
        # 屏幕显示 Interrupted
        self.baseline()
        self.turn(interrupted=True)
        self.settle()
        # 用户中断
        self.turn()
        self.n.suppress_done("s1")
        self.settle()
        self.obs("busy")
        self.n.suppress_done("s1")
        self.obs("idle")
        self.settle()
        # 确认延迟内又忙起来
        self.turn()
        self.obs("busy")
        self.settle()
        self.obs("idle")
        self.clock.t += 0.1
        # 关闭 / 重启（mute）
        self.sched.calls.clear()
        self.obs("busy")
        self.n.mute("s1")
        self.obs("idle")
        self.settle()
        # 启动 / 重新附着的基线：busy → idle 不算
        self.n.reset("s2")
        self.obs("busy", sid="s2")
        self.obs("idle", sid="s2")
        self.settle()
        # 需要操作（对话框）不算完成
        self.baseline("s3")
        self.obs("busy", sid="s3")
        self.obs("waiting", "permission", "sigA", sid="s3")
        self.settle()
        self.assertEqual(self.events, [])
        self.assertTrue(self.played)               # 对话框照常响
        self.assertEqual(self.n.history[-1]["event"], "attention")

    def test_callback_outside_lock(self):
        """回调里再拿 Notifier 的锁（模拟 SessionManager：s.lock → _emit_meta → observe）不死锁。"""
        seen = []

        def cb(sid, kind):
            got = self.n._lock.acquire(timeout=1)
            seen.append(got)
            if got:
                self.n._lock.release()
            self.n.observe(sid, "123", "idle")

        self.n.add_listener(cb)
        self.baseline()
        self.turn()
        self.settle()
        self.assertEqual(seen, [True])

    def test_callback_error_does_not_break_sound(self):
        self.n.add_listener(lambda sid, kind: 1 / 0)
        self.baseline()
        self.turn()
        with self.assertLogs("ccr.notify", "ERROR"):
            self.settle()
        self.assertEqual(len(self.played), 1)
        self.assertEqual(self.events, [("s1", "done")])

    def test_null_notifier_silent_but_calls_back(self):
        n = N.NullNotifier()
        self.assertIsNone(n.test)
        ev, timers = [], []
        n._schedule = lambda d, f: timers.append(f)
        n.QUIET_AFTER_RESET = 0.0
        n.add_listener(lambda sid, kind: ev.append(kind))
        with mock.patch.object(N, "play_file") as pf:
            n.observe("s1", "1", "idle")
            n.observe("s1", "1", "busy")
            n.observe("s1", "1", "idle")
            for f in timers:
                f()
        self.assertEqual(ev, ["done"])
        pf.assert_not_called()
        self.assertEqual(n.history[-1]["reason"], "disabled")


# ============================================================================ SessionManager 集成
class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name,) + a)


class TestSessionIntegration(SessionBase):
    def mgr_with(self, notifier):
        def factory(cfg, jobid, on_event, on_state):
            from tests.test_session import StubLink
            return StubLink(self.rem, cfg, jobid, on_event, on_state)
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp), link_factory=factory, notifier=notifier)
        m.step_timeout = 1.0
        m.poll_interval = 0.01
        m.link_timeout = 3.0
        m.exit_timeout = 2.0
        self.addCleanup(lambda: m._shutdown or m.shutdown())
        return m

    def test_hooks_called(self):
        rec = Recorder()
        m = self.mgr_with(rec)
        sid = self.start(m)
        names = [c[0] for c in rec.calls]
        self.assertIn("reset", names)                       # watch 之前重置基线
        obs = [c for c in rec.calls if c[0] == "observe"]
        self.assertTrue(obs and obs[-1][3] == "idle", obs)
        t = self.tui(sid)
        t.busy = True
        m._fresh(m._get(sid))
        self.assertTrue(any(c[0] == "observe" and c[3] == "busy" for c in rec.calls))
        rec.calls.clear()
        m.interrupt(sid)
        self.assertIn(("suppress_done", sid), rec.calls)
        rec.calls.clear()
        t.view = "perm"
        m._fresh(m._get(sid))
        d = m.get_screen(sid)["state"]["dialog"]
        self.assertTrue(any(c[0] == "observe" and c[3] == "waiting" and c[4] == "permission" for c in rec.calls))
        m.choose(sid, 3, expect=S.dialog_fingerprint(d))
        self.assertIn(("user_acted", sid), rec.calls)
        rec.calls.clear()
        m.send_keys(sid, ["Escape"])
        self.assertIn(("suppress_done", sid), rec.calls)
        m.remove(sid)
        self.assertIn(("forget", sid), rec.calls)

    def test_end_to_end_with_real_notifier(self):
        """真实 Notifier（afplay 用 mock）：发消息 → 忙 → 空闲 = 响一次；权限框 = 响一次；默认 NullNotifier 不响。"""
        played = []
        n = N.Notifier(lambda: dict(self.cfg), play=played.append, sound_dir=None)
        n.CONFIRM_DELAY = 0.05
        n.QUIET_AFTER_RESET = 0.0
        n.GLOBAL_GAP = 0.0
        p = mock.patch.object(N, "sound_path", return_value="/System/Library/Sounds/Blow.aiff")
        p.start()
        self.addCleanup(p.stop)
        m = self.mgr_with(n)
        sid = self.start(m)
        t = self.tui(sid)
        t.busy = True
        m._fresh(m._get(sid))
        t.busy = False
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: len(played) == 1, 3), n.history)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["unread"], 3))      # 响一声 = 侧栏"新回复"
        t.view = "perm"
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: len(played) == 2, 3), n.history)
        self.assertEqual(played, ["/System/Library/Sounds/Blow.aiff"] * 2)
        done_at = m.get_meta(sid)["done_at"]
        self.assertEqual(m.get_meta(sid)["state"], "waiting")                  # 对话框不改 done_at
        self.assertEqual(m.get_meta(sid)["done_at"], done_at)

    def test_default_is_silent(self):
        m = self.mgr()
        self.assertIsInstance(m.notifier, N.NullNotifier)


if __name__ == "__main__":
    unittest.main()


class TestMuteOnClose(unittest.TestCase):
    def test_mute_blocks_attention_and_reset_restores(self):
        from ccr import notify as nt
        played, timers = [], []
        clock = [100.0]
        n = nt.Notifier(lambda: {}, play=lambda p: played.append(p) or True, clock=lambda: clock[0],
                        schedule=lambda d, f: timers.append(f), local_hook=lambda: False)
        n.observe("s1", "1", "idle")          # 基线
        clock[0] += 5
        n.observe("s1", "1", "busy")
        n.mute("s1")
        n.observe("s1", "1", "waiting", "permission", "sigA")
        for f in timers: f()
        timers.clear()
        self.assertEqual(played, [])
        n.reset("s1")                           # 重新启动
        n.observe("s1", "1", "idle")
        clock[0] += 5
        n.observe("s1", "1", "waiting", "permission", "sigB")
        for f in timers: f()
        self.assertEqual(len(played), 1)
