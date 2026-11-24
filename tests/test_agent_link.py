"""AgentLink 测试：远端命令构造与引用（含真实 shell 执行）、keeper 命令、ensure_keeper 逻辑（mock），
fake 模式端到端（本机 tmux + node_agent + fake_tui）、断线重连、超时、close 不留子进程/线程。"""
import json
import os
import pathlib
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ccr import agent_link, remote
from tests.test_node_agent import HAVE_TMUX, NASTY, SID, FakeEnv, pid_alive

SRC = agent_link.agent_source()
TMUX_REMOTE = "/mnt/ssd/home/demouser/.local/x86_64/bin/tmux"


def link_threads():
    return [t for t in threading.enumerate() if t.name.startswith("ccr-agent")]


class CommandTests(unittest.TestCase):
    def test_agent_remote_command_roundtrip(self):
        cmd = agent_link.build_agent_remote_command("276403")
        outer = shlex.split(cmd)
        self.assertEqual(outer[:2], ["bash", "-lc"])
        self.assertEqual(len(outer), 3)
        inner = shlex.split(outer[2])
        self.assertEqual(inner, ["exec", "srun", "--jobid=276403", "--overlap", "-n1", "-N1", "-u", "-J", "ccr-agent",
                                 "python3", "-I", "-u", "-c", SRC])
        self.assertLess(len(cmd.encode()), agent_link.MAX_REMOTE_COMMAND)
        for bad in ("1;id", "$(id)", "1 2", "", "abc"):
            with self.assertRaises(ValueError):
                agent_link.build_agent_remote_command(bad)
        with self.assertRaises(ValueError):
            agent_link.build_agent_remote_command("1", "x" * 200000)
        # 多节点作业：srun -w <节点>（单节点不加，命令同上）
        inner = shlex.split(shlex.split(agent_link.build_agent_remote_command("276403", node="gpu0001"))[2])
        self.assertEqual(inner[:11], ["exec", "srun", "--jobid=276403", "--overlap", "-n1", "-N1", "-w", "gpu0001",
                                      "-u", "-J", "ccr-agent"])
        with self.assertRaises(ValueError):
            agent_link.build_agent_remote_command("1", node="h1;id")

    def test_nodelist_helpers(self):
        for nl, first, multi in (("gpu0001", "gpu0001", False), ("gpu[0001-0002]", "gpu0001", True),
                                 ("gpu[0001,0010]", "gpu0001", True), ("gpu[01-04],cpu7", "gpu01", True),
                                 ("gpu[0001]", "gpu0001", False), ("a,b", "a", True), ("(null)", None, None),
                                 ("", None, None)):
            self.assertEqual(agent_link.first_node(nl) if nl not in ("(null)", "") else None, first, nl)
            self.assertEqual(agent_link.is_multi_node(nl), multi, nl)

    def test_agent_argv_real_mode(self):
        cfg = {"host": "login-01", "ssh_multiplex": False}
        argv, env = agent_link.build_agent_argv(cfg, "276403")
        self.assertIsNone(env)  # 真实模式不传 env
        self.assertEqual(argv[:-1], remote.ssh_base_argv(cfg))
        self.assertEqual(argv[-1], agent_link.build_agent_remote_command("276403"))
        joined = " ".join(argv[:-1])
        for bad in ("SendEnv", "SetEnv", "CCR_", "HOME="):
            self.assertNotIn(bad, joined)

    def test_agent_command_runs_in_real_shell(self):
        """把内层命令交给真正的 bash 执行（srun/python3 用桩替换），确认源码经两层引用后原样到达。"""
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            srun = pathlib.Path(td, "srun")
            srun.write_text('#!/bin/sh\nwhile [ "$1" != python3 ]; do shift; done\nexec "$@"\n')
            srun.chmod(0o755)
            os.symlink(sys.executable, os.path.join(td, "python3"))
            inner = shlex.split(agent_link.build_agent_remote_command("123"))[2]
            env = {"PATH": td + ":/usr/bin:/bin", "HOME": td}
            p = subprocess.Popen(["bash", "-c", inner], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env)
            out, err = p.communicate(b'{"id":7,"op":"ping"}\n', timeout=20)
            self.assertEqual(p.returncode, 0, err)
            self.assertEqual(json.loads(out), {"id": 7, "ok": True, "result": {}})

    def test_keeper_command_matches_spec(self):
        cmd = agent_link.build_keeper_remote_command("276403", TMUX_REMOTE)
        outer = shlex.split(cmd)
        self.assertEqual(outer[:2], ["bash", "-lc"])
        inner = outer[2]
        t = TMUX_REMOTE + " -L ccr -f /dev/null"
        script = ('%s new-session -d -s _ccr_keeper "sleep 2147483647" || exit 0; i=0; while %s has-session -t '
                  "_ccr_keeper 2>/dev/null; do sleep 15; if s=$(%s list-sessions -F '#{session_name}' 2>/dev/null); "
                  "then if printf '%%s\\n' \"$s\" | grep -q '^ccr-'; then i=0; else i=$((i+15)); fi; "
                  'if [ "$i" -ge 600 ]; then %s kill-session -t _ccr_keeper; fi; fi; done' % (t, t, t, t))
        self.assertEqual(agent_link.keeper_script(TMUX_REMOTE), script)
        self.assertEqual(shlex.split(inner), [
            "setsid", "nohup", "srun", "--jobid=276403", "--overlap", "-n1", "-N1", "-J", "ccr-keeper",
            "bash", "-c", script, "</dev/null", ">/dev/null", "2>&1", "&"])
        # 多节点作业：-w 钉节点（agent 与 keeper 同一节点）
        inner_w = shlex.split(shlex.split(agent_link.build_keeper_remote_command("276403", TMUX_REMOTE,
                                                                                 node="gpu0002"))[2])
        self.assertEqual(inner_w[:9], ["setsid", "nohup", "srun", "--jobid=276403", "--overlap", "-n1", "-N1",
                                       "-w", "gpu0002"])
        for bad in ("a b", "-x", "n;id", "$(id)", ""):
            with self.assertRaises(ValueError):
                agent_link.build_keeper_remote_command("1", TMUX_REMOTE, node=bad)
        self.assertTrue(inner.endswith(" </dev/null >/dev/null 2>&1 &"))
        for bad in ("tmux", "/x;id", "/x/$(id)", "/x/`id`", "/x/'q'", "/x/../../bin/sh", "",
                    None, "/x\n"):
            with self.assertRaises(agent_link.AgentError, msg=repr(bad)):
                agent_link.build_keeper_remote_command("1", bad)
        with self.assertRaises(ValueError):
            agent_link.build_keeper_remote_command("1;id", TMUX_REMOTE)

    def test_fake_env(self):
        cfg = {"fake": True, "fake_home": "/tmp/fh", "fake_tmpdir": "/tmp/ft", "fake_tmux": "/x/tmux",
               "fake_env": {"CCR_FAKE_DELAY": "0", "PATH": "/evil", "CCR_FAKE_X": 1}}
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1/default,1,0", "CCR_OTHER": "x"}):
            env = agent_link.fake_env(cfg)
        self.assertEqual(env["CCR_FAKE"], "1")
        self.assertEqual(env["HOME"], os.path.realpath("/tmp/fh"))
        self.assertEqual(env["TMUX_TMPDIR"], os.path.realpath("/tmp/ft"))
        self.assertEqual(env["CCR_FAKE_TMUX"], "/x/tmux")
        self.assertEqual(env["CCR_FAKE_DELAY"], "0")
        self.assertNotIn("TMUX", env)
        self.assertNotIn("CCR_OTHER", env)
        self.assertNotIn("CCR_FAKE_X", env)
        self.assertNotEqual(env.get("PATH"), "/evil")
        self.assertEqual(shlex.split(env["CCR_CLAUDE_CMD"]), [sys.executable, "-I", str(agent_link.FAKE_TUI_PATH)])
        argv, env2 = agent_link.build_agent_argv(cfg, "1")
        self.assertEqual(argv, [sys.executable, "-I", "-u", str(agent_link.AGENT_PATH)])
        self.assertEqual(env2["CCR_FAKE"], "1")


class _ScriptConn:
    """替代 _Conn：按 op 返回脚本化结果。"""

    def __init__(self, hellos):
        self.hellos = list(hellos)
        self.calls = []
        self.dead = threading.Event()

    def call(self, op, params, timeout):
        self.calls.append(op)
        if op == "hello":
            return self.hellos.pop(0) if len(self.hellos) > 1 else self.hellos[0]
        return {}

    def close(self):
        pass


class EnsureKeeperRealModeTests(unittest.TestCase):
    CFG = {"host": "login-01", "ssh_multiplex": False}

    def make(self, hellos):
        link = agent_link.AgentLink(self.CFG, "276403", lambda e: None, lambda s, d: None)
        link._conn = _ScriptConn([{"server_alive": a, "tmux_path": TMUX_REMOTE} for a in hellos])
        link._ready = True
        link.state = "ready"
        return link

    def test_alive_no_ssh(self):
        link = self.make([True])
        with mock.patch.object(remote, "run_remote") as rr, mock.patch.object(remote, "_run_with_retry") as rw:
            link.ensure_keeper(5)
        rr.assert_not_called()
        rw.assert_not_called()

    def test_launch_when_not_alive(self):
        link = self.make([False, False, True])
        with mock.patch.object(remote, "run_remote", return_value=(0, "276403.0|bash\n", "")) as rr, \
                mock.patch.object(remote, "_run_with_retry", return_value=(0, "", "")) as rw:
            link.ensure_keeper(10)
        self.assertEqual(rr.call_args[0][1], ["squeue", "-h", "-s", "-j", "276403", "-o", "%i|%j"])
        argv = rw.call_args[0][0]
        self.assertEqual(argv[:-1], remote.ssh_base_argv(self.CFG))
        self.assertEqual(argv[-1], agent_link.build_keeper_remote_command("276403", TMUX_REMOTE))
        self.assertNotIn("env", rw.call_args[1])

    def test_existing_keeper_step_not_duplicated(self):
        link = self.make([False, True])
        with mock.patch.object(remote, "run_remote", return_value=(0, "276403.4|ccr-keeper\n", "")), \
                mock.patch.object(remote, "_run_with_retry") as rw:
            link.ensure_keeper(10)
        rw.assert_not_called()

    def test_launch_failure_and_timeout(self):
        link = self.make([False])
        with mock.patch.object(remote, "run_remote", return_value=(0, "", "")), \
                mock.patch.object(remote, "_run_with_retry", return_value=(1, "", "srun: error: Invalid job id")):
            with self.assertRaises(agent_link.AgentError) as cm:
                link.ensure_keeper(5)
        self.assertIn("Invalid job id", str(cm.exception))
        link = self.make([False])
        with mock.patch.object(remote, "run_remote", return_value=(0, "", "")), \
                mock.patch.object(remote, "_run_with_retry", return_value=(0, "", "")):
            t0 = time.time()
            with self.assertRaises(agent_link.AgentError) as cm:
                link.ensure_keeper(1.5)
            self.assertLess(time.time() - t0, 6)
        self.assertIn("超时", str(cm.exception))

    def test_not_connected(self):
        link = agent_link.AgentLink(self.CFG, "276403", lambda e: None, lambda s, d: None)
        self.assertEqual(link.state, "down")
        with self.assertRaises(agent_link.AgentDown):
            link.request("hello")
        with self.assertRaises(agent_link.AgentDown):
            link.ensure_keeper(1)
        self.assertTrue(issubclass(agent_link.AgentDown, agent_link.AgentError))
        link.close()


class StubAgentTests(unittest.TestCase):
    """用桩 agent 脚本替换 node_agent.py，测试超时、连接失败重试、stderr 进入错误信息。"""

    def run_stub(self, code, **cfgx):
        td = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        stub = pathlib.Path(td, "stub.py")
        stub.write_text(code)
        states = []
        cfg = {"fake": True, "fake_home": td + "/h", "fake_tmpdir": td + "/t"}
        cfg.update(cfgx)
        p = mock.patch.object(agent_link, "AGENT_PATH", stub)
        p.start()
        self.addCleanup(p.stop)
        link = agent_link.AgentLink(cfg, "1", lambda e: None, lambda s, d: states.append((s, d)))
        link.BACKOFF_FIRST = 0.2
        self.addCleanup(link.close)
        link.start()
        return link, states

    def test_request_timeout_is_agent_error(self):
        code = ("import sys, json\nfor line in sys.stdin:\n    m = json.loads(line)\n"
                "    if m['op'] == 'hello':\n        print(json.dumps({'id': m['id'], 'ok': True, 'result': {}}),"
                " flush=True)\n")
        link, states = self.run_stub(code)
        self.assertTrue(link.wait_ready(10))
        t0 = time.time()
        with self.assertRaises(agent_link.AgentError) as cm:
            link.request("keys", timeout=0.5, sid=SID, keys=["Enter"])
        self.assertNotIsInstance(cm.exception, agent_link.AgentDown)
        self.assertIn("超时", str(cm.exception))
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(link.state, "ready")

    def test_connect_failure_retries_with_stderr(self):
        code = "import sys\nsys.stderr.write('srun: error: Unable to confirm allocation\\n')\nsys.exit(1)\n"
        link, states = self.run_stub(code)
        deadline = time.time() + 10
        while time.time() < deadline and len([s for s in states if s[0] == "down"]) < 2:
            time.sleep(0.05)
        downs = [d for s, d in states if s == "down"]
        self.assertGreaterEqual(len(downs), 2)
        self.assertIn("Unable to confirm allocation", downs[0])
        self.assertEqual(states[0], ("connecting", None))
        self.assertFalse(link.wait_ready(0.1))
        t0 = time.time()
        link.close()
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(link_threads(), [])
        self.assertEqual(states[-1][0], "down")
        self.assertEqual(link.state, "down")


class SlowBlobLinkTests(unittest.TestCase):
    """传一条很大的 blob 响应（链路慢）时：ping 响应排在后面也不算断开；长行跨多个读块能完整切出。"""

    STUB = r"""
import sys, json, time
out = sys.stdout.buffer
for line in sys.stdin:
    m = json.loads(line)
    if m['op'] == 'hello':
        out.write((json.dumps({'id': m['id'], 'ok': True, 'result': {}}) + '\n').encode()); out.flush()
    elif m['op'] == 'blob':
        # 先在同一块里夹一个事件，再用 ~1.2s 慢慢写出一条 3MB 的响应（模拟慢链路；真实 agent 的输出锁同样串行）
        data = ('{"ev":"screen","sid":"x","text":"t"}\n' + json.dumps({'id': m['id'], 'ok': True, 'result': {'data': 'A' * 3000000}}) + '\n').encode()
        step = len(data) // 24 + 1
        for i in range(0, len(data), step):
            out.write(data[i:i + step]); out.flush(); time.sleep(0.05)
    elif m['op'] == 'ping':
        out.write((json.dumps({'id': m['id'], 'ok': True, 'result': {}}) + '\n').encode()); out.flush()
"""

    def test_ping_not_broken_while_large_response_streams(self):
        td = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        stub = pathlib.Path(td, "stub.py")
        stub.write_text(self.STUB)
        states, events = [], []
        p = mock.patch.object(agent_link, "AGENT_PATH", stub)
        p.start()
        self.addCleanup(p.stop)
        link = agent_link.AgentLink({"fake": True, "fake_home": td + "/h", "fake_tmpdir": td + "/t"}, "1",
                                    events.append, lambda s, d: states.append((s, d)))
        link.PING_INTERVAL = 0.15
        link.PING_TIMEOUT = 0.3
        self.addCleanup(link.close)
        link.start()
        self.assertTrue(link.wait_ready(10))
        t0 = time.time()
        res = link.request("blob", timeout=20, sid=SID, ref="x")
        self.assertGreater(time.time() - t0, 0.8)                     # 确实传了一阵子，期间 ping 超时多次
        self.assertEqual(len(res["data"]), 3000000)                    # 长行跨多个读块完整切出
        self.assertEqual([e.get("ev") for e in events], ["screen"])    # 同一块里的事件也分发了
        self.assertEqual([s for s, _ in states if s != "connecting"], ["ready"])   # 没有因为 ping 超时而断开重连
        self.assertEqual(link.state, "ready")
        link.request("ping", timeout=5)


@unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
class FakeEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.fe = FakeEnv()
        self.addCleanup(self.fe.cleanup, self)
        self.events = []
        self.states = []
        self.lock = threading.Lock()

    def on_event(self, e):
        with self.lock:
            self.events.append(e)

    def on_state(self, s, d):
        self.states.append((s, d))

    def wait_event(self, pred, timeout=15, start=0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for e in self.events[start:]:
                    if pred(e):
                        return e
            time.sleep(0.05)
        raise AssertionError("等待事件超时")

    def test_link_end_to_end_reconnect_close(self):
        link = agent_link.AgentLink(self.fe.cfg, "123", self.on_event, self.on_state)
        link.BACKOFF_FIRST = 0.3
        self.addCleanup(link.close)
        link.start()
        self.assertTrue(link.wait_ready(20))
        self.assertEqual(self.states[:2], [("connecting", None), ("ready", None)])
        self.assertEqual(link.hello["server_alive"], False)
        link.ensure_keeper(20)
        self.fe.track_server()
        self.assertTrue(link.request("hello")["server_alive"])
        link.ensure_keeper(5)  # 已在运行：不重复启动
        self.assertEqual(link.request("hello")["sessions"], ["_ccr_keeper"])

        args = remote.build_claude_args(model="haiku", permission_mode="auto", effort="medium")
        proj = os.path.join(self.fe.env["HOME"], "p q")
        os.makedirs(proj)
        r = link.request("start", sid=SID, cwd="~/p q", args=args)
        self.fe.pids.add(r["pane_pid"])
        link.request("watch", sid=SID, on=True)
        self.wait_event(lambda e: e["ev"] == "screen" and "⏵⏵ auto mode on" in e["text"]
                        and "effort:medium" in e["text"])
        with self.assertRaises(agent_link.AgentError) as cm:
            link.request("keys", sid=SID, keys=["q"])
        self.assertIn("不允许的按键", str(cm.exception))
        with self.assertRaises(agent_link.AgentError):
            link.request("bogus")
        link.request("paste", sid=SID, text=NASTY, enter=True)
        self.wait_event(lambda e: e["ev"] == "transcript" and any(
            x["type"] == "user" and x["message"]["content"] == NASTY for x in e["entries"]))

        # 断线：杀掉 agent → down → 自动重连 → ready；被监视的会话自动恢复 watch（reset 重发 transcript）
        old = link._conn.proc
        with self.lock:
            mark = len(self.events)
        old.kill()
        deadline = time.time() + 15
        while time.time() < deadline and self.states.count(("ready", None)) < 2:
            time.sleep(0.05)
        self.assertEqual(self.states.count(("ready", None)), 2)
        self.assertTrue(any(s == "down" and d for s, d in self.states))
        self.wait_event(lambda e: e["ev"] == "transcript" and e["reset"] and any(
            x["type"] == "user" and x["message"]["content"] == NASTY for x in e["entries"]), start=mark)
        self.wait_event(lambda e: e["ev"] == "screen" and e["sid"] == SID, start=mark)
        self.assertEqual(link.request("list")[0]["sid"], SID)  # claude 未受断线影响

        # close：不留子进程/线程；远端（本机 tmux）会话不受影响
        proc = link._conn.proc
        link.close()
        self.assertEqual(link.state, "down")
        self.assertIsNotNone(proc.poll())
        self.assertIsNotNone(old.poll())
        self.assertEqual(link_threads(), [])
        self.assertTrue(pid_alive(r["pane_pid"]))
        with self.assertRaises(agent_link.AgentDown):
            link.request("hello")

    def test_fake_keeper_files_only_in_fake_dirs(self):
        link = agent_link.AgentLink(self.fe.cfg, "123", self.on_event, self.on_state)
        self.addCleanup(link.close)
        link.start()
        self.assertTrue(link.wait_ready(20))
        link.ensure_keeper(20)
        self.fe.track_server()
        sock = os.path.join(self.fe.env["TMUX_TMPDIR"], "tmux-%d" % os.getuid(), "ccr")
        self.assertTrue(stat.S_ISSOCK(os.stat(sock).st_mode))
        self.assertEqual(link.request("stop_keeper"), {"stopped": True, "remaining": 0})
        deadline = time.time() + 5
        while self.fe.server_alive() and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.fe.server_alive())


class JobStateTests(unittest.TestCase):
    """高 1 / 低 8：作业已结束（squeue 空输出 / Invalid job id / srun "has expired"）→ ended，不再无限重连。"""
    CFG = {"host": "gpu-cluster", "ssh_multiplex": False}

    def link(self, states=None):
        st = states if states is not None else []
        link = agent_link.AgentLink(self.CFG, "276403", lambda e: None, lambda s, d: st.append((s, d)))
        link.BACKOFF_FIRST = 0.05
        self.addCleanup(link.close)
        return link

    def test_probe_job(self):
        cases = [((0, "", ""), True), ((0, "RUNNING|gpu0001\n", ""), False), ((0, "PENDING|\n", ""), False),
                 ((0, "COMPLETING|gpu0001\n", ""), True), ((0, "TIMEOUT|gpu0001\n", ""), True),
                 ((0, "motd banner\n", ""), True),
                 ((1, "", "slurm_load_jobs error: Invalid job id specified"), True),
                 ((255, "", "ssh: connect to host x port 22: Connection refused"), None),
                 ((1, "", "slurm_load_jobs error: Unable to contact slurm controller"), None)]
        for ret, want in cases:
            link = self.link()
            with mock.patch.object(remote, "run_remote", return_value=ret) as rr:
                self.assertEqual(link.probe_job()["ended"], want, ret)
            self.assertEqual(rr.call_args[0][1], ["squeue", "-h", "-j", "276403", "-o", "%T|%N"])
        # squeue 不可用时退而看 srun 的真实报错文本
        link = self.link()
        with mock.patch.object(remote, "run_remote", return_value=(255, "", "ssh 失败")):
            for hint in ("srun: error: Slurm job 276403 has expired",
                         "srun: Check SLURM_JOB_ID environment variable. Expired or invalid job 276403"):
                self.assertTrue(link.probe_job(hint=hint)["ended"], hint)
        # 以 squeue 为准：作业还在 RUNNING 时，即使 srun 报错文本像"已结束"也不判结束
        with mock.patch.object(remote, "run_remote", return_value=(0, "RUNNING|h1\n", "")):
            self.assertFalse(link.probe_job(hint="Expired or invalid job 276403")["ended"])

    def test_multi_node_pinning(self):
        link = self.link()
        with mock.patch.object(remote, "run_remote", return_value=(0, "RUNNING|gpu[0001-0002]\n", "")):
            link.probe_job()
        self.assertTrue(link.multi_node)
        self.assertEqual(link.node, "gpu0001")
        seen = []

        def fake_argv(cfg, jobid, node=None):
            seen.append(node)
            raise OSError("测试：不真的连接")
        with mock.patch.object(agent_link, "build_agent_argv", fake_argv):
            with self.assertRaises(OSError):
                link._connect()
        self.assertEqual(seen, ["gpu0001"])
        self.assertEqual(link._keeper_node({"slurm_node": "gpu0001", "node": "gpu0001.cluster"}), "gpu0001")
        self.assertEqual(link._keeper_node({"node": "gpu0002.cluster"}), "gpu0002")
        # 单节点作业：不加 -w（命令与以前相同）
        single = self.link()
        with mock.patch.object(remote, "run_remote", return_value=(0, "RUNNING|gpu0001\n", "")):
            single.probe_job()
        self.assertIs(single.multi_node, False)
        self.assertIsNone(single._keeper_node({"slurm_node": "gpu0001"}))
        seen.clear()
        with mock.patch.object(agent_link, "build_agent_argv", fake_argv):
            with self.assertRaises(OSError):
                single._connect()
        self.assertEqual(seen, [None])

    def test_ended_stops_reconnect(self):
        states = []
        link = self.link(states)
        calls = []

        def connect():
            calls.append(1)
            raise agent_link.AgentError("srun: error: Slurm job 276403 has expired")
        squeue = [(0, "RUNNING|gpu0001\n", ""), (1, "", "slurm_load_jobs error: Invalid job id specified")]
        with mock.patch.object(link, "_connect", connect), \
                mock.patch.object(link, "_squeue_job", side_effect=lambda: squeue.pop(0) if len(squeue) > 1
                                  else squeue[0]):
            link.start()
            deadline = time.time() + 5
            while time.time() < deadline and link._thread.is_alive():
                time.sleep(0.02)
        self.assertFalse(link._thread.is_alive(), "作业结束后应停止重连")
        self.assertEqual(link.state, "ended")
        self.assertTrue(link.ended)
        self.assertEqual(len(calls), 1)
        self.assertEqual(states[-1][0], "ended")
        self.assertIn("276403", states[-1][1])
        self.assertIn("has expired", states[-1][1])
        with self.assertRaises(agent_link.AgentDown) as cm:
            link.request("hello")
        self.assertIn("已结束", str(cm.exception))
        self.assertTrue(link.check_job())

    def test_alive_but_unreachable_keeps_retrying(self):
        states = []
        link = self.link(states)

        def connect():
            raise agent_link.AgentError("ssh: Connection timed out")
        with mock.patch.object(link, "_connect", connect), \
                mock.patch.object(link, "_squeue_job", return_value=(255, "", "ssh: Connection timed out")):
            link.start()
            deadline = time.time() + 5
            while time.time() < deadline and len([s for s, _ in states if s == "down"]) < 3:
                time.sleep(0.02)
            self.assertGreaterEqual(len([s for s, _ in states if s == "down"]), 3)
            self.assertFalse(link.ended)
            self.assertIsNone(link.check_job())
            # 用户操作时同步查询：查到作业已结束 → ended，重连循环随之停止
            with mock.patch.object(link, "_squeue_job", return_value=(0, "", "")):
                self.assertTrue(link.check_job())
            deadline = time.time() + 5
            while time.time() < deadline and link._thread.is_alive():
                time.sleep(0.02)
        self.assertFalse(link._thread.is_alive())
        self.assertEqual(link.state, "ended")

    def test_fake_mode_never_probes(self):
        link = agent_link.AgentLink({"fake": True}, "1", lambda e: None, lambda s, d: None)
        with mock.patch.object(remote, "run_remote") as rr:
            self.assertEqual(link.probe_job(), {"ended": None, "state": None, "nodes": None})
            self.assertIsNone(link.check_job())
        rr.assert_not_called()


@unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
class KeeperScriptTests(unittest.TestCase):
    """低 8：keeper 脚本在真实 bash + 本机 tmux 下的行为（空闲自停、重复 keeper 立即退出）。"""

    def setUp(self):
        from tests.test_node_agent import TMUX
        self.T = TMUX
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccrks", dir="/tmp"))
        self.env = dict(os.environ, TMUX_TMPDIR=self.tmp)
        self.env.pop("TMUX", None)
        self.procs = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.tmux("kill-server")
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(5)
        __import__("shutil").rmtree(self.tmp, ignore_errors=True)

    def tmux(self, *args):
        return subprocess.run([self.T, "-L", "ccr", "-f", "/dev/null"] + list(args), env=self.env,
                              capture_output=True, text=True, timeout=10)

    def run_keeper(self, idle_exit=2, poll=1):
        p = subprocess.Popen(["bash", "-c", agent_link.keeper_script(self.T, idle_exit=idle_exit, poll=poll)],
                             env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def wait(self, pred, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.1)
        return pred()

    def test_idle_exit_and_duplicate(self):
        k1 = self.run_keeper(idle_exit=3, poll=1)
        self.assertTrue(self.wait(lambda: self.tmux("has-session", "-t", "_ccr_keeper").returncode == 0))
        # 有 ccr- 会话时不自停
        self.assertEqual(self.tmux("new-session", "-d", "-s", "ccr-s0123456789ab", "sleep", "60").returncode, 0)
        # 重复启动的 keeper（duplicate session）立即退出，不留第二个空转的 step
        k2 = self.run_keeper()
        self.assertEqual(k2.wait(10), 0)
        time.sleep(4.5)
        self.assertIsNone(k1.poll(), "有 ccr- 会话时 keeper 不应退出")
        # 没有 ccr- 会话且持续空闲 → 自己杀掉 _ccr_keeper，脚本结束
        self.tmux("kill-session", "-t", "ccr-s0123456789ab")
        self.assertEqual(k1.wait(15), 0)
        self.assertNotEqual(self.tmux("has-session", "-t", "_ccr_keeper").returncode, 0)


if __name__ == "__main__":
    unittest.main()
