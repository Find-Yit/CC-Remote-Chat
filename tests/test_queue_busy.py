"""忙时连发消息（排队）时界面不再抖动的回归测试（2026-11-24 用户报告："模型在思考时再发两条，再接着发，聊天框一直在抖动"）。

真机 claude v2.1.331（tmux 200x50，haiku）实测的两处画面事实：
1. 输入框里有文字时，模式行不显示 "esc to interrupt"（此时 Esc 是清空输入框）——忙时发消息"粘贴 → 确认 → 回车"的
   那几帧只看模式行就成了"空闲"，前端占位文字 / 中断按钮 / 实时草稿 / 侧栏状态每发一条闪一次。
   → session._busy_hidden_by_input 用排队提示 / claude 自己的 session status 补回 busy。
2. 本轮还在 thinking（没有任何输出）时，排队块紧挨在刚发出的那条消息下面，往上就是上一轮的结束行 / 启动横幅：
   原来会把刚发出的消息也算成"排队中"、回显退回上一条用户消息 → 实时草稿里重复显示上一轮的整段回复，
   一出输出又全部缩回去（时间线大幅跳动）。→ tui_parse._queued_blocks 识别"上一轮已结束"的边界。
画面按实测版式重建（路径、状态栏换成中性内容）。
"""

import threading
import unittest

from ccr import session as S, tui_parse
from tests.test_fake_tui import Base as FakeBase
from tests.test_session import RULE, Base, SimTUI, wait_until

QUEUED = "  ⎿  Queued · ctrl+x ctrl+s to send now"
BANNER = ["", " ▐▛███▛█   Claude Code v2.1.331", "▝▜██████▀  Haiku 5.5 with low effort · API Usage Billing",
          " ▝▝   ▝▝   /home/u", "", ""]
SENT = "❯ 请先认真思考再回答：用三种不同方法证明素数有无穷多个。"
STATUS = "  u@node ▱▱▱▱▱▱▱▱▱▱ 2% 20k/1M $0.0010 [Haiku 5.5 · 1M · effort:low]"
MODE = "  ⏵⏵ auto mode on (shift+tab to cycle)"
BUSY_MODE = MODE + " · esc to interrupt · ← for agents"


def screen(content, inp="Press up to edit queued messages", busy=True, spinner="✽ Whatchamacalliting…"):
    body = list(content) + [""] * 6 + ([spinner] if spinner else []) + [""]
    return "\n".join(body + [RULE, "❯ " + inp if inp else "❯", RULE, STATUS, BUSY_MODE if busy else MODE])


class QueuedWhileThinkingParseTests(unittest.TestCase):
    """问题 2：thinking 阶段刚发出的消息不是排队消息。"""

    def test_fresh_session_banner_above(self):
        # 真机 3.153s 那一帧：横幅 → 刚发出的消息 → 空行 → 排队的 Q1 → Queued
        r = tui_parse.parse_screen(screen(BANNER + [SENT, "", "❯ 只回复：Q1-OK", QUEUED]))
        self.assertTrue(r["busy"])
        self.assertEqual(r["hints"]["queued"], ["只回复：Q1-OK"])
        self.assertTrue(r["hints"]["send_now"])
        self.assertEqual(r["echo"], SENT[2:])
        self.assertIsNone(r["live"])   # 原来 echo 为空、live = 整个横幅

    def test_previous_turn_done_line_above(self):
        prev = ["❯ 你好", "", "⏺ 你好！这是上一轮的回复，", "  有好几行。", "", "✻ Worked for 9s · done 19:43", ""]
        for queued in (["❯ Q1", "", "❯ Q2"], ["❯ Q1", "❯ Q2"]):   # v2.1.331 块间有空行；v2.1.330 连续
            with self.subTest(queued=queued):
                r = tui_parse.parse_screen(screen(BANNER + prev + [SENT, ""] + queued + [QUEUED]))
                self.assertEqual(r["hints"]["queued"], ["Q1", "Q2"])
                self.assertEqual(r["echo"], SENT[2:])
                self.assertIsNone(r["live"])   # 原来 echo = "你好"、live = 上一轮的整段回复（草稿里重复一遍）
                self.assertIsNone(r["turn_done"])

    def test_interrupted_above(self):
        prev = ["❯ 你好", "  ⎿  Interrupted · What should Claude do instead?", ""]
        r = tui_parse.parse_screen(screen(BANNER + prev + [SENT, "", "❯ Q1", QUEUED]))
        self.assertEqual(r["hints"]["queued"], ["Q1"])
        self.assertEqual(r["echo"], SENT[2:])

    def test_streaming_unchanged(self):
        # 已经有输出：排队块上方是本轮的回复，所有 ❯ 块都是排队消息（与原来一致）
        content = BANNER + [SENT, "", "⏺ 方法一：欧几里得", "", "  假设素数只有有限个……", "", "❯ Q1", "", "❯ Q2", QUEUED]
        r = tui_parse.parse_screen(screen(content, spinner=None))
        self.assertEqual(r["hints"]["queued"], ["Q1", "Q2"])
        self.assertEqual(r["echo"], SENT[2:])
        self.assertIn("假设素数只有有限个", r["live"])
        self.assertNotIn("Q1", r["live"])

    def test_single_block_after_boundary_kept(self):
        # 只有一个 ❯ 块（例如这一轮不是由用户消息开启的）：不能把唯一的排队消息当成已发送
        r = tui_parse.parse_screen(screen(BANNER + ["✻ Worked for 9s · done 19:43", "", "❯ Q1", QUEUED]))
        self.assertEqual(r["hints"]["queued"], ["Q1"])

    def test_top_of_screen_kept(self):
        # 排队块从屏幕第一行开始（上面滚出去了）：不确定，保持原样
        r = tui_parse.parse_screen(screen(["❯ A", "", "❯ B", QUEUED]))
        self.assertEqual(r["hints"]["queued"], ["A", "B"])


class BusyHiddenByInputTests(unittest.TestCase):
    """问题 1：输入框有文字时模式行没有 "esc to interrupt"。"""

    def test_raw_parse_and_inference(self):
        # 真机 5.208s 那一帧：两条已排队，第三条刚粘贴进输入框、还没回车
        content = BANNER + [SENT, "", "⏺ 方法一", "", "❯ Q1", "", "❯ Q2", QUEUED]
        st = tui_parse.parse_screen(screen(content, inp="只回复：Q3-OK", busy=False, spinner=None))
        self.assertFalse(st["busy"])            # 只看模式行
        self.assertEqual(st["input"]["text"], "只回复：Q3-OK")
        self.assertTrue(S._busy_hidden_by_input(st, "busy"))
        self.assertTrue(S._busy_hidden_by_input(st, None))   # 排队提示本身就说明一轮正在进行
        self.assertTrue(S._busy_hidden_by_input(st, "idle"))

    def test_status_and_spinner_fallback(self):
        # 第一条排队消息粘贴进来时还没有排队提示：看 claude 自己的 session status；读不到时看 spinner
        st = tui_parse.parse_screen(screen(BANNER + [SENT], inp="只回复：Q1-OK", busy=False))
        self.assertFalse(st["busy"])
        self.assertFalse(st["hints"]["queued"])
        self.assertTrue(S._busy_hidden_by_input(st, "busy"))
        self.assertFalse(S._busy_hidden_by_input(st, "idle"))
        self.assertTrue(S._busy_hidden_by_input(st, None))
        st2 = tui_parse.parse_screen(screen(BANNER + [SENT], inp="x", busy=False, spinner=None))
        self.assertFalse(S._busy_hidden_by_input(st2, None))

    def test_not_inferred(self):
        idle_done = BANNER + [SENT, "", "⏺ 好的", "", "✻ Worked for 3s · done 19:43"]
        # 本轮已经结束（结束行在回显之后）：status 还没来得及变 idle 也不算忙
        st = tui_parse.parse_screen(screen(idle_done, inp="新消息", busy=False, spinner=None))
        self.assertFalse(S._busy_hidden_by_input(st, "busy"))
        # 输入框为空 / 不可见 / 有对话框：不推断
        st = tui_parse.parse_screen(screen(BANNER + [SENT], inp="", busy=False))
        self.assertFalse(S._busy_hidden_by_input(st, "busy"))
        self.assertFalse(S._busy_hidden_by_input({"busy": False, "input": {"visible": False, "text": "x"}}, "busy"))
        self.assertFalse(S._busy_hidden_by_input({"busy": False, "dialog": {"kind": "permission"},
                                                  "input": {"visible": True, "text": "x"}}, "busy"))


class FakeTuiModeLineTests(FakeBase):
    """fake_tui 仿真机：输入框有文字时模式行不显示 "esc to interrupt"（--fake 才能复现这个问题）。"""

    def test_mode_line_hides_esc_while_typing(self):
        a = self.app(CCR_FAKE_THINK="5")
        self.submit(a, "/long 忙")
        self.assertTrue(self.frame(a)[-2].endswith(" · esc to interrupt"))
        a.on_text("排队消息")
        got = self.frame(a)
        self.assertFalse(got[-2].endswith("esc to interrupt"), got[-2])
        st = tui_parse.parse_screen("\n".join(got))
        self.assertFalse(st["busy"])
        self.assertTrue(S._busy_hidden_by_input(st, "busy"))
        a.on_key("Enter")
        got = self.frame(a)
        self.assertTrue(got[-2].endswith(" · esc to interrupt"), got[-2])
        st = tui_parse.parse_screen("\n".join(got))
        self.assertTrue(st["busy"])
        self.assertEqual(st["hints"]["queued"], ["排队消息"])   # thinking 中：刚发出的 /long 不算排队
        self.assertEqual(st["echo"], "/long 忙")


class QueueTUI(SimTUI):
    """SimTUI + 真机行为：忙时提交 = 进入队列（"❯ …" 块 + 共用的 Queued 行、占位文字），
    输入框有文字时模式行不显示 "esc to interrupt"。"""

    def _footer(self):
        mode = self.MODES[self.mode] + (" · esc to interrupt" if self.busy and not self.input else "")
        shown = self.input or ("Press up to edit queued messages" if self.queue else "")
        return [RULE, "❯ " + shown, RULE, "  u@node ▱▱▱ 0% [Haiku · 1M · effort:low]", "  " + mode]

    def _body(self, n=16):
        c = list(self.content)
        if self.busy:
            for q in self.queue:
                c += ["❯ " + q, ""]
            if self.queue:
                c[-1] = QUEUED
            c += ["", "✶ Schlepping… (3s · thinking with low effort)"]
        return (c[-n:] + [""] * n)[:n] if len(c) < n else c[-n:]

    def _submit(self, text):
        if self.busy:
            self.input = ""
            self.queue.append(text.strip())
            return
        super()._submit(text)


class SendWhileBusyTests(Base):
    """端到端（桩 agent）：忙时连发 3 条，send_message 过程中每一帧屏幕状态都是 busy，meta 一直是 busy。"""

    def test_no_idle_flicker_while_queueing(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        with t.lock:
            t.__class__ = QueueTUI
            t.queue = []
        m.send_message(sid, "/busy")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "busy"))
        seen = []
        lock = threading.Lock()
        orig = m._apply_screen

        def rec(s, text):
            st = orig(s, text)
            with lock:
                seen.append((bool(st.get("busy")), (st.get("input") or {}).get("text"), s.state))
            return st
        m._apply_screen = rec
        for q in ("Q1", "Q2", "Q3"):
            m.send_message(sid, q)
        self.assertEqual(t.queue, ["Q1", "Q2", "Q3"])
        with lock:
            typed = [x for x in seen if x[1]]
            self.assertTrue(typed, seen)   # 确实抓到了"文字在输入框里、还没回车"的帧
            self.assertTrue(all(b for b, _i, _st in seen), seen)
            self.assertTrue(all(st == "busy" for _b, _i, st in seen), seen)
        # 第 3 条的 Enter 刚按下时，最近一次屏幕快照可能还没轮到（忙时 0.25s 抓一次屏）：等下一帧再核对
        self.assertTrue(wait_until(lambda: m.get_screen(sid)["state"]["hints"]["queued"] == ["Q1", "Q2", "Q3"]),
                        m.get_screen(sid)["state"]["hints"])


if __name__ == "__main__":
    unittest.main()
