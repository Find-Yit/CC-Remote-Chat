"""本机运行位置（SPEC §9）测试：目标标识校验、本机 agent 命令/环境、node_agent --local 的命令构造、
本机 keeper（start_new_session、env 不含 CCR_、bash 3.2）、不查 squeue、历史/目录的 target 路由、
server 的 ?target= 参数、SessionManager 的本机会话，以及 fake 模式的本机端到端。

红线：不运行真实 claude、不启动 `zsh -lic 'exec claude'`、不碰真实的 /tmp/tmux-<uid>/ccr 套接字
（所有用到 tmux 的测试都用 /tmp 下的临时 TMUX_TMPDIR），不读 ~/.cc-remote-chat。
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ccr import agent_link, history as H, node_agent, remote, session as S, stats, tui_parse
from tests.test_agent_link import TMUX_REMOTE, _ScriptConn
from tests.test_node_agent import HAVE_TMUX, SID, TMUX
from tests.test_session import Base as SessionBase, UUID1, UUID2, wait_until
from tests.test_server import Base as ServerBase, StubHistory, UUID

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SOCKET_DIR = "/tmp/tmux-%d" % os.getuid()


def _real_socket_exists():
    return os.path.exists(os.path.join(REAL_SOCKET_DIR, "ccr"))


# ============================================================================ 目标标识
class TestTargetValidate(unittest.TestCase):
    def test_validate_target(self):
        self.assertEqual(remote.LOCAL_JOBID, "local")
        self.assertEqual(remote.validate_target("local"), "local")
        self.assertEqual(remote.validate_target(" local "), "local")
        self.assertEqual(remote.validate_target("276403"), "276403")
        self.assertEqual(remote.validate_target("12_3"), "12_3")
        for bad in ("", "Local", "LOCAL", "localhost", "loc", "1;id", "$(id)", "1 2", "lo cal", "local;id", None, 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                remote.validate_target(bad)
        self.assertTrue(remote.is_local("local"))
        for x in ("276403", "", None, "Local"):
            self.assertFalse(remote.is_local(x))

    def test_validate_jobid_still_strict(self):
        """srun/squeue 只用 validate_jobid：本机标识绝不能当成 Slurm 作业号。"""
        with self.assertRaises(ValueError):
            remote.validate_jobid("local")
        with self.assertRaises(ValueError):
            agent_link.build_agent_remote_command("local")
        with self.assertRaises(ValueError):
            agent_link.build_keeper_remote_command("local", TMUX_REMOTE)


# ============================================================================ 本机 agent 命令 / 环境
class TestLocalAgentArgv(unittest.TestCase):
    ENV = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3", "CCR_FAKE": "1", "CCR_CLAUDE_CMD": "x",
           "CCR_ANYTHING": "y", "PATH": "/opt/homebrew/bin:/usr/bin:/bin", "LANG": "zh_CN.UTF-8",
           "SHELL": "/bin/zsh", "HOME": "/Users/someone", "TMUX_TMPDIR": "/tmp/mine"}

    def test_real_local_argv_and_env(self):
        cfg = {"host": "gpu-cluster", "ssh_multiplex": True}
        with mock.patch.dict(os.environ, self.ENV, clear=True):
            argv, env = agent_link.build_agent_argv(cfg, "local")
        self.assertEqual(argv, [sys.executable, "-I", "-u", str(agent_link.AGENT_PATH), "--local"])
        self.assertNotIn("ssh", argv)
        # env = 白名单（HOME/SHELL/LANG…）+ 系统默认 PATH；TMUX*/CCR_* 等一律不带
        self.assertEqual(env, {"HOME": "/Users/someone", "SHELL": "/bin/zsh", "LANG": "zh_CN.UTF-8",
                               "PATH": agent_link.LOCAL_BASE_PATH})
        self.assertFalse([k for k in env if k.startswith("CCR_")])

    def test_local_env_whitelist(self):
        env_in = dict(self.ENV, CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="x", CLAUDE_CODE_MESSAGING_SOCKET="/tmp/s",
                      ANTHROPIC_AUTH_TOKEN="sk-test", AI_AGENT="claude-code", TERM_PROGRAM="Apple_Terminal",
                      HOME="/Users/u", USER="u", SHELL="/bin/zsh", LANG="zh_CN.UTF-8", PATH="/a:/b")
        with mock.patch.dict(os.environ, env_in, clear=True):
            env = agent_link.local_env()
        for k in env:
            self.assertTrue(k in agent_link.LOCAL_ENV_KEEP or k in ("PATH", "LANG") or k.startswith("LC_"), k)
        for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "ANTHROPIC_AUTH_TOKEN",
                  "AI_AGENT", "TERM_PROGRAM", "TMUX", "TMUX_PANE", "CCR_FAKE"):
            self.assertNotIn(k, env)
        self.assertEqual(env["PATH"], agent_link.LOCAL_BASE_PATH)
        self.assertEqual(env["LANG"], "zh_CN.UTF-8")
        self.assertEqual(env["HOME"], "/Users/u")

    def test_local_env_adds_utf8_lang_when_missing(self):
        with mock.patch.dict(os.environ, {"HOME": "/Users/u"}, clear=True), \
                mock.patch.object(agent_link, "_system_locale", return_value="zh_CN.UTF-8"):
            env = agent_link.local_env()
        self.assertEqual(env["LANG"], "zh_CN.UTF-8")

    def test_fake_local_argv_and_env(self):
        base = tempfile.mkdtemp(prefix="ccrlo", dir="/tmp")
        self.addCleanup(shutil.rmtree, base, True)
        cfg = {"fake": True, "fake_home": base + "/home", "fake_tmpdir": base + "/t", "fake_tmux": TMUX}
        argv, env = agent_link.build_agent_argv(cfg, "local")
        self.assertEqual(argv, [sys.executable, "-I", "-u", str(agent_link.AGENT_PATH), "--local"])
        self.assertEqual(env["CCR_FAKE"], "1")
        # fake 的"本机"是单独一套 HOME / TMUX_TMPDIR（与 fake 服务器同级目录，测试里都在 /tmp 下）
        self.assertEqual(env["HOME"], os.path.realpath(base + "/local-home"))
        self.assertEqual(env["TMUX_TMPDIR"], os.path.realpath(base + "/local-tmux"))
        self.assertEqual(remote.fake_home(cfg, local=True), env["HOME"])
        self.assertEqual(remote.fake_tmpdir(cfg, local=True), env["TMUX_TMPDIR"])
        self.assertEqual(remote.fake_home(cfg), os.path.realpath(base + "/home"))
        # 显式配置优先
        cfg2 = dict(cfg, fake_local_home=base + "/lh", fake_local_tmpdir=base + "/lt")
        self.assertEqual(remote.fake_home(cfg2, local=True), os.path.realpath(base + "/lh"))
        self.assertEqual(remote.fake_tmpdir(cfg2, local=True), os.path.realpath(base + "/lt"))
        # fake 服务器作业的命令不变（不带 --local）
        argv2, env2 = agent_link.build_agent_argv(cfg, "123")
        self.assertEqual(argv2, [sys.executable, "-I", "-u", str(agent_link.AGENT_PATH)])
        self.assertEqual(env2["HOME"], os.path.realpath(base + "/home"))


# ============================================================================ 本机 link：不查 squeue、不跑 ssh
class TestLocalLinkNoSlurm(unittest.TestCase):
    CFG = {"host": "gpu-cluster", "ssh_multiplex": False}

    def make(self, hellos=(True,)):
        link = agent_link.AgentLink(self.CFG, "local", lambda e: None, lambda s, d: None)
        link._conn = _ScriptConn([{"server_alive": a, "tmux_path": TMUX, "local": True} for a in hellos])
        link._ready = True
        link.state = "ready"
        return link

    def test_never_ssh(self):
        boom = mock.Mock(side_effect=AssertionError("本机不应 ssh / squeue"))
        link = self.make()
        self.assertTrue(link.local)
        self.assertEqual(link.jobid, "local")
        with mock.patch.object(remote, "run_remote", boom), mock.patch.object(remote, "_run_with_retry", boom), \
                mock.patch.object(remote, "ssh_base_argv", boom):
            self.assertEqual(link.probe_job(), {"ended": False, "state": None, "nodes": None})
            self.assertEqual(link.probe_job(hint="srun: error: Slurm job 1 has expired"),
                             {"ended": False, "state": None, "nodes": None})
            self.assertIs(link.check_job(), False)
            self.assertEqual(link._keeper_steps(), [])
            self.assertIsNone(link._squeue_job())
            link.ensure_keeper(5)   # server_alive → 直接返回
        self.assertFalse(link.ended)
        self.assertNotEqual(link.state, "ended")

    def test_messages_say_local(self):
        link = agent_link.AgentLink(self.CFG, "local", lambda e: None, lambda s, d: None)
        with self.assertRaises(agent_link.AgentDown) as cm:
            link.request("hello")
        self.assertIn("本机", str(cm.exception))
        self.assertNotIn("计算节点", str(cm.exception))
        link.close()

    def test_run_loop_never_ends(self):
        """连不上时：不会进入 ended（没有作业可结束），只会退避重连。"""
        states = []
        link = agent_link.AgentLink(self.CFG, "local", lambda e: None, lambda s, d: states.append((s, d)))
        link.BACKOFF_FIRST = 0.1
        boom = mock.Mock(side_effect=AssertionError("本机不应 ssh / squeue"))
        with mock.patch.object(agent_link, "build_agent_argv",
                               return_value=([sys.executable, "-I", "-c", "import sys; sys.exit(3)"], None)), \
                mock.patch.object(remote, "run_remote", boom):
            link.start()
            self.assertTrue(wait_until(lambda: len([s for s, _ in states if s == "down"]) >= 2, 10))
            link.close()
        self.assertFalse([s for s, _ in states if s == "ended"])
        downs = [d for s, d in states if s == "down" and d]
        self.assertTrue(downs and "本机" in downs[0], downs)


# ============================================================================ 本机 keeper
class TestLocalKeeperLaunch(unittest.TestCase):
    CFG = {"host": "gpu-cluster", "ssh_multiplex": False}

    def test_keeper_popen_new_session_clean_env(self):
        link = agent_link.AgentLink(self.CFG, "local", lambda e: None, lambda s, d: None)
        link._conn = _ScriptConn([{"server_alive": a, "tmux_path": TMUX} for a in (False, False, True)])
        link._ready = True
        link.state = "ready"
        proc = mock.Mock()
        proc.wait.return_value = 0
        boom = mock.Mock(side_effect=AssertionError("本机 keeper 不应 ssh / squeue"))
        env = {"PATH": "/usr/bin:/bin", "HOME": "/Users/x", "TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%1",
               "CCR_FAKE": "1", "CCR_X": "y", "LANG": "zh_CN.UTF-8"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(agent_link.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(remote, "run_remote", boom), mock.patch.object(remote, "_run_with_retry", boom):
            link.ensure_keeper(10)
        self.assertEqual(popen.call_count, 1)
        args, kw = popen.call_args
        self.assertEqual(args[0], ["/bin/bash", "-c", agent_link.keeper_script(TMUX)])
        self.assertIs(kw.get("start_new_session"), True)
        self.assertFalse(kw.get("shell"))
        for k in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw.get(k), subprocess.DEVNULL, k)
        self.assertEqual(kw.get("cwd"), "/Users/x")
        self.assertEqual(kw["env"], {"PATH": agent_link.LOCAL_BASE_PATH, "HOME": "/Users/x", "LANG": "zh_CN.UTF-8"})
        self.assertFalse([k for k in kw["env"] if k.startswith("CCR_") or k.startswith("TMUX")])
        self.assertTrue(wait_until(lambda: proc.wait.called, 2))   # 后台线程回收 keeper，避免僵尸进程

    def test_keeper_bad_tmux_path(self):
        link = agent_link.AgentLink(self.CFG, "local", lambda e: None, lambda s, d: None)
        link._conn = _ScriptConn([{"server_alive": False, "tmux_path": "/x;id"}])
        link._ready = True
        link.state = "ready"
        with mock.patch.object(agent_link.subprocess, "Popen") as popen:
            with self.assertRaises(agent_link.AgentError):
                link.ensure_keeper(2)
        popen.assert_not_called()

    def test_keeper_timeout_message(self):
        link = agent_link.AgentLink(self.CFG, "local", lambda e: None, lambda s, d: None)
        link._conn = _ScriptConn([{"server_alive": False, "tmux_path": TMUX}])
        link._ready = True
        link.state = "ready"
        with mock.patch.object(agent_link.subprocess, "Popen", return_value=mock.Mock()):
            with self.assertRaises(agent_link.AgentError) as cm:
                link.ensure_keeper(1.2)
        self.assertIn("本机", str(cm.exception))
        self.assertNotIn("作业", str(cm.exception))


@unittest.skipUnless(HAVE_TMUX and os.path.exists("/bin/bash"), "需要本机 tmux 与 /bin/bash")
class TestLocalKeeperRealBash(unittest.TestCase):
    """keeper_script 在 macOS 自带 /bin/bash（3.2）+ 本机 tmux 下的行为：start_new_session 后台运行、
    空闲自停、重复启动立即退出。临时 TMUX_TMPDIR，绝不碰真实的 /tmp/tmux-<uid>/ccr。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccrlk", dir="/tmp"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX") and not k.startswith("CCR_")}
        self.env["TMUX_TMPDIR"] = self.tmp
        self.procs = []
        self.real_before = _real_socket_exists()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if os.path.isdir(self.tmp):   # 目录不在时绝不调用 tmux（会退回 /tmp 打到真实套接字）
            self.tmux("kill-server")
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(5)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(_real_socket_exists(), self.real_before, "不应在真实套接字上起 tmux server")

    def tmux(self, *args):
        return subprocess.run([TMUX, "-L", "ccr", "-f", "/dev/null"] + list(args), env=self.env,
                              capture_output=True, text=True, timeout=10)

    def run_keeper(self, idle_exit, poll):
        p = subprocess.Popen([agent_link.LOCAL_KEEPER_SHELL, "-c",
                              agent_link.keeper_script(TMUX, idle_exit=idle_exit, poll=poll)],
                             env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        return p

    def test_bash32_keeper(self):
        self.assertEqual(agent_link.LOCAL_KEEPER_SHELL, "/bin/bash")
        k1 = self.run_keeper(3, 1)
        self.assertTrue(wait_until(lambda: self.tmux("has-session", "-t", "_ccr_keeper").returncode == 0, 10))
        self.assertNotEqual(os.getpgid(k1.pid), os.getpgid(0), "keeper 应在自己的会话/进程组里")
        self.assertEqual(self.tmux("new-session", "-d", "-s", "ccr-s0123456789ab", "sleep", "60").returncode, 0)
        k2 = self.run_keeper(600, 15)
        self.assertEqual(k2.wait(10), 0)   # 已有 keeper：立即退出
        time.sleep(3.5)
        self.assertIsNone(k1.poll(), "有 ccr- 会话时 keeper 不应退出")
        self.tmux("kill-session", "-t", "ccr-s0123456789ab")
        self.assertEqual(k1.wait(15), 0)
        self.assertNotEqual(self.tmux("has-session", "-t", "_ccr_keeper").returncode, 0)


# ============================================================================ node_agent --local
class TestNodeAgentLocal(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(node_agent, "LOCAL", True), mock.patch.object(node_agent, "FAKE", False)):
            p.start()
            self.addCleanup(p.stop)

    def shells(self, *names):
        return mock.patch.object(node_agent, "_listed_shells", return_value=set(names))

    def test_claude_command_local(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}), self.shells("/bin/zsh", "/bin/bash"):
            self.assertEqual(node_agent.claude_command(),
                             ["/bin/zsh", "-l", "-i", "-c", 'exec claude "$@"', "claude"])
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}), self.shells("/bin/zsh", "/bin/bash"):
            self.assertEqual(node_agent.claude_command()[0], "/bin/bash")

    def test_local_shell_validation(self):
        cases = [
            ({"SHELL": "/bin/zsh"}, ("/bin/zsh",), "/bin/zsh"),
            ({"SHELL": "/bin/bash"}, ("/bin/zsh",), "/bin/zsh"),          # 不在 /etc/shells
            ({"SHELL": "zsh"}, ("zsh", "/bin/zsh"), "/bin/zsh"),           # 不是绝对路径
            ({}, ("/bin/zsh",), "/bin/zsh"),                               # 没有 $SHELL
            ({"SHELL": "/opt/homebrew/bin/fish"}, ("/opt/homebrew/bin/fish",), "/bin/zsh"),   # 非 POSIX
            ({"SHELL": "/nonexistent/zsh"}, ("/nonexistent/zsh",), "/bin/zsh"),               # 不存在
            ({"SHELL": "/bin/zsh; id"}, ("/bin/zsh; id",), "/bin/zsh"),
        ]
        for env, listed, want in cases:
            with mock.patch.dict(os.environ, env, clear=True), self.shells(*listed):
                self.assertEqual(node_agent.local_shell(), want, (env, listed))

    def test_listed_shells_reads_etc_shells(self):
        got = node_agent._listed_shells()
        if os.path.exists("/etc/shells"):
            self.assertIn("/bin/zsh" if sys.platform == "darwin" else "/bin/sh", got)
        self.assertFalse([x for x in got if x.startswith("#")])

    def test_remote_and_fake_unchanged(self):
        with mock.patch.object(node_agent, "LOCAL", False):
            self.assertEqual(node_agent.claude_command(), ["bash", "-l", "-i", "-c", 'exec claude "$@"', "claude"])
        with mock.patch.object(node_agent, "FAKE", True), \
                mock.patch.dict(os.environ, {"CCR_CLAUDE_CMD": "/usr/bin/python3 -I '/a b/fake_tui.py'"}):
            self.assertEqual(node_agent.claude_command(), ["/usr/bin/python3", "-I", "/a b/fake_tui.py"])

    def test_resolve_tmux_local(self):
        node_agent._tmux["path"] = None
        self.addCleanup(node_agent._tmux.__setitem__, "path", None)
        with mock.patch.object(node_agent.shutil, "which", return_value="/somewhere/tmux"), \
                mock.patch.object(node_agent, "_executable", side_effect=lambda p: p == "/somewhere/tmux"):
            self.assertEqual(node_agent.resolve_tmux(), "/somewhere/tmux")
        node_agent._tmux["path"] = None
        with mock.patch.object(node_agent.shutil, "which", return_value=None), \
                mock.patch.object(node_agent, "_executable", side_effect=lambda p: p == "/usr/local/bin/tmux"):
            self.assertEqual(node_agent.resolve_tmux(), "/usr/local/bin/tmux")
        node_agent._tmux["path"] = None
        with mock.patch.object(node_agent.shutil, "which", return_value=None), \
                mock.patch.object(node_agent, "_executable", return_value=False):
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.resolve_tmux()
        self.assertIn("本机找不到 tmux", str(cm.exception))
        self.assertIn("brew install tmux", str(cm.exception))

    def test_op_start_local_argv(self):
        """start 的 tmux 命令：<登录 shell> -l -i -c 'exec claude "$@"' claude <白名单参数>（多参数，不经 shell 拼接）。"""
        calls = []

        def fake_tmux(args, input_bytes=None, timeout=15):
            calls.append(list(args))
            return 0, "4242\n", ""

        with tempfile.TemporaryDirectory(dir="/tmp") as td, \
                mock.patch.dict(os.environ, {"SHELL": "/bin/zsh", "HOME": td}), self.shells("/bin/zsh"), \
                mock.patch.object(node_agent, "tmux", fake_tmux), \
                mock.patch.object(node_agent, "list_session_names", return_value=["_ccr_keeper"]):
            res = node_agent.op_start({"sid": SID, "cwd": "~", "args": ["--model", "opus", "--effort", "low"]})
            self.assertEqual(res, {"pane_pid": 4242})
            argv = calls[0]
            i = argv.index("-c", argv.index("new-session"))
            self.assertEqual(argv[i + 1], os.path.realpath(td) if os.path.realpath(td) == td else td)
            j = argv.index("/bin/zsh")
            self.assertEqual(argv[j:j + 10], ["/bin/zsh", "-l", "-i", "-c", 'exec claude "$@"', "claude",
                                              "--model", "opus", "--effort", "low"])
            self.assertEqual(argv[j + 10], ";")
            # 白名单不变：本机同样拒绝 -p / --settings / 其它参数
            for bad in (["-p"], ["--settings", td + "/x.json"], ["--append-system-prompt", "x"],
                        ["--plugin-dir", "/x"], ["--mcp-config", "{}"]):
                with self.assertRaises(node_agent.OpError):
                    node_agent.op_start({"sid": SID, "cwd": "~", "args": bad})

    def test_hello_reports_local(self):
        with mock.patch.object(node_agent, "list_session_names", return_value=None), \
                mock.patch.object(node_agent, "resolve_tmux", return_value="/opt/homebrew/bin/tmux"), \
                mock.patch.object(node_agent, "tmux_version", return_value="tmux 3.6a"), \
                mock.patch.dict(os.environ, {"SLURMD_NODENAME": "gpu0001"}):
            h = node_agent.op_hello({})
        self.assertIs(h["local"], True)
        self.assertIsNone(h["slurm_node"])   # 本机不钉 Slurm 节点
        self.assertFalse(h["server_alive"])


@unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
class TestNodeAgentLocalProcess(unittest.TestCase):
    """以子进程运行 node_agent.py --local（真实本机分支，不是 fake）：只做 hello / list（只读），
    临时 HOME 与 TMUX_TMPDIR，不 start（start 会启动登录 shell + claude）。"""

    def test_local_flag_hello(self):
        td = os.path.realpath(tempfile.mkdtemp(prefix="ccrlh", dir="/tmp"))
        self.addCleanup(shutil.rmtree, td, True)
        before = _real_socket_exists()
        env = {k: v for k, v in os.environ.items() if not k.startswith("CCR_") and not k.startswith("TMUX")}
        env.update(HOME=td, TMUX_TMPDIR=td)
        p = subprocess.Popen([sys.executable, "-I", "-u", str(ROOT / "ccr" / "node_agent.py"), "--local"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        out, err = p.communicate(b'{"id":1,"op":"hello"}\n{"id":2,"op":"list"}\n', timeout=30)
        self.assertEqual(p.returncode, 0, err)
        r = [json.loads(x) for x in out.splitlines()]
        h = r[0]["result"]
        self.assertTrue(r[0]["ok"], r)
        self.assertIs(h["local"], True)
        self.assertFalse(h["server_alive"])
        self.assertTrue(h["tmux_path"].startswith("/"))
        self.assertEqual(h["home"], td)
        self.assertEqual(r[1], {"id": 2, "ok": True, "result": []})
        self.assertEqual(_real_socket_exists(), before)
        # 只有 tmux 客户端自己建的套接字目录（list-sessions 连不上 server 时不会建套接字）；agent 不写任何文件
        sock_dir = "tmux-%d" % os.getuid()
        self.assertTrue(set(os.listdir(td)) <= {sock_dir}, os.listdir(td))
        if os.path.isdir(os.path.join(td, sock_dir)):
            self.assertEqual(os.listdir(os.path.join(td, sock_dir)), [])
        # 不带 --local：local=false（远端 / fake 的行为不变）
        env["CCR_FAKE"] = "1"
        env["CCR_FAKE_TMUX"] = TMUX
        p = subprocess.Popen([sys.executable, "-I", "-u", str(ROOT / "ccr" / "node_agent.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        out, err = p.communicate(b'{"id":1,"op":"hello"}\n', timeout=30)
        self.assertIs(json.loads(out.splitlines()[0])["result"]["local"], False)


# ============================================================================ 历史 / 目录：target 路由
class _Runner:
    def __init__(self, result):
        self.calls = []
        self.result = result

    def __call__(self, cfg, script, args, timeout):
        self.calls.append(list(args))
        return 0, json.dumps(self.result) + "\n", ""


T1 = "59dd443a-c637-4148-bf1c-fd720f2e5b1f"


class TestHistoryTarget(unittest.TestCase):
    def cfg(self, **kw):
        c = {"host": "gpu-cluster", "fake": False, "default_cwd": "/mnt/ssd/home/u/work"}
        c.update(kw)
        return lambda: dict(c)

    def boom(self, *a, **k):
        raise AssertionError("本机查询不应 ssh")

    def test_local_routes_to_local_runner(self):
        loc = _Runner({"sessions": [{"claude_session_id": T1}]})
        svc = H.HistoryService(self.cfg(), run_python=self.boom, run_local_python=loc)
        self.assertEqual(svc.list_history(5, target="local"), {"sessions": [{"claude_session_id": T1}]})
        self.assertEqual(loc.calls[-1], ["list", "5"])   # 真实本机：不传 --root（脚本默认读本机 ~/.claude/projects）
        loc.result = {"claude_session_id": T1, "cwd": "/Users/u", "entries": []}
        self.assertEqual(svc.read_history(T1, target="local")["cwd"], "/Users/u")
        self.assertEqual(loc.calls[-1], ["read", T1])
        loc.result = {"path": "/Users/u", "parent": "/Users", "dirs": ["a"]}
        svc.list_dirs(None, target="local")
        self.assertEqual(loc.calls[-1], ["dirs", "~", "0"])   # 本机默认 ~，不用服务器的 default_cwd
        svc.list_dirs("/Users/u/p", True, target="local")
        self.assertEqual(loc.calls[-1], ["dirs", "/Users/u/p", "1"])

    def test_server_default_unchanged(self):
        srv = _Runner({"sessions": []})
        svc = H.HistoryService(self.cfg(), run_python=srv, run_local_python=self.boom)
        svc.list_history(3)
        svc.list_history(3, target="server")
        svc.list_history(3, target=None)
        self.assertEqual(srv.calls, [["list", "3"]] * 3)
        srv.result = {"path": "/x", "parent": "/", "dirs": []}
        svc.list_dirs(None)
        self.assertEqual(srv.calls[-1], ["dirs", "/mnt/ssd/home/u/work", "0"])

    def test_bad_target(self):
        svc = H.HistoryService(self.cfg(), run_python=self.boom, run_local_python=self.boom)
        for bad in ("Local", "both", "../x", 1):
            with self.assertRaises(H.HistoryError) as cm:
                svc.list_history(5, target=bad)
            self.assertEqual(cm.exception.status, 400)
            with self.assertRaises(H.HistoryError):
                svc.list_dirs("/", target=bad)
            with self.assertRaises(H.HistoryError):
                svc.read_history(T1, target=bad)

    def test_local_errors_not_ssh_wording(self):
        svc = H.HistoryService(self.cfg(), run_local_python=lambda c, s, a, t: (255, "", "boom"))
        with self.assertRaises(H.HistoryError) as cm:
            svc.list_history(5, target="local")
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("本机", cm.exception.message)
        self.assertNotIn("ssh", cm.exception.message)

    def test_real_local_runner_runs_script(self):
        """默认的本机 runner：本机 python3 -I - 跑同一份只读脚本（这里只列一个临时目录）。"""
        svc = H.HistoryService(self.cfg(), run_python=self.boom)
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            os.mkdir(os.path.join(td, "sub"))
            r = svc.list_dirs(td, target="local")
        self.assertEqual(r["dirs"], ["sub"])

    def test_fake_local_reads_fake_local_home(self):
        base = tempfile.mkdtemp(prefix="ccrlh", dir="/tmp")
        self.addCleanup(shutil.rmtree, base, True)
        cfg = {"fake": True, "fake_home": base + "/home", "fake_tmpdir": base + "/t"}
        proj = pathlib.Path(remote.fake_home(cfg, local=True), ".claude", "projects", "-x")
        proj.mkdir(parents=True)
        line = {"type": "user", "uuid": "u1", "cwd": "/x", "message": {"role": "user", "content": "本机你好"}}
        (proj / (T1 + ".jsonl")).write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
        svc = H.HistoryService(lambda: dict(cfg), run_python=self.boom, run_local_python=self.boom)
        items = svc.list_history(10, target="local")["sessions"]
        self.assertEqual([(x["claude_session_id"], x["title"]) for x in items], [(T1, "本机你好")])
        self.assertEqual(len(svc.read_history(T1, target="local")["entries"]), 1)
        # fake 服务器仍然读 fixtures/
        self.assertNotIn(T1 + "x", {x["claude_session_id"] for x in svc.list_history(10)["sessions"]})


# ============================================================================ server ?target=
class _TargetHistory(StubHistory):
    def __init__(self):
        super().__init__()
        self.calls = []

    def list_dirs(self, path, hidden=False, **kw):
        self.calls.append(("dirs", path, hidden, kw))
        return {"path": path or "/home/u", "parent": "/", "dirs": ["a"]}

    def list_history(self, limit=100, **kw):
        self.calls.append(("list", limit, kw))
        return {"sessions": [{"claude_session_id": UUID, "limit": limit}]}

    def read_history(self, uid, **kw):
        self.calls.append(("read", uid, kw))
        return {"claude_session_id": UUID, "cwd": "/x", "entries": []}


class TestServerTarget(ServerBase):
    def setUp(self):
        super().setUp()
        self.th = _TargetHistory()
        self.srv.history = self.th

    def test_history_target(self):
        st, js, _, _ = self.req("GET", "/api/history?limit=7&target=local")
        self.assertEqual(st, 200, js)
        self.assertEqual(self.th.calls[-1], ("list", 7, {"target": "local"}))
        st, js, _, _ = self.req("GET", "/api/history?limit=7")
        self.assertEqual(st, 200)
        self.assertEqual(self.th.calls[-1], ("list", 7, {}))   # 缺省 = 服务器，调用与以前完全一致
        st, _, _, _ = self.req("GET", "/api/history?target=server")
        self.assertEqual(self.th.calls[-1], ("list", 100, {}))
        st, js, _, _ = self.req("GET", "/api/history/%s?target=local" % UUID)
        self.assertEqual(st, 200)
        self.assertEqual(self.th.calls[-1], ("read", UUID, {"target": "local"}))
        st, js, _, _ = self.req("GET", "/api/dirs?path=%2FUsers&target=local&hidden=1")
        self.assertEqual(st, 200)
        self.assertEqual(self.th.calls[-1], ("dirs", "/Users", True, {"target": "local"}))
        n = len(self.th.calls)
        for path in ("/api/history?target=bogus", "/api/dirs?target=Local", "/api/history/%s?target=x" % UUID):
            self.assertErr(self.req("GET", path), 400)
        self.assertEqual(len(self.th.calls), n)

    def test_create_local(self):
        self.req("POST", "/api/sessions", {"jobid": "local"})   # 桩 manager 只接受数字 jobid：只看传进去的参数
        call = [c for c in self.mgr.calls if c[0] == "create"][-1]
        kw = call[1] if isinstance(call[1], dict) else {}
        self.assertEqual(kw.get("jobid"), "local")
        self.assertEqual(kw.get("cwd"), "~")   # 本机默认 ~，不套用服务器的 default_cwd


# ============================================================================ SessionManager：本机会话
class TestLocalSession(SessionBase):
    def test_create_local_meta(self):
        m = self.mgr()
        sid = self.start(m, jobid="local", cwd="~/proj")
        meta = m.get_meta(sid)
        self.assertEqual(meta["jobid"], "local")
        self.assertEqual(meta["link"], "ready")
        self.assertEqual(self.rem.links[-1].jobid, "local")
        self.assertTrue(self.rem.keeper)
        st = self.rem.ops("start")[0]
        self.assertEqual(st["cwd"], "~/proj")
        self.assertNotIn("api_key", st)
        self.assertEqual(st["args"], [])
        for bad in ("Local", "local;id", "loc"):
            with self.assertRaises(ValueError):
                m.create(jobid=bad, cwd="~")

    def test_local_and_job_links_separate(self):
        m = self.mgr()
        a = self.start(m, jobid="local")
        b = self.start(m, jobid="12345")
        self.assertEqual(sorted(m._links), ["12345", "local"])
        self.assertEqual(m.get_meta(a)["jobid"], "local")
        self.assertEqual(m.get_meta(b)["jobid"], "12345")

    def test_restart_cross_target_rejected(self):
        m = self.mgr()
        a = self.start(m, jobid="local")
        self.converse(m, a)
        b = self.start(m, jobid="12345", resume=UUID2)   # 桩里新会话都是 UUID1：服务器这个用另一个 id
        self.converse(m, b)
        n_start = len(self.rem.ops("start"))
        with self.assertRaises(S.OpError) as cm:
            m.restart(a, jobid="12345")
        self.assertEqual(cm.exception.api_code, "target_mismatch")
        with self.assertRaises(S.OpError) as cm:
            m.restart(b, jobid="local")
        self.assertEqual(cm.exception.api_code, "target_mismatch")
        self.assertEqual(len(self.rem.ops("start")), n_start)   # 什么都没动
        self.assertEqual(self.rem.ops("kill"), [])
        self.assertEqual(m.get_meta(a)["state"], "idle")
        # 本机会话在本机重启：带 --resume
        m.restart(a, jobid="local")
        self.assertTrue(wait_until(lambda: len(self.rem.ops("start")) == n_start + 1 and
                                   m.get_meta(a)["state"] == "idle", 5))
        self.assertEqual(self.rem.ops("start")[-1]["args"], ["--resume", UUID1])
        self.assertEqual(m.get_meta(a)["jobid"], "local")

    def test_persist_and_reattach_local(self):
        m = self.mgr()
        sid = self.start(m, jobid="local")
        m.shutdown()
        data = json.loads(pathlib.Path(self.tmp, "sessions.json").read_text(encoding="utf-8"))
        self.assertEqual(data["sessions"][0]["meta"]["jobid"], "local")
        m2 = self.mgr()
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["link"] == "ready" and
                                   m2.get_meta(sid)["state"] == "idle", 5))
        self.assertEqual(m2.get_meta(sid)["jobid"], "local")
        self.assertEqual(self.rem.links[-1].jobid, "local")

    def test_close_link_down_local_wording(self):
        m = self.mgr()
        sid = self.start(m, jobid="local")
        link = self.rem.links[-1]
        self.rem.job_state = False   # 本机 check_job 永远 False
        link.set_state("down", "agent 进程退出")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "down", 3))
        with self.assertRaises(S.OpError) as cm:
            m.close(sid)
        self.assertEqual(cm.exception.api_code, "link_down")
        self.assertIn("本机", str(cm.exception))
        self.assertNotIn("作业", str(cm.exception))
        m.close(sid, force=True)
        self.assertEqual(m.get_meta(sid)["state"], "exited")

    def test_local_api_key_start_params(self):
        """本机同样可以选 key：start 请求带 api_key（agent 写本机 ~/.cc-remote-chat/keys/）。明文不进 meta。"""
        m = self.mgr()
        k = m.api_keys.add("本机测试", "sk-test-local-1111", None)
        sid = self.start(m, jobid="local", api_key_id=k["id"])
        st = self.rem.ops("start")[-1]
        self.assertEqual(st["api_key"], "sk-test-local-1111")
        meta = m.get_meta(sid)
        self.assertEqual(meta["launch"]["api_key_id"], k["id"])
        self.assertNotIn("sk-test-local-1111", json.dumps(meta, ensure_ascii=False))


# ============================================================================ 统计：本机 statusline 格式不同 → 回退
class TestLocalStatsFallback(unittest.TestCase):
    RULE = "─" * 80

    def screen(self, status):
        return "\n".join(["", " ▐▛███▛█   Claude Code v2.1.330", "", "❯ 你好", "", "● 你好！", "",
                          self.RULE, "❯ ", self.RULE] + status + ["  ⏵⏵ auto mode on (shift+tab to cycle)"])

    def test_unknown_statusline_falls_back_to_transcript(self):
        entry = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 2000}}}
        for status in (["  ~/proj (main*) | Opus 5.5 | 12:34:56"],
                       ["  my custom line ⚡ tok/s ⚙"],
                       ["  a/b c/d 2026-11-22 /Users/x/proj"],
                       []):
            st = tui_parse.parse_screen(self.screen(status))
            self.assertTrue(st["input"]["visible"], (status, st))
            self.assertIsNone(st["dialog"])
            tr = stats.StatsTracker()
            tr.on_screen(st, 1.0)
            tr.on_transcript([entry])
            snap = tr.snapshot()
            self.assertEqual(snap["context_source"], "transcript", (status, snap))
            self.assertEqual(snap["context_used"], 3010)
            self.assertTrue(snap["context_approx"])
            self.assertIsNone(snap["cost_usd"])

    def test_parse_never_raises(self):
        for status in ("💰 $ | ctx: ?% | 5h: tok, $", "|||", " " * 5, "$$$ 1/0 0/0k", "ctx: 999%"):
            tui_parse.parse_status_stats(status)
            tui_parse.parse_screen(self.screen(["  " + status]))


# ============================================================================ fake 端到端：本机运行位置
@unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
class TestLocalFakeEndToEnd(unittest.TestCase):
    """真实 SessionManager + AgentLink（fake 模式的"本机"：node_agent --local + fake_tui + 本机 tmux），
    HOME / TMUX_TMPDIR 都是 /tmp 下的临时目录，与 fake 服务器分开。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccr-le-", dir="/tmp"))
        self.cfg = {"host": "gpu-cluster", "jobid": "12345", "default_cwd": "~", "fake": True,
                    "fake_home": os.path.join(self.tmp, "home"), "fake_tmpdir": os.path.join(self.tmp, "tmux"),
                    "fake_tmux": TMUX, "fake_env": {"CCR_FAKE_DELAY": "0.03", "CCR_FAKE_THINK": "0.2"}}
        self.lhome = remote.fake_home(self.cfg, local=True)
        self.ltmux = remote.fake_tmpdir(self.cfg, local=True)
        os.makedirs(os.path.join(self.lhome, "proj"))
        self.real_before = _real_socket_exists()
        self.addCleanup(self._cleanup)

    def _tmux(self, tmpdir, *args):
        """只对 tmpdir 里的套接字操作：用 -S 绝对路径（绝不经 -L + TMUX_TMPDIR——TMUX_TMPDIR 指向不存在的
        目录时 tmux 会退回 /tmp，打到用户真实的 /tmp/tmux-<uid>/ccr，曾因此杀掉用户正在用的本机会话）。
        目录不存在 = 那里不可能有 server，直接返回失败、不调用 tmux。"""
        sock = os.path.join(tmpdir, "tmux-%d" % os.getuid(), "ccr")
        if not os.path.isdir(tmpdir):
            return subprocess.CompletedProcess([TMUX], 1, "", "no such dir: %s" % tmpdir)
        env = dict(os.environ)
        env.pop("TMUX", None)
        return subprocess.run([TMUX, "-S", sock, "-f", "/dev/null"] + list(args), env=env,
                              capture_output=True, text=True, timeout=10)

    def _cleanup(self):
        for d in (self.ltmux, remote.fake_tmpdir(self.cfg)):
            self._tmux(d, "kill-server")
        shutil.rmtree(self.tmp, True)
        self.assertEqual(_real_socket_exists(), self.real_before, "不应在真实套接字上起 tmux server")

    def mgr(self):
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp, "data"))
        m.exit_timeout = 8
        self.addCleanup(lambda: m._shutdown or m.shutdown())
        return m

    def wait_state(self, m, sid, states, timeout=20):
        ok = wait_until(lambda: m.get_meta(sid)["state"] in states, timeout, 0.1)
        self.assertTrue(ok, (m.get_meta(sid), m.get_screen(sid)["text"]))

    def test_local_flow(self):
        m = self.mgr()
        sid = m.create(jobid="local", cwd="~/proj", title="本机")["sid"]
        self.wait_state(m, sid, ("idle", "trust"))
        if m.get_meta(sid)["state"] == "trust":
            d = m.get_screen(sid)["state"]["dialog"]
            m.choose(sid, next(o["index"] for o in d["options"] if o["label"].startswith("Yes")))
            self.wait_state(m, sid, ("idle",))
        link = m._links["local"]
        self.assertTrue(link.local)
        self.assertIs(link.hello.get("local"), True)
        # tmux server 在 fake "本机" 的 TMUX_TMPDIR 里；fake 服务器的没有启动
        self.assertEqual(self._tmux(self.ltmux, "has-session", "-t", "_ccr_keeper").returncode, 0)
        self.assertNotEqual(self._tmux(remote.fake_tmpdir(self.cfg), "list-sessions").returncode, 0)
        m.send_message(sid, "本机你好")
        self.assertTrue(wait_until(lambda: any(
            e["kind"] == "transcript" and any(x.get("type") == "assistant" for x in e["data"]["entries"])
            for e in m.session_events(sid)), 20, 0.1), m.get_screen(sid)["text"])
        self.wait_state(m, sid, ("idle",))
        cid = m.get_meta(sid)["claude_session_id"]
        self.assertTrue(cid)
        # 本机历史：读 fake "本机" HOME 的 ~/.claude/projects（fake_tui 写的 transcript）
        svc = H.HistoryService(lambda: dict(self.cfg))
        ids = [x["claude_session_id"] for x in svc.list_history(20, target="local")["sessions"]]
        self.assertIn(cid, ids)
        self.assertTrue(svc.read_history(cid, target="local")["entries"])
        self.assertNotIn(cid, [x["claude_session_id"] for x in svc.list_history(20)["sessions"]])
        # app 重启：重新接回本机会话
        m.shutdown()
        m2 = self.mgr()
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["link"] == "ready", 20, 0.1))
        self.wait_state(m2, sid, ("idle",))
        # 关闭：/exit → 无剩余会话 → stop_keeper、关 link
        m2.close(sid)
        self.assertEqual(m2.get_meta(sid)["state"], "exited")
        self.assertTrue(wait_until(lambda: not m2._links, 10, 0.1))
        self.assertTrue(wait_until(lambda: self._tmux(self.ltmux, "list-sessions").returncode != 0, 10, 0.1))
        # 什么都没写进 fake 服务器的 HOME
        self.assertFalse(os.path.exists(os.path.join(remote.fake_home(self.cfg), ".claude")))


if __name__ == "__main__":
    unittest.main()
