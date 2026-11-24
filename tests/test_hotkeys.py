"""CLI 快捷键提示（ctrl+b 转后台、ctrl+x ctrl+s 立即发送排队消息）：屏幕解析（真机 fixture）、按键白名单、
node_agent 的 guard.hint（白名单式）、SessionManager.run_in_background / send_now（409、guard）、server 接口、
fake_tui 的假场景。不运行真实 claude。"""
import json
import pathlib
import time
import unittest

from ccr import fake_tui, node_agent, server as S_srv, session as S, tui_parse
from tests.test_fake_tui import Base as FakeBase
from tests.test_server import Base as ServerBase
from tests.test_session import Base as SessionBase, SimTUI, RULE, wait_until

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "tui_screens_keys.ndjson"


def frames():
    return {d["name"]: d for d in (json.loads(x) for x in FIX.read_text(encoding="utf-8").splitlines() if x.strip())}


FR = frames()


# ============================================================================ 解析
class TestHintsParse(unittest.TestCase):
    def test_real_frame_bg_queued(self):
        """123456 真机摘录：后台提示 + 排队消息 + 占位文字；不当作用户草稿、不当作回显。"""
        self.assertEqual(FR["bg_queued"]["source"], "real-excerpt")
        for name in ("bg_queued", "bg_queued_wide"):
            st = tui_parse.parse_screen(FR[name]["plain"])
            self.assertEqual(st["hints"], {"background": True, "send_now": True, "queued": ["只回复：Q3-OK"],
                                           "shells": None}, name)
            self.assertTrue(st["busy"])
            self.assertIsNone(st["dialog"])
            self.assertEqual(st["input"]["text"], "", name)   # 占位文字不是用户输入
            self.assertTrue(st["input"]["visible"])
            self.assertEqual(st["input"]["placeholder"], tui_parse.QUEUED_PLACEHOLDER)
            self.assertIsNone(st["echo"], name)               # 排队的 "❯ 只回复：Q3-OK" 不是已发送的回显
            self.assertEqual(st["spinner"], "Jitterbugging… (8s · ↓ 124 tokens)")
            self.assertEqual(st["spinner_info"]["tokens"], 124)
            live = st["live"] or ""
            self.assertIn("Running 45-second counting loop", live)
            self.assertNotIn("Q3-OK", live)                   # 排队消息不进实时草稿
            self.assertNotIn("Queued", live)
            self.assertNotIn("run in background", live)       # 提示由按钮呈现
            self.assertNotIn("/effort", live)                 # 输入框上方的 effort 提示
            self.assertNotIn("focus-events", live)            # tmux focus-events 提示不干扰
            self.assertEqual(st["mode"], "auto")
            self.assertEqual(st["status_stats"]["context_used"], 25000)

    def test_after_background_and_done(self):
        st = tui_parse.parse_screen(FR["after_background"]["plain"])
        self.assertEqual(st["hints"], {"background": False, "send_now": False, "queued": [], "shells": 1})
        self.assertTrue(st["busy"])
        self.assertEqual(st["mode"], "auto")
        st = tui_parse.parse_screen(FR["done_with_shell"]["plain"])
        self.assertFalse(st["busy"])
        self.assertEqual(st["hints"]["shells"], 1)
        self.assertEqual(st["turn_done"], "Crunched for 13s · done 12:13 AM · 1 shell still running")
        self.assertEqual(st["turn_done_s"], 13.0)

    def test_variants(self):
        st = tui_parse.parse_screen(FR["bg_once_two_queued"]["plain"])
        self.assertTrue(st["hints"]["background"])          # "(ctrl+b to run in background)"
        self.assertTrue(st["hints"]["send_now"])
        self.assertEqual(st["hints"]["queued"], ["第一条排队", "第二条排队\n第二行"])
        self.assertEqual(st["echo"], "跑一下测试")             # 真正的回显仍然识别
        self.assertEqual(st["input"]["text"], "")
        self.assertNotIn("排队", st["live"] or "")

    def test_three_queued_shared(self):
        """真机版式：多条排队消息是连续的 ❯ 行，后面只有一行共用的 Queued。"""
        fr = FR["three_queued_shared"]
        self.assertEqual(fr["source"], "real-excerpt")
        st = tui_parse.parse_screen(fr["plain"])
        self.assertEqual(st["hints"]["queued"], ["只回复：Q1-OK", "只回复：Q2-OK", "只回复：Q3-OK"])
        self.assertTrue(st["hints"]["send_now"])
        self.assertFalse(st["hints"]["background"])
        self.assertEqual(st["echo"], "写一篇1500字的中文科幻短篇小说，不要使用任何工具")
        self.assertEqual(st["input"]["text"], "")
        live = st["live"] or ""
        self.assertIn("迟到的家书", live)
        self.assertIn("第二段正文", live)   # 助手正文（两空格缩进）不被当成排队消息的续行
        self.assertNotIn("Q1-OK", live)
        self.assertNotIn("Queued", live)
        self.assertTrue(st["busy"])
        self.assertEqual(st["mode"], "manual")

    def test_queued_shared_variants(self):
        """共用一行 Queued 的多行消息 / 消息之间夹空行 / 上方紧挨着回复正文或工具输出。"""
        def parse(*body):
            return tui_parse.parse_screen("\n".join(list(body) + [RULE, "❯ " + tui_parse.QUEUED_PLACEHOLDER, RULE,
                                                                   "  ⏵⏵ auto mode on · esc to interrupt"]))
        st = parse("❯ 真正发出的", "", "● 回复第一行", "  回复续行",
                   "❯ 多行排队", "  第二行", "  第三行", "", "❯ 单行排队", "  ⎿  Queued · ctrl+x ctrl+s to send now",
                   "✶ Crafting… (3s)")
        self.assertEqual(st["hints"]["queued"], ["多行排队\n第二行\n第三行", "单行排队"])
        self.assertEqual(st["echo"], "真正发出的")
        self.assertIn("回复续行", st["live"])
        self.assertNotIn("第二行", st["live"])
        # 上方是工具输出（⎿）：停在那里
        st = parse("● Bash(ls)", "  ⎿  a.txt", "❯ Q1", "❯ Q2", "  ⎿  Queued · ctrl+x ctrl+s to send now")
        self.assertEqual(st["hints"]["queued"], ["Q1", "Q2"])
        # 上方是 spinner / 顶格文字：停
        st = parse("✶ Crafting…", "❯ Q1", "  ⎿  Queued · ctrl+x ctrl+s to send now")
        self.assertEqual(st["hints"]["queued"], ["Q1"])
        # 没有 Queued 行：一条都不算
        st = parse("❯ A", "❯ B")
        self.assertEqual(st["hints"]["queued"], [])

    def test_prose_is_not_hint(self):
        st = tui_parse.parse_screen(FR["prose_not_hint"]["plain"])
        self.assertEqual(st["hints"], {"background": False, "send_now": False, "queued": [], "shells": None})
        # 与占位文字不完全相同 = 用户输入
        self.assertEqual(st["input"]["text"], "Press up to edit queued messages please")
        self.assertNotIn("placeholder", st["input"])

    def test_hint_line_patterns(self):
        ok_bg = ["  (ctrl+b ctrl+b (twice) to run in background)", "(ctrl+b to run in background)",
                 "  ctrl+b to run in background", "  (Ctrl+B Ctrl+B (twice) to run in background)"]
        bad_bg = ["● 按 (ctrl+b ctrl+b (twice) to run in background) 即可", "ctrl+b ctrl+b ctrl+b to run in background",
                  "  ctrl+x to run in background", ""]
        for l in ok_bg:
            self.assertTrue(tui_parse.HINT_BACKGROUND_RE.match(l), l)
            self.assertTrue(node_agent.GUARD_HINTS["background"].match(l), l)
        for l in bad_bg:
            self.assertFalse(tui_parse.HINT_BACKGROUND_RE.match(l), l)
            self.assertFalse(node_agent.GUARD_HINTS["background"].match(l), l)
        ok_sn = ["  ⎿  Queued · ctrl+x ctrl+s to send now", "ctrl+x ctrl+s to send now", "  ⎿ Queued · ctrl+x ctrl+s to send now"]
        bad_sn = ["● 用 ctrl+x ctrl+s to send now 发送", "  ⎿  Queued", "ctrl+s ctrl+x to send now"]
        for l in ok_sn:
            self.assertTrue(tui_parse.HINT_SEND_NOW_RE.match(l), l)
            self.assertTrue(node_agent.GUARD_HINTS["send_now"].match(l), l)
        for l in bad_sn:
            self.assertFalse(tui_parse.HINT_SEND_NOW_RE.match(l), l)
            self.assertFalse(node_agent.GUARD_HINTS["send_now"].match(l), l)

    def test_hints_default_and_dialog(self):
        st = tui_parse.parse_screen("")
        self.assertEqual(st["hints"], {"background": False, "send_now": False, "queued": [], "shells": None})
        # 对话框区域里的同样文字不算（对话框出现时这些动作都不可用）
        text = "\n".join(["❯ hi", "", RULE, " Bash command", "", "  (ctrl+b to run in background)", "",
                          " Do you want to proceed?", " ❯ 1. Yes", "   2. No", "", " Esc to cancel"])
        st = tui_parse.parse_screen(text)
        self.assertIsNotNone(st["dialog"])
        self.assertFalse(st["hints"]["background"])


# ============================================================================ 白名单 / guard
class TestWhitelistAndGuard(unittest.TestCase):
    def test_three_whitelists_in_sync(self):
        new = {"C-b", "C-x", "C-s"}
        self.assertTrue(new <= set(node_agent.KEYS))
        self.assertTrue(new <= set(S.KEYS_WHITELIST))
        self.assertTrue(new <= set(S_srv.KEYS_WHITELIST))
        self.assertEqual(set(node_agent.KEYS), set(S.KEYS_WHITELIST))
        self.assertEqual(set(S_srv.KEYS_WHITELIST), set(S.KEYS_WHITELIST))
        self.assertNotIn("C-z", node_agent.KEYS)

    def test_guard_hint_whitelist(self):
        node_agent.validate_guard({"hint": "background"})
        node_agent.validate_guard({"hint": "send_now", "input_box": True})
        for bad in ("x", "", None, 1, ".*", "ctrl\\+b", ["background"]):
            with self.assertRaises(node_agent.OpError, msg=repr(bad)):
                node_agent.validate_guard({"hint": bad})

    def test_check_guard_hint(self):
        text = FR["bg_queued_wide"]["plain"]
        self.assertIsNone(node_agent.check_guard(text, {"input_box": True, "hint": "background"}))
        self.assertIsNone(node_agent.check_guard(text, {"input_box": True, "hint": "send_now"}))
        after = FR["after_background"]["plain"]
        self.assertIn("没有对应的提示", node_agent.check_guard(after, {"input_box": True, "hint": "background"}))
        self.assertIn("没有对应的提示", node_agent.check_guard(after, {"hint": "send_now"}))
        prose = FR["prose_not_hint"]["plain"]
        self.assertIsNotNone(node_agent.check_guard(prose, {"hint": "background"}))
        # 对话框盖住输入框：不发
        dlg = "\n".join(["  (ctrl+b to run in background)", RULE, " Bash command", " Do you want to proceed?",
                         " ❯ 1. Yes", "   2. No", " Esc to cancel"])
        self.assertIn("输入框不可见", node_agent.check_guard(dlg, {"hint": "background"}))


# ============================================================================ SessionManager 动作
class HintSim(SimTUI):
    """SimTUI + 后台提示 / 排队消息（按真机版式），C-b / C-x C-s 的效果与实测一致。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.bg = False       # 屏幕上有 ctrl+b 提示
        self.queued = []
        self.shells = 0
        self.cx = False

    def render(self):
        with self.lock:
            if self.view != "idle" or not (self.bg or self.queued or self.shells):
                return super().render()
            body = self._body(12)
            extra = []
            if self.bg:
                extra += ["  Running loop · 3s", "  ⎿  $ python3 loop.py", "  (ctrl+b ctrl+b (twice) to run in background)"]
            for q in self.queued:
                extra += ["❯ " + q, "  ⎿  Queued · ctrl+x ctrl+s to send now"]
            if self.busy:
                extra.append("✻ Jitterbugging… (8s · ↓ 124 tokens)")
            foot = self._footer()
            if self.queued and not self.input:
                foot[1] = "❯ " + tui_parse.QUEUED_PLACEHOLDER
            if self.shells:
                foot[-1] = foot[-1] + " · %d shell" % self.shells
            return "\n".join(body + extra + foot)

    def key(self, k):
        with self.lock:
            if k == "C-b":
                if self.bg:
                    self.bg = False
                    self.shells += 1
                return
            if k == "C-x":
                self.cx = True
                return
            if k == "C-s":
                if self.cx and self.queued:
                    self.queued = []
                    self.busy = True
                self.cx = False
                return
        super().key(k)


class TestSessionActions(SessionBase):
    def setUp(self):
        super().setUp()
        import tests.test_session as TS
        self._orig = TS.SimTUI
        TS.SimTUI = HintSim   # StubLink 用模块里的 SimTUI 新建终端
        self.addCleanup(setattr, TS, "SimTUI", self._orig)

    def test_background(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        with self.assertRaises(S.OpError) as cm:
            m.run_in_background(sid)
        self.assertEqual(cm.exception.api_code, "no_background_hint")
        self.assertEqual(self.rem.keys_sent(sid), [])
        t.busy, t.bg = True, True
        r = m.run_in_background(sid)
        self.assertTrue(r["ok"])
        ks = [p for op, p in self.rem.log if op == "keys"]
        self.assertEqual(ks[-1]["keys"], ["C-b"])   # 一次 C-b（send-keys 不经过 tmux 前缀键）
        self.assertEqual(ks[-1]["guard"], {"input_box": True, "hint": "background"})
        self.assertEqual(t.shells, 1)
        self.assertEqual(m.get_screen(sid)["state"]["hints"]["shells"], 1)

    def test_background_guard_race(self):
        """核对时有提示，但 agent 发键前重抓屏时提示已消失（命令刚好结束）：一个键都不按，409 screen_changed。"""
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        t.busy, t.bg = True, True

        def gone(tt, p):
            tt.bg = False
        self.rem.before_guard = gone
        with self.assertRaises(S.OpError) as cm:
            m.run_in_background(sid)
        self.assertEqual(cm.exception.api_code, "screen_changed")
        self.assertEqual(t.shells, 0)

    def test_send_now(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        with self.assertRaises(S.OpError) as cm:
            m.send_now(sid)
        self.assertEqual(cm.exception.api_code, "no_send_now_hint")
        t.busy, t.queued = True, ["只回复：Q3-OK"]
        calls = []

        class Rec:
            def __getattr__(self, name):
                return lambda *a, **k: calls.append(name)
        m.notifier = Rec()
        r = m.send_now(sid)
        self.assertTrue(r["ok"])
        # 不压制完成提示音：被打断的一轮直接进入排队消息的新一轮（中间没有 idle），压制会吞掉新一轮的完成
        self.assertNotIn("suppress_done", calls)
        ks = [p for op, p in self.rem.log if op == "keys"]
        self.assertEqual(ks[-1]["keys"], ["C-x", "C-s"])
        self.assertEqual(ks[-1]["guard"], {"input_box": True, "hint": "send_now"})
        self.assertEqual(t.queued, [])

    def test_dialog_blocks(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        t.bg = True
        t.view = "perm"
        n = len(self.rem.keys_sent(sid))
        for fn in (m.run_in_background, m.send_now):
            with self.assertRaises(S.OpError) as cm:
                fn(sid)
            self.assertEqual(cm.exception.api_code, "dialog_open")
        self.assertEqual(len(self.rem.keys_sent(sid)), n)

    def test_placeholder_not_input(self):
        """排队时输入框里的占位文字不会被当成"输入框有残留"：照常可以继续发消息（进入队列）。"""
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        t.busy, t.queued = True, ["第一条"]
        st = m._fresh(m._get(sid))
        self.assertEqual(st["input"]["text"], "")
        m._ensure_input_empty(m._get(sid), st, False)   # 不抛 input_not_empty


# ============================================================================ server
class TestServerRoutes(ServerBase):
    def test_routes_and_whitelist(self):
        sid = self.mgr.create(jobid="276403", cwd="/x")["sid"]
        calls = []
        self.mgr.run_in_background = lambda s: calls.append(("bg", s)) or {"ok": True}
        self.mgr.send_now = lambda s: calls.append(("now", s)) or {"ok": True}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/background" % sid, {})
        self.assertEqual(st, 200, js)
        st, js, _, _ = self.req("POST", "/api/sessions/%s/send_now" % sid, {})
        self.assertEqual(st, 200, js)
        self.assertEqual(calls, [("bg", sid), ("now", sid)])

        def boom(s):
            raise S.OpError("屏幕上没有提示", code="no_background_hint")
        self.mgr.run_in_background = boom
        st, js, _, _ = self.req("POST", "/api/sessions/%s/background" % sid, {})
        self.assertEqual((st, js.get("code")), (409, "no_background_hint"))
        st, js, _, _ = self.req("GET", "/api/bootstrap")
        self.assertTrue({"C-b", "C-x", "C-s"} <= set(js["keys"]))


# ============================================================================ fake_tui
class TestFakeScenarios(FakeBase):
    def step(self, a, n=1):
        for _ in range(n):
            a.turn_due = 0
            a.step_turn()

    def test_bg_and_queue(self):
        a = self.app()
        self.submit(a, "/bg 跑一下")
        self.step(a, 4)
        st = tui_parse.parse_screen("\n".join(a.frame()))
        self.assertTrue(st["hints"]["background"], "\n".join(a.frame()))
        self.assertTrue(st["busy"])
        # 忙时再发一条 → 排队
        self.submit(a, "只回复：Q3-OK")
        st = tui_parse.parse_screen("\n".join(a.frame()))
        self.assertEqual(st["hints"]["queued"], ["只回复：Q3-OK"])
        self.assertTrue(st["hints"]["send_now"])
        self.assertEqual(st["input"]["text"], "")
        self.assertEqual(st["input"].get("placeholder"), tui_parse.QUEUED_PLACEHOLDER)
        self.submit(a, "第二条排队")   # 多条排队：连续的 ❯ 块 + 一行共用的 Queued（真机版式）
        fr = "\n".join(a.frame())
        self.assertEqual(fr.count("Queued · ctrl+x ctrl+s to send now"), 1, fr)
        self.assertEqual(tui_parse.parse_screen(fr)["hints"]["queued"], ["只回复：Q3-OK", "第二条排队"])
        # Ctrl+B → 转入后台
        a.feed(b"\x02")
        self.step(a, 2)
        st = tui_parse.parse_screen("\n".join(a.frame()))
        self.assertFalse(st["hints"]["background"])
        self.assertEqual(st["hints"]["shells"], 1)
        self.assertIn("Ran 1 shell command", "\n".join(a.frame()))
        # Ctrl+X Ctrl+S → 中断当前生成，立即发送排队的消息
        a.feed(b"\x18\x13")
        st = tui_parse.parse_screen("\n".join(a.frame()))
        self.assertEqual(st["hints"]["queued"], [])
        self.assertEqual(st["echo"], "只回复：Q3-OK\n第二条排队")
        self.run_turn(a)
        st = tui_parse.parse_screen("\n".join(a.frame()))
        self.assertFalse(st["busy"])
        self.assertIn("1 shell still running", st["turn_done"] or "")

    def test_ctrl_keys_ignored_without_hint(self):
        a = self.app()
        a.on_text("abc")
        a.feed(b"\x02\x18\x13")   # 没有提示时：不影响输入、不报错
        self.assertEqual(a.input, "abc")
        self.assertIsNone(a.turn)
        a.feed(b"\x18")
        time.sleep(0.01)
        a.cx_at -= 5              # 和弦超时
        a.feed(b"\x13")
        self.assertIsNone(a.turn)


if __name__ == "__main__":
    unittest.main()
