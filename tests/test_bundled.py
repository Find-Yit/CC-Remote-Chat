"""内置模式（dmg 版，app.py --bundled，SPEC §14.10）测试：

- 不传新参数时本机 agent 的 argv、keeper 脚本与以前逐字相同（从源码运行时行为零变化）；
- tmux 路径允许空格（/Applications/CC Remote Chat.app/...），且经 shlex.quote 后在真实 bash 3.2 里可用；
- 运行时配置 local_tmux / tmux_label 传到 node_agent（--tmux= / --tmux-label=）、keeper、fake keeper；
- 后端以 -B 运行时，再起的 Python 子进程（agent、fake_tui、历史脚本）也带 -B；
- 本机会话退出码 127 的友好提示（static/exitnotice.js）；
- --bundled 的配置推导（tmux 路径、标签、默认数据目录）与前端可见的 tmux_label；
- node_agent：没有 tmux-256color terminfo 时本机会话改用 screen-256color；
- 升级（新 .app 原地替换旧 .app）：内置模式下已有实例的构建标识（app.pid 的 build）与自己的不同 → SIGTERM 旧后台并接管，
  相同 → SIGUSR1；旧后台不退出 → 报错、不强杀；非内置模式 app.pid 与行为不变。

红线：不碰真实的 /tmp/tmux-<uid>/ccr（tmux 一律临时 TMUX_TMPDIR），不读 ~/.cc-remote-chat，不运行真实 claude。
"""
import functools
import io
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import app as ccr_app
from ccr import agent_link, config as C, history as H, node_agent, remote
from tests.test_agent_link import _ScriptConn
from tests.test_node_agent import HAVE_TMUX, SID, TMUX
from tests.test_session import wait_until

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SOCKET_DIR = "/tmp/tmux-%d" % os.getuid()
SPACE_TMUX = "/Applications/CC Remote Chat.app/Contents/Resources/bin/tmux"
BUNDLED_CFG = {"local_tmux": SPACE_TMUX, "tmux_label": "ccr-app"}


def _real_socket_exists():
    return os.path.exists(os.path.join(REAL_SOCKET_DIR, "ccr"))


def _flags(dont_write_bytecode):
    return mock.patch.object(sys, "flags", types.SimpleNamespace(dont_write_bytecode=dont_write_bytecode))


# ============================================================================ 不传新参数：逐字不变
class TestDefaultsUnchanged(unittest.TestCase):
    def test_local_argv_unchanged(self):
        with _flags(False):
            argv, _env = agent_link.build_agent_argv({"host": "h", "ssh_multiplex": False}, "local")
            self.assertEqual(argv, [sys.executable, "-I", "-u", str(agent_link.AGENT_PATH), "--local"])
            fargv, fenv = agent_link.build_agent_argv({"fake": True, "fake_home": "/tmp/fh", "fake_tmpdir": "/tmp/ft",
                                                       "fake_tmux": "/x/tmux"}, "local")
            self.assertEqual(fargv, [sys.executable, "-I", "-u", str(agent_link.AGENT_PATH), "--local"])
            self.assertEqual(shlex.split(fenv["CCR_CLAUDE_CMD"]), [sys.executable, "-I", str(agent_link.FAKE_TUI_PATH)])
        self.assertEqual(agent_link.local_tmux_args({}), [])
        self.assertEqual(agent_link.local_tmux_label({}), "ccr")

    def test_keeper_script_unchanged(self):
        t = "/opt/homebrew/bin/tmux"
        q = t + " -L ccr -f /dev/null"
        want = ('%s new-session -d -s _ccr_keeper "sleep 2147483647" || exit 0; i=0; while %s has-session -t '
                "_ccr_keeper 2>/dev/null; do sleep 15; if s=$(%s list-sessions -F '#{session_name}' 2>/dev/null); "
                "then if printf '%%s\\n' \"$s\" | grep -q '^ccr-'; then i=0; else i=$((i+15)); fi; "
                'if [ "$i" -ge 600 ]; then %s kill-session -t _ccr_keeper; fi; fi; done' % (q, q, q, q))
        self.assertEqual(agent_link.keeper_script(t), want)
        self.assertEqual(agent_link.keeper_script(t, label="ccr"), want)

    def test_local_keeper_default_label(self):
        link = agent_link.AgentLink({"host": "h", "ssh_multiplex": False}, "local", lambda e: None, lambda s, d: None)
        link._conn = _ScriptConn([{"server_alive": a, "tmux_path": TMUX} for a in (False, True)])
        link._ready = True
        link.state = "ready"
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(agent_link.subprocess, "Popen", return_value=proc) as popen:
            link.ensure_keeper(10)
        self.assertEqual(popen.call_args[0][0], ["/bin/bash", "-c", agent_link.keeper_script(TMUX)])

    def test_public_config_unchanged(self):
        cfg = dict(C.DEFAULT_CONFIG, control_dir="/x", fake=True)
        self.assertEqual(C.public_config(cfg), {k: cfg[k] for k in C.DEFAULT_CONFIG})
        self.assertNotIn("tmux_label", C.public_config(cfg))

    def test_default_data_dir(self):
        self.assertEqual(ccr_app.default_data_dir(False), str(C.DEFAULT_DATA_DIR))
        self.assertEqual(ccr_app.default_data_dir(True), "~/.cc-remote-chat-app")


# ============================================================================ tmux 路径含空格
class TestSpacePath(unittest.TestCase):
    def test_space_path_accepted_and_quoted(self):
        script = agent_link.keeper_script(SPACE_TMUX, label="ccr-app")
        self.assertTrue(script.startswith(shlex.quote(SPACE_TMUX) + " -L ccr-app -f /dev/null new-session"))
        self.assertEqual(shlex.split(script)[:5], [SPACE_TMUX, "-L", "ccr-app", "-f", "/dev/null"])
        cmd = agent_link.build_keeper_remote_command("1", SPACE_TMUX)
        self.assertIn(shlex.quote(SPACE_TMUX), shlex.split(shlex.split(cmd)[2])[11])
        for bad in ("/x;id", "/x/$(id)", "/x/`id`", "/x/'q'", "/a b/../../bin/sh", "relative/tmux", "/x\n", "/x\tb"):
            with self.assertRaises(agent_link.AgentError, msg=repr(bad)):
                agent_link.keeper_script(bad)

    @unittest.skipUnless(HAVE_TMUX and os.path.exists("/bin/bash"), "需要本机 tmux 与 /bin/bash")
    def test_space_path_real_bash32_keeper(self):
        """真实的 /bin/bash 3.2 + 临时 TMUX_TMPDIR：带空格路径的 tmux、-L ccr-app 能起 keeper，套接字名就是标签。"""
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccrbk", dir="/tmp"))
        before = _real_socket_exists()
        d = os.path.join(tmp, "A pp", "bin")
        os.makedirs(d)
        t = os.path.join(d, "tmux")
        os.symlink(TMUX, t)
        env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX") and not k.startswith("CCR_")}
        env["TMUX_TMPDIR"] = tmp

        def tm(*a):
            return subprocess.run([TMUX, "-L", "ccr-app", "-f", "/dev/null"] + list(a), env=env,
                                  capture_output=True, text=True, timeout=10)

        def cleanup():
            if os.path.isdir(tmp):   # 目录不在时绝不调用 tmux（会退回 /tmp 打到真实套接字）
                tm("kill-server")
            shutil.rmtree(tmp, ignore_errors=True)
        self.addCleanup(cleanup)
        p = subprocess.Popen(["/bin/bash", "-c", agent_link.keeper_script(t, idle_exit=2, poll=1, label="ccr-app")],
                             env=env, cwd=tmp, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(5)))
        self.assertTrue(wait_until(lambda: tm("has-session", "-t", "_ccr_keeper").returncode == 0, 10))
        self.assertTrue(os.path.exists(os.path.join(tmp, "tmux-%d" % os.getuid(), "ccr-app")))
        self.assertFalse(os.path.exists(os.path.join(tmp, "tmux-%d" % os.getuid(), "ccr")))
        self.assertEqual(p.wait(15), 0)   # 没有 ccr- 会话：空闲自停
        self.assertEqual(_real_socket_exists(), before)


# ============================================================================ 标签与 tmux 路径生效
class TestLabelAndPath(unittest.TestCase):
    def test_agent_argv_bundled(self):
        with _flags(False):
            argv, env = agent_link.build_agent_argv(dict(BUNDLED_CFG, host="h", ssh_multiplex=False), "local")
        self.assertEqual(argv, [sys.executable, "-I", "-u", str(agent_link.AGENT_PATH), "--local",
                                "--tmux=" + SPACE_TMUX, "--tmux-label=ccr-app"])
        self.assertFalse([k for k in env if k.startswith("CCR_")])   # 只经 argv，不加环境变量
        # 服务器作业的远端命令不受影响（不带 --tmux）
        argv2, env2 = agent_link.build_agent_argv(dict(BUNDLED_CFG, host="h", ssh_multiplex=False), "123")
        self.assertIsNone(env2)
        self.assertEqual(argv2[-1], agent_link.build_agent_remote_command("123"))
        self.assertNotIn("--tmux", " ".join(argv2[:-1]))

    def test_bad_label_or_path_rejected(self):
        for bad in ("ccr app", "x;id", "", "a" * 33, "../x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                agent_link.validate_tmux_label(bad)
        with self.assertRaises(ValueError):
            agent_link.build_agent_argv({"tmux_label": "x;id"}, "local")
        with self.assertRaises(agent_link.AgentError):
            agent_link.build_agent_argv({"local_tmux": "/x;id"}, "local")

    def test_local_keeper_uses_label(self):
        link = agent_link.AgentLink(dict(BUNDLED_CFG, host="h", ssh_multiplex=False), "local",
                                    lambda e: None, lambda s, d: None)
        link._conn = _ScriptConn([{"server_alive": a, "tmux_path": SPACE_TMUX} for a in (False, True)])
        link._ready = True
        link.state = "ready"
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(agent_link.subprocess, "Popen", return_value=proc) as popen:
            link.ensure_keeper(10)
        script = popen.call_args[0][0][2]
        self.assertEqual(script, agent_link.keeper_script(SPACE_TMUX, label="ccr-app"))
        self.assertIn(" -L ccr-app -f /dev/null", script)
        self.assertNotIn(" -L ccr ", script)

    def test_fake_keeper_and_env_use_bundled_tmux(self):
        base = tempfile.mkdtemp(prefix="ccrbf", dir="/tmp")
        self.addCleanup(shutil.rmtree, base, True)
        cfg = dict(BUNDLED_CFG, fake=True, fake_home=base + "/home", fake_tmpdir=base + "/t")
        self.assertEqual(agent_link.fake_env(cfg, local=True)["CCR_FAKE_TMUX"], SPACE_TMUX)
        for local, label in ((True, "ccr-app"), (False, "ccr")):
            link = agent_link.AgentLink(cfg, "local" if local else "1", lambda e: None, lambda s, d: None)
            with mock.patch.object(agent_link.subprocess, "run",
                                   return_value=types.SimpleNamespace(returncode=0, stderr=b"")) as run:
                link._start_fake_keeper()
            self.assertEqual(run.call_args[0][0][:5], [SPACE_TMUX, "-L", label, "-f", "/dev/null"])

    def test_node_agent_parse_local_opts(self):
        self.assertEqual(node_agent.parse_local_opts(["--local"]), (None, "ccr"))
        self.assertEqual(node_agent.parse_local_opts(["--local", "--tmux=" + SPACE_TMUX, "--tmux-label=ccr-app"]),
                         (SPACE_TMUX, "ccr-app"))
        for bad in (["--tmux=tmux"], ["--tmux=/a/../b"], ["--tmux-label=a b"], ["--tmux-label="],
                    ["--tmux-label=x;id"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                node_agent.parse_local_opts(bad)

    def test_node_agent_tmux_uses_label_and_path(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        node_agent._tmux["path"] = None
        self.addCleanup(node_agent._tmux.__setitem__, "path", None)
        with mock.patch.object(node_agent, "LOCAL", True), mock.patch.object(node_agent, "FAKE", False), \
                mock.patch.object(node_agent, "ARG_TMUX", SPACE_TMUX), \
                mock.patch.object(node_agent, "TMUX_LABEL", "ccr-app"), \
                mock.patch.object(node_agent, "_executable", side_effect=lambda p: p == SPACE_TMUX), \
                mock.patch.object(node_agent.shutil, "which", return_value="/opt/homebrew/bin/tmux"), \
                mock.patch.object(node_agent.subprocess, "run", run):
            node_agent.tmux(["list-sessions"])
        self.assertEqual(calls[0], [SPACE_TMUX, "-L", "ccr-app", "-f", "/dev/null", "list-sessions"])
        # 指定的 tmux 不可执行：报错，不退回 PATH / Homebrew 里的
        node_agent._tmux["path"] = None
        with mock.patch.object(node_agent, "LOCAL", True), mock.patch.object(node_agent, "FAKE", False), \
                mock.patch.object(node_agent, "ARG_TMUX", "/nonexistent/tmux"), \
                mock.patch.object(node_agent.shutil, "which", return_value="/opt/homebrew/bin/tmux"):
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.resolve_tmux()
        self.assertIn("/nonexistent/tmux", str(cm.exception))
        # 默认（非内置模式）：-L ccr
        self.assertEqual(node_agent.TMUX_LABEL, "ccr")
        self.assertIsNone(node_agent.ARG_TMUX)

    @unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
    def test_node_agent_process_with_opts(self):
        """子进程运行 node_agent.py --local --tmux=<带空格路径> --tmux-label=ccr-app：hello 报告该路径；只读，不 start。"""
        td = os.path.realpath(tempfile.mkdtemp(prefix="ccrbh", dir="/tmp"))
        self.addCleanup(shutil.rmtree, td, True)
        d = os.path.join(td, "C C", "bin")
        os.makedirs(d)
        t = os.path.join(d, "tmux")
        os.symlink(TMUX, t)
        before = _real_socket_exists()
        env = {k: v for k, v in os.environ.items() if not k.startswith("CCR_") and not k.startswith("TMUX")}
        env.update(HOME=td, TMUX_TMPDIR=td)
        argv = [sys.executable, "-I", "-u", str(ROOT / "ccr" / "node_agent.py"), "--local", "--tmux=" + t,
                "--tmux-label=ccr-app"]
        p = subprocess.run(argv, input=b'{"id":1,"op":"hello"}\n', capture_output=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        h = json.loads(p.stdout.splitlines()[0])["result"]
        self.assertEqual(h["tmux_path"], t)
        self.assertFalse(h["server_alive"])
        p = subprocess.run(argv[:-1] + ["--tmux-label=bad label"], input=b"", capture_output=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(_real_socket_exists(), before)

    def test_public_config_has_label_in_bundled_mode(self):
        cfg = dict(C.DEFAULT_CONFIG, **BUNDLED_CFG)
        pub = C.public_config(cfg)
        self.assertEqual(pub["tmux_label"], "ccr-app")
        self.assertNotIn("local_tmux", pub)   # 路径不给前端
        with self.assertRaises(ValueError):
            C.validate_partial({"tmux_label": "ccr"})   # 前端不能改


# ============================================================================ -B 透传
class TestNoBytecode(unittest.TestCase):
    def test_py_flags(self):
        with _flags(False):
            self.assertEqual(agent_link.py_flags(), ["-I", "-u"])
            self.assertEqual(agent_link.py_flags(unbuffered=False), ["-I"])
        with _flags(True):
            self.assertEqual(agent_link.py_flags(), ["-I", "-B", "-u"])
            self.assertEqual(agent_link.py_flags(unbuffered=False), ["-I", "-B"])

    def test_agent_and_fake_tui_get_B(self):
        with _flags(True):
            argv, _ = agent_link.build_agent_argv(BUNDLED_CFG, "local")
            self.assertEqual(argv[:5], [sys.executable, "-I", "-B", "-u", str(agent_link.AGENT_PATH)])
            fargv, fenv = agent_link.build_agent_argv({"fake": True, "fake_home": "/tmp/fh", "fake_tmpdir": "/tmp/ft",
                                                       "fake_tmux": "/x/tmux"}, "1")
            self.assertEqual(fargv, [sys.executable, "-I", "-B", "-u", str(agent_link.AGENT_PATH)])
            self.assertEqual(shlex.split(fenv["CCR_CLAUDE_CMD"]),
                             [sys.executable, "-I", "-B", str(agent_link.FAKE_TUI_PATH)])

    def test_history_local_runner_gets_B(self):
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        for flag, want in ((False, [sys.executable, "-I", "-", "a"]), (True, [sys.executable, "-I", "-B", "-", "a"])):
            seen.clear()
            with _flags(flag), mock.patch.object(H.subprocess, "run", run):
                H._local_run_python({}, "pass", ["a"], 5)
            self.assertEqual(seen[0], want)

    def test_real_child_flag(self):
        """真实子进程：以 -B 启动的解释器里 py_flags() 带 -B，不带时不带。"""
        code = "import sys; sys.path.insert(0, %r); from ccr import agent_link; print(agent_link.py_flags())" % str(ROOT)
        out_b = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=30)
        out_n = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out_b.stdout.strip(), "['-I', '-B', '-u']", out_b.stderr)
        self.assertEqual(out_n.stdout.strip(), "['-I', '-u']", out_n.stderr)


# ============================================================================ 127：找不到 claude
@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestExitNotice(unittest.TestCase):
    def test_exit_notice(self):
        url = (ROOT / "static" / "exitnotice.js").as_uri()
        js = r"""
const m = await import(%s);
console.log(JSON.stringify([
  m.exitNoticeText(127, false, true), m.exitNoticeText(127, false, false), m.exitNoticeText(1, false, true),
  m.exitNoticeText(0, true, true), m.exitNoticeText(null, true, false), m.exitNoticeText(null, false, true),
  m.exitNoticeText(127, true, true)]));
""" % json.dumps(url)
        r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out[0], "找不到 claude 命令：请先安装 Claude Code，并确认在『终端』里能运行 claude")
        self.assertEqual(out[1], "claude 进程意外退出（退出码 127）")   # 服务器会话：与以前相同
        self.assertEqual(out[2], "claude 进程意外退出（退出码 1）")
        self.assertEqual(out[3], "claude 进程已结束（退出码 0）")
        self.assertEqual(out[4], "claude 进程已结束")
        self.assertEqual(out[5], "claude 进程意外退出（退出码 无）")
        self.assertEqual(out[6], "claude 进程已结束（退出码 127）")

    def test_render_uses_it(self):
        src = (ROOT / "static" / "render.js").read_text(encoding="utf-8")
        self.assertIn("from './exitnotice.js'", src)
        self.assertIn("exitNoticeText(code, expected, !!s.meta && s.meta.jobid === 'local')", src)


# ============================================================================ --bundled 的配置推导
class TestBundledConfig(unittest.TestCase):
    def test_overrides_from_app_location(self):
        res = pathlib.Path("/Applications/CC Remote Chat.app/Contents/Resources")
        o = ccr_app.bundled_overrides(res / "app")
        self.assertEqual(o, {"local_tmux": str(res / "bin" / "tmux"), "tmux_label": "ccr-app"})
        self.assertNotEqual(o["tmux_label"], "ccr")
        # 推出来的参数能原样通过 agent_link 的校验
        self.assertEqual(agent_link.local_tmux_args(o), ["--tmux=" + str(res / "bin" / "tmux"), "--tmux-label=ccr-app"])

    def test_bundled_stop_uses_app_data_dir(self):
        """app.py --bundled --stop（临时 HOME）：默认数据目录是 ~/.cc-remote-chat-app，不碰 ~/.cc-remote-chat。"""
        home = os.path.realpath(tempfile.mkdtemp(prefix="ccrbh", dir="/tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
        r = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "app.py"), "--bundled", "--stop"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(home, ".cc-remote-chat-app")))
        self.assertFalse(os.path.exists(os.path.join(home, ".cc-remote-chat")))

    def test_bundled_help_lists_flag(self):
        r = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "app.py"), "--help"], capture_output=True,
                           text=True, timeout=60)
        self.assertIn("--bundled", r.stdout)


# ============================================================================ terminfo（macOS 12/13）
class TestDefaultTerminal(unittest.TestCase):
    def test_default_terminal_args(self):
        missing = "/nonexistent/terminfo/74/tmux-256color"
        self.assertEqual(node_agent.default_terminal_args(local=True, terminfo=missing),
                         ["set-option", "-g", "default-terminal", "screen-256color", ";"])
        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(node_agent.default_terminal_args(local=True, terminfo=f.name), [])
        self.assertEqual(node_agent.default_terminal_args(local=False, terminfo=missing), [])   # 远端不变

    def test_op_start_prefix_when_missing(self):
        calls = []

        def fake_tmux(args, input_bytes=None, timeout=15):
            calls.append(list(args))
            return 0, "4242\n", ""
        orig = node_agent.default_terminal_args
        with tempfile.TemporaryDirectory(dir="/tmp") as td, \
                mock.patch.object(node_agent, "LOCAL", True), mock.patch.object(node_agent, "FAKE", False), \
                mock.patch.object(node_agent, "default_terminal_args",
                                  functools.partial(orig, terminfo="/nonexistent/terminfo/74/tmux-256color")), \
                mock.patch.dict(os.environ, {"SHELL": "/bin/zsh", "HOME": td}), \
                mock.patch.object(node_agent, "_listed_shells", return_value={"/bin/zsh"}), \
                mock.patch.object(node_agent, "tmux", fake_tmux), \
                mock.patch.object(node_agent, "list_session_names", return_value=["_ccr_keeper"]):
            node_agent.op_start({"sid": SID, "cwd": "~", "args": []})
        self.assertEqual(calls[0][:5], ["set-option", "-g", "default-terminal", "screen-256color", ";"])
        self.assertEqual(calls[0][5:10], ["set-option", "-g", "history-limit", "10000", ";"])


# ============================================================================ 升级：构建标识与接管旧后台
# 假的"已有实例"：拿住实例锁、写 app.pid（只有 pid，像 2.9）、记下收到的信号；ignore_term 时收到 SIGTERM 也不退出
_HOLDER = r"""
import fcntl, json, os, signal, sys, time
d, ignore_term = sys.argv[1], sys.argv[2] == "1"
fd = os.open(os.path.join(d, "app.lock"), os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
with open(os.path.join(d, "app.pid"), "w") as f:
    json.dump({"pid": os.getpid()}, f)
def on_sig(n, _f):
    with open(os.path.join(d, "signals"), "a") as f:
        f.write("%d\n" % n)
    if n == signal.SIGTERM and not ignore_term:
        sys.exit(0)
for n in (signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2):
    signal.signal(n, on_sig)
print("ready", flush=True)
while True:
    time.sleep(0.1)
"""


class TestBuildId(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="ccrbid", dir="/tmp")))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_read_build_id(self):
        self.assertIsNone(ccr_app.read_build_id(self.dir))   # 没有 BUILD（从源码运行、2.9）
        (self.dir / "BUILD").write_text("2.12 20261124T010203Z 0123456789abcdef\n")
        self.assertEqual(ccr_app.read_build_id(self.dir), "2.12 20261124T010203Z 0123456789abcdef")
        for bad in ("", "\n", "a\x1bb\n", "x" * 201 + "\n"):
            (self.dir / "BUILD").write_text(bad)
            self.assertIsNone(ccr_app.read_build_id(self.dir), repr(bad))
        (self.dir / "BUILD").write_bytes("版本\n".encode("utf-8"))
        self.assertIsNone(ccr_app.read_build_id(self.dir))
        self.assertIsNone(ccr_app.read_build_id(ROOT))   # 仓库里没有 BUILD：从源码运行时永远没有标识

    def test_pid_file_build_only_when_given(self):
        lock = ccr_app.InstanceLock(self.dir)
        self.assertTrue(lock.acquire())
        self.assertEqual(json.loads((self.dir / "app.pid").read_text()), {"pid": os.getpid()})   # 与以前逐字相同
        lock.release()
        lock = ccr_app.InstanceLock(self.dir, build="b1")
        self.assertTrue(lock.acquire())
        self.assertEqual(json.loads((self.dir / "app.pid").read_text()), {"pid": os.getpid(), "build": "b1"})
        self.assertEqual(ccr_app.InstanceLock(self.dir).other_build(), "b1")
        lock.release()
        (self.dir / "app.pid").write_text('{"pid": 123}')
        self.assertIsNone(ccr_app.InstanceLock(self.dir).other_build())
        (self.dir / "app.pid").write_text('{"pid": 123, "build": 5}')
        self.assertIsNone(ccr_app.InstanceLock(self.dir).other_build())

    def test_make_dmg_writes_build_before_compile_and_sign(self):
        src = (ROOT / "tools" / "make_dmg.sh").read_text(encoding="utf-8")
        i = src.index('> "$RES/app/BUILD"')
        self.assertLess(i, src.index("compileall"))
        self.assertLess(i, src.index('sign "$APP"'))
        self.assertIn("date -u", src[src.rindex("\n", 0, i - 200):i])   # 带构建时间：同版本重新打包也不同


class TestTakeoverInProcess(unittest.TestCase):
    """main() 的"已有实例"分支（进程内调用：这些路径都不会启动服务器）。已有实例 = 假进程 _HOLDER。"""

    def setUp(self):
        self.dir = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="ccrtk", dir="/tmp")))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.holder = None

    def start_holder(self, ignore_term):
        self.holder = subprocess.Popen([sys.executable, "-I", "-B", "-c", _HOLDER, str(self.dir),
                                        "1" if ignore_term else "0"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._kill_holder)
        self.assertEqual(self.holder.stdout.readline().strip(), "ready")

    def _kill_holder(self):
        if self.holder.poll() is None:
            self.holder.kill()   # 测试自己的假进程
        self.holder.wait(5)
        self.holder.stdout.close()

    def signals(self):
        p = self.dir / "signals"
        return [int(x) for x in p.read_text().split()] if p.exists() else []

    def run_main(self, *argv, build="new-build", timeout=1.0):
        out, err = io.StringIO(), io.StringIO()
        real_kill = os.kill

        def kill(pid, sig):   # 只允许发 SIGTERM / SIGUSR1 / SIGUSR2（绝不强杀）
            self.assertIn(sig, (signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2))
            return real_kill(pid, sig)
        with mock.patch.object(ccr_app, "read_build_id", return_value=build) as rb, \
                mock.patch.object(ccr_app, "TAKEOVER_TIMEOUT", timeout), \
                mock.patch.object(ccr_app.os, "kill", side_effect=kill), \
                redirect_stdout(out), redirect_stderr(err):
            rc = ccr_app.main(["--data-dir", str(self.dir)] + list(argv))
        return rc, out.getvalue(), err.getvalue(), rb

    def test_old_backend_not_exiting_reports_error_without_kill(self):
        self.start_holder(ignore_term=True)
        t0 = time.monotonic()
        rc, out, err, _ = self.run_main("--bundled", "--fake", "--no-browser")
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        self.assertIn("没有退出", err)
        self.assertIn("没有强行结束", err)
        self.assertTrue(wait_until(lambda: self.signals() == [signal.SIGTERM], 3), self.signals())
        time.sleep(0.3)
        self.assertIsNone(self.holder.poll())                                  # 旧后台还活着：没有强杀
        self.assertEqual(self.signals(), [signal.SIGTERM])                     # 只发过一次 SIGTERM
        self.assertEqual(json.loads((self.dir / "app.pid").read_text()), {"pid": self.holder.pid})

    def test_same_build_sends_sigusr1(self):
        self.start_holder(ignore_term=False)
        (self.dir / "app.pid").write_text(json.dumps({"pid": self.holder.pid, "build": "same"}))
        rc, out, _err, _ = self.run_main("--bundled", "--no-browser", build="same")
        self.assertEqual(rc, 0)
        self.assertIn("已让它重新打开窗口", out)
        self.assertTrue(wait_until(lambda: self.signals() == [signal.SIGUSR1], 3), self.signals())
        self.assertIsNone(self.holder.poll())

    def test_bundled_without_own_build_keeps_old_behaviour(self):
        """--bundled 但自己没有 BUILD（例如从源码运行 --bundled）→ 照旧 SIGUSR1，不接管。"""
        self.start_holder(ignore_term=False)
        rc, out, _err, _ = self.run_main("--bundled", "--no-browser", build=None)
        self.assertEqual(rc, 0)
        self.assertTrue(wait_until(lambda: self.signals() == [signal.SIGUSR1], 3), self.signals())
        self.assertIsNone(self.holder.poll())

    def test_not_bundled_never_reads_build_and_sends_sigusr1(self):
        self.start_holder(ignore_term=False)
        rc, out, _err, rb = self.run_main("--no-browser", build="whatever")
        self.assertEqual(rc, 0)
        rb.assert_not_called()
        self.assertIn("已让它重新打开窗口", out)
        self.assertTrue(wait_until(lambda: self.signals() == [signal.SIGUSR1], 3), self.signals())
        self.assertIsNone(self.holder.poll())

    def test_stop_and_reload_unchanged_in_bundled_mode(self):
        self.start_holder(ignore_term=True)
        rc, out, _err, _ = self.run_main("--bundled", "--no-browser", "--jobid", "123")
        self.assertEqual(rc, 0)
        self.assertIn("重新加载配置", out)
        self.assertTrue(wait_until(lambda: self.signals() == [signal.SIGUSR2], 3), self.signals())
        rc, out, _err, _ = self.run_main("--bundled", "--stop")
        self.assertEqual(rc, 0)
        self.assertIn("已通知正在运行的实例", out)
        self.assertTrue(wait_until(lambda: self.signals() == [signal.SIGUSR2, signal.SIGTERM], 3), self.signals())


class TestTakeoverEndToEnd(unittest.TestCase):
    """真实运行 app.py（fake 模式、临时数据目录与 TMUX_TMPDIR、--port 0）：模拟新 .app 原地替换旧 .app。
    每个"版本"是一份 <tmp>/<名字>/Resources/app/（app.py 拷贝 + 指向仓库的 ccr/static/assets 软链接 + 可选 BUILD）。"""

    def setUp(self):
        self.tmp = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="ccrup", dir="/tmp")))
        self.data = self.tmp / "data"
        (self.tmp / "tmux").mkdir()
        (self.tmp / "home").mkdir()
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        live = [p for p in self.procs if p.poll() is None]
        if live:
            subprocess.run([sys.executable, "-I", "-B", str(ROOT / "app.py"), "--data-dir", str(self.data), "--stop"],
                           env=self.env(), capture_output=True, timeout=30)
        for p in self.procs:
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
            p.reader.join(5)
            p.stdout.close()
        if (self.tmp / "tmux").is_dir() and HAVE_TMUX:   # 目录不在时绝不调用 tmux
            for sock in self.tmp.glob("**/tmux-*/*"):
                if sock.is_socket():
                    subprocess.run([TMUX, "-S", str(sock), "kill-server"], capture_output=True, timeout=10)
        shutil.rmtree(self.tmp, True)

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "ANTHROPIC_", "CCR_", "TMUX", "PYTHON"))}
        env.update(HOME=str(self.tmp / "home"), TMUX_TMPDIR=str(self.tmp / "tmux"), PYTHONDONTWRITEBYTECODE="1")
        return env

    def make_app(self, name, build):
        app = self.tmp / name / "Resources" / "app"
        app.mkdir(parents=True)
        shutil.copy2(ROOT / "app.py", app / "app.py")
        for d in ("ccr", "static", "assets"):
            os.symlink(ROOT / d, app / d)
        (app.parent / "bin").mkdir()
        if HAVE_TMUX:
            os.symlink(TMUX, app.parent / "bin" / "tmux")
        if build is not None:
            (app / "BUILD").write_text(build + "\n")
        return app / "app.py"

    def argv(self, app_py, bundled=True):
        return ([sys.executable, "-I", "-B", str(app_py)] + (["--bundled"] if bundled else []) +
                ["--fake", "--no-browser", "--no-sound", "--port", "0", "--data-dir", str(self.data)])

    def start(self, app_py, bundled=True):
        self.data.mkdir(exist_ok=True)
        (self.data / "window_request").write_text("1\n")   # 与原生壳一样：先写请求再运行 app.py
        p = subprocess.Popen(self.argv(app_py, bundled), env=self.env(), cwd=str(self.tmp / "home"),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        p.lines = []
        p.reader = threading.Thread(target=lambda: [p.lines.append(x) for x in p.stdout], daemon=True)
        p.reader.start()
        self.procs.append(p)
        return p

    def pid_info(self):
        try:
            return json.loads((self.data / "app.pid").read_text())
        except (OSError, ValueError):
            return None

    def take_url(self):
        f = self.data / "window_login_url"
        self.assertTrue(wait_until(f.exists, 30), "没有写出 window_login_url")
        url = f.read_text()
        f.unlink()
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/#code=")
        self.assertFalse((self.data / "window_request").exists())
        return url

    def wait_instance(self, p, build):
        want = {"pid": p.pid, "build": build} if build else {"pid": p.pid}
        self.assertTrue(wait_until(lambda: self.pid_info() == want, 30), (self.pid_info(), "".join(p.lines)))
        return self.take_url()

    def test_upgrade_takeover_then_same_build_reopen(self):
        old_app = self.make_app("Old", None)            # 像 2.9：没有 BUILD，app.pid 只有 pid
        new_app = self.make_app("New", "2.12 t1 aaaa")
        newer_app = self.make_app("Newer", "2.12 t2 bbbb")
        old = self.start(old_app)
        self.wait_instance(old, None)
        # 1) 新版本（标识不同）接管：旧后台收到 SIGTERM 正常退出，新进程成为实例并把登录链接交给原生壳
        new = self.start(new_app)
        self.assertEqual(old.wait(20), 0, "".join(old.lines))
        url1 = self.wait_instance(new, "2.12 t1 aaaa")
        self.assertIsNone(new.poll())
        log = (self.data / "logs" / "app.log").read_text(encoding="utf-8")
        self.assertIn("已停止旧版本的后台（pid %d，构建 无标识）" % old.pid, log)
        # 2) 同一版本再打开：只发 SIGUSR1，不重启（同端口、新登录码）
        (self.data / "window_request").write_text("1\n")
        r = subprocess.run(self.argv(new_app), env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已让它重新打开窗口", r.stdout)
        url2 = self.take_url()
        self.assertNotEqual(url1, url2)
        self.assertEqual(url1.split("#")[0], url2.split("#")[0])   # 同一个实例、同一个端口
        self.assertIsNone(new.poll())
        self.assertEqual(self.pid_info(), {"pid": new.pid, "build": "2.12 t1 aaaa"})
        # 3) 再换一个标识不同的构建：同样接管
        newer = self.start(newer_app)
        self.assertEqual(new.wait(20), 0, "".join(new.lines))
        self.wait_instance(newer, "2.12 t2 bbbb")

    def test_not_bundled_unchanged(self):
        """非内置模式：即使 app.py 旁边有 BUILD，app.pid 也只有 pid，再次运行只发 SIGUSR1。"""
        a = self.make_app("A", "x1")
        b = self.make_app("B", "x2")
        first = self.start(a, bundled=False)
        self.wait_instance(first, None)
        self.assertEqual(sorted(self.pid_info()), ["pid"])
        (self.data / "window_request").write_text("1\n")
        r = subprocess.run(self.argv(b, bundled=False), env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已让它重新打开窗口", r.stdout)
        self.take_url()
        self.assertIsNone(first.poll())
        self.assertEqual(self.pid_info(), {"pid": first.pid})


if __name__ == "__main__":
    unittest.main()
