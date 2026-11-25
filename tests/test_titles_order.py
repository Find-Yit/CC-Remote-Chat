"""侧栏三件事：Claude Code 里的会话标题、手动排序、子代理在跑时算"在干活"（含提示音）。"""
import json
import pathlib
import unittest

from ccr import session as S
from ccr import titles
from ccr.node_agent import filter_entry
from tests.test_session import UUID1, Base, wait_until

UUID2 = "22222222-2222-4222-8222-222222222222"


def ev_transcript(m, sid, entries, reset=False, cid=UUID1):
    m._on_link_event(m.get_meta(sid)["jobid"], {"ev": "transcript", "sid": sid, "session_id": cid,
                                                "reset": reset, "entries": entries})


def user(text, **kw):
    return dict({"type": "user", "message": {"role": "user", "content": text}}, **kw)


class TestTitleTracker(unittest.TestCase):
    def test_priority(self):
        t = titles.TitleTracker()
        self.assertIsNone(t.title())
        t.absorb([user("<command-name>/model</command-name>")])
        self.assertEqual(t.title(), "/model")                       # 只有斜杠命令时兜底
        t.absorb([user("隐藏", isMeta=True), user("caveat", promptSource="system"),
                  user("帮我修复\n登录 bug"), user("第二条")])
        self.assertEqual(t.title(), "帮我修复 登录 bug")               # 首条真正的用户输入
        t.absorb([{"type": "summary", "summary": "摘要"}])
        self.assertEqual(t.title(), "摘要")
        t.absorb([{"type": "ai-title", "aiTitle": "修复登录"}])
        self.assertEqual(t.title(), "修复登录")
        t.absorb([{"type": "custom-title", "customTitle": "我的标题"}, {"type": "ai-title", "aiTitle": "新自动标题"}])
        self.assertEqual(t.title(), "我的标题")                       # /rename 优先
        t.absorb([user("另一个会话")], reset=True)
        self.assertEqual(t.title(), "另一个会话")
        t.absorb([user("子代理", isSidechain=True)], reset=True)
        self.assertIsNone(t.title())

    def test_node_agent_forwards_title_entries(self):
        for e in ({"type": "custom-title", "customTitle": "x", "sessionId": UUID1},
                  {"type": "ai-title", "aiTitle": "y", "sessionId": UUID1}):
            self.assertEqual(filter_entry(json.dumps(e).encode())["type"], e["type"])
        self.assertIsNone(filter_entry(json.dumps({"type": "attachment"}).encode()))

    def test_default_like_title(self):
        self.assertTrue(S._default_like_title("proj", "/home/u/proj/"))
        self.assertTrue(S._default_like_title("~", "~"))
        self.assertTrue(S._default_like_title("续接 1a2b3c4d", "~"))
        self.assertFalse(S._default_like_title("我起的名字", "/home/u/proj"))


# 真机（CLI v2.1.330）/rename 之后的空闲画面：输入框上边线右侧嵌着会话名
RENAMED_SCREEN = "\n".join([
    " ▐▛███▛█   Claude Code v2.1.330",
    "❯ /rename 端到端标题测试",
    "  ⎿  Session renamed to: 端到端标题测试",
    "",
    "─" * 120 + " 端到端标题测试 ─",
    "❯ ",
    "─" * 140,
    "  Haiku 5.5 | ⚡ low | 📊 ctx: 2% | 16:09",
    "  ⏵⏵ auto mode on (shift+tab to cycle)",
])


class TestRenamedInputBox(unittest.TestCase):
    def test_parse_renamed_screen_is_idle_input(self):
        from ccr import tui_parse
        from ccr import node_agent
        r = tui_parse.parse_screen(RENAMED_SCREEN)
        self.assertIsNone(r["dialog"])
        self.assertEqual(r["mode"], "auto")
        self.assertIsNotNone(node_agent.find_input_box(node_agent.screen_lines(RENAMED_SCREEN)))
        self.assertFalse(tui_parse._RULE_RE.match("─" * 30 + " 有─横线 ─"))
        self.assertFalse(tui_parse._RULE_RE.match("─" * 10))


class TestSessionTitle(Base):
    def test_cli_title_and_rename(self):
        m = self.mgr()
        sid = self.start(m, cwd="~/proj")
        meta = m.get_meta(sid)
        self.assertEqual((meta["display_title"], meta["title_user"], meta["cli_title"]), ("proj", False, None))
        ev_transcript(m, sid, [user("帮我看看训练脚本为什么报错"),
                               {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}]}}],
                      reset=True)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["display_title"] == "帮我看看训练脚本为什么报错", 3))
        ev_transcript(m, sid, [{"type": "custom-title", "customTitle": "训练报错排查", "sessionId": UUID1}])
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["display_title"] == "训练报错排查", 3))
        m.rename(sid, "我的名字")
        self.assertEqual(m.get_meta(sid)["display_title"], "我的名字")
        ev_transcript(m, sid, [{"type": "custom-title", "customTitle": "CLI 里又改了", "sessionId": UUID1}])
        self.assertEqual(m.get_meta(sid)["display_title"], "我的名字")       # 自己起的名字优先
        m.rename(sid, "")
        self.assertEqual(m.get_meta(sid)["display_title"], "CLI 里又改了")
        # 标题与排序随 sessions.json 保存
        m.flush()
        data = json.loads((pathlib.Path(self.tmp) / "sessions.json").read_text())
        rec = [r for r in data["sessions"] if r["meta"]["sid"] == sid][0]["meta"]
        self.assertEqual((rec["cli_title"], rec["title_user"]), ("CLI 里又改了", False))

    def test_title_given_at_create_is_user_title(self):
        m = self.mgr()
        sid = self.start(m, title="长任务")
        ev_transcript(m, sid, [user("第一条消息")], reset=True)
        self.assertEqual(m.get_meta(sid)["display_title"], "长任务")
        self.assertTrue(m.get_meta(sid)["title_user"])

    def test_legacy_record_inference(self):
        m = self.mgr()
        a = self.start(m, cwd="~/proj")
        b = self.start(m, cwd="~/other")
        m.shutdown()
        p = pathlib.Path(self.tmp) / "sessions.json"
        data = json.loads(p.read_text())
        for r in data["sessions"]:   # 模拟旧版本写的记录：没有 title_user / cli_title / order
            for k in ("title_user", "cli_title", "order", "display_title"):
                r["meta"].pop(k, None)
            if r["meta"]["sid"] == b:
                r["meta"]["title"] = "帮我看看训练脚本…"   # 旧版"从历史续接"预填的截断标题
        p.write_text(json.dumps(data))
        m2 = self.mgr()
        self.assertFalse(m2.get_meta(a)["title_user"])      # 目录名 = 自动标题
        self.assertIsNone(m2.get_meta(b)["title_user"])     # 待定：等 transcript
        self.assertEqual(m2.get_meta(b)["display_title"], "帮我看看训练脚本…")
        self.assertTrue(wait_until(lambda: m2.get_meta(b)["link"] == "ready", 5))
        ev_transcript(m2, b, [user("帮我看看训练脚本为什么报错")], reset=True)
        self.assertFalse(m2.get_meta(b)["title_user"])      # 是 CLI 标题的截断 → 自动
        self.assertEqual(m2.get_meta(b)["display_title"], "帮我看看训练脚本为什么报错")


class TestOrder(Base):
    def test_reorder_persists(self):
        m = self.mgr()
        a = self.start(m)
        b = self.start(m, cwd="/tmp")
        self.assertIsNone(m.get_meta(a)["order"])
        r = m.reorder([b, a, "s000000000000"])
        self.assertEqual(r["changed"], 2)
        self.assertEqual((m.get_meta(b)["order"], m.get_meta(a)["order"]), (0.0, 1.0))
        self.assertEqual(m.reorder([b, a])["changed"], 0)
        for bad in ("x", [1], None):
            with self.assertRaises(ValueError):
                m.reorder(bad)
        m.shutdown()
        m2 = self.mgr()
        self.assertEqual((m2.get_meta(b)["order"], m2.get_meta(a)["order"]), (0.0, 1.0))


class RecNotifier:
    def __init__(self):
        self.states = []

    def observe(self, sid, jobid, state, *a, **k):
        self.states.append(state)

    def __getattr__(self, name):   # reset / mute / suppress_done / user_acted / forget：忽略
        return lambda *a, **k: None


def link_settled(tc, m, jobid="12345"):
    """先把作业的 link 连好、等"连接就绪后的重新附着"（_reattach）跑完，再启动会话（条件等待，不靠固定 sleep）。
    否则高负载下 _reattach 线程可能在会话启动完成之后才轮到：它会对刚启动的会话再 watch 一次，桩 agent 随之推送
    transcript reset（清掉测试直接注入的子代理）并重置提示音基线——测试就在 working / unread 上偶发失败。"""
    m._ensure_link(jobid)
    lk = m._reattach_locks
    tc.assertTrue(wait_until(lambda: tc.rem.ops("list") and jobid in lk and not lk[jobid].locked(), 5))
    tc.assertTrue(m._links[jobid].sync())


class TestWorkingWithSubagents(Base):
    def test_running_subagent_counts_as_working(self):
        m = self.mgr()
        rec = RecNotifier()
        m.notifier = rec
        link_settled(self, m)
        sid = self.start(m)
        jobid = m.get_meta(sid)["jobid"]
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        self.assertFalse(m.get_meta(sid)["working"])
        aid = "a0123456789abcdef"
        base_meta = {"agentType": "general-purpose", "description": "后台任务", "requestShape": "background"}
        m._on_link_event(jobid, {"ev": "subagent", "sid": sid, "session_id": UUID1, "agent_id": aid,
                                 "meta": dict(base_meta), "entries": [], "reset": True})
        meta = m.get_meta(sid)
        self.assertEqual((meta["state"], meta["subagents"]["running"], meta["working"]), ("idle", 1, True))
        self.assertEqual(rec.states[-1], "busy")          # 提示音状态机：还在干活，不算"完成"
        m._on_link_event(jobid, {"ev": "subagent", "sid": sid, "session_id": UUID1, "agent_id": aid,
                                 "meta": dict(base_meta, finished=True), "entries": [], "reset": False})
        meta = m.get_meta(sid)
        self.assertEqual((meta["subagents"]["running"], meta["working"]), (0, False))
        self.assertEqual(rec.states[-1], "idle")          # 全部结束 → idle（busy→idle 才会响"完成"）


class TestUnreadWithSubagents(Base):
    def test_done_at_only_after_all_subagents_finish(self):
        """主会话回复完但子代理还在跑：不算完成（不标"新回复"）；全部结束才设 done_at。"""
        import time
        m = self.mgr()
        m.notifier.QUIET_AFTER_RESET = 0.0
        m.notifier.CONFIRM_DELAY = 0.05
        link_settled(self, m)
        sid = self.start(m)
        jobid = m.get_meta(sid)["jobid"]
        t = self.rem.tuis[sid]
        t.busy = True
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "busy", 3))
        aid = "a0123456789abcdef"
        base_meta = {"agentType": "general-purpose", "description": "后台任务", "requestShape": "background"}
        m._on_link_event(jobid, {"ev": "subagent", "sid": sid, "session_id": UUID1, "agent_id": aid,
                                 "meta": dict(base_meta), "entries": [], "reset": True})
        t.busy = False
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 3))
        time.sleep(0.3)
        meta = m.get_meta(sid)
        self.assertTrue(meta["working"])
        self.assertIsNone(meta["done_at"])
        m._on_link_event(jobid, {"ev": "subagent", "sid": sid, "session_id": UUID1, "agent_id": aid,
                                 "meta": dict(base_meta, finished=True), "entries": [], "reset": False})
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["unread"], 3))
        self.assertFalse(m.get_meta(sid)["working"])


if __name__ == "__main__":
    unittest.main()
