"""ccr.tui_parse：对 fixtures 里每一个真实交互式 Claude Code 画面逐一断言（SPEC_V2_TUI §4.1）。"""

import json
import pathlib
import unittest

from ccr import tui_parse
from ccr.tui_parse import parse_screen

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


def _load(name):
    out = {}
    with open(FIX / name, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            out[d["name"]] = d
    return out


STREAM = _load("tui_screens_stream.ndjson")
DIALOGS = _load("tui_screens_dialogs.ndjson")
STATUS_0 = "demouser@gpu0001:~ ▱▱▱▱▱▱▱▱▱▱ 0% 0/1M $0.0000 [Haiku 5.5 · 1M · effort:low]"
AUTO = "⏵⏵ auto mode on (shift+tab to cycle)"


def S(name):
    return parse_screen(STREAM[name]["plain"])


def D(name):
    return parse_screen(DIALOGS[name]["plain"])


def labels(d):
    return [o["label"] for o in d["options"]]


class TestCoverage(unittest.TestCase):
    def test_every_fixture_screen_is_covered(self):
        """每个带 plain 的画面都在本文件里有专门的断言用例（防止新增 fixture 漏测）。"""
        covered_stream = {"trust", "trust_down", "ready", "after_stream", "perm", "perm_after_esc", "ask",
                          "ask_down", "ask_after_esc", "slash_popup", "model", "model_right", "model_down",
                          "model_after_esc", "pasted", "after_cu", "busy_count", "interrupted"}
        covered_dialogs = {"ready", "slash_popup", "model", "model_right", "model_down", "model_after_esc",
                           "effort", "effort_after_esc", "perm", "perm_after_esc", "perm_write",
                           "perm_write_after_3", "final"}
        self.assertEqual({n for n, d in STREAM.items() if "plain" in d}, covered_stream)
        self.assertEqual({n for n, d in DIALOGS.items() if "plain" in d}, covered_dialogs)
        self.assertEqual(len(STREAM["stream_frames"]["frames"]), 29)

    def test_all_screens_have_schema(self):
        frames = [d["plain"] for d in list(STREAM.values()) + list(DIALOGS.values()) if "plain" in d]
        frames += [f["s"] for f in STREAM["stream_frames"]["frames"]]
        for text in frames:
            r = parse_screen(text)
            for k in ("busy", "spinner", "mode", "status_line", "input", "live", "dialog", "spinner_info",
                      "turn_done_s", "status_stats"):
                self.assertIn(k, r)
            self.assertIsInstance(r["busy"], bool)
            self.assertIn(r["mode"], ("auto", "acceptEdits", "plan", "bypassPermissions", "manual", "dontAsk"))
            self.assertEqual(set(r["input"]), {"visible", "text"})
            if r["dialog"] is not None:
                d = r["dialog"]
                self.assertIn(d["kind"], ("permission", "question", "trust", "model", "effort", "generic"))
                for k in ("title", "lines", "options", "hints"):
                    self.assertIn(k, d)
                for i, o in enumerate(d["options"]):
                    self.assertEqual(o["index"], i)
                    self.assertEqual(set(o) - {"free_text"}, {"index", "n", "label", "desc", "selected"})
            json.dumps(r, ensure_ascii=False)

    def test_garbage_and_empty(self):
        for t in ("", "\n\n\n", "random text\nno structure", None, "─" * 80, "▔" * 80 + "\nfoo"):
            r = parse_screen(t)
            self.assertFalse(r["busy"])
            self.assertFalse(r["input"]["visible"])
        r = parse_screen("▔" * 80 + "\n  Something new\n  ❯ 1. Alpha\n    2. Beta\n\n  Enter to confirm · Esc to cancel")
        self.assertEqual(r["dialog"]["kind"], "generic")
        self.assertEqual(labels(r["dialog"]), ["Alpha", "Beta"])
        self.assertEqual(r["dialog"]["hints"], "Enter to confirm · Esc to cancel")


class TestTrust(unittest.TestCase):
    def check(self, r, sel):
        self.assertFalse(r["busy"])
        self.assertIsNone(r["spinner"])
        self.assertFalse(r["input"]["visible"])
        self.assertIsNone(r["live"])
        d = r["dialog"]
        self.assertEqual(d["kind"], "trust")
        self.assertEqual(d["path"], "/mnt/ssd/home/demouser")
        self.assertEqual(labels(d), ["No, exit", "Yes, I trust this folder"])
        self.assertEqual([o["n"] for o in d["options"]], [None, None])
        self.assertEqual([o["selected"] for o in d["options"]], [sel == 0, sel == 1])
        self.assertEqual(tui_parse.selected_index(d), sel)
        self.assertEqual(d["hints"], "Enter to confirm · Esc to cancel")
        self.assertTrue(any("home folder, so Yes lasts for this session only" in l for l in d["lines"]))

    def test_trust(self):
        self.check(S("trust"), 0)

    def test_trust_down(self):
        self.check(S("trust_down"), 1)


class TestIdleScreens(unittest.TestCase):
    def test_stream_ready(self):
        r = S("ready")
        self.assertFalse(r["busy"])
        self.assertIsNone(r["spinner"])
        self.assertIsNone(r["dialog"])
        self.assertIsNone(r["live"])
        self.assertEqual(r["mode"], "auto")
        self.assertEqual(r["mode_line"], AUTO)
        self.assertEqual(r["status_line"], STATUS_0)
        self.assertEqual(r["input"], {"visible": True, "text": ""})
        self.assertEqual(r["model_hint"]["model"], "Haiku 5.5")
        self.assertEqual(r["model_hint"]["effort"], "low")

    def test_dialogs_ready_manual_mode(self):
        r = D("ready")
        self.assertEqual(r["mode"], "manual")
        self.assertEqual(r["mode_line"], "⏸ manual mode on")
        self.assertEqual(r["status_line"], STATUS_0)
        self.assertEqual(r["input"], {"visible": True, "text": ""})
        self.assertIsNone(r["dialog"])
        self.assertFalse(r["busy"])

    def test_after_stream(self):
        r = S("after_stream")
        self.assertFalse(r["busy"])
        self.assertIsNone(r["spinner"])
        self.assertIsNone(r["dialog"])
        self.assertIsNone(r["live"])          # 回显已滚出屏幕且空闲
        self.assertIn("25k/1M", r["status_line"])
        self.assertEqual(r["mode"], "auto")

    def test_stream_perm_screen_is_idle(self):
        # 该录制中 perm 画面时序不准：实际是 auto 模式下已执行完的空闲画面
        for name in ("perm", "perm_after_esc"):
            r = S(name)
            self.assertIsNone(r["dialog"], name)
            self.assertFalse(r["busy"])
            self.assertEqual(r["echo"], "用 Bash 执行 touch /tmp/ccr_tui_probe_denied ，被拒绝就告诉我")
            self.assertEqual(r["live"], "  Ran 1 shell command\n\n● 没有被拒绝，touch /tmp/ccr_tui_probe_denied 已成功执行，命令没有输出。")
            self.assertEqual(r["turn_done"], "Baked for 8s · done 7:50 PM")

    def test_ask_after_esc_restores_input(self):
        r = S("ask_after_esc")
        self.assertIsNone(r["dialog"])
        self.assertFalse(r["busy"])
        self.assertEqual(r["input"], {"visible": True, "text": "请使用 AskUserQuestion 工具问我一个单选问题：我更喜欢红色还是蓝色"})

    def test_stream_slash_popup_absent_text_appended(self):
        r = S("slash_popup")
        self.assertEqual(r["input"]["text"], "请使用 AskUserQuestion 工具问我一个单选问题：我更喜欢红色还是蓝色/model")
        self.assertIsNone(r["popup"])
        self.assertIsNone(r["dialog"])

    def test_dialogs_slash_popup(self):
        r = D("slash_popup")
        self.assertEqual(r["input"], {"visible": True, "text": "/model"})
        self.assertIsNone(r["dialog"])
        self.assertIsNone(r["live"])
        self.assertEqual([p["cmd"] for p in r["popup"]], ["/model", "/auto-mode-setup", "/code-review", "/effort"])
        self.assertTrue(r["popup"][0]["selected"])
        self.assertEqual(r["popup"][0]["desc"], "Switch model (currently Haiku 5.5)")
        self.assertFalse(any(p["selected"] for p in r["popup"][1:]))

    def test_pasted_multiline(self):
        r = S("pasted")
        self.assertEqual(r["input"], {"visible": True, "text": "第一行\n第二行 with  spaces"})
        self.assertNotIn("ctrl+g", r["live"])
        self.assertEqual(r["live"], "● Declined to answer Claude's questions:\n  ⎿  · 你更喜欢红色还是蓝色？ (红色 / 蓝色)")
        self.assertFalse(r["busy"])

    def test_after_ctrl_u(self):
        r = S("after_cu")
        self.assertEqual(r["input"], {"visible": True, "text": "第一行"})
        self.assertNotIn("ctrl+y", r["live"])

    def test_model_after_esc_stream(self):
        r = S("model_after_esc")
        self.assertFalse(r["busy"])
        self.assertIsNone(r["dialog"])
        self.assertIn("Declined to answer Claude's questions:", r["live"])

    def test_model_after_esc_dialogs(self):
        r = D("model_after_esc")
        self.assertIsNone(r["dialog"])
        self.assertEqual(r["echo"], "/model")
        self.assertEqual(r["live"], "  ⎿  Kept model as Haiku 5.5")
        self.assertEqual(r["last_result"], "Kept model as Haiku 5.5")
        self.assertEqual(r["mode"], "manual")

    def test_effort_after_esc(self):
        r = D("effort_after_esc")
        self.assertIsNone(r["dialog"])
        self.assertEqual(r["echo"], "/effort")
        self.assertEqual(r["live"], "  ⎿  Cancelled")
        self.assertIsNone(r["last_result"])

    def test_perm_after_esc_dialogs(self):
        r = D("perm_after_esc")
        self.assertIsNone(r["dialog"])
        self.assertTrue(r["interrupted"])
        self.assertTrue(r["live"].endswith("⎿  Interrupted · What should Claude do instead?"))
        self.assertIn("Did not run 1 call", r["live"])

    def test_perm_write_after_3_and_final(self):
        for name in ("perm_write_after_3", "final"):
            r = D(name)
            self.assertIsNone(r["dialog"])
            self.assertFalse(r["busy"])
            self.assertEqual(r["echo"], "请用 Write 工具在 /mnt/ssd/home/demouser/.ccr_probe_write_denied.txt 写入\nhi，被拒绝就直接告诉我")
            self.assertIn("Rejected write to .ccr_probe_write_denied.txt", r["live"])
            self.assertEqual(r["mode"], "manual")
            self.assertEqual(r["input"], {"visible": True, "text": ""})


class TestBusy(unittest.TestCase):
    def test_ask_frames_busy_with_spinner_and_tip_removed(self):
        for name in ("ask", "ask_down"):
            r = S(name)
            self.assertTrue(r["busy"], name)
            self.assertEqual(r["spinner"], "Wandering…")
            self.assertIsNone(r["live"])   # spinner 与 Tip 行被去掉，回显之后无其它内容
            self.assertEqual(r["echo"], "请使用 AskUserQuestion 工具问我一个单选问题：我更喜欢红色还是蓝色")
            self.assertIsNone(r["dialog"])
            self.assertEqual(r["mode"], "auto")
            self.assertEqual(r["mode_line"], AUTO + " · esc to interrupt")

    def test_stream_model_frames_are_busy(self):
        r = S("model")
        self.assertTrue(r["busy"])
        self.assertEqual(r["spinner"], "Sketching…")
        r = S("model_right")
        self.assertEqual(r["spinner"], "Sketching… (3s · thinking with low effort)")
        self.assertIsNone(r["live"])
        self.assertEqual(r["echo"], "请使用 AskUserQuestion 工具问我一个单选问题：我更喜欢红色还是蓝色/model")

    def test_busy_count_without_echo(self):
        r = S("busy_count")
        self.assertTrue(r["busy"])
        self.assertIsNone(r["spinner"])
        self.assertIsNone(r["echo"])
        self.assertEqual(r["live"], "\n".join("  %d" % i for i in range(4, 21)))
        self.assertIsNone(r["dialog"])

    def test_interrupted(self):
        r = S("interrupted")
        self.assertFalse(r["busy"])
        self.assertTrue(r["interrupted"])
        self.assertIsNone(r["live"])
        self.assertIsNone(r["spinner"])
        self.assertEqual(r["input"], {"visible": True, "text": ""})

    def test_stream_frames(self):
        frames = STREAM["stream_frames"]["frames"]
        res = [parse_screen(f["s"]) for f in frames]
        # 0：发送前；1–27：忙；28：完成
        self.assertFalse(res[0]["busy"])
        self.assertTrue(all(r["busy"] for r in res[1:28]))
        self.assertFalse(res[28]["busy"])
        self.assertEqual(res[28]["turn_done"], "Baked for 9s · done 7:50 PM")
        for r in res:
            self.assertIsNone(r["dialog"])
            self.assertEqual(r["mode"], "auto")
            self.assertTrue(r["input"]["visible"])
        for i in range(1, 16):
            self.assertEqual(res[i]["spinner"], "Schlepping…", i)
            self.assertIsNone(res[i]["live"], i)
            self.assertEqual(res[i]["echo"], "写一段约 400 字的中文短文，介绍深海热泉，不要用任何工具。")
        self.assertEqual(res[16]["spinner"], "Schlepping… (4s · thinking with low effort)")
        self.assertEqual(res[19]["spinner"], "Schlepping… (5s · ↓ 88 tokens · thinking with low effort)")
        self.assertEqual(res[20]["spinner"], "Schlepping… (5s · ↓ 175 tokens · thinking with low effort)")
        self.assertEqual(res[22]["spinner"], "Schlepping… (6s · ↓ 211 tokens · thinking with low effort)")
        for i in range(23, 29):
            self.assertIsNone(res[i]["spinner"], i)
        # 逐字草稿：随帧增长，以 "● " 开头，不含 spinner
        self.assertTrue(res[19]["live"].startswith("● 深海热泉是洋中脊"))
        lens = [len(res[i]["live"] or "") for i in range(19, 26)]
        self.assertEqual(lens, sorted(lens))
        self.assertGreater(lens[-1], lens[0])
        for i in range(19, 28):
            self.assertNotIn("Schlepping", res[i]["live"])
        self.assertIn("黑烟囱", res[23]["live"])
        # 26、27：回显滚出屏幕但仍忙 → live = 整个内容区
        for i in (26, 27):
            self.assertIsNone(res[i]["echo"])
            self.assertIn("1977 年", res[i]["live"])
        self.assertIsNone(res[28]["live"])


class TestModelDialog(unittest.TestCase):
    def test_model(self):
        r = D("model")
        d = r["dialog"]
        self.assertFalse(r["busy"])
        self.assertFalse(r["input"]["visible"])
        self.assertEqual(d["kind"], "model")
        self.assertEqual(d["title"], "Select model")
        self.assertEqual(d["more_above"], 2)
        self.assertEqual(d["more_below"], 1)
        self.assertEqual([x["n"] for x in d["rows"]], [3, 4, 5])
        self.assertEqual([x["name"] for x in d["rows"]], ["Fable 5.1", "Sonnet", "Haiku"])
        self.assertEqual(d["rows"][0]["desc"], "Custom Fable model (claude-fable-5-1[1m])")
        self.assertEqual(d["rows"][1]["desc"], "Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok")
        self.assertEqual(d["rows"][2]["desc"], "Haiku 5.5 · Fastest for quick answers · $0.10/$0.50 per Mtok "
                                               "($0.50/$2.50 for prompts over 100k)")
        self.assertEqual([x["current"] for x in d["rows"]], [False, False, True])
        self.assertEqual([x["selected"] for x in d["rows"]], [False, False, True])
        self.assertEqual(tui_parse.selected_model_n(d), 5)
        self.assertEqual(d["effort"], {"level": "low", "label": "Low effort", "is_default": False})
        self.assertEqual(d["hints"], "s to use this session only · Enter to save · Esc to cancel")
        self.assertEqual(labels(d), ["Fable 5.1", "Sonnet", "Haiku"])

    def test_model_right(self):
        d = D("model_right")["dialog"]
        self.assertEqual(d["kind"], "model")
        self.assertEqual(d["effort"], {"level": "medium", "label": "Medium effort", "is_default": True})
        self.assertEqual(tui_parse.selected_model_n(d), 5)

    def test_model_down(self):
        d = D("model_down")["dialog"]
        self.assertEqual(d["kind"], "model")
        self.assertEqual(d["more_above"], 3)
        self.assertEqual(d["more_below"], 0)
        self.assertEqual([x["n"] for x in d["rows"]], [4, 5, 6])
        self.assertEqual(d["rows"][2]["name"], "Fable 5.2 (1M)")
        self.assertEqual(d["rows"][2]["desc"], "claude-fable-5-2，1M 上下文")
        self.assertTrue(d["rows"][1]["current"])
        self.assertFalse(d["rows"][1]["selected"])
        self.assertTrue(d["rows"][2]["selected"])
        self.assertEqual(tui_parse.selected_model_n(d), 6)
        self.assertEqual(d["effort"]["level"], "low")


class TestEffortDialog(unittest.TestCase):
    def test_effort(self):
        r = D("effort")
        d = r["dialog"]
        self.assertEqual(d["kind"], "effort")
        self.assertEqual(d["title"], "Effort")
        self.assertEqual(d["level"], "low")
        self.assertEqual(d["ultracode"], "off")
        self.assertEqual(d["hints"], "←/→ to adjust · s to use this session only · Enter to save · Esc to cancel")
        self.assertFalse(r["input"]["visible"])

    def test_triangle_alignment_each_level(self):
        base = DIALOGS["effort"]["plain"].split("\n")
        idx = next(i for i, l in enumerate(base) if "▲" in l)
        names = base[idx + 1]
        for lvl in ("low", "medium", "high", "xhigh", "max"):
            col = names.index(" " + lvl + " ") + 1 if lvl != "max" else names.index("max")
            col += len(lvl) // 2
            slider = list("         " + "─" * 42 + "      Ultracode  on")
            slider[col] = "▲"
            lines = list(base)
            lines[idx] = "".join(slider)
            d = parse_screen("\n".join(lines))["dialog"]
            self.assertEqual(d["kind"], "effort")
            self.assertEqual(d["level"], lvl)
            self.assertEqual(d["ultracode"], "on")


class TestPermission(unittest.TestCase):
    def test_bash(self):
        r = D("perm")
        d = r["dialog"]
        self.assertFalse(r["busy"])
        self.assertFalse(r["input"]["visible"])
        self.assertEqual(r["echo"], "用 Bash 执行 touch /tmp/ccr_tui_probe_denied2 ，如果被拒绝就直接告诉我")
        self.assertEqual(r["live"], "  Creating probe file in /tmp\n  ⎿  $ touch /tmp/ccr_tui_probe_denied2")
        self.assertEqual(d["kind"], "permission")
        self.assertEqual(d["tool"], "Bash command")
        self.assertEqual(d["title"], "Bash command")
        self.assertEqual(d["question"], "Do you want to proceed?")
        self.assertEqual(d["preview"], ["touch /tmp/ccr_tui_probe_denied2"])
        self.assertEqual(d["description"], "Create probe file in /tmp")
        self.assertTrue(d["tip"].startswith("Tip: auto mode handles these prompts"))
        self.assertEqual(labels(d), ["Yes", "Yes, and always allow access to /tmp from this project",
                                     "Yes, and switch to auto mode · auto mode handles these prompts for you", "No"])
        self.assertEqual([o["n"] for o in d["options"]], [1, 2, 3, 4])
        self.assertEqual(tui_parse.selected_index(d), 0)
        self.assertEqual(d["hints"], "Esc to cancel · Tab to amend")

    def test_write_wrapped_option_merged(self):
        r = D("perm_write")
        d = r["dialog"]
        self.assertEqual(d["kind"], "permission")
        self.assertEqual(d["tool"], "Create file")
        self.assertEqual(d["description"], ".ccr_probe_write_denied.txt")
        self.assertEqual(d["question"], "Do you want to create .ccr_probe_write_denied.txt?")
        self.assertEqual(d["preview"], [" 1 hi"])
        self.assertEqual(labels(d), ["Yes", "Yes, and switch to accept edits (auto-approve file edits and common "
                                            "file commands) for this session (shift+tab)", "No"])
        self.assertEqual([o["desc"] for o in d["options"]], ["", "", ""])
        self.assertEqual([o["n"] for o in d["options"]], [1, 2, 3])
        self.assertEqual(d["hints"], "Esc to cancel · Tab to amend")
        self.assertEqual(r["live"], "● Write(.ccr_probe_write_denied.txt)")


class TestQuestion(unittest.TestCase):
    def test_ask_dialog(self):
        # 本录制里 AskUserQuestion 对话框出现在名为 model_down 的画面中
        r = S("model_down")
        d = r["dialog"]
        self.assertFalse(r["busy"])
        self.assertFalse(r["input"]["visible"])
        self.assertEqual(d["kind"], "question")
        self.assertEqual(d["header"], "颜色偏好")
        self.assertEqual(d["title"], "颜色偏好")
        self.assertEqual(d["question"], "你更喜欢红色还是蓝色？")
        self.assertEqual(labels(d), ["红色", "蓝色", "Type something.", "Chat about this"])
        self.assertEqual([o["desc"] for o in d["options"]], ["选择红色", "选择蓝色", "", ""])
        self.assertEqual([o["n"] for o in d["options"]], [1, 2, 3, 4])
        self.assertEqual([o["free_text"] for o in d["options"]], [False, False, True, False])
        self.assertEqual(tui_parse.selected_index(d), 0)
        self.assertEqual(d["hints"], "Enter to select · ↑/↓ to navigate · Esc to cancel")
        self.assertEqual(r["echo"], "请使用 AskUserQuestion 工具问我一个单选问题：我更喜欢红色还是蓝色/model")


class TestReviewFixes(unittest.TestCase):
    """审查修复：低 6（输入框位置约束）、xhigh/Max effort 文案（真机实测）。"""

    def test_stray_rule_above_echo_is_not_an_input_box(self):
        lines = DIALOGS["perm"]["plain"].split("\n")
        i = next(k for k, l in enumerate(lines) if l.startswith("❯ 用 Bash"))
        lines.insert(i, "─" * 80)      # 用户回显正上方恰好有一条顶格整行横线
        r = parse_screen("\n".join(lines))
        self.assertFalse(r["input"]["visible"])
        self.assertEqual(r["dialog"]["kind"], "permission")
        self.assertEqual(labels(r["dialog"])[-1], "No")

    def test_input_box_must_be_at_bottom(self):
        base = DIALOGS["ready"]["plain"].rstrip("\n")
        self.assertTrue(parse_screen(base)["input"]["visible"])
        # 下横线之后只允许 ≤4 行状态栏/模式行（另可有子代理列表行）；多了或出现对话框内容 → 不是输入框
        self.assertFalse(parse_screen(base + "\n" + "\n".join("  多余行 %d" % k for k in range(4)))["input"]["visible"])
        self.assertFalse(parse_screen(base + "\n ❯ 1. Yes\n   2. No")["input"]["visible"])
        self.assertFalse(parse_screen(base + "\n Esc to cancel · Tab to amend")["input"]["visible"])
        agents = "\n".join(["  ● main", "  ◯ general-purpose  前台计算      2s · ↑ 1k tokens"] * 3)
        self.assertTrue(parse_screen(base + "\n" + agents)["input"]["visible"])

    def test_model_panel_effort_labels(self):
        top = "▔" * 80
        for line, level in (("○ Low effort ←/→ to adjust", "low"), ("◐ Medium effort (default) ←/→ to adjust", "medium"),
                            ("● High effort ←/→ to adjust", "high"), ("◉ xHigh effort ←/→ to adjust", "xhigh"),
                            ("◉ Extra high effort ←/→ to adjust", "xhigh"), ("◈ Max effort ←/→ to adjust", "max"),
                            ("Max effort ←/→ to adjust", "max"), ("◉ Turbo effort ←/→ to adjust", None)):
            scr = "\n".join(["x", top, "  Select model", "  Your pick is the default for new sessions.",
                             "  ❯ 1. Opus 5.5 ✔             Opus 5.5 · Most capable", "                              with high effort",
                             "", "  " + line,
                             "  May use excessive tokens resulting in long response times or overthinking.", "",
                             "  s to use this session only · Enter to save · Esc to cancel"])
            d = parse_screen(scr)["dialog"]
            self.assertEqual(d["kind"], "model", line)
            self.assertEqual(d["effort"]["level"], level, line)   # xHigh 不能被当成 high；识别不了 = None
            self.assertEqual(d["rows"][0]["desc"], "Opus 5.5 · Most capable with high effort")
            self.assertEqual(d["hints"], "s to use this session only · Enter to save · Esc to cancel")

    def test_effort_panel_max_real_layout(self):
        ind = " " * 49
        names = "low     medium     high     xhigh      max"
        for level, hint in (("high", "←/→ to adjust · s to use this session only · Enter to save · Esc to cancel"),
                            ("max", "←/→ to adjust · Enter to use this session only · Esc to cancel")):
            col = names.index(level) + len(level) // 2 if level != "high" else names.index(" high ") + 3
            bar = list("─" * 42)
            bar[col] = "▲"
            scr = "\n".join(["", "▔" * 140, "  Effort", ind + "Faster" + " " * 29 + "Smarter",
                             ind + "".join(bar) + "      Ultracode  off", ind + names + "      Tab to toggle"]
                            + (["  May use excessive tokens resulting in long response times or overthinking. "
                                "Use sparingly for the hardest tasks."] if level == "max" else []) + ["  " + hint])
            d = parse_screen(scr)["dialog"]
            self.assertEqual((d["kind"], d["level"], d["ultracode"], d["hints"]), ("effort", level, "off", hint))


if __name__ == "__main__":
    unittest.main()


# ============================================================================ 统计：状态栏 / spinner
def _sub_frames():
    """fixtures/subagent_sample/screen_frames.txt → [(t, text)]（真机录制，约 0.5s 一帧）。"""
    import re
    txt = (FIX / "subagent_sample" / "screen_frames.txt").read_text(encoding="utf-8")
    parts = re.split(r"^=== t=([\d.]+)\n", txt, flags=re.M)
    return [(float(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]


class TestStatusStats(unittest.TestCase):
    """状态栏 = 用户自己的 statusline：宽松解析，解析不到返回 None，绝不抛异常。"""

    def ss(self, scr):
        return scr["status_stats"]

    def test_fixture_status_lines(self):
        exp = {   # 画面 → (已用, 上限, 百分比, 花费原文)
            ("S", "ready"): (0, 1000000, 0.0, "$0.0000"),
            ("S", "after_stream"): (25000, 1000000, 3.0, "$0.0035"),
            ("S", "perm"): (26000, 1000000, 3.0, "$0.0043"),
            ("S", "ask"): (26000, 1000000, 3.0, "$0.0043"),
            ("S", "pasted"): (26000, 1000000, 3.0, "$0.0049"),
            ("S", "interrupted"): (27000, 1000000, 3.0, "$0.0054"),
            ("D", "ready"): (0, 1000000, 0.0, "$0.0000"),
            ("D", "perm_after_esc"): (25000, 1000000, 3.0, "$0.0014"),
            ("D", "final"): (26000, 1000000, 3.0, "$0.0018"),
        }
        for (src, name), (used, limit, pct, cost) in exp.items():
            r = (S if src == "S" else D)(name)["status_stats"]
            self.assertEqual((r["context_used"], r["context_limit"], r["context_pct"], r["cost_text"]),
                             (used, limit, pct, cost), name)
            self.assertAlmostEqual(r["cost_usd"], float(cost[1:]))

    def test_dialog_screens_have_no_status(self):
        for name in ("model", "model_right", "model_down", "effort", "perm", "perm_write"):
            self.assertIsNone(D(name)["status_stats"], name)
        self.assertIsNone(S("trust")["status_stats"])

    def test_every_frame_parses_without_error(self):
        frames = [d["plain"] for d in list(STREAM.values()) + list(DIALOGS.values()) if "plain" in d]
        frames += [f["s"] for f in STREAM["stream_frames"]["frames"]] + [t for _, t in _sub_frames()]
        for text in frames:
            r = parse_screen(text)
            if r["status_line"] and "$0." in r["status_line"]:
                self.assertIsNotNone(r["status_stats"], r["status_line"])
                self.assertEqual(r["status_stats"]["context_limit"], 1000000)

    def test_task_example_and_cache_not_confused(self):
        r = tui_parse.parse_status_stats(
            "  demouser@gpu0001:~ ▱▱▱▱▱▱▱▱▱▱ 3% 25k/1M $0.0037 cache r:2551/w:9564 [Haiku 5.5 · 1M · effort:low]")
        self.assertEqual(r, {"context_used": 25000, "context_limit": 1000000, "context_pct": 3.0,
                             "cost_usd": 0.0037, "cost_text": "$0.0037"})
        # 截断的状态栏（80 列录制）：上下文与花费仍在
        r = tui_parse.parse_status_stats("demouser@gpu0001:~ ▱▱▱ 3% 26k/1M $0.0043 cache r:26321/w:172 [Haiku …")
        self.assertEqual((r["context_used"], r["cost_text"]), (26000, "$0.0043"))

    def test_loose_formats(self):
        P = tui_parse.parse_status_stats
        r = P("ctx 120.5k / 200k (60%) · $1.23")
        self.assertEqual((r["context_used"], r["context_limit"], r["context_pct"], r["cost_usd"]), (120500, 200000, 60.0, 1.23))
        r = P("Opus · $12,345.67 · 120.5k/200k")   # 无百分比 → 按已用/上限算
        self.assertEqual((r["context_pct"], r["cost_usd"], r["cost_text"]), (60.2, 12345.67, "$12,345.67"))
        r = P("25000/200000 tokens 12.5%")         # 上限无单位但 ≥ 10000
        self.assertEqual((r["context_used"], r["context_limit"], r["context_pct"]), (25000, 200000, 12.5))
        r = P("5h 23% | ctx 45%")                  # 多个百分比：选 ctx 旁边那个
        self.assertEqual((r["context_pct"], r["context_used"], r["cost_usd"]), (45.0, None, None))
        r = P("ctx:45% | 5h 23% | $1.23")
        self.assertEqual((r["context_pct"], r["cost_usd"]), (45.0, 1.23))
        r = P("u@node ▱▱▱ 0% [Haiku 5.5 · 1M · effort:low]")   # 只有百分比
        self.assertEqual((r["context_pct"], r["context_used"], r["context_limit"]), (0.0, None, None))
        r = P("[Sonnet] $0.42")                     # 只有花费
        self.assertEqual((r["cost_usd"], r["context_pct"]), (0.42, None))

    def test_no_false_positives(self):
        P = tui_parse.parse_status_stats
        for t in ("[Opus 5.5] 📁 proj | 🌿 main", "~/data/2023/1000 build 12/200", "2026/11/22 7:50 PM",
                  "cache r:26321/w:172", "3/4 tasks", "US$5 x", "", "   ", None, 42, ["x"], "─" * 200):
            self.assertIsNone(P(t), t)
        self.assertIsNone(P("x" * 100000))   # 超长也不报错

    def test_has_1m_hint(self):
        H = tui_parse.has_1m_hint
        for t in ("claude-fable-5-1[1m]", "opus[1M]", "[Haiku 5.5 · 1M · effort:low]", "Fable 5.2 (1M)",
                  "Sonnet 5.5 · 1M context"):
            self.assertTrue(H(t), t)
        for t in ("claude-haiku-5-5", "1m 5s", "11M", "1.1M", "", None):
            self.assertFalse(H(t), t)


class TestSpinnerInfo(unittest.TestCase):
    def test_stream_frames(self):
        res = [parse_screen(f["s"]) for f in STREAM["stream_frames"]["frames"]]
        for i in range(1, 16):   # "Schlepping…"：还没有括号
            self.assertEqual(res[i]["spinner_info"], {"verb": "Schlepping", "elapsed_s": None, "tokens": None,
                                                      "token_dir": None, "note": None}, i)
        self.assertEqual(res[16]["spinner_info"], {"verb": "Schlepping", "elapsed_s": 4.0, "tokens": None,
                                                   "token_dir": None, "note": "thinking with low effort"})
        got = [(res[i]["spinner_info"]["elapsed_s"], res[i]["spinner_info"]["tokens"]) for i in range(19, 23)]
        self.assertEqual(got, [(5.0, 88), (5.0, 175), (5.0, 197), (6.0, 211)])
        self.assertEqual(res[22]["spinner_info"]["token_dir"], "down")
        for i in list(range(23, 29)) + [0]:
            self.assertIsNone(res[i]["spinner_info"], i)
        self.assertEqual(res[28]["turn_done_s"], 9.0)
        self.assertIsNone(res[27]["turn_done_s"])

    def test_subagent_frames_variants(self):
        seen = {}
        for _, text in _sub_frames():
            r = parse_screen(text)
            if r["spinner_info"]:
                seen[r["spinner"]] = r["spinner_info"]
        self.assertEqual(seen["Shimmying… (6s · ↓ 277 tokens · thought for 1s)"],
                         {"verb": "Shimmying", "elapsed_s": 6.0, "tokens": 277, "token_dir": "down",
                          "note": "thought for 1s"})
        self.assertEqual(seen["Cogitating… (3s · ↓ 156 tokens)"]["tokens"], 156)
        self.assertEqual(seen["Cogitating… (0s · ↓ 291 tokens · thinking with low effort)"]["elapsed_s"], 0.0)
        # 子代理列表行 "2s · ↑ 14.3k tokens" 不是 spinner；"✻ Waiting for 1 background agent…" 也不是
        self.assertFalse(any("14.3k" in k or "Waiting" in k for k in seen))
        done = [parse_screen(t)["turn_done_s"] for _, t in _sub_frames()]
        self.assertIn(17.0, done)

    def test_written_forms(self):
        P = tui_parse.parse_spinner
        r = P("Working… (1m 5s · ↑ 1.2k tokens · esc to interrupt)")
        self.assertEqual((r["elapsed_s"], r["tokens"], r["token_dir"], r["note"]), (65.0, 1200, "up", "esc to interrupt"))
        r = P("Brewing… (12s · ↓ 3,456 tokens")     # 括号被截断
        self.assertEqual((r["elapsed_s"], r["tokens"]), (12.0, 3456))
        r = P("Thinking… (2h 3m · ↓ 1.5M tokens)")
        self.assertEqual((r["elapsed_s"], r["tokens"]), (7380.0, 1500000))
        r = P("Foo… (5s · ↓ 17")                     # tokens 被截断：只拿到用时
        self.assertEqual((r["elapsed_s"], r["tokens"]), (5.0, None))
        r = P("Bar… (12.5s · 40 tokens)")           # 没有箭头
        self.assertEqual((r["elapsed_s"], r["tokens"], r["token_dir"]), (12.5, 40, None))
        for t in (None, "", "   ", 5):
            self.assertIsNone(P(t))

    def test_parse_duration(self):
        D_ = tui_parse.parse_duration
        for t, v in (("9s", 9.0), ("1m 5s", 65.0), ("1m5s", 65.0), ("2h 3m", 7380.0), ("12.5s", 12.5),
                     ("800ms", 0.8), ("0s", 0.0)):
            self.assertEqual(D_(t), v, t)
        for t in ("5 apples", "", None, "s", "12", "9s later"):
            self.assertIsNone(D_(t), t)


class TestCostTextFloatRepr(unittest.TestCase):
    def test_long_float_cost_is_rounded_for_display(self):
        from ccr import tui_parse as t
        r = t.parse_status_stats("Haiku 5.5 | ⚡ low | 📊 ctx: 4% | 💰 $0.006114425000000001 | 5h: 1M tok")
        self.assertEqual(r["cost_text"], "$0.0061")
        self.assertAlmostEqual(r["cost_usd"], 0.006114425, places=8)
        self.assertEqual(t.parse_status_stats("x 3% 25k/1M $0.0032 cache")["cost_text"], "$0.0032")


# ============================================================================ 右侧 diff 面板（CLI 2.1.331）
PANEL_DIR = FIX / "diff_panel"


def P(name):
    return (PANEL_DIR / (name + ".txt")).read_text(encoding="utf-8")


def _manual_unpanel(text, col):
    """测试自己的"去掉面板"：凡在显示列 col 有 "│" 的行都裁到它之前（与被测实现无关的朴素写法）。"""
    out = []
    for line in text.split("\n"):
        w = 0
        cut = None
        for i, ch in enumerate(line):
            if w == col and ch == "│":
                cut = i
                break
            w += 2 if __import__("unicodedata").east_asian_width(ch) in ("W", "F") else 1
        out.append(line[:cut].rstrip() if cut is not None else line)
    return "\n".join(out)


def _old_frames():
    """没有面板的全部旧 fixtures 画面（ndjson 的 plain / stream_frames、按键提示、子代理录屏）。"""
    frames = []
    for name in ("tui_screens_stream.ndjson", "tui_screens_dialogs.ndjson", "tui_screens_keys.ndjson"):
        for d in _load(name).values():
            if "plain" in d:
                frames.append(d["plain"])
            for f in d.get("frames") or ():
                frames.append(f["s"])
    frames += [t for _t, t in _sub_frames()]
    return frames


# 每个面板画面：左栏宽度（面板竖线所在显示列）
PANEL_COLS = {"done_200": 110, "permission_200": 110, "after_bash_200": 110, "streaming_200": 110,
              "spinner_200": 110, "spinner_tip_200": 110, "spinner_hooks_200": 110,
              "interrupted_w260": 170, "interrupted_w200": 110, "interrupted_w143": 79, "interrupted_w120": 70}


class TestDiffPanel(unittest.TestCase):
    """fullscreen 渲染器下右侧 diff 面板：先裁掉再解析（§4.1）。fixtures/diff_panel/ 为真机探测画面。"""

    def test_fixture_files_covered(self):
        names = {p.stem for p in PANEL_DIR.glob("*.txt")}
        self.assertEqual(names, set(PANEL_COLS) | {"interrupted_w109"})

    def test_same_as_without_panel(self):
        """有面板时各字段与"同一画面去掉面板"完全一致（side_panel 除外）。"""
        for name, col in PANEL_COLS.items():
            text = P(name)
            a = parse_screen(text)
            b = parse_screen(_manual_unpanel(text, col))
            self.assertIsNotNone(a["side_panel"], name)
            self.assertIsNone(b["side_panel"], name)
            a.pop("side_panel")
            b.pop("side_panel")
            self.assertEqual(a, b, name)
            # 裁后的行里不再有面板竖线；原来带面板的行裁后都不超过左栏宽度（行号一一对应，不增删行）
            raw = text.split("\n")
            for i, l in enumerate(tui_parse.screen_lines(text)):
                self.assertNotIn("│", l, name)
                if "│" in raw[i]:
                    self.assertLessEqual(tui_parse._dwidth(l), col, name)

    def test_side_panel_header(self):
        self.assertEqual(parse_screen(P("done_200"))["side_panel"], {"files": 3, "add": 12, "del": 1})
        self.assertEqual(parse_screen(P("permission_200"))["side_panel"], {"files": 4, "add": 13, "del": 1})
        self.assertEqual(parse_screen(P("spinner_hooks_200"))["side_panel"], {"files": 5, "add": 13, "del": 1})
        self.assertIsNone(parse_screen(P("interrupted_w109"))["side_panel"])   # <110 列：面板隐藏

    def test_permission_dialog_in_left_column(self):
        """对话框画在左栏、面板一直延伸到屏幕底部：上横线 "───│───" 裁后恢复为整条横线，对话框可识别。"""
        r = parse_screen(P("permission_200"))
        d = r["dialog"]
        self.assertFalse(r["input"]["visible"])
        self.assertFalse(r["busy"])
        self.assertEqual(d["kind"], "permission")
        self.assertEqual(d["title"], "Bash command")
        self.assertEqual(d["question"], "Do you want to proceed?")
        self.assertEqual(d["preview"], ['python3 -c "print(6*7)" > answer.txt'])
        self.assertEqual(d["description"], "Run python3 one-liner and write output to answer.txt")
        self.assertTrue(d["tip"].startswith("Tip: auto mode handles these prompts"))
        self.assertEqual(labels(d), ["Yes", "Yes, and don’t ask again for: python3 *",
                                     "Yes, and switch to auto mode · auto mode handles these prompts for you", "No"])
        self.assertEqual(tui_parse.selected_index(d), 0)
        self.assertEqual(d["hints"], "Esc to cancel · Tab to amend")
        self.assertFalse(any("│" in l for l in d["lines"]))
        self.assertEqual(r["echo"], 'Use the Bash tool to run exactly: python3 -c "print(6*7)" > answer.txt')
        self.assertEqual(r["live"], '  Running python3 one-liner and writing output to answer.txt\n'
                                    '  ⎿  $ python3 -c "print(6*7)" > answer.txt')

    def test_done_screen(self):
        r = parse_screen(P("done_200"))
        self.assertTrue(r["input"]["visible"])
        self.assertEqual(r["input"]["text"], "")
        self.assertIsNone(r["dialog"])
        self.assertEqual(r["mode"], "acceptEdits")
        self.assertTrue(r["status_line"].startswith("Haiku 5.5 | ⚡ low"))
        self.assertEqual(r["turn_done"], "Cogitated for 12s · done 2:52 PM")
        self.assertEqual(r["turn_done_s"], 12.0)
        self.assertTrue(r["echo"].startswith("Please do these edits directly without asking: in calc.py"))
        self.assertTrue(r["echo"].endswith('returning s.upper() + "!". Then reply DONE.'))
        self.assertEqual(len(r["echo"].split("\n")), 3)
        # 左栏有中文（字符下标 ≠ 显示列）：面板文字不混进来
        self.assertIn("  我没有运行代码或测试,修改都是直接编辑后的结果。", r["live"].split("\n"))
        self.assertNotIn("│", r["live"])
        self.assertNotIn("files changed", r["live"])
        self.assertEqual(r["model_hint"], {"model": "Haiku 5.5", "effort": "low", "source": "banner"})

    def test_spinner_and_busy(self):
        r = parse_screen(P("spinner_200"))
        self.assertTrue(r["busy"])
        self.assertEqual(r["spinner"], "Proofing… (4s · ↓ 236 tokens · thought for 1s)")
        self.assertEqual(r["spinner_info"]["note"], "thought for 1s")
        self.assertEqual(r["echo"], "Write a 25-line poem about git diffs into poem.txt, then reply DONE.")
        r = parse_screen(P("spinner_tip_200"))
        self.assertEqual(r["spinner"], "Proofing…")
        self.assertIsNone(r["live"])            # spinner 与 Tip 行不进实时草稿，面板 diff 也不进
        r = parse_screen(P("spinner_hooks_200"))
        self.assertEqual(r["spinner_info"]["elapsed_s"], 7.0)
        self.assertEqual(r["spinner_info"]["tokens"], 509)
        self.assertTrue(r["live"].endswith("  poem.txt 已写入 25 行关于 git diff 的诗。"))
        r = parse_screen(P("streaming_200"))
        self.assertTrue(r["busy"])
        self.assertNotIn("│", r["live"])

    def test_widths_and_interrupted(self):
        for w in ("w260", "w200", "w143", "w120", "w109"):
            r = parse_screen(P("interrupted_" + w))
            self.assertTrue(r["interrupted"], w)
            self.assertTrue(r["input"]["visible"], w)
            self.assertEqual(r["mode"], "acceptEdits", w)
            self.assertIsNone(r["dialog"], w)
            self.assertEqual(r["live"].split("\n")[-1], "  ⎿  Interrupted · What should Claude do instead?", w)
            self.assertEqual(r["echo"].replace("\n", " "),
                             'Use the Bash tool to run exactly: python3 -c "print(6*7)" > answer.txt', w)
        # <110 列时面板隐藏，输入框上方的 "Resize your terminal…" 提示框不进实时草稿
        self.assertNotIn("Resize your terminal", parse_screen(P("interrupted_w109"))["live"])

    def test_no_false_positive_on_tables(self):
        """正文里的表格/框线竖线：没有面板头、或右侧还有竖线、或不从屏幕顶端开始 → 不裁。"""
        base = P("done_200").split("\n")
        tail = base[45:]   # 输入框 + 状态栏（整行宽）
        rows = ["  │ 名称" + " " * 40 + "│ 3 files changed" + " " * 30 + "│ x │"] * 8
        rows2 = [" " * 60 + "│ /diff to close          │"] * 6
        rows3 = ["  some text" + " " * 50 + "│" + ("4 files changed +1 -1" if i == 1 else "") for i in range(8)]
        rows4 = [l.replace(" " * 40, "", 1) for l in rows3]          # 竖线在第 21 列（< 40）
        self.assertIsNotNone(tui_parse._split_side_panel(rows3)[1])  # 对照：同样的行从屏幕顶端开始就算面板
        for top in (rows, rows2, ["", "", ""] + rows3, rows4):
            text = "\n".join(top + tail)
            lines = text.split("\n")
            self.assertIs(tui_parse.strip_side_panel(lines), lines)
            self.assertIsNone(parse_screen(text)["side_panel"])

    def test_wide_char_slop(self):
        """个别行宽度算不准（emoji + 变体选择符：终端画 2 列）时仍整段裁掉。"""
        lines = P("done_200").split("\n")
        k = next(i for i, l in enumerate(lines) if l.startswith("⏺ DONE"))
        bar = lines[k].index("│")
        lines[k] = "⚠️ DONE" + " " * (110 - 7) + lines[k][bar:]   # ⚠️ 在终端里占 2 列
        out = tui_parse.screen_lines("\n".join(lines))
        self.assertEqual(out[k], "⚠️ DONE")
        self.assertFalse(any("│" in l for l in out))

    def test_old_fixtures_unchanged(self):
        """没有面板的全部旧画面：裁剪步骤是恒等变换，parse_screen 结果与不做裁剪时逐字相同。"""
        frames = _old_frames()
        self.assertGreater(len(frames), 60)
        orig = tui_parse._split_side_panel
        try:
            base = []
            for t in frames:
                lines = [l.rstrip() for l in t.replace("\r", "").replace("\u00a0", " ").split("\n")]
                self.assertIs(tui_parse.strip_side_panel(lines), lines)
                base.append(parse_screen(t))
            tui_parse._split_side_panel = lambda ls: (ls, None)
            for t, r in zip(frames, base):
                self.assertEqual(parse_screen(t), r)
                self.assertIsNone(r["side_panel"])
        finally:
            tui_parse._split_side_panel = orig


# ============================================================================ /context 的输出（CLI 2.1.331）
CTX_DIR = FIX / "context_cmd"


def C(name):
    return (CTX_DIR / name).read_text(encoding="utf-8")


def _ctx_entries(name):
    return [json.loads(l) for l in (FIX / name).read_text(encoding="utf-8").splitlines() if l.strip()]


class TestContextCmd(unittest.TestCase):
    """/context：fullscreen 下输出画在消息区（不是需要 Esc 的面板），经典渲染器下同样内联、往上滚；
    transcript 里是带 ANSI 的 <local-command-stdout> + isMeta 的 Markdown 版。fixtures/context_cmd/ 为真机画面、
    fixtures/transcript_context_*.jsonl 为同一次的 transcript（都已脱敏）。"""

    def test_fullscreen_screen_is_not_a_dialog(self):
        for name, used in (("screen_fullscreen_200.txt", 16000), ("screen_fullscreen_72.txt", 16600)):
            st = parse_screen(C(name))
            self.assertIsNone(st["dialog"], name)
            self.assertFalse(st["busy"], name)
            self.assertTrue(st["input"]["visible"], name)
            self.assertEqual(st["input"]["text"], "", name)
            self.assertEqual(st["echo"], "/context", name)
            self.assertIsNone(st["popup"], name)
            self.assertIn("Context usage", st["live"], name)          # 空闲时的 live 不会显示成草稿（只有 busy 才显示）
            self.assertEqual(st["context_usage"], {"used": used, "limit": 1000000, "pct": 2.0,
                                                   "model": "claude-haiku-5-5", "display": "Haiku 5.5"}, name)
            self.assertIsNone(st["status_stats"]["context_used"], name)

    def test_classic_screen_header_scrolled_off(self):
        st = parse_screen(C("screen_classic_120.txt"))
        self.assertIsNone(st["dialog"])
        self.assertTrue(st["input"]["visible"])
        self.assertIsNone(st["context_usage"])     # 标题与总量行已滚出屏幕：不瞎猜

    def test_transcript_outputs(self):
        from ccr import tui_parse as t
        got = []
        for name in ("transcript_context_fullscreen.jsonl", "transcript_context_classic.jsonl"):
            for e in _ctx_entries(name):
                c = e.get("content") if e["type"] == "system" else e["message"]["content"]
                if isinstance(c, str):
                    r = t.parse_context_usage(c)
                    if r:
                        got.append((name[19:23], e["type"], r["used"], r["limit"], r["model"], r["display"]))
        self.assertEqual(got, [
            ("full", "system", 16000, 1000000, "claude-haiku-5-5", "Haiku 5.5"),
            ("full", "user", 16000, 1000000, "claude-haiku-5-5", None),
            ("full", "system", 16600, 1000000, "claude-haiku-5-5", "Haiku 5.5"),
            ("full", "user", 16600, 1000000, "claude-haiku-5-5", None),
            ("clas", "system", 16000, 1000000, "claude-haiku-5-5", "Haiku 5.5"),
            ("clas", "user", 16000, 1000000, "claude-haiku-5-5", None),
        ])

    def test_parse_variants_and_garbage(self):
        from ccr import tui_parse as t
        md = "## Context Usage\n\n**Model:** claude-opus-5-5  \n**Tokens:** 46.8k / 1m (5%)\n"
        self.assertEqual(t.parse_context_usage(md), {"used": 46800, "limit": 1000000, "pct": 5.0,
                                                     "model": "claude-opus-5-5", "display": None})
        txt = ("Context usage\n⛁ ⛁ ⛶ ⛶   Sonnet 4.6\n⛶ ⛶ ⛶ ⛶   claude-sonnet-4-6\n⛶ ⛶ ⛶ ⛶   150.2k/200k tokens (75%)\n")
        self.assertEqual(t.parse_context_usage(txt)["limit"], 200000)
        self.assertEqual(t.parse_context_usage(txt)["used"], 150200)
        # 只有模型 id（未知模型没有显示名）
        r = t.parse_context_usage("Context usage\n⛁ ⛶   my-gateway-model\n⛶ ⛶   12k/1m tokens (1%)")
        self.assertEqual((r["model"], r["display"]), ("my-gateway-model", None))
        # 多份时取最后一份
        self.assertEqual(t.parse_context_usage(txt + "\n" + md)["model"], "claude-opus-5-5")
        for bad in (None, 5, "", "Context usage", "context usage\nno numbers here", "16k/1m tokens (2%)",
                    "Context usage\n" + "\n" * 30 + "16k/1m tokens (2%)", "Context usage\n0/0 tokens (0%)",
                    "\x1b[1mContext usage\x1b[22m\n\x1b[38;5;241m⛶ ⛶"):
            self.assertIsNone(t.parse_context_usage(bad), repr(bad))
        self.assertEqual(t.strip_ansi("\x1b[1mA\x1b[22m\x1b[38;5;245mB\x1b[39m\x1b]0;t\x07C"), "ABC")

    def test_old_fixtures_have_no_context_usage(self):
        for text in _old_frames():
            self.assertIsNone(parse_screen(text)["context_usage"])
