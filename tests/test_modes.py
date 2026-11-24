"""SPEC §13：本机用系统"选择文件夹"对话框（osascript，mock，绝不真的弹出）、新建会话的默认权限（node 跑
static/launchprefs.js）、权限模式选择（set_mode：逐个 Shift+Tab 并确认）、restart 覆盖权限启动参数、fake 端到端。"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from ccr import macdialog as MD, session as S, tui_parse
from tests.test_node_agent import HAVE_TMUX, TMUX
from tests.test_server import Base as ServerBase
from tests.test_session import Base as SessionBase, SimTUI, wait_until

ROOT = pathlib.Path(__file__).resolve().parent.parent


class NoDialog:
    """安全网：任何没有 mock 掉的 osascript 调用直接失败（用户就坐在电脑前，绝不能真的弹框）。"""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(MD.subprocess, "run", side_effect=AssertionError("测试里不能真的运行 osascript"))
        p.start()
        self.addCleanup(p.stop)


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


# ============================================================================ 选择文件夹（osascript）
class TestChooseFolder(NoDialog, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.td = os.path.realpath(tempfile.mkdtemp(prefix="ccrcf", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.td, True)
        p = mock.patch.object(MD, "available", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, result=None, exc=None, default=None, **kw):
        calls = []

        def run(argv, **k):
            calls.append((argv, k))
            if exc is not None:
                raise exc
            return result
        return MD.choose_folder(default, cfg={}, run=run, **kw), calls

    def test_argv_and_path(self):
        r, calls = self.run_with(done(0, self.td + "/\n"), default=self.td)
        self.assertEqual(r, {"path": self.td})
        argv, kw = calls[0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[-1], self.td)                    # 默认目录只是 argv 的数据
        script = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertEqual(tuple(script), MD.SCRIPT_LINES)
        self.assertEqual(len(argv), 1 + 2 * len(MD.SCRIPT_LINES) + 1)
        self.assertFalse(kw.get("shell"))
        self.assertEqual(kw.get("timeout"), 600.0)
        self.assertEqual(kw.get("stdin"), subprocess.DEVNULL)
        joined = "\n".join(script)
        self.assertIn("choose folder", joined)
        self.assertIn("on run argv", joined)
        self.assertIn("tell me to activate", joined)
        self.assertNotIn("Finder", joined)                      # 不触发 TCC 自动化授权
        self.assertNotIn("System Events", joined)
        self.assertNotIn(self.td, joined)

    def test_injection_stays_data(self):
        evil = os.path.join(self.td, 'a" & (do shell script "touch ccr_pwned") & "')
        os.mkdir(evil)
        r, calls = self.run_with(done(0, evil + "/\n"), default=evil)
        argv = calls[0][0]
        self.assertEqual(argv[-1], evil)
        self.assertTrue(all(evil not in a for a in argv[:-1]))
        self.assertEqual(r["path"], evil)

    def test_default_normalization(self):
        home = os.path.expanduser("~")
        self.assertEqual(MD.default_dir("~"), home)
        self.assertEqual(MD.default_dir(self.td + "/x/.."), self.td)
        for bad in (None, "", "relative/dir", self.td + "/nope", "/etc/hosts", "~user", "a\nb", 3):
            self.assertIsNone(MD.default_dir(bad), bad)
        # 默认目录不存在 / 不合法：对话框不带 default location（argv 不带参数）
        _, calls = self.run_with(done(0, "/Users/x/\n"), default=self.td + "/nope")
        self.assertEqual(len(calls[0][0]), 1 + 2 * len(MD.SCRIPT_LINES))
        _, calls = self.run_with(done(0, "/Users/x/\n"))
        self.assertEqual(len(calls[0][0]), 1 + 2 * len(MD.SCRIPT_LINES))

    def test_result_normalization(self):
        self.assertEqual(MD.normalize_result("/\n"), "/")
        self.assertEqual(MD.normalize_result("/Users/a b/中文/\n"), "/Users/a b/中文")
        self.assertEqual(MD.normalize_result("/x//"), "/x")
        self.assertIsNone(MD.normalize_result(""))
        self.assertIsNone(MD.normalize_result("Macintosh HD:Users:x:"))
        r, _ = self.run_with(done(0, "/\n"))
        self.assertEqual(r, {"path": "/"})

    def test_cancel_and_errors(self):
        r, _ = self.run_with(done(1, "", "0:1: execution error: User canceled. (-128)\n"))
        self.assertEqual(r, {"cancelled": True})
        with self.assertRaises(MD.DialogError) as cm:
            self.run_with(done(1, "", "execution error: something else (-1700)\n"))
        self.assertFalse(cm.exception.timeout)
        with self.assertRaises(MD.DialogError) as cm:
            self.run_with(exc=subprocess.TimeoutExpired(["osascript"], 600))
        self.assertTrue(cm.exception.timeout)
        with self.assertRaises(MD.DialogError):
            self.run_with(done(0, "not a path\n"))
        with self.assertRaises(MD.DialogError):
            self.run_with(exc=OSError(2, "No such file"))

    def test_single_dialog(self):
        entered, release = threading.Event(), threading.Event()

        def slow(argv, **k):
            entered.set()
            release.wait(5)
            return done(0, "/tmp/\n")
        out = {}
        t = threading.Thread(target=lambda: out.update(MD.choose_folder(None, cfg={}, run=slow)))
        t.start()
        self.assertTrue(entered.wait(5))
        with self.assertRaises(MD.Busy):
            MD.choose_folder(None, cfg={}, run=slow)
        release.set()
        t.join(5)
        self.assertEqual(out, {"path": "/tmp"})
        r, _ = self.run_with(done(0, "/tmp/\n"))   # 释放后可以再开
        self.assertEqual(r, {"path": "/tmp"})


class TestChooseFolderUnavailable(NoDialog, unittest.TestCase):
    """真实的 available()（不 mock）：fake 模式 / 非 macOS / 没有 osascript → 501，且绝不运行 osascript。"""

    def test_unavailable(self):
        self.assertFalse(MD.available({"fake": True}))
        with self.assertRaises(MD.Unavailable):
            MD.choose_folder("~", cfg={"fake": True})
        with mock.patch.object(MD.sys, "platform", "linux"):
            self.assertFalse(MD.available({}))
            with self.assertRaises(MD.Unavailable):
                MD.choose_folder("~", cfg={})
        with mock.patch.object(MD, "OSASCRIPT", "/definitely/not/osascript"):
            self.assertFalse(MD.available({}))
            with self.assertRaises(MD.Unavailable):
                MD.choose_folder("~", cfg={})
        MD.subprocess.run.assert_not_called()


class TestChooseFolderApi(NoDialog, ServerBase):
    def test_routes(self):
        with mock.patch.object(MD, "choose_folder", return_value={"path": "/Users/u/proj"}) as cf:
            st, js, _, _ = self.req("POST", "/api/local/choose_folder", {"default": "~/proj"})
        self.assertEqual((st, js), (200, {"path": "/Users/u/proj"}))
        self.assertEqual(cf.call_args[0][0], "~/proj")
        with mock.patch.object(MD, "choose_folder", return_value={"cancelled": True}):
            st, js, _, _ = self.req("POST", "/api/local/choose_folder", {})
        self.assertEqual((st, js), (200, {"cancelled": True}))
        for exc, status, code in ((MD.Unavailable("x"), 501, "unavailable"), (MD.Busy("x"), 409, "dialog_busy"),
                                  (MD.DialogError("超时", timeout=True), 504, "timeout"),
                                  (MD.DialogError("坏了"), 502, "dialog_error")):
            with mock.patch.object(MD, "choose_folder", side_effect=exc):
                st, js, _, _ = self.req("POST", "/api/local/choose_folder", {})
            self.assertEqual((st, js.get("code")), (status, code))
        self.assertErr(self.req("POST", "/api/local/choose_folder", {"default": 5}), 400)
        # 需要 token
        st, _, _, _ = self.req("POST", "/api/local/choose_folder", {}, auth=False)
        self.assertEqual(st, 401)

    def test_fake_mode_501_without_osascript(self):
        self.store.overrides["fake"] = True
        st, js, _, _ = self.req("POST", "/api/local/choose_folder", {"default": "~"})
        self.assertEqual((st, js.get("code")), (501, "unavailable"))


# ============================================================================ 新建会话的默认权限（static/launchprefs.js）
@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestDefaultPermission(unittest.TestCase):
    def test_launchprefs(self):
        url = (ROOT / "static" / "launchprefs.js").as_uri()
        js = r"""
const m = await import(%s);
const mem = () => { const d = {}; return { getItem: (k) => (k in d ? d[k] : null), setItem: (k, v) => { d[k] = String(v); }, d }; };
const out = {};
let st = mem();
out.init = [m.loadPerm('local', st), m.loadPerm('server', st)];
m.savePerm('local', 'plan', st); m.savePerm('server', '__skip__', st);
out.saved = [m.loadPerm('local', st), m.loadPerm('server', st)];
m.savePerm('local', 'evil', st);
out.invalid = m.loadPerm('local', st);
st = mem(); st.setItem('ccr.perm.local', 'garbage');
out.garbage = m.loadPerm('local', st);
st = mem();
out.resumeLocal = m.initialPerm('local', { resume: 'u' }, st);
out.presetSkip = m.initialPerm('server', { skip_permissions: true }, st);
out.presetMode = m.initialPerm('local', { permission_mode: 'dontAsk' }, st);
st.setItem('ccr.perm.local', ''); out.localEmpty = m.initialPerm('local', {}, st);
const broken = { getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); } };
out.broken = m.loadPerm('local', broken); m.savePerm('local', 'plan', broken);
console.log(JSON.stringify(out));
""" % json.dumps(url)
        r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["init"], ["", ""])                 # 初始都 = 不加参数（安全默认）
        self.assertEqual(out["saved"], ["plan", "__skip__"])    # 按运行位置分别记住
        self.assertEqual(out["invalid"], "plan")                 # 非法值不保存
        self.assertEqual(out["garbage"], "")
        self.assertEqual(out["resumeLocal"], "")                 # 从历史续接：没有记住的选择时不加参数
        self.assertEqual(out["presetSkip"], "__skip__")
        self.assertEqual(out["presetMode"], "dontAsk")
        self.assertEqual(out["localEmpty"], "")                  # 用户上次在本机选了"不加参数"：尊重
        self.assertEqual(out["broken"], "")


# ============================================================================ 权限模式选择（set_mode）
REAL = {"manual": "⏸ manual mode on", "acceptEdits": "⏵⏵ accept edits on (shift+tab to cycle)",
        "plan": "⏸ plan mode on (shift+tab to cycle)", "bypassPermissions": "⏵⏵ bypass permissions on (shift+tab to cycle)",
        "auto": "⏵⏵ auto mode on (shift+tab to cycle)", "dontAsk": "⏵⏵ don't ask on (shift+tab to cycle)"}


class ModeSim(SimTUI):
    """真机实测的模式行与 Shift+Tab 循环顺序（bypass 只在带跳过权限启动时出现）。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.m = "manual"
        self.bypass = False
        self.order = None        # 覆盖循环（测试"转一圈到不了"）
        self.stuck = False       # BTab 无反应
        self.dialog_after = None  # 第 N 次 BTab 后弹出权限框
        self.btabs = 0

    def _footer(self):
        out = super()._footer()
        out[-1] = "  " + REAL[self.m] + (" · esc to interrupt" if self.busy else "") + " · ← for agents"
        return out

    def key(self, k):
        with self.lock:
            if k == "BTab" and self.view == "idle":
                self.btabs += 1
                if not self.stuck:
                    order = self.order or (["manual", "acceptEdits", "plan"] + (["bypassPermissions"] if self.bypass else [])
                                           + ["auto"])
                    self.m = order[(order.index(self.m) + 1) % len(order)] if self.m in order else order[0]
                if self.dialog_after == self.btabs:
                    self.view = "perm"
                return
        super().key(k)


class ModeBase(SessionBase):
    def setUp(self):
        super().setUp()
        import tests.test_session as TS
        orig = TS.SimTUI
        TS.SimTUI = ModeSim
        self.addCleanup(setattr, TS, "SimTUI", orig)

    def ready(self, m, mode="manual", bypass=False, **kw):
        sid = self.start(m, **kw)
        t = self.tui(sid)
        t.m, t.bypass = mode, bypass
        m._fresh(m._get(sid))
        self.assertEqual(m.get_meta(sid)["mode"], mode)
        return sid, t

    def btabs(self, sid):
        return [p for op, p in self.rem.log if op == "keys" and p["sid"] == sid and p["keys"] == ["BTab"]]


class TestSetMode(ModeBase):
    def test_step_by_step(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual")
        r = m.set_mode(sid, "auto")
        self.assertEqual(r, {"ok": True, "mode": "auto", "steps": 3})   # manual → acceptEdits → plan → auto
        ks = self.btabs(sid)
        self.assertEqual(len(ks), 3)
        self.assertTrue(all(k["guard"] == {"input_box": True} for k in ks))
        self.assertEqual(m.get_meta(sid)["mode"], "auto")
        self.assertEqual(m.get_meta(sid)["mode_cycle"], ["manual", "acceptEdits", "plan", "auto"])

    def test_already_there_and_busy(self):
        m = self.mgr()
        sid, t = self.ready(m, "plan")
        self.assertEqual(m.set_mode(sid, "plan"), {"ok": True, "mode": "plan", "steps": 0})
        self.assertEqual(self.btabs(sid), [])
        t.busy = True   # 生成中也可以切（CLI 本身支持）
        self.assertEqual(m.set_mode(sid, "manual")["steps"], 2)          # plan → auto → manual
        self.assertEqual(t.m, "manual")

    def test_with_bypass(self):
        m = self.mgr()
        sid, t = self.ready(m, "bypassPermissions", bypass=True, skip_permissions=True)
        self.assertEqual(m.get_meta(sid)["mode_cycle"], ["manual", "acceptEdits", "plan", "bypassPermissions", "auto"])
        self.assertEqual(m.set_mode(sid, "plan")["steps"], 4)            # bypass → auto → manual → acceptEdits → plan
        self.assertEqual(m.set_mode(sid, "bypassPermissions")["steps"], 1)

    def test_needs_restart(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual")
        for target in ("bypassPermissions", "dontAsk"):
            with self.assertRaises(S.OpError) as cm:
                m.set_mode(sid, target)
            self.assertEqual(cm.exception.api_code, "mode_needs_restart")
        self.assertEqual(self.btabs(sid), [])
        with self.assertRaises(ValueError):
            m.set_mode(sid, "yolo")

    def test_unreachable_after_full_circle(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual")
        t.order = ["manual", "acceptEdits", "plan"]   # 真机循环里没有 auto（假设）：转一圈回到起点就停
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid, "auto")
        self.assertEqual(cm.exception.api_code, "mode_unreachable")
        self.assertIn("manual", str(cm.exception))
        self.assertEqual(len(self.btabs(sid)), 3)

    def test_max_steps(self):
        m = self.mgr()
        m.max_mode_steps = 2
        sid, t = self.ready(m, "manual")
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid, "auto")
        self.assertEqual(cm.exception.api_code, "mode_unreachable")
        self.assertIn("plan", str(cm.exception))   # 停在 plan
        self.assertEqual(len(self.btabs(sid)), 2)

    def test_stuck(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual")
        t.stuck = True
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid, "plan")
        self.assertEqual(cm.exception.api_code, "mode_stuck")
        self.assertIn("manual", str(cm.exception))
        self.assertEqual(len(self.btabs(sid)), 1)

    def test_dialog(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual")
        t.dialog_after = 1   # 第一次 BTab 后弹出权限框：立即停止
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid, "auto")
        self.assertEqual(cm.exception.api_code, "dialog_open")
        self.assertEqual(len(self.btabs(sid)), 1)
        # 一开始就有对话框：一个键都不按
        n = len(self.rem.log)
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid, "plan")
        self.assertEqual(cm.exception.api_code, "dialog_open")
        self.assertEqual([op for op, p in self.rem.log[n:] if op == "keys"], [])

    def test_guard_failure_sends_nothing(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual")

        def pop(tt, p):
            tt.view = "perm"   # agent 复核时恰好弹出权限框
        self.rem.before_guard = pop
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid, "plan")
        self.assertEqual(cm.exception.api_code, "screen_changed")
        self.assertEqual(t.btabs, 0)   # BTab 没有到达终端
        self.assertEqual(t.m, "manual")


class TestRestartOverride(ModeBase):
    def args(self):
        return self.rem.ops("start")[-1]["args"]

    def test_override_modes(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual")
        self.converse(m, sid)   # 有过对话才会 --resume（没有对话的会话重启时以新会话启动，见下一个测试）
        # 会话里改过模型、切到了 plan：覆盖 dontAsk 之后模型仍沿用当前值，权限用覆盖值
        t.current = 3   # Sonnet
        t.m = "plan"
        m._fresh(m._get(sid))
        m.restart(sid, mode="dontAsk")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        a = self.args()
        self.assertIn("--resume", a)
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertEqual(a[a.index("--model") + 1], "sonnet")
        la = m.get_meta(sid)["launch"]
        self.assertEqual((la["permission_mode"], la["skip_permissions"]), ("dontAsk", False))
        self.assertEqual(m.get_meta(sid)["mode_cycle"], ["manual", "acceptEdits", "plan", "auto"])
        # 切到 bypass：带跳过权限、不带 --permission-mode
        m.restart(sid, mode="bypassPermissions")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        a = self.args()
        self.assertIn("--dangerously-skip-permissions", a)
        self.assertNotIn("--permission-mode", a)
        self.assertIn("bypassPermissions", m.get_meta(sid)["mode_cycle"])
        # 从跳过权限切回普通模式：去掉跳过权限
        m.restart(sid, mode="plan")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        a = self.args()
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertEqual(a[a.index("--permission-mode") + 1], "plan")

    def test_override_without_conversation(self):
        """v2.2.1：还没有任何对话的会话重启时不带 --resume（以新会话启动）；权限覆盖照样生效。"""
        m = self.mgr()
        sid, t = self.ready(m, "manual")
        m.restart(sid, mode="bypassPermissions")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        a = self.args()
        self.assertNotIn("--resume", a)
        self.assertIn("--dangerously-skip-permissions", a)
        self.assertTrue(m.get_meta(sid)["launch"]["skip_permissions"])
        # 万一 --resume 报 No conversation found 自动以新会话重启（restart 不带 mode）：沿用已覆盖的权限参数
        m.restart(sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        self.assertIn("--dangerously-skip-permissions", self.args())

    def test_no_override_keeps_old_behaviour(self):
        m = self.mgr()
        sid, t = self.ready(m, "manual", bypass=True, skip_permissions=True)
        t.m = "plan"
        m._fresh(m._get(sid))
        m.restart(sid)   # 不覆盖：跳过权限启动的会话不动模式（沿用以前的规则）
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        a = self.args()
        self.assertIn("--dangerously-skip-permissions", a)
        self.assertNotIn("--permission-mode", a)
        with self.assertRaises(ValueError):
            m.restart(sid, mode="yolo")

    def test_modes_seen_resets(self):
        m = self.mgr()
        sid, t = self.ready(m, "bypassPermissions", bypass=True)   # 屏幕上见过 bypass（启动参数里没有）
        self.assertIn("bypassPermissions", m.get_meta(sid)["mode_cycle"])
        m.restart(sid, mode="manual")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        self.assertNotIn("bypassPermissions", m.get_meta(sid)["mode_cycle"])


class TestModeApi(ServerBase):
    def test_routes(self):
        sid = self.mgr.create(jobid="276403", cwd="/x")["sid"]
        calls = []
        self.mgr.set_mode = lambda s, mode: calls.append(("mode", s, mode)) or {"ok": True, "mode": mode, "steps": 1}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/mode" % sid, {"mode": "plan"})
        self.assertEqual((st, js["mode"]), (200, "plan"))
        self.assertErr(self.req("POST", "/api/sessions/%s/mode" % sid, {"mode": "yolo"}), 400)
        self.assertErr(self.req("POST", "/api/sessions/%s/mode" % sid, {}), 400)

        def boom(s, mode):
            raise S.OpError("需要重启", code="mode_needs_restart")
        self.mgr.set_mode = boom
        st, js, _, _ = self.req("POST", "/api/sessions/%s/mode" % sid, {"mode": "dontAsk"})
        self.assertEqual((st, js["code"]), (409, "mode_needs_restart"))
        rs = []
        self.mgr.restart = lambda s, **kw: rs.append(kw) or {"sid": s}
        st, _, _, _ = self.req("POST", "/api/sessions/%s/restart" % sid, {"mode": "bypassPermissions"})
        self.assertEqual(st, 200)
        self.assertEqual(rs[-1].get("mode"), "bypassPermissions")
        self.req("POST", "/api/sessions/%s/restart" % sid, {})
        self.assertNotIn("mode", rs[-1])
        self.assertErr(self.req("POST", "/api/sessions/%s/restart" % sid, {"mode": "yolo"}), 400)


class TestModeCycleHelper(unittest.TestCase):
    def test_cycle(self):
        self.assertEqual(S.mode_cycle({}), ["manual", "acceptEdits", "plan", "auto"])
        self.assertEqual(S.mode_cycle({"skip_permissions": True}),
                         ["manual", "acceptEdits", "plan", "bypassPermissions", "auto"])
        self.assertIn("bypassPermissions", S.mode_cycle({"permission_mode": "bypassPermissions"}))
        self.assertIn("bypassPermissions", S.mode_cycle({}, {"bypassPermissions"}))
        self.assertNotIn("dontAsk", S.mode_cycle({"permission_mode": "dontAsk"}))

    def test_real_mode_lines_parse(self):
        for mode, line in REAL.items():
            for tail in ("", " · ← for agents", " · esc to interrupt", " · 1 shell · esc to interrupt · ↓ to manage"):
                rule = "─" * 80
                st = tui_parse.parse_screen("\n".join(["", rule, "❯ ", rule, "  u@h ▱ 0% [Haiku · effort:low]",
                                                       "  " + line + tail]))
                self.assertEqual(st["mode"], mode, line + tail)


# ============================================================================ fake 端到端（真实 AgentLink + fake_tui + 本机 tmux）
@unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
class TestFakeModesEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccr-me-", dir="/tmp"))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "proj"))
        self.cfg = {"host": "gpu-cluster", "jobid": "12345", "default_cwd": "~", "fake": True,
                    "fake_home": self.home, "fake_tmpdir": os.path.join(self.tmp, "tmux"), "fake_tmux": TMUX,
                    "fake_env": {"CCR_FAKE_DELAY": "0.03", "CCR_FAKE_THINK": "0.1"}}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # 绝不碰真实的 /tmp/tmux-<uid>/ccr：用 -S 指定临时目录里的套接字绝对路径（TMUX_TMPDIR 指向不存在的目录时
        # tmux 会退回 /tmp），套接字目录不存在就不调用 tmux
        sock_dir = os.path.join(self.tmp, "tmux", "tmux-%d" % os.getuid())
        if os.path.isdir(sock_dir):
            env = dict(os.environ)
            env.pop("TMUX", None)
            subprocess.run([TMUX, "-S", os.path.join(sock_dir, "ccr"), "-f", "/dev/null", "kill-server"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        shutil.rmtree(self.tmp, True)

    def mgr(self):
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp, "data"))
        m.exit_timeout = 8
        self.addCleanup(lambda: m._shutdown or m.shutdown())
        return m

    def wait_idle(self, m, sid):
        ok = wait_until(lambda: m.get_meta(sid)["state"] == "idle" and m.get_screen(sid)["state"].get("mode_line"),
                        20, 0.1)
        self.assertTrue(ok, (m.get_meta(sid), m.get_screen(sid)["text"]))

    def test_modes(self):
        m = self.mgr()
        sid = m.create(jobid="12345", cwd="~/proj", skip_permissions=True)["sid"]
        self.wait_idle(m, sid)
        self.assertEqual(m.get_meta(sid)["mode"], "bypassPermissions")
        r = m.set_mode(sid, "plan")
        self.assertEqual((r["mode"], r["steps"]), ("plan", 4))
        self.assertEqual(m.get_screen(sid)["state"]["mode"], "plan")
        self.assertEqual(m.set_mode(sid, "bypassPermissions")["steps"], 1)
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid, "dontAsk")
        self.assertEqual(cm.exception.api_code, "mode_needs_restart")
        # 不带跳过权限的会话：bypass 需要重启；restart(mode=bypass) 之后就有了
        sid2 = m.create(jobid="12345", cwd="~/proj")["sid"]
        self.wait_idle(m, sid2)
        self.assertEqual(m.get_meta(sid2)["mode"], "manual")
        self.assertEqual(m.set_mode(sid2, "auto")["steps"], 3)
        with self.assertRaises(S.OpError) as cm:
            m.set_mode(sid2, "bypassPermissions")
        self.assertEqual(cm.exception.api_code, "mode_needs_restart")
        m.restart(sid2, mode="bypassPermissions")
        self.assertTrue(wait_until(lambda: m.get_meta(sid2)["mode"] == "bypassPermissions"
                                   and m.get_meta(sid2)["state"] == "idle", 20, 0.1), m.get_screen(sid2)["text"])
        self.assertTrue(m.get_meta(sid2)["launch"]["skip_permissions"])


if __name__ == "__main__":
    unittest.main()
