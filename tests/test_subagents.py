"""子代理（subagent）面板测试：node agent 监视（过滤 / 路径约束 / 不跟随软链 / reset / 上限 / 只读）、
ccr.subagents 摘要与状态关联、SessionManager 事件与回放、server API、tui_parse 对真实子代理画面、
fake_tui 的 /agent，以及 fake 端到端（会话里 /agent → SSE 收到子代理事件 → running → completed）。

数据来自真实 CLI v2.1.330 录制的 fixtures/subagent_sample/。
"""
import http.client
import json
import os
import pathlib
import re
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from ccr import fake_tui, history as H, node_agent as NA, subagents as SA, tui_parse
from tests import test_server as TS
from tests.test_session import Base as SessionBase, wait_until

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "subagent_sample"
MAIN_ID = "1415819f-dc53-4016-a48a-a7c6deda394d"
A1, A2 = "a43d26d57257e2ab7", "a42ae49c2c11712ed"
T1, T2 = "toolu_019NCgKQHpnesjpw1cCnSeFK", "toolu_01W4r763Vd7qUGnFf2B3h76w"
SID = "s0123456789ab"
OTHER_ID = "11111111-2222-3333-4444-555555555555"


def main_entries():
    out = []
    for line in (FIX / "main_1415819f.jsonl").read_bytes().split(b"\n"):
        if line.strip():
            e = NA.filter_entry(line)
            if e is not None:
                out.append(e)
    return out


def sub_entries(aid):
    out = []
    for line in (FIX / "subagents" / ("agent-%s.jsonl" % aid)).read_bytes().split(b"\n"):
        if line.strip():
            e = NA.filter_entry(line, NA.SUBAGENT_TYPES, sidechain=True, max_bytes=NA.MAX_SUB_ENTRY_BYTES)
            if e is not None:
                out.append(e)
    return out


def sub_meta(aid):
    return json.loads((FIX / "subagents" / ("agent-%s.meta.json" % aid)).read_text(encoding="utf-8"))


def note_entry(aid, tid, status="completed", result="ok", ts="2026-11-22T12:50:00.000Z", tools=0, dur=1000):
    text = ("<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>%s</status>\n"
            "<summary>Agent \"x\" finished</summary>\n<result>%s</result>\n<usage><subagent_tokens>10</subagent_tokens>"
            "<tool_uses>%d</tool_uses><duration_ms>%d</duration_ms></usage>\n</task-notification>"
            % (aid, tid, status, result, tools, dur))
    return {"type": "user", "uuid": "n-" + aid + status, "timestamp": ts, "promptSource": "system",
            "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": text}}


def tree_state(root):
    """目录树快照（路径 → (size, mtime_ns)），用于断言 node agent 什么都没写。"""
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns, stat.S_IFMT(st.st_mode))
    return out


# ============================================================================ node agent
class NodeAgentSubagentTests(unittest.TestCase):
    def setUp(self):
        self.td = os.path.realpath(tempfile.mkdtemp(prefix="ccrsa", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.td, True)
        self.proj = os.path.join(self.td, ".claude", "projects", "-x")
        os.makedirs(self.proj)
        p = mock.patch.dict(os.environ, {"HOME": self.td})
        p.start()
        self.addCleanup(p.stop)
        self.out = []
        p = mock.patch.object(NA, "emit", self.out.append)
        p.start()
        self.addCleanup(p.stop)
        self.w = NA.Watched(SID)
        self.watcher = NA.Watcher.__new__(NA.Watcher)

    def sdir(self, sess=MAIN_ID):
        d = os.path.join(self.proj, sess, "subagents")
        os.makedirs(d, exist_ok=True)
        return d

    def main(self, sess=MAIN_ID, data=b'{"type":"user","uuid":"m1","message":{"role":"user","content":"hi"}}\n'):
        with open(os.path.join(self.proj, sess + ".jsonl"), "ab") as f:
            f.write(data)

    def poll(self, sess=MAIN_ID, scan=True):
        self.out.clear()
        if scan:
            self.w.next_subscan = 0.0
        self.watcher.poll_transcript(self.w, sess, "/x")
        return [e for e in self.out if e["ev"] == "subagent"]

    def write(self, d, aid, entries, mode="ab", raw=b""):
        with open(os.path.join(d, "agent-%s.jsonl" % aid), mode) as f:
            f.write(b"".join(json.dumps(e).encode() + b"\n" for e in entries) + raw)

    def test_fixture_layout(self):
        shutil.copy(FIX / "main_1415819f.jsonl", os.path.join(self.proj, MAIN_ID + ".jsonl"))
        shutil.copytree(FIX / "subagents", self.sdir(), dirs_exist_ok=True)
        before = tree_state(self.td)
        evs = self.poll()
        self.assertEqual(sorted(e["agent_id"] for e in evs), sorted([A1, A2]))
        for e in evs:
            self.assertTrue(e["reset"])
            self.assertEqual((e["sid"], e["session_id"]), (SID, MAIN_ID))
            self.assertEqual([x["type"] for x in e["entries"]], ["user", "assistant"])
            self.assertTrue(all(x["isSidechain"] and x["agentId"] == e["agent_id"] for x in e["entries"]))
            self.assertTrue(e["meta"]["finished"])
            self.assertEqual(e["meta"]["agentType"], "general-purpose")
            self.assertLess(len(json.dumps(e)), 8000)   # 不含 attachment / api-request* / 系统提示
        self.assertEqual({e["agent_id"]: e["meta"]["toolUseId"] for e in evs}, {A1: T1, A2: T2})
        # 主 transcript 事件在子代理事件之前
        self.assertEqual(self.out[0]["ev"], "transcript")
        self.assertEqual(self.poll(), [])
        self.assertEqual(tree_state(self.td), before, "node agent 不应写任何文件")

    def test_filter_truncate_partial_and_batches(self):
        self.main()
        d = self.sdir()
        ents = [
            {"type": "user", "uuid": "u1", "isSidechain": True, "message": {"role": "user", "content": "任务"}},
            {"type": "attachment", "uuid": "x1", "attachment": {"type": "skill_listing", "content": "s" * 50000}},
            {"type": "api-request-shape", "shape": {"system": "x" * 1000}},
            {"type": "api-request-blob", "message": {"role": "system", "content": "y"}},
            {"type": "api-request", "id": "r"},
            {"type": "user", "uuid": "meta", "isMeta": True, "message": {"role": "user", "content": "caveat"}},
            {"type": "system", "uuid": "sys"},
            {"type": "assistant", "uuid": "a1", "isSidechain": True,
             "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                                           "input": {"file_path": "/a/b.py"}}]}},
            {"type": "user", "uuid": "r1", "isSidechain": True,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                      "content": "z" * 30000}]}},
            {"type": "user", "uuid": "big", "isSidechain": True,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2",
                                                      "content": [{"type": "text", "text": "w" * 15000}] * 40}]}},
        ]
        self.write(d, "agentA", ents, raw=b'{"type":"assistant","uuid":"half"')   # 最后一行不完整
        with open(os.path.join(d, "agent-agentA.meta.json"), "w") as f:
            json.dump({"agentType": "Explore", "description": "d", "finished": False, "nested": {"x": 1},
                       "long": "L" * 5000}, f)
        evs = self.poll()
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual([x["uuid"] for x in e["entries"]], ["u1", "a1", "r1", "big"])
        r1 = e["entries"][2]
        self.assertTrue(r1["truncated"])
        self.assertEqual(len(r1["message"]["content"][0]["content"]), NA.MAX_STR)
        big = e["entries"][3]
        self.assertTrue(big["truncated"])
        self.assertLessEqual(len(json.dumps(big, ensure_ascii=False)), NA.MAX_SUB_ENTRY_BYTES)
        self.assertEqual(e["meta"], {"agentType": "Explore", "description": "d", "finished": False,
                                     "long": "L" * NA.MAX_META_STR})   # 只转发标量字段
        self.assertEqual(self.poll(scan=False), [])
        with open(os.path.join(d, "agent-agentA.jsonl"), "ab") as f:
            f.write(b',"message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}\n')
        evs = self.poll(scan=False)
        self.assertEqual([(x["reset"], x["meta"], [y["uuid"] for y in x["entries"]]) for x in evs],
                         [(False, None, ["half"])])
        # 650 条 → 3 批，均非 reset；meta 未变化不重发
        self.write(d, "agentA", [{"type": "assistant", "uuid": "n%d" % i} for i in range(650)])
        evs = self.poll()
        self.assertEqual([len(x["entries"]) for x in evs], [300, 300, 50])
        self.assertEqual([x["reset"] for x in evs], [False] * 3)
        self.assertEqual([x["meta"] for x in evs], [None] * 3)
        # meta 变化（finished）→ 只发 meta
        with open(os.path.join(d, "agent-agentA.meta.json"), "w") as f:
            json.dump({"agentType": "Explore", "description": "d", "finished": True}, f)
        evs = self.poll()
        self.assertEqual([(x["meta"], x["entries"], x["reset"]) for x in evs],
                         [({"agentType": "Explore", "description": "d", "finished": True}, [], False)])
        # jsonl 被截断重写 → 该子代理 reset 并从头发送
        self.write(d, "agentA", [{"type": "user", "uuid": "again"}], mode="wb")
        evs = self.poll()
        self.assertEqual([(x["reset"], [y["uuid"] for y in x["entries"]]) for x in evs], [(True, ["again"])])

    def test_path_constraints_and_symlinks(self):
        self.main()
        d = self.sdir()
        secret = os.path.join(self.td, "secret.jsonl")
        with open(secret, "w") as f:
            f.write(json.dumps({"type": "user", "uuid": "SECRET", "message": {"content": "pw"}}) + "\n")
        good = [{"type": "user", "uuid": "g1"}]
        self.write(d, "ok_1-A", good)
        for bad in ("agent-a.b.jsonl", "agent-x.txt", "other.jsonl", "agent-%s.jsonl" % ("x" * 65),
                    "agent-.jsonl", "agent-x.jsonl.bak", "xagent-y.jsonl"):
            with open(os.path.join(d, bad), "w") as f:
                f.write(json.dumps({"type": "user", "uuid": "BAD-" + bad}) + "\n")
        os.symlink(secret, os.path.join(d, "agent-link.jsonl"))
        os.symlink(secret, os.path.join(d, "agent-link2.meta.json"))
        os.mkdir(os.path.join(d, "agent-dir.jsonl"))
        os.mkfifo(os.path.join(d, "agent-fifo.jsonl"))
        t0 = time.time()
        evs = self.poll()
        self.assertLess(time.time() - t0, 3, "FIFO 不应阻塞")
        self.assertEqual([(e["agent_id"], [x["uuid"] for x in e["entries"]]) for e in evs], [("ok_1-A", ["g1"])])
        self.assertNotIn("SECRET", json.dumps(self.out))
        self.assertNotIn("BAD-", json.dumps(self.out))

    def test_symlinked_dirs_not_followed(self):
        self.main()
        outside = os.path.join(self.td, "outside")
        os.makedirs(os.path.join(outside, "subagents"))
        with open(os.path.join(outside, "subagents", "agent-z.jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "uuid": "OUT"}) + "\n")
        os.symlink(outside, os.path.join(self.proj, MAIN_ID))          # <sessionId> 是软链接
        self.assertEqual(self.poll(), [])
        os.unlink(os.path.join(self.proj, MAIN_ID))
        os.makedirs(os.path.join(self.proj, MAIN_ID))
        os.symlink(os.path.join(outside, "subagents"), os.path.join(self.proj, MAIN_ID, "subagents"))  # subagents 是软链接
        self.assertEqual(self.poll(), [])
        self.assertNotIn("OUT", json.dumps(self.out))
        self.assertIsNone(NA.subagents_dir(os.path.join(self.proj, MAIN_ID + ".jsonl"), "../x"))

    def test_session_switch_resends_and_limit(self):
        self.main()
        d = self.sdir()
        now = time.time()
        for i in range(55):
            self.write(d, "ag%02d" % i, [{"type": "user", "uuid": "u%d" % i}])
            os.utime(os.path.join(d, "agent-ag%02d.jsonl" % i), (now - 100 + i, now - 100 + i))
        evs = self.poll()
        self.assertEqual(len(evs), NA.MAX_SUBAGENTS)
        self.assertEqual(sorted(e["agent_id"] for e in evs), ["ag%02d" % i for i in range(5, 55)])  # 最近的 50 个
        self.assertTrue(all(e["reset"] for e in evs))
        self.assertEqual(self.poll(), [])
        # /clear：换 sessionId → 新会话没有子代理；切回来（相当于重新 watch）→ 全部从头重发
        self.main(OTHER_ID)
        self.assertEqual(self.poll(OTHER_ID), [])
        evs = self.poll(MAIN_ID)
        self.assertEqual(len(evs), NA.MAX_SUBAGENTS)
        self.assertTrue(all(e["reset"] and len(e["entries"]) == 1 for e in evs))
        tr = [e for e in self.out if e["ev"] == "transcript"]
        self.assertTrue(tr and tr[0]["reset"])
        # 新 Watched（agent 重连 / 重新 watch）同样完整重发
        self.w = NA.Watched(SID)
        self.assertEqual(len(self.poll()), NA.MAX_SUBAGENTS)

    def test_finished_agents_only_checked_on_scan(self):
        self.main()
        d = self.sdir()
        self.write(d, "done1", [{"type": "user", "uuid": "a"}])
        with open(os.path.join(d, "agent-done1.meta.json"), "w") as f:
            json.dump({"finished": True}, f)
        self.assertEqual(len(self.poll()), 1)
        self.write(d, "done1", [{"type": "assistant", "uuid": "b"}])
        with mock.patch.object(NA, "read_nofollow", side_effect=AssertionError("不应读取")):
            self.assertEqual(self.poll(scan=False), [])
        evs = self.poll(scan=True)
        self.assertEqual([[x["uuid"] for x in e["entries"]] for e in evs], [["b"]])


# ============================================================================ ccr.subagents
class TrackerTests(unittest.TestCase):
    def fixture_tracker(self):
        t = SA.Tracker()
        changed = t.absorb_main(main_entries())
        self.assertEqual(changed, {A1, A2})
        for aid in (A1, A2):
            t.apply_event(aid, sub_meta(aid), sub_entries(aid), True)
        return t

    def test_fixture_summaries(self):
        t = self.fixture_tracker()
        s1, s2 = t.summary(A1), t.summary(A2)
        self.assertEqual((s1["status"], s1["description"], s1["agent_type"], s1["model"], s1["tool_use_id"]),
                         ("completed", "前台计算 1+1", "general-purpose", "haiku", T1))
        self.assertEqual((s1["result"], s1["duration_ms"], s1["tool_uses"], s1["entries"]), ("2", 2795, 0, 2))
        self.assertEqual(s1["usage"], {"subagent_tokens": 14335, "tool_uses": 0, "duration_ms": 2795})
        self.assertEqual(s1["prompt"], "不使用任何工具，直接回答“1+1 等于几”，只回复数字，不要添加其他文字。")
        self.assertEqual(s1["resolved_model"], "claude-haiku-5-5")
        self.assertEqual((s2["status"], s2["result"], s2["background"], s2["notifications"]),
                         ("completed", "2 + 3 等于几", True, 1))
        self.assertTrue(s1["started_at"] < s1["ended_at"])
        self.assertEqual(t.counts(), {"running": 0, "total": 2})
        d = t.detail(A1)
        self.assertEqual((d["from"], d["total"], len(d["entries"]), d["truncated"]), (0, 2, 2, False))

    def test_running_until_notification(self):
        t = SA.Tracker()
        ents = main_entries()
        cut = next(i for i, e in enumerate(ents) if SA.notification_text(e))
        t.absorb_main(ents[:cut])     # 调用 + 启动结果，还没有通知
        self.assertEqual(t.summary(A1)["status"], "running")
        self.assertEqual(t.summary(A1)["description"], "前台计算 1+1")
        self.assertFalse(t.summary(A1)["has_transcript"])
        meta = dict(sub_meta(A1), finished=False)
        t.apply_event(A1, meta, sub_entries(A1)[:1], True)
        s = t.summary(A1)
        self.assertEqual((s["status"], s["entries"], s["last_action"]), ("running", 1, None))
        t.apply_event(A1, None, sub_entries(A1)[1:], False)
        self.assertEqual(t.summary(A1)["last_action"], "回复：2")
        t.apply_event(A1, dict(meta, finished=True), [], False)
        self.assertEqual(t.summary(A1)["status"], "completed")    # 没有通知时 meta.finished 也算完成
        self.assertEqual(t.summary(A1)["result"], "2")

    def test_statuses_resume_and_killed(self):
        t = SA.Tracker()
        mk = lambda uid, ts, blocks: {"type": "assistant", "uuid": uid, "timestamp": ts,  # noqa: E731
                                      "message": {"content": blocks}}
        t.apply_event("ag1", {"toolUseId": "tu1", "description": "x", "finished": False},
                      [{"type": "user", "uuid": "p", "timestamp": "2026-11-22T12:00:00Z",
                        "message": {"content": "做事"}},
                       mk("a", "2026-11-22T12:00:01Z", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                         "input": {"command": "pytest -q\nmore"}}]),
                       mk("a2", "2026-11-22T12:00:02Z", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                          "input": {"command": "pytest -q"}}])], True)
        s = t.summary("ag1")
        self.assertEqual((s["tool_uses"], s["last_action"], s["prompt"]), (1, "Bash: pytest -q", "做事"))
        for raw, want in (("failed", "failed"), ("error", "failed"), ("killed", "killed"), ("stopped", "killed"),
                          ("completed", "completed")):
            t.absorb_main([note_entry("ag1", "tu1", raw, ts="2026-11-22T12:00:05Z")])
            self.assertEqual(t.summary("ag1")["status"], want, raw)
        self.assertEqual(t.summary("ag1")["notifications"], 5)
        # 通知之后又有新活动（SendMessage 续跑）→ running
        t.apply_event("ag1", None, [mk("a3", "2026-11-22T12:01:00Z", [{"type": "text", "text": "继续"}])], False)
        self.assertEqual(t.summary("ag1")["status"], "running")
        # 会话退出：仍在运行的 → killed；transcript reset 重建后没有新活动仍是 killed
        self.assertEqual(t.mark_exited(), ["ag1"])
        self.assertEqual(t.summary("ag1", exited=True)["status"], "killed")
        n = t.subs["ag1"].total
        t.clear()
        t.apply_event("ag1", {"finished": False}, [{"type": "user", "uuid": "u%d" % i} for i in range(n)], True)
        self.assertEqual(t.summary("ag1")["status"], "killed")
        t.apply_event("ag1", None, [{"type": "user", "uuid": "new"}], False)
        self.assertEqual(t.summary("ag1")["status"], "running")

    def test_sync_completion_and_cap(self):
        t = SA.Tracker()
        t.absorb_main([
            {"type": "assistant", "uuid": "a", "message": {"content": [
                {"type": "tool_use", "id": "tuS", "name": "Task", "input": {"description": "同步", "prompt": "p"}}]}},
            {"type": "user", "uuid": "b", "toolUseResult": {"status": "completed", "agentId": "agS",
                                                            "totalDurationMs": 1500, "totalToolUseCount": 4},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "tuS", "content": "结果文本"}]}},
        ])
        s = t.summary("agS")
        self.assertEqual((s["status"], s["result"], s["duration_ms"], s["tool_uses"], s["description"]),
                         ("completed", "结果文本", 1500, 4, "同步"))
        with mock.patch.object(SA, "MAX_ENTRIES", 5):
            sub, new, start, ev = t.apply_event("agS", None, [{"type": "user", "uuid": str(i)} for i in range(8)], False)
            self.assertEqual((sub.base, len(sub.entries), sub.total, start, [e["uuid"] for e in new]),
                             (3, 5, 8, 3, ["3", "4", "5", "6", "7"]))
            sub, new, start, ev = t.apply_event("agS", None, [{"type": "user", "uuid": "8"}], False)
            self.assertEqual((sub.base, start, sub.total), (4, 8, 9))
            self.assertTrue(t.detail("agS")["truncated"])
        # 同一 uuid 不重复计入
        sub, new, start, ev = t.apply_event("agS", None, [{"type": "user", "uuid": "8"}], False)
        self.assertEqual(new, [])

    def test_parse_notification_and_helpers(self):
        n = SA.parse_notification(note_entry("ag", "tu", "completed", "含 <status>failed</status> 的结果")["message"]["content"])
        self.assertEqual((n["status"], n["task_id"], n["tool_use_id"]), ("completed", "ag", "tu"))
        self.assertEqual(n["result"], "含 <status>failed</status> 的结果")
        self.assertIsNone(SA.parse_notification("hello"))
        self.assertEqual(SA.describe_action("Read", {"file_path": "/a/b/foo.py"}), "Read foo.py")
        self.assertEqual(SA.describe_action("Grep", {"pattern": "TODO"}), "Grep TODO")
        self.assertEqual(SA.describe_action("mcp__srv__tool", {"q": "x"}), "srv · tool x")
        self.assertIsNone(SA.iso_ts("bad"))
        self.assertAlmostEqual(SA.iso_ts("2026-11-22T12:48:52.425Z"), 1795351732.425, places=3)
        self.assertIsNone(SA.Tracker().apply_event("../x", None, [], True))


# ============================================================================ SessionManager
class SessionSubagentTests(SessionBase):
    def feed(self, m, sid, ev):
        m._on_link_event("12345", dict(ev, sid=sid))

    def test_events_summary_replay_and_api(self):
        m = self.mgr()
        sid = self.start(m)
        sub = m.subscribe()
        self.feed(m, sid, {"ev": "transcript", "session_id": MAIN_ID, "entries": main_entries(), "reset": True})
        for aid in (A1, A2):
            self.feed(m, sid, {"ev": "subagent", "session_id": MAIN_ID, "agent_id": aid, "meta": sub_meta(aid),
                               "entries": sub_entries(aid), "reset": True})
        live = []
        while True:
            e = sub.get(timeout=0.2)
            if e is None:
                break
            if e["kind"] == "subagent":
                live.append(e)
        sub.close()
        with_entries = [e for e in live if e["data"]["entries"]]
        self.assertEqual(sorted(e["data"]["agent_id"] for e in with_entries), sorted([A1, A2]))
        for e in with_entries:
            d = e["data"]
            self.assertEqual((d["type"], d["from"], d["total"], d["reset"]), ("subagent", 0, 2, True))
            self.assertEqual(d["summary"]["status"], "completed")
        # 回放：每个子代理只留最新一条摘要，且不带条目
        evs = [e for e in m.session_events(sid) if e["kind"] == "subagent"]
        self.assertEqual(sorted(e["data"]["agent_id"] for e in evs), sorted([A1, A2]))
        self.assertTrue(all(e["data"]["entries"] == [] and e["data"]["from"] == 2 for e in evs))
        self.assertEqual(len([e for e in m.events_since(0) if e["kind"] == "subagent"]), 2)
        gs = [e["gseq"] for e in m.events_since(0)]
        self.assertEqual(gs, sorted(gs))
        # API
        lst = m.list_subagents(sid)["subagents"]
        self.assertEqual({s["agent_id"]: s["result"] for s in lst}, {A1: "2", A2: "2 + 3 等于几"})
        det = m.get_subagent(sid, A1)
        self.assertEqual([e["type"] for e in det["entries"]], ["user", "assistant"])
        self.assertIsNone(m.get_subagent(sid, "nope"))
        self.assertIsNone(m.get_subagent(sid, "../x"))
        with self.assertRaises(KeyError):
            m.get_subagent("s999999999999", A1)
        self.assertEqual(m.get_meta(sid)["subagents"], {"running": 0, "total": 2})
        # 主 transcript reset（如 /clear）→ 子代理状态清空
        self.feed(m, sid, {"ev": "transcript", "session_id": OTHER_ID, "entries": [], "reset": True})
        self.assertEqual([e for e in m.session_events(sid) if e["kind"] == "subagent"], [])
        self.assertEqual(m.list_subagents(sid)["subagents"], [])
        self.assertEqual(m.get_meta(sid)["subagents"], {"running": 0, "total": 0})

    def test_running_then_notification_then_exit(self):
        m = self.mgr()
        sid = self.start(m)
        ents = main_entries()
        cut = next(i for i, e in enumerate(ents) if SA.notification_text(e))
        self.feed(m, sid, {"ev": "transcript", "session_id": MAIN_ID, "entries": ents[:cut], "reset": True})
        # 启动结果一到就有摘要（还没读到子代理文件）
        self.assertEqual(m.get_meta(sid)["subagents"], {"running": 1, "total": 1})
        self.feed(m, sid, {"ev": "subagent", "session_id": MAIN_ID, "agent_id": A1,
                           "meta": dict(sub_meta(A1), finished=False), "entries": sub_entries(A1)[:1], "reset": True})
        self.assertEqual(m.list_subagents(sid)["subagents"][0]["status"], "running")
        self.feed(m, sid, {"ev": "transcript", "session_id": MAIN_ID, "entries": ents[cut:cut + 1], "reset": False})
        s1 = next(s for s in m.list_subagents(sid)["subagents"] if s["agent_id"] == A1)
        self.assertEqual((s1["status"], s1["result"]), ("completed", "2"))
        # 另一个运行中的子代理；会话退出 → killed，并推送新摘要
        self.feed(m, sid, {"ev": "subagent", "session_id": MAIN_ID, "agent_id": "agX",
                           "meta": {"finished": False, "description": "跑着"}, "entries": [], "reset": True})
        self.assertEqual(m.get_meta(sid)["subagents"]["running"], 1)
        n0 = m.gseq
        t = self.tui(sid)
        t.dead, t.exit_status = True, 0          # pane 真的死了（exited 事件会先用 list 核实）
        self.feed(m, sid, {"ev": "exited", "exit_status": 0})
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "exited", 5))
        evs = [e for e in m.events_since(n0) if e["kind"] == "subagent"]
        self.assertEqual([(e["data"]["agent_id"], e["data"]["summary"]["status"]) for e in evs], [("agX", "killed")])
        self.assertEqual(m.get_meta(sid)["subagents"], {"running": 0, "total": 2})

    def test_bad_events_ignored(self):
        m = self.mgr()
        sid = self.start(m)
        n = m.gseq
        for ev in ({"ev": "subagent", "agent_id": "../../etc", "entries": []},
                   {"ev": "subagent", "agent_id": None},
                   {"ev": "subagent", "agent_id": "x" * 65}):
            self.feed(m, sid, ev)
        self.assertEqual(m.gseq, n)
        self.feed(m, sid, {"ev": "subagent", "agent_id": "ok", "entries": "notalist", "meta": "x", "reset": True})
        self.assertEqual(m.get_subagent(sid, "ok")["entries"], [])


# ============================================================================ server API
class ServerSubagentApiTests(TS.Base):
    def setUp(self):
        super().setUp()
        self.mgr.metas["s1"] = {"sid": "s1"}

        def list_subagents(sid):
            self.mgr._need(sid)
            return {"subagents": [{"agent_id": "a1", "status": "running"}]}

        def get_subagent(sid, aid):
            self.mgr._need(sid)
            self.mgr.calls.append(("get_subagent", sid, aid))
            return {"agent_id": aid, "entries": [{"type": "user"}]} if aid == "a1" else None
        self.mgr.list_subagents = list_subagents
        self.mgr.get_subagent = get_subagent

    def test_routes(self):
        st, js, _, _ = self.req("GET", "/api/sessions/s1/subagents")
        self.assertEqual((st, js), (200, {"subagents": [{"agent_id": "a1", "status": "running"}]}))
        st, js, _, _ = self.req("GET", "/api/sessions/s1/subagents/a1")
        self.assertEqual((st, js["entries"]), (200, [{"type": "user"}]))
        res = self.req("GET", "/api/sessions/s1/subagents/a2")
        self.assertErr(res, 404)
        self.assertEqual(res[1]["error"], "子代理不存在")
        for bad in ("/api/sessions/s1/subagents/a.b", "/api/sessions/s1/subagents/%2e%2e",
                    "/api/sessions/s1/subagents/" + "x" * 65):
            self.assertErr(self.req("GET", bad), 404)
        self.assertNotIn(("get_subagent", "s1", ".."), self.mgr.calls)
        self.assertEqual(self.req("GET", "/api/sessions/nope/subagents")[1]["error"], "会话不存在")
        self.assertErr(self.req("GET", "/api/sessions/s1/subagents", auth=False), 401)
        self.assertErr(self.req("GET", "/api/sessions/s1/subagents/a1", auth=False), 401)
        self.assertErr(self.req("POST", "/api/sessions/s1/subagents", {}), 404)


# ============================================================================ tui_parse：真实子代理画面
def _frames():
    txt = (FIX / "screen_frames.txt").read_text(encoding="utf-8")
    parts = re.split(r"^=== t=([0-9.]+)\n", txt, flags=re.M)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]


class SubagentScreenTests(unittest.TestCase):
    def test_all_frames(self):
        frames = _frames()
        self.assertGreater(len(frames), 30)
        for t, body in frames:
            r = tui_parse.parse_screen(body)
            self.assertIsNone(r["dialog"], t)
            self.assertTrue(r["input"]["visible"], t)
            self.assertEqual(r["mode"], "manual", t)
            self.assertEqual(r["busy"], t not in ("0.1", "18.4", "19.4"), t)
            for bad in ("● main", "general-purpose", "◯"):
                self.assertNotIn(bad, r["status_line"] or "", t)
            if t != "0.1":   # 0.1 帧输入框里是用户刚打的 prompt（含 general-purpose 字样）
                self.assertEqual(r["input"]["text"], "", t)
            self.assertNotEqual(r["turn_done"], "Waiting for 1 background agent to finish", t)
            for bad in ("○ low · /effort", "tmux focus-events off"):
                self.assertNotIn(bad, r["live"] or "", t)
            if "◯ general-purpose" in body:
                names = [a["name"] for a in r["agents_panel"]]
                self.assertEqual(names, ["main", "general-purpose"], t)
                self.assertTrue(r["agents_panel"][0]["current"])
                self.assertIn(r["agents_panel"][1]["desc"], ("前台计算 1+1", "后台计算 2+3"))
                self.assertRegex(r["agents_panel"][1]["info"], r"^\d+s")
            else:
                self.assertIsNone(r["agents_panel"], t)
        r = tui_parse.parse_screen(dict(frames)["7.2"])
        self.assertEqual(r["agents_panel"][1]["info"], "2s · ↑ 14.3k tokens")
        self.assertIn("[Haiku 5.5 · 1M · effort:low]", r["status_line"])
        r = tui_parse.parse_screen(dict(frames)["18.4"])
        self.assertEqual(r["turn_done"], "Cogitated for 17s · done 8:49 PM")
        r = tui_parse.parse_screen(dict(frames)["9.8"])
        self.assertIn("✻ Waiting for 1 background agent to finish", r["live"])
        self.assertIn('● Agent "前台计算 1+1" finished · 2s', r["live"])


# ============================================================================ fake_tui /agent
class FakeTuiAgentTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="ccrsaft", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        os.makedirs(os.path.join(self.home, "proj"))
        self.app = fake_tui.FakeClaude([], env={"HOME": self.home, "USER": "u", "CCR_FAKE_DELAY": "0",
                                                "CCR_FAKE_THINK": "0"}, size=(120, 30),
                                       cwd=os.path.join(self.home, "proj"))

    def run_turn(self, a, until=None, limit=2000):
        for _ in range(limit):
            if a.turn is None or (until and until()):
                return
            a.turn_due = 0
            a.step_turn()
        raise AssertionError("回合没有结束")

    def transcript(self):
        with open(self.app.tpath, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_agent_flow(self):
        a = self.app
        a.on_text("请 /agent")
        a.on_key("Enter")
        self.run_turn(a, until=lambda: bool(a.agents))
        frame = "\n".join(a.frame())
        r = tui_parse.parse_screen(frame)
        self.assertTrue(r["busy"])
        self.assertEqual([x["name"] for x in r["agents_panel"]], ["main", "general-purpose"])
        self.assertIn("↓ to manage", r["mode_line"])
        self.assertIn("Backgrounded agent (↓ to manage · ctrl+o to expand)", frame)
        sub = next(iter(a.agents.values()))
        meta = json.loads(pathlib.Path(sub.metap).read_text(encoding="utf-8"))
        self.assertFalse(meta["finished"])
        self.run_turn(a)
        self.assertEqual(a.agents, {})
        self.assertTrue(json.loads(pathlib.Path(sub.metap).read_text(encoding="utf-8"))["finished"])
        self.assertIn('● Agent "%s" finished' % fake_tui.AGENT_DESC, "\n".join(a.frame()))
        ents = [e for e in self.transcript() if e.get("type") in ("user", "assistant")]
        tu = [b for e in ents if e["type"] == "assistant" for b in e["message"]["content"] if b["type"] == "tool_use"]
        self.assertEqual(tu[0]["name"], "Agent")
        launched = next(e for e in ents if e.get("toolUseResult", {}).get("agentId") == sub.aid)
        self.assertIn("agentId: %s" % sub.aid, launched["message"]["content"][0]["content"][0]["text"])
        note = next(e for e in ents if SA.notification_text(e))
        self.assertEqual((note["origin"]["kind"], note["promptSource"]), ("task-notification", "system"))
        n = SA.parse_notification(note["message"]["content"])
        self.assertEqual((n["task_id"], n["tool_use_id"], n["status"], n["usage"]["tool_uses"]),
                         (sub.aid, tu[0]["id"], "completed", 3))
        # 子代理 jsonl：有 api-request*/attachment 噪声行，node agent 只转发 user/assistant
        raw = [json.loads(x) for x in pathlib.Path(sub.jsonl).read_text(encoding="utf-8").splitlines() if x]
        self.assertTrue({"attachment", "api-request-blob", "api-request-shape", "api-request"} <= {x["type"] for x in raw})
        self.assertTrue(all(x["isSidechain"] and x["agentId"] == sub.aid for x in raw if x["type"] in ("user", "assistant")))
        t = SA.Tracker()
        t.absorb_main(ents)
        t.apply_event(sub.aid, meta, [e for e in (NA.filter_entry(json.dumps(x).encode(), NA.SUBAGENT_TYPES, True,
                                                                  NA.MAX_SUB_ENTRY_BYTES) for x in raw) if e], True)
        s = t.summary(sub.aid)
        self.assertEqual((s["status"], s["tool_uses"], s["description"], s["result"]),
                         ("completed", 3, fake_tui.AGENT_DESC, sub.result))

    def test_interrupt_kills_subagent(self):
        a = self.app
        a.on_text("/agent")
        a.on_key("Enter")
        self.run_turn(a, until=lambda: bool(a.agents))
        sub = next(iter(a.agents.values()))
        a.on_key("Escape")
        self.assertIsNone(a.turn)
        self.assertEqual(a.agents, {})
        self.assertTrue(json.loads(pathlib.Path(sub.metap).read_text(encoding="utf-8"))["finished"])
        notes = [SA.parse_notification(e["message"]["content"]) for e in self.transcript() if SA.notification_text(e)]
        self.assertEqual([x["status"] for x in notes], ["killed"])

    def sub_tracker(self, sub):
        t = SA.Tracker()
        t.absorb_main([e for e in (NA.filter_entry(json.dumps(x).encode()) for x in self.transcript()) if e])
        raw = pathlib.Path(sub.jsonl).read_bytes().split(b"\n")
        t.apply_event(sub.aid, json.loads(pathlib.Path(sub.metap).read_text(encoding="utf-8")),
                      [e for e in (NA.filter_entry(x, NA.SUBAGENT_TYPES, True, NA.MAX_SUB_ENTRY_BYTES)
                                   for x in raw if x.strip()) if e], True)
        return t

    def test_ctrl_x_ctrl_k_stops_subagent(self):
        a = self.app
        a.on_text("/agent")
        a.on_key("Enter")
        self.run_turn(a, until=lambda: bool(a.agents))
        sub = next(iter(a.agents.values()))
        a.on_key("C-x")
        a.on_key("C-k")
        self.assertIn(fake_tui.KILL_HINT, "\n".join(a.frame()))
        self.assertEqual(len(a.agents), 1)    # 第一次只提示
        a.on_key("C-x")
        a.on_key("C-k")
        self.assertEqual(a.agents, {})
        meta = json.loads(pathlib.Path(sub.metap).read_text(encoding="utf-8"))
        self.assertEqual((meta.get("finished"), meta.get("stoppedByUser")), (False, True))
        self.run_turn(a)
        frame = "\n".join(a.frame())
        self.assertIn("● Background agents stopped", frame)
        raw = [json.loads(x) for x in pathlib.Path(sub.jsonl).read_text(encoding="utf-8").splitlines() if x]
        convo = [x for x in raw if x["type"] in ("user", "assistant")]
        self.assertTrue(SA.is_interrupt_marker(convo[-1]["message"]["content"]))   # 停止后不再写子代理条目
        tr = self.transcript()
        self.assertTrue(any(e.get("subtype") == "agents_killed" for e in tr))
        plain = [e for e in tr if e.get("type") == "user" and e.get("origin", {}).get("kind") == "task-notification"]
        self.assertEqual([e["message"]["content"] for e in plain],
                         ['Background agent "%s" was stopped by the user.' % fake_tui.AGENT_DESC])
        self.assertFalse(any(SA.notification_text(e) for e in tr))
        self.assertEqual(self.sub_tracker(sub).summary(sub.aid)["status"], "killed")

    def test_agentstop_absorbed_attachment(self):
        a = self.app
        a.on_text("/agentstop")
        a.on_key("Enter")
        self.run_turn(a)
        self.assertEqual(a.agents, {})
        frame = "\n".join(a.frame())
        self.assertIn("Stop Task", frame)
        tr = self.transcript()
        tu = [b["name"] for e in tr if e.get("type") == "assistant" for b in e["message"]["content"]
              if b.get("type") == "tool_use"]
        self.assertEqual(tu, ["Agent", "TaskStop"])
        self.assertFalse(any(e.get("type") == "user" and SA.notification_text(e) for e in tr))
        att = [e for e in tr if e.get("type") == "attachment" and SA.notification_text(e)]
        self.assertEqual(len(att), 1)
        sub = FakeSubFiles(self.home)
        meta = json.loads(pathlib.Path(sub.metap).read_text(encoding="utf-8"))
        self.assertFalse(meta.get("finished") or meta.get("stoppedByUser"))
        t = self.sub_tracker(sub)
        s = t.summary(sub.aid)
        self.assertEqual((s["status"], s["raw_status"], s["summary"]),
                         ("killed", "killed", 'Agent "%s" was stopped by Claude' % fake_tui.AGENT_DESC))


class FakeSubFiles(object):
    """fake 会话里唯一一个子代理的文件路径（/agentstop 结束后 app.agents 已清空）。"""
    def __init__(self, home):
        metas = list(pathlib.Path(home).glob(".claude/projects/*/*/subagents/agent-*.meta.json"))
        assert len(metas) == 1, metas
        self.metap = str(metas[0])
        self.jsonl = self.metap[:-len(".meta.json")] + ".jsonl"
        self.aid = metas[0].name[len("agent-"):-len(".meta.json")]


# ============================================================================ 被中断 / 被停止的子代理（CLI 2.1.332 实测）
# fixtures/subagent_stop/：本机 2.1.332 真机录制（只保留 user/assistant/system/queue-operation 与通知 attachment，
# 去掉 thinking 签名与 hook 信息），同一会话里四个后台子代理分别被这样停止：
#   慢慢数数   Ctrl+X Ctrl+K 两次 → meta.stoppedByUser、子代理末尾中断标记、system agents_killed、纯文本通知（无 task-id）
#   再数一次   Claude 调 TaskStop（本轮刚好结束）→ 中断标记；killed 通知 3 秒后才作为 user 条目出现
#   第三次数数 空闲时 Ctrl+C → 同 Ctrl+X Ctrl+K，但纯文本通知是"被动"的（下一条用户消息时才以 attachment 送达）
#   第四次数数 子代理列表里按 x → meta.stoppedByUser + 中断标记 + killed 通知（"was stopped by user"）
# fixtures/subagent_stop/absorbed/：服务器上的真实样本（脱敏改写）——主会话回复中途 TaskStop，killed 通知被吸收为
#   attachment/queued_command，**没有** user 条目；子代理 meta 没有 finished / stoppedByUser，只有末尾的中断标记。
STOP = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "subagent_stop"
STOP_MAIN = "9d61c899-4e9a-4d4b-b33c-03d3d2708ec9"
ABS_MAIN = "4c4bf040-0000-4000-8000-000000000001"
S_KK, S_TS, S_CC, S_X = "a5083efeea30bdfd0", "a463eec73b10b26d4", "a27061ff901fbfbd5", "a7d061856955cf1d7"
S_ABS = "a8d153a165a587509"


def lines_of(path, *a, **kw):
    out = []
    for line in pathlib.Path(path).read_bytes().split(b"\n"):
        if line.strip():
            e = NA.filter_entry(line, *a, **kw)
            if e is not None:
                out.append(e)
    return out


def stop_sub(root, aid):
    return (json.loads((root / "subagents" / ("agent-%s.meta.json" % aid)).read_text(encoding="utf-8")),
            lines_of(root / "subagents" / ("agent-%s.jsonl" % aid), NA.SUBAGENT_TYPES, sidechain=True,
                     max_bytes=NA.MAX_SUB_ENTRY_BYTES))


def marker(uid, ts="2026-11-22T12:00:09Z", text="[Request interrupted by user for tool use]"):
    return {"type": "user", "uuid": uid, "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "text", "text": text}]}}


def asst(uid, ts, text="继续"):
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}


class StoppedSubagentTests(unittest.TestCase):
    def tracker(self, root, main, aids):
        t = SA.Tracker()
        t.absorb_main(lines_of(root / main))
        for aid in aids:
            meta, ents = stop_sub(root, aid)
            t.apply_event(aid, meta, ents, True)
        return t

    def test_fixture_all_stops_are_killed(self):
        t = self.tracker(STOP, "main_9d61c899.jsonl", (S_KK, S_TS, S_CC, S_X))
        got = {s["agent_id"]: (s["status"], s["raw_status"], s["notifications"]) for s in t.summaries()}
        self.assertEqual(got, {S_KK: ("killed", None, 0), S_TS: ("killed", "killed", 1), S_CC: ("killed", None, 0),
                               S_X: ("killed", "killed", 1)})
        self.assertEqual(t.counts(), {"running": 0, "total": 4})
        s = t.summary(S_KK)
        self.assertIsNone(s["result"])
        self.assertTrue(s["ended_at"] >= s["started_at"])

    def test_fixture_meta_and_markers(self):
        """fixture 本身的形状（防止以后重录时丢了要点）。"""
        for aid, stopped in ((S_KK, True), (S_TS, False), (S_CC, True), (S_X, True)):
            meta, ents = stop_sub(STOP, aid)
            self.assertNotIn("finished", meta)
            self.assertEqual(meta.get("stoppedByUser", False), stopped, aid)
            self.assertTrue(SA.is_interrupt_marker(ents[-1]["message"]["content"]), aid)
        raw = [json.loads(x) for x in (STOP / "main_9d61c899.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(1 for e in raw if e.get("subtype") == "agents_killed"), 2)
        plain = [e for e in raw if e.get("type") == "user" and e.get("origin", {}).get("kind") == "task-notification"
                 and isinstance(e["message"]["content"], str) and "<task-notification>" not in e["message"]["content"]]
        self.assertEqual([e["message"]["content"] for e in plain], ['Background agent "慢慢数数" was stopped by the user.'])

    def test_absorbed_taskstop_attachment(self):
        root = STOP / "absorbed"
        main = "main_%s.jsonl" % ABS_MAIN
        ents = lines_of(root / main)
        atts = [e for e in ents if e["type"] == "attachment"]
        # 只转发通知那一条（排队的用户消息 commandMode=prompt 不转发），且只剩精简字段
        self.assertEqual(len(atts), 1)
        self.assertEqual(set(atts[0]), {"type", "uuid", "parentUuid", "timestamp", "isSidechain", "attachment"})
        self.assertEqual(set(atts[0]["attachment"]), {"type", "commandMode", "prompt", "origin"})
        self.assertTrue(atts[0]["attachment"]["prompt"].startswith("<task-notification>"))
        self.assertTrue(SA.notification_text(atts[0]))
        self.assertFalse(any(e["type"] == "user" and SA.notification_text(e) for e in ents))
        t = self.tracker(root, main, (S_ABS,))
        s = t.summary(S_ABS)
        self.assertEqual((s["status"], s["raw_status"], s["summary"], s["notifications"]),
                         ("killed", "killed", 'Agent "搭建沙盒" was stopped by Claude', 1))
        # 只有子代理文件、还没读到主 transcript 的通知时，单靠末尾的中断标记也判为已终止
        t2 = SA.Tracker()
        t2.apply_event(S_ABS, *stop_sub(root, S_ABS), True)
        self.assertEqual(t2.summary(S_ABS)["status"], "killed")
        # 旧写法（没有通知、也没有中断标记）仍是运行中
        meta, sub = stop_sub(root, S_ABS)
        t3 = SA.Tracker()
        t3.apply_event(S_ABS, meta, sub[:-1], True)
        self.assertEqual(t3.summary(S_ABS)["status"], "running")

    def test_live_user_stop_then_resume(self):
        """实时顺序：meta.stoppedByUser 先到（立即已终止），之后的拒绝结果 / 中断标记不改变；被续跑后恢复运行中。"""
        t = SA.Tracker()
        meta = {"toolUseId": "tu1", "description": "x", "requestShape": "background"}
        t.apply_event("ag1", meta, [{"type": "user", "uuid": "p", "timestamp": "2026-11-22T12:00:00Z",
                                     "message": {"content": "做事"}},
                                    {"type": "assistant", "uuid": "a", "timestamp": "2026-11-22T12:00:01Z",
                                     "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                              "input": {"command": "sleep 20"}}]}}], True)
        self.assertEqual(t.summary("ag1")["status"], "running")
        t.apply_event("ag1", dict(meta, stoppedByUser=True), [], False)
        self.assertEqual(t.summary("ag1")["status"], "killed")
        self.assertEqual(t.counts(), {"running": 0, "total": 1})
        rej = {"type": "user", "uuid": "r", "timestamp": "2026-11-22T12:00:08Z", "toolUseResult": "User rejected tool use",
               "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": fake_tui.REJECT_TEXT}]}}
        t.apply_event("ag1", None, [rej], False)
        self.assertEqual(t.summary("ag1")["status"], "killed")
        t.apply_event("ag1", None, [marker("m")], False)
        self.assertEqual(t.summary("ag1")["status"], "killed")
        # 续跑（meta 里 stoppedByUser 仍在）：新的 user 消息 + assistant → 运行中
        t.apply_event("ag1", None, [{"type": "user", "uuid": "u2", "timestamp": "2026-11-22T12:05:00Z",
                                     "message": {"content": "接着做"}}], False)
        self.assertEqual(t.summary("ag1")["status"], "running")   # 停止之后有了新的用户消息
        t.apply_event("ag1", None, [asst("a2", "2026-11-22T12:05:01Z")], False)
        self.assertEqual(t.summary("ag1")["status"], "running")
        # 再次停止（只多一条中断标记）→ 已终止
        t.apply_event("ag1", None, [marker("m2", "2026-11-22T12:06:00Z")], False)
        self.assertEqual(t.summary("ag1")["status"], "killed")
        # transcript reset 后整批重发：续跑过的子代理以最后一条中断标记为准
        t.clear()
        meta2 = dict(meta, stoppedByUser=True)
        ents = [marker("m"), {"type": "user", "uuid": "u2", "message": {"content": "接着做"}},
                asst("a2", "2026-11-22T12:05:01Z")]
        t.apply_event("ag1", meta2, ents, True)
        self.assertEqual(t.summary("ag1")["status"], "running")
        t.clear()
        t.apply_event("ag1", meta2, ents[:1], True)
        self.assertEqual(t.summary("ag1")["status"], "killed")

    def test_marker_precedence(self):
        t = SA.Tracker()
        t.apply_event("ag1", {"toolUseId": "tu1"}, [asst("a", "2026-11-22T12:00:01Z"), marker("m")], True)
        self.assertEqual(t.summary("ag1")["status"], "killed")
        # 被拒绝后又正常结束（finished）/ 有更新的完成通知 → 以它们为准
        t.apply_event("ag1", {"toolUseId": "tu1", "finished": True}, [], False)
        self.assertEqual(t.summary("ag1")["status"], "completed")
        t2 = SA.Tracker()
        t2.apply_event("ag2", {"toolUseId": "tu2"}, [asst("a", "2026-11-22T12:00:01Z"), marker("m")], True)
        t2.absorb_main([note_entry("ag2", "tu2", "completed", ts="2026-11-22T12:00:10Z")])
        self.assertEqual(t2.summary("ag2")["status"], "completed")
        # 通知之后续跑、又被停止 → 已终止
        t2.apply_event("ag2", None, [asst("a3", "2026-11-22T12:01:00Z"), marker("m3", "2026-11-22T12:01:05Z")], False)
        self.assertEqual(t2.summary("ag2")["status"], "killed")
        # 字符串形式与"[Request interrupted by user]"（等待 API 时被停）也算；普通文字不算
        for c, want in (("[Request interrupted by user]", True), ([{"type": "text", "text": "[Request interrupted by user]"}], True),
                        ([{"type": "text", "text": "请问 [Request interrupted by user] 是什么"}], False),
                        ([{"type": "tool_result", "content": "[Request interrupted by user]"}], False), ("hi", False)):
            self.assertEqual(SA.is_interrupt_marker(c), want, c)

    def test_node_agent_attachment_filter(self):
        mk = lambda a, **kw: json.dumps(dict({"type": "attachment", "uuid": "x", "attachment": a}, **kw)).encode()  # noqa: E731
        note = "<task-notification>\n<task-id>ag1</task-id>\n<status>killed</status>\n</task-notification>"
        ok = NA.filter_entry(mk({"type": "queued_command", "prompt": note, "commandMode": "task-notification",
                                 "source_uuid": "s", "rendered": "x" * 100}, rendered=[{"content": "x" * 5000}]))
        self.assertEqual(ok, {"type": "attachment", "uuid": "x", "attachment": {
            "type": "queued_command", "commandMode": "task-notification", "prompt": note}})
        # 只有 origin.kind 也算；纯文本通知（无 XML）同样转发（前端显示为系统消息）
        self.assertIsNotNone(NA.filter_entry(mk({"type": "queued_command", "prompt": "Background agent \"x\" was stopped",
                                                 "origin": {"kind": "task-notification"}})))
        for bad in ({"type": "queued_command", "prompt": "排队的用户消息", "commandMode": "prompt", "origin": {"kind": "human"}},
                    {"type": "queued_command", "prompt": "", "commandMode": "task-notification"},
                    {"type": "skill_listing", "content": note}, {"type": "environment"}, "x"):
            self.assertIsNone(NA.filter_entry(mk(bad)), bad)
        self.assertIsNone(NA.filter_entry(mk({"type": "queued_command", "prompt": note, "commandMode": "task-notification"},
                                             isSidechain=True)))
        # 子代理 transcript 不转发 attachment
        self.assertIsNone(NA.filter_entry(mk({"type": "queued_command", "prompt": note, "commandMode": "task-notification"}),
                                          NA.SUBAGENT_TYPES, sidechain=True))
        # 超长 prompt 截断
        big = NA.filter_entry(mk({"type": "queued_command", "prompt": note + "y" * 30000, "commandMode": "task-notification"}))
        self.assertTrue(big["truncated"])

    def test_history_keeps_note_attachment(self):
        lines = (STOP / "absorbed" / ("main_%s.jsonl" % ABS_MAIN)).read_bytes().split(b"\n")
        r = H.read_lines(lines)
        atts = [e for e in r["entries"] if e["type"] == "attachment"]
        self.assertEqual(len(atts), 1)
        self.assertEqual(set(atts[0]["attachment"]), {"type", "commandMode", "prompt", "origin"})
        self.assertIn("<status>killed</status>", atts[0]["attachment"]["prompt"])
        self.assertNotIn("rendered", atts[0])


class SessionStoppedSubagentTests(SessionBase):
    def test_stop_turns_working_off(self):
        m = self.mgr()
        sid = self.start(m)
        feed = lambda ev: m._on_link_event("12345", dict(ev, sid=sid))  # noqa: E731
        root = STOP / "absorbed"
        ents = lines_of(root / ("main_%s.jsonl" % ABS_MAIN))
        cut = next(i for i, e in enumerate(ents) if e["type"] == "attachment")
        meta, sub = stop_sub(root, S_ABS)
        feed({"ev": "transcript", "session_id": ABS_MAIN, "entries": ents[:cut], "reset": True})
        feed({"ev": "subagent", "session_id": ABS_MAIN, "agent_id": S_ABS, "meta": meta, "entries": sub[:-1],
              "reset": True})
        self.assertEqual(m.get_meta(sid)["subagents"], {"running": 1, "total": 1})
        self.assertTrue(m.get_meta(sid)["working"])
        n0 = m.gseq
        feed({"ev": "subagent", "session_id": ABS_MAIN, "agent_id": S_ABS, "meta": None, "entries": sub[-1:],
              "reset": False})
        self.assertEqual(m.get_meta(sid)["subagents"], {"running": 0, "total": 1})
        self.assertFalse(m.get_meta(sid)["working"])
        evs = [e for e in m.events_since(n0) if e["kind"] == "subagent"]
        self.assertEqual(evs[-1]["data"]["summary"]["status"], "killed")
        # 吸收进回复的 attachment 通知随 transcript 事件转发给前端，并补上通知信息
        feed({"ev": "transcript", "session_id": ABS_MAIN, "entries": ents[cut:], "reset": False})
        s = m.list_subagents(sid)["subagents"][0]
        self.assertEqual((s["status"], s["raw_status"], s["notifications"]), ("killed", "killed", 1))
        tr = [x for e in m.session_events(sid) if e["kind"] == "transcript" and e["gseq"] > n0
              for x in e["data"]["entries"]]
        self.assertEqual([x["type"] for x in tr if x["type"] == "attachment"], ["attachment"])


# ============================================================================ history
class HistorySubagentTests(unittest.TestCase):
    def test_trim_entry_keeps_origin_and_title_skips_system(self):
        lines = (FIX / "main_1415819f.jsonl").read_bytes().split(b"\n")
        r = H.read_lines(lines)
        notes = [e for e in r["entries"] if "<task-notification>" in str(e["message"]["content"])]
        self.assertEqual(len(notes), 2)
        for e in notes:
            self.assertEqual((e["promptSource"], e["origin"]), ("system", {"kind": "task-notification"}))
        launched = [e["toolUseResult"] for e in r["entries"] if "toolUseResult" in e]
        self.assertEqual(launched, [{"agentId": A1, "status": "async_launched"},
                                    {"agentId": A2, "status": "async_launched"}])
        # 标题不取系统注入的消息
        note = json.loads(lines[45])
        title = H.summarize_lines([json.dumps(note).encode()])["title"]
        self.assertEqual(title, "")
        self.assertTrue(H.is_system_injected(note))
        self.assertFalse(H.is_system_injected(json.loads(lines[4])))


# ============================================================================ fake 端到端（server + SSE）
class SubagentFakeEndToEnd(TS.TestIntegrationFake):
    test_create_message_stream_replay = None   # 不重复跑父类的用例

    def test_agent_via_sse(self):
        st, boot, _, _ = self.req("GET", "/api/bootstrap")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)
        conn.request("GET", "/api/stream?since=0&instance=" + boot["instance_id"],
                     headers={"Host": self.host, "X-CCR-Token": self.tok})
        r = conn.getresponse()
        st, meta, _, raw = self.req("POST", "/api/sessions", {"title": "子代理"})
        self.assertEqual(st, 200, raw)
        sid = meta["sid"]
        js = self.wait_state(sid, ("idle", "trust"))
        if js["state"] == "trust":
            d = self.req("GET", "/api/sessions/%s/screen" % sid)[1]["state"]["dialog"]
            yes = next(o["index"] for o in d["options"] if o["label"].startswith("Yes"))
            self.req("POST", "/api/sessions/%s/choose" % sid, {"index": yes})
            self.wait_state(sid, ("idle",))
        st, js, _, raw = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "派个子代理 /agent"})
        self.assertEqual(st, 200, raw)
        statuses, entries, aid = [], [], None
        deadline = time.time() + 45
        while time.time() < deadline:
            line = r.fp.readline()
            if not line.startswith(b"data: "):
                continue
            ev = json.loads(line[6:])
            if ev["kind"] != "subagent" or ev["sid"] != sid:
                continue
            d = ev["data"]
            aid = d["agent_id"]
            statuses.append(d["summary"]["status"])
            entries += d["entries"]
            if d["summary"]["status"] == "completed":
                break
        conn.close()
        self.assertIsNotNone(aid)
        self.assertEqual(statuses[0], "running", statuses)
        self.assertEqual(statuses[-1], "completed", statuses)
        self.assertTrue(entries and all(e["type"] in ("user", "assistant") for e in entries))
        self.assertTrue(any(e.get("truncated") for e in entries))   # 超长 Read 结果被截断
        st, det, _, _ = self.req("GET", "/api/sessions/%s/subagents/%s" % (sid, aid))
        self.assertEqual(st, 200)
        self.assertEqual(det["summary"]["status"], "completed")
        self.assertEqual(det["summary"]["tool_uses"], 3)
        self.assertEqual(det["summary"]["description"], fake_tui.AGENT_DESC)
        self.assertGreaterEqual(len(det["entries"]), 8)
        self.assertEqual(det["entries"][0]["message"]["content"], fake_tui.AGENT_PROMPT)
        st, lst, _, _ = self.req("GET", "/api/sessions/%s/subagents" % sid)
        self.assertEqual([s["agent_id"] for s in lst["subagents"]], [aid])
        st, js, _, _ = self.req("GET", "/api/sessions/%s/events" % sid)
        sa = [e for e in js["events"] if e["kind"] == "subagent"]
        self.assertEqual(len(sa), 1)
        self.assertEqual(sa[0]["data"]["entries"], [])
        # 主 transcript 里的 <task-notification> 作为 user 条目（前端渲染成系统行）
        tr = [x for e in js["events"] if e["kind"] == "transcript" for x in e["data"]["entries"]]
        self.assertTrue(any(SA.notification_text(x) for x in tr))
        self.wait_state(sid, ("idle",))
        self.assertEqual(self.req("POST", "/api/sessions/%s/close" % sid)[0], 200)

    def test_agentstop_via_sse(self):
        """/agentstop：主会话 TaskStop 停掉子代理，通知只以 attachment 出现 → 摘要 running → killed，会话不再"在干活"。"""
        st, boot, _, _ = self.req("GET", "/api/bootstrap")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)
        conn.request("GET", "/api/stream?since=0&instance=" + boot["instance_id"],
                     headers={"Host": self.host, "X-CCR-Token": self.tok})
        r = conn.getresponse()
        st, meta, _, raw = self.req("POST", "/api/sessions", {"title": "停止子代理"})
        self.assertEqual(st, 200, raw)
        sid = meta["sid"]
        js = self.wait_state(sid, ("idle", "trust"))
        if js["state"] == "trust":
            d = self.req("GET", "/api/sessions/%s/screen" % sid)[1]["state"]["dialog"]
            yes = next(o["index"] for o in d["options"] if o["label"].startswith("Yes"))
            self.req("POST", "/api/sessions/%s/choose" % sid, {"index": yes})
            self.wait_state(sid, ("idle",))
        st, js, _, raw = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "派个子代理再停掉 /agentstop"})
        self.assertEqual(st, 200, raw)
        statuses, atts = [], []
        deadline = time.time() + 45
        while time.time() < deadline:
            line = r.fp.readline()
            if not line.startswith(b"data: "):
                continue
            ev = json.loads(line[6:])
            if ev["sid"] != sid:
                continue
            if ev["kind"] == "transcript":
                atts += [x for x in ev["data"]["entries"] if x.get("type") == "attachment"]
            if ev["kind"] == "subagent":
                statuses.append(ev["data"]["summary"]["status"])
                if statuses[-1] == "killed" and atts:
                    break
        conn.close()
        self.assertEqual(statuses[0], "running", statuses)
        self.assertEqual(statuses[-1], "killed", statuses)
        self.assertEqual(len(atts), 1)
        self.assertIn("<status>killed</status>", atts[0]["attachment"]["prompt"])
        self.wait_state(sid, ("idle",))
        st, m2, _, _ = self.req("GET", "/api/sessions/%s" % sid)
        sub = m2.get("subagents") or m2.get("meta", {}).get("subagents")
        self.assertEqual(sub, {"running": 0, "total": 1})
        self.assertEqual(self.req("POST", "/api/sessions/%s/close" % sid)[0], 200)


if __name__ == "__main__":
    unittest.main()
