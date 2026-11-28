"""审批时附带说明（权限框 Tab to amend）与停止子代理（底部列表 x / Ctrl+X Ctrl+K）：SPEC §4.1、§4.2、§5。

画面事实都来自 CLI 2.1.332 真机（haiku，tmux 200x50，2026-11-28，fixtures/cli_2_1_332/amend_*.txt、agents2_*.txt）：
- 权限框：第一项 Yes / 最后一项 No 上按 Tab → "Yes, <说明>"（空的时候是灰色占位文字 "and tell Claude what to do next"），
  提示行 "Esc to cancel · Tab to amend" → "Esc to cancel"；再按 Tab 收起（文字保留）；Ctrl+U 删一个显示行；Esc = 取消整个对话框。
- 子代理列表：输入框里 ↓ →（有后台 shell 时先到任务胶囊 "· enter to view tasks"）→ 列表（main 选中）；x 立即停止（没有确认），
  行变成 "stopped · …"、提示 "x to clear"；Esc 回到输入框；Ctrl+X Ctrl+K → "Press ctrl+x ctrl+k again …" → 再按一次停止全部。
"""
import glob
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

from ccr import fake_tui, node_agent, server as S_srv, session as S, subagents, tui_parse, tuiact
import tests.test_session as TS
from tests.test_fake_tui import Base as FakeBase
from tests.test_server import Base as ServerBase
from tests.test_session import Base as SessionBase, SimTUI, RULE, wait_until

FX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "cli_2_1_332"
HAVE_TMUX = bool(shutil.which("tmux"))
TMUX = shutil.which("tmux") or "tmux"


def fx(name):
    return (FX / (name + ".txt")).read_text("utf-8")


def parse(name):
    return tui_parse.parse_screen(fx(name))


# ============================================================================ 解析（真机画面）
class TestAmendParse(unittest.TestCase):
    def test_ready(self):
        for name, n in (("amend_perm_ready", 4), ("amend_write_ready", 3)):
            d = parse(name)["dialog"]
            self.assertEqual(d["kind"], "permission")
            self.assertEqual(d["hints"], "Esc to cancel · Tab to amend")
            self.assertEqual(d["amendable"], [0, n - 1], name)
            self.assertIsNone(d["amend"])

    def test_editing_states(self):
        cases = {
            "amend_yes_empty": (0, "Yes", "", True),
            "amend_yes_typed": (0, "Yes", "请在回复开头先写「收到说明A」", False),
            "amend_toggled_back": (0, "Yes", "AAA", False),
            "amend_no_empty": (3, "No", "", True),
            "amend_no_typed": (3, "No", "先不要执行，直接回复「收到说明B」", False),
            "amend_write_no_empty": (2, "No", "", True),
        }
        for name, (i, base, text, ph) in cases.items():
            d = parse(name)["dialog"]
            self.assertEqual(d["hints"], "Esc to cancel", name)
            self.assertEqual(d["amend"], {"index": i, "base": base, "text": text, "placeholder": ph}, name)
            self.assertNotIn(i, d["amendable"], name)

    def test_long_wrapped(self):
        a = parse("amend_yes_long")["dialog"]["amend"]
        self.assertTrue(a["text"].startswith("执行完后在回复开头写「收到说明C」 另外"))
        self.assertTrue(a["text"].endswith("lazy dog END"))   # 折行的续行并进说明
        a2 = parse("amend_yes_long_cu")["dialog"]["amend"]   # Ctrl+U 只删了光标所在的那一个显示行
        self.assertTrue(a2["text"].endswith("0123456789 the") and "END" not in a2["text"])

    def test_not_editing(self):
        for name in ("amend_moved_away", "amend_opt3_hint", "amend_write_opt2", "amend_toggled_off"):
            d = parse(name)["dialog"]
            self.assertIsNone(d["amend"], name)
        d = parse("amend_moved_away")["dialog"]   # 第一项留着说明 "Yes, BBB"：不再算可附带说明的项
        self.assertEqual(d["amendable"], [3])
        self.assertEqual(d["options"][0]["label"], "Yes, BBB")

    def test_after(self):
        st = parse("amend_yes_done")
        self.assertIsNone(st["dialog"])
        self.assertEqual(st["echo"], "执行完后在回复开头写「收到说明C」")   # Yes + 说明：屏幕回显成一条用户消息
        st = parse("amend_esc_cancelled")                                    # 输入说明时 Esc = 取消整个对话框
        self.assertTrue(st["interrupted"] or "Interrupted" in fx("amend_esc_cancelled"))

    def test_other_dialogs_have_no_amend(self):
        for line in (FX.parent / "tui_screens_dialogs.ndjson").read_text("utf-8").splitlines():
            d = json.loads(line)
            st = tui_parse.parse_screen(d.get("plain") or "")
            dd = st["dialog"]
            if dd and dd["kind"] != "permission":
                self.assertNotIn("amend", dd)
            elif dd:
                self.assertIsNone(dd["amend"], d["name"])

    def test_transcript_shapes(self):
        """只读 transcript：Yes + 说明 = 工具结果后多一个文字块；No + 说明 = 拒绝文字里带 "the user said:"。"""
        es = [json.loads(x) for x in (FX / "amend_transcript.jsonl").read_text("utf-8").splitlines()]
        yes = [e for e in es if e["type"] == "user" and isinstance(e["message"]["content"], list)
               and any(b.get("type") == "text" and "收到说明C" in b.get("text", "") for b in e["message"]["content"])]
        self.assertEqual(len(yes), 1)
        self.assertEqual([b["type"] for b in yes[0]["message"]["content"]], ["tool_result", "text"])
        no = [e for e in es if e["type"] == "user" and isinstance(e["message"]["content"], list)
              and "the user said:\n先不要执行" in str(e["message"]["content"][0].get("content"))]
        self.assertEqual(len(no), 1)
        self.assertTrue(no[0]["message"]["content"][0]["is_error"])


class TestReplayFixture(unittest.TestCase):
    def test_states_match_parser(self):
        """回放页第 14j 节用的 fixtures/tui_screens_tuiact.ndjson：state 必须就是当前解析器对 plain 的结果（解析器改了就重新生成：
        对每个画面 json.dumps({"name", "plain", "state": tui_parse.parse_screen(plain)})）；transcript 与 amend_transcript.jsonl 相同。"""
        lines = (FX.parent / "tui_screens_tuiact.ndjson").read_text("utf-8").splitlines()
        names = []
        for line in lines:
            d = json.loads(line)
            names.append(d["name"])
            if "plain" in d:
                self.assertEqual(d["plain"], fx(d["name"]), d["name"])
                self.assertEqual(d["state"], json.loads(json.dumps(tui_parse.parse_screen(d["plain"]))), d["name"])
            else:
                want = [json.loads(x) for x in (FX / "amend_transcript.jsonl").read_text("utf-8").splitlines()]
                self.assertEqual(d["entries"], want)
        self.assertIn("amend_transcript", names)
        self.assertIn("amend_perm_ready", names)


class TestFooterParse(unittest.TestCase):
    def test_tasks_pill(self):
        st = parse("agents2_tasks_pill")
        self.assertTrue(st["tasks_focus"])
        self.assertFalse(st["agents_focus"])
        self.assertEqual(st["mode"], "auto")
        self.assertFalse(parse("agents2_shell_esc")["tasks_focus"])
        st = parse("agents2_pill_down_main")
        self.assertTrue(st["agents_focus"] and not st["tasks_focus"])

    def test_rows(self):
        st = parse("agents2_waiting_shell_row")
        self.assertEqual(st["agents_panel"][1]["desc"], "waiting on 1 shell · 丙数数 · since 9:20")
        self.assertIsNone(st["agents_panel"][1]["state"])
        st = parse("agents2_x_stopped")
        self.assertEqual(st["agents_hint"], "enter to view · x to clear")
        self.assertEqual(st["agents_panel"][1]["state"], "stopped")
        st = parse("agents2_cleared")
        self.assertEqual(len(st["agents_panel"]), 2)
        self.assertTrue(st["agents_panel"][0]["selected"])

    def test_text_down_keeps_input(self):
        st = parse("agents2_text_down")   # 输入框有文字时 ↓ 也进列表，文字原样保留
        self.assertTrue(st["agents_focus"])
        self.assertEqual(st["input"]["text"], "abc")

    def test_kill_hints(self):
        self.assertTrue(tuiact.kill_hint_visible(fx("agents2_killall_hint")))
        self.assertTrue(tuiact.kill_hint_visible(fx("agents2_killall_hint2")))
        self.assertFalse(tuiact.kill_hint_visible(fx("agents2_running")))
        self.assertTrue(tuiact.no_bg_visible(fx("agents2_no_bg")))
        self.assertFalse(tuiact.no_bg_visible(fx("agents2_running")))

    def test_node_agent_footer_consistent(self):
        """node_agent.footer_agents 与 tui_parse 对全部子代理画面逐帧一致（guard 一致性）。"""
        files = sorted(glob.glob(str(FX / "agents*.txt"))) + [str(FX.parent / "subagent_sample" / "screen_frames.txt")]
        for f in files:
            text = pathlib.Path(f).read_text("utf-8")
            st = tui_parse.parse_screen(text)
            lines = node_agent.screen_lines(text)
            box = node_agent.find_input_box(lines)
            self.assertIsNotNone(box, f)
            hint, tasks, rows = node_agent.footer_agents(lines, box)
            self.assertEqual(hint, st["agents_hint"], f)
            self.assertEqual(tasks, st["tasks_focus"], f)
            want = [(a["selected"], a["name"], a["state"] == "stopped") for a in st["agents_panel"] or []] \
                if st["agents_focus"] else []
            self.assertEqual(rows, want, f)


# ============================================================================ node_agent guard / 按键策略
class TestGuards(unittest.TestCase):
    def test_agents_sel(self):
        g = {"agents_sel": {"pos": 1, "count": 3, "name": "general-purpose"}}
        node_agent.validate_guard(g)
        self.assertIsNone(node_agent.check_guard(fx("agents2_row1"), g))
        self.assertIsNone(node_agent.check_guard(fx("agents2_summary_desc"), g))   # 描述换成摘要也认
        self.assertIsNone(node_agent.check_guard(fx("agents2_waiting_shell_row"), g))
        self.assertIn("选中的不是", node_agent.check_guard(fx("agents2_row1"), {"agents_sel": dict(g["agents_sel"], pos=2)}))
        self.assertIn("行数", node_agent.check_guard(fx("agents2_row1"), {"agents_sel": dict(g["agents_sel"], count=4)}))
        self.assertIn("类型", node_agent.check_guard(fx("agents2_row1"), {"agents_sel": dict(g["agents_sel"], name="Explore")}))
        self.assertIn("x to stop", node_agent.check_guard(fx("agents2_x_stopped"), g))   # 已停止：提示是 x to clear
        self.assertIn("x to stop", node_agent.check_guard(fx("agents2_running"), g))     # 焦点在输入框：x 会变成一个字
        self.assertIn("x to stop", node_agent.check_guard(fx("agents2_pill_down_main"), g))
        self.assertIn("输入框不可见", node_agent.check_guard(fx("amend_perm_ready"), g))
        for bad in ({"pos": 0, "count": 3, "name": "a"}, {"pos": 3, "count": 3, "name": "a"}, {"pos": 1, "count": 3},
                    {"pos": True, "count": 3, "name": "a"}, {"pos": 1, "count": 3, "name": ""},
                    {"pos": 1, "count": 3, "name": "a\nb"}, {"pos": 1, "count": 99, "name": "a"}, [1, 3]):
            with self.assertRaises(node_agent.OpError, msg=bad):
                node_agent.validate_guard({"agents_sel": bad})

    def test_footer_focus_and_input_empty(self):
        g = {"footer_focus": True}
        for name in ("agents2_row1", "agents2_tasks_pill", "agents2_pill_down_main", "agents2_x_stopped", "agents2_cleared"):
            self.assertIsNone(node_agent.check_guard(fx(name), g), name)
        for name in ("agents2_running", "agents2_after_esc", "agents2_shell_esc"):
            self.assertIn("焦点不在", node_agent.check_guard(fx(name), g), name)
        e = {"input_box": True, "input_empty": True}
        self.assertIsNone(node_agent.check_guard(fx("agents2_running"), e))
        self.assertIn("有文字", node_agent.check_guard(fx("agents2_esc_back"), e))
        self.assertIn("输入框不可见", node_agent.check_guard(fx("amend_perm_ready"), e))
        for bad in ("x", 1, None):
            with self.assertRaises(node_agent.OpError):
                node_agent.validate_guard({"input_empty": bad})
            with self.assertRaises(node_agent.OpError):
                node_agent.validate_guard({"footer_focus": bad})

    def test_kill_hint_guard(self):
        g = {"input_box": True, "input_empty": True, "hint": "kill_agents"}
        node_agent.validate_guard(g)
        self.assertIsNone(node_agent.check_guard(fx("agents2_killall_hint"), g))
        self.assertIn("没有对应的提示", node_agent.check_guard(fx("agents2_running"), g))
        self.assertTrue(node_agent.GUARD_HINTS["kill_agents"].pattern == tuiact.KILL_HINT_RE.pattern)

    def test_key_policy(self):
        pol = node_agent.check_key_policy
        pol(["x"], {"agents_sel": {"pos": 1, "count": 2, "name": "a"}})
        pol(["C-x", "C-k"], {"input_box": True, "input_empty": True})
        for keys, g in ((["x"], None), (["x"], {"footer_focus": True}), (["x", "Enter"], {"agents_sel": {}}),
                        (["Down", "x"], {"agents_sel": {"pos": 1, "count": 2, "name": "a"}}),
                        (["C-k"], {"input_empty": True}), (["C-x", "C-k"], None), (["C-x", "C-k"], {"input_box": True}),
                        (["C-x", "C-k", "C-x", "C-k"], {"input_empty": True})):
            with self.assertRaises(node_agent.OpError, msg=(keys, g)):
                pol(keys, g)
        pol(["Enter"], None)   # 其它键不受影响
        with self.assertRaises(node_agent.OpError):   # op_keys 在抓屏 / 发键之前就拒绝
            node_agent.op_keys({"sid": "s0123456789ab", "keys": ["x"]})

    def test_amend_tail_guard_consistent(self):
        """session 算的对话框区域 sha1（tail guard）在同一帧上一定通过 node_agent 复核、换一帧就不通过。"""
        names = ["amend_perm_ready", "amend_yes_empty", "amend_yes_typed", "amend_no_empty", "amend_no_typed",
                 "amend_moved_away", "amend_toggled_back", "amend_yes_long", "amend_write_ready"]
        for i, name in enumerate(names):
            g = tuiact.tail_guard(fx(name))
            self.assertIsNotNone(g, name)
            self.assertEqual(g, S._dialog_tail_guard(fx(name)))
            self.assertIsNone(node_agent.check_guard(fx(name), g), name)
            other = names[(i + 1) % len(names)]
            self.assertIsNotNone(node_agent.check_guard(fx(other), g), (name, other))

    def test_whitelists(self):
        self.assertTrue({"x", "C-k"} <= node_agent.KEYS)
        self.assertNotIn("x", S.KEYS_WHITELIST)
        self.assertNotIn("C-k", S_srv.KEYS_WHITELIST)


# ============================================================================ 纯函数：对位
def _row(name="general-purpose", desc="", state=None, sel=False):
    return {"name": name, "desc": desc, "info": "3s", "state": state, "current": False, "selected": sel}


MAIN = {"name": "main", "desc": "", "info": "", "state": None, "current": True, "selected": False}


def _agent(aid, desc, typ="general-purpose"):
    return {"agent_id": aid, "description": desc, "agent_type": typ, "status": "running"}


class TestMatch(unittest.TestCase):
    def test_positional(self):
        run = [_agent("a1", "甲数数"), _agent("a2", "乙数数")]
        rows = [MAIN, _row(desc="Running first 20-second sleep step"), _row(desc="Running first 20-second sleep command")]
        self.assertEqual(tuiact.match_agent_row(rows, run, "a1"), 1)
        self.assertEqual(tuiact.match_agent_row(rows, run, "a2"), 2)
        rows2 = [MAIN, _row(desc="甲数数", state="stopped"), _row(desc="waiting on 1 shell · 乙数数 · since 9:20")]
        self.assertEqual(tuiact.match_agent_row(rows2, [_agent("a2", "乙数数")], "a2"), 2)   # 停止的行不参与对位

    def test_refuse(self):
        run = [_agent("a1", "甲数数"), _agent("a2", "乙数数")]
        M = tuiact.match_agent_row
        self.assertIsNone(M([MAIN, _row(desc="甲数数")], run, "a1"))                          # 行数对不上
        self.assertIsNone(M([MAIN, _row(desc="x"), _row(name="Explore", desc="y")], run, "a1"))   # 类型名对不上
        self.assertIsNone(M([MAIN, _row(desc="乙数数"), _row(desc="甲数数")], run, "a1"))     # 认得出的描述与位置矛盾
        self.assertIsNone(M([_row(desc="甲"), _row(desc="乙")], run, "a1"))                    # 第 0 行不是 main
        self.assertIsNone(M(None, run, "a1"))
        self.assertIsNone(M([MAIN, _row(), _row()], run, "zz"))
        dup = [_agent("a1", "同名"), _agent("a2", "同名")]
        self.assertEqual(M([MAIN, _row(desc="同名"), _row(desc="同名")], dup, "a2"), 2)       # 重名描述不作为矛盾证据

    def test_fixture_rows(self):
        st = parse("agents2_summary_desc")
        run = [_agent("a1", "甲数数"), _agent("a2", "乙数数")]
        self.assertEqual(tuiact.match_agent_row(tuiact.panel_rows(st), run, "a2"), 2)
        st = parse("agents2_waiting_shell_row")
        run = [_agent("a3", "丙数数"), _agent("a4", "丁数数")]
        self.assertEqual(tuiact.match_agent_row(tuiact.panel_rows(st), run, "a3"), 1)
        self.assertIsNone(tuiact.match_agent_row(tuiact.panel_rows(st), run[::-1], "a3"))   # 顺序反了：描述矛盾

    def test_amend_text(self):
        V = tuiact.validate_amend_text
        self.assertEqual(V("  改用 echo  "), "改用 echo")
        for bad in ("", "   ", "a\nb", "a\tb", "x" * 2001, 5, None, "and tell Claude what to do next"):
            with self.assertRaises(ValueError, msg=bad):
                V(bad)


# ============================================================================ SessionManager 流程（模拟终端，按真机语义）
PH = {"Yes": tui_parse.AMEND_PLACEHOLDERS["Yes"], "No": tui_parse.AMEND_PLACEHOLDERS["No"]}
LABS = ["Yes", "Yes, and always allow access to /tmp from this project",
        "Yes, and switch to auto mode · auto mode handles these prompts for you", "No"]


class ActSim(SimTUI):
    """SimTUI + 权限框的附带说明 + 底部子代理列表 / 任务胶囊 + Ctrl+X Ctrl+K（语义同真机 2.1.332）。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.amends = {}
        self.amend_on = False
        self.notes = []          # Enter 时 (选中项, 说明)
        self.no_tab = False      # 测试钩子：Tab 没反应
        self.paste_drop = 0      # 测试钩子：粘贴时丢掉最后 n 个字
        self.rows = []           # [{"type", "text", "stopped"}]
        self.footer = None
        self.fsel = 0
        self.shells = 0
        self.cx = False
        self.armed = False
        self.ignore_chord = 0    # 测试钩子：前 n 次 Ctrl+X Ctrl+K 没反应

    def _label(self, i):
        base = LABS[i]
        if i in (0, 3):
            if i == self.sel and self.amend_on:
                return base + ", " + (self.amends.get(i) or PH[base])
            if i != self.sel and self.amends.get(i):
                return base + ", " + self.amends[i]
        return base

    def render(self):
        with self.lock:
            if self.view == "perm":
                lines = self._body(8) + [RULE, " Bash command", " Create probe file", "╌" * 80, " " + self.perm_cmd,
                                         "╌" * 80, " Do you want to proceed?"]
                for i in range(4):
                    lines.append((" ❯ " if self.sel == i else "   ") + "%d. %s" % (i + 1, self._label(i)))
                tab = self.sel in (0, 3) and not self.amend_on
                lines += ["", " Esc to cancel · Tab to amend" if tab else " Esc to cancel"]
                return "\n".join(lines)
            if self.view != "idle" or not (self.rows or self.shells or self.armed):
                return super().render()
            body = self._body(12)
            sep = [" " * 60 + "Press ctrl+x ctrl+k again to stop background agents"] if self.armed else []
            foot = self._footer()
            if self.footer == "agents":
                if self.fsel == 0:
                    foot[-1] = "  ↑/↓ to select"
                else:
                    foot[-1] = "  enter to view · x to clear" if self.rows[self.fsel - 1]["stopped"] else \
                        "  enter to view · x to stop · ctrl+x ctrl+k to stop all agents"
            elif self.footer == "tasks":
                foot[-1] = "  ⏵⏵ auto mode on · %d shell · enter to view tasks" % self.shells
            else:
                foot[-1] += (" · %d shell" % self.shells if self.shells else "") + " · ↓ to manage"
            ag = []
            if self.rows:
                ag = ["", ("❯ " if self.footer == "agents" and self.fsel == 0 else "  ") + "⏺ main"]
                for k, r in enumerate(self.rows, 1):
                    mark = "❯ " if self.footer == "agents" and self.fsel == k else "  "
                    ag.append(mark + "◯ %s  %s" % (r["type"], r["text"]) + " " * 8 +
                              ("stopped · " if r["stopped"] else "") + "12s · ↓ 11.2k tokens")
            return "\n".join(body + sep + foot + ag)

    def paste(self, text, enter):
        with self.lock:
            if self.view == "perm":
                if self.amend_on:
                    t = text[:len(text) - self.paste_drop] if self.paste_drop else text
                    self.amends[self.sel] = self.amends.get(self.sel, "") + t
                return
        super().paste(text, enter)

    def key(self, k):
        with self.lock:
            if self.view == "perm":
                if k == "Tab":
                    if self.sel in (0, 3) and not self.no_tab:
                        self.amend_on = not self.amend_on
                    return
                if self.amend_on and k == "C-u":
                    self.amends[self.sel] = ""
                    return
                if k in ("Up", "Down"):
                    if not self.amends.get(self.sel):
                        self.amends.pop(self.sel, None)
                    self.sel = max(0, min(3, self.sel + (1 if k == "Down" else -1)))
                    self.amend_on = bool(self.amends.get(self.sel))
                    return
                if k == "Enter":
                    self.notes.append((self.sel, self.amends.get(self.sel) if self.amend_on else None))
                    self.amends, self.amend_on = {}, False
            if self.view == "idle" and (self.rows or self.shells):
                if k == "C-x":
                    self.cx = True
                    return
                if k == "C-k":
                    if self.cx and self.ignore_chord:
                        self.ignore_chord -= 1
                    elif self.cx and self.armed:
                        self.armed = False
                        for r in self.rows:
                            r["stopped"] = True
                    elif self.cx:
                        self.armed = True
                    self.cx = False
                    return
                if self.footer is not None:
                    if k == "Escape":
                        self.footer = None
                    elif k == "Down":
                        if self.footer == "tasks":
                            self.footer, self.fsel = ("agents", 0) if self.rows else ("tasks", 0)
                        else:
                            self.fsel = min(len(self.rows), self.fsel + 1)
                    elif k == "Up":
                        self.fsel = max(0, self.fsel - 1)
                    elif k == "x" and self.footer == "agents" and self.fsel:
                        r = self.rows[self.fsel - 1]
                        if r["stopped"]:
                            self.rows.pop(self.fsel - 1)
                            self.fsel = 0
                        else:
                            r["stopped"] = True
                    return
                if k == "Down":
                    self.footer, self.fsel = ("tasks", 0) if self.shells else ("agents", 0)
                    return
            if k == "x":   # 焦点在输入框：x 就是一个字
                self.input += "x"
                return
        super().key(k)


class GuardLink(TS.StubLink):
    """StubLink + node_agent 的按键策略（x / C-k）与 paste 的 guard 复核。"""

    def request(self, op, timeout=30, **p):
        rem = self.rem
        if self.state == "ready" and op in ("keys", "paste"):
            t = rem.tuis.get(p.get("sid"))
            g = p.get("guard")
            try:
                if op == "keys":
                    node_agent.check_key_policy(p["keys"], g)
                if g is not None:
                    node_agent.validate_guard(g)
            except node_agent.OpError as e:
                raise type("AgentError", (Exception,), {})(str(e))
            if t is not None and (op == "paste" and g is not None or op == "keys" and set(p["keys"]) & {"x", "C-k"}):
                with rem.lock:
                    rem.log.append((op, dict(p)))
                    if rem.before_guard is not None:
                        rem.before_guard(t, p)
                    why = node_agent.check_guard(t.render(), g) if g is not None else None
                    if why:
                        e = type("AgentError", (Exception,), {})("未发送：" + why)
                        e.code = "guard"
                        self._emit_all()
                        raise e
                    if op == "paste":
                        t.paste(p["text"], p["enter"])
                    else:
                        for k in p["keys"]:
                            t.key(k)
                    self._emit_all()
                    return {}
        return super().request(op, timeout, **p)


def _launch_entries(specs):
    """主 transcript：Agent 调用 + 启动结果（agentId），按顺序。"""
    out = []
    for i, (aid, desc) in enumerate(specs):
        tid = "toolu_t%d" % i
        out.append({"type": "assistant", "uuid": "ua%d" % i, "timestamp": "2026-11-28T01:17:0%d.000Z" % i,
                    "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Agent", "input": {
                        "description": desc, "prompt": "数数", "subagent_type": "general-purpose",
                        "run_in_background": True}}]}})
        out.append({"type": "user", "uuid": "uu%d" % i, "timestamp": "2026-11-28T01:17:0%d.500Z" % i,
                    "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": aid, "description": desc},
                    "message": {"role": "user", "content": [{"tool_use_id": tid, "type": "tool_result",
                                                             "content": [{"type": "text", "text": "agentId: " + aid}]}]}})
    return out


class TestSessionFlows(SessionBase):
    def setUp(self):
        super().setUp()
        self._orig = TS.SimTUI
        TS.SimTUI = ActSim
        self.addCleanup(setattr, TS, "SimTUI", self._orig)

    def mgr(self, auto_ready=True):
        def factory(cfg, jobid, on_event, on_state):
            return GuardLink(self.rem, cfg, jobid, on_event, on_state, auto_ready=auto_ready)
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp), link_factory=factory)
        for k, v in (("step_timeout", 1.0), ("panel_timeout", 2.0), ("result_timeout", 2.0), ("exit_timeout", 2.0),
                     ("poll_interval", 0.01), ("link_timeout", 3.0), ("connect_grace", 0.5), ("screen_interval", 0.05)):
            setattr(m, k, v)
        self.mgrs.append(m)
        self.addCleanup(self._shut, m)
        return m

    def keyops(self, sid):
        return [p for op, p in self.rem.log if op == "keys" and p["sid"] == sid]

    def perm(self, m, sid):
        t = self.tui(sid)
        t.view, t.sel = "perm", 0
        st = m._fresh(m._get(sid))
        return t, S.dialog_fingerprint(st["dialog"])

    # -------------------------------------------------------------- 附带说明
    def test_amend_yes(self):
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)
        n0 = len(self.keyops(sid))
        r = m.amend_choose(sid, 0, "  先确认目录再执行  ", fp)
        self.assertTrue(r["ok"])
        self.assertEqual(t.notes, [(0, "先确认目录再执行")])
        ks = self.keyops(sid)[n0:]
        self.assertEqual([k["keys"] for k in ks], [["Tab"], ["Enter"]])
        self.assertTrue(all(set(k["guard"]) == {"tail"} for k in ks))
        pastes = [p for op, p in self.rem.log if op == "paste" and p["sid"] == sid]
        self.assertEqual(pastes[-1]["text"], "先确认目录再执行")
        self.assertEqual(set(pastes[-1]["guard"]), {"tail"})

    def test_amend_no(self):
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)
        n0 = len(self.keyops(sid))
        m.amend_choose(sid, 3, "不要动 /tmp，改在项目目录里建", fp)
        self.assertEqual(t.notes, [(3, "不要动 /tmp，改在项目目录里建")])
        self.assertEqual([k["keys"] for k in self.keyops(sid)[n0:]], [["Down"]] * 3 + [["Tab"], ["Enter"]])

    def test_amend_clears_leftover(self):
        """上次 Tab 收起时留下的说明：再次 Tab 会恢复，先用 Ctrl+U 清掉（每步带 guard）。"""
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)
        t.amends[0] = "旧的说明"
        n0 = len(self.keyops(sid))
        m.amend_choose(sid, 0, "新的说明", fp)
        self.assertEqual(t.notes, [(0, "新的说明")])
        self.assertEqual([k["keys"] for k in self.keyops(sid)[n0:]], [["Tab"], ["C-u"], ["Enter"]])

    def test_amend_refusals_press_nothing(self):
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)
        n0 = len(self.keyops(sid))
        for idx, exp, code in ((1, fp, "amend_unsupported"), (2, fp, "amend_unsupported"),
                               (0, dict(fp, preview=["rm -rf /"]), "dialog_changed")):
            with self.assertRaises(S.OpError) as cm:
                m.amend_choose(sid, idx, "说明", exp)
            self.assertEqual(cm.exception.api_code, code)
        for bad in ("a\nb", "", "x" * 3000):
            with self.assertRaises(ValueError):
                m.amend_choose(sid, 0, bad, fp)
        t.amend_on = True   # 终端里已经在输入说明
        with self.assertRaises(S.OpError) as cm:
            m.amend_choose(sid, 0, "说明", fp)
        self.assertIn(cm.exception.api_code, ("amend_busy", "dialog_changed"))
        t.amend_on, t.view = False, "idle"
        with self.assertRaises(S.OpError) as cm:
            m.amend_choose(sid, 0, "说明", fp)
        self.assertEqual(cm.exception.api_code, "no_dialog")
        self.assertEqual(self.keyops(sid)[n0:], [])
        self.assertEqual(t.notes, [])

    def test_amend_tab_no_effect(self):
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)
        t.no_tab = True
        n0 = len(self.keyops(sid))
        with self.assertRaises(S.OpError) as cm:
            m.amend_choose(sid, 0, "说明", fp)
        self.assertEqual(cm.exception.api_code, "amend_no_input")
        self.assertEqual([k["keys"] for k in self.keyops(sid)[n0:]], [["Tab"]])   # 停下，不再按任何键
        self.assertEqual(t.notes, [])
        self.assertNotIn("Escape", sum((k["keys"] for k in self.keyops(sid)), []))

    def test_amend_paste_incomplete(self):
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)
        t.paste_drop = 2
        with self.assertRaises(S.OpError) as cm:
            m.amend_choose(sid, 0, "先确认目录再执行", fp)
        self.assertEqual(cm.exception.api_code, "amend_unconfirmed")
        self.assertEqual(t.notes, [])   # 没有按 Enter
        self.assertNotIn(["Enter"], [k["keys"] for k in self.keyops(sid)])

    def test_amend_dialog_swapped_before_enter(self):
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)

        def swap(tt, p):
            if p.get("keys") == ["Enter"]:
                tt.perm_cmd = "rm -rf /tmp/other"
        self.rem.before_guard = swap
        with self.assertRaises(S.OpError) as cm:
            m.amend_choose(sid, 0, "说明", fp)
        self.assertEqual(cm.exception.api_code, "dialog_changed")
        self.assertEqual(t.notes, [])

    def test_amend_paste_guard(self):
        """粘贴前对话框变了（paste 的 guard）：一个字都不粘，也不按 Enter。"""
        m = self.mgr()
        sid = self.start(m)
        t, fp = self.perm(m, sid)

        def swap(tt, p):
            if "text" in p:
                tt.perm_cmd = "rm -rf /tmp/other"
        self.rem.before_guard = swap
        with self.assertRaises(S.OpError) as cm:
            m.amend_choose(sid, 0, "说明", fp)
        self.assertEqual(cm.exception.api_code, "dialog_changed")
        self.assertEqual(t.amends.get(0, ""), "")
        self.assertEqual(t.notes, [])

    # -------------------------------------------------------------- 停止子代理
    def agents(self, m, sid, specs, texts=None):
        s = m._get(sid)
        with s.lock:
            s.subs.absorb_main(_launch_entries(specs))
            for aid, desc in specs:
                s.subs.apply_event(aid, {"agentType": "general-purpose", "description": desc,
                                         "requestShape": "background"}, [], True)
        t = self.tui(sid)
        t.rows = [{"type": "general-purpose", "text": (texts or {}).get(aid, desc), "stopped": False}
                  for aid, desc in specs]
        return t

    def test_stop_one(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数"), ("a2", "乙数数")], {"a1": "Running first step"})
        n0 = len(self.keyops(sid))
        r = m.stop_subagent(sid, "a2")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("warning", r)
        self.assertEqual([x["stopped"] for x in t.rows], [False, True])
        self.assertIsNone(t.footer)   # Esc 回到了输入框
        ks = self.keyops(sid)[n0:]
        self.assertEqual([k["keys"] for k in ks], [["Down"], ["Down"], ["Down"], ["x"], ["Escape"]])
        self.assertEqual(ks[0]["guard"], {"input_box": True, "input_empty": True})
        self.assertEqual(ks[1]["guard"], {"footer_focus": True})
        self.assertEqual(ks[3]["guard"], {"agents_sel": {"pos": 2, "count": 3, "name": "general-purpose"}})
        self.assertEqual(ks[4]["guard"], {"footer_focus": True})
        self.assertEqual(t.input, "")

    def test_stop_via_tasks_pill(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数")])
        t.shells = 1
        n0 = len(self.keyops(sid))
        m.stop_subagent(sid, "a1")
        self.assertTrue(t.rows[0]["stopped"])
        self.assertEqual([k["keys"] for k in self.keyops(sid)[n0:]], [["Down"], ["Down"], ["Down"], ["x"], ["Escape"]])

    def test_stop_from_list_focus(self):
        """焦点已经在列表上（用户在终端里留下的）：不再按 ↓ 进入，直接移到目标行。"""
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数"), ("a2", "乙数数")])
        t.footer, t.fsel = "agents", 2
        n0 = len(self.keyops(sid))
        m.stop_subagent(sid, "a1")
        self.assertEqual([x["stopped"] for x in t.rows], [True, False])
        self.assertEqual([k["keys"] for k in self.keyops(sid)[n0:]], [["Up"], ["x"], ["Escape"]])

    def test_stop_refusals(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数"), ("a2", "乙数数")])
        n0 = len(self.keyops(sid))
        t.input = "还没发出去的话"
        for fn in (lambda: m.stop_subagent(sid, "a1"), lambda: m.stop_all_subagents(sid)):
            with self.assertRaises(S.OpError) as cm:
                fn()
            self.assertEqual(cm.exception.api_code, "input_not_empty")
        t.input = ""
        with self.assertRaises(S.OpError) as cm:
            m.stop_subagent(sid, "zz")
        self.assertEqual(cm.exception.api_code, "agent_not_running")
        with self.assertRaises(ValueError):
            m.stop_subagent(sid, "../x")
        t.rows[0]["text"] = "乙数数"   # 屏幕第 1 行认得出是乙：和启动顺序矛盾
        t.rows[1]["text"] = "甲数数"
        with self.assertRaises(S.OpError) as cm:
            m.stop_subagent(sid, "a1")
        self.assertEqual(cm.exception.api_code, "agent_ambiguous")
        t.rows.pop()   # 行数对不上
        with self.assertRaises(S.OpError) as cm:
            m.stop_subagent(sid, "a1")
        self.assertEqual(cm.exception.api_code, "agent_ambiguous")
        t.rows = []
        with self.assertRaises(S.OpError) as cm:
            m.stop_subagent(sid, "a1")
        self.assertEqual(cm.exception.api_code, "no_agents_panel")
        t.view = "perm"
        with self.assertRaises(S.OpError) as cm:
            m.stop_subagent(sid, "a1")
        self.assertEqual(cm.exception.api_code, "dialog_open")
        self.assertEqual(self.keyops(sid)[n0:], [])

    def test_stop_row_changes_before_x(self):
        """按 x 前一刻列表变了（另一个子代理刚好结束）：x 不按，焦点按 Esc 回到输入框。"""
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数"), ("a2", "乙数数")])

        def gone(tt, p):
            if p.get("keys") == ["x"]:
                tt.rows.pop(0)
                tt.fsel = 1
        self.rem.before_guard = gone
        with self.assertRaises(S.OpError) as cm:
            m.stop_subagent(sid, "a2")
        self.assertEqual(cm.exception.api_code, "screen_changed")
        self.assertFalse(any(x["stopped"] for x in t.rows))
        self.assertIsNone(t.footer)

    def test_stop_all(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数"), ("a2", "乙数数")])
        n0 = len(self.keyops(sid))
        r = m.stop_all_subagents(sid)
        self.assertTrue(r["ok"])
        self.assertEqual(r["stopped"], 2)
        self.assertTrue(all(x["stopped"] for x in t.rows))
        ks = self.keyops(sid)[n0:]
        self.assertEqual([k["keys"] for k in ks], [["C-x", "C-k"], ["C-x", "C-k"]])
        self.assertEqual(ks[0]["guard"], {"input_box": True, "input_empty": True})
        self.assertEqual(ks[1]["guard"], {"input_box": True, "input_empty": True, "hint": "kill_agents"})

    def test_stop_all_retry_and_give_up(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数")])
        t.ignore_chord = 1   # 真机偶尔第一次没反应：再按一次
        m.stop_all_subagents(sid)
        self.assertTrue(t.rows[0]["stopped"])
        t2 = self.agents(m, sid, [("a1", "甲数数")])
        t2.ignore_chord = 5
        n0 = len(self.keyops(sid))
        with self.assertRaises(S.OpError) as cm:
            m.stop_all_subagents(sid)
        self.assertEqual(cm.exception.api_code, "kill_unconfirmed")
        self.assertEqual(len(self.keyops(sid)[n0:]), 2)
        self.assertFalse(t2.rows[0]["stopped"])

    def test_stop_all_leaves_list_first(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.agents(m, sid, [("a1", "甲数数")])
        t.footer, t.fsel = "agents", 1
        n0 = len(self.keyops(sid))
        m.stop_all_subagents(sid)
        self.assertEqual([k["keys"] for k in self.keyops(sid)[n0:]], [["Escape"], ["C-x", "C-k"], ["C-x", "C-k"]])
        self.assertTrue(t.rows[0]["stopped"])


# ============================================================================ server
class TestRoutes(ServerBase):
    def test_routes(self):
        sid = self.mgr.create(jobid="276403", cwd="/x")["sid"]
        calls = []
        self.mgr.amend_choose = lambda s, i, t, e: calls.append(("amend", s, i, t, e)) or {"ok": True}
        self.mgr.stop_subagent = lambda s, a: calls.append(("stop", s, a)) or {"ok": True}
        self.mgr.stop_all_subagents = lambda s: calls.append(("all", s)) or {"ok": True, "stopped": 1}
        fp = {"kind": "permission", "labels": ["Yes", "No"]}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/amend" % sid, {"index": 0, "text": "说明", "expect": fp})
        self.assertEqual(st, 200, js)
        st, js, _, _ = self.req("POST", "/api/sessions/%s/agents/stop" % sid, {"agent_id": "a1"})
        self.assertEqual(st, 200, js)
        st, js, _, _ = self.req("POST", "/api/sessions/%s/agents/stop_all" % sid, {})
        self.assertEqual(st, 200, js)
        self.assertEqual(calls, [("amend", sid, 0, "说明", fp), ("stop", sid, "a1"), ("all", sid)])
        for body in ({"index": 0}, {"index": "0", "text": "x"}, {"index": 0, "text": "  "}, {"index": 0, "text": "x", "expect": 1}):
            st, js, _, _ = self.req("POST", "/api/sessions/%s/amend" % sid, body)
            self.assertEqual(st, 400, (body, js))

        def boom(s, a):
            raise S.OpError("认不准", code="agent_ambiguous")
        self.mgr.stop_subagent = boom
        st, js, _, _ = self.req("POST", "/api/sessions/%s/agents/stop" % sid, {"agent_id": "a1"})
        self.assertEqual((st, js.get("code")), (409, "agent_ambiguous"))
        st, js, _, _ = self.req("GET", "/api/bootstrap")
        self.assertFalse({"x", "C-k"} & set(js["keys"]))   # 终端面板的按键白名单没有放开


# ============================================================================ fake_tui
class TestFakeTui(FakeBase):
    def st(self, a):
        return tui_parse.parse_screen("\n".join(a.frame()))

    def test_amend_yes(self):
        a = self.app()
        self.submit(a, "/tool 看看")
        self.run_turn(a)
        d = self.st(a)["dialog"]
        self.assertEqual((d["hints"], d["amendable"]), ("Esc to cancel · Tab to amend", [0, 3]))
        a.on_key("Tab")
        self.assertEqual(self.st(a)["dialog"]["amend"], {"index": 0, "base": "Yes", "text": "", "placeholder": True})
        a.feed("\x1b[200~顺便告诉我 1 s\x1b[201~".encode())   # 说明里的数字 / s 是文字，不是选项快捷键
        self.assertEqual(self.st(a)["dialog"]["amend"]["text"], "顺便告诉我 1 s")
        a.on_key("Tab")   # 收起：文字保留
        self.assertIsNone(self.st(a)["dialog"]["amend"])
        a.on_key("Tab")
        self.assertEqual(self.st(a)["dialog"]["amend"]["text"], "顺便告诉我 1 s")
        a.on_key("Enter")
        self.run_turn(a)
        es = self.transcript(a)
        tr = [e for e in es if e["type"] == "user" and isinstance(e["message"]["content"], list)
              and e["message"]["content"][0].get("type") == "tool_result"][-1]
        self.assertEqual([b["type"] for b in tr["message"]["content"]], ["tool_result", "text"])
        self.assertEqual(tr["message"]["content"][1]["text"], "顺便告诉我 1 s")
        st = self.st(a)
        self.assertIsNone(st["dialog"])
        self.assertIn("❯ 顺便告诉我 1 s", "\n".join(a.frame()))

    def test_amend_no_continues(self):
        a = self.app()
        self.submit(a, "/tool 看看")
        self.run_turn(a)
        for k in ("Down", "Down", "Down", "Tab"):
            a.on_key(k)
        a.on_text("改用 echo")
        a.on_key("Enter")
        self.run_turn(a)
        es = self.transcript(a)
        rej = [e for e in es if e["type"] == "user" and isinstance(e["message"]["content"], list)
               and str(e["message"]["content"][0].get("content")).endswith("the user said:\n改用 echo")]
        self.assertEqual(len(rej), 1)
        self.assertFalse(any("[Request interrupted" in json.dumps(e, ensure_ascii=False) for e in es))
        self.assertIn("Not run · rejected", "\n".join(a.frame()))

    def test_amend_esc_cancels(self):
        a = self.app()
        self.submit(a, "/tool 看看")
        self.run_turn(a)
        a.on_key("Tab")
        a.on_text("说明")
        a.on_key("Escape")
        self.run_turn(a)
        self.assertIsNone(self.st(a)["dialog"])
        self.assertTrue(any("[Request interrupted" in json.dumps(e, ensure_ascii=False) for e in self.transcript(a)))

    def test_agents_footer(self):
        a = self.app()
        self.submit(a, "/agents2 派两个")
        self.run_turn(a)
        st = self.st(a)
        self.assertEqual([r["desc"] for r in st["agents_panel"]], ["", "fake 子代理甲", "fake 子代理乙"])
        self.assertTrue(st["mode_line"].endswith("· ↓ to manage"))
        a.on_key("Down")
        st = self.st(a)
        self.assertEqual((st["agents_focus"], st["agents_hint"]), (True, "↑/↓ to select"))
        a.on_key("Down")
        a.on_text("x")
        st = self.st(a)
        self.assertEqual(st["agents_hint"], "enter to view · x to clear")
        self.assertEqual([r["state"] for r in st["agents_panel"]], [None, "stopped", None])
        aid = [x for x in a.stopped_rows][0]
        with open(a.stopped_rows[aid].metap, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["stoppedByUser"])
        note = [e for e in self.transcript(a) if e["type"] == "user"
                and "<status>killed</status>" in json.dumps(e, ensure_ascii=False)]
        self.assertEqual(len(note), 1)
        a.on_key("Escape")
        st = self.st(a)
        self.assertFalse(st["agents_focus"])
        self.assertEqual(st["input"]["text"], "")
        a.on_text("x")   # 焦点在输入框：x 是一个字
        self.assertEqual(self.st(a)["input"]["text"], "x")

    def test_summary_text(self):
        a = self.app()
        self.submit(a, "/agents2 派两个")
        self.run_turn(a)
        for sub in a.agents.values():
            sub.started -= 10
        self.assertTrue(all("progress summary" in r["desc"] for r in self.st(a)["agents_panel"][1:]))

    def test_tasks_pill(self):
        a = self.app()
        self.submit(a, "/agents2 派两个")
        self.run_turn(a)
        a.bg_until = [time.time() + 30]
        a.on_key("Down")
        st = self.st(a)
        self.assertTrue(st["tasks_focus"])
        a.on_key("Down")
        self.assertTrue(self.st(a)["agents_focus"])

    def test_no_bg_hint(self):
        a = self.app()
        a.on_key("C-x")
        a.on_key("C-k")
        self.assertTrue(tuiact.no_bg_visible("\n".join(a.frame())))


# ============================================================================ fake 端到端（真实 AgentLink + fake_tui + 本机 tmux）
@unittest.skipUnless(HAVE_TMUX, "需要本机 tmux")
class TestFakeEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccr-ta-", dir="/tmp"))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "proj"))
        self.cfg = {"host": "gpu-cluster", "jobid": "12345", "default_cwd": "~", "fake": True,
                    "fake_home": self.home, "fake_tmpdir": os.path.join(self.tmp, "tmux"), "fake_tmux": TMUX,
                    "fake_env": {"CCR_FAKE_DELAY": "0.03", "CCR_FAKE_THINK": "0.1"}}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        sock_dir = os.path.join(self.tmp, "tmux", "tmux-%d" % os.getuid())
        if os.path.isdir(sock_dir):
            env = dict(os.environ)
            env.pop("TMUX", None)
            subprocess.run([TMUX, "-S", os.path.join(sock_dir, "ccr"), "-f", "/dev/null", "kill-server"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        shutil.rmtree(self.tmp, True)

    def mgr(self):
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp, "data"))
        m.exit_timeout = 8
        self.addCleanup(lambda: m._shutdown or m.shutdown())
        return m

    def wait(self, pred, what, m, sid, t=20):
        self.assertTrue(wait_until(pred, t, 0.1), (what, m.get_screen(sid)["text"]))

    def test_amend_and_stop(self):
        m = self.mgr()
        sid = m.create(jobid="12345", cwd="~/proj")["sid"]
        self.wait(lambda: m.get_meta(sid)["state"] == "idle", "idle", m, sid)
        m.send_message(sid, "/tool 测一下")
        self.wait(lambda: (m.get_screen(sid)["state"].get("dialog") or {}).get("kind") == "permission", "perm", m, sid)
        fp = S.dialog_fingerprint(m.get_screen(sid)["state"]["dialog"])
        r = m.amend_choose(sid, 0, "执行完说一声「好」", fp)
        self.assertTrue(r["ok"])
        self.wait(lambda: m.get_meta(sid)["state"] == "idle", "idle2", m, sid)
        self.assertIn("❯ 执行完说一声「好」", m.get_screen(sid)["text"])
        # No + 说明
        m.send_message(sid, "/tool 再来")
        self.wait(lambda: (m.get_screen(sid)["state"].get("dialog") or {}).get("kind") == "permission", "perm2", m, sid)
        fp = S.dialog_fingerprint(m.get_screen(sid)["state"]["dialog"])
        m.amend_choose(sid, 3, "别执行了", fp)
        self.wait(lambda: "收到你的说明：「别执行了」" in m.get_screen(sid)["text"], "reply", m, sid)
        self.wait(lambda: m.get_meta(sid)["state"] == "idle", "idle3", m, sid)
        # 两个后台子代理：停止乙（列表行此时可能已换成进度摘要），再全部停止
        m.send_message(sid, "/agents2 派两个")
        s = m._get(sid)
        self.wait(lambda: len(tuiact.sorted_running(s.subs)) == 2 and m.get_meta(sid)["state"] == "idle", "agents",
                  m, sid)
        run = tuiact.sorted_running(s.subs)
        self.assertEqual([x["description"] for x in run], list(fake_tui.AGENTS2))
        r = m.stop_subagent(sid, run[1]["agent_id"])
        self.assertTrue(r["ok"], r)
        self.assertNotIn("warning", r)
        st = m.get_screen(sid)["state"]
        self.assertFalse(st["agents_focus"])
        self.assertEqual([x.get("state") for x in st["agents_panel"]][2], "stopped")
        self.wait(lambda: s.subs.summary(run[1]["agent_id"])["status"] == "killed", "killed", m, sid)
        r = m.stop_all_subagents(sid)
        self.assertTrue(r["ok"], r)
        self.wait(lambda: s.subs.summary(run[0]["agent_id"])["status"] == "killed", "killed all", m, sid)
        self.assertEqual(m.get_screen(sid)["state"]["input"]["text"], "")


if __name__ == "__main__":
    unittest.main()
