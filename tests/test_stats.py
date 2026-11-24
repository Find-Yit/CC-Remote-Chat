"""ccr.stats：上下文 / 总花费 / token/s 的汇总（真机录制的 fixtures 帧 + transcript 回放）。"""

import json
import pathlib
import re
import unittest

from ccr import stats as ST
from ccr.tui_parse import parse_screen

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


def stream_frames():
    with open(FIX / "tui_screens_stream.ndjson", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if "frames" in d:
                return [(x["t"], x["s"]) for x in d["frames"]]
    raise AssertionError("no stream_frames")


def screens(name):
    out = {}
    with open(FIX / name, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if "plain" in d:
                out[d["name"]] = d["plain"]
    return out


def sub_frames():
    txt = (FIX / "subagent_sample" / "screen_frames.txt").read_text(encoding="utf-8")
    parts = re.split(r"^=== t=([\d.]+)\n", txt, flags=re.M)
    return [(float(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]


def transcript(name):
    with open(FIX / name, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


STREAM = screens("tui_screens_stream.ndjson")
DIALOGS = screens("tui_screens_dialogs.ndjson")


def replay(frames, tr=None):
    tr = tr or ST.StatsTracker()
    snaps = []
    for t, text in frames:
        tr.on_screen(parse_screen(text), t)
        snaps.append(tr.snapshot())
    return tr, snaps


def no_status(text):
    """去掉画面里的状态栏行（模拟用户没配 statusline）。"""
    return "\n".join(l for l in text.split("\n") if "$0." not in l)


class TestStreamFixture(unittest.TestCase):
    """29 帧真实流式回复：spinner 只在 70.67–71.49 显示 tokens（88→211），随后消失到本轮结束（Baked for 9s）。"""

    def test_context_and_cost_from_status(self):
        tr, snaps = replay(stream_frames())
        self.assertEqual(snaps[0]["context_used"], 0)
        self.assertEqual(snaps[0]["context_limit"], 1000000)
        s = snaps[-1]
        self.assertEqual((s["context_used"], s["context_limit"], s["context_pct"], s["context_source"],
                          s["context_approx"]), (25000, 1000000, 3.0, "status", False))
        self.assertEqual((s["cost_usd"], s["cost_text"], s["cost_source"]), (0.0, "$0.0000", "status"))
        tr.on_screen(parse_screen(STREAM["after_stream"]), 80.0)
        self.assertEqual(tr.snapshot()["cost_text"], "$0.0035")

    def test_live_rate_and_last_turn(self):
        tr, snaps = replay(stream_frames())
        live = [s["tok_s_live"] for s in snaps]
        self.assertTrue(all(v is None for v in live[:21]), live)          # 0.5s 内不出数
        self.assertAlmostEqual(live[21], (197 - 88) / (71.22 - 70.67), delta=0.2)
        self.assertAlmostEqual(live[22], (211 - 88) / (71.49 - 70.67), delta=0.2)
        self.assertIsNone(live[-1])                                      # 本轮结束：没有"生成中"
        self.assertTrue(all(s["tok_s_last"] is None for s in snaps[:-1]))
        last = snaps[-1]
        self.assertEqual(last["tok_s_last"], round(211 / 6, 1))          # 该轮最终 tokens / 该轮 elapsed
        self.assertEqual((last["last_turn_tokens"], last["last_turn_elapsed_s"]), (211, 6.0))
        self.assertFalse(tr.in_turn)

    def test_spinner_vanishing_holds_then_drops(self):
        tr, snaps = replay(stream_frames()[:-1])   # 不含最后一帧（仍在忙）
        # spinner 在 71.49 后消失：3s 内保留最近速率，之后变 None（74.23 - 71.49 < 3）
        self.assertIsNotNone(snaps[-1]["tok_s_live"])
        busy = parse_screen(stream_frames()[-2][1])
        tr.on_screen(busy, 71.49 + ST.StatsTracker.LIVE_HOLD + 0.1)
        self.assertIsNone(tr.snapshot()["tok_s_live"])
        self.assertTrue(tr.in_turn)


class TestSubagentFixture(unittest.TestCase):
    """真机：一轮里三次请求（elapsed 归零换段；新请求开始时计数先往下走 428→291→…→4 再上升）。"""

    def test_segments_and_last(self):
        tr, snaps = replay(sub_frames())
        last = snaps[-1]
        # 每段最后一次看到的值：8s·428、5s·190、3s·25 → (428+190+25)/(8+5+3)
        self.assertEqual(last["tok_s_last"], round(643 / 16, 1))
        self.assertEqual((last["last_turn_tokens"], last["last_turn_elapsed_s"]), (643, 16.0))
        self.assertEqual((last["context_used"], last["cost_text"]), (28000, "$0.0079"))

    def test_counter_drop_reports_zero_not_spike(self):
        tr = ST.StatsTracker()
        by_t = {}
        for t, text in sub_frames():
            tr.on_screen(parse_screen(text), t)
            snap = tr.snapshot()
            by_t[t] = snap["tok_s_live"] if snap else None
        self.assertGreater(by_t[5.2], 100)            # 第一次请求输出中
        for t in (9.2, 9.8, 10.3, 10.8, 11.3):        # 新请求：计数动画回落 → 此刻没在生成
            self.assertEqual(by_t[t], 0.0, t)
        self.assertGreater(by_t[12.3], 0)             # 计数重新上升
        self.assertIsNone(by_t[18.4])                 # 本轮结束
        self.assertTrue(all(v is None or v >= 0 for v in by_t.values()))

    def test_tool_wait_decays_to_zero(self):
        """tokens 停在 277（前台子代理在跑）：窗口滑过后速率趋近 0。"""
        tr = ST.StatsTracker()
        base = parse_screen([t for tt, t in sub_frames() if tt == 6.7][0])
        for k in range(30):
            tr.on_screen(base, 100.0 + k * 0.25)
        self.assertEqual(tr.snapshot()["tok_s_live"], 0.0)


class TestTurnLifecycle(unittest.TestCase):
    def _busy(self, tokens, elapsed, status="3% 25k/1M $0.0100"):
        return {"busy": True, "dialog": None, "input": {"visible": True}, "status_line": status,
                "status_stats": None, "spinner_info": {"tokens": tokens, "elapsed_s": elapsed, "token_dir": "down"}}

    def test_dialog_pauses_turn(self):
        tr = ST.StatsTracker()
        tr.on_screen(self._busy(100, 2.0), 1.0)
        tr.on_screen(self._busy(200, 3.0), 2.0)
        tr.on_screen(parse_screen(DIALOGS["perm"]), 3.0)   # 权限框：不结束本轮
        self.assertTrue(tr.in_turn)
        self.assertEqual(tr.snapshot()["tok_s_live"], 100.0)   # 对话框期间保留本轮速率
        tr.on_screen(self._busy(260, 4.0), 4.0)
        tr.on_screen(parse_screen(DIALOGS["perm_after_esc"]), 5.0)   # 空闲（输入框可见）→ 结束
        snap = tr.snapshot()
        self.assertFalse(tr.in_turn)
        self.assertEqual(snap["tok_s_last"], 65.0)   # 260 / 4
        self.assertIsNone(snap["tok_s_live"])

    def test_turn_without_tokens_keeps_previous_last(self):
        tr = ST.StatsTracker()
        tr.on_screen(self._busy(120, 4.0), 1.0)
        tr.on_screen(parse_screen(STREAM["ready"]), 2.0)
        self.assertEqual(tr.snapshot()["tok_s_last"], 30.0)
        tr.on_screen(parse_screen(STREAM["ask"]), 3.0)       # 忙，spinner 没有 tokens
        tr.on_screen(parse_screen(STREAM["ask_after_esc"]), 4.0)
        self.assertEqual(tr.snapshot()["tok_s_last"], 30.0)

    def test_stop_discards_running_turn(self):
        tr = ST.StatsTracker()
        tr.on_screen(self._busy(100, 1.0), 1.0)
        tr.on_screen(self._busy(300, 2.0), 2.0)
        self.assertIsNotNone(tr.snapshot()["tok_s_live"])
        tr.stop()
        self.assertIsNone(tr.snapshot())   # 只有进行中的速率（没有状态栏/上一轮）：丢掉后什么都不知道

    def test_up_arrow_and_k_tokens(self):
        tr = ST.StatsTracker()
        for i, text in enumerate(("Working… (1m 5s · ↑ 1.2k tokens)", "Working… (1m 6s · ↑ 1.5k tokens)",
                                  "Working… (1m 7s · ↑ 1.8k tokens)")):
            st = parse_screen("✻ " + text + "\n" + "─" * 80 + "\n❯ \n" + "─" * 80 +
                              "\n  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt")
            self.assertEqual(st["spinner_info"]["token_dir"], "up")
            tr.on_screen(st, 10.0 + i)
        self.assertEqual(tr.snapshot()["tok_s_live"], 300.0)


class TestContextFallback(unittest.TestCase):
    ENTRIES = transcript("transcript_interactive_59dd443a.jsonl")

    def test_transcript_usage_when_no_status(self):
        tr = ST.StatsTracker()
        tr.on_screen(parse_screen(no_status(STREAM["ready"])), 1.0)
        self.assertIsNone(tr.snapshot())
        tr.on_transcript(self.ENTRIES, reset=True)
        s = tr.snapshot()
        # 最后一条主链 assistant：input 2 + cache_creation 321 + cache_read 25727
        self.assertEqual((s["context_used"], s["context_limit"], s["context_source"], s["context_approx"]),
                         (26050, 200000, "transcript", True))
        self.assertEqual(s["context_pct"], 13.0)
        self.assertIsNone(s["cost_usd"])
        self.assertIsNone(s["cost_source"])

    def test_limit_inferred_from_hints(self):
        tr = ST.StatsTracker()
        tr.on_transcript(self.ENTRIES, reset=True)
        self.assertEqual(tr.snapshot(["Haiku 5.5", None])["context_limit"], 200000)
        self.assertEqual(tr.snapshot(["claude-fable-5-1[1m]"])["context_limit"], 1000000)
        self.assertEqual(tr.snapshot(["Fable 5.2 (1M)"])["context_limit"], 1000000)
        # 状态栏里没有上下文、但有 "· 1M ·" 模型标记
        tr.on_screen(parse_screen("─" * 80 + "\n❯ \n" + "─" * 80 + "\n  u@n [Haiku 5.5 · 1M · effort:low]\n"
                                  "  ⏵⏵ auto mode on (shift+tab to cycle)"), 1.0)
        s = tr.snapshot()
        self.assertEqual((s["context_limit"], s["context_source"], s["context_pct"]), (1000000, "transcript", 2.6))

    def test_status_preferred_and_kept_when_hidden(self):
        tr = ST.StatsTracker()
        tr.on_transcript(self.ENTRIES, reset=True)
        tr.on_screen(parse_screen(DIALOGS["perm_after_esc"]), 1.0)
        s = tr.snapshot()
        self.assertEqual((s["context_used"], s["context_source"], s["cost_text"]), (25000, "status", "$0.0014"))
        tr.on_screen(parse_screen(DIALOGS["perm_write"]), 2.0)   # 对话框遮住状态栏：沿用上次
        self.assertEqual(tr.snapshot()["cost_text"], "$0.0014")

    def test_skips_sidechain_synthetic_and_reset(self):
        tr = ST.StatsTracker()
        main = {"type": "assistant", "message": {"model": "claude-x", "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30}}}
        side = {"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 999999}}}
        synth = {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}}
        junk = [None, 5, {"type": "assistant"}, {"type": "assistant", "message": {"usage": {"input_tokens": "x"}}},
                {"type": "user", "message": {"usage": {"input_tokens": 5}}}]
        tr.on_transcript([main, side, synth] + junk)
        self.assertEqual(tr.snapshot()["context_used"], 60)
        tr.on_transcript([], reset=True)   # /clear：新 transcript
        self.assertIsNone(tr.snapshot())


class TestPersist(unittest.TestCase):
    def test_sanitize_and_restore(self):
        self.assertIsNone(ST.sanitize(None))
        self.assertIsNone(ST.sanitize({"context_used": "25k"}))
        d = ST.sanitize({"context_used": 25000, "context_limit": 1000000, "context_pct": 3, "context_source": "status",
                         "cost_usd": 0.0035, "cost_text": "$0.0035", "cost_source": "status", "tok_s_live": 50,
                         "tok_s_last": 35.2, "last_turn_tokens": 211, "last_turn_elapsed_s": 6, "updated_at": 1.0,
                         "evil": "<script>", "context_approx": 0})
        self.assertIsNone(d["tok_s_live"])           # 生成中速率不恢复
        self.assertNotIn("evil", d)
        self.assertEqual((d["context_used"], d["cost_text"], d["tok_s_last"]), (25000, "$0.0035", 35.2))
        bad = ST.sanitize({"context_used": float("nan"), "context_pct": -1, "cost_usd": True, "cost_text": "x" * 99,
                           "context_source": "evil", "tok_s_last": 3})
        self.assertEqual((bad["context_used"], bad["context_pct"], bad["cost_usd"], bad["cost_text"],
                          bad["context_source"]), (None, None, None, None, None))
        tr = ST.StatsTracker()
        tr.restore(d)
        s = tr.snapshot()
        self.assertEqual((s["context_used"], s["context_source"], s["cost_text"], s["tok_s_last"], s["tok_s_live"]),
                         (25000, "status", "$0.0035", 35.2, None))
        tr.on_screen(parse_screen(STREAM["interrupted"]), 1.0)   # 新屏幕覆盖
        self.assertEqual(tr.snapshot()["cost_text"], "$0.0054")


if __name__ == "__main__":
    unittest.main()
