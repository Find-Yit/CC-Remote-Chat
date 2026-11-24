"""remote.py 测试（v2）：校验、§0.2 白名单参数、ssh 参数、引用、注入。不连接任何远端。"""
import itertools
import os
import pathlib
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from ccr import remote

UUID = "9cdf8e9b-fd70-4c80-8b58-e1189e65abe1"
SID = "s0123456789ab"
NASTY_PATHS = [
    "/tmp/it's here",
    "/tmp/$(touch /tmp/ccr_pwned_x)",
    "/tmp/a b  c",
    "/tmp/`id`;rm -rf ~",
    "/tmp/\"dq\" & | > < * ? [x] {a,b} ! # ~",
    "/tmp/中文 目录",
]
WHITELIST = {"--resume", "--model", "--permission-mode", "--dangerously-skip-permissions", "--effort", "--settings"}


class ValidateTests(unittest.TestCase):
    def test_jobid(self):
        for ok in ("1", "123456", "123456789012", "12_3", "999_12345678", " 42 "):
            self.assertEqual(remote.validate_jobid(ok), ok.strip())
        for bad in ("", "abc", "1;rm -rf ~", "1 2", "1\n2", "$(id)", "1234567890123", "1_", "_1",
                    "-1", "1_123456789", "１２", None, 12):
            with self.assertRaises(ValueError, msg=repr(bad)):
                remote.validate_jobid(bad)

    def test_uuid(self):
        self.assertEqual(remote.validate_session_uuid(UUID.upper()), UUID)
        for bad in ("", "x", UUID + "0", UUID[:-1], "../" + UUID, UUID.replace("-", ""), f"{UUID};id"):
            with self.assertRaises(ValueError):
                remote.validate_session_uuid(bad)

    def test_model(self):
        self.assertIsNone(remote.validate_model(""))
        self.assertIsNone(remote.validate_model(None))
        self.assertIsNone(remote.validate_model("  "))
        for ok in ("opus", "claude-opus-5-5", "claude-sonnet-4-5[1m]", "us.anthropic.x:1"):
            self.assertEqual(remote.validate_model(ok), ok)
        for bad in ("a b", "x;id", "$(id)", "a" * 81, "--settings", "-x", "a'b", "a\nb"):
            with self.assertRaises(ValueError, msg=bad):
                remote.validate_model(bad)

    def test_permission_mode(self):
        self.assertEqual(set(remote.PERMISSION_MODES),
                         {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"})
        for m in remote.PERMISSION_MODES:
            self.assertEqual(remote.validate_permission_mode(m), m)
        self.assertEqual(remote.validate_permission_mode("default"), "manual")
        for bad in ("", "bypass", "BYPASSPERMISSIONS", "plan;id", "--dangerously-skip-permissions"):
            with self.assertRaises(ValueError):
                remote.validate_permission_mode(bad)

    def test_effort(self):
        self.assertEqual(remote.EFFORT_LEVELS, ("low", "medium", "high", "xhigh", "max"))
        for e in remote.EFFORT_LEVELS:
            self.assertEqual(remote.validate_effort(e), e)
        self.assertIsNone(remote.validate_effort(None))
        self.assertIsNone(remote.validate_effort(""))
        for bad in ("LOW", "ultra", "low;id", "--effort", "x high", 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                remote.validate_effort(bad)

    def test_path(self):
        for ok in ("/", "~", "~/", "~/a b", "/x/it's", "/x/$(id)", "/x/`id`") + tuple(NASTY_PATHS):
            self.assertEqual(remote.validate_path(ok), ok)
        for bad in ("", "rel/path", "./x", "~root", "~root/x", "/a\nb", "/a\rb", "/a\x00b",
                    "/" + "a" * 4096, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                remote.validate_path(bad)

    def test_host(self):
        self.assertEqual(remote.validate_host("login-01"), "login-01")
        self.assertEqual(remote.validate_host("user@host.example"), "user@host.example")
        for bad in ("", "-oProxyCommand=id", "a b", "h;id", "h\nx", "ħost"):
            with self.assertRaises(ValueError):
                remote.validate_host(bad)

    def test_extra_srun_args(self):
        self.assertEqual(remote.parse_extra_srun_args(""), [])
        self.assertEqual(remote.parse_extra_srun_args(None), [])
        self.assertEqual(remote.parse_extra_srun_args("--gres=gpu:1  --mem=8G"), ["--gres=gpu:1", "--mem=8G"])
        self.assertEqual(remote.parse_extra_srun_args("--gres=gpu:1"), ["--gres=gpu:1"])
        ok = ("--gpus=2 --gpus-per-node=a100:1 --gpus-per-task=1 --cpus-per-task=8 --cpus-per-gpu=4 "
              "--mem=64G --mem-per-cpu=4000M --mem-per-gpu=80G --time=1-00:00:00 --cpu-bind=cores "
              "--gpu-bind=closest --gres=gpu:a100:1,shard:2 --mem=50% --time=30+ --gres=a@b.c_d-e")
        self.assertEqual(remote.parse_extra_srun_args(ok), ok.split())
        self.assertEqual(set(remote.SRUN_ARG_WHITELIST), {
            "--gres", "--gpus", "--gpus-per-node", "--gpus-per-task", "--cpus-per-task", "--cpus-per-gpu",
            "--mem", "--mem-per-cpu", "--mem-per-gpu", "--time", "--cpu-bind", "--gpu-bind"})
        bad_list = (
            # FIXES F2 必测
            "--output=", "--output=x", "--output=/tmp/x", "--chd=/tmp", "--chd=x", "-l", "--task-prolog=x",
            "--task-prolog=/tmp/x.sh",
            # 缩写 / 短选项 / 无值 / 非白名单
            "--gre=gpu:1", "--me=8G", "-c4", "-c", "-v", "--gres", "--gres=", "--mem 8G", "-G=1",
            "--export=ALL", "--chdir=/tmp", "--jobid=1", "-D", "--pty", "--overlap", "-J=x", "--job-name=x",
            "--error=x", "--input=x", "--propagate=ALL", "--get-user-env", "--uid=0",
            # 取值不合法：含 / 、引号、shell 元字符、= 、过长
            "--gres=a/b", "--gres=../x", "--x=$(id)", "; rm -rf ~", "--gres=b c", "--gres='b'", "--gres=b;c",
            "--gres=`id`", "--gres=a=b", "--gres=" + "a" * 65, "--GRES=gpu:1", "--gres=gpu:1\n", "foo")
        for bad in bad_list:
            with self.assertRaises(ValueError, msg=bad):
                remote.parse_extra_srun_args(bad)
        with self.assertRaises(ValueError) as cm:
            remote.parse_extra_srun_args("--output=x")
        self.assertIn("--gres", str(cm.exception))   # 中文错误信息列出允许项
        self.assertIn("只允许", str(cm.exception))
        self.assertEqual(remote.parse_extra_srun_args("--gres=" + "a" * 64), ["--gres=" + "a" * 64])

    def test_sid(self):
        self.assertEqual(remote.validate_sid(SID), SID)
        for bad in ("", "s0123", "s0123456789AB", "x0123456789ab", "s0123456789abc", "s0123456789a;",
                    "s0123456789ab\n", None, 5, "ccr-s0123456789ab"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                remote.validate_sid(bad)


class ClaudeArgsTests(unittest.TestCase):
    def test_flag_table_is_exactly_spec(self):
        self.assertEqual(set(remote.CLAUDE_ARG_FLAGS), WHITELIST)
        self.assertFalse(remote.CLAUDE_ARG_FLAGS["--dangerously-skip-permissions"])
        self.assertFalse(hasattr(remote, "build_claude_argv"))
        self.assertFalse(hasattr(remote, "spawn_session"))
        self.assertFalse(hasattr(remote, "build_session_remote_command"))

    def test_nothing_selected_is_empty(self):
        self.assertEqual(remote.build_claude_args(), [])
        self.assertEqual(remote.build_claude_args(model="", permission_mode="", effort="", resume=""), [])
        self.assertEqual(remote.build_claude_args(None, None, False, None, None), [])

    def test_exact(self):
        self.assertEqual(
            remote.build_claude_args(model="claude-opus-5-5[1m]", permission_mode="plan", skip_permissions=True,
                                     effort="xhigh", resume=UUID.upper()),
            ["--resume", UUID, "--model", "claude-opus-5-5[1m]", "--permission-mode", "plan",
             "--dangerously-skip-permissions", "--effort", "xhigh"])
        self.assertEqual(remote.build_claude_args(permission_mode="default"), ["--permission-mode", "manual"])

    def test_all_combinations_whitelisted(self):
        for model, mode, skip, effort, resume in itertools.product(
                (None, "opus"), (None,) + remote.PERMISSION_MODES, (False, True), (None,) + remote.EFFORT_LEVELS,
                (None, UUID)):
            args = remote.build_claude_args(model, mode, skip, effort, resume)
            remote.assert_claude_args(args)
            flags = [a for a in args if a.startswith("-")]
            self.assertTrue(set(flags) <= WHITELIST, args)
            self.assertEqual(len(flags), len(set(flags)))
            for bad in ("-p", "--print", "--input-format", "--output-format", "--settings", "--session-id"):
                self.assertNotIn(bad, args)

    def test_injection_rejected(self):
        for kw in ({"model": "x;id"}, {"model": "--settings"}, {"model": "$(id)"}, {"model": "a b"},
                   {"permission_mode": "plan --settings x"}, {"permission_mode": "bypass"}, {"effort": "max;id"},
                   {"resume": "not-a-uuid"}, {"resume": UUID + " --settings x"}, {"skip_permissions": "yes"}):
            with self.assertRaises(ValueError, msg=repr(kw)):
                remote.build_claude_args(**kw)

    def test_assert_guard(self):
        for bad in (["-p"], ["--print"], ["--settings", "x"], ["--model"], ["--model", "a b"], ["--model=x"],
                    ["--effort", "ultra"], ["--permission-mode", "default"], ["--resume", "x"],
                    ["--dangerously-skip-permissions", "--dangerously-skip-permissions"],
                    ["--model", "a", "--model", "b"], ["claude"], ["--append-system-prompt", "x"], "--model x"):
            with self.assertRaises(AssertionError, msg=repr(bad)):
                remote.assert_claude_args(bad)

    def test_settings_only_agent_key_file(self):
        """--settings 只允许 <HOME>/.cc-remote-chat/keys/<16hex>.json，且必须给出远端 HOME 才能校验。"""
        home = "/mnt/ssd/home/u"
        good = home + "/.cc-remote-chat/keys/0123456789abcdef.json"
        self.assertEqual(remote.build_claude_args(model="haiku", settings_path=good, home=home),
                         ["--model", "haiku", "--settings", good])
        remote.assert_claude_args(["--resume", UUID, "--settings", good], home=home)
        remote.assert_claude_args(["--settings", good], home=home + "/")   # HOME 规范化
        self.assertEqual(remote.validate_settings_path("/.cc-remote-chat/keys/0123456789abcdef.json", "/"),
                         "/.cc-remote-chat/keys/0123456789abcdef.json")
        with self.assertRaises(AssertionError):
            remote.assert_claude_args(["--settings", good])          # 没有 home → 拒绝
        bad_paths = ["{}", '{"env":{"ANTHROPIC_AUTH_TOKEN":"x"}}', "/tmp/x.json", good + " ", good + "\n",
                     home + "/.cc-remote-chat/keys/0123456789ABCDEF.json", home + "/.cc-remote-chat/keys/abc.json",
                     home + "/.cc-remote-chat/keys/../../.claude/settings.json",
                     home + "/.cc-remote-chat/keys/0123456789abcdef.json.bak",
                     "/other/home/.cc-remote-chat/keys/0123456789abcdef.json",
                     home + "x/.cc-remote-chat/keys/0123456789abcdef.json", home + "/.claude/settings.json",
                     "~/.cc-remote-chat/keys/0123456789abcdef.json"]
        for p in bad_paths:
            with self.assertRaises(AssertionError, msg=p):
                remote.assert_claude_args(["--settings", p], home=home)
            with self.assertRaises(ValueError, msg=p):
                remote.build_claude_args(settings_path=p, home=home)
        for h in (None, "", "rel", "/a\nb"):
            with self.assertRaises(ValueError, msg=repr(h)):
                remote.validate_settings_path(good, h)
        with self.assertRaises(AssertionError):
            remote.assert_claude_args(["--settings", good, "--settings", good], home=home)


class FakePathTests(unittest.TestCase):
    def test_defaults_follow_data_dir(self):
        cfg = {"fake": True, "control_dir": "/tmp/ccrdata/ssh"}
        self.assertEqual(remote.fake_home(cfg), os.path.realpath("/tmp/ccrdata/fake/home"))
        self.assertEqual(remote.fake_tmpdir(cfg), os.path.realpath("/tmp/ccrdata/fake/tmux"))
        cfg = {"fake": True, "fake_home": "/tmp/h", "fake_tmpdir": "/tmp/t"}
        self.assertEqual(remote.fake_home(cfg), os.path.realpath("/tmp/h"))
        self.assertEqual(remote.fake_tmpdir(cfg), os.path.realpath("/tmp/t"))

    def test_parse_job_steps(self):
        out = "276403.0|bash\n276403.11|ccr-keeper\n276403.batch|ccr-keeper\njunk\n276403.12|ccr-agent\n"
        self.assertEqual(remote.parse_job_steps(out),
                         [("276403.0", "bash"), ("276403.11", "ccr-keeper"), ("276403.12", "ccr-agent")])
        self.assertEqual(remote.parse_job_steps(""), [])


class SshArgvTests(unittest.TestCase):
    def test_base_argv_multiplex(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            cdir = pathlib.Path(td, "app", "ssh")
            argv = remote.ssh_base_argv({"host": "login-01", "ssh_multiplex": True}, control_dir=cdir)
            self.assertEqual(argv, [
                "ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=4", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={cdir}/cm-%C", "-o", "ControlPersist=600", "login-01"])
            self.assertTrue(cdir.is_dir())
            self.assertEqual(stat.S_IMODE(cdir.stat().st_mode), 0o700)

    def test_control_dir_from_runtime_cfg(self):
        """F6-3：运行时配置注入的 control_dir 被所有 ssh 使用（run_remote / run_remote_python / agent）。"""
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            cdir = pathlib.Path(td, "data", "ssh")
            cfg = {"host": "login-01", "ssh_multiplex": True, "control_dir": str(cdir)}
            self.assertIn(f"ControlPath={cdir}/cm-%C", remote.ssh_base_argv(cfg))
            self.assertTrue(cdir.is_dir())
            with mock.patch.object(remote.subprocess, "run") as R:
                R.return_value = subprocess.CompletedProcess([], 0, "", "")
                remote.run_remote(cfg, ["true"])
                self.assertIn(f"ControlPath={cdir}/cm-%C", R.call_args[0][0])
                remote.run_remote_python(cfg, "pass", [])
                self.assertIn(f"ControlPath={cdir}/cm-%C", R.call_args[0][0])
            # 显式参数优先
            other = pathlib.Path(td, "o")
            self.assertIn(f"ControlPath={other}/cm-%C", remote.ssh_base_argv(cfg, control_dir=other))

    def test_control_path_too_long_disables_multiplex(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            cdir = pathlib.Path(td, "x" * 60, "ssh")
            argv = remote.ssh_base_argv({"host": "h", "control_dir": str(cdir)})
            self.assertFalse(any(a.startswith("Control") for a in argv))
            self.assertEqual(argv[-1], "h")

    def test_default_control_path_literal(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td, \
                mock.patch.object(remote, "APP_DIR", pathlib.Path(td, ".cc-remote-chat")):
            argv = remote.ssh_base_argv({"host": "login-01"})
            self.assertIn("ControlPath=~/.cc-remote-chat/ssh/cm-%C", argv)
            self.assertTrue(pathlib.Path(td, ".cc-remote-chat", "ssh").is_dir())

    def test_no_multiplex_no_env(self):
        argv = remote.ssh_base_argv({"host": "login-01", "ssh_multiplex": False})
        self.assertEqual(argv, ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
                                "-o", "ServerAliveCountMax=4", "login-01"])
        joined = " ".join(argv)
        for bad in ("SendEnv", "SetEnv", "-A", "-X", "ProxyCommand", "LocalCommand"):
            self.assertNotIn(bad, joined)

    def test_bad_host(self):
        with self.assertRaises(ValueError):
            remote.ssh_base_argv({"host": "-oProxyCommand=touch /tmp/x", "ssh_multiplex": False})


class RunRemoteTests(unittest.TestCase):
    CFG = {"host": "login-01", "ssh_multiplex": False}

    def test_run_remote_quoting(self):
        with mock.patch.object(remote.subprocess, "run") as R:
            R.return_value = subprocess.CompletedProcess([], 0, "out", "")
            rc, out, err = remote.run_remote(self.CFG, ["ls", "-1", "/a b/it's", "$(id)"], timeout=5)
            self.assertEqual((rc, out), (0, "out"))
            argv = R.call_args[0][0]
            kw = R.call_args[1]
            self.assertFalse(kw.get("shell"))
            self.assertNotIn("env", kw)
            self.assertEqual(argv[:-1], remote.ssh_base_argv(self.CFG))
            outer = shlex.split(argv[-1])
            self.assertEqual(outer[:2], ["bash", "-lc"])
            self.assertEqual(shlex.split(outer[2]), ["ls", "-1", "/a b/it's", "$(id)"])

    def test_run_remote_retries_on_255(self):
        with mock.patch.object(remote.subprocess, "run") as R, mock.patch.object(remote.time, "sleep"):
            R.return_value = subprocess.CompletedProcess([], 255, "", "MaxStartups")
            rc, _, err = remote.run_remote(self.CFG, ["true"])
            self.assertEqual(rc, 255)
            self.assertEqual(R.call_count, 3)
            R.reset_mock()
            R.return_value = subprocess.CompletedProcess([], 1, "", "")
            remote.run_remote(self.CFG, ["false"])
            self.assertEqual(R.call_count, 1)

    def test_run_remote_python(self):
        with mock.patch.object(remote.subprocess, "run") as R:
            R.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            remote.run_remote_python(self.CFG, "print(1)\n", ["a b", "it's", "$(id)"])
            argv = R.call_args[0][0]
            self.assertEqual(R.call_args[1]["input"], "print(1)\n")
            self.assertEqual(shlex.split(argv[-1]), ["python3", "-I", "-", "a b", "it's", "$(id)"])

    def test_resolve_remote_path(self):
        remote._home_cache.clear()
        cfg = {"host": "test-host-x", "ssh_multiplex": False}
        with mock.patch.object(remote, "run_remote", return_value=(0, "motd junk\n/home/u\n", "")) as R:
            self.assertEqual(remote.resolve_remote_path(cfg, "~"), "/home/u")
            self.assertEqual(remote.resolve_remote_path(cfg, "~/a b/"), "/home/u/a b")
            self.assertEqual(remote.resolve_remote_path(cfg, "/x/../y"), "/y")
            self.assertEqual(R.call_count, 1)  # 缓存
            self.assertEqual(R.call_args[0][1], ["printenv", "HOME"])
        remote._home_cache.clear()
        with mock.patch.object(remote, "run_remote", return_value=(255, "", "refused")):
            with self.assertRaises(RuntimeError):
                remote.resolve_remote_path(cfg, "~/x")
        with self.assertRaises(ValueError):
            remote.resolve_remote_path(cfg, "~/a\nb")
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            fh = os.path.realpath(td)
            self.assertEqual(remote.resolve_remote_path({"fake": True, "fake_home": fh}, "~/q"), fh + "/q")


class CloseControlMasterTests(unittest.TestCase):
    def test_exit_argv_and_noop_cases(self):
        from ccr import remote as r
        # macOS 默认临时目录路径过长会让 ssh_base_argv 关闭复用，这里用 /tmp 下的短路径
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            cfg = {"host": "login-01", "ssh_multiplex": True, "control_dir": td}
            with mock.patch.object(r.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0)
                self.assertTrue(r.close_control_master(cfg))
                argv = run.call_args[0][0]
                self.assertEqual(argv[-3:], ["-O", "exit", "login-01"])
                self.assertIn("ControlPath=" + td + "/cm-%C", argv)
                self.assertNotIn("SendEnv", " ".join(argv))
                run.reset_mock()
                self.assertFalse(r.close_control_master(dict(cfg, fake=True)))
                self.assertFalse(r.close_control_master(dict(cfg, ssh_multiplex=False)))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
