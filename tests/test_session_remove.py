"""删除会话后不"复活"：后端删除时推 app/removed、之后不再为该 sid 发布任何事件（迟到的 agent 事件、定时器、
直接 _emit_meta）、回放里没有它、停掉监视；新建会话的第一条 meta 带 added；重连回放（gseq 不连续）不漏事件；
同名 / 同一 claude_session_id 的两个会话按 sid 互不串事件。前端部分见 static/dev/fixture.html 第 14f 节。"""
import unittest

from tests.test_session import Base, wait_until

UUID_X = "11111111-2222-3333-4444-555555555555"


def drain(sub, t=0.3):
    out = []
    while True:
        e = sub.get(timeout=t)
        if e is None:
            return out
        out.append(e)


class TestRemoveNoResurrect(Base):
    def test_removed_event_then_silence(self):
        m = self.mgr()
        a = self.start(m, title="这是我导师想的一个idea")
        b = self.start(m, title="这是我导师想的一个idea")
        self.assertNotEqual(a, b)
        old = m._sessions[a]
        sub = m.subscribe()
        self.assertEqual(m.remove(a), {"ok": True})
        evs = drain(sub)
        mine = [e for e in evs if e["sid"] == a]
        self.assertTrue(mine, evs)
        self.assertEqual((mine[-1]["kind"], mine[-1]["data"]), ("app", {"type": "removed"}))
        # 删除之后：迟到的 agent 事件、定时器 / 其它线程的 _emit_meta、子代理事件……都不再发布
        n0 = m.gseq
        jobid = old.jobid
        m._on_link_event(jobid, {"ev": "transcript", "sid": a, "session_id": UUID_X, "reset": True, "entries": [
            {"type": "user", "uuid": "late", "message": {"role": "user", "content": "迟到"}}]})
        m._on_link_event(jobid, {"ev": "subagent", "sid": a, "session_id": UUID_X, "agent_id": "ag1",
                                 "meta": {"description": "x"}, "entries": [], "reset": True})
        m._on_link_event(jobid, {"ev": "exited", "sid": a, "exit_status": 0})
        m._emit_meta(old, force=True)
        m._notice(old, "info", "迟到的提示")
        m._publish(old, "subagent", {"type": "subagent", "agent_id": "ag1", "summary": {"status": "killed"}})
        self.assertEqual([e for e in drain(sub) if e["sid"] == a], [])
        self.assertEqual(m.gseq, n0)
        self.assertEqual([e for e in m.events_since(0) if e["sid"] == a], [])
        with self.assertRaises(KeyError):
            m.session_events(a)
        self.assertEqual([x["sid"] for x in m.list_sessions()], [b])
        # 已退出的会话（不会 kill）删除时也停掉监视
        self.assertIn(("watch", {"sid": a, "on": False}), [(op, p) for op, p in self.rem.log if op == "watch"])
        # 同名的另一个会话照常
        m._on_link_event(jobid, {"ev": "transcript", "sid": b, "session_id": UUID_X, "reset": True, "entries": [
            {"type": "user", "uuid": "ub", "message": {"role": "user", "content": "B"}}]})
        self.assertTrue(any(e["sid"] == b and e["kind"] == "transcript" for e in drain(sub)))
        sub.close()

    def test_remove_exited_session(self):
        m = self.mgr()
        a = self.start(m)
        t = self.tui(a)
        t.dead, t.exit_status = True, 0
        m._on_link_event("12345", {"ev": "exited", "sid": a, "exit_status": 0})
        self.assertTrue(wait_until(lambda: m.get_meta(a)["state"] == "exited", 5))
        sub = m.subscribe()
        m.remove(a)
        evs = [e for e in drain(sub) if e["sid"] == a]
        self.assertEqual([e["data"]["type"] for e in evs if e["kind"] == "app"][-1:], ["removed"])
        sub.close()

    def test_added_only_on_create(self):
        m = self.mgr()
        sub = m.subscribe()
        a = self.start(m)
        metas = [e for e in drain(sub) if e["sid"] == a and e["kind"] == "app" and e["data"]["type"] == "meta"]
        self.assertTrue(metas[0]["data"].get("added"))
        self.assertTrue(len(metas) >= 2 and not any(e["data"].get("added") for e in metas[1:]))
        sub.close()

    def test_replay_with_gseq_gaps(self):
        """screen / subagent 事件不进全局缓冲（gseq 有空洞）：从任意 gseq 回放都不能漏掉之后的 transcript / meta。"""
        m = self.mgr()
        a = self.start(m)
        s = m._sessions[a]
        g0 = m.gseq
        m._publish(s, "screen", {"type": "screen", "state": {}, "text": "1"})
        m._publish(s, "app", {"type": "notice", "level": "info", "text": "n1"})
        m._publish(s, "subagent", {"type": "subagent", "agent_id": "ag1", "summary": {"status": "running"}})
        m._publish(s, "screen", {"type": "screen", "state": {}, "text": "2"})
        m._publish(s, "app", {"type": "notice", "level": "info", "text": "n2"})
        for since in range(g0, m.gseq):
            texts = [e["data"].get("text") for e in m.events_since(since) if e["kind"] == "app"]
            want = [t for g, t in ((g0 + 2, "n1"), (g0 + 5, "n2")) if g > since]
            self.assertEqual(texts, want, since)
        evs, gap = m.replay_since(g0 + 1)
        self.assertFalse(gap)
        self.assertEqual([e["data"].get("text") for e in evs if e["kind"] == "app"], ["n1", "n2"])

    def test_same_claude_session_id_kept_apart(self):
        """两个会话 resume 过同一个 claude 会话（先后）：事件只按 sid 路由，不按 claude_session_id / 标题合并。"""
        m = self.mgr()
        a = self.start(m, title="同名")
        b = self.start(m, title="同名")
        sub = m.subscribe()
        for sid, text in ((a, "给 A"), (b, "给 B")):
            m._on_link_event("12345", {"ev": "transcript", "sid": sid, "session_id": UUID_X, "reset": True, "entries": [
                {"type": "user", "uuid": "u-" + sid, "message": {"role": "user", "content": text}}]})
        tr = [(e["sid"], e["data"]["entries"][0]["message"]["content"]) for e in drain(sub) if e["kind"] == "transcript"]
        self.assertEqual(sorted(tr), sorted([(a, "给 A"), (b, "给 B")]))
        self.assertEqual(m.get_meta(a)["claude_session_id"], UUID_X)
        self.assertEqual(m.get_meta(b)["claude_session_id"], UUID_X)
        m.remove(a)
        self.assertEqual(m.get_meta(b)["claude_session_id"], UUID_X)
        self.assertTrue(any(e["kind"] == "transcript" for e in m.session_events(b)))
        sub.close()


if __name__ == "__main__":
    unittest.main()
