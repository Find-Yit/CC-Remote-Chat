"""/effort、/model 的结果以 CLI 回报为准（CLI 2.1.332：effort 被 CLAUDE_CODE_EFFORT_LEVEL 锁定、模型名后缀）。

用 tests.test_session 的桩（StubLink + SimTUI），给 SimTUI 加上 2.1.332 的锁定文案：
  /effort 调档后按 s → "CLAUDE_CODE_EFFORT_LEVEL=max overrides this session — clear it and xhigh takes over"
  /model 改 effort 后按 s → "Set model to X for this session only · effort stays max (set by …); xhigh isn't saved"
"""

import json
import pathlib

from ccr import session as S
from tests.test_session import Base, wait_until

VAR = "CLAUDE_CODE_EFFORT_LEVEL"


def lock_tui(t, env="max"):
    """模拟以 CLAUDE_CODE_EFFORT_LEVEL=<env> 启动的 claude（只改假终端的回报文案，按键语义不变）。"""
    orig = t.key

    def key(k):
        with t.lock:
            v = t.view
            if (v == "effort" and k in ("s", "Enter") and not t.ignore_confirm
                    and not (k == "s" and t.panel_effort == "max")):
                if k == "Enter" and t.panel_effort != "max":
                    t.saved.append("effort:" + t.panel_effort)
                wanted = t.panel_effort
                t.view = "idle"
                t.effort = env
                t._say("❯ /effort", "  ⎿  %s=%s overrides this session — clear it and %s takes over" % (VAR, env, wanted))
                return
            if v == "model" and k == "s" and t.panel_effort != env:
                wanted = t.panel_effort
                t.current = t.sel
                t.view = "idle"
                t.effort = env
                t._say("❯ /model", "  ⎿  Set model to %s for this session only · effort stays %s (set by %s); %s isn't "
                                   "saved" % (t.MODELS[t.sel][0], env, VAR, wanted))
                return
        orig(k)

    with t.lock:
        t.effort = t.panel_effort = env
    t.key = key


def result_tui(t, view, text):
    """面板按 s 后的结果行换成指定文字（None = 面板关闭但不打印结果）。"""
    orig = t.key

    def key(k):
        with t.lock:
            if t.view == view and k == "s":
                if view == "model":
                    t.current = t.sel
                t.view = "idle"
                t._say(*(["❯ /" + view] + (["  ⎿  " + text] if text else [])))
                return
        orig(k)
    t.key = key


class TestEffortEnvLocked(Base):
    def test_apply_effort_locked(self):
        m = self.mgr()
        sid = self.start(m)
        lock_tui(self.tui(sid))
        with self.assertRaises(S.OpError) as cm:
            m.apply_effort(sid, "xhigh", "session")
        self.assertEqual(cm.exception.api_code, "effort_env_locked")
        msg = str(cm.exception)
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL=max", msg)
        self.assertIn("本会话实际仍是 max", msg)
        self.assertIn("重启会话", msg)
        meta = m.get_meta(sid)
        self.assertEqual(meta["effort"], "max")                      # 不再误写成 xhigh
        self.assertEqual(meta["effort_locked"], {"level": "max", "source": VAR})
        keys = self.rem.keys_sent(sid)
        self.assertEqual(keys[-1], "s")
        self.assertNotIn("Enter", keys)                               # 只按 s（仅本会话），没有保存为默认
        self.assertEqual(self.tui(sid).saved, [])

    def test_apply_effort_locked_same_level(self):
        m = self.mgr()
        sid = self.start(m)
        lock_tui(self.tui(sid))
        r = m.apply_effort(sid, "max", "session")      # max 档只有 "Enter to use this session only"
        self.assertTrue(r["ok"])
        self.assertEqual(r["code"], "effort_env_locked")
        self.assertIn("锁定", r["warning"])
        self.assertEqual(m.get_meta(sid)["effort"], "max")
        self.assertEqual(self.tui(sid).saved, [])

    def test_apply_model_locked(self):
        m = self.mgr()
        sid = self.start(m)
        lock_tui(self.tui(sid))
        m.open_model_picker(sid)
        r = m.apply_model(sid, 1, effort="xhigh", scope="session")
        self.assertTrue(r["ok"])
        self.assertEqual(r["code"], "effort_env_locked")
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL=max", r["warning"])
        meta = m.get_meta(sid)
        self.assertEqual(meta["effort"], "max")
        self.assertEqual(meta["effort_locked"]["level"], "max")
        # 模型名只取名字本身，不把 " for this session only · effort stays …" 吞进来
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["model"] in ("Opus 5.5",)), m.get_meta(sid)["model"])
        self.assertEqual(self.tui(sid).saved, [])

    def test_restart_clears_lock(self):
        m = self.mgr()
        sid = self.start(m)
        lock_tui(self.tui(sid))
        with self.assertRaises(S.OpError):
            m.apply_effort(sid, "low", "session")
        self.assertIsNotNone(m.get_meta(sid)["effort_locked"])
        m.restart(sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertIsNone(m.get_meta(sid)["effort_locked"])


class TestResultUnknown(Base):
    def test_effort_unknown_result_not_written(self):
        m = self.mgr()
        m.result_settle = 0.3
        sid = self.start(m)
        result_tui(self.tui(sid), "effort", None)
        r = m.apply_effort(sid, "high", "session")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["result"])
        self.assertEqual((r["code"], r["warning"]), ("result_unknown", S.EFFORT_RESULT_UNKNOWN))
        self.assertEqual(m.get_meta(sid)["effort"], "low")   # 结果未知：不把 s.effort 写成 high

    def test_effort_unrecognized_text(self):
        m = self.mgr()
        sid = self.start(m)
        result_tui(self.tui(sid), "effort", "Effort tweaks are queued for the next turn")
        r = m.apply_effort(sid, "high", "session")
        self.assertEqual(r["result"], "Effort tweaks are queued for the next turn")   # 原文照给，前端展示
        self.assertEqual(r["code"], "result_unknown")
        self.assertEqual(m.get_meta(sid)["effort"], "low")

    def test_model_result_suffixes(self):
        m = self.mgr()
        sid = self.start(m)
        result_tui(self.tui(sid), "model", "Set model to Haiku 5.5 for this session only with high effort")
        m.open_model_picker(sid)
        r = m.apply_model(sid, 4, scope="session")
        self.assertNotIn("code", r)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["model"] == "Haiku 5.5"), m.get_meta(sid)["model"])

    def test_model_unknown_result(self):
        m = self.mgr()
        m.result_settle = 0.3
        sid = self.start(m)
        result_tui(self.tui(sid), "model", None)
        m.open_model_picker(sid)
        r = m.apply_model(sid, 4, scope="session")
        self.assertEqual(r["code"], "result_unknown")


class TestPollutedModelCleaned(Base):
    def test_restore_cleans_model(self):
        m1 = self.mgr()
        sid = self.start(m1)
        m1.close(sid)
        m1.shutdown()
        p = pathlib.Path(self.tmp) / "sessions.json"
        data = json.loads(p.read_text())
        rec = data["sessions"][0]
        bad = "Haiku 5.5 for this session only · effort stays max (set by CLAUDE_CODE_EFFORT_LEVEL); low isn't saved"
        rec["meta"]["model"] = bad
        rec["meta"]["effort"] = "null"
        rec["seen_base"] = {"model": "Opus 5.5 for this session only", "effort": "max"}
        p.write_text(json.dumps(data))
        m2 = self.mgr()
        meta = m2.get_meta(sid)
        self.assertEqual(meta["model"], "Haiku 5.5")
        self.assertIsNone(meta["effort"])
        self.assertEqual(m2._sessions[sid].seen_base["model"], "Opus 5.5")


class TestAgentsFocus(Base):
    def test_cycle_mode_refused_when_agents_list_focused(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        with t.lock:
            t.MODES = ["enter to view · x to stop · ctrl+x ctrl+k to stop all agents"]
            t.mode = 0
        n = len(self.rem.keys_sent(sid))
        for fn in (lambda: m.cycle_mode(sid), lambda: m.set_mode(sid, "plan")):
            with self.assertRaises(S.OpError) as cm:
                fn()
            self.assertEqual(cm.exception.api_code, "agents_focus")
        self.assertEqual(self.rem.keys_sent(sid)[n:], [])
        self.assertTrue(m._sessions[sid].screen_state["agents_focus"])
        self.assertEqual(m.get_meta(sid)["mode"], "auto")   # 沿用之前看到的模式，不因焦点行被误判成 manual
