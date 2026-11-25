#!/usr/bin/env python3
"""假的交互式 Claude Code（docs/SPEC_V2_TUI.md 第 5 节），只用于本地 fake 模式与测试。

在 tmux 里运行，tty raw 模式读键盘，直接用 ANSI 光标定位画整屏；画面文字尽量逐字复刻
fixtures/tui_screens_*.ndjson（真实 Claude Code v2.1.330 的画面）。

关键词（消息里包含即触发）：
  /tool  Bash 权限框           /write  Write 权限框         /ask  AskUserQuestion
  /long  慢速长输出（可 Esc 中断） /md   markdown 输出
  /html  （可视化演示）在 cwd/ccr-demo/ 下 Write 网页（index.html + style.css + app.js + logo.png）、Edit 一处、
         Bash 假装起 http.server；toolUseResult 带 structuredPatch（只写 HOME 以内的演示文件）
  /shot  （可视化演示）MCP 截图（tool_result = [text, image, text]）、Bash 保存截图到 ccr-demo/shot.png、
         Read 该图片（toolUseResult.file.base64 重复一份），回复里用 markdown 图片引用它
  /bg    运行一个较慢的 Bash 命令，下方显示 "(ctrl+b ctrl+b (twice) to run in background)"；Ctrl+B 转入后台
         （"Ran 1 shell command"，底栏 "· 1 shell"，结束行 "… · 1 shell still running"）
忙时提交的消息进入队列：显示连续的 "❯ <消息>" + 一行共用的 "⎿  Queued · ctrl+x ctrl+s to send now"，输入框占位
"Press up to edit queued messages"；Ctrl+X Ctrl+S（1.5 秒内）= 中断当前生成并立即发送排队的消息。
  /agent 派一个"后台"子代理：写 Agent tool_use、"Async agent launched…agentId: …" 结果、
         <transcript 目录>/<sessionId>/subagents/agent-<id>.meta.json 与逐步写入的 agent-<id>.jsonl
         （几次工具调用，夹杂 api-request*/attachment 行），结束时 meta.finished=true 并写 <task-notification>；
         屏幕上显示与真实 CLI 相同的子代理列表行（Esc 中断 → 子代理以 killed 结束）；子代理运行时
         Ctrl+X Ctrl+K 按两次（3 秒内）= 仿 CLI 2.1.332 的"停止后台子代理"：meta 写 stoppedByUser:true（没有 finished）、
         子代理 jsonl 末尾写 "[Request interrupted by user for tool use]"、主 transcript 写 system agents_killed 与
         纯文本通知 'Background agent "…" was stopped by the user.'（没有 <task-notification>）
  /agentstop 派一个后台子代理，跑几步后主会话调用 TaskStop 停掉它（仿 CLI 2.1.332：子代理只多一条中断标记、
         meta 不变；killed 通知在回复中途被吸收，只写成 attachment/queued_command 条目，不写 user 条目）
整条输入为以下命令时：/model 模型面板、/effort effort 面板、/clear 新会话、/exit 退出、
  /context 上下文用量（仿 CLI 2.1.331：屏幕上内联画方块网格 + 右列，transcript 写带 ANSI 的 <local-command-stdout>
  与 isMeta 的 Markdown 版；窗口默认 1,000,000，CCR_FAKE_CTX_WINDOW 可改）。
Shift+Tab 循环权限模式；忙时 Esc / Ctrl-C 中断。
--settings <文件>：读取其中 env.ANTHROPIC_AUTH_TOKEN，在启动横幅下显示 key 的掩码（验证 API key 选择生效）。

写 $HOME/.claude/sessions/<pid>.json 与 $HOME/.claude/projects/<slug>/<sessionId>.jsonl。
绝不执行任何命令、绝不写 HOME 以外的文件（权限框选 Yes 也只是假装执行）。

环境变量（均可选）：CCR_FAKE_TRUST=1 强制显示信任框；CCR_FAKE_DELAY 每行流式输出间隔（秒，默认 0.3）；
CCR_FAKE_THINK 回复前"思考"时长（默认 1.0）；CCR_FAKE_MODEL 默认模型（haiku）；
CCR_FAKE_EFFORT 默认 effort（low）；CCR_FAKE_MODE 默认权限模式（manual）；
CCR_FAKE_STATUSLINE=0 不画状态栏（模拟用户没配 statusline，测试统计条的 transcript 后备）；
CCR_FAKE_BOOT 出第一帧前的等待秒数（模拟启动慢，观察重启/续接进度）。
"""
import base64
import datetime
import difflib
import getpass
import json
import os
import re
import select
import signal
import socket
import sys
import time
import struct
import unicodedata
import uuid
import zlib

VERSION = "2.1.330"

# name：面板里的名字；display：状态栏/提示里的名字
MODELS = [
    {"name": "Default (recommended)", "display": "Opus 5.5", "id": "default",
     "desc": "Use the default model (currently Opus 5.5) · $5/$25 per Mtok", "aliases": ("default",)},
    {"name": "Opus 5.5", "display": "Opus 5.5", "id": "claude-opus-5-5",
     "desc": "Opus 5.5 · Most capable for complex work · $5/$25 per Mtok", "aliases": ("opus",)},
    {"name": "Fable 5.1", "display": "Fable 5.1", "id": "claude-fable-5-1[1m]",
     "desc": "Custom Fable model (claude-fable-5-1[1m])", "aliases": ("fable",)},
    {"name": "Sonnet", "display": "Sonnet 5.5", "id": "claude-sonnet-5-5",
     "desc": "Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok", "aliases": ("sonnet",)},
    {"name": "Haiku", "display": "Haiku 5.5", "id": "claude-haiku-5-5",
     "desc": "Haiku 5.5 · Fastest for quick answers · $0.10/$0.50 per Mtok ($0.50/$2.50 for prompts over 100k)",
     "aliases": ("haiku",)},
    {"name": "Fable 5.2 (1M)", "display": "Fable 5.2", "id": "claude-fable-5-2[1m]",
     "desc": "claude-fable-5-2，1M 上下文", "aliases": ()},
]
MODEL_WINDOW = 3  # 实测可视窗口：3 个模型
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# 实测（v2.1.330 /model 面板）："○ Low" "● High" "◉ xHigh" "◈ Max"（xHigh 大小写混合）
EFFORT_LABEL = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "xHigh", "max": "Max"}
EFFORT_ICON = {"low": "○", "medium": "◐", "high": "●", "xhigh": "◉", "max": "◈"}
# 实测：effort 到 max 时面板多一行警告；/effort 面板的提示行变成只有"Enter = 仅本会话"（没有 s、不能保存）
MAX_WARNING = ("May use excessive tokens resulting in long response times or overthinking. "
               "Use sparingly for the hardest tasks.")
EFFORT_HINT = "←/→ to adjust · s to use this session only · Enter to save · Esc to cancel"
EFFORT_HINT_MAX = "←/→ to adjust · Enter to use this session only · Esc to cancel"
DEFAULT_EFFORT = "medium"
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
MODE_TEXT = {
    "manual": "⏸ manual mode on",
    "auto": "⏵⏵ auto mode on (shift+tab to cycle)",
    "acceptEdits": "⏵⏵ accept edits on (shift+tab to cycle)",
    "plan": "⏸ plan mode on (shift+tab to cycle)",
    "bypassPermissions": "⏵⏵ bypass permissions on (shift+tab to cycle)",
    "dontAsk": "⏵⏵ don't ask on (shift+tab to cycle)",
}
SPINNER = "·✢✳✶✻✽✻✶✳✢"
VERBS = ["Schlepping", "Wandering", "Sketching", "Pondering", "Brewing"]
TIPS = ["Tip: Use /theme to change the color theme", 'Tip: Say "fan out subagents" to run work in parallel']
COMMANDS = [
    ("/model", "Switch model (currently {model})"),
    ("/auto-mode-setup", "Teach auto mode about your environment"),
    ("/code-review", "Review the current diff or a PR for bugs"),
    ("/effort", "Set the model's effort level"),
    ("/context", "Show what fills the context"),
    ("/clear", "Clear conversation history and free up context"),
    ("/exit", "Exit the CLI"),
]
NB = "\xa0"
RES = "  ⎿ " + NB  # 工具结果/本地命令输出行前缀（实测 ⎿ 后是空格 + NBSP）
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

# /bg：仿真机实测画面（fixtures/tui_screens_keys.ndjson 的 bg_queued）
BG_DESC = "Running 45-second counting loop with progress output"
BG_CMD = ["python3 -c \"import time", "for i in range(45): print(i, flush=True); time.sleep(1)",
          "print('BG-DONE')\""]
BG_HINT = "(ctrl+b ctrl+b (twice) to run in background)"
BG_STEPS = 45
BG_SHELL_SECONDS = 45.0      # 转入后台的 shell 显示为"运行中"的时长
QUEUED_HINT = "Queued · ctrl+x ctrl+s to send now"
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
CHORD_WINDOW = 1.5
KILL_CONFIRM_WINDOW = 3.0    # 真机：Ctrl+X Ctrl+K 两次之间最多 3 秒
KILL_HINT = "Press ctrl+x ctrl+k again to stop background agents"

PROBE_CMD = "touch /tmp/ccr_fake_probe"
PROBE_DESC = "Create probe file in /tmp"
WRITE_FILE = ".ccr_fake_write.txt"
ASK_Q = {"question": "你更喜欢红色还是蓝色？", "header": "颜色偏好",
         "options": [{"label": "红色", "description": "选择红色"}, {"label": "蓝色", "description": "选择蓝色"}],
         "multiSelect": False}
MD_TEXT = ("## Markdown 示例\n\n这是一段 **粗体** 和 `行内代码`。\n\n- 第一项\n- 第二项\n\n"
           "```python\nprint(\"hello\")\n```\n\n| 列A | 列B |\n|---|---|\n| 1 | 2 |")
AGENT_DESC = "fake 子代理：检查项目"
AGENT_PROMPT = "列出当前目录的文件，读取 README.md，再搜索 TODO，最后用一句话总结（fake 子代理任务，不会真的执行）。"
AGENT_LAUNCHED = (
    "Async agent launched successfully. (This tool result is internal metadata — never quote or paste any part of "
    "it, including the agentId below, into a user-facing reply.)\nagentId: %s (internal ID - do not mention to "
    "user. Use SendMessage with to: '%s', summary: '<5-10 word recap>' to continue this agent.)\nThe agent is "
    "working in the background. You will be notified automatically when it completes.\noutput_file: %s")
AGENT_NOTE = (
    "<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>%s</output-file>\n"
    "<status>%s</status>\n<summary>Agent \"%s\" %s</summary>\n<note>A task-notification fires each time this agent "
    "stops with no live background children of its own.</note>\n%s<usage><subagent_tokens>%d</subagent_tokens>"
    "<tool_uses>%d</tool_uses><duration_ms>%d</duration_ms></usage>\n</task-notification>")
TASKSTOP_RESULT = '{"message":"Successfully stopped task: %s (%s)","task_id":"%s","task_type":"local_agent","command":"%s"}'
REJECT_TEXT = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a "
               "file edit, the new_string was NOT written to the file). STOP what you are doing and wait for the "
               "user to tell you how to proceed.")


# ------------------------------------------------------------------ 显示宽度 / 折行
def cwidth(c):
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def dwidth(s):
    return sum(cwidth(c) for c in s)


def clip(s, width):
    out, w = [], 0
    for c in s:
        cw = cwidth(c)
        if w + cw > width:
            break
        out.append(c)
        w += cw
    return "".join(out)


def wrap(text, width, first="", cont=""):
    """按显示宽度折行（英文按词、CJK 按字），返回带前缀的行列表。"""
    width = max(10, width)
    lines, cur, prefix = [], "", first
    avail = width - dwidth(prefix)
    for tok in re.findall(r"[ \t]+|[\x21-\x7e]+|.", text):
        if dwidth(cur) + dwidth(tok) <= avail:
            cur += tok
            continue
        if tok.strip() == "":  # 行尾空白：直接折行
            lines.append(prefix + cur)
            cur, prefix = "", cont
            avail = width - dwidth(prefix)
            continue
        if cur:
            lines.append(prefix + cur)
            cur, prefix = "", cont
            avail = width - dwidth(prefix)
        while dwidth(tok) > avail:
            part = clip(tok, avail)
            lines.append(prefix + part)
            tok = tok[len(part):]
            prefix = cont
            avail = width - dwidth(prefix)
        cur = tok
    lines.append(prefix + cur)
    return lines


def fmt_tokens(n):
    """与真实 spinner 相同：< 1000 原样，否则 1.2k / 12.3k。"""
    return str(n) if n < 1000 else "%.1fk" % (n / 1000.0)


def now_iso():
    t = datetime.datetime.now(datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def clock():
    return time.strftime("%I:%M %p").lstrip("0")


# ------------------------------------------------------------------ 对话框对象
class Dialog(object):
    def __init__(self, kind, **kw):
        self.kind = kind
        self.cursor = 0
        self.__dict__.update(kw)


# ------------------------------------------------------------------ 子代理（只写 HOME 下的 subagents 目录）
class FakeSubagent(object):
    """仿真实 CLI v2.1.330 的子代理文件：agent-<id>.meta.json + agent-<id>.jsonl（isSidechain/agentId）。"""

    def __init__(self, app, desc, prompt, tool_use_id, agent_type="general-purpose", model="haiku"):
        self.app = app
        self.aid = "a" + uuid.uuid4().hex[:16]
        self.desc = desc
        self.agent_type = agent_type
        self.started = time.time()
        self.tool_uses = 0
        self.tokens = 0
        self.result = None
        self.last_uuid = None
        self.done = False
        self.stopped = None   # 被停止："user"（Ctrl+X Ctrl+K）/ "claude"（TaskStop）
        self.dir = os.path.join(os.path.dirname(app.tpath), app.session_id, "subagents")
        self.jsonl = os.path.join(self.dir, "agent-%s.jsonl" % self.aid)
        self.metap = os.path.join(self.dir, "agent-%s.meta.json" % self.aid)
        self.output_file = os.path.join("/tmp", "claude-fake", app.session_id, "tasks", self.aid + ".output")
        self.meta = {"agentType": agent_type, "isBuiltIn": True, "description": desc, "toolUseId": tool_use_id,
                     "spawnDepth": 1, "requestShape": "background", "requestNonInteractive": True,
                     "model": model, "pickedEffortModel": app.model_id(), "finished": False}
        os.makedirs(self.dir, exist_ok=True)
        self.write_meta()
        self.entry("user", {"role": "user", "content": prompt}, {"promptId": str(uuid.uuid4())})
        for att in ({"type": "deferred_tools_delta", "addedNames": ["Monitor", "NotebookEdit", "WebFetch"]},
                    {"type": "skill_listing", "content": "- fake-skill: " + "这一段很长的技能清单不应被转发。" * 400}):
            e = self.base("attachment")
            e["attachment"] = att
            self.raw(e)

    def base(self, typ):
        return {"parentUuid": self.last_uuid, "isSidechain": True, "agentId": self.aid, "type": typ,
                "uuid": str(uuid.uuid4()), "timestamp": now_iso(), "userType": "external", "entrypoint": "cli",
                "cwd": self.app.cwd, "sessionId": self.app.session_id, "version": VERSION, "gitBranch": "HEAD"}

    def raw(self, obj, split=False):
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with open(self.jsonl, "ab") as f:
            if split and len(data) > 20:  # 模拟分两次写入（agent 只能转发完整行）
                f.write(data[:len(data) // 2])
                f.flush()
                time.sleep(0.03)
                f.write(data[len(data) // 2:])
            else:
                f.write(data)

    def entry(self, typ, message, extra=None, split=False):
        e = self.base(typ)
        e["message"] = message
        if extra:
            e.update(extra)
        self.raw(e, split=split)
        self.last_uuid = e["uuid"]
        return e

    def write_meta(self):
        tmp = self.metap + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.metap)

    def api_noise(self):
        sid = self.app.session_id
        self.raw({"type": "api-request-shape", "sessionId": sid, "agentId": self.aid, "timestamp": now_iso(),
                  "version": VERSION, "shape": {"system": [{"type": "text", "text": "You are an agent… " * 300}]}})
        self.raw({"type": "api-request-blob", "sessionId": sid, "agentId": self.aid, "hash": uuid.uuid4().hex,
                  "message": {"role": "system", "content": [{"type": "text", "text": "# Environment " * 300}]}})
        self.raw({"type": "api-request", "sessionId": sid, "agentId": self.aid, "id": str(uuid.uuid4()),
                  "timestamp": now_iso(), "version": VERSION, "querySource": "agent:builtin:" + self.agent_type})

    def assistant(self, content, stop="end_turn"):
        self.tokens += 120
        msg = {"model": self.app.model_id(), "id": "msg_fakesub" + uuid.uuid4().hex[:16], "type": "message",
               "role": "assistant", "content": content, "stop_reason": stop, "stop_sequence": None,
               "usage": {"input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                         "output_tokens": 40}}
        return self.entry("assistant", msg, {"attributionAgent": self.agent_type, "effort": self.app.effort},
                          split=True)

    def tool_use(self, name, inp):
        tid = "toolu_fakesub" + uuid.uuid4().hex[:12]
        self.tool_uses += 1
        self.assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}], stop="tool_use")
        return tid

    def tool_result(self, tid, text, is_error=False):
        c = [{"tool_use_id": tid, "type": "tool_result", "content": text, "is_error": is_error}]
        self.entry("user", {"role": "user", "content": c}, {"promptId": str(uuid.uuid4())})

    def script(self):
        """逐步写入（每步之后 yield 等待秒数）。"""
        d = max(self.app.delay, 0.02)
        cwd = self.app.cwd
        self.api_noise()
        yield d
        tid = self.tool_use("Bash", {"command": "ls -la", "description": "列出当前目录"})
        yield d * 2
        self.tool_result(tid, "total 8\n-rw-r--r-- 1 u u 120 README.md\n-rw-r--r-- 1 u u  40 main.py\n"
                              "（fake：命令没有真正执行）")
        self.api_noise()
        yield d
        tid = self.tool_use("Read", {"file_path": os.path.join(cwd, "README.md")})
        yield d * 2
        # 超长工具结果：node agent 应截断（> 20000 字符）
        self.tool_result(tid, "     1\t# fake 项目\n" + "".join("%6d\t这是 README 的第 %d 行（fake）。\n" % (i, i)
                                                             for i in range(2, 1500)))
        yield d
        tid = self.tool_use("Grep", {"pattern": "TODO", "path": cwd})
        yield d * 2
        self.tool_result(tid, "No matches found")
        yield d
        self.result = "检查完毕：项目里有 README.md 和 main.py，没有发现 TODO（fake 子代理的总结）。"
        self.assistant([{"type": "text", "text": self.result}])
        e = self.base("attachment")
        e["attachment"] = {"type": "prompt_snapshot", "systemPrompt": ["You are an agent for Claude Code… " * 200]}
        self.raw(e)
        yield d

    def finish(self):
        if not self.done:
            self.done = True
            self.meta["finished"] = True
            self.write_meta()

    def stop(self, by):
        """仿 CLI 2.1.332 停止子代理：不写 finished；用户停止时 meta 加 stoppedByUser；jsonl 末尾写中断标记。"""
        if self.done or self.stopped:
            return
        self.stopped = by
        if by == "user":
            self.meta["stoppedByUser"] = True
            self.write_meta()
        self.entry("user", {"role": "user", "content": [{"type": "text",
                                                         "text": "[Request interrupted by user for tool use]"}]},
                   {"promptId": str(uuid.uuid4()), "rowSource": "harness"})

    def elapsed(self):
        return max(0, int(time.time() - self.started))


# ------------------------------------------------------------------ 主程序
# ------------------------------------------------------------------ 可视化演示的数据（/html、/shot）
DEMO_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <title>CCR 演示页</title>
    <link rel="stylesheet" href="style.css">
  </head>
  <body>
    <header>
      <img src="logo.png" alt="logo" width="48" height="48">
      <h1>CCR 演示页</h1>
    </header>
    <main>
      <p>这是 fake claude 写的网页，用来测试右侧预览面板与“网页预览”卡片。</p>
      <button id="btn">点我</button>
      <p id="out"></p>
    </main>
    <script src="app.js"></script>
  </body>
</html>
"""
DEMO_CSS = """body { font-family: -apple-system, sans-serif; margin: 0; background: #f4f6fb; color: #1d1f23; }
header { display: flex; align-items: center; gap: 12px; padding: 16px 24px; background: #2f6bf0; color: #fff; }
header h1 { font-size: 20px; margin: 0; }
.sub { margin: 0; opacity: .85; font-size: 13px; }
main { padding: 24px; }
button { padding: 8px 16px; border-radius: 8px; border: 0; background: #d97757; color: #fff; font-size: 14px; }
"""
DEMO_JS = """document.getElementById('btn').addEventListener('click', () => {
  const n = (window.__n = (window.__n || 0) + 1);
  document.getElementById('out').textContent = '点击了 ' + n + ' 次（脚本在沙箱 iframe 里运行）';
});
"""


def structured_patch(old, new, context=3):
    """仿 Claude Code 的 toolUseResult.structuredPatch：[{oldStart, oldLines, newStart, newLines, lines}]。"""
    out = []
    a, b = old.splitlines(), new.splitlines()
    for line in difflib.unified_diff(a, b, lineterm="", n=context):
        if line.startswith("---") or line.startswith("+++"):
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            out.append({"oldStart": int(m.group(1)), "oldLines": int(m.group(2) or 1),
                        "newStart": int(m.group(3)), "newLines": int(m.group(4) or 1), "lines": []})
        elif out:
            out[-1]["lines"].append(line)
    return out


def fake_png(w, h, seed=1, style="page"):
    """纯标准库生成一张示意 PNG（假"网页截图"/图标），背景带一点噪声让体积接近真实截图。"""
    rows = []
    x0 = (seed * 2654435761) & 0xffffffff
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            x0 = (x0 * 1103515245 + 12345) & 0x7fffffff
            n = (x0 >> 16) % 9 - 4
            if style == "logo":
                cx, cy = x - w / 2.0, y - h / 2.0
                r2 = cx * cx + cy * cy
                if r2 < (w * 0.42) ** 2:
                    c = (217 + n, 119 + n, 87 + n) if r2 > (w * 0.22) ** 2 else (255, 244, 238)
                else:
                    c = (255, 255, 255)
            elif y < h * 0.12:
                c = (47, 107, 240)
            elif 0.2 * h < y < 0.55 * h and 0.06 * w < x < 0.46 * w:
                c = (255 + min(0, n), 255 + min(0, n), 255 + min(0, n)) if not (0.3 * h < y < 0.34 * h) else (60, 64, 72)
            elif 0.2 * h < y < 0.55 * h and 0.54 * w < x < 0.94 * w:
                c = (217, 119 + n, 87)
            elif 0.65 * h < y < 0.72 * h and 0.06 * w < x < 0.7 * w:
                c = (180, 186, 196)
            else:
                c = (240 + n, 243 + n, 248 + n)
            row += bytes(max(0, min(255, v)) for v in c)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class FakeClaude(object):
    def __init__(self, argv, env=None, size=None, out=None, cwd=None):
        env = os.environ if env is None else env
        self.env = env
        self.out = out
        self.fixed_size = size
        self.pid = os.getpid()
        self.home = env.get("HOME") or os.path.expanduser("~")
        self.cwd = os.path.realpath(cwd) if cwd else os.getcwd()
        self.user = env.get("USER") or self._getuser()
        self.host = socket.gethostname().split(".")[0]
        self.delay = self._float("CCR_FAKE_DELAY", 0.3)
        self.think = self._float("CCR_FAKE_THINK", 1.0)
        self.model = env.get("CCR_FAKE_MODEL") or "haiku"
        self.effort = env.get("CCR_FAKE_EFFORT") or "low"
        self.mode = env.get("CCR_FAKE_MODE") or "manual"
        self.show_status = env.get("CCR_FAKE_STATUSLINE") != "0"
        self.resume = None
        self.api_key_mask = None   # --settings 文件里 env.ANTHROPIC_AUTH_TOKEN 的掩码
        self.bypass_allowed = False
        self.parse_args(argv)
        if self.mode == "bypassPermissions":
            self.bypass_allowed = True
        self.session_id = self.resume or str(uuid.uuid4())
        self.started_ms = int(time.time() * 1000)
        self.content = []  # [(first_prefix, cont_prefix, text)]
        self.input = ""
        self.popup_cursor = 0
        self.popup_hidden = False
        self.dialog = None
        self.turn = None
        self.turn_due = 0.0
        self.turn_send = None
        self.turn_started = 0.0
        self.turn_verb = VERBS[0]
        self.turn_tokens = 0
        self.turn_tip = None
        self.spinner_on = False
        self.queue = []
        self.bg_running = False   # /bg 的命令正在前台运行（屏幕上有 ctrl+b 提示）
        self.bg_flag = False      # 用户按了 Ctrl+B：转入后台
        self.bg_until = []        # 转入后台的 shell 的结束时间（底栏 "N shell"）
        self.cx_at = 0.0          # Ctrl+X 的时间（Ctrl+X Ctrl+S / Ctrl+X Ctrl+K 和弦）
        self.kill_at = 0.0        # 第一次 Ctrl+X Ctrl+K 的时间（再按一次才停止后台子代理）
        self.agents = {}  # 运行中的子代理 agent_id -> FakeSubagent（屏幕底部列表）
        self.status = None
        self.ctx_tokens = 0
        self.cost = 0.0
        self.last_uuid = None
        self.prompt_index = 0
        self.ctrl_c_at = 0.0
        self.exit_code = None
        self.dirty = True
        self.last_frame = None
        self.tpath = self._transcript_path()
        self.header()
        if self.resume:
            self.load_resume()
        trust = env.get("CCR_FAKE_TRUST") == "1" or os.path.realpath(self.cwd) == os.path.realpath(self.home)
        if trust:
            self.dialog = Dialog("trust")
        self.update_status()

    # ---------------------------------------------------------- 参数
    def _float(self, key, default):
        try:
            return max(0.0, float(self.env.get(key, default)))
        except ValueError:
            return default

    @staticmethod
    def _getuser():
        try:
            return getpass.getuser()
        except Exception:
            return "user"

    def parse_args(self, argv):
        i = 0
        while i < len(argv):
            a = argv[i]
            if a == "--dangerously-skip-permissions":
                self.bypass_allowed = True
                self.mode = "bypassPermissions"
                i += 1
                continue
            if a in ("--resume", "--model", "--permission-mode", "--effort", "--settings") and i + 1 < len(argv):
                v = argv[i + 1]
                if a == "--settings":
                    self.load_settings(v)
                elif a == "--resume":
                    if not UUID_RE.match(v):
                        self.die("--resume 需要会话 UUID：%s" % v)
                    self.resume = v.lower()
                elif a == "--model":
                    self.model = v
                elif a == "--permission-mode":
                    if v == "default":
                        v = "manual"
                    if v not in PERMISSION_MODES:
                        self.die("error: option '--permission-mode <mode>' argument '%s' is invalid." % v)
                    self.mode = v
                else:
                    if v not in EFFORTS:
                        self.die("error: option '--effort <level>' argument '%s' is invalid." % v)
                    self.effort = v
                i += 2
                continue
            self.die("error: unknown option '%s'" % a)

    def load_settings(self, path):
        """--settings <文件>：只认 env.ANTHROPIC_AUTH_TOKEN（与 node_agent 写的 key 文件一致），
        启动横幅下多显示一行 key 的掩码，供端到端测试确认 claude 拿到了 --settings。"""
        try:
            with open(path, "rb") as f:
                data = json.loads(f.read(65536).decode("utf-8"))
        except (OSError, ValueError):
            self.die("error: settings file not found or invalid: %s" % path)
        env = (data.get("env") or {}) if isinstance(data, dict) else {}
        tok = env.get("ANTHROPIC_AUTH_TOKEN") if isinstance(env, dict) else None
        if not isinstance(tok, str) or len(tok) < 8:
            self.die("error: settings file has no env.ANTHROPIC_AUTH_TOKEN: %s" % path)
        head, tail = (7, 4) if len(tok) >= 24 else (max(1, len(tok) // 4), max(1, len(tok) // 8))
        self.api_key_mask = tok[:head] + "…" + tok[-tail:]
        base = env.get("ANTHROPIC_BASE_URL")
        if isinstance(base, str) and base:
            self.api_key_mask += " @ " + base[:120]

    @staticmethod
    def die(msg):
        sys.stderr.write(msg + "\n")
        sys.exit(1)

    # ---------------------------------------------------------- 模型
    def model_index(self, m=None):
        m = (self.model if m is None else m).lower()
        for i, info in enumerate(MODELS):
            if m == info["id"].lower() or m == info["name"].lower() or m in info["aliases"]:
                return i
        return None

    def model_display(self):
        i = self.model_index()
        return MODELS[i]["display"] if i is not None else self.model

    def model_id(self):
        i = self.model_index()
        return MODELS[i]["id"] if i is not None else self.model

    # ---------------------------------------------------------- 文件
    def _transcript_path(self):
        base = os.path.join(self.home, ".claude", "projects")
        if self.resume and os.path.isdir(base):
            for d in sorted(os.listdir(base)):
                p = os.path.join(base, d, self.session_id + ".jsonl")
                if os.path.isfile(p):
                    return p
        slug = re.sub(r"[^A-Za-z0-9]", "-", self.cwd)
        return os.path.join(base, slug, self.session_id + ".jsonl")

    def write_session_json(self):
        d = os.path.join(self.home, ".claude", "sessions")
        os.makedirs(d, exist_ok=True)
        data = {"pid": self.pid, "sessionId": self.session_id, "cwd": self.cwd, "startedAt": self.started_ms,
                "kind": "interactive", "entrypoint": "cli", "status": self.status, "version": VERSION}
        p = os.path.join(d, "%d.json" % self.pid)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

    def remove_session_json(self):
        try:
            os.unlink(os.path.join(self.home, ".claude", "sessions", "%d.json" % self.pid))
        except OSError:
            pass

    def update_status(self):
        if self.dialog is not None and self.dialog.kind in ("trust", "perm", "ask"):
            st = "waiting"
        elif self.turn is not None:
            st = "busy"
        else:
            st = "idle"
        if st != self.status:
            self.status = st
            self.write_session_json()

    def _base(self, typ):
        return {"parentUuid": self.last_uuid, "isSidechain": False, "type": typ, "uuid": str(uuid.uuid4()),
                "timestamp": now_iso(), "userType": "external", "entrypoint": "cli", "cwd": self.cwd,
                "sessionId": self.session_id, "version": VERSION, "gitBranch": "HEAD"}

    def append_raw(self, obj, split=False):
        os.makedirs(os.path.dirname(self.tpath), exist_ok=True)
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with open(self.tpath, "ab") as f:
            if split and len(data) > 20:  # 模拟分两次写入（agent 只能转发完整行）
                half = len(data) // 2
                f.write(data[:half])
                f.flush()
                time.sleep(0.05)
                f.write(data[half:])
            else:
                f.write(data)

    def write_entry(self, typ, message, extra=None, split=False):
        e = self._base(typ)
        e["message"] = message
        if extra:
            e.update(extra)
        self.append_raw(e, split=split)
        self.last_uuid = e["uuid"]
        return e

    def write_user_text(self, text, prompt=False):
        extra = {"promptId": str(uuid.uuid4())}
        if prompt:
            self.prompt_index += 1
            extra.update({"permissionMode": "default" if self.mode == "manual" else self.mode,
                          "origin": {"kind": "human"}, "promptSource": "typed", "turnOrigin": "human",
                          "turnPosition": {"promptIndex": self.prompt_index, "turnIndex": self.prompt_index}})
            if not os.path.exists(self.tpath):
                self.append_raw({"type": "permission-mode", "permissionMode": "default", "sessionId": self.session_id})
            self.append_raw({"type": "file-history-snapshot", "messageId": str(uuid.uuid4()),
                             "snapshot": {"trackedFileBackups": {}, "timestamp": now_iso()}, "isSnapshotUpdate": False})
        e = self.write_entry("user", {"role": "user", "content": text}, extra)
        if prompt:
            att = self._base("attachment")
            att["attachment"] = {"type": "date", "date": time.strftime("%Y-%m-%d")}
            self.append_raw(att)
        return e

    def write_assistant(self, content, stop="end_turn"):
        msg = {"model": self.model_id(), "id": "msg_fake" + uuid.uuid4().hex[:20], "type": "message",
               "role": "assistant", "content": content, "stop_reason": stop, "stop_sequence": None,
               "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1200,
                         "cache_read_input_tokens": self.ctx_tokens, "output_tokens": max(1, self.turn_tokens)}}
        return self.write_entry("assistant", msg, {"effort": self.effort}, split=True)

    def write_tool_result(self, tool_id, text, is_error):
        c = [{"type": "tool_result", "content": text, "is_error": is_error, "tool_use_id": tool_id}]
        return self.write_entry("user", {"role": "user", "content": c}, {"promptId": str(uuid.uuid4())})

    def write_local_command(self, name, stdout):
        e = self._base("system")
        e.update({"subtype": "local_command", "level": "info", "isMeta": False,
                  "content": "<command-name>/%s</command-name>\n            <command-message>%s</command-message>\n"
                             "            <command-args></command-args>" % (name, name)})
        self.append_raw(e)
        self.last_uuid = e["uuid"]
        e = self._base("system")
        e.update({"subtype": "local_command", "level": "info", "isMeta": False,
                  "content": "<local-command-stdout>%s</local-command-stdout>" % stdout})
        self.append_raw(e)
        self.last_uuid = e["uuid"]

    def load_resume(self):
        try:
            with open(self.tpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            if e.get("uuid"):
                self.last_uuid = e["uuid"]
            msg = e.get("message") or {}
            if e.get("type") == "user" and isinstance(msg.get("content"), str) and not e.get("isMeta"):
                self.prompt_index += 1
                self.add_block("❯ ", "  ", msg["content"])
            elif e.get("type") == "assistant":
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "text":
                        self.add_block("● ", "  ", c.get("text", ""))

    # ---------------------------------------------------------- 内容
    def header(self):
        self.content = [
            ("", "", ""),
            ("", "", " ▐▛███▛█   Claude Code v" + VERSION),
            ("", "", "▝▜██████▀  %s with %s effort · API Usage Billing" % (self.model_display(), self.effort)),
            ("", "", " ▝▝   ▝▝   " + self.cwd),
        ]
        if self.api_key_mask:   # fake 专用：--settings 生效的可见证据（真实 CLI 没有这一行）
            self.content.append(("", "", "           API key: %s (--settings)" % self.api_key_mask))
        self.content.append(("", "", ""))

    def add(self, first, cont, text):
        self.content.append((first, cont, text))
        self.dirty = True

    def blank(self):
        if self.content and self.content[-1][2] != "":
            self.add("", "", "")

    def add_block(self, first, cont, text):
        """多行文本：第一行用 first 前缀，其余行用 cont 前缀。"""
        self.blank()
        for i, line in enumerate(text.split("\n")):
            self.add(first if i == 0 else cont, cont, line)

    # ---------------------------------------------------------- 渲染
    def size(self):
        if self.fixed_size:
            return self.fixed_size
        try:
            s = os.get_terminal_size(sys.stdout.fileno())
            return max(20, s.columns), max(10, s.lines)
        except OSError:
            return 200, 50

    def popup(self):
        if self.popup_hidden or not self.input.startswith("/") or " " in self.input or "\n" in self.input:
            return []
        hit = [c for c in COMMANDS if c[0].startswith(self.input)]
        if not hit:
            return []
        rest = [c for c in COMMANDS if c not in hit]
        return (hit + rest)[:4]

    def status_lines(self, W):
        return [self.status_line(W)] if self.show_status else []

    def status_line(self, W):
        cwd = self.cwd
        home = os.path.realpath(self.home)
        if cwd == home or cwd == self.home:
            cwd = "~"
        elif cwd.startswith(home + "/"):
            cwd = "~" + cwd[len(home):]
        pct = min(100, self.ctx_tokens * 100 // 1000000)
        tok = "%dk" % (self.ctx_tokens // 1000) if self.ctx_tokens >= 1000 else str(self.ctx_tokens)
        s = "  %s@%s:%s ▱▱▱▱▱▱▱▱▱▱ %d%% %s/1M $%.4f [%s · 1M · effort:%s]" % (
            self.user, self.host, cwd, pct, tok, self.cost, self.model_display(), self.effort)
        if dwidth(s) > W - 2:
            s = clip(s, W - 3) + "…"
        return s

    def spinner_text(self):
        el = time.time() - self.turn_started
        ch = SPINNER[int(el * 8) % len(SPINNER)]
        s = "%s %s…" % (ch, self.turn_verb)
        if el >= 3:
            secs = int(el)
            if self.turn_tokens:
                s += " (%ds · ↓ %s tokens · thinking with %s effort)" % (secs, fmt_tokens(self.turn_tokens), self.effort)
            else:
                s += " (%ds · thinking with %s effort)" % (secs, self.effort)
        return s

    def frame(self):
        W, H = self.size()
        d = self.dialog
        if d is not None and d.kind == "trust":
            lines = self.trust_lines(W)
        else:
            if d is not None and d.kind in ("perm", "ask"):
                bottom = ["─" * W] + (self.perm_lines(W) if d.kind == "perm" else self.ask_lines(W)) + [""]
            elif d is not None and d.kind in ("model", "effort"):
                bottom = ["▔" * W] + (self.model_lines(W) if d.kind == "model" else self.effort_lines(W)) + [""]
            else:
                pop = self.popup()
                if pop:
                    self.popup_cursor = min(self.popup_cursor, len(pop) - 1)
                    sep = []
                    for i, (name, desc) in enumerate(pop):
                        mark = "❯ " if i == self.popup_cursor else "  "
                        sep.append("  " + mark + name.ljust(28) + desc.format(model=self.model_display()))
                elif "\n" in self.input:
                    hint = "ctrl+g to edit in Vim"
                    sep = [" " * max(0, W - len(hint) - 2) + hint]
                elif self.agents and time.time() - self.kill_at <= KILL_CONFIRM_WINDOW:
                    sep = [" " * max(0, W - len(KILL_HINT) - 2) + KILL_HINT]
                else:
                    sep = [""]
                inp = []
                shown = self.input if (self.input or not self.queue) else QUEUED_PLACEHOLDER
                for i, line in enumerate(shown.split("\n")):
                    inp += wrap(line, W, "❯" + NB if i == 0 else "  ", "  ")
                shells = self.shells()
                # 真机（v2.1.331 实测）：输入框里有文字时模式行不显示 "esc to interrupt"（此时 Esc 是清空输入框），
                # 即使正在生成——只看模式行会把"忙时往输入框粘贴、还没按回车"的那几帧误当成空闲
                mode = "  " + MODE_TEXT.get(self.mode, self.mode) + (" · %d shell" % shells if shells else "") + \
                    (" · esc to interrupt" if self.turn and not self.input else "")
                agents = []
                if self.agents or shells:
                    mode += " · ↓ to manage"
                if self.agents:
                    agents.append("  ● main")
                    for sub in list(self.agents.values()):
                        left = "  ◯ %s  %s" % (sub.agent_type, sub.desc)
                        right = "%ds" % sub.elapsed()
                        agents.append(left + " " * max(2, W - 2 - dwidth(left) - len(right)) + right)
                bottom = sep + ["─" * W] + inp + ["─" * W] + self.status_lines(W) + [mode] + agents + [""]
            body = []
            for first, cont, text in self.content:
                body += wrap(text, W, first, cont)
            if self.turn is not None and d is None and self.queue:
                # 排队的消息（真机实测：连续的 ❯ 块，后面只有一行共用的 "⎿  Queued · ctrl+x ctrl+s to send now"）
                for q in self.queue:
                    for i, line in enumerate(q.split("\n")):
                        body += wrap(line, W, "❯" + NB if i == 0 else "  ", "  ")
                body.append("  ⎿  " + QUEUED_HINT)
            if self.turn is not None and self.spinner_on and (d is None or d.kind not in ("perm", "ask")):
                body.append(self.spinner_text())
                if self.turn_tip and not self.turn_tokens:
                    body.append(RES + self.turn_tip)
            avail = max(0, H - len(bottom))
            body = body[-avail:] if len(body) > avail else body + [""] * (avail - len(body))
            lines = (body + bottom)[-H:]
        lines = (lines + [""] * H)[:H]
        return [clip(x, W) for x in lines]

    def trust_lines(self, W):
        is_home = os.path.realpath(self.cwd) == os.path.realpath(self.home)
        p2 = "Claude Code'll be able to read, edit, and execute files here."
        if is_home:
            p2 += " This is your home folder, so Yes lasts for this session only."
        out = ["", "─" * W, " Accessing workspace:", "", " " + self.cwd, ""]
        out += wrap("Quick safety check: Is this a project you created or one you trust? (Like your own code, a "
                    "well-known open source project, or work from your team). If not, take a moment to review "
                    "what's in this folder first.", W - 1, " ", " ")
        out += [""] + wrap(p2, W - 1, " ", " ") + ["", " Security guide", ""]
        for i, label in enumerate(("No, exit", "Yes, I trust this folder")):
            out.append((" ❯ " if self.dialog.cursor == i else "   ") + label)
        out += ["", " Enter to confirm · Esc to cancel"]
        return out

    def option_lines(self, W, options, cursor, indent=" "):
        out = []
        for i, label in enumerate(options):
            mark = "❯ " if i == cursor else "  "
            out += wrap(label, W - 2, indent + mark + "%d. " % (i + 1), " " * (len(indent) + 5))
        return out

    def perm_lines(self, W):
        d = self.dialog
        out = [" " + d.title]
        if d.tip:
            out += wrap('Tip: auto mode handles these prompts for you — choose "switch to auto mode" below',
                        W - 1, " ", " ")
        out += [" " + d.subtitle, "╌" * W] + d.preview + ["╌" * W, " " + d.question]
        out += self.option_lines(W, d.options, d.cursor)
        out += ["", " Esc to cancel · Tab to amend"]
        return out

    def ask_lines(self, W):
        d = self.dialog
        out = [" ☐ " + ASK_Q["header"], "", ASK_Q["question"], ""]
        labels = [o["label"] for o in ASK_Q["options"]] + [d.typed or "Type something."]
        descs = [o["description"] for o in ASK_Q["options"]] + [None]
        for i, label in enumerate(labels):
            out.append(("❯ " if d.cursor == i else "  ") + "%d. %s" % (i + 1, label))
            if descs[i]:
                out.append("     " + descs[i])
        out.append("─" * W)
        out.append(("❯ " if d.cursor == 3 else "  ") + "4. Chat about this")
        out += ["", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
        return out

    def model_lines(self, W):
        d = self.dialog
        out = ["  Select model", "  Your pick is the default for new sessions. For other models, use --model."]
        top = d.top
        out.append("    ↑ %d more" % top if top > 0 else "")
        cur = self.model_index()
        for i in range(top, min(len(MODELS), top + MODEL_WINDOW)):
            m = MODELS[i]
            name = m["name"] + (" ✔" if i == cur else "")
            mark = "  ❯ " if i == d.cursor else "    "
            head = mark + "%d. " % (i + 1) + name.ljust(23)
            out += wrap(m["desc"], W - 4, head, " " * 30)
        below = len(MODELS) - (top + MODEL_WINDOW)
        out.append("    ↓ %d more" % below if below > 0 else "")
        lvl = d.effort
        label = EFFORT_LABEL[lvl] + " effort" + (" (default)" if lvl == DEFAULT_EFFORT else "")
        out += ["", "  %s %s ←/→ to adjust" % (EFFORT_ICON[lvl], label)]
        if lvl == "max":
            out += wrap(MAX_WARNING, W - 2, "  ", "  ")
        # 实测：/model 面板在 max 时提示行不变（仍可 s / Enter 保存）
        out += ["", "  s to use this session only · Enter to save · Esc to cancel"]
        return out

    def effort_lines(self, W):
        d = self.dialog
        names = "low     medium     high     xhigh      max"
        starts = [names.index(n) for n in EFFORTS]
        centers = [s + len(n) // 2 for s, n in zip(starts, EFFORTS)]
        bar = ["─"] * len(names)
        bar[centers[EFFORTS.index(d.level)]] = "▲"
        ind = " " * 9
        mx = d.level == "max"
        return ["  Effort", "", ind + "Faster" + " " * 29 + "Smarter",
                ind + "".join(bar) + "      Ultracode  " + ("on" if d.ultracode else "off"),
                ind + names + "      Tab to toggle", ""] + (wrap(MAX_WARNING, W - 2, "  ", "  ") if mx else [""]) + [
                "  " + (EFFORT_HINT_MAX if mx else EFFORT_HINT)]

    def draw(self, force=False):
        lines = self.frame()
        if not force and lines == self.last_frame:
            return
        prev = self.last_frame or []
        buf = []
        for i, line in enumerate(lines):
            if force or i >= len(prev) or prev[i] != line:
                buf.append("\x1b[%d;1H%s\x1b[K" % (i + 1, line))
        self.last_frame = lines
        self.write("".join(buf))

    def write(self, s):
        if self.out is not None:
            self.out.write(s)
            return
        data = s.encode("utf-8")
        while data:
            try:
                n = os.write(sys.stdout.fileno(), data)
            except InterruptedError:
                continue
            data = data[n:]

    # ---------------------------------------------------------- 回合（生成器：yield 秒数 或 ("dialog", Dialog)）
    def start_turn(self, gen, verb=None):
        self.turn = gen
        self.turn_started = time.time()
        self.turn_due = 0.0
        self.turn_send = None
        self.turn_tokens = 0
        self.turn_verb = verb or VERBS[self.prompt_index % len(VERBS)]
        self.turn_tip = TIPS[self.prompt_index % len(TIPS)]
        self.spinner_on = True
        self.update_status()
        self.dirty = True

    def step_turn(self):
        if self.turn is None or self.dialog is not None or time.time() < self.turn_due:
            return
        try:
            v = self.turn.send(self.turn_send)
        except StopIteration:
            self.end_turn()
            return
        self.turn_send = None
        if isinstance(v, tuple) and v[0] == "dialog":
            self.dialog = v[1]
            self.update_status()
        else:
            self.turn_due = time.time() + float(v or 0)
        self.dirty = True

    def end_turn(self):
        self.turn = None
        self.spinner_on = False
        self.update_status()
        self.dirty = True
        if self.queue:
            self.submit(self.queue.pop(0))

    def interrupt(self):
        if self.turn is None:
            return
        try:
            self.turn.close()
        except Exception:
            pass
        self.turn = None
        self.dialog = None
        self.add(RES, "     ", "Interrupted · What should Claude do instead?")
        self.write_user_text([{"type": "text", "text": "[Request interrupted by user]"}])
        self.queue = []
        self.end_turn()

    def stream(self, paragraphs, first_prefix="● "):
        """逐行流式输出回复（每行一个 yield）。"""
        W, _ = self.size()
        self.blank()
        first = True
        for para in paragraphs:
            if not first:
                self.add("", "", "")
            for i, piece in enumerate(wrap(para, W, "", "") if para else [""]):
                self.add(first_prefix if first and i == 0 else "  ", "  ", piece)
                self.turn_tokens += max(1, len(piece) // 2)
                yield self.delay
            first = False

    def shells(self):
        now = time.time()
        self.bg_until = [t for t in self.bg_until if t > now]
        return len(self.bg_until)

    def finish(self, text):
        el = max(1, int(time.time() - self.turn_started))
        self.turn_tokens = max(self.turn_tokens, 1)
        self.write_assistant([{"type": "text", "text": text}])
        self.ctx_tokens += 1200 + self.turn_tokens * 3
        self.cost += 0.0004
        self.blank()
        n = self.shells()
        self.add("", "", "✻ Baked for %ds · done %s%s" % (el, clock(), " · %d shell still running" % n if n else ""))
        self.append_raw({"type": "last-prompt", "lastPrompt": "", "leafUuid": self.last_uuid,
                         "sessionId": self.session_id})

    def turn_reply(self, msg):
        yield self.think
        first = msg.strip().split("\n")[0][:60]
        paras = ["收到：%s" % first, "这是 fake claude 的回复（本地测试用，不会调用任何模型）。",
                 "当前模型 %s，effort %s，权限模式 %s。" % (self.model_display(), self.effort, self.mode)]
        for d in self.stream(paras):
            yield d
        self.finish("\n\n".join(paras))

    def turn_long(self, msg):
        yield self.think
        self.blank()
        n = 200
        for i in range(1, n + 1):
            self.add("● " if i == 1 else "  ", "  ", str(i))
            self.turn_tokens += 2
            yield self.delay
        self.finish("\n".join(str(i) for i in range(1, n + 1)))

    def turn_md(self, msg):
        yield self.think
        rendered, code = [], False
        for line in MD_TEXT.split("\n"):
            if line.startswith("```"):
                code = not code
                continue
            if not code:
                line = line.replace("**", "").replace("`", "")
                if line.startswith("#"):
                    line = line.lstrip("#").strip()
                elif line.startswith("- "):
                    line = " - " + line[2:]
            rendered.append(line)
        self.blank()
        for i, line in enumerate(rendered):
            self.add("● " if i == 0 else "  ", "  ", line)
            self.turn_tokens += 3
            yield self.delay
        self.finish(MD_TEXT)

    def turn_bg(self, msg):
        """较慢的 Bash 命令：屏幕显示进度与 "(ctrl+b ctrl+b (twice) to run in background)"；Ctrl+B → 转入后台。"""
        yield self.think
        tid = self.tool_use("Bash", {"command": "\n".join(BG_CMD), "description": BG_DESC})
        mark = len(self.content)
        self.blank()
        self.add("  ", "  ", BG_DESC)
        self.add("  ⎿  ", "  ", "$ " + BG_CMD[0])
        for line in BG_CMD[1:]:
            self.add("  ", "  ", line)
        prog = len(self.content) - 1
        self.add("  ", "  ", BG_HINT)
        self.bg_running, self.bg_flag = True, False
        backgrounded = False
        try:
            for i in range(BG_STEPS):
                if self.bg_flag:
                    backgrounded = True
                    break
                self.content[prog] = ("  ", "  ", BG_CMD[-1] + " (%ds · %d lines)" % (i + 1, i + 1))
                self.dirty = True
                yield self.delay
        finally:
            self.bg_running = False
        del self.content[mark:]
        self.blank()
        self.add("● ", "  ", "Ran 1 shell command")
        if backgrounded:
            self.bg_until.append(time.time() + BG_SHELL_SECONDS)
            self.write_tool_result(tid, "Command running in background with ID: fake%s" % uuid.uuid4().hex[:6], False)
            paras = ["计数循环已转入后台运行（fake：没有真正运行）。"]
        else:
            self.write_tool_result(tid, "\n".join(str(i) for i in range(BG_STEPS)) + "\nBG-DONE", False)
            paras = ["计数循环已运行完毕（fake：没有真正运行）。"]
        yield self.delay
        for d in self.stream(paras):
            yield d
        self.finish(paras[0])

    def send_now(self):
        """Ctrl+X Ctrl+S：中断当前生成，立即发送排队的消息。"""
        if self.turn is None or not self.queue:
            return
        q, self.queue = list(self.queue), []
        self.interrupt()
        self.submit("\n".join(q))

    def tool_use(self, name, inp):
        tid = "toolu_fake" + uuid.uuid4().hex[:16]
        self.turn_tokens += 40
        self.write_assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}], stop="tool_use")
        return tid

    def rejected(self, tid):
        self.write_tool_result(tid, REJECT_TEXT, True)
        self.write_user_text([{"type": "text", "text": "[Request interrupted by user for tool use]"}])

    def turn_agent(self, msg, task_stop=False):
        yield self.think
        sub = None
        ok = False
        try:
            tid = self.tool_use("Agent", {"description": AGENT_DESC, "prompt": AGENT_PROMPT,
                                          "subagent_type": "general-purpose", "model": "haiku"})
            sub = FakeSubagent(self, AGENT_DESC, AGENT_PROMPT, tid)
            text = AGENT_LAUNCHED % (sub.aid, sub.aid, sub.output_file)
            c = [{"tool_use_id": tid, "type": "tool_result", "content": [{"type": "text", "text": text}]}]
            self.write_entry("user", {"role": "user", "content": c}, {
                "promptId": str(uuid.uuid4()), "sourceToolAssistantUUID": self.last_uuid,
                "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": sub.aid,
                                  "description": AGENT_DESC, "resolvedModel": self.model_id(), "prompt": AGENT_PROMPT,
                                  "outputFile": sub.output_file, "canReadOutputFile": True,
                                  "canContinueAgent": True}})
            self.agents[sub.aid] = sub
            self.blank()
            self.add("● ", "  ", "Agent(%s)" % AGENT_DESC)
            self.add("  ⎿  ", "     ", "Backgrounded agent (↓ to manage · ctrl+o to expand)")
            yield self.delay
            paras = ["子代理已转入后台运行，我等它完成后再汇总结果。"]
            for d in self.stream(paras):
                yield d
            self.write_assistant([{"type": "text", "text": paras[0]}])
            if task_stop:
                for d in self.agent_task_stop(sub):
                    yield d
                ok = True
                return
            self.blank()
            self.add("", "", "✻ Waiting for 1 background agent to finish")
            mark = len(self.content) - 1
            steps = sub.script()
            while not sub.stopped:   # 每一步之前先看是否已被停止（停止后不能再写子代理条目）
                try:
                    d = next(steps)
                except StopIteration:
                    break
                yield d
            if sub.stopped:   # 用户 Ctrl+X Ctrl+K 停掉了它（kill_agents 已写好文件）
                ok = True
                del self.content[mark]
                self.add("● ", "  ", "Background agents stopped")
                yield self.delay
                paras = ["子代理「%s」已被停止，没有完成。" % AGENT_DESC]
                for d in self.stream(paras):
                    yield d
                self.finish(paras[0])
                return
            sub.finish()
            self.agents.pop(sub.aid, None)
            del self.content[mark]
            self.add("● ", "  ", 'Agent "%s" finished · %ds' % (AGENT_DESC, sub.elapsed()))
            self.write_task_notification(sub, "completed")
            ok = True
            yield self.delay
            paras = ["子代理已完成：%s" % sub.result]
            for d in self.stream(paras):
                yield d
            self.finish(paras[0])
        finally:
            if sub is not None and not ok:  # 被 Esc 中断：子代理随之结束
                self.agents.pop(sub.aid, None)
                sub.finish()
                self.write_task_notification(sub, "killed")

    def agent_task_stop(self, sub):
        """/agentstop：子代理跑两步后，主会话调用 TaskStop（仿 CLI 2.1.332 的写法）。"""
        steps = sub.script()
        for _ in range(3):
            yield next(steps)
        steps.close()
        tid = self.tool_use("TaskStop", {"task_id": sub.aid})
        self.write_entry("user", {"role": "user", "content": [
            {"tool_use_id": tid, "type": "tool_result", "content": TASKSTOP_RESULT % (sub.aid, sub.desc, sub.aid,
                                                                                       sub.desc)}]},
            {"promptId": str(uuid.uuid4()), "sourceToolAssistantUUID": self.last_uuid})
        sub.stop("claude")
        self.agents.pop(sub.aid, None)
        self.blank()
        self.add("● ", "  ", "Stop Task")
        self.add("  ⎿  ", "     ", "%s · stopped" % sub.desc)
        content = AGENT_NOTE % (sub.aid, sub.meta["toolUseId"], sub.output_file, "killed", sub.desc,
                                "was stopped by Claude", "", 12000 + sub.tokens, sub.tool_uses,
                                int((time.time() - sub.started) * 1000))
        self.append_raw({"type": "queue-operation", "operation": "enqueue", "timestamp": now_iso(),
                         "sessionId": self.session_id, "content": content})
        att = self._base("attachment")
        att["attachment"] = {"type": "queued_command", "prompt": content, "source_uuid": str(uuid.uuid4()),
                             "commandMode": "task-notification",
                             "origin": {"kind": "task-notification", "producer": "session-task"},
                             "rowSource": "recorded", "timestamp": now_iso()}
        att["rendered"] = [{"content": "<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n" + content +
                                       "\n</system-reminder>"}]
        self.append_raw(att)
        self.append_raw({"type": "queue-operation", "operation": "remove", "timestamp": now_iso(),
                         "sessionId": self.session_id, "content": content, "reason": "absorbed_mid_turn"})
        yield self.delay
        paras = ["已停止子代理「%s」，它没有完成。" % sub.desc]
        for d in self.stream(paras):
            yield d
        self.finish(paras[0])

    def kill_agents(self):
        """Ctrl+X Ctrl+K 两次：停止全部后台子代理（仿 CLI 2.1.332，见 turn_agent / FakeSubagent.stop）。"""
        subs = [x for x in self.agents.values() if not x.done and not x.stopped]
        if not subs:
            return
        for sub in subs:
            sub.stop("user")
            self.agents.pop(sub.aid, None)
        names = [x.desc for x in subs]
        text = ('Background agent "%s" was stopped by the user.' % names[0] if len(names) == 1 else
                "%d background agents were stopped by the user: %s." % (len(names), ", ".join('"%s"' % n for n in names)))
        self.append_raw({"type": "queue-operation", "operation": "enqueue", "timestamp": now_iso(),
                         "sessionId": self.session_id, "content": text})
        e = self._base("system")
        e.update({"subtype": "agents_killed", "isMeta": False})
        self.append_raw(e)
        self.last_uuid = e["uuid"]
        self.write_entry("user", {"role": "user", "content": text}, {
            "promptId": str(uuid.uuid4()), "origin": {"kind": "task-notification"}, "promptSource": "system",
            "rowSource": "harness", "turnOrigin": "task_notification", "queueSkipAttachments": True})
        self.turn_due = 0.0

    def write_task_notification(self, sub, status):
        verb = "finished" if status == "completed" else "was stopped"
        result = "<result>%s</result>\n" % sub.result if status == "completed" and sub.result else ""
        content = AGENT_NOTE % (sub.aid, sub.meta["toolUseId"], sub.output_file, status, sub.desc, verb, result,
                                12000 + sub.tokens, sub.tool_uses, int((time.time() - sub.started) * 1000))
        self.append_raw({"type": "queue-operation", "operation": "enqueue", "timestamp": now_iso(),
                         "sessionId": self.session_id, "content": content})
        self.write_entry("user", {"role": "user", "content": content}, {
            "promptId": str(uuid.uuid4()), "permissionMode": "default" if self.mode == "manual" else self.mode,
            "origin": {"kind": "task-notification", "producer": "session-task", "runId": uuid.uuid4().hex[:12]},
            "promptSource": "system", "turnOrigin": "task_notification", "queueSkipAttachments": True})

    def turn_tool(self, msg):
        yield self.think
        tid = self.tool_use("Bash", {"command": PROBE_CMD, "description": PROBE_DESC})
        if self.mode == "dontAsk":
            choice = len(self.bash_options()) - 1
        elif self.mode == "bypassPermissions":
            choice = 0
        else:
            mark = len(self.content)
            self.blank()
            self.add("  ", "  ", PROBE_DESC.replace("Create", "Creating"))
            self.add("  ⎿  ", "     ", "$ " + PROBE_CMD)
            self.add("", "", "")
            opts = self.bash_options()
            choice = yield ("dialog", Dialog("perm", tool="Bash", title="Bash command", tip=self.mode == "manual",
                                             subtitle=PROBE_DESC, preview=[" " + PROBE_CMD],
                                             question="Do you want to proceed?", options=opts))
            del self.content[mark:]
        opts = self.bash_options()
        if choice is None or choice == len(opts) - 1:  # Esc 或 No
            self.blank()
            self.add("  ", "  ", "Did not run 1 call")
            self.add(RES, "     ", "$ " + PROBE_CMD)
            self.add(RES, "     ", "Interrupted · What should Claude do instead?")
            self.rejected(tid)
            return
        if "switch to auto mode" in opts[choice]:
            self.mode = "auto"
        self.write_tool_result(tid, "(fake：命令没有真正执行)", False)
        self.blank()
        self.add("  ", "  ", "Ran 1 shell command")
        yield self.delay
        paras = ["已执行 %s（fake：没有真正运行，命令没有输出）。" % PROBE_CMD]
        for d in self.stream(paras):
            yield d
        self.finish(paras[0])

    def bash_options(self):
        opts = ["Yes", "Yes, and always allow access to /tmp from this project"]
        if self.mode == "manual":
            opts.append("Yes, and switch to auto mode · auto mode handles these prompts for you")
        opts.append("No")
        return opts

    def turn_write(self, msg):
        yield self.think
        path = os.path.join(self.cwd, WRITE_FILE)
        tid = self.tool_use("Write", {"file_path": path, "content": "hi"})
        opts = ["Yes", "Yes, and switch to accept edits (auto-approve file edits and common file commands) for "
                       "this session (shift+tab)", "No"]
        if self.mode in ("acceptEdits", "bypassPermissions"):
            choice = 0
        elif self.mode == "dontAsk":
            choice = 2
        else:
            mark = len(self.content)
            self.blank()
            self.add("● ", "  ", "Write(%s)" % WRITE_FILE)
            self.add("", "", "")
            choice = yield ("dialog", Dialog("perm", tool="Write", title="Create file", tip=False,
                                             subtitle=WRITE_FILE, preview=["  1 hi"],
                                             question="Do you want to create %s?" % WRITE_FILE, options=opts))
            del self.content[mark:]
        self.blank()
        self.add("● ", "  ", "Write(%s)" % WRITE_FILE)
        if choice is None or choice == 2:
            self.add(RES, "     ", "Rejected write to %s" % WRITE_FILE)
            self.add("      ", "      ", "1 hi")
            self.rejected(tid)
            return
        if choice == 1:
            self.mode = "acceptEdits"
        self.add(RES, "     ", "Wrote 1 lines to %s（fake：没有真正写入）" % WRITE_FILE)
        self.write_tool_result(tid, "File created successfully at: %s (fake)" % path, False)
        yield self.delay
        paras = ["已写入 %s（fake：没有真正写入文件）。" % WRITE_FILE]
        for d in self.stream(paras):
            yield d
        self.finish(paras[0])

    def turn_ask(self, msg):
        yield self.think
        tid = self.tool_use("AskUserQuestion", {"questions": [ASK_Q]})
        self.spinner_on = False
        res = yield ("dialog", Dialog("ask", typed=""))
        q = ASK_Q["question"]
        if res is None or res == "__chat__":
            self.blank()
            self.add("● ", "  ", "Declined to answer Claude's questions:")
            self.add(RES, "     ", "· %s (%s)" % (q, " / ".join(o["label"] for o in ASK_Q["options"])))
            self.rejected(tid)
            return
        self.spinner_on = True
        self.blank()
        self.add("● ", "  ", "User answered Claude's questions:")
        self.add(RES, "     ", "· %s → %s" % (q, res))
        self.write_tool_result(tid, 'User has answered your questions: "%s"="%s".' % (q, res), False)
        yield self.delay
        paras = ["好的，你选择了：%s。" % res]
        for d in self.stream(paras):
            yield d
        self.finish(paras[0])

    # ---------------------------------------------------------- 提交
    def submit(self, text):
        cmd = text.strip()
        if self.turn is not None:
            self.queue.append(text)
            return
        if cmd == "/exit":
            self.add_block("❯ ", "  ", "/exit")
            self.add(RES, "     ", "Goodbye!")
            self.exit_code = 0
            return
        if cmd == "/clear":
            self.session_id = str(uuid.uuid4())
            self.tpath = self._transcript_path()
            self.last_uuid = None
            self.prompt_index = 0
            self.ctx_tokens = 0
            self.header()
            self.status = None
            self.update_status()
            return
        if cmd == "/model":
            cur = self.model_index()
            cur = 0 if cur is None else cur
            self.dialog = Dialog("model", cursor=cur, orig=cur, top=max(0, min(cur - MODEL_WINDOW + 1,
                                                                                len(MODELS) - MODEL_WINDOW)),
                                 effort=self.effort)
            self.dirty = True
            return
        if cmd == "/effort":
            self.dialog = Dialog("effort", level=self.effort, ultracode=False)
            self.dirty = True
            return
        if cmd == "/context":
            self.context_usage()
            return
        self.add_block("❯ ", "  ", text)
        self.write_user_text(text, prompt=True)
        if "/tool" in text:
            gen = self.turn_tool(text)
        elif "/write" in text:
            gen = self.turn_write(text)
        elif "/ask" in text:
            gen = self.turn_ask(text)
        elif "/long" in text:
            gen = self.turn_long(text)
        elif "/md" in text:
            gen = self.turn_md(text)
        elif "/agentstop" in text:
            gen = self.turn_agent(text, task_stop=True)
        elif "/agent" in text:
            gen = self.turn_agent(text)
        elif "/html" in text:
            gen = self.turn_html(text)
        elif "/shot" in text:
            gen = self.turn_shot(text)
        elif "/bg" in text:
            gen = self.turn_bg(text)
        else:
            gen = self.turn_reply(text)
        self.start_turn(gen)

    # ---------------------------------------------------------- 可视化演示（/html、/shot）
    def _demo_dir(self):
        """演示文件目录：cwd/ccr-demo（cwd 在 HOME 以内时），否则 HOME/ccr-demo；绝不写 HOME 以外。"""
        home = os.path.realpath(self.home)
        base = os.path.realpath(self.cwd)
        if not (base == home or base.startswith(home + os.sep)):
            base = home
        return os.path.join(base, "ccr-demo")

    def _write_demo(self, path, data):
        home = os.path.realpath(self.home)
        real = os.path.realpath(os.path.dirname(path))
        if not (real == home or real.startswith(home + os.sep)) and not real.startswith(os.path.realpath(self._demo_dir())):
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return True

    def _tool_result_full(self, tool_id, content, tur, is_error=False):
        c = [{"tool_use_id": tool_id, "type": "tool_result", "content": content}]
        if is_error:
            c[0]["is_error"] = True
        extra = {"promptId": str(uuid.uuid4()), "sourceToolAssistantUUID": self.last_uuid}
        if tur is not None:
            extra["toolUseResult"] = tur
        return self.write_entry("user", {"role": "user", "content": c}, extra)

    def _demo_tool(self, name, inp, label, result_line):
        tid = self.tool_use(name, inp)
        self.blank()
        self.add("● ", "  ", label)
        self.add(RES, "     ", result_line)
        return tid

    def turn_html(self, msg):
        yield self.think
        d = self._demo_dir()
        files = {"index.html": DEMO_HTML, "style.css": DEMO_CSS, "app.js": DEMO_JS}
        for name in ("index.html", "style.css", "app.js"):
            path = os.path.join(d, name)
            self._write_demo(path, files[name])
            tid = self._demo_tool("Write", {"file_path": path, "content": files[name]}, "Write(%s)" % path,
                                  "Wrote %d lines to %s" % (files[name].count("\n"), path))
            self._tool_result_full(tid, "File created successfully at: %s" % path,
                                   {"type": "create", "filePath": path, "content": files[name], "structuredPatch": [],
                                    "originalFile": None})
            yield self.delay
        logo = fake_png(160, 160, seed=7, style="logo")
        self._write_demo(os.path.join(d, "logo.png"), logo)
        # Edit：改标题 + 加一行副标题（structuredPatch 带新旧行号）
        path = os.path.join(d, "index.html")
        old, new = "<h1>CCR 演示页</h1>", "<h1>CCR 可视化演示页</h1>\n      <p class=\"sub\">由 fake claude 生成 · 实时刷新</p>"
        updated = DEMO_HTML.replace(old, new)
        self._write_demo(path, updated)
        tid = self._demo_tool("Edit", {"file_path": path, "old_string": old, "new_string": new, "replace_all": False},
                              "Update(%s)" % path, "Updated %s with 2 additions and 1 removal" % path)
        self._tool_result_full(tid, "The file %s has been updated successfully." % path,
                               {"filePath": path, "oldString": old, "newString": new, "originalFile": DEMO_HTML,
                                "structuredPatch": structured_patch(DEMO_HTML, updated), "userModified": False,
                                "replaceAll": False})
        yield self.delay
        cmd = "cd %s && python3 -m http.server 8123 --bind 127.0.0.1" % d
        tid = self._demo_tool("Bash", {"command": cmd, "description": "Start a static file server (fake)",
                                       "run_in_background": True}, "Bash(%s)" % cmd, "Running in the background")
        out = "Serving HTTP on 127.0.0.1 port 8123 (http://127.0.0.1:8123/) ...（fake：没有真正启动）"
        self._tool_result_full(tid, out, {"stdout": out, "stderr": "", "interrupted": False, "isImage": False})
        yield self.delay
        rel = os.path.relpath(d, self.cwd) if os.path.realpath(d).startswith(os.path.realpath(self.cwd)) else d
        text = ("网页已生成：`%s/index.html`（样式在 [style.css](%s/style.css)，脚本 `%s/app.js`）。\n\n"
                "本地预览：http://127.0.0.1:8123/ （fake：服务器没有真正启动）。\n\n"
                "- 标题已改为“CCR 可视化演示页”\n- 图标：`%s/logo.png`" % (rel, rel, rel, rel))
        for dd in self.stream(["网页已生成：%s/index.html" % rel, "本地预览：http://127.0.0.1:8123/"]):
            yield dd
        self.finish(text)

    def turn_shot(self, msg):
        yield self.think
        d = self._demo_dir()
        shot = fake_png(720, 450, seed=int(time.time()) % 97, style="page")
        b64 = base64.b64encode(shot).decode("ascii")
        tid = self._demo_tool("mcp__chrome-devtools__take_screenshot", {"pageId": 1}, "chrome-devtools - take_screenshot (MCP)",
                              "Took a screenshot of the current page's viewport.")
        content = [{"type": "text", "text": "Took a screenshot of the current page's viewport."},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                   {"type": "text", "text": "[Image: source: %s/.claude/projects/fake/shot.png]" % self.home}]
        self._tool_result_full(tid, content, content)
        yield self.delay
        path = os.path.join(d, "shot.png")
        self._write_demo(path, shot)
        cmd = "mkdir -p %s && cp /tmp/ccr-fake-screenshot.png %s" % (d, path)
        tid = self._demo_tool("Bash", {"command": cmd, "description": "Save screenshot (fake)"}, "Bash(%s)" % cmd,
                              "(fake) saved")
        out = "已保存截图到 %s（fake）" % path
        self._tool_result_full(tid, out, {"stdout": out, "stderr": "", "interrupted": False, "isImage": False})
        yield self.delay
        tid = self._demo_tool("Read", {"file_path": path}, "Read(%s)" % path, "Read image (%dKB)" % (len(shot) // 1024))
        self._tool_result_full(tid, [{"type": "image", "source": {"type": "base64", "data": b64, "media_type": "image/png"}}],
                               {"type": "image", "file": {"base64": b64, "type": "image/png", "originalSize": len(shot),
                                                          "dimensions": {"originalWidth": 720, "originalHeight": 450,
                                                                         "displayWidth": 720, "displayHeight": 450}}})
        yield self.delay
        rel = os.path.relpath(path, self.cwd) if os.path.realpath(path).startswith(os.path.realpath(self.cwd)) else path
        text = "截图已保存到 `%s`：\n\n![页面截图](%s)\n\n（fake：截图是程序生成的示意图）" % (rel, rel)
        for dd in self.stream(["截图已保存到 %s" % rel]):
            yield dd
        self.finish(text)

    def context_usage(self):
        """/context：版式照抄 CLI 2.1.331 实测（fixtures/context_cmd/、transcript_context_*.jsonl），数字按假的上下文 tokens 算。"""
        try:
            window = int(self.env.get("CCR_FAKE_CTX_WINDOW") or 1000000)
        except ValueError:
            window = 1000000
        fmt = lambda n: ("%gm" % round(n / 1e6, 1)) if n >= 1e6 else ("%gk" % round(n / 1e3, 1)) if n >= 1000 else str(n)
        cats = [("System prompt", 3100, 245), ("System tools", 10100, 241), ("Messages", 1202 + self.ctx_tokens, 127)]
        used = sum(c[1] for c in cats)
        buf = 33000
        pct = lambda n: "<0.1%" if n * 1000 < window else "%g%%" % round(n * 100.0 / window, 1 if n * 100 < window * 10 else 0)
        right = [self.model_display(), self.model_id(), "%s/%s tokens (%s)" % (fmt(used), fmt(window), pct(used)), "",
                 "Estimated usage by category"]
        right += ["⛁ %s: %s tokens (%s)" % (n, fmt(t), pct(t)) for n, t, _c in cats]
        right += ["⛶ Free space: %s tokens (%s)" % (fmt(window - used - buf), pct(window - used - buf)),
                  "⛝ Autocompact buffer: %s tokens (%s)" % (fmt(buf), pct(buf))]
        cells = ["⛁"] * max(1, min(100, used * 100 // window)) + ["⛶"] * 100
        cells = (cells[:100 - 3] + ["⛝"] * 3)
        rows = [" ".join(cells[r * 10:(r + 1) * 10]) for r in range(10)]
        screen = ["Context usage"] + [(rows[k] if k < 10 else " " * 19) + ("   " + right[k] if k < len(right) and right[k] else "")
                                      for k in range(max(10, len(right)))]
        screen += ["", "Auto-compact window:  %s tokens" % fmt(window), "", "/context all to expand"]
        self.add_block("❯ ", "  ", "/context")
        for i, line in enumerate(screen):
            self.add(RES if i == 0 else "     ", "     ", line.rstrip())
        # transcript：带 ANSI 的 stdout（与真机同一套颜色码）+ isMeta 的 Markdown 版
        g = lambda line: line.replace("⛁", "\x1b[38;5;245m⛁\x1b[39m").replace("⛶", "\x1b[38;5;241m⛶\x1b[39m")
        ansi = [" \x1b[1mContext usage\x1b[22m"]
        for k in range(max(10, len(right))):
            r = right[k] if k < len(right) else ""
            if r and r[0] in "⛁⛶⛝":
                color = {"⛁": next((c for n, _t, c in cats if r[2:].startswith(n)), 245), "⛶": 241, "⛝": 241}[r[0]]
                r = "\x1b[38;5;%dm%s\x1b[39m%s" % (color, r[0], r[1:])
            elif r and k > 0:
                r = "\x1b[38;5;241m%s\x1b[39m" % r
            ansi.append((g(rows[k]) + " " if k < 10 else " " * 20) + ("  " + r if r else ""))
        ansi += ["", "\x1b[1mAuto-compact window:\x1b[22m  %s tokens" % fmt(window), "",
                 "\x1b[38;5;241m/context all to expand\x1b[39m"]
        self.write_local_command("context", "\n".join(ansi))
        md = "## Context Usage\n\n**Model:** %s  \n**Tokens:** %s / %s (%s)\n" % (
            self.model_id(), fmt(used), fmt(window), pct(used))
        e = self.write_entry("user", {"role": "user", "content": md}, {"isMeta": True})
        return e

    def local_result(self, cmd, text):
        self.add_block("❯ ", "  ", cmd)
        self.add(RES, "     ", text)

    # ---------------------------------------------------------- 按键
    def on_key(self, k):
        self.dirty = True
        d = self.dialog
        if d is not None:
            getattr(self, "key_" + d.kind)(k)
            self.update_status()
            return
        if k == "BTab":
            # 真机实测（v2.1.330）：manual → acceptEdits → plan → [bypassPermissions，仅带跳过权限启动] → auto → manual；
            # dontAsk 不在循环里
            order = ["manual", "acceptEdits", "plan"] + (["bypassPermissions"] if self.bypass_allowed else []) + ["auto"]
            if self.mode not in order:
                order.append(self.mode)
            self.mode = order[(order.index(self.mode) + 1) % len(order)]
            return
        if k in ("Escape", "C-c") and self.turn is not None:
            self.interrupt()
            return
        if k == "C-b":
            if self.bg_running:   # 实测：tmux send-keys 一次 C-b 即转入后台
                self.bg_flag = True
                self.turn_due = 0.0
            return
        if k == "C-x":
            self.cx_at = time.time()
            return
        if k == "C-s":
            if time.time() - self.cx_at <= CHORD_WINDOW:
                self.cx_at = 0.0
                self.send_now()
            return
        if k == "C-k":
            if time.time() - self.cx_at <= CHORD_WINDOW and self.agents:
                self.cx_at = 0.0
                if time.time() - self.kill_at <= KILL_CONFIRM_WINDOW:
                    self.kill_at = 0.0
                    self.kill_agents()
                else:
                    self.kill_at = time.time()
            return
        pop = self.popup()
        if pop and k in ("Up", "Down"):
            self.popup_cursor = (self.popup_cursor + (1 if k == "Down" else -1)) % len(pop)
            return
        if pop and k == "Tab":
            self.input = pop[self.popup_cursor][0]
            return
        if k == "Escape":
            if pop:
                self.popup_hidden = True
            return
        if k == "Enter":
            if pop:
                self.input = pop[self.popup_cursor][0]
            text = self.input
            if text.strip():
                self.input = ""
                self.popup_cursor = 0
                self.popup_hidden = False
                self.submit(text)
            return
        if k == "BSpace":
            self.input = self.input[:-1]
            self.popup_hidden = False
            return
        if k == "C-u":
            head, sep, _ = self.input.rpartition("\n")
            self.input = head + sep
            return
        if k == "C-c":
            if self.input:
                self.input = ""
            elif time.time() - self.ctrl_c_at < 2.0:
                self.exit_code = 0
            else:
                self.ctrl_c_at = time.time()
            return

    def on_text(self, s):
        self.dirty = True
        d = self.dialog
        if d is not None:
            if d.kind == "ask" and d.cursor == 2:
                d.typed += s.replace("\r", " ").replace("\n", " ")
                return
            for ch in s:
                if ch in "123456789sync ":
                    self.on_key("Space" if ch == " " else ch)
            return
        self.input += s.replace("\r\n", "\n").replace("\r", "\n")
        self.popup_hidden = False

    def resolve(self, value):
        self.dialog = None
        self.turn_send = value
        self.turn_due = 0.0
        self.dirty = True

    def key_trust(self, k):
        d = self.dialog
        if k in ("Up", "Down"):
            d.cursor = 1 - d.cursor
        elif k == "Escape" or (k == "Enter" and d.cursor == 0):
            self.exit_code = 1
        elif k == "Enter":
            self.dialog = None

    def key_perm(self, k):
        d = self.dialog
        n = len(d.options)
        if k == "Up":
            d.cursor = max(0, d.cursor - 1)
        elif k == "Down":
            d.cursor = min(n - 1, d.cursor + 1)
        elif k.isdigit() and 1 <= int(k) <= n:
            self.resolve(int(k) - 1)
        elif k == "Enter":
            self.resolve(d.cursor)
        elif k in ("Escape", "C-c"):
            self.resolve(None)

    def key_ask(self, k):
        d = self.dialog
        if k == "Up":
            d.cursor = max(0, d.cursor - 1)
        elif k == "Down":
            d.cursor = min(3, d.cursor + 1)
        elif k in ("1", "2"):
            self.resolve(ASK_Q["options"][int(k) - 1]["label"])
        elif k == "3":
            d.cursor = 2
        elif k == "4":
            self.resolve("__chat__")
        elif k == "BSpace" and d.cursor == 2:
            d.typed = d.typed[:-1]
        elif k == "Space" and d.cursor == 2:
            d.typed += " "
        elif k == "Enter":
            if d.cursor < 2:
                self.resolve(ASK_Q["options"][d.cursor]["label"])
            elif d.cursor == 2:
                if d.typed.strip():
                    self.resolve(d.typed.strip())
            else:
                self.resolve("__chat__")
        elif k in ("Escape", "C-c"):
            self.resolve(None)

    def key_model(self, k):
        d = self.dialog
        if k in ("Up", "Down"):
            d.cursor = max(0, min(len(MODELS) - 1, d.cursor + (1 if k == "Down" else -1)))
            if d.cursor < d.top:
                d.top = d.cursor
            elif d.cursor >= d.top + MODEL_WINDOW:
                d.top = d.cursor - MODEL_WINDOW + 1
        elif k in ("Left", "Right"):
            i = EFFORTS.index(d.effort) + (1 if k == "Right" else -1)
            d.effort = EFFORTS[max(0, min(len(EFFORTS) - 1, i))]
        elif k in ("s", "Enter"):
            self.dialog = None
            m = MODELS[d.cursor]
            changed = d.cursor != self.model_index() or d.effort != self.effort
            if changed:
                self.model = m["id"] if m["id"] != "default" else "default"
                self.effort = d.effort
                text = "Set model to %s with %s effort" % (m["display"], d.effort)
                if k == "s":
                    text += " (this session only)"
            else:
                text = "Kept model as %s" % m["display"]
            self.local_result("/model", text)
            self.write_local_command("model", text.replace(m["display"], "`%s`" % m["display"], 1))
        elif k in ("Escape", "C-c"):
            self.dialog = None
            text = "Kept model as %s" % self.model_display()
            self.local_result("/model", text)
            self.write_local_command("model", "Kept model as `%s`" % self.model_display())

    def key_effort(self, k):
        d = self.dialog
        if k in ("Left", "Right"):
            i = EFFORTS.index(d.level) + (1 if k == "Right" else -1)
            d.level = EFFORTS[max(0, min(len(EFFORTS) - 1, i))]
        elif k == "Tab":
            d.ultracode = not d.ultracode
        elif k == "s" and d.level == "max":
            pass   # 实测：max 档没有 s（提示行只有 Enter to use this session only）
        elif k in ("s", "Enter"):
            self.dialog = None
            self.effort = d.level
            only = k == "s" or d.level == "max"   # max 档的 Enter = 仅本会话
            text = "Set effort level to %s" % d.level + (" (this session only)" if only else "")
            self.local_result("/effort", text)
            self.write_local_command("effort", text)
        elif k in ("Escape", "C-c"):
            self.dialog = None
            self.local_result("/effort", "Cancelled")
            self.write_local_command("effort", "Cancelled")

    # ---------------------------------------------------------- 输入解析
    SEQ = {b"\x1b[A": "Up", b"\x1b[B": "Down", b"\x1b[C": "Right", b"\x1b[D": "Left", b"\x1b[Z": "BTab",
           b"\x1bOA": "Up", b"\x1bOB": "Down", b"\x1bOC": "Right", b"\x1bOD": "Left"}
    CTRL = {b"\r": "Enter", b"\n": "Enter", b"\x7f": "BSpace", b"\x08": "BSpace", b"\t": "Tab",
            b"\x03": "C-c", b"\x15": "C-u", b"\x02": "C-b", b"\x18": "C-x", b"\x13": "C-s", b"\x0b": "C-k"}

    def feed(self, buf, final=False):
        """解析输入字节，返回未消费的剩余部分。final=True 时单独的 ESC 视为 Escape 键。"""
        while buf:
            if buf.startswith(b"\x1b[200~"):
                end = buf.find(b"\x1b[201~")
                if end < 0:
                    return buf
                self.on_text(buf[6:end].decode("utf-8", "replace"))
                buf = buf[end + 6:]
                continue
            if buf[:1] == b"\x1b":
                hit = None
                for seq, name in self.SEQ.items():
                    if buf.startswith(seq):
                        hit = (seq, name)
                        break
                if hit is not None:
                    self.on_key(hit[1])
                    buf = buf[len(hit[0]):]
                    continue
                if not final and any(seq.startswith(buf) for seq in list(self.SEQ) + [b"\x1b[200~"]):
                    return buf  # 可能是不完整的转义序列，等下一批字节（或超时后按单独 Esc 处理）
                m = re.match(rb"\x1b\[[0-9;?]*[@-~]", buf)
                if m:  # 其它 CSI 序列：忽略
                    buf = buf[m.end():]
                    continue
                self.on_key("Escape")
                buf = buf[1:]
                continue
            c = buf[:1]
            if c in self.CTRL:
                self.on_key(self.CTRL[c])
                buf = buf[1:]
                continue
            if c < b" ":
                buf = buf[1:]
                continue
            n = 1
            b0 = buf[0]
            if b0 >= 0xF0:
                n = 4
            elif b0 >= 0xE0:
                n = 3
            elif b0 >= 0xC0:
                n = 2
            if len(buf) < n and not final:
                return buf
            ch = buf[:n].decode("utf-8", "replace")
            buf = buf[n:]
            if self.dialog is not None and ch == " ":
                self.on_key("Space")
            elif self.dialog is not None and len(ch) == 1 and ch in "123456789syn" and \
                    not (self.dialog.kind == "ask" and self.dialog.cursor == 2):
                self.on_key(ch)
            else:
                self.on_text(ch)
        return b""

    # ---------------------------------------------------------- 主循环
    def run(self):
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGHUP, self._on_signal)
        signal.signal(signal.SIGWINCH, lambda *a: setattr(self, "last_frame", None))
        self.write("\x1b[?25l\x1b[?2004h\x1b[2J")
        pending = b""
        pending_at = 0.0
        try:
            while self.exit_code is None:
                self.step_turn()
                if self.dirty or self.turn is not None or self.last_frame is None:
                    self.draw(force=self.last_frame is None)
                    self.dirty = False
                try:
                    r, _, _ = select.select([fd], [], [], 0.05)
                except InterruptedError:
                    continue
                if r:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    pending = self.feed(pending + data)
                    pending_at = time.time()
                elif pending and time.time() - pending_at > 0.03:
                    pending = self.feed(pending, final=True)
                self.update_status()
            self.draw()
        finally:
            self.write("\x1b[?2004l\x1b[?25h\r\n")
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            self.remove_session_json()
        return self.exit_code or 0

    def _on_signal(self, signum, frame):
        self.remove_session_json()
        os._exit(128 + signum)


def main(argv=None):
    # CCR_FAKE_BOOT：出第一帧（以及写 sessions/<pid>.json）之前等这么多秒，模拟 claude 启动慢（只用于 fake 演示/测试）
    try:
        boot = float(os.environ.get("CCR_FAKE_BOOT") or 0)
    except ValueError:
        boot = 0.0
    if 0 < boot <= 600:
        time.sleep(boot)
    app = FakeClaude(sys.argv[1:] if argv is None else argv)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
