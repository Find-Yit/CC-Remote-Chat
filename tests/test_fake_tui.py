"""fake_tui 测试：画面与真实 fixtures 逐字对照、按键解析、回合/对话框、sessions json 与 transcript。

不需要 tmux（直接驱动 FakeClaude 对象、固定 80x24 尺寸，与 fixtures 录制尺寸一致）。
"""
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
import unittest

from ccr import fake_tui

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
FIX_HOME = "/mnt/ssd/home/demouser"


def load_screens():
    out = {}
    for name in ("tui_screens_stream.ndjson", "tui_screens_dialogs.ndjson"):
        for line in (FIX / name).read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if "plain" in d:
                out.setdefault(d["name"], []).append(d["plain"].split("\n"))
    return out


SCREENS = load_screens()


def fx(name, idx=-1):
    """fixture 画面行（rstrip）。同名画面取最后一个（dialogs 录制）。"""
    return [x.rstrip() for x in SCREENS[name][idx]]


def block(lines, start_pred, n):
    for i, x in enumerate(lines):
        if start_pred(x):
            return lines[i:i + n]
    raise AssertionError("没有找到起始行")


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="ccrft", dir="/tmp")
        self.home = os.path.realpath(self.td)
        self.proj = os.path.join(self.home, "proj")
        os.makedirs(self.proj)
        self.env = {"HOME": self.home, "USER": "demouser", "CCR_FAKE_DELAY": "0", "CCR_FAKE_THINK": "0"}

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def app(self, argv=(), cwd=None, **env):
        e = dict(self.env)
        e.update(env)
        a = fake_tui.FakeClaude(list(argv), env=e, size=(80, 24), cwd=cwd or self.proj)
        a.host = "gpu0001"
        return a

    def frame(self, a):
        return [x.rstrip() for x in a.frame()]

    def run_turn(self, a, limit=5000):
        for _ in range(limit):
            if a.turn is None or a.dialog is not None:
                return
            a.turn_due = 0
            a.step_turn()
        raise AssertionError("回合没有结束")

    def submit(self, a, text):
        a.on_text(text)
        a.on_key("Enter")

    def transcript(self, a):
        with open(a.tpath, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def session_json(self, a):
        with open(os.path.join(self.home, ".claude", "sessions", "%d.json" % a.pid), encoding="utf-8") as f:
            return json.load(f)


class ScreenFixtureTests(Base):
    def test_trust_dialog_matches_fixture(self):
        a = self.app(cwd=self.home)
        self.assertEqual(a.dialog.kind, "trust")
        want = [x.replace(FIX_HOME, self.home) for x in fx("trust")]
        self.assertEqual(self.frame(a)[:18], want[:18])
        self.assertEqual(self.session_json(a)["status"], "waiting")
        a.on_key("Down")
        self.assertEqual(self.frame(a)[15:17], fx("trust_down")[15:17])
        a.on_key("Enter")
        self.assertIsNone(a.dialog)
        self.assertEqual(self.session_json(a)["status"], "idle")

    def test_trust_no_exit(self):
        a = self.app(cwd=self.home)
        a.on_key("Enter")
        self.assertEqual(a.exit_code, 1)
        a = self.app(CCR_FAKE_TRUST="1")
        self.assertEqual(a.dialog.kind, "trust")
        self.assertNotIn(" home folder, so Yes lasts for this session only.", self.frame(a))

    def test_ready_layout(self):
        a = self.app()
        got = self.frame(a)
        ready = fx("ready")  # dialogs 录制：manual 模式
        self.assertEqual(got[1], ready[1])
        self.assertEqual(got[2], ready[2])
        self.assertEqual(got[3], " ▝▝   ▝▝   " + self.proj)
        self.assertEqual(got[-6:-3], [ready[-6], "❯", ready[-4]])
        self.assertEqual(got[-2], ready[-2])  # "  ⏸ manual mode on"
        self.assertEqual(got[-1], "")
        self.assertRegex(got[-3], r"^  demouser@gpu0001:~/proj ▱{10} 0% 0/1M \$0\.0000 \[Haiku 5\.5 · 1M · effort…$")
        a.fixed_size = (200, 50)
        self.assertRegex(self.frame(a)[-3],
                         r"^  demouser@gpu0001:~/proj ▱{10} 0% 0/1M \$0\.0000 \[Haiku 5\.5 · 1M · effort:low\]$")
        self.assertEqual(self.frame(a)[-4], "─" * 200)
        a.fixed_size = (80, 24)
        a.mode = "auto"
        self.assertEqual(self.frame(a)[-2], fx("ready", 0)[-2])  # "  ⏵⏵ auto mode on (shift+tab to cycle)"

    def test_slash_popup_matches_fixture(self):
        a = self.app()
        a.on_text("/model")
        got = self.frame(a)
        want = fx("slash_popup")
        self.assertEqual(got[-11:-6], want[-11:-6])  # 4 行补全 + 横线
        self.assertEqual(a.frame()[-5].rstrip(), "❯\xa0/model")

    def test_model_panel_matches_fixture(self):
        a = self.app()
        self.submit(a, "/model")
        self.assertEqual(a.dialog.kind, "model")
        start = lambda x: x.startswith("▔")
        self.assertEqual(block(self.frame(a), start, 15), block(fx("model"), start, 15))
        a.on_key("Right")
        self.assertEqual(block(self.frame(a), start, 15), block(fx("model_right"), start, 15))
        a.on_key("Left")
        a.on_key("Down")
        self.assertEqual(block(self.frame(a), start, 15), block(fx("model_down"), start, 15))
        a.on_key("Escape")
        got = self.frame(a)
        self.assertIn("❯ /model", got)
        self.assertIn("  ⎿ \xa0Kept model as Haiku 5.5".rstrip(), [x for x in a.frame()])
        self.assertIsNone(a.dialog)
        sysent = [e for e in self.transcript(a) if e["type"] == "system"]
        self.assertEqual(sysent[-1]["content"], "<local-command-stdout>Kept model as `Haiku 5.5`</local-command-stdout>")

    def test_model_scroll_and_apply(self):
        a = self.app()
        self.submit(a, "/model")
        for _ in range(6):
            a.on_key("Up")
        got = self.frame(a)
        self.assertIn("  ❯ 1. Default (recommended)  Use the default model (currently Opus 5.5) ·", got)
        self.assertEqual(got[12], "")  # 已在顶部：没有 "↑ N more"
        self.assertIn("    ↓ 3 more", got)
        names = []
        for _ in range(6):
            names.append(fake_tui.MODELS[a.dialog.cursor]["name"])
            a.on_key("Down")
        self.assertEqual(names, ["Default (recommended)", "Opus 5.5", "Fable 5.1", "Sonnet", "Haiku",
                                 "Fable 5.2 (1M)"])
        a.dialog.cursor, a.dialog.top = 3, 1
        a.on_key("Right")
        a.on_key("Right")
        self.assertIn("  ● High effort ←/→ to adjust", self.frame(a))
        a.on_key("s")
        self.assertEqual((a.model, a.effort), ("claude-sonnet-5-5", "high"))
        self.assertIn(fake_tui.RES + "Set model to Sonnet 5.5 with high effort (this session only)", a.frame())
        a.fixed_size = (200, 50)
        self.assertIn("[Sonnet 5.5 · 1M · effort:high]", self.frame(a)[-3])

    def test_effort_panel_matches_fixture(self):
        a = self.app()
        self.submit(a, "/effort")
        start = lambda x: x.startswith("▔")
        self.assertEqual(block(self.frame(a), start, 9), block(fx("effort"), start, 9))
        a.on_key("Right")
        bar = [x for x in self.frame(a) if "▲" in x][0]
        labels = "         low     medium     high     xhigh      max"
        col = bar.index("▲")
        self.assertEqual(labels[col - 3:col + 3].strip(), "medium")
        a.on_key("Tab")
        self.assertIn("Ultracode  on", [x for x in self.frame(a) if "▲" in x][0])
        a.on_key("Escape")
        self.assertIn(fake_tui.RES + "Cancelled", a.frame())
        self.submit(a, "/effort")
        a.on_key("Right")
        a.on_key("Enter")
        self.assertEqual(a.effort, "medium")
        self.assertIn(fake_tui.RES + "Set effort level to medium", a.frame())

    def test_bash_permission_matches_fixture(self):
        a = self.app()
        self.submit(a, "用 Bash /tool")
        self.run_turn(a)
        self.assertEqual(a.dialog.kind, "perm")
        self.assertEqual(self.session_json(a)["status"], "waiting")
        want = [x.replace("ccr_tui_probe_denied2", "ccr_fake_probe") for x in fx("perm")]
        start = lambda x: x == " Bash command"
        self.assertEqual(block(self.frame(a), start, 15), block(want, start, 15))
        got = self.frame(a)
        self.assertIn("  Creating probe file in /tmp", got)
        self.assertIn("  ⎿  $ touch /tmp/ccr_fake_probe", got)
        self.assertEqual(got[-1], "")
        a.on_key("Escape")
        self.run_turn(a)
        got = self.frame(a)
        self.assertIn("  Did not run 1 call", got)
        self.assertIn("  ⎿ \xa0Interrupted · What should Claude do instead?", a.frame()[-8:] + got)
        self.assertFalse(os.path.exists("/tmp/ccr_fake_probe"))  # 绝不真的执行
        t = self.transcript(a)
        self.assertEqual(t[-1]["message"]["content"][0]["text"], "[Request interrupted by user for tool use]")
        self.assertEqual(t[-2]["message"]["content"][0]["type"], "tool_result")
        self.assertTrue(t[-2]["message"]["content"][0]["is_error"])
        self.assertEqual(self.session_json(a)["status"], "idle")

    def test_bash_permission_yes(self):
        a = self.app()
        self.submit(a, "/tool")
        self.run_turn(a)
        a.on_key("1")
        self.run_turn(a)
        self.assertIsNone(a.turn)
        self.assertIn("  Ran 1 shell command", self.frame(a) + [x.rstrip() for x in a.frame()])
        types = [(e["type"], e["message"]["content"][0]["type"] if isinstance(e["message"]["content"], list)
                  else "str") for e in self.transcript(a) if e["type"] in ("user", "assistant")]
        self.assertEqual(types, [("user", "str"), ("assistant", "tool_use"), ("user", "tool_result"),
                                 ("assistant", "text")])

    def test_write_permission_matches_fixture(self):
        a = self.app()
        self.submit(a, "/write")
        self.run_turn(a)
        want = [x.replace(".ccr_probe_write_denied.txt", ".ccr_fake_write.txt") for x in fx("perm_write")]
        start = lambda x: x == " Create file"
        self.assertEqual(block(self.frame(a), start, 13), block(want, start, 13))
        a.on_key("3")
        self.run_turn(a)
        got = [x.rstrip() for x in a.frame()]
        self.assertIn("● Write(.ccr_fake_write.txt)", got)
        self.assertIn("  ⎿ \xa0Rejected write to .ccr_fake_write.txt", got)
        self.assertFalse(os.path.exists(os.path.join(self.proj, ".ccr_fake_write.txt")))

    def test_ask_matches_fixture(self):
        a = self.app()
        self.submit(a, "请 /ask")
        self.run_turn(a)
        self.assertEqual(a.dialog.kind, "ask")
        start = lambda x: x.startswith(" ☐")
        self.assertEqual(block(self.frame(a), start, 13), block(fx("model_down", 0), start, 13))
        a.on_key("Escape")
        self.run_turn(a)
        got = [x.rstrip() for x in a.frame()]
        self.assertIn("● Declined to answer Claude's questions:", got)
        self.assertIn("  ⎿ \xa0· 你更喜欢红色还是蓝色？ (红色 / 蓝色)", got)

    def test_ask_type_something(self):
        a = self.app()
        self.submit(a, "/ask")
        self.run_turn(a)
        a.on_key("Down")
        a.on_key("Down")
        a.on_key("Enter")  # 空文本：不提交
        self.assertEqual(a.dialog.kind, "ask")
        a.feed(b"\x1b[200~\xe7\xbb\xbf\xe8\x89\xb2 y\x1b[201~")  # 粘贴"绿色 y"
        self.assertIn("❯ 3. 绿色 y", self.frame(a))
        a.on_key("Enter")
        self.run_turn(a)
        self.assertIn("  ⎿ \xa0· 你更喜欢红色还是蓝色？ → 绿色 y", [x.rstrip() for x in a.frame()])


class TurnTests(Base):
    def test_reply_transcript_and_status(self):
        a = self.app()
        self.assertEqual(self.session_json(a), {
            "pid": a.pid, "sessionId": a.session_id, "cwd": self.proj, "startedAt": a.started_ms,
            "kind": "interactive", "entrypoint": "cli", "status": "idle", "version": fake_tui.VERSION})
        self.submit(a, "你好\n第二行")
        self.assertEqual(self.session_json(a)["status"], "busy")
        a.turn_due = 0
        a.step_turn()
        got = self.frame(a)
        self.assertTrue(got[-2].endswith(" · esc to interrupt"), got[-2])
        self.run_turn(a)
        self.assertEqual(self.session_json(a)["status"], "idle")
        got = self.frame(a)
        self.assertIn("❯ 你好", got)
        self.assertIn("  第二行", got)
        self.assertIn("● 收到：你好", got)
        self.assertTrue(any(re.match(r"^✻ Baked for \d+s · done \d{1,2}:\d\d [AP]M$", x) for x in got))
        slug = re.sub(r"[^A-Za-z0-9]", "-", self.proj)
        self.assertEqual(a.tpath, os.path.join(self.home, ".claude", "projects", slug, a.session_id + ".jsonl"))
        ents = [e for e in self.transcript(a) if e["type"] in ("user", "assistant")]
        self.assertEqual(ents[0]["message"], {"role": "user", "content": "你好\n第二行"})
        self.assertEqual(ents[1]["message"]["content"][0]["type"], "text")
        self.assertEqual(ents[1]["parentUuid"], ents[0]["uuid"])
        for e in ents:
            self.assertEqual(e["sessionId"], a.session_id)
            self.assertFalse(e["isSidechain"])
        types = {e["type"] for e in self.transcript(a)}
        self.assertTrue({"permission-mode", "file-history-snapshot", "attachment", "last-prompt"} <= types)

    def test_spinner_format(self):
        a = self.app()
        self.submit(a, "/long")
        a.turn_started -= 5
        sp = a.spinner_text()
        self.assertRegex(sp, r"^[·✢✳✶✻✽] \w+… \(5s · thinking with low effort\)$")
        self.assertIn(fake_tui.RES + "Tip: ", "\n".join(a.frame()))
        a.turn_tokens = 175
        self.assertRegex(a.spinner_text(), r"^[·✢✳✶✻✽] \w+… \(5s · ↓ 175 tokens · thinking with low effort\)$")
        a.turn_tokens = 1234   # 与真实 spinner 相同：≥1000 写成 1.2k
        self.assertRegex(a.spinner_text(), r"^[·✢✳✶✻✽] \w+… \(5s · ↓ 1\.2k tokens · thinking with low effort\)$")
        self.assertEqual([fake_tui.fmt_tokens(n) for n in (0, 999, 1000, 12345)], ["0", "999", "1.0k", "12.3k"])

    def test_no_statusline_option(self):
        """CCR_FAKE_STATUSLINE=0：不画状态栏（模拟用户没配 statusline）；输入框/模式行不变。"""
        a = self.app(CCR_FAKE_STATUSLINE="0")
        got = self.frame(a)
        self.assertFalse(any("$0." in l for l in got))
        self.assertEqual(got[-3], "─" * 80)
        self.assertEqual(got[-2], "  ⏸ manual mode on")
        self.submit(a, "你好")
        self.run_turn(a)
        u = [e for e in self.transcript(a) if e["type"] == "assistant"][-1]["message"]["usage"]
        self.assertEqual((u["input_tokens"], u["cache_creation_input_tokens"], u["cache_read_input_tokens"]),
                         (2, 1200, 0))

    def test_long_interrupt(self):
        a = self.app()
        self.submit(a, "/long")
        for _ in range(30):
            a.turn_due = 0
            a.step_turn()
        self.assertIsNotNone(a.turn)
        a.feed(b"\x1b", final=True)
        self.assertIsNone(a.turn)
        got = [x.rstrip() for x in a.frame()]
        self.assertIn("  ⎿ \xa0Interrupted · What should Claude do instead?", got)
        self.assertFalse(got[-2].endswith("esc to interrupt"))
        self.assertEqual(self.transcript(a)[-1]["message"]["content"][0]["text"], "[Request interrupted by user]")

    def test_md(self):
        a = self.app()
        self.submit(a, "/md")
        self.run_turn(a)
        got = self.frame(a)
        self.assertIn("● Markdown 示例", got)
        ent = [e for e in self.transcript(a) if e["type"] == "assistant"][-1]
        self.assertIn("**粗体**", ent["message"]["content"][0]["text"])

    def test_queue_while_busy(self):
        a = self.app()
        self.submit(a, "第一条")
        self.submit(a, "第二条")
        self.run_turn(a)
        self.run_turn(a)
        users = [e["message"]["content"] for e in self.transcript(a)
                 if e["type"] == "user" and isinstance(e["message"]["content"], str)]
        self.assertEqual(users, ["第一条", "第二条"])

    def test_resume_and_clear(self):
        a = self.app()
        self.submit(a, "记住这句")
        self.run_turn(a)
        sid = a.session_id
        b = self.app(["--resume", sid.upper(), "--model", "opus", "--effort", "max"])
        self.assertEqual(b.session_id, sid)
        self.assertEqual(b.tpath, a.tpath)
        self.assertIn("❯ 记住这句", self.frame(b))
        self.submit(b, "继续")
        self.run_turn(b)
        users = [e for e in self.transcript(b) if e["type"] == "user" and isinstance(e["message"]["content"], str)]
        self.assertEqual([u["message"]["content"] for u in users], ["记住这句", "继续"])
        b.fixed_size = (200, 50)
        self.assertIn("[Opus 5.5 · 1M · effort:max]", self.frame(b)[-3])
        self.submit(b, "/clear")
        self.assertNotEqual(b.session_id, sid)
        self.assertEqual(self.session_json(b)["sessionId"], b.session_id)
        self.assertNotIn("❯ 记住这句", self.frame(b))

    def test_mode_cycle(self):
        a = self.app()
        seen = []
        for _ in range(5):
            seen.append(a.mode)
            a.feed(b"\x1b[Z")
        self.assertEqual(seen, ["manual", "acceptEdits", "plan", "auto", "manual"])
        b = self.app(["--dangerously-skip-permissions"])
        self.assertEqual(b.mode, "bypassPermissions")
        self.assertEqual(self.frame(b)[-2], "  ⏵⏵ bypass permissions on (shift+tab to cycle)")
        seen = []
        for _ in range(6):   # 真机实测：带跳过权限启动时 bypass → auto → manual → accept edits → plan → bypass
            b.on_key("BTab")
            seen.append(b.mode)
        self.assertEqual(seen, ["auto", "manual", "acceptEdits", "plan", "bypassPermissions", "auto"])
        c = self.app(["--permission-mode", "acceptEdits"])
        self.assertEqual(self.frame(c)[-2], "  ⏵⏵ accept edits on (shift+tab to cycle)")

    def test_bad_args(self):
        for argv in (["-p"], ["--settings", "x"], ["--resume", "nope"], ["--effort", "ultra"],
                     ["--permission-mode", "x"], ["--model"]):
            with self.assertRaises(SystemExit, msg=argv), contextlib.redirect_stderr(io.StringIO()):
                self.app(argv)


class InputTests(Base):
    def test_keys_and_paste(self):
        a = self.app()
        a.feed(b"ab\x7fc")
        self.assertEqual(a.input, "ac")
        a.feed(b"\x1b[200~x\ry\r\xe4\xb8\xad\x1b[201~")
        self.assertEqual(a.input, "acx\ny\n中")
        got = self.frame(a)
        self.assertIn("❯\xa0acx", a.frame()[-7])
        self.assertTrue(got[-9].endswith("ctrl+g to edit in Vim"), got[-9])
        self.assertEqual(got[-6:-4], ["  y", "  中"])
        a.feed(b"\x15")  # C-u 删除当前行
        self.assertEqual(a.input, "acx\ny\n")
        rest = a.feed(b"\x1b")
        self.assertEqual(rest, b"\x1b")  # 单独 ESC：等待
        self.assertEqual(a.feed(rest, final=True), b"")
        self.assertEqual(a.feed(b"\xe4\xb8"), b"\xe4\xb8")  # 不完整 UTF-8
        a.feed(b"\x03")
        self.assertEqual(a.input, "")
        a.feed(b"\x03")
        a.feed(b"\x03")
        self.assertEqual(a.exit_code, 0)

    def test_exit_command(self):
        a = self.app()
        self.submit(a, "/exit")
        self.assertEqual(a.exit_code, 0)
        a.remove_session_json()
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "sessions", "%d.json" % a.pid)))

    def test_wrap_width(self):
        self.assertEqual(fake_tui.dwidth("中a"), 3)
        lines = fake_tui.wrap("中" * 50, 80, "● ", "  ")
        self.assertEqual([fake_tui.dwidth(x) for x in lines], [80, 24])
        self.assertTrue(lines[1].startswith("  中"))
        lines = fake_tui.wrap("Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok", 76, " " * 30, " " * 30)
        self.assertEqual([x.strip() for x in lines], ["Sonnet 5.5 · Efficient for routine tasks ·", "$2/$10 per Mtok"])



class ContextCmdTests(Base):
    """/context：屏幕内联输出 + transcript 的 ANSI stdout 与 isMeta Markdown（版式同 fixtures/context_cmd/ 真机样本）。"""

    def test_context_screen_and_transcript(self):
        from ccr import stats, tui_parse
        a = self.app()
        a.fixed_size = (120, 40)
        self.submit(a, "/context")
        self.assertIsNone(a.dialog)
        self.assertIsNone(a.turn)
        st = tui_parse.parse_screen("\n".join(self.frame(a)))
        self.assertIsNone(st["dialog"])
        self.assertTrue(st["input"]["visible"])
        self.assertEqual(st["context_usage"], {"used": 14400, "limit": 1000000, "pct": 1.4,
                                               "model": "claude-haiku-5-5", "display": "Haiku 5.5"})
        tr = self.transcript(a)
        cmd, out, md = tr[-3:]
        self.assertIn("<command-name>/context</command-name>", cmd["content"])
        self.assertTrue(out["content"].startswith("<local-command-stdout> \x1b[1mContext usage"))
        self.assertTrue(md["isMeta"])
        for c in (out["content"], md["message"]["content"]):
            self.assertEqual(tui_parse.parse_context_usage(c)["limit"], 1000000)
        # 窗口可改：200k → 统计条按 /context 的窗口
        b = self.app(CCR_FAKE_CTX_WINDOW="200000")
        self.submit(b, "/context")
        t = stats.StatsTracker()
        t.on_transcript(self.transcript(b) + [{"type": "assistant", "message": {
            "model": "claude-haiku-5-5", "usage": {"input_tokens": 1, "cache_read_input_tokens": 49999}}}])
        s = t.snapshot()
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (200000, "context_cmd", 25.0))


if __name__ == "__main__":
    unittest.main()
