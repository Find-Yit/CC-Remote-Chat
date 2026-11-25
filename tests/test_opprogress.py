"""重启 / 续接 / 新建 / 换 API 重启的阶段进度（meta.op，ccr/opprogress.py + ccr/session.py）。

桩：tests.test_session 的 StubRemote / StubLink / SimTUI（不起 tmux、不连服务器）；
fake：AgentLink 的 fake 钩子（--fake-delays、<数据目录>/fake/ended_jobs）只测纯逻辑，不起进程。
"""
import json
import pathlib
import tempfile
import threading
import time
import unittest

import app as ccr_app
from ccr import agent_link, opprogress as P, session as S
from tests.test_session import UUID1, Base, StubLink, notices, wait_until


def op_trail(m, sid, since=0):
    """meta 事件里 op 的（phase, state）序列（去重相邻重复）。"""
    out = []
    for e in m.session_events(sid)[since:]:
        d = e["data"]
        if d.get("type") != "meta":
            continue
        op = d["meta"].get("op")
        key = (op["phase"], op["state"]) if op else None
        if not out or out[-1] != key:
            out.append(key)
    return out


def phases_seen(m, sid, since=0):
    seen = []
    for k in op_trail(m, sid, since):
        if k and k[0] not in seen:
            seen.append(k[0])
    return seen


class OpBase(Base):
    def mgr(self, auto_ready=True, ended=(), never=()):
        """ended：这些作业的 link 一启动就报告"作业已结束"；never：这些作业的 link 永远连不上。"""
        def factory(cfg, jobid, on_event, on_state):
            if jobid in ended:
                lk = StubLink(self.rem, cfg, jobid, on_event, on_state, auto_ready=False)
                orig = lk.start

                def start():
                    orig()
                    threading.Timer(0.05, lk.end_job).start()
                lk.start = start
                return lk
            return StubLink(self.rem, cfg, jobid, on_event, on_state,
                            auto_ready=auto_ready and jobid not in never)
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp), link_factory=factory)
        m.step_timeout = 1.0
        m.exit_timeout = 2.0
        m.poll_interval = 0.01
        m.link_timeout = 3.0
        m.connect_grace = 0.3
        m.screen_interval = 0.05
        m.op_done_linger = 0.3
        self.mgrs.append(m)
        self.addCleanup(self._shut, m)
        return m


class TestPhases(OpBase):
    def test_restart_live_remote_phases_in_order_then_cleared(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        n0 = len(m.session_events(sid))
        meta = m.restart(sid)
        op = meta["op"]
        self.assertEqual(op["kind"], "restart")
        self.assertEqual([p["id"] for p in op["phases"]], ["stop", "keeper", "start", "ready"])  # 同作业：link 已就绪
        self.assertIn("--resume 59dd443a", P.phase(op, "start")["label"])
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["op"] or {}).get("state") == "done"
                                   or m.get_meta(sid)["op"] is None))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["op"] is None, 3))   # 成功后短暂显示"已就绪"再收起
        trail = op_trail(m, sid, n0)
        self.assertIn(("ready", "done"), trail)
        self.assertEqual(phases_seen(m, sid, n0), ["stop", "keeper", "start", "ready"])
        self.assertEqual(trail[-1], None)
        # 每次进入新阶段都推一次 meta（SSE），阶段时间单调
        metas = [e["data"]["meta"]["op"] for e in m.session_events(sid)[n0:]
                 if e["data"].get("type") == "meta" and e["data"]["meta"].get("op")]
        last = metas[-1]
        ts = [p["t0"] for p in last["phases"]]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(p["state"] == "done" for p in last["phases"]))

    def test_resume_new_job_phases_include_ssh_and_srun(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        n0 = len(m.session_events(sid))
        meta = m.restart(sid, jobid="67890")
        op = meta["op"]
        self.assertEqual((op["kind"], op["jobid"], op["local"]), ("resume_new_job", "67890", False))
        labels = {p["id"]: p["label"] for p in op["phases"]}
        self.assertEqual(list(labels), ["stop", "probe", "connect", "keeper", "start", "ready"])
        self.assertEqual(labels["probe"], "ssh gpu-cluster：查询作业 67890（squeue）")
        self.assertEqual(labels["connect"], "srun --jobid=67890 --overlap：在作业上启动 agent")
        self.assertIn("Slurm 偶尔不稳", P.phase(op, "connect")["slow_hint"])
        self.assertEqual(P.phase(op, "connect")["slow_after"], 30)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        seen = phases_seen(m, sid, n0)
        self.assertEqual(seen[0], "stop")
        self.assertEqual([x for x in seen if x != "probe"], ["stop", "connect", "keeper", "start", "ready"])

    def test_create_local_phases(self):
        m = self.mgr()
        meta = m.create(jobid="local", cwd="~")
        op = meta["op"]
        self.assertEqual((op["kind"], op["local"]), ("create", True))
        self.assertEqual([p["id"] for p in op["phases"]], ["connect", "keeper", "start", "ready"])
        text = json.dumps(op, ensure_ascii=False)
        self.assertNotIn("srun", text)
        self.assertNotIn("ssh", text)
        self.assertIn("本机", P.phase(op, "connect")["label"])
        self.assertEqual(P.phase(op, "connect")["slow_hint"], "比平时慢，仍在进行")
        sid = meta["sid"]
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["op"] is None, 3))

    def test_reconnect_after_exit_kind_and_no_stop(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        m.close(sid)
        op = m.restart(sid)["op"]
        self.assertEqual(op["kind"], "reconnect")
        self.assertNotIn("stop", [p["id"] for p in op["phases"]])

    def test_switch_api_and_mode_kinds(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        kid = m.api_keys.add("k1", "sk-test-aaaaaaaaaaaa1111")["id"] if hasattr(m.api_keys, "add") else None
        if kid:
            self.assertEqual(m.restart(sid, api_key_id=kid)["op"]["kind"], "switch_api")
            self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle" and m.get_meta(sid)["op"] is None, 5))
        self.assertEqual(m.restart(sid, mode="dontAsk")["op"]["kind"], "mode")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))

    def test_stop_phase_details(self):
        m = self.mgr()
        sid = self.start(m)
        m.restart(sid)
        details = [e["data"]["meta"]["op"].get("detail") for e in m.session_events(sid)
                   if e["data"].get("type") == "meta" and e["data"]["meta"].get("op")]
        self.assertIn("在终端里输入 /exit…", details)
        self.assertTrue(any(d and d.startswith("等待 claude 退出") for d in details), details)

    def test_trust_dialog_ends_progress_without_answering(self):
        self.rem.trust = True
        m = self.mgr()
        meta = m.create(jobid="12345", cwd="~")
        sid = meta["sid"]
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["op"] or {}).get("state") == "trust"))
        op = m.get_meta(sid)["op"]
        self.assertEqual(op["detail"], "等待你在上方确认信任目录")
        time.sleep(0.5)
        self.assertEqual(self.rem.ops("keys"), [])                     # 红线：信任框绝不自动回答
        self.assertEqual(m.get_meta(sid)["op"]["state"], "trust")      # 一直提示，直到用户处理
        m.choose(sid, 1)                                               # 用户点了"Yes, I trust"
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["op"] is None))

    def test_op_not_persisted_and_args_unchanged(self):
        m = self.mgr()
        sid = self.start(m, model="sonnet", permission_mode="plan")
        self.converse(m, sid)
        m.restart(sid)
        m.flush()
        data = json.loads((pathlib.Path(self.tmp) / "sessions.json").read_text())
        self.assertNotIn("op", data["sessions"][0]["meta"])
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        # 进度只是本应用自己的报告：启动参数与以前完全相同（白名单不变）
        self.assertEqual(self.rem.ops("start")[-1]["args"], ["--resume", UUID1, "--model", "sonnet",
                                                             "--permission-mode", "plan"])


class TestFailures(OpBase):
    def test_new_job_ended_failure_text(self):
        m = self.mgr(ended=("99999",))
        sid = self.start(m)
        self.converse(m, sid)
        m.restart(sid, jobid="99999")
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["op"] or {}).get("state") == "failed"))
        meta = m.get_meta(sid)
        op = meta["op"]
        self.assertEqual(op["error"], "srun 无法连接作业 99999：作业已结束（请在新作业上续接）")
        self.assertEqual(op["error_code"], "job_ended")
        self.assertIn(P.phase(op, op["phase"])["state"], ("fail",))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "error"))
        self.assertEqual(m.get_meta(sid)["error"], "启动失败：srun 无法连接作业 99999：作业已结束（请在新作业上续接）")
        # 可以重试（换一个作业）：新的进度替换失败的
        op2 = m.restart(sid, jobid="67890")["op"]
        self.assertNotEqual(op2["id"], op["id"])
        self.assertEqual(op2["state"], "running")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))

    def test_connect_retries_then_timeout_short_reason(self):
        m = self.mgr(never=("67890",))
        m.link_timeout = 1.0
        sid = self.start(m)
        self.converse(m, sid)
        m.restart(sid, jobid="67890")
        link = self.rem.links[-1]
        self.assertEqual(link.jobid, "67890")
        raw = ("连接计算节点 agent 失败：agent 连接已断开：ssh: connect to host wz.example.cn port 11808: "
               "Operation timed out；Connection closed")
        link.set_state("down", raw)
        self.assertTrue(wait_until(lambda: "第 1 次连接失败" in ((m.get_meta(sid)["op"] or {}).get("detail") or "")))
        op = m.get_meta(sid)["op"]
        self.assertEqual(op["phase"], "connect")
        self.assertEqual(op["retries"], 1)
        self.assertIn("ssh 连接 gpu-cluster 超时", op["detail"])
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["op"] or {}).get("state") == "failed", 4))
        op = m.get_meta(sid)["op"]
        self.assertEqual(op["error"], "连接作业 67890 超时（已等 1 秒）：ssh 连接 gpu-cluster 超时")
        self.assertNotIn("Connection closed", op["error"])             # 不把整段 stderr 甩给用户
        self.assertNotIn("Traceback", m.get_meta(sid)["error"])
        self.assertTrue(any(t == "启动失败：" + op["error"] for t in notices(m, sid)), notices(m, sid))

    def test_start_failure_text(self):
        m = self.mgr()
        sid = self.start(m)
        self.rem.fail_start = "目录不存在：/nope"
        m.restart(sid)
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["op"] or {}).get("state") == "failed"))
        op = m.get_meta(sid)["op"]
        self.assertEqual(op["phase"], "start")
        self.assertEqual(op["error"], "在作业 12345的 tmux 里启动 claude 失败：目录不存在：/nope")

    def test_claude_exits_during_ready(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        orig = self.rem.links[0].request

        def request(op, timeout=30, **p):
            if op == "watch" and p.get("on"):   # 新 claude 还没画出输入框就退出了
                t = self.rem.tuis[p["sid"]]
                t.dead, t.exit_status = True, 1
            return orig(op, timeout=timeout, **p)
        self.rem.links[0].request = request
        m.restart(sid)
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["op"] or {}).get("state") == "failed"))
        self.assertEqual(m.get_meta(sid)["op"]["error"], "claude 启动后就退出了（退出码 1），可打开终端面板查看它的输出")

    def test_ready_timeout_stalled(self):
        m = self.mgr()
        m.ready_timeout = 0.4
        sid = self.start(m)
        self.converse(m, sid)
        orig_start = self.rem.links[0].request

        def request(op, timeout=30, **p):
            res = orig_start(op, timeout=timeout, **p)
            if op == "start":
                self.rem.tuis[p["sid"]].render = lambda: ""   # claude 一直没画出输入框
                self.rem.tuis[p["sid"]].status = lambda: None
            return res
        self.rem.links[0].request = request
        m.restart(sid)
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["op"] or {}).get("state") == "stalled", 4))
        op = m.get_meta(sid)["op"]
        self.assertIn("仍未出现输入框", op["detail"])
        self.assertEqual(self.rem.keys_sent(sid), [])

    def test_sync_errors_clear_progress(self):
        """job_ended / api_key_missing / link_down 等由前端接着弹选择框：进度收起，不残留"失败"。"""
        m = self.mgr()
        sid = self.start(m)
        self.rem.links[0].set_state("down", "ssh: Connection timed out")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "down"))
        self.rem.job_state = False
        with self.assertRaises(S.OpError) as cm:
            m.restart(sid, jobid="123456")
        self.assertEqual(cm.exception.api_code, "link_down")
        self.assertIsNone(m.get_meta(sid)["op"])

    def test_waits_for_other_operation(self):
        m = self.mgr()
        sid = self.start(m)
        s = m._sessions[sid]
        s.oplock.acquire()
        threading.Timer(0.4, s.oplock.release).start()
        t = threading.Thread(target=m.restart, args=(sid,))
        t.start()
        self.assertTrue(wait_until(lambda: ((m.get_meta(sid)["op"] or {}).get("detail") or "")
                                   == "等待该会话当前的操作结束…", 2))
        t.join(5)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))


class TestIdempotent(OpBase):
    def _slow_keeper(self, delay):
        orig = StubLink.ensure_keeper

        def slow(link, timeout=120):
            time.sleep(delay)
            return orig(link, timeout)
        self._orig = orig
        StubLink.ensure_keeper = slow
        self.addCleanup(setattr, StubLink, "ensure_keeper", orig)

    def test_concurrent_requests_execute_once(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        self._slow_keeper(0.6)
        n_start = len(self.rem.ops("start"))
        n_exit = len([p for p in self.rem.ops("paste") if p["text"] == "/exit"])
        res, errs = [], []
        barrier = threading.Barrier(4)

        def go():
            barrier.wait()
            try:
                res.append(m.restart(sid))
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(errs, [])
        self.assertEqual(len(res), 4)
        self.assertEqual(sum(1 for r in res if r.get("op_reused")), 3)             # 其余 3 个直接拿到当前进度
        self.assertEqual(len({r["op"]["id"] for r in res}), 1)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        self.assertEqual(len(self.rem.ops("start")) - n_start, 1)                  # 只启动了一次
        self.assertEqual(len([p for p in self.rem.ops("paste") if p["text"] == "/exit"]) - n_exit, 1)

    def test_repeat_while_starting_returns_progress_fast(self):
        """以前：第二次点击要等启动线程释放操作锁（最多 60 秒）后报 409「正在执行其它操作」，或者把刚启动的 claude 再重启一遍。"""
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        self._slow_keeper(1.0)
        first = m.restart(sid, jobid="67890")
        t0 = time.monotonic()
        again = m.restart(sid, jobid="67890")
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertTrue(again["op_reused"])
        self.assertEqual(again["op"]["id"], first["op"]["id"])
        self.assertEqual(again["op"]["kind"], "resume_new_job")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        self.assertEqual(len([p for p in self.rem.ops("start") if "--resume" in p["args"]]), 1)

    def test_create_in_progress_then_restart_reused(self):
        m = self.mgr()
        self._slow_keeper(0.5)
        sid = m.create(jobid="12345", cwd="~")["sid"]
        r = m.restart(sid)
        self.assertTrue(r["op_reused"])
        self.assertEqual(r["op"]["kind"], "create")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        self.assertEqual(len(self.rem.ops("start")), 1)


class TestResumeAfterJobEnded(OpBase):
    """回归：作业已结束 → 在新作业上续接，不能碰已结束的旧作业（任何会挂起的操作），要和"从历史恢复"一样快。"""

    def _end_old_job(self, m, sid):
        link = self.rem.links[0]
        self.rem.tuis.clear()          # Slurm 结束作业：tmux / claude 都随之消失
        self.rem.keeper = False
        link.end_job()
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "ended" and m.get_meta(sid)["state"] == "exited"))
        self.assertTrue(wait_until(lambda: link.closed))
        # 旧作业的 link 若再被调用就挂 30 秒（模拟 srun --jobid=<已结束作业> 长时间挂起）
        hung = []

        def hang(*a, **k):
            hung.append(a)
            time.sleep(30)
        link.request = hang
        link.check_job = hang
        link.ensure_keeper = hang
        return hung

    def test_resume_on_new_job_as_fast_as_history(self):
        m = self.mgr()
        sid = self.start(m, jobid="123456")
        self.converse(m, sid)
        hung = self._end_old_job(m, sid)
        n_links = len(self.rem.links)
        t0 = time.monotonic()
        meta = m.restart(sid, jobid="421865")
        t_http = time.monotonic() - t0
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))
        t_ready = time.monotonic() - t0
        self.assertEqual(meta["op"]["kind"], "resume_new_job")
        self.assertNotIn("stop", [p["id"] for p in meta["op"]["phases"]])   # 旧 claude 已随作业退出，不再去停它
        self.assertEqual(hung, [])                                           # 没有对已结束的作业做任何操作
        self.assertEqual([lk.jobid for lk in self.rem.links[n_links:]], ["421865"])
        self.assertEqual(self.rem.ops("start")[-1]["args"], ["--resume", UUID1])
        # 对照：删掉后"从历史续接"同一个 claude 会话
        m.remove(sid)
        t1 = time.monotonic()
        c = m.create(jobid="421865", cwd="~", resume=UUID1)["sid"]
        self.assertTrue(wait_until(lambda: m.get_meta(c)["state"] == "idle", 5))
        t_hist = time.monotonic() - t1
        self.assertLess(t_http, 0.5)
        self.assertLess(t_ready, 1.5)
        self.assertLess(t_ready, t_hist + 1.0, (t_ready, t_hist))

    def test_old_job_cleanup_runs_in_background(self):
        """旧作业还没被判定结束（link 仍 ready 但已不响应）时，换作业续接也不等它的 stop_keeper。"""
        m = self.mgr()
        sid = self.start(m, jobid="123456")
        self.converse(m, sid)
        old = self.rem.links[0]
        orig = old.request

        def slow(op, timeout=30, **p):
            if op == "stop_keeper":
                time.sleep(3)
            return orig(op, timeout=timeout, **p)
        old.request = slow
        t0 = time.monotonic()
        m.restart(sid, jobid="421865")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 5))

    def test_reconnect_on_ended_job_after_app_restart(self):
        """app 重启后已结束作业上的会话只记得 link=down：点「重连」→ 查到作业已结束 → 进度写明并提示换作业。"""
        m = self.mgr(ended=("123456",))
        s = S._Session("s0123456789ab", 100)
        s.jobid, s.cwd, s.exited, s.state, s.claude_session_id = "123456", "~", True, "exited", UUID1
        with m._lock:
            m._sessions[s.sid] = s
        m.restart(s.sid)
        self.assertTrue(wait_until(lambda: (m.get_meta(s.sid)["op"] or {}).get("state") == "failed"))
        op = m.get_meta(s.sid)["op"]
        self.assertEqual(op["error_code"], "job_ended")
        self.assertIn("作业已结束", op["error"])


class TestPure(unittest.TestCase):
    def test_short_reason(self):
        sr = lambda t, **k: P.short_reason(t, host="gpu-cluster", jobid="123456", **k)  # noqa: E731
        self.assertEqual(sr("srun: error: Unable to confirm allocation for job 123456: Invalid job id specified"),
                         "srun 无法连接作业 123456：作业已结束")
        self.assertEqual(sr("ssh: connect to host x port 22: Operation timed out"), "ssh 连接 gpu-cluster 超时")
        self.assertEqual(sr("ssh: Could not resolve hostname gpu-cluster: nodename nor servname provided"),
                         "ssh 无法解析主机 gpu-cluster（请检查网络 / ssh 配置）")
        self.assertEqual(sr("Permission denied (publickey)."), "ssh 登录 gpu-cluster 被拒绝（Permission denied）")
        self.assertEqual(sr("kex_exchange_identification: Connection closed by remote host"),
                         "ssh 连接 gpu-cluster 被对方断开（登录节点繁忙？）")
        self.assertEqual(sr("srun: error: Slurm controller not responding, sleeping and retrying"),
                         "srun 报错：Slurm controller not responding, sleeping and retrying")
        self.assertLessEqual(len(sr("x" * 5000)), 160)

    def test_set_phase_inserts_and_marks_done(self):
        op = P.new_op(1, "restart", ["stop", "keeper", "start", "ready"], local=False, host="h", jobid="1", now=100)
        self.assertEqual(op["phase"], "stop")
        self.assertTrue(P.set_phase(op, "connect", now=101))      # 中途断线：插入"连接"阶段
        self.assertEqual([p["id"] for p in op["phases"]], ["stop", "connect", "keeper", "start", "ready"])
        self.assertEqual(P.phase(op, "stop")["state"], "done")
        self.assertEqual((op["phase"], op["phase_started_at"]), ("connect", 101))
        P.set_phase(op, "start", now=105)
        self.assertEqual([p["state"] for p in op["phases"]], ["done", "done", "done", "cur", "todo"])
        P.finish(op, "failed", error="e" * 500, now=106)
        self.assertEqual(P.phase(op, "start")["state"], "fail")
        self.assertLessEqual(len(op["error"]), P.MAX_ERR)
        self.assertFalse(P.set_phase(op, "ready"))                # 结束后不再推进
        pub = P.public(op)
        self.assertFalse(any(k.startswith("_") for k in pub))

    def test_describe_failure(self):
        d = lambda pid, raw, **k: P.describe_failure(pid, raw, host="gpu-cluster", jobid="123456",  # noqa: E731
                                                    local=k.pop("local", False), **k)
        self.assertEqual(d("connect", "连接计算节点超时（作业 123456）", timeout=90), "连接作业 123456 超时（已等 90 秒）")
        self.assertEqual(d("probe", "作业 123456 已结束，请在新的作业上启动/续接", code="job_ended"),
                         "srun 无法连接作业 123456：作业已结束（请在新作业上续接）")
        self.assertEqual(d("connect", "连接本机 agent 超时", timeout=90, local=True), "启动本机 agent 超时（已等 90 秒）")
        self.assertIn("tmux 常驻进程", d("keeper", "等待 tmux 常驻进程启动超时（120s）"))


class TestFakeHooks(unittest.TestCase):
    def test_parse_fake_delays(self):
        self.assertEqual(ccr_app.parse_fake_delays("probe=2,connect=6.5,keeper=x,boot=3,unreachable=777+abc+888,bad=1"),
                         {"probe": 2.0, "connect": 6.5, "boot": 3.0, "unreachable": ["777", "888"]})
        self.assertEqual(ccr_app.parse_fake_delays(""), {})

    def test_fake_ended_marker_only_in_fake_mode(self):
        d = tempfile.mkdtemp(prefix="ccr-op-")
        (pathlib.Path(d) / "fake").mkdir()
        (pathlib.Path(d) / "fake" / "ended_jobs").write_text("123456\n")
        cd = str(pathlib.Path(d) / "ssh")
        noop = lambda *a, **k: None  # noqa: E731
        lk = agent_link.AgentLink({"fake": True, "control_dir": cd}, "123456", noop, noop)
        self.assertEqual(lk.probe_job()["ended"], True)
        lk2 = agent_link.AgentLink({"fake": True, "control_dir": cd}, "421865", noop, noop)
        self.assertIsNone(lk2.probe_job()["ended"])
        # 真实模式不读这个文件（会去 ssh squeue；这里只确认 fake 分支不生效：_fake_delay 不等待）
        real = agent_link.AgentLink({"fake": False, "control_dir": cd, "fake_delays": {"probe": 5}}, "123456", noop, noop)
        t0 = time.monotonic()
        real._fake_delay("probe")
        self.assertLess(time.monotonic() - t0, 0.1)

    def test_fake_delay_waits_and_is_interruptible(self):
        noop = lambda *a, **k: None  # noqa: E731
        lk = agent_link.AgentLink({"fake": True, "fake_delays": {"keeper": 0.3}}, "1", noop, noop)
        t0 = time.monotonic()
        lk._fake_delay("keeper")
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        lk._stop.set()
        t0 = time.monotonic()
        lk._fake_delay("keeper")
        self.assertLess(time.monotonic() - t0, 0.1)

    def test_keeper_progress_callback(self):
        noop = lambda *a, **k: None  # noqa: E731
        lk = agent_link.AgentLink({"fake": True}, "1", noop, noop)
        lk.request = lambda op, timeout=30, **p: {"server_alive": True}
        notes = []
        lk.ensure_keeper(timeout=5, progress=notes.append)
        self.assertEqual(notes, ["检查 tmux 常驻进程是否在运行…"])


if __name__ == "__main__":
    unittest.main()
