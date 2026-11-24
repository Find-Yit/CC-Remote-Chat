"""/context 卡片与命令输出的 ANSI（static/ctxcard.js）、统计条上下文窗口来源说明（static/stats.js）：用 node 跑纯函数。
样本是 CLI 2.1.331 真机 transcript（fixtures/transcript_context_*.jsonl，已脱敏）；DOM 部分（卡片、原文上色、对齐）在回放自测页第 14d 节。"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"


def stdout_samples(name):
    out = []
    for l in (FIX / name).read_text(encoding="utf-8").splitlines():
        e = json.loads(l)
        if e.get("type") == "system" and "<local-command-stdout>" in (e.get("content") or ""):
            out.append(e["content"])
    return out


def run_js(body, module="ctxcard.js", **data):
    url = (ROOT / "static" / module).as_uri()
    js = "const m = await import(%s);\nconst D = %s;\n%s" % (json.dumps(url), json.dumps(data), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestAnsi(unittest.TestCase):
    def test_strip_and_colors(self):
        out = run_js(r"""
console.log(JSON.stringify({
  strip: m.stripAnsi('\x1b[1mContext usage\x1b[22m \x1b[38;5;245m⛀\x1b[39m\x1b]0;title\x07!'),
  has: [m.hasAnsi('a\x1b[0m'), m.hasAnsi('plain'), m.hasAnsi(null)],
  c: [0, 7, 8, 15, 16, 69, 127, 196, 214, 215, 231, 232, 241, 245, 255, 256, -1, 'x'].map(m.xterm256),
  lines: m.ansiLines('\x1b[1mA\x1b[22m\x1b[38;5;69mB\nC\x1b[39mD\x1b[2mE\x1b[0mF\x1b[38;2;1;2;3mG\x1b[91mH\x1b[48;5;9mI'),
  tok: ['16k', '1m', '950.4k', '~480', '73', '1.2M', 'x', ''].map(m.parseTokens),
  isCtx: ['/context', '/context all', ' /context ', '/contexts', '/model', null].map(m.isContextCommand),
}));
""")
        self.assertEqual(out["strip"], "Context usage ⛀!")
        self.assertEqual(out["has"], [True, False, False])
        self.assertEqual(out["c"], ["#000000", "#e5e5e5", "#666666", "#ffffff", "#000000", "#5f87ff", "#af00af", "#ff0000",
                                    "#ffaf00", "#ffaf5f", "#ffffff", "#080808", "#626262", "#8a8a8a", "#eeeeee",
                                    None, None, None])
        seg = lambda t, fg=None, b=False, d=False: {"text": t, "fg": fg, "bold": b, "dim": d}
        self.assertEqual(out["lines"], [[seg("A", b=True), seg("B", "#5f87ff")],
                                        [seg("C", "#5f87ff"), seg("D"), seg("E", d=True), seg("F"), seg("G", "#010203"),
                                         seg("HI", "#f14c4c")]])      # 同样式相邻段合并；背景色不显示
        self.assertEqual(out["tok"], [16000, 1000000, 950400, 480, 73, 1200000, None, None])
        self.assertEqual(out["isCtx"], [True, True, True, False, False, False])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestContextParse(unittest.TestCase):
    def parse(self, texts):
        return run_js("console.log(JSON.stringify(D.t.map((x) => m.parseContextOutput(x))));", t=texts)

    def test_fullscreen_sample(self):
        a, b = self.parse(stdout_samples("transcript_context_fullscreen.jsonl"))
        for r, used in ((a, 16000), (b, 16600)):
            self.assertEqual((r["model"], r["modelId"], r["used"], r["limit"], r["limitText"], r["pctText"]),
                             ("Haiku 5.5", "claude-haiku-5-5", used, 1000000, "1m", "2%"))
            self.assertEqual(r["gridRows"], 10)
            self.assertEqual(r["autoCompact"], "1m tokens")
            self.assertEqual(r["footer"], "/context all to expand")
        self.assertEqual([(c["name"], c["kind"], c["tokensText"], c["tokens"], c["pctText"], c["color"]) for c in a["cats"]], [
            ("System prompt", "used", "3.1k", 3100, "0.3%", "#8a8a8a"),
            ("System tools", "used", "10.1k", 10100, "1%", "#626262"),
            ("Custom agents", "used", "73", 73, "<0.1%", "#5f87ff"),
            ("Memory files", "used", "130", 130, "<0.1%", "#ffaf5f"),
            ("Skills", "used", "2.6k", 2600, "0.3%", "#ffaf00"),
            ("Messages", "used", "10", 10, "<0.1%", "#af00af"),
            ("Free space", "free", "951k", 951000, "95%", None),
            ("Autocompact buffer", "buffer", "33k", 33000, "3%", None),
        ])
        self.assertEqual(a["sections"], [
            {"title": "Custom agents", "note": ".claude/agents/", "groups": [{"label": None, "items": ["1 agent · 73 tokens"]}]},
            {"title": "Memory files", "note": "/memory", "groups": [{"label": None, "items": ["1 file · 130 tokens"]}]},
            {"title": "Skills", "note": "/skills", "groups": [{"label": None, "items": ["18 skills · 2.6k tokens"]}]},
        ])

    def test_classic_expanded_sample(self):
        (r,) = self.parse(stdout_samples("transcript_context_classic.jsonl"))
        self.assertEqual((r["used"], r["limit"], len(r["cats"]), r["footer"]), (16000, 1000000, 8, None))
        secs = {s["title"]: s for s in r["sections"]}
        self.assertEqual(list(secs), ["Custom agents", "Memory files", "Skills"])
        self.assertEqual(secs["Custom agents"]["groups"], [{"label": "Plugin", "items": ["code-simplifier:code-simplifier: 73 tokens"]}])
        self.assertEqual(secs["Memory files"]["groups"], [{"label": None, "items": ["~/.claude/CLAUDE.md: 130 tokens"]}])
        self.assertEqual([(g["label"], len(g["items"])) for g in secs["Skills"]["groups"]],
                         [("User", 3), ("Plugin (claude-hud)", 2), ("Built-in", 13)])

    def test_narrow_wrapped_and_plain(self):
        # 窄终端：右列折到网格下方、分类行折行；没有 ANSI 也能解析（按空行分节）
        narrow = ("Context usage\n⛁ ⛀ ⛶ ⛶ ⛶\n⛶ ⛶ ⛶ ⛶ ⛝\nOpus 5.5\nclaude-opus-5-5\n214.6k/1m tokens (21%)\n \n"
                  "Estimated usage by category\n⛁ System prompt: 3.1k tokens (0.3%)\n⛁ Messages: 200k tokens\n(20%)\n"
                  "⛶ Free space: 752.4k tokens (75%)\n⛝ Autocompact buffer: 33k\ntokens (3%)\n\n"
                  "Auto-compact window:  1m tokens\n\nMCP tools · /mcp (loaded on-demand)\n└ 33 tools\n")
        (r,) = self.parse([narrow])
        self.assertEqual((r["model"], r["modelId"], r["used"], r["pctText"]), ("Opus 5.5", "claude-opus-5-5", 214600, "21%"))
        self.assertEqual([(c["name"], c["tokens"], c["pctText"]) for c in r["cats"]],
                         [("System prompt", 3100, "0.3%"), ("Messages", 200000, "20%"), ("Free space", 752400, "75%"),
                          ("Autocompact buffer", 33000, "3%")])
        self.assertEqual(r["sections"], [{"title": "MCP tools", "note": "/mcp (loaded on-demand)",
                                          "groups": [{"label": None, "items": ["33 tools"]}]}])

    def test_not_context(self):
        out = self.parse([None, "", "Set model to Opus 5.5", "Context usage", "Context usage\n⛁ ⛶  foo\nno totals",
                          "16k/1m tokens (2%)", "\x1b[1mContext usage\x1b[22m\n\x1b[38;5;241m⛶ ⛶\x1b[39m"])
        self.assertEqual(out, [None] * 7)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestStatsWindowTip(unittest.TestCase):
    def test_tooltip_window_source(self):
        out = run_js(r"""
const base = { context_used: 214632, context_limit: 1000000, context_pct: 21.5, context_source: 'transcript', context_approx: true };
const tip = (x) => m.describeStats({ ...base, ...x }).ctx;
console.log(JSON.stringify({
  model: tip({ context_limit_source: 'model', context_model: 'Opus 5.5' }),
  cmd: tip({ context_limit_source: 'context_cmd', context_model: 'Haiku 5.5' }).tip,
  hint: tip({ context_limit_source: 'hint' }).tip,
  obs: tip({ context_limit_source: 'observed', context_model: 'my-gw' }).tip,
  unk: tip({ context_used: 50000, context_limit: 1000000, context_pct: 5, context_limit_source: 'unknown_model', context_model: 'my-gw' }),
  def2: tip({ context_used: 50000, context_limit: 200000, context_pct: 25, context_limit_source: 'default' }).tip,
  old: tip({}).tip,
  status: m.describeStats({ context_used: 25000, context_limit: 1000000, context_pct: 3, context_source: 'status' }).ctx.tip,
}));
""", module="stats.js")
        self.assertEqual(out["model"]["text"], "约 215k / 1M · 21%")   # 21.46%：不因后端 21.5 二次进位成 22%
        self.assertEqual(out["model"]["pctText"], "21%")
        self.assertTrue(out["model"]["approx"])
        self.assertEqual(out["model"]["tip"].split("\n")[:3], [
            "上下文已用 约 21% · 215k / 1M",
            "上下文（约）：214,632 tokens / 1,000,000（21%）",
            "窗口 1M：按模型 Opus 5.5 推断（Claude Code 2.1.331 内置模型目录）"])
        self.assertIn("窗口 1M：来自 /context（Haiku 5.5）", out["cmd"])
        self.assertIn("窗口 1M：模型名 / 状态栏 / /model 面板里有 [1m] 或 1M", out["hint"])
        self.assertIn("窗口 1M：本会话 my-gw 的上下文曾超过 200k，按 1M 计", out["obs"])
        self.assertEqual(out["unk"]["tip"].split("\n")[2], "窗口 1M：模型 my-gw 不在 Claude Code 2.1.331 内置模型目录里，"
                                                            "按 Claude Code 的默认 1M 计；运行一次 /context 可得到准确值")
        self.assertEqual(out["unk"]["pctText"], "5%")
        self.assertIn("窗口 200k：默认估算；运行一次 /context", out["def2"])
        self.assertIn("窗口 1M：按模型推断", out["old"])         # 旧版持久化的统计没有来源字段
        self.assertNotIn("窗口", out["status"])
        self.assertIn("来自 Claude Code 状态栏", out["status"])


if __name__ == "__main__":
    unittest.main()
