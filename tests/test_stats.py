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
        # 最后一条主链 assistant：input 2 + cache_creation 321 + cache_read 25727；模型 claude-haiku-5-5 原生 1M
        self.assertEqual((s["context_used"], s["context_limit"], s["context_source"], s["context_approx"]),
                         (26050, 1000000, "transcript", True))
        self.assertEqual((s["context_pct"], s["context_limit_source"], s["context_model"]), (2.6, "model", "Haiku 5.5"))
        self.assertIsNone(s["cost_usd"])
        self.assertIsNone(s["cost_source"])

    def test_limit_inferred_from_hints(self):
        tr = ST.StatsTracker()
        # 表里 200k 的模型（Haiku 4.5）：提示里有 [1m] / 1M → 1M
        tr.on_transcript([dict(e, message=dict(e["message"], model="claude-haiku-4-5-20251001"))
                          if e.get("type") == "assistant" else e for e in self.ENTRIES], reset=True)
        s = tr.snapshot(["Haiku 4.5", None])
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_model"]),
                         (200000, "model", "Haiku 4.5"))
        self.assertEqual(tr.snapshot(["claude-fable-5-1[1m]"])["context_limit"], 1000000)
        self.assertEqual(tr.snapshot(["Fable 5.2 (1M)"])["context_limit_source"], "hint")
        # 状态栏里没有上下文、但有 "· 1M ·" 模型标记
        tr.on_screen(parse_screen("─" * 80 + "\n❯ \n" + "─" * 80 + "\n  u@n [Haiku 5.5 · 1M · effort:low]\n"
                                  "  ⏵⏵ auto mode on (shift+tab to cycle)"), 1.0)
        s = tr.snapshot()
        self.assertEqual((s["context_limit"], s["context_source"], s["context_pct"], s["context_limit_source"]),
                         (1000000, "transcript", 2.6, "hint"))

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


def asst(model, ctx, side=False):
    """主链 assistant 条目：上下文 tokens = ctx（input 1 + cache_creation 0 + cache_read ctx-1）。"""
    return {"type": "assistant", "isSidechain": side, "message": {"model": model, "usage": {
        "input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 1}}}


def ctx_entries(name):
    with open(FIX / name, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def ctx_stdout(used, limit, model="claude-opus-5-5", display="Opus 5.5"):
    """仿 CLI 2.1.331 交互式 /context 输出（带 ANSI；数字为 CLI 紧凑写法）。"""
    return {"type": "system", "subtype": "local_command", "content":
            "<local-command-stdout> \x1b[1mContext usage\x1b[22m\n\x1b[38;5;245m⛀ \x1b[38;5;241m⛶ ⛶ \x1b[39m  %s\n"
            "\x1b[38;5;241m⛶ ⛶ ⛶ \x1b[39m  \x1b[38;5;241m%s\x1b[39m\n"
            "\x1b[38;5;241m⛶ ⛶ ⛶ \x1b[39m  \x1b[38;5;241m%s/%s tokens (1%%)\x1b[39m\n</local-command-stdout>"
            % (display, model, used, limit)}


class TestContextWindow(unittest.TestCase):
    """上下文窗口（分母）：状态栏明确上限 > /context > 按模型 / [1m] 提示 > 观测值超过 → 1M > 默认 200k（§4.2 meta.stats）。"""

    def snap(self, entries, hints=(), tr=None):
        tr = tr or ST.StatsTracker()
        tr.on_transcript(entries)
        return tr.snapshot(hints)

    def test_user_log_numbers_opus_5_5(self):
        # 用户日志里的真实数字（claude-opus-5-5，CLI 2.1.331 原生 1M）：以前按 200k 显示 98% / 100% / 107% / 110%
        for used, pct in ((196886, 19.7), (200245, 20.0), (214632, 21.5), (220211, 22.0)):
            s = self.snap([asst("claude-opus-5-5", used)])
            self.assertEqual((s["context_used"], s["context_limit"], s["context_pct"], s["context_approx"],
                              s["context_limit_source"], s["context_model"]),
                             (used, 1000000, pct, True, "model", "Opus 5.5"), used)
        # 同一会话依次到达也一样（不受先前的值影响）
        tr = ST.StatsTracker()
        for used, pct in ((196886, 19.7), (200245, 20.0), (214632, 21.5), (220211, 22.0)):
            tr.on_transcript([asst("claude-opus-5-5", used)])
            self.assertEqual(tr.snapshot()["context_pct"], pct)

    def test_model_table(self):
        cases = {
            "claude-haiku-5-5": (1000000, "Haiku 5.5"), "claude-haiku-4-5-20251001": (200000, "Haiku 4.5"),
            "claude-sonnet-5-5": (1000000, "Sonnet 5.5"), "claude-sonnet-5": (1000000, "Sonnet 5"),
            "claude-sonnet-4-6": (200000, "Sonnet 4.6"), "claude-sonnet-4-5-20250929": (200000, "Sonnet 4.5"),
            "claude-sonnet-4-20250514": (200000, "Sonnet 4"), "claude-opus-4-20250514": (200000, "Opus 4"),
            "claude-opus-4-6": (200000, "Opus 4.6"), "claude-opus-4-7": (1000000, "Opus 4.7"),
            "claude-opus-4-8": (1000000, "Opus 4.8"), "claude-opus-5": (1000000, "Opus 5"),
            "claude-fable-5-2": (1000000, "Fable 5.2"), "claude-mythos-5-1": (1000000, "Mythos 5.1"),
            "claude-3-5-haiku-20241022": (200000, "Haiku 3.5"), "claude-3-7-sonnet-20250219": (200000, "Sonnet 3.7"),
            # Bedrock / Vertex 写法
            "us.anthropic.claude-haiku-4-5-20251001-v1:0": (200000, "Haiku 4.5"),
            "us.anthropic.claude-opus-5-5": (1000000, "Opus 5.5"), "claude-3-5-sonnet-v2@20241022": (200000, "Sonnet 3.5"),
            "claude-opus-4-1@20250805": (200000, "Opus 4.1"), "CLAUDE-OPUS-5-5": (1000000, "Opus 5.5"),
            # CLI 的 qv()：目录外但算"已知"的老模型（没有窗口 → 200k）
            "claude-3-opus-20240229": (200000, "Opus 3"), "claude-3-haiku-20240307": (200000, "Haiku 3"),
            "claude-3-sonnet-20240229": (200000, "Sonnet 3"),
        }
        for model, (limit, label) in cases.items():
            s = self.snap([asst(model, 150000)])
            self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_model"]), (limit, "model", label), model)
            self.assertEqual(s["context_pct"], round(150000 * 100.0 / limit, 1), model)
        self.assertIsNone(ST.model_context("gpt-x"))
        self.assertIsNone(ST.norm_model(None))
        self.assertEqual(ST.norm_model("claude-opus-5-5[1m]"), "claude-opus-5-5")

    def test_1m_suffix_and_hints(self):
        # transcript 的 message.model 不带 [1m]；Sonnet 4.6 / Opus 4.6 只有选了 [1m] 才是 1M
        s = self.snap([asst("claude-sonnet-4-6", 150000)], ["claude-sonnet-4-6[1m]"])
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (1000000, "hint", 15.0))
        s = self.snap([asst("claude-opus-4-6", 150000)], ["Opus 4.6 (1M context)"])
        self.assertEqual((s["context_limit"], s["context_limit_source"]), (1000000, "hint"))
        s = self.snap([asst("claude-sonnet-4-6", 150000)], ["Sonnet 4.6", None, "sonnet"])
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (200000, "model", 75.0))
        # 原生 1M 的模型：有没有 [1m] 都是 1M，来源记为按模型
        for hints in ((), ["claude-opus-5-5[1m]"]):
            s = self.snap([asst("claude-opus-5-5", 214632)], hints)
            self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (1000000, "model", 21.5))
        # 不在 CLI 2.1.331 模型目录里的模型（第三方网关的叫法、更新的模型）：与 CLI 一致按 1M
        for model in ("my-gateway-model", "claude-opus-6", "gpt-x", "claude-opus-5-5-preview"):
            s = self.snap([asst(model, 50000)])
            self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"], s["context_model"]),
                             (1000000, "unknown_model", 5.0, model), model)
        # 不在表里但有 [1m] 提示：同样 1M，来源记为提示
        s = self.snap([asst("my-gateway-model", 50000)], ["my-gateway-model[1m]"])
        self.assertEqual((s["context_limit"], s["context_limit_source"]), (1000000, "hint"))
        # 连模型名都没有（usage 条目缺 model）→ 默认 200k
        s = self.snap([{"type": "assistant", "message": {"usage": {"input_tokens": 50000}}}])
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"], s["context_model"]),
                         (200000, "default", 25.0, None))

    def test_context_cmd_overrides_inference(self):
        # /context 说 200k（例如 CLAUDE_CODE_DISABLE_1M_CONTEXT=1）：覆盖模型表的 1M
        s = self.snap([asst("claude-opus-5-5", 150000), ctx_stdout("150k", "200k")])
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"], s["context_model"]),
                         (200000, "context_cmd", 75.0, "Opus 5.5"))
        # /context 说 1M：覆盖 Sonnet 4.6 的 200k（用户开了 1M beta）
        tr = ST.StatsTracker()
        s = self.snap([ctx_stdout("12k", "1m", "claude-sonnet-4-6", "Sonnet 4.6"), asst("claude-sonnet-4-6", 150000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (1000000, "context_cmd", 15.0))
        # 之后换到别的模型：/context 的窗口只属于原来那个模型
        s = self.snap([asst("claude-haiku-4-5", 50000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"]), (200000, "model"))
        # 换回来：仍记得
        s = self.snap([asst("claude-sonnet-4-6", 160000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"]), (1000000, "context_cmd"))
        # Markdown 版（isMeta user 条目）同样有效；transcript reset（/clear）后仍记得（同一个 claude 进程）
        tr = ST.StatsTracker()
        md = {"type": "user", "isMeta": True, "message": {"role": "user", "content":
              "## Context Usage\n\n**Model:** claude-sonnet-4-6[1m]  \n**Tokens:** 46.8k / 1m (5%)\n"}}
        tr.on_transcript([md])
        tr.on_transcript([asst("claude-sonnet-4-6", 300000)], reset=True)
        s = tr.snapshot()
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (1000000, "context_cmd", 30.0))

    def test_context_cmd_gateway_model_name(self):
        # 第三方网关：transcript 的 message.model 与 /context 的模型 id 不同 → 按"运行 /context 时的模型"对上
        # （窗口故意写成 500k，与"不在表里 → 1M"区分开）
        tr = ST.StatsTracker()
        s = self.snap([asst("gw-opus", 150000), ctx_stdout("150k", "500k", "claude-opus-5-5"),
                       asst("gw-opus", 160000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (500000, "context_cmd", 32.0))
        # 不在表里的另一个网关模型：不套用，按 CLI 对未知模型的默认 1M
        s = self.snap([asst("gw-other", 10000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"]), (1000000, "unknown_model"))

    def test_context_cmd_real_fixtures(self):
        for name in ("transcript_context_fullscreen.jsonl", "transcript_context_classic.jsonl"):
            tr = ST.StatsTracker()
            tr.on_transcript(ctx_entries(name), reset=True)
            self.assertIsNone(tr.snapshot(), name)      # 只有 /context、还没有回复：没有已用值
            # 让 Haiku 4.5（表里 200k）冒充：/context 的模型是 claude-haiku-5-5，对不上 → 按表
            tr.on_transcript([asst("claude-haiku-4-5", 100000)])
            self.assertEqual(tr.snapshot()["context_limit"], 200000, name)
            tr.on_transcript([asst("claude-haiku-5-5", 100000)])
            s = tr.snapshot()
            self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_model"]),
                             (1000000, "context_cmd", "Haiku 5.5"), name)

    def test_context_cmd_from_screen(self):
        tr = ST.StatsTracker()
        tr.on_transcript([asst("claude-haiku-5-5", 16000)])
        text = (FIX / "context_cmd" / "screen_fullscreen_200.txt").read_text(encoding="utf-8")
        tr.on_screen(parse_screen(text.replace("16k/1m tokens", "16k/200k tokens")), 1.0)
        s = tr.snapshot()
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (200000, "context_cmd", 8.0))

    def test_observed_over_200k_raises_to_1m(self):
        # 表里 200k 的模型（Haiku 4.5；例如用户开了 [1m] 但我们看不到）
        tr = ST.StatsTracker()
        s = self.snap([asst("claude-haiku-4-5", 150000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_pct"]), (200000, 75.0))
        s = self.snap([asst("claude-haiku-4-5", 214632)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (1000000, "observed", 21.5))
        # 只升不降：压缩后回到 50k 仍按 1M
        s = self.snap([asst("claude-haiku-4-5", 50000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (1000000, "observed", 5.0))
        # 换模型重新判断
        s = self.snap([asst("claude-sonnet-4-6", 50000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (200000, "model", 25.0))
        s = self.snap([asst("claude-sonnet-4-6", 250000)], tr=tr)
        self.assertEqual((s["context_limit"], s["context_limit_source"], s["context_pct"]), (1000000, "observed", 25.0))
        # 子代理（isSidechain）的大上下文不算
        tr2 = ST.StatsTracker()
        s = self.snap([asst("claude-opus-4-5", 100000), asst("claude-opus-4-5", 900000, side=True)], tr=tr2)
        self.assertEqual((s["context_limit"], s["context_pct"]), (200000, 50.0))
        # 永远不超过 100%（推断的分母）
        s = self.snap([asst("my-gateway-model", 1200000)], tr=ST.StatsTracker())
        self.assertEqual((s["context_limit"], s["context_pct"]), (1000000, 100.0))
        # /context 给的窗口也会被更大的观测值顶上去（/context 过时了）
        s = self.snap([ctx_stdout("100k", "200k", "claude-opus-4-6", "Opus 4.6"), asst("claude-opus-4-6", 250000)])
        self.assertEqual((s["context_limit"], s["context_limit_source"]), (1000000, "observed"))

    def test_status_explicit_limit_first(self):
        tr = ST.StatsTracker()
        tr.on_transcript([asst("claude-opus-5-5", 214632), ctx_stdout("214.6k", "1m")])
        tr.on_screen(parse_screen("─" * 80 + "\n❯ \n" + "─" * 80 + "\n  Opus 5.5 | ctx 120k/200k | $1.23\n"
                                  "  ⏵⏵ auto mode on (shift+tab to cycle)"), 1.0)
        s = tr.snapshot()
        self.assertEqual((s["context_used"], s["context_limit"], s["context_pct"], s["context_source"],
                          s["context_approx"], s["context_limit_source"], s["context_model"]),
                         (120000, 200000, 60.0, "status", False, None, None))

    def test_persist_window_source(self):
        tr = ST.StatsTracker()
        tr.on_transcript([asst("claude-opus-5-5", 214632)])
        d = ST.sanitize(dict(tr.snapshot(), updated_at=1.0))
        self.assertEqual((d["context_limit_source"], d["context_model"]), ("model", "Opus 5.5"))
        tr2 = ST.StatsTracker()
        tr2.restore(d)          # app 重启：transcript 到达之前沿用上次的窗口，不会退回 200k 显示 107%
        s = tr2.snapshot()
        self.assertEqual((s["context_limit"], s["context_pct"], s["context_limit_source"], s["context_model"]),
                         (1000000, 21.5, "model", "Opus 5.5"))
        bad = ST.sanitize(dict(d, context_limit_source="evil", context_model="<script>alert(1)</script>"))
        self.assertEqual((bad["context_limit_source"], bad["context_model"]), (None, None))


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
