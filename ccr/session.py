"""会话管理：驱动远端 tmux 里的**交互式** Claude Code（docs/SPEC_V2_TUI.md §4.2）。

- 每个 jobid 一个 AgentLink（ccr.agent_link，开发者 A）；本地会话 sid ↔ 远端 tmux 会话 `ccr-<sid>`。
  运行位置为本机时 jobid = remote.LOCAL_JOBID（"local"，SPEC §9）：同样一个 AgentLink（本地 node_agent --local），
  其余逻辑完全复用；本机没有 Slurm 作业，永远不会"作业已结束"，也不能与服务器作业互相续接（transcript 不在同一台机器）。
- 数据来源只有三种：屏幕（capture/screen 事件 → ccr.tui_parse）、~/.claude/sessions/<pid>.json（session 事件）、
  transcript jsonl（transcript 事件；子代理的 transcript/meta 经 subagent 事件，由 ccr.subagents 汇总）。本模块**不**做任何 headless / stream-json / control_request。
- 红线 §0.5：除用户在界面上触发的操作外绝不发送按键。所有发键路径都在 send_message / send_keys / choose /
  interrupt / cycle_mode / open_model_picker / apply_model / apply_effort / cancel_dialog / close 里，
  且导航序列每一步都基于**最新**屏幕解析结果计算，有步数上限与超时。
- app 退出（shutdown）只关闭 link，不关闭远端 claude（keeper 保活）；重启后按作业重新附着。

锁顺序：s.oplock（多步操作，跨远端调用持有）→ s.lock（字段）→ mgr._lock（注册表/事件缓冲）。
持有 s.lock / mgr._lock 时绝不做远端调用。
"""
from __future__ import annotations

import collections
import contextlib
import hashlib
import importlib
import inspect
import itertools
import json
import logging
import os
import pathlib
import queue
import re
import threading
import time
import uuid
from typing import Callable, Optional

from . import api_keys as ccr_api_keys
from . import notify as ccr_notify
from . import opprogress as ccr_op
from . import remote, stats, subagents, titles, tui_parse

log = logging.getLogger("ccr.session")

STATES = ("starting", "trust", "idle", "busy", "waiting", "exited", "error")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
EFFORT_LEVELS = tui_parse.EFFORT_LEVELS
KEYS_WHITELIST = tuple("Enter Escape Up Down Left Right Tab BTab BSpace Space C-c C-u C-b C-x C-s "
                       "1 2 3 4 5 6 7 8 9 s y n".split())
MAX_KEYS = 40
MAX_TEXT = 200000
SCOPES = ("session", "save")
# Shift+Tab 的循环顺序（claude v2.1.330 真机实测，SPEC §13）：manual → acceptEdits → plan →
# [bypassPermissions：只有以 --dangerously-skip-permissions（或 --permission-mode bypassPermissions）启动才有] → auto → manual。
# dontAsk 不在循环里（只能用 --permission-mode dontAsk 启动）。
MODE_CYCLE = ("manual", "acceptEdits", "plan", "bypassPermissions", "auto")
MAX_MODE_STEPS = 6
BLOB_TIMEOUT = 90.0   # get_blob 的远端超时（秒）
_KEEP = object()   # restart(api_key_id=…) 的"不传 = 沿用当前 API key"

_SID_RE = re.compile(r"^s[0-9a-f]{12}\Z")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_DATA_DIR = pathlib.Path("~/.cc-remote-chat").expanduser()
_SET_MODEL_RE = re.compile(r"^(?:Set model to|Kept model as)\s+`?(.+?)`?(?:\s+with\s+\w+\s+effort)?"
                           r"(?:\s+\(.*\))?\s*$")
_SET_EFFORT_RE = re.compile(r"effort(?: level)? (?:to|as|at)\s+`?(\w+)`?", re.I)


# ============================================================================ 异常
class OpError(RuntimeError):
    """操作在当前状态下不可执行（server 映射为 http_status，默认 409，body 带 code）。"""
    http_status = 409

    def __init__(self, message: str, code: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        self.api_code = code
        if status is not None:
            self.http_status = status


class LinkNotReady(OpError):
    http_status = 503

    def __init__(self, message: str = "与计算节点的连接未就绪，请稍后重试"):
        super().__init__(message, code="link_not_ready")


class SubscriptionDropped(Exception):
    """订阅者消费太慢、队列溢出被丢弃。调用方应断开连接，让客户端用最后的 gseq 重连回放。"""


_DROP = object()


class Subscription:
    def __init__(self, mgr: "SessionManager", maxsize: int):
        self._mgr = mgr
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._closed = False
        self._dropped = False

    @property
    def closed(self) -> bool:
        return self._closed or self._dropped

    @property
    def dropped(self) -> bool:
        return self._dropped

    def _offer(self, env) -> bool:
        try:
            self._q.put_nowait(env)
            return True
        except queue.Full:
            return False

    def _drop(self) -> None:
        self._dropped = True
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(_DROP)
        except queue.Full:
            pass

    def get(self, timeout=None):
        """返回下一个 envelope；超时返回 None；已 close 返回 None；被丢弃则抛 SubscriptionDropped。"""
        if self._closed:
            return None
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            if self._dropped:
                raise SubscriptionDropped("订阅者过慢已被丢弃，请重连")
            return None
        if item is _DROP:
            raise SubscriptionDropped("订阅者过慢已被丢弃，请重连")
        return item

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._mgr._unsubscribe(self)


# ============================================================================ 会话
def mode_cycle(launch: Optional[dict], seen=()) -> list:
    """本次启动后 Shift+Tab 能循环到的模式（按循环顺序）。bypassPermissions 只在以跳过权限启动（或屏幕上实际看到过）时才有。"""
    la = launch or {}
    bypass = (bool(la.get("skip_permissions")) or la.get("permission_mode") == "bypassPermissions"
              or "bypassPermissions" in (seen or ()))
    return [m for m in MODE_CYCLE if m != "bypassPermissions" or bypass]


def _place(jobid) -> str:
    """中文提示里的运行位置：本机 / 计算节点。"""
    return "本机" if remote.is_local(jobid) else "计算节点"


def _job_text(jobid) -> str:
    """中文提示里的"作业 J" / "本机"。"""
    return "本机" if remote.is_local(jobid) else "作业 %s" % jobid


def _empty_launch() -> dict:
    # api_key_id：None = 服务器默认（不加 --settings）；否则 = 本地 api_keys.json 里的条目 id（不是 key 本身）
    return {"model": None, "permission_mode": None, "skip_permissions": False, "effort": None, "api_key_id": None}


class _Session:
    def __init__(self, sid: str, max_events: int):
        self.sid = sid
        # 续接判断（--resume 只能续接"有对话"的会话；只执行过 /model 等本地命令的新会话 CLI 会报
        # "No conversation found with session ID" 并退出）：
        self.tx_loaded: set = set()    # 已从头收到 transcript 的 claude 会话 id
        self.tx_conv: set = set()      # 其中出现过真正对话（assistant 回复或用户输入）的 id
        self.tx_noconv: set = set()    # 实测 --resume 报 No conversation found 的 id
        self.title = ""
        self.jobid = ""
        self.cwd = ""
        self.launch = _empty_launch()
        self.claude_session_id: Optional[str] = None
        self.claude_pid: Optional[int] = None
        self.state = "starting"
        self.status: Optional[str] = None        # sessions/<pid>.json 的 status（idle/busy/waiting）
        self.mode: Optional[str] = None
        self.dialog_kind: Optional[str] = None
        self.model: Optional[str] = None         # 仅展示（屏幕/面板解析得到）
        self.model_desc: Optional[str] = None    # /model 面板里当前模型的描述（推断 1M 上下文用）
        self.effort: Optional[str] = None
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.error: Optional[str] = None
        self.link = "down"
        self.exit_status = None
        # 运行时
        self.attached = False      # 认为远端 tmux 会话存在（需要 watch / 重新附着）
        self.starting = False      # start 正在进行（重新附着时跳过）
        self.exited = False
        self.closing = False
        self.pane_pid: Optional[int] = None   # 当前代际的 tmux pane_pid（丢弃旧代际的迟到 exited 事件）
        self.exit_evt = threading.Event()
        self.screen_text: Optional[str] = None
        self.screen_state: Optional[dict] = None
        self.screen_ts = 0.0
        self.screen_env: Optional[dict] = None   # 最新一帧 screen 事件（不进入事件缓冲）
        self.screen_pub_text: Optional[str] = None
        self.screen_pub_t = 0.0
        self.screen_timer: Optional[threading.Timer] = None
        self.model_rows: Optional[list] = None
        # API key（显示用；明文从不放在 _Session 上）：名称与"已被删除"标记，随 api_keys.json 变更刷新
        self.api_key_name: Optional[str] = None
        self.api_key_missing = False
        self.key_noted = None   # 已写入 session_keys.json 的 (claude_session_id, api_key_id)
        self.nt_key = None      # 最近一次交给提示音（notifier.observe）的 (state, 对话框指纹)
        # 本次启动后屏幕上第一次看到的模型/effort/权限模式（基线）：重启时与当前值比较，
        # 用户在会话里改过（/model 仅本会话、Shift+Tab…）就把当前值作为 --model/--effort/--permission-mode 带上
        self.seen_base: dict = {}
        self.modes_seen: set = set()   # 本次启动后模式行上实际出现过的模式（判断循环里有没有 bypass）
        # 统计（上下文 / 花费 / token/s）：tracker 每帧更新；stats = 已发布到 meta 的快照（节流，见 _stats_update_locked）
        self.tracker = stats.StatsTracker()
        self.stats: Optional[dict] = None
        self.stats_pub_t = 0.0
        self.stats_timer: Optional[threading.Timer] = None
        # 子代理：摘要/条目在 subs（ccr.subagents.Tracker），每个子代理只把最新一条摘要事件留作回放（sa_env）
        self.subs = subagents.Tracker()
        self.sa_env: "dict[str, dict]" = {}
        # 标题：cli_title = Claude Code 自己的会话标题（/rename > 自动标题 > 首条输入，来自 transcript，见 ccr.titles）；
        # title_user = 用户在本应用里明确起过名字（True）/ 没有（False，显示 cli_title）/ 旧记录待判断（None）
        self.titles = titles.TitleTracker()
        self.cli_title: Optional[str] = None
        self.title_user: Optional[bool] = False
        # 侧栏手动排序：数字小的在上；None = 没排过（按创建时间，新的在最上）
        self.order: Optional[float] = None
        # 完成但未看（侧栏"新回复"，SPEC §15）：done_at = 最近一次确认完成的时间（与提示音同一个判定，
        # 见 _on_notify_event）；seen_at = 已看水位（用户点过 / 发消息 / 回答对话框）。unread = done_at > seen_at
        self.done_at: Optional[float] = None
        self.seen_at: Optional[float] = None
        # app 停止期间可能完成的回复：持久化时还在 busy → 重新附着后第一次稳定看到 idle 时补记 done_at（不响）
        self.done_pending = False
        self.done_pending_since: Optional[float] = None   # 满足条件的起点（monotonic）；None = 当前不满足
        # 重启 / 续接 / 新建 / 换 API 重启的阶段进度（meta.op，见 ccr.opprogress）；只在内存里，不落盘
        self.op: Optional[dict] = None
        self.op_timer: Optional[threading.Timer] = None
        self.events: "collections.deque[dict]" = collections.deque(maxlen=max_events)
        self.truncated = False
        self.last_meta = None
        self.last_persist = None
        self.lock = threading.RLock()
        self.oplock = threading.Lock()

    def display_title(self) -> str:
        if self.title_user is not False:
            return self.title
        return self.cli_title or self.title

    def unread(self) -> bool:
        return self.done_at is not None and self.done_at > (self.seen_at or 0)

    def working(self, sub_running: int) -> bool:
        """在干活：主会话在生成，或主会话已回复完但还有子代理在跑（等待对话框时算"待处理"，不算）。"""
        return self.state == "busy" or (self.state == "idle" and sub_running > 0)

    def meta(self) -> dict:
        sub_counts = self.subs.counts(self.exited)
        return {
            "sid": self.sid, "title": self.title, "jobid": self.jobid, "cwd": self.cwd,
            "display_title": self.display_title(), "cli_title": self.cli_title, "title_user": self.title_user,
            "order": self.order, "working": self.working(sub_counts["running"]),
            "done_at": self.done_at, "seen_at": self.seen_at, "unread": self.unread(),
            "launch": dict(self.launch), "claude_session_id": self.claude_session_id,
            "claude_pid": self.claude_pid, "state": self.state, "status": self.status,
            "mode": self.mode, "busy": self.state == "busy", "dialog_kind": self.dialog_kind,
            "created_at": self.created_at, "last_activity": self.last_activity, "error": self.error,
            "link": self.link, "model": self.model, "effort": self.effort, "exit_status": self.exit_status,
            "subagents": sub_counts,
            "stats": dict(self.stats) if self.stats else None,
            "api_key_name": self.api_key_name, "api_key_missing": self.api_key_missing,
            # 本次启动后 Shift+Tab 能循环到的模式；不在里面的（如没带跳过权限时的 bypass、dontAsk）需要重启才能切换
            "mode_cycle": mode_cycle(self.launch, self.modes_seen),
            "op": ccr_op.public(self.op),
        }

    def live(self) -> bool:
        return self.attached and not self.exited


def _stats_same(a: Optional[dict], b: Optional[dict]) -> bool:
    """两个 meta.stats 是否相同（忽略 updated_at）。"""
    if a is None or b is None:
        return a is b
    return {k: v for k, v in a.items() if k != "updated_at"} == {k: v for k, v in b.items() if k != "updated_at"}


_RESUME_TITLE_RE = re.compile(r"续接 [0-9a-f]{8}\Z")


def _default_like_title(title: str, cwd: str) -> bool:
    """旧记录的标题是不是程序自动起的（目录名 / "续接 xxxxxxxx" / "会话"），而不是用户自己改的名字。"""
    c = (cwd or "").rstrip("/")
    return (title in ("会话", "~", c, os.path.basename(c) or c)
            or bool(_RESUME_TITLE_RE.match(title or "")))


def _clean_title(t, default: str) -> str:
    if t is None:
        return default
    if not isinstance(t, str):
        raise ValueError("标题必须是字符串")
    t = _CTRL_RE.sub(" ", t).strip()[:200]
    return t or default


def _dialog_sig(d: Optional[dict]):
    if not d:
        return None
    return (d.get("kind"), d.get("title"), tuple(o.get("label") for o in d.get("options") or []))


# ---------------------------------------------------------------------------- 对话框指纹 / 发键守卫
FP_KEYS = ("kind", "title", "question", "preview", "labels")


def _notify_sig(d: Optional[dict]):
    """提示音去重用的对话框指纹：种类、标题、问题、预览、选项（不含自由输入项——用户打字会改变它的文字）。"""
    if not d:
        return None
    fp = dialog_fingerprint(d)
    free = _free_indexes(d)
    return (fp["kind"], fp["title"], fp["question"], tuple(fp["preview"]),
            tuple(x for i, x in enumerate(fp["labels"]) if i not in free))


def _free_indexes(d: Optional[dict]) -> frozenset:
    """自由输入项（"Type something."）的 index：光标移上去/打字后其文字会变，比对指纹时不看它的 label。"""
    return frozenset(o.get("index") for o in (d or {}).get("options") or [] if o.get("free_text"))


def dialog_fingerprint(d: Optional[dict]) -> Optional[dict]:
    """对话框指纹 {kind,title,question,preview,labels}（前端 choose 时原样带回 expect）。"""
    if not d:
        return None
    return {"kind": d.get("kind") or "", "title": d.get("title") or "", "question": d.get("question") or "",
            "preview": [str(x) for x in d.get("preview") or []],
            "labels": [str(o.get("label") or "") for o in d.get("options") or []]}


def _fp_norm(v):
    if isinstance(v, str):
        return " ".join(v.split())
    if isinstance(v, list):
        return [_fp_norm(x) for x in v]
    return v


def fingerprint_matches(d: Optional[dict], expect: dict, free=frozenset()) -> bool:
    """当前对话框 d 与指纹 expect 是否一致（只比 expect 里给出的键；空白差异忽略；自由输入项的 label 不比）。"""
    fp = dialog_fingerprint(d)
    if fp is None:
        return False
    for k in FP_KEYS:
        if k not in expect:
            continue
        a, b = _fp_norm(fp[k]), _fp_norm(expect[k])
        if k == "labels":
            if not isinstance(b, list) or len(a) != len(b):
                return False
            a = [x for i, x in enumerate(a) if i not in free]
            b = [x for i, x in enumerate(b) if i not in free]
        if a != b:
            return False
    return True


def validate_expect(expect) -> Optional[dict]:
    """校验前端带来的对话框指纹；None = 未提供。"""
    if expect is None:
        return None
    if not isinstance(expect, dict) or not set(expect) <= set(FP_KEYS) or "kind" not in expect \
            or "labels" not in expect:
        raise ValueError("expect 必须是对话框指纹 {kind,title,question,preview,labels}")
    for k in ("kind", "title", "question"):
        v = expect.get(k, "")
        if not isinstance(v, str) or len(v) > 4000:
            raise ValueError("expect.%s 不合法" % k)
    for k, lim in (("preview", 400), ("labels", 60)):
        v = expect.get(k, [])
        if not isinstance(v, list) or len(v) > lim or not all(isinstance(x, str) and len(x) <= 4000 for x in v):
            raise ValueError("expect.%s 不合法" % k)
    return expect


def _screen_lines(text: Optional[str]) -> list:
    """与 node_agent.screen_lines 相同的规范化（rstrip、NBSP→空格、去掉末尾空行、裁掉右侧 diff 面板）：
    tail sha1 / guard.lines 都按这样的行计算，agent 发键前按同样的行复核。"""
    return tui_parse.screen_lines(text)


def _dialog_tail_guard(text: Optional[str]) -> Optional[dict]:
    """{"tail": {"n", "sha1"}}：对话框区域（起点到屏幕底部）的指纹；agent 在发 Enter 前重抓屏比对，
    对话框哪怕被换成另一个也不会按下去。找不到对话框区域返回 None。"""
    lines = _screen_lines(text)
    if tui_parse._find_input_box(lines) is not None:
        return None
    ds = tui_parse._find_dialog_start(lines)
    if ds is None:
        return None
    tail = lines[ds:]
    return {"tail": {"n": len(tail), "sha1": hashlib.sha1("\n".join(tail).encode("utf-8", "replace")).hexdigest()}}


def _raw_line(text: Optional[str], stripped: str) -> Optional[str]:
    """屏幕上（规范化后）strip() 等于 stripped 的最后一行。"""
    for l in reversed(_screen_lines(text)):
        if l.strip() == stripped:
            return l
    return None


def _busy_hidden_by_input(st: dict, status: Optional[str]) -> bool:
    """屏幕解析说"不忙"，但其实只是输入框里有文字把 "esc to interrupt" 挡掉了？

    真机（v2.1.331 实测）：输入框里有文字时，模式行不显示 "esc to interrupt"（此时 Esc 是清空输入框），即使正在生成。
    忙时发消息要先粘贴、确认后再回车，这几帧只看模式行就成了"空闲"→ 前端占位文字 / 中断按钮 / 实时草稿 / 侧栏状态
    每发一条就闪一下（用户报告的"排队发消息时输入框一直抖"）。此时改用别的证据：排队提示、后台提示只在一轮进行中出现；
    claude 自己的 session 状态（~/.claude/sessions/<pid>.json 的 status）是 busy 且画面上没有本轮的结束行；
    读不到 session 状态时退回看 spinner。"""
    if st.get("busy") or st.get("dialog"):
        return False
    inp = st.get("input") or {}
    if not inp.get("visible") or not (inp.get("text") or "").strip():
        return False
    hn = st.get("hints") or {}
    if hn.get("queued") or hn.get("send_now") or hn.get("background"):
        return True
    if st.get("turn_done"):
        return False
    if status:
        return status == "busy"
    return bool(st.get("spinner"))


def _input_matches(itext: str, text: str) -> bool:
    """粘贴后输入框里的文字是否就是刚粘贴的内容（忽略空白/折行；大段粘贴会被折叠成 [Pasted text #1 …]）。"""
    a = re.sub(r"\s+", "", itext or "")
    b = re.sub(r"\s+", "", text or "")
    if not a:
        return False
    return a == b or "[Pastedtext" in a or (len(a) >= 16 and a in b)


_NO_CONV_RE = re.compile(r"No conversation found with session ID:?\s*([0-9a-fA-F-]{36})")


def _is_conversation_entry(e) -> bool:
    """transcript 条目是否算"真正的对话"：assistant 回复，或用户真正输入的内容
    （排除 isMeta、本地 slash 命令及其输出如 /model 的 <command-name>/<local-command-stdout>）。"""
    if not isinstance(e, dict) or e.get("isSidechain"):
        return False
    t = e.get("type")
    if t == "assistant":
        return True
    if t != "user" or e.get("isMeta"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") in ("text", "image", "tool_result")
                   and not str(b.get("text") or "").lstrip().startswith(("<command-", "<local-command"))
                   for b in c)
    return isinstance(c, str) and bool(c.strip()) and not c.lstrip().startswith(("<command-", "<local-command"))


def _slash_target(text: str) -> Optional[str]:
    """以 / 开头的消息：补全列表里应当高亮的命令（第一个词）。"""
    t = (text or "").lstrip()
    if not t.startswith("/"):
        return None
    m = re.match(r"/[\w:.\-]{1,64}", t)
    return m.group(0) if m else None


def _popup_selected(st: dict) -> Optional[str]:
    for it in st.get("popup") or []:
        if it.get("selected"):
            return it.get("cmd")
    return None


def _default_link_factory(cfg, jobid, on_event, on_state):
    mod = importlib.import_module("ccr.agent_link")
    return mod.AgentLink(cfg, jobid, on_event, on_state)


# ============================================================================ 管理器
class SessionManager:
    def __init__(self, get_config: Callable[[], dict], data_dir: pathlib.Path,
                 max_events_per_session=50000, link_factory=None, autostart=True, api_keys=None, notifier=None):
        self._get_config = get_config
        # 提示音（ccr.notify.Notifier；app.py 传入）。不传 = 不发声（测试等），但"完成"判定照常（侧栏"新回复"）
        self.notifier = notifier if notifier is not None else ccr_notify.NullNotifier()
        add = getattr(self.notifier, "add_listener", None)
        if callable(add):
            add(self._on_notify_event)
        self.data_dir = pathlib.Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sessions_path = self.data_dir / "sessions.json"
        # 已保存的 API key（<数据目录>/api_keys.json）；server 的 /api/api_keys 用同一个实例
        self.api_keys = api_keys if api_keys is not None else ccr_api_keys.ApiKeyStore(
            self.data_dir / "api_keys.json")
        # claude_session_id → 上次用的 api_key_id（"从历史续接"默认选中同一个 key）
        self.session_keys = ccr_api_keys.SessionKeyMap(self.data_dir / "session_keys.json")
        self._max_events = int(max_events_per_session)
        self._link_factory = link_factory or _default_link_factory
        self._lock = threading.RLock()
        self._gseq = 0
        self._global: "collections.deque[dict]" = collections.deque(maxlen=max(1000, self._max_events * 2))
        self._sessions: "dict[str, _Session]" = {}
        self._subs: list = []
        self._links: dict = {}
        self._link_ready: "dict[str, threading.Event]" = {}
        self._link_err: "dict[str, Optional[str]]" = {}
        self._reattach_locks: "dict[str, threading.Lock]" = {}
        self._ended_jobs: set = set()   # 已确认结束的作业（squeue 查不到）：link 已关闭，不再重连
        self._shutdown = False
        # 可调参数（测试可改小）
        self.screen_interval = 0.25      # 每会话 screen 事件最多 4 次/秒
        self.stats_interval = 1.0        # meta.stats 变化时最多每秒推一次 meta
        self.poll_interval = 0.1         # 等待屏幕变化时的抓屏间隔
        self.step_timeout = 3.0          # 单步按键后等待屏幕响应
        self.panel_timeout = 10.0        # 等 /model、/effort 面板出现
        self.result_timeout = 5.0        # 等面板关闭/结果行
        self.max_nav_steps = 24
        self.max_effort_steps = 8
        self.max_clear_steps = 12
        self.max_mode_steps = MAX_MODE_STEPS   # 选择权限模式：最多按这么多次 Shift+Tab
        self.op_lock_timeout = 15.0
        self.exit_timeout = 15.0
        self.link_timeout = 90.0
        self.keeper_timeout = 120.0
        self.connect_grace = 8.0         # link 正在连接时，用户操作最多等这么久
        self.resume_done_delay = 3.0     # app 停止期间完成的回复：重新附着后 idle 稳定这么久才补记"新回复"
        # 阶段进度（meta.op）：就绪后短暂显示"已就绪"；失败/仍未就绪的提示保留一段时间（新的操作会替换它）
        self.ready_timeout = 90.0        # watch 之后这么久仍没出现输入框/对话框 → op 记为 stalled（不再转圈）
        self.op_done_linger = 3.0
        self.op_fail_linger = 600.0
        self._op_ids = itertools.count(1)
        self.sub_queue_size = 20000
        # 持久化
        self._persist_lock = threading.Lock()
        self._dirty = threading.Event()
        self._persist_stop = False
        self._load()
        for s in self._sessions.values():
            self._refresh_api_key_locked(s)
        self.api_keys.add_listener(self._on_api_keys_changed)
        self._persister = threading.Thread(target=self._persist_loop, name="ccr-persist", daemon=True)
        self._persister.start()
        if autostart:
            jobs = sorted({s.jobid for s in self._sessions.values() if s.live() and s.jobid})
            for j in jobs:
                try:
                    self._ensure_link(j)
                except Exception:  # noqa: BLE001
                    log.exception("连接作业 %s 失败", j)

    # ================================================================ 订阅 / 事件
    def subscribe(self, since: Optional[int] = None) -> Subscription:
        sub = Subscription(self, self.sub_queue_size)
        with self._lock:
            if since is not None:
                for env in self._events_since_locked(int(since)):
                    if not sub._offer(env):
                        sub._drop()
                        return sub
            self._subs.append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def _events_since_locked(self, gseq: int) -> list:
        out = []
        buf = self._global
        if buf:
            first = buf[0]["gseq"]
            start = max(0, gseq - first + 1)
            if start < len(buf):
                out = [e for e in itertools.islice(buf, start, None) if e["sid"] in self._sessions]
        latest = [s.screen_env for s in self._sessions.values()
                  if s.screen_env is not None and s.screen_env["gseq"] > gseq]
        latest += [e for s in self._sessions.values() for e in s.sa_env.values() if e["gseq"] > gseq]
        if latest:
            out = sorted(out + latest, key=lambda e: e["gseq"])
        return out

    def events_since(self, gseq: int) -> list:
        with self._lock:
            return self._events_since_locked(int(gseq))

    def replay_since(self, gseq: int):
        """(gseq 之后可回放的事件, 是否存在缺口)。screen 事件只回放每个会话的最新一帧；
        subagent 事件只回放每个子代理的最新摘要（不含条目，客户端按 total/gen 发现缺口后 GET 补齐）。"""
        gseq = int(gseq)
        with self._lock:
            buf = self._global
            first = buf[0]["gseq"] if buf else self._gseq + 1
            gap = gseq < self._gseq and gseq < first - 1
            return self._events_since_locked(gseq), gap

    def session_events(self, sid) -> list:
        return self.session_events_ex(sid)[0]

    def session_events_ex(self, sid):
        s = self._get(sid)
        with self._lock:
            evs = list(s.events)
            extra = list(s.sa_env.values())
            if s.screen_env is not None:
                extra.append(s.screen_env)
            if extra:
                evs = sorted(evs + extra, key=lambda e: e["gseq"])
            return evs, bool(s.truncated)

    @property
    def gseq(self) -> int:
        with self._lock:
            return self._gseq

    def _publish(self, s: _Session, kind: str, data: dict) -> dict:
        with self._lock:
            self._gseq += 1
            env = {"gseq": self._gseq, "sid": s.sid, "ts": time.time(), "kind": kind, "data": data}
            registered = self._sessions.get(s.sid) is s
            if kind == "screen":
                if registered:
                    s.screen_env = env
            elif kind == "subagent":
                # 实时事件带条目；回放只留每个子代理最新的摘要（不带条目）
                if registered:
                    aid = data.get("agent_id")
                    if data.get("removed"):
                        s.sa_env.pop(aid, None)
                    else:
                        s.sa_env[aid] = dict(env, data=dict(data, entries=[], **{"from": data.get("total", 0)},
                                                            reset=False))
            else:
                self._global.append(env)
                if registered:
                    if kind == "transcript" and data.get("reset"):
                        s.events = collections.deque((e for e in s.events if e["kind"] != "transcript"),
                                                     maxlen=s.events.maxlen)
                        s.sa_env = {}
                    if s.events.maxlen is not None and len(s.events) >= s.events.maxlen:
                        s.truncated = True
                    s.events.append(env)
            if not registered:
                return env
            dead = [sub for sub in self._subs if not sub._offer(env)]
            for sub in dead:
                self._subs.remove(sub)
                sub._drop()
                log.warning("subscriber too slow, dropped")
            return env

    def _app(self, s: _Session, data: dict) -> None:
        self._publish(s, "app", data)

    def _notice(self, s: _Session, level: str, text: str) -> None:
        self._app(s, {"type": "notice", "level": level, "text": text})

    def _notify_locked(self, s: _Session) -> None:
        """调用方持有 s.lock。状态或对话框变化时交给提示音状态机（只更新内存、可能排一个确认定时器，不做 I/O）。"""
        st = s.screen_state or {}
        d = st.get("dialog")
        sig = _notify_sig(d)
        state = s.state
        if state == "idle" and s.subs.counts(s.exited)["running"] > 0:
            state = "busy"   # 主会话回复完了但子代理还在跑：仍算在干活，全部结束才响"完成"
        if s.done_pending:
            self._pending_done_locked(s, state)
        key = (state, sig)
        if key == s.nt_key:
            return
        s.nt_key = key
        try:
            self.notifier.observe(s.sid, s.jobid, state, (d or {}).get("kind"), sig, bool(st.get("interrupted")))
        except Exception:  # noqa: BLE001  提示音出错不能影响会话
            log.exception("提示音状态机出错")

    # ---------------------------------------------------------------- 完成但未看（侧栏"新回复"）
    def _on_notify_event(self, sid: str, kind: str) -> None:
        """Notifier 回调（定时器线程、Notifier 锁之外）：本轮完成确认 → 记 done_at（与提示音同一个判定）。"""
        if kind != "done":
            return   # "需要操作"（对话框）不算新回复：侧栏按实时状态显示"待处理"
        with self._lock:
            s = self._sessions.get(sid)
        if s is None or self._shutdown:
            return
        with s.lock:
            if s.exited:
                return
            self._mark_done_locked(s)
        self._emit_meta(s)

    @staticmethod
    def _mark_done_locked(s: _Session) -> None:
        now = time.time()
        if s.done_at is not None and now <= s.done_at:
            now = s.done_at + 0.001   # 时钟回拨也保证单调递增（否则新的一次完成可能被旧的已看水位盖住）
        s.done_at = now
        s.done_pending = False
        s.done_pending_since = None

    def _pending_done_locked(self, s: _Session, state: str) -> None:
        """调用方持有 s.lock。app 停止期间完成的回复：持久化时还在 busy 的会话，重新附着后（transcript 已从头收到、
        有效状态 = idle、没有子代理在跑）稳定 resume_done_delay 秒 → 补记 done_at（不响）。中途又忙起来就等它真正结束
        （届时提示音状态机若确认完成也会记，二者都只是设置 done_at）。退出 / 出错 = 放弃。"""
        if state in ("exited", "error"):
            s.done_pending = False
            s.done_pending_since = None
            return
        if state != "idle" or not s.tx_loaded:
            s.done_pending_since = None
            return
        if s.done_pending_since is not None:
            return
        s.done_pending_since = time.monotonic()
        if self._shutdown:
            return
        t = threading.Timer(self.resume_done_delay, self._pending_done_fire, (s,))
        t.daemon = True
        t.start()

    def _pending_done_fire(self, s: _Session) -> None:
        with s.lock:
            since = s.done_pending_since
            if (self._shutdown or not s.done_pending or since is None
                    or time.monotonic() - since < self.resume_done_delay - 0.05):
                return   # 期间状态变过（起点被清空或重设；重设时另有定时器）
            if s.exited or s.state != "idle" or s.subs.counts(s.exited)["running"] > 0:
                return
            self._mark_done_locked(s)
        log.info("会话 %s：app 停止期间完成的回复，标记为新回复", s.sid)
        self._emit_meta(s)

    @staticmethod
    def _clear_unread_locked(s: _Session) -> None:
        """调用方持有 s.lock。用户在这个会话里操作（发消息 / 回答对话框 / 中断…）：已看水位推到当前 done_at。"""
        s.done_pending = False
        s.done_pending_since = None
        if s.unread():
            s.seen_at = s.done_at

    def _user_engaged(self, s: _Session) -> None:
        with s.lock:
            changed = s.unread() or s.done_pending
            self._clear_unread_locked(s)
        if changed:
            self._emit_meta(s)

    @staticmethod
    def _check_ts(v, name: str) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v < 1e11):
            raise ValueError("%s 必须是有效的时间戳（秒）" % name)
        return float(v)

    def mark_seen(self, sid, done_at) -> dict:
        """POST /seen：已看水位只往前推，推到客户端看到的 done_at（不超过服务器当前的 done_at）。
        用客户端看到的值而不是"现在"：客户端还没渲染出来的新一次完成不会被一起盖掉。"""
        s = self._get(sid)
        v = self._check_ts(done_at, "done_at")
        with s.lock:
            if s.done_at is not None:
                upto = min(v, s.done_at)
                if s.seen_at is None or upto > s.seen_at:
                    s.seen_at = upto
            res = {"ok": True, "done_at": s.done_at, "seen_at": s.seen_at, "unread": s.unread()}
        self._emit_meta(s)
        return res

    def mark_unseen(self, sid) -> dict:
        """POST /unseen（右键「标为未读」）：已看水位退到 done_at 之前一点。没完成过的会话不能标。"""
        s = self._get(sid)
        with s.lock:
            if s.done_at is None:
                raise ValueError("这个会话还没有完成过回复，不能标为未读")
            s.seen_at = s.done_at - 0.001
            res = {"ok": True, "done_at": s.done_at, "seen_at": s.seen_at, "unread": s.unread()}
        self._emit_meta(s)
        return res

    def _update_cli_title_locked(self, s: _Session, reset: bool) -> None:
        """调用方持有 s.lock。transcript 变化后刷新 cli_title；旧记录（title_user 未知）此时判断标题是不是自己起的。"""
        t = s.titles.title()
        if t or reset:
            s.cli_title = t
        if s.title_user is None and s.cli_title:
            base = (s.title or "").rstrip("…").strip()
            s.title_user = not (base and s.cli_title.startswith(base))

    def _emit_meta(self, s: _Session, force=False) -> None:
        with s.lock:
            self._notify_locked(s)
            self._op_track_locked(s)
            m = s.meta()
            cmp = dict(m)
            cmp.pop("last_activity", None)
            if not force and cmp == s.last_meta:
                return
            s.last_meta = cmp
            self._app(s, {"type": "meta", "meta": m})
            # 生成中每秒都会变的 tok_s_live / updated_at 不值得重写 sessions.json（其余变化照常落盘）
            pk = dict(cmp)
            pk.pop("op", None)   # 阶段进度只在内存里（不为它重写 sessions.json）
            if pk.get("stats"):
                pk["stats"] = {k: v for k, v in pk["stats"].items() if k not in ("tok_s_live", "updated_at")}
            persist = force or pk != s.last_persist
            s.last_persist = pk
        if persist:
            self._dirty.set()

    # ================================================================ 阶段进度（meta.op，ccr.opprogress）
    # 只在本应用这侧报告"做到哪一步"（经现有的 meta 推送）；不改变 claude 的启动参数、环境、按键。
    def _op_host(self) -> str:
        h = (self._get_config() or {}).get("host")
        return h if isinstance(h, str) and h else "服务器"

    @staticmethod
    def _op_live(s: _Session, op_id=None) -> Optional[dict]:
        """调用方持有 s.lock。进行中的 op（op_id 给定时必须是同一个）。"""
        op = s.op
        if op is None or op.get("state") != ccr_op.RUNNING:
            return None
        if op_id is not None and op.get("id") != op_id:
            return None
        return op

    def _op_timer_locked(self, s: _Session, delay: Optional[float], fn, *args) -> None:
        if s.op_timer is not None:
            s.op_timer.cancel()
            s.op_timer = None
        if delay is None:
            return
        t = threading.Timer(max(0.0, delay), fn, args)
        t.daemon = True
        s.op_timer = t
        t.start()

    def _op_begin_locked(self, s: _Session, kind: str, ids: list, jobid: str, resume=None,
                         started_at=None, detail=None) -> dict:
        self._op_timer_locked(s, None, None)
        s.op = ccr_op.new_op(next(self._op_ids), kind, ids, local=remote.is_local(jobid), host=self._op_host(),
                             jobid=jobid, resume=resume, started_at=started_at, detail=detail)
        return s.op

    def _op_link_ids(self, jobid: str) -> list:
        with self._lock:
            link = self._links.get(jobid)
        st = None if link is None else self._link_state(jobid)
        return ccr_op.link_phases(remote.is_local(jobid), st)

    def _op_phase(self, s: _Session, op_id, pid: str, detail=None) -> None:
        with s.lock:
            op = self._op_live(s, op_id)
            changed = op is not None and ccr_op.set_phase(op, pid, detail=detail)
            if changed and pid == "ready":
                self._op_timer_locked(s, self.ready_timeout, self._op_ready_timeout, s, op["id"])
        if changed:
            self._emit_meta(s)

    def _op_note(self, s: _Session, detail: Optional[str], op_id=None) -> None:
        """更新当前阶段的补充说明（没有进行中的 op 时什么都不做：close/remove 也走同样的代码）。"""
        with s.lock:
            op = self._op_live(s, op_id)
            if op is None or op.get("detail") == detail:
                return
            op["detail"] = detail
        self._emit_meta(s)

    def _op_finish(self, s: _Session, op_id, state: str, error=None, code=None, detail=None) -> None:
        with s.lock:
            op = self._op_live(s, op_id)
            if op is None:
                return
            ccr_op.finish(op, state, error=error, code=code, detail=detail)
            self._op_finish_timer_locked(s, op)
        self._emit_meta(s)

    def _op_finish_timer_locked(self, s: _Session, op: dict) -> None:
        st = op.get("state")
        linger = self.op_done_linger if st == "done" else self.op_fail_linger if st in ("failed", "stalled") else None
        self._op_timer_locked(s, linger, self._op_expire, s, op["id"])

    def _op_clear(self, s: _Session, op_id=None) -> None:
        with s.lock:
            if s.op is None or (op_id is not None and s.op.get("id") != op_id):
                return
            s.op = None
            self._op_timer_locked(s, None, None)
        self._emit_meta(s)

    def _op_expire(self, s: _Session, op_id) -> None:
        with s.lock:
            op = s.op
            if op is None or op.get("id") != op_id or op.get("state") == ccr_op.RUNNING:
                return
            s.op = None
            s.op_timer = None
        self._emit_meta(s)

    def _op_ready_timeout(self, s: _Session, op_id) -> None:
        with s.lock:
            op = self._op_live(s, op_id)
            if op is None or op.get("phase") != "ready":
                return
            s.op_timer = None
        self._op_finish(s, op_id, "stalled",
                        detail="claude 已启动，但 %d 秒后仍未出现输入框；请打开终端面板看看它停在哪里"
                               % int(self.ready_timeout))

    def _op_track_locked(self, s: _Session) -> None:
        """调用方持有 s.lock（_emit_meta 里）：等待就绪阶段根据会话状态收尾。
        出现输入框（idle/busy）或其它对话框（waiting）= 就绪；信任框 = 结束进度并提示用户确认（绝不自动回答）。"""
        op = s.op
        if op is None:
            return
        if op.get("state") == ccr_op.RUNNING and op.get("phase") == "ready":
            if s.state in ("idle", "busy", "waiting"):
                ccr_op.finish(op, "done", detail="claude 已启动，正在等待你处理对话框" if s.state == "waiting" else None)
            elif s.state == "trust":
                ccr_op.finish(op, "trust", detail="等待你在上方确认信任目录")
            else:
                return
            self._op_finish_timer_locked(s, op)
        elif op.get("state") == "trust" and s.state != "trust":
            s.op = None
            self._op_timer_locked(s, None, None)

    def _op_sync_link(self, s: _Session, op_id, jobid: str, st: Optional[str]) -> None:
        """等待连接时按 link 状态推进 probe → connect，连不上时写明第几次失败、原因（link 会自动退避重连）。"""
        if op_id is None:
            return
        with self._lock:
            err = self._link_err.get(jobid)
        local = remote.is_local(jobid)
        changed = False
        with s.lock:
            op = self._op_live(s, op_id)
            if op is None or st not in ("down", "connecting"):
                return
            n = int(op.get("retries") or 0)   # 失败次数由 _on_link_state 计（每次 link 报告 down + 原因）
            why = ccr_op.short_reason(op.get("_last_err"), host=self._op_host(), jobid=jobid, local=local) if n else ""
            if st == "connecting":
                pid, detail = "connect", ("第 %d 次连接失败（%s），正在重试…" % (n, why) if n else None)
            elif err or local or op.get("phase") == "connect":
                pid, detail = "connect", ("第 %d 次连接失败（%s），稍后自动重试" % (n, why) if n else None)
            else:
                pid, detail = "probe", None
            changed = ccr_op.set_phase(op, pid, detail=detail) or changed
        if changed:
            self._emit_meta(s)

    def _op_fail_from(self, s: _Session, op_id, e, jobid: str, timeout: float = 0) -> str:
        """阶段失败：op 写清原因（短句），返回给用户看的那句话（同时用于 s.error / 提示）。"""
        raw = str(e) or type(e).__name__
        with s.lock:
            op = self._op_live(s, op_id)
            pid = op.get("phase") if op else None
        code = getattr(e, "api_code", None)
        text = ccr_op.describe_failure(pid, raw, host=self._op_host(), jobid=jobid, local=remote.is_local(jobid),
                                       timeout=timeout if pid in ("probe", "connect") else 0, code=code)
        self._op_finish(s, op_id, "failed", error=text,
                        code="job_ended" if (code == "job_ended" or "作业已结束" in text) else code)
        return text

    # ================================================================ 查询
    def _get(self, sid) -> _Session:
        with self._lock:
            s = self._sessions.get(sid)
        if s is None:
            raise KeyError(sid)
        return s

    def list_sessions(self) -> list:
        with self._lock:
            ss = list(self._sessions.values())
        out = []
        for s in ss:
            with s.lock:
                out.append(s.meta())
        out.sort(key=lambda m: m["created_at"])
        return out

    def get_meta(self, sid) -> dict:
        s = self._get(sid)
        with s.lock:
            return s.meta()

    def get_screen(self, sid) -> dict:
        """最新一帧（缓存；没有缓存且会话存活时现抓一帧）。"""
        s = self._get(sid)
        with s.lock:
            text, st = s.screen_text, s.screen_state
        if text is None and s.live() and not s.starting:
            try:
                st = self._fresh(s)
                with s.lock:
                    text = s.screen_text
            except OpError:
                pass
        if st is None:
            st = tui_parse.parse_screen(text or "")
        return {"text": text or "", "state": st, "ts": s.screen_ts or None}

    # ================================================================ AgentLink
    def _control_dir(self, cfg: Optional[dict] = None):
        cd = (cfg or {}).get("control_dir")
        if cd:
            return pathlib.Path(cd)
        return None if self.data_dir == _DEFAULT_DATA_DIR else self.data_dir / "ssh"

    def _remote_cfg(self) -> dict:
        cfg = dict(self._get_config() or {})
        cd = self._control_dir(cfg)
        if cd is not None:
            cfg["control_dir"] = str(cd)
        return cfg

    def _ensure_link(self, jobid: str):
        with self._lock:
            if self._shutdown:
                raise OpError("服务正在关闭")
            link = self._links.get(jobid)
            if link is not None:
                return link
            self._ended_jobs.discard(jobid)   # 用户又要在这个作业上操作：让新 link 重新核实一次
            self._link_ready[jobid] = threading.Event()
            self._link_err[jobid] = None
            holder: dict = {}   # 回调里据此识别"已被替换/关闭的旧 link"，丢弃它的迟到回调
            link = self._link_factory(
                self._remote_cfg(), jobid,
                lambda ev, j=jobid, h=holder: self._on_link_event(j, ev, h.get("link")),
                lambda st, err=None, j=jobid, h=holder: self._on_link_state(j, st, err, h.get("link")))
            holder["link"] = link
            self._links[jobid] = link
        link.start()
        self._sync_link_state(jobid)
        return link

    def _link_state(self, jobid: str) -> str:
        with self._lock:
            link = self._links.get(jobid)
            ended = jobid in self._ended_jobs
        if link is None:
            return "ended" if ended else "down"
        st = getattr(link, "state", "connecting")
        return st if st in ("connecting", "ready", "down", "ended") else "connecting"

    def _stale_link(self, jobid: str, link) -> bool:
        with self._lock:
            return link is not None and self._links.get(jobid) is not link

    def _job_ended(self, jobid: str) -> Optional[bool]:
        """作业是否已结束：True/False/None(不确定)。link 连不上时由用户操作触发一次同步 squeue 核实。"""
        with self._lock:
            if jobid in self._ended_jobs:
                return True
            link = self._links.get(jobid)
        if link is None:
            return None
        if getattr(link, "state", None) == "ended":
            return True
        fn = getattr(link, "check_job", None)
        if fn is None:
            return None
        try:
            return fn()
        except Exception:  # noqa: BLE001
            log.exception("查询作业 %s 状态失败", jobid)
            return None

    def _maybe_close_link(self, jobid: str) -> None:
        """该作业已没有存活/启动中的会话：关闭 link（ccr-agent step 随之结束），不再保持连接。"""
        with self._lock:
            if self._shutdown:
                return
            if any(x.jobid == jobid and (x.live() or x.starting) for x in self._sessions.values()):
                return
            link = self._links.pop(jobid, None)
            self._link_ready.pop(jobid, None)
            self._link_err.pop(jobid, None)
        if link is not None:
            threading.Thread(target=self._close_link_quiet, args=(link,), name="ccr-link-close-" + jobid,
                             daemon=True).start()
            self._sync_link_state(jobid)

    @staticmethod
    def _close_link_quiet(link) -> None:
        try:
            link.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭 agent 连接失败")

    def _on_job_ended(self, jobid: str, err=None) -> None:
        """link 报告作业已结束（squeue 查不到 / Invalid job id）：存活会话标为已退出（可在新作业上 --resume），
        关闭并移除 link（不再重连）。"""
        with self._lock:
            link = self._links.get(jobid)
            if link is not None and getattr(link, "state", None) == "ended":
                self._links.pop(jobid, None)
                self._link_ready.pop(jobid, None)
            else:
                link = None
            self._ended_jobs.add(jobid)
            ss = [s for s in self._sessions.values() if s.jobid == jobid]
        for s in ss:
            with s.lock:
                was_live = s.live() and not s.starting
                s.link = "ended"
                op = self._op_live(s)
                op_id = op["id"] if op is not None and op.get("jobid") == jobid else None
            if op_id is not None:   # 正在这个作业上重启/续接：进度里写明作业已结束（之后的启动失败不再覆盖）
                self._op_finish(s, op_id, "failed", code="job_ended",
                                error=ccr_op.describe_failure(None, err or "", host=self._op_host(), jobid=jobid,
                                                              local=False, code="job_ended"))
            if was_live:
                self._mark_exited(s, None)
                with s.lock:
                    s.attached = False
                self._notice(s, "warn", "作业 %s 已结束，claude 已随作业退出；可在新作业上用 --resume 续接本会话"
                             % jobid)
            self._emit_meta(s)
        self._dirty.set()
        if link is not None:
            threading.Thread(target=self._close_link_quiet, args=(link,), name="ccr-link-close-" + jobid,
                             daemon=True).start()

    def _sync_link_state(self, jobid: str) -> None:
        st = self._link_state(jobid)
        with self._lock:
            ss = [s for s in self._sessions.values() if s.jobid == jobid]
        for s in ss:
            with s.lock:
                s.link = st
            self._emit_meta(s)

    def _wait_link(self, jobid: str, timeout: float, s: Optional[_Session] = None, op_id=None):
        link = self._ensure_link(jobid)
        with self._lock:
            ev = self._link_ready.get(jobid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._shutdown:
                raise OpError("服务正在关闭")
            st = getattr(link, "state", None)
            if s is not None:
                self._op_sync_link(s, op_id, jobid, st)
            if st == "ready":
                return link
            with self._lock:
                gone = jobid in self._ended_jobs   # link 已因作业结束被关闭/移除（state 可能已不是 ended）
            if st == "ended" or gone:
                raise OpError("作业 %s 已结束，请在新的作业上启动/续接" % jobid, code="job_ended")
            if ev is not None:
                ev.wait(min(0.5, max(0.01, deadline - time.monotonic())))
        err = self._link_err.get(jobid)
        if remote.is_local(jobid):
            raise LinkNotReady("连接本机 agent 超时%s" % ("：" + err if err else ""))
        raise LinkNotReady("连接计算节点超时（作业 %s）%s" % (jobid, "：" + err if err else ""))

    def _on_link_state(self, jobid: str, state: str, err=None, link=None) -> None:
        if self._stale_link(jobid, link):
            return   # 已被关闭/替换的旧 link 的迟到回调
        if state == "ended":
            self._on_job_ended(jobid, err)
            return
        with self._lock:
            ev = self._link_ready.get(jobid)
            self._link_err[jobid] = err
        if ev is not None:
            if state == "ready":
                ev.set()
            else:
                ev.clear()
        if state == "down" and err:
            log.warning("%s 的 agent 连接断开：%s", _job_text(jobid), err)
        with self._lock:
            ss = [s for s in self._sessions.values() if s.jobid == jobid]
        if state in ("down", "connecting"):
            for s in ss:   # 断线 / 重连中：重连后的首次状态同步不响（旧屏幕状态 → 新首帧不算一次"完成"）
                self.notifier.reset(s.sid)
        for s in ss:
            with s.lock:
                s.link = state if state in ("connecting", "ready", "down") else "connecting"
                op = self._op_live(s)
                if op is not None and state == "down" and err and op.get("jobid") == jobid:
                    op["retries"] = int(op.get("retries") or 0) + 1   # 正在重启/续接时又一次连接失败（link 会退避重连）
                    op["_last_err"] = err
            self._emit_meta(s)
        if state == "ready" and not self._shutdown:
            threading.Thread(target=self._reattach, args=(jobid,), name="ccr-reattach-" + jobid,
                             daemon=True).start()

    def _req(self, jobid: str, op: str, timeout: float = 30, **params):
        with self._lock:
            link = self._links.get(jobid)
            ev = self._link_ready.get(jobid)
        if link is not None and getattr(link, "state", None) == "connecting" and ev is not None:
            ev.wait(self.connect_grace)   # 正在（重）连：稍等一下再报错
        if link is None or getattr(link, "state", None) != "ready":
            if self._link_state(jobid) == "ended":
                raise OpError("作业 %s 已结束" % jobid, code="job_ended")
            raise LinkNotReady("与%s的连接未就绪，请稍后重试" % _place(jobid))
        try:
            return link.request(op, timeout=timeout, **params)
        except OpError:
            raise
        except Exception as e:  # noqa: BLE001  AgentDown / AgentError / 超时
            name = type(e).__name__
            msg = str(e) or name
            code = getattr(e, "code", None)
            if code == "guard":   # agent 发键前重抓屏复核未通过：屏幕已变，什么都没按
                raise OpError("终端画面在发送前发生了变化，%s；请看一眼终端后重试" % msg,
                              code="screen_changed") from None
            if name == "AgentDown":
                if getattr(link, "state", None) == "ended":
                    raise OpError("作业 %s 已结束" % jobid, code="job_ended") from None
                raise LinkNotReady("与%s的连接已断开，正在重连…" % _place(jobid)) from None
            where = "本机" if remote.is_local(jobid) else "远端"
            if isinstance(e, TimeoutError) or "Timeout" in name:
                raise OpError("%s操作超时（%s）" % (where, op), code="remote_timeout", status=504) from None
            raise OpError("%s操作失败：%s" % (where, msg), code="remote_error", status=502) from None

    # ---------------------------------------------------------------- 事件处理（在 link 线程里，快速返回）
    def _on_link_event(self, jobid: str, ev: dict, link=None) -> None:
        if not isinstance(ev, dict) or self._stale_link(jobid, link):
            return
        kind = ev.get("ev")
        sid = ev.get("sid")
        with self._lock:
            s = self._sessions.get(sid) if isinstance(sid, str) else None
        if s is None or s.jobid != jobid:
            if kind == "error":
                log.warning("agent(%s) 报错：%s", jobid, ev.get("error"))
            return
        try:
            if kind == "screen":
                text = ev.get("text")
                if isinstance(text, str):
                    self._apply_screen(s, text)
            elif kind == "session":
                self._apply_session(s, ev.get("data") or {})
            elif kind == "transcript":
                entries = ev.get("entries")
                if not isinstance(entries, list):
                    return
                sid_c = ev.get("session_id")
                reset = bool(ev.get("reset"))
                with s.lock:
                    if isinstance(sid_c, str) and _UUID_RE.match(sid_c):
                        s.claude_session_id = sid_c
                        if reset:
                            s.tx_loaded.add(sid_c)
                        if any(_is_conversation_entry(e) for e in entries):
                            s.tx_conv.add(sid_c)
                    if entries:
                        s.last_activity = time.time()
                    if reset:
                        s.subs.clear()   # 子代理随主 transcript 一起从头重建（node agent 会重发）
                    changed = s.subs.absorb_main(entries)
                    s.titles.absorb(entries, reset)
                    self._update_cli_title_locked(s, reset)
                    s.tracker.on_transcript(entries, reset)   # 上下文后备：最近一条主链 assistant 的 usage
                    self._stats_update_locked(s)
                    self._publish(s, "transcript", {"type": "transcript", "entries": entries,
                                                    "reset": reset, "session_id": sid_c})
                    for aid in sorted(changed):
                        self._publish_subagent_locked(s, aid, [], None, False)
                self._note_session_key(s)
                self._emit_meta(s)
            elif kind == "subagent":
                self._apply_subagent(s, ev)
            elif kind == "exited":
                self._on_exited_event(s, jobid, ev)
        except Exception:  # noqa: BLE001
            log.exception("处理 agent 事件失败：%s", kind)

    # ---------------------------------------------------------------- 子代理
    def _apply_subagent(self, s: _Session, ev: dict) -> None:
        aid = ev.get("agent_id")
        if not isinstance(aid, str) or not subagents.AGENT_ID_RE.match(aid):
            return
        entries = ev.get("entries")
        entries = [x for x in entries if isinstance(x, dict)] if isinstance(entries, list) else []
        meta = ev.get("meta") if isinstance(ev.get("meta"), dict) else None
        reset = bool(ev.get("reset"))
        with s.lock:
            res = s.subs.apply_event(aid, meta, entries, reset, s.exited)
            if res is None:
                return
            sub, new, start, evicted = res
            if new:
                s.last_activity = time.time()
            self._publish_subagent_locked(s, aid, new, start, reset)
            for gone in evicted:
                self._publish(s, "subagent", {"type": "subagent", "agent_id": gone, "removed": True})
        self._emit_meta(s)

    def _publish_subagent_locked(self, s: _Session, aid: str, entries: list, start, reset: bool) -> None:
        """调用方持有 s.lock。data = {type, agent_id, gen, summary, entries, from, total, reset}。"""
        sub = s.subs.subs.get(aid)
        if sub is None:
            return
        total = sub.total
        self._publish(s, "subagent", {
            "type": "subagent", "agent_id": aid, "gen": sub.gen, "summary": s.subs.summary(aid, s.exited),
            "entries": entries, "from": total - len(entries) if start is None else start, "total": total,
            "reset": bool(reset)})

    def list_subagents(self, sid) -> dict:
        s = self._get(sid)
        with s.lock:
            return {"subagents": s.subs.summaries(s.exited)}

    def get_subagent(self, sid, agent_id) -> Optional[dict]:
        """该子代理全部已缓存条目 + 摘要；会话不存在抛 KeyError，子代理不存在返回 None。"""
        s = self._get(sid)
        if not isinstance(agent_id, str) or not subagents.AGENT_ID_RE.match(agent_id):
            return None
        with s.lock:
            return s.subs.detail(agent_id, s.exited)

    def get_blob(self, sid, ref) -> dict:
        """（可视化）按 ccr_ref 引用向 node agent 取回大图片 → {media_type, data(base64)}。只读；
        ref 里的 sessionId 必须是本会话当前的 claude 会话（agent 端再校验一次并核对内容 hash）。"""
        s = self._get(sid)
        with s.lock:
            cid, jobid = s.claude_session_id, s.jobid
        parts = ref.split(".") if isinstance(ref, str) else []
        if not cid or len(parts) < 5 or parts[1] != cid:
            raise OpError("该图片不属于这个会话", code="forbidden", status=403)
        # 独立的较长超时：远端在 NFS 上、链路慢时一张大图要传好几秒（agent 端 blob 在独立线程，不影响 ping/按键）
        return self._req(jobid, "blob", timeout=BLOB_TIMEOUT, sid=sid, ref=ref, session_id=cid)

    def _apply_session(self, s: _Session, data: dict) -> None:
        with s.lock:
            st = data.get("status")
            if isinstance(st, str):
                s.status = st[:20]
            pid = data.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool):
                s.claude_pid = pid
            cid = data.get("sessionId")
            if isinstance(cid, str) and _UUID_RE.match(cid):
                s.claude_session_id = cid
            cwd = data.get("cwd")
            if isinstance(cwd, str) and cwd.startswith("/") and len(cwd) < 4096:
                s.cwd = cwd
            self._derive_locked(s)
        self._note_session_key(s)
        self._emit_meta(s)

    def _apply_screen(self, s: _Session, text: str) -> dict:
        st = tui_parse.parse_screen(text)
        with s.lock:
            if _busy_hidden_by_input(st, s.status):
                st["busy"] = True   # 见 _busy_hidden_by_input：前端（占位文字、中断按钮、草稿）与统计都读这个字段
            s.screen_text = text
            s.screen_state = st
            s.screen_ts = time.time()
            if st.get("mode_line"):
                s.mode = st["mode"]
                s.seen_base.setdefault("mode", s.mode)
                s.modes_seen.add(s.mode)
            d = st.get("dialog")
            s.dialog_kind = d.get("kind") if d else None
            hint = st.get("model_hint")
            if hint:   # 启动后第一次看到的横幅/状态栏 = 启动参数实际得到的模型与 effort（基线）
                for k in ("model", "effort"):
                    if hint.get(k):
                        s.seen_base.setdefault(k, hint[k])
            if hint and (hint.get("source") == "status" or s.model is None):
                s.model = hint.get("model") or s.model
                s.effort = hint.get("effort") or s.effort
            res = st.get("last_result")
            if res:
                m = _SET_MODEL_RE.match(res)
                if m:
                    s.model = m.group(1).strip()
                m = _SET_EFFORT_RE.search(res) or re.search(r"with (\w+) effort", res)
                if m and m.group(1).lower() in EFFORT_LEVELS:
                    s.effort = m.group(1).lower()
            if d and d.get("kind") == "model":
                for r in d.get("rows") or []:
                    if r.get("current"):
                        s.model = r.get("name") or s.model
                        s.model_desc = r.get("desc") or s.model_desc
            self._derive_locked(s)
            s.tracker.on_screen(st, time.monotonic())
            self._stats_update_locked(s)
        self._schedule_screen(s)
        self._emit_meta(s)
        return st

    # ---------------------------------------------------------------- 统计（meta.stats）
    @staticmethod
    def _stats_hints(s: _Session) -> list:
        """推断上下文上限（1M / 200k）用的文本：当前模型名、/model 面板里当前模型的描述、启动参数里的模型。"""
        return [s.model, s.model_desc, (s.launch or {}).get("model")]

    def _stats_update_locked(self, s: _Session) -> None:
        """调用方持有 s.lock。统计值变化才更新 s.stats（随下一次 meta 推送）；距上次更新不足 stats_interval
        时排一个定时器稍后补上（节流：不每帧推 meta，但最终值一定会推出去）。"""
        snap = s.tracker.snapshot(self._stats_hints(s))
        if _stats_same(snap, s.stats):
            return
        now = time.monotonic()
        delay = s.stats_pub_t + self.stats_interval - now
        if delay > 0:
            if s.stats_timer is None and not self._shutdown:
                t = threading.Timer(delay, self._stats_flush, (s,))
                t.daemon = True
                s.stats_timer = t
                t.start()
            return
        if snap is not None:
            snap["updated_at"] = time.time()
        s.stats = snap
        s.stats_pub_t = now

    def _stats_flush(self, s: _Session) -> None:
        with s.lock:
            s.stats_timer = None
            if self._shutdown:
                return
            self._stats_update_locked(s)
        self._emit_meta(s)

    @staticmethod
    def _derive_locked(s: _Session) -> None:
        if s.exited:
            s.state = "exited"
            return
        if s.error and not s.attached:
            s.state = "error"
            return
        st = s.screen_state
        if st is None:
            s.state = "starting"
            return
        d = st.get("dialog")
        if d:
            s.state = "trust" if d.get("kind") == "trust" else "waiting"
        elif s.status == "waiting":
            s.state = "waiting"   # claude 自己说在等审批/对话框：即使屏幕没解析出对话框也按"有对话框"处理
        elif st.get("busy"):
            s.state = "busy"
        elif not (st.get("input") or {}).get("visible"):
            s.state = {"waiting": "waiting", "busy": "busy", "idle": "idle"}.get(s.status or "", "starting")
        else:
            s.state = "idle"

    def _schedule_screen(self, s: _Session) -> None:
        with s.lock:
            if s.screen_timer is not None:
                return
            delay = s.screen_pub_t + self.screen_interval - time.monotonic()
            if delay > 0:
                t = threading.Timer(delay, self._flush_screen, (s,))
                t.daemon = True
                s.screen_timer = t
                t.start()
                return
        self._flush_screen(s)

    def _flush_screen(self, s: _Session) -> None:
        with s.lock:
            s.screen_timer = None
            text, st = s.screen_text, s.screen_state
            if text is None or text == s.screen_pub_text:
                return
            s.screen_pub_text = text
            s.screen_pub_t = time.monotonic()
        self._publish(s, "screen", {"type": "screen", "state": st, "text": text})

    def _mark_exited(self, s: _Session, exit_status=None) -> None:
        with s.lock:
            if s.exited:
                return
            retry_fresh = False
            m = _NO_CONV_RE.search(s.screen_text or "")
            if m:   # --resume 了一个没有对话的会话（例如刚建好、还没聊过就换 API）
                cidm = m.group(1).lower()
                retry_fresh = cidm not in s.tx_noconv and not s.closing
                s.tx_noconv.add(cidm)
                if not retry_fresh:
                    s.error = s.error or ("这个会话还没有任何对话，无法 --resume 续接；"
                                          "点「重启」将以新会话启动（目录、模型、API 不变）")
            s.exited = True
            s.exit_status = exit_status if isinstance(exit_status, int) else None
            # 新启动的 claude 在启动/等待就绪阶段就退出了：进度里写明（旧 claude 在 stop 阶段退出是预期的）
            op = self._op_live(s)
            op_id = None
            if op is not None and op.get("phase") in ("start", "ready"):
                op_id = op["id"]
                if retry_fresh:
                    op["detail"] = "这个会话还没有任何对话，无法 --resume 续接，正在改为以新会话启动…"
                else:
                    ccr_op.finish(op, "failed", error="claude 启动后就退出了%s，可打开终端面板查看它的输出"
                                  % ("（退出码 %d）" % s.exit_status if s.exit_status is not None else ""))
                    self._op_finish_timer_locked(s, op)
            s.status = None
            s.claude_pid = None
            s.dialog_kind = None
            s.exit_evt.set()
            self._derive_locked(s)
            s.tracker.stop()   # 进程已不在：不再有"生成中"速率
            self._stats_update_locked(s)
            for aid in s.subs.mark_exited():   # 进程已不在：仍在运行的子代理显示为已终止
                self._publish_subagent_locked(s, aid, [], None, False)
        self._app(s, {"type": "exit", "exit_status": s.exit_status})
        self._emit_meta(s)
        if retry_fresh:
            threading.Thread(target=self._restart_fresh_after_noconv, args=(s, op_id), name="ccr-fresh-" + s.sid,
                             daemon=True).start()

    def _restart_fresh_after_noconv(self, s: _Session, op_id=None) -> None:
        """--resume 报 No conversation found（会话还没有任何对话）：自动以新会话重启一次
        （restart 会因 tx_noconv 不带 --resume；目录/模型/权限/API 沿用）。只会发生一次，没有可丢的历史。
        op_id：正在进行的重启/续接进度，接着用它（不当成"已在进行"而跳过）。"""
        time.sleep(0.5)
        try:
            self.restart(s.sid, _cont=op_id)
            self._notice(s, "info", "这个会话还没有任何对话，无法 --resume 续接，已改为以新会话启动（目录、模型、API 不变）")
        except Exception as e:  # noqa: BLE001
            msg = str(e) or type(e).__name__
            with s.lock:
                s.error = "这个会话还没有任何对话，自动以新会话启动失败：%s；可点「重启」重试" % msg
            self._emit_meta(s)
            log.warning("会话 %s 自动以新会话重启失败：%s", s.sid, msg)

    def _on_exited_event(self, s: _Session, jobid: str, ev: dict) -> None:
        """agent 报告 pane 死亡/会话消失。迟到或误报的事件绝不能 kill 新启动或仍存活的会话：
        启动中忽略；pane_pid（代际）对不上丢弃；其余先用 list 核实 pane 确实已死再标记/清理。"""
        pp = ev.get("pane_pid")
        pp = pp if isinstance(pp, int) and not isinstance(pp, bool) else None
        with s.lock:
            if s.starting or s.exited or not s.attached:
                return
            if pp is not None and s.pane_pid is not None and pp != s.pane_pid:
                return
            closing = s.closing
        if closing:
            # close 正在等 claude 退出：只记下退出（_close_session 随后自己 kill）
            self._mark_exited(s, ev.get("exit_status"))
            return
        threading.Thread(target=self._verify_exit, args=(s, jobid), name="ccr-verify-" + s.sid,
                         daemon=True).start()

    def _verify_exit(self, s: _Session, jobid: str) -> None:
        """用 list 核实 pane 已死（或会话已不在）后才标记退出、kill 空壳；pane 还活着 = 误报，恢复监视。
        持有 s.oplock，避免与 restart/close 交错（kill 掉新会话）。"""
        if not s.oplock.acquire(timeout=self.op_lock_timeout * 4):
            log.info("核实会话 %s 是否退出：等待操作锁超时，稍后恢复监视", s.sid)
            self._rewatch_later(s, jobid)
            return
        marked = False
        try:
            with s.lock:
                if s.starting or s.exited or not s.attached or s.jobid != jobid:
                    return
                cur = s.pane_pid
            try:
                items = self._req(jobid, "list", timeout=30)
            except OpError as e:
                log.info("核实会话 %s 是否退出失败（%s），保持会话并稍后恢复监视", s.sid, e)
                self._rewatch_later(s, jobid)
                return
            it = next((x for x in items or [] if isinstance(x, dict) and x.get("sid") == s.sid), None)
            if it is not None and not it.get("pane_dead"):
                # pane 还活着：误报（绝不 kill 活着的 pane），恢复监视
                log.info("会话 %s 的 exited 事件是误报（pane %s 仍在，本地记录 %s），恢复监视",
                         s.sid, it.get("pane_pid"), cur)
                self.notifier.reset(s.sid)   # 重新 watch 的首帧只作提示音基线
                try:
                    self._req(jobid, "watch", sid=s.sid, on=True, timeout=20)
                except OpError as e:
                    log.info("恢复监视 %s 失败：%s", s.sid, e)
                return
            self._mark_exited(s, it.get("exit_status") if it is not None else None)
            marked = True
            if it is not None:
                try:
                    self._req(jobid, "kill", sid=s.sid, timeout=20)   # remain-on-exit 留下的空壳
                except OpError as e:
                    log.info("清理已退出会话 %s 失败：%s", s.sid, e)
            with s.lock:
                s.attached = False
            self._dirty.set()
        finally:
            s.oplock.release()
        if marked:
            self._maybe_stop_keeper(jobid)

    def _rewatch_later(self, s: _Session, jobid: str, delay: float = 5.0) -> None:
        def go():
            time.sleep(delay)
            with s.lock:
                ok = s.live() and not s.starting and s.jobid == jobid
            if ok and not self._shutdown:
                self.notifier.reset(s.sid)
                try:
                    self._req(jobid, "watch", sid=s.sid, on=True, timeout=20)
                except OpError as e:
                    log.info("恢复监视 %s 失败：%s（重连后会自动恢复）", s.sid, e)
        threading.Thread(target=go, name="ccr-rewatch-" + s.sid, daemon=True).start()

    def _maybe_stop_keeper(self, jobid: str) -> None:
        """该作业已无存活/启动中的会话：stop_keeper（agent 只在没有 ccr- 会话时才真的停），然后关闭 link。"""
        with self._lock:
            others = [x for x in self._sessions.values() if x.jobid == jobid and (x.live() or x.starting)]
        if others:
            return
        if self._link_state(jobid) == "ready":
            try:
                self._req(jobid, "stop_keeper", timeout=20)
            except OpError as e:
                log.info("stop_keeper(%s) 失败：%s", jobid, e)
        self._maybe_close_link(jobid)

    # ---------------------------------------------------------------- 重新附着
    def _reattach(self, jobid: str) -> None:
        with self._lock:
            lk = self._reattach_locks.setdefault(jobid, threading.Lock())
        with lk:
            try:
                items = self._req(jobid, "list", timeout=30)
            except OpError as e:
                log.warning("list(%s) 失败：%s", jobid, e)
                return
            by_sid = {}
            for it in items if isinstance(items, list) else []:
                if isinstance(it, dict) and isinstance(it.get("sid"), str):
                    by_sid[it["sid"]] = it
            with self._lock:
                ss = [s for s in self._sessions.values() if s.jobid == jobid]
            dead_any = False
            for s in ss:
                with s.lock:
                    if not s.live() or s.starting:
                        continue
                it = by_sid.get(s.sid)
                if it is None:
                    self._mark_exited(s, None)
                    with s.lock:
                        s.attached = False
                    self._notice(s, "warn", "本机的 claude 会话已不存在（tmux 被结束或电脑重启过？），可点「重连」用 --resume 续接"
                                 if remote.is_local(jobid) else
                                 "远端的 claude 会话已不存在（节点重启或作业结束？），可点「重连」用 --resume 续接")
                    dead_any = True
                    continue
                if it.get("pane_dead"):
                    threading.Thread(target=self._verify_exit, args=(s, jobid), name="ccr-verify-" + s.sid,
                                     daemon=True).start()
                    continue
                with s.lock:
                    cp = it.get("claude_pid")
                    if isinstance(cp, int) and not isinstance(cp, bool):
                        s.claude_pid = cp
                    pp = it.get("pane_pid")
                    if isinstance(pp, int) and not isinstance(pp, bool):
                        s.pane_pid = pp
                self.notifier.reset(s.sid)   # 重新附着：首次状态同步不响
                if isinstance(it.get("session"), dict):
                    self._apply_session(s, it["session"])
                try:
                    self._req(jobid, "watch", sid=s.sid, on=True, timeout=20)
                except OpError as e:
                    self._notice(s, "warn", "重新监视会话失败：%s" % e)
            if dead_any:
                self._dirty.set()
                self._maybe_stop_keeper(jobid)   # 本作业已无存活会话：停 keeper、关 link

    # ================================================================ 生命周期
    @staticmethod
    def _validate_launch(model, permission_mode, skip_permissions, effort, api_key_id=None) -> dict:
        m = remote.validate_model(model) if model else None
        pm = permission_mode or None
        if pm is not None and pm not in PERMISSION_MODES:
            raise ValueError("permission_mode 不合法（可选：%s）" % "/".join(PERMISSION_MODES))
        if not isinstance(skip_permissions, bool):
            raise ValueError("skip_permissions 必须是布尔值")
        ef = effort or None
        if ef is not None and ef not in EFFORT_LEVELS:
            raise ValueError("effort 不合法（可选：%s）" % "/".join(EFFORT_LEVELS))
        kid = ccr_api_keys.validate_id_opt(api_key_id)
        return {"model": m, "permission_mode": pm, "skip_permissions": skip_permissions, "effort": ef,
                "api_key_id": kid}

    # ---------------------------------------------------------------- API key（--settings，由 agent 生成）
    def _norm_kid(self, kid: Optional[str]) -> Optional[str]:
        """"服务器默认"条目（is_server_default，只用于查询用量/显示名称）不能被选作 --settings：视同服务器默认。"""
        return None if kid is not None and self.api_keys.is_server_default(kid) else kid

    def _resolve_api_key(self, kid: Optional[str], name_hint: Optional[str] = None, restart: bool = False):
        """((明文 key, base_url|None) | None, 显示名 | None)。None = 服务器默认（不加 --settings）。
        明文只交给 start 请求的 api_key 字段，不存到 _Session/meta/日志。"""
        if kid is None:
            return None, None
        try:
            return self.api_keys.credentials(kid), self.api_keys.name_of(kid)
        except ccr_api_keys.ApiKeyNotFound:
            label = "「%s」" % name_hint if name_hint else " "
            if restart:
                msg = ("该会话使用的 API key%s已被删除，请在「API」里改选其它 key 或服务器默认后再重启/续接"
                       % label)
            else:
                msg = "所选 API key%s不存在（可能已被删除），请重新选择" % label
            raise OpError(msg, code="api_key_missing") from None

    def _refresh_api_key_locked(self, s: _Session) -> None:
        kid = (s.launch or {}).get("api_key_id")
        if kid is None:
            s.api_key_name, s.api_key_missing = None, False
            return
        name = self.api_keys.name_of(kid)
        if name is None:
            s.api_key_missing = True     # 保留最后已知的名称用于显示
        else:
            s.api_key_name, s.api_key_missing = name, False

    def _on_api_keys_changed(self, event: str, kid: str) -> None:
        """api_keys.json 增删改：刷新引用该 key 的会话的显示名 / 已删除标记（已运行的 claude 不受影响）。"""
        with self._lock:
            ss = list(self._sessions.values())
        for s in ss:
            with s.lock:
                if (s.launch or {}).get("api_key_id") != kid:
                    continue
                self._refresh_api_key_locked(s)
            self._emit_meta(s)

    def _note_session_key(self, s: _Session) -> None:
        """拿到/换了 claude_session_id（启动、续接、/clear）时记下它用的 key（null = 服务器默认）。"""
        with s.lock:
            cid = s.claude_session_id
            kid = (s.launch or {}).get("api_key_id")
            if not cid or (cid, kid) == s.key_noted:
                return
            s.key_noted = (cid, kid)
            name = s.api_key_name if kid else None
        self.session_keys.record(cid, kid, name)

    def api_key_for_claude_session(self, cid) -> Optional[dict]:
        """该 claude 会话上次用的 API：{"id": id|None(服务器默认), "name", "missing"}；没有记录 → None。"""
        rec = self.session_keys.get(cid)
        if rec is None:
            return None
        kid = rec.get("api_key_id")
        if kid is None:
            return {"id": None, "name": None, "missing": False}
        name = self.api_keys.name_of(kid)
        return {"id": kid, "name": name or rec.get("api_key_name"), "missing": name is None}

    def api_key_users(self, kid) -> list:
        """引用该 key 的本地会话（含已退出的：重启/续接时会用到）。"""
        with self._lock:
            ss = list(self._sessions.values())
        out = []
        for s in ss:
            with s.lock:
                if (s.launch or {}).get("api_key_id") == kid:
                    out.append({"sid": s.sid, "title": s.display_title(), "live": bool(s.live() or s.starting)})
        return out

    # ---------------------------------------------------------------- 重启时保留会话里改过的模型/effort/模式
    @staticmethod
    def _model_arg(display: Optional[str], desc: Optional[str]) -> Optional[str]:
        """屏幕上的模型名 → `--model` 可接受的值；拿不准返回 None（调用方沿用 launch.model）。"""
        for t in (desc, display):
            m = re.search(r"\((claude-[A-Za-z0-9._\-\[\]]{1,72})\)", t or "")
            if m:
                return m.group(1)
        d = (display or "").strip()
        if re.fullmatch(r"claude-[A-Za-z0-9._\-\[\]]{1,73}", d):
            return d
        m = re.match(r"(opus|sonnet|haiku)\b", d, re.I)
        if m:
            return m.group(1).lower() + ("[1m]" if re.search(r"\b1M\b", d) else "")
        return None

    def _carry_launch_locked(self, s: _Session) -> dict:
        """重启用的 launch：用户在会话里改过（与本次启动后第一次看到的基线不同，或没有基线）的
        模型 / effort / 权限模式用当前值（都在 §0.2 白名单内）；识别不了的沿用原 launch。"""
        la = dict(s.launch)
        base = s.seen_base or {}

        def norm(x):
            return self._model_arg(x, None) or (x or "").strip().lower()

        if s.model and ("model" not in base or norm(base["model"]) != norm(s.model)):
            arg = self._model_arg(s.model, s.model_desc)
            if arg and arg != la.get("model"):
                try:
                    la["model"] = remote.validate_model(arg)
                except ValueError:
                    pass
        if s.effort in EFFORT_LEVELS and base.get("effort") != s.effort:
            la["effort"] = s.effort
        if (s.mode in PERMISSION_MODES and not la.get("skip_permissions")
                and base.get("mode") != s.mode and s.mode != la.get("permission_mode")):
            la["permission_mode"] = s.mode
        return la

    @staticmethod
    def _build_args(launch: dict, resume: Optional[str]) -> list:
        return list(remote.build_claude_args(
            model=launch.get("model"), permission_mode=launch.get("permission_mode"),
            skip_permissions=bool(launch.get("skip_permissions")), effort=launch.get("effort"),
            resume=resume))

    def _check_unique_resume(self, cid: Optional[str], me: Optional[_Session] = None) -> None:
        if not cid:
            return
        with self._lock:
            ss = list(self._sessions.values())
        for x in ss:
            if x is me:
                continue
            with x.lock:
                if x.claude_session_id == cid and (x.live() or x.starting):
                    raise OpError("该 Claude 会话已在本地会话「%s」中运行，请先关闭它" % x.display_title(), code="already_running")

    def _require_server_host(self, jobid) -> None:
        """服务器会话需要已设置 ssh 主机（config.host）；没设置就直接 400，而不是让连接线程反复重试。"""
        if remote.is_local(jobid):
            return
        cfg = dict(self._get_config() or {})
        if cfg.get("fake"):
            return
        remote.validate_host(cfg.get("host") or "")

    def create(self, jobid, cwd, title=None, model=None, permission_mode=None, skip_permissions=False,
               effort=None, resume=None, api_key_id=None) -> dict:
        if self._shutdown:
            raise OpError("服务正在关闭")
        if jobid is None or str(jobid).strip() == "":
            raise ValueError("请先设置 jobid")
        j = remote.validate_target(str(jobid).strip())   # 作业 jobid，或本机 LOCAL_JOBID
        self._require_server_host(j)
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError("工作目录不能为空")
        cwd_v = remote.validate_path(cwd.strip())
        launch = self._validate_launch(model, permission_mode, skip_permissions, effort, api_key_id)
        r = None
        if resume:
            if not isinstance(resume, str) or not _UUID_RE.match(resume):
                raise ValueError("resume 必须是会话 UUID")
            r = resume
        args = self._build_args(launch, r)
        launch["api_key_id"] = self._norm_kid(launch["api_key_id"])
        key, key_name = self._resolve_api_key(launch["api_key_id"])
        self._check_unique_resume(r)
        default_title = ("续接 %s" % r[:8]) if r else (os.path.basename(cwd_v.rstrip("/")) or cwd_v)
        sid = "s" + uuid.uuid4().hex[:12]
        s = _Session(sid, self._max_events)
        s.title = _clean_title(title, default_title)
        s.title_user = bool(_clean_title(title, ""))
        s.jobid, s.cwd, s.launch = j, cwd_v, launch
        s.api_key_name = key_name
        s.claude_session_id = r
        s.attached = True
        s.starting = True
        s.state = "starting"
        op = self._op_begin_locked(s, "create", self._op_link_ids(j) + ["keeper", "start", "ready"], j, resume=r)
        s.oplock.acquire()   # 由启动线程释放
        try:
            with self._lock:
                self._sessions[sid] = s
            self._ensure_link(j)
            with s.lock:
                s.link = self._link_state(j)
            self._emit_meta(s, force=True)
            threading.Thread(target=self._start_worker, args=(s, args, key, op["id"]), name="ccr-start-" + sid,
                             daemon=True).start()
        except BaseException:
            with s.lock:
                s.starting = False
                s.attached = False
                s.error = "启动失败"
                self._derive_locked(s)
            s.oplock.release()
            raise
        return self.get_meta(sid)

    def _ensure_keeper(self, link, s: _Session, op_id) -> None:
        """link.ensure_keeper；支持 progress 回调的（ccr.agent_link.AgentLink）把子步骤写进进度说明。"""
        fn = link.ensure_keeper
        try:
            accepts = "progress" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            accepts = False
        if accepts:
            fn(timeout=self.keeper_timeout, progress=lambda text: self._op_note(s, text, op_id))
        else:
            fn(timeout=self.keeper_timeout)

    def _start_worker(self, s: _Session, args: list, cred=None, op_id=None) -> None:
        """调用方已为本线程获取 s.oplock；结束时释放。
        cred = (明文 key, base_url|None)：只作为 start 请求的 api_key/base_url 字段经 agent 的 stdin 发出
        （agent 写 0600 文件并追加 --settings）；None = 服务器默认，与以前完全一致。
        op_id：阶段进度（连接 → keeper → 启动 claude → 等待就绪），失败时写明原因。"""
        started = False
        jobid = s.jobid
        try:
            self._wait_link(jobid, self.link_timeout, s=s, op_id=op_id)
            self._op_phase(s, op_id, "keeper")
            with self._lock:
                link = self._links.get(jobid)
            try:
                self._ensure_keeper(link, s, op_id)
            except OpError:
                raise
            except Exception as e:  # noqa: BLE001
                raise OpError("启动 tmux 常驻进程失败：%s" % (str(e) or type(e).__name__)) from None
            with s.lock:
                s.exit_evt.clear()
            params = {"sid": s.sid, "cwd": s.cwd, "args": args}
            if cred is not None:
                params["api_key"] = cred[0]
                if cred[1]:
                    params["base_url"] = cred[1]
            self._op_phase(s, op_id, "start")
            res = self._req(jobid, "start", timeout=60, **params)
            started = True
            pp = res.get("pane_pid") if isinstance(res, dict) else None
            with s.lock:
                s.pane_pid = pp if isinstance(pp, int) and not isinstance(pp, bool) else None
                s.starting = False
                s.error = None
            self.notifier.reset(s.sid)   # 启动后的首帧只作提示音基线
            self._req(jobid, "watch", sid=s.sid, on=True, timeout=20)
            self._op_phase(s, op_id, "ready")
        except Exception as e:  # noqa: BLE001
            msg = str(e) or type(e).__name__
            # 给用户看的是一句话的原因（ssh 超时 / 作业已结束 / srun 报错…），原始报错只进日志
            text = self._op_fail_from(s, op_id, e, jobid, timeout=self.link_timeout)
            with s.lock:
                s.starting = False
                if not started:
                    s.attached = False
                    s.error = "启动失败：%s" % text
                self._derive_locked(s)
            self._notice(s, "error", ("启动失败：%s" if not started else "监视会话失败：%s") % text)
            log.warning("启动会话 %s 失败：%s", s.sid, msg)
        finally:
            with s.lock:
                s.starting = False
                self._derive_locked(s)
            s.oplock.release()
            self._emit_meta(s)

    @contextlib.contextmanager
    def _op(self, s: _Session, timeout: Optional[float] = None):
        if not s.oplock.acquire(timeout=self.op_lock_timeout if timeout is None else timeout):
            raise OpError("该会话正在执行其它操作，请稍候再试", code="op_in_progress")
        try:
            yield
        finally:
            s.oplock.release()

    def _require_live(self, s: _Session) -> None:
        with s.lock:
            if s.exited or not s.attached:
                raise OpError("会话已结束", code="exited")
            if s.starting:
                raise OpError("会话正在启动，请稍候", code="starting")

    # ---------------------------------------------------------------- 远端小操作
    def _fresh(self, s: _Session) -> dict:
        self._require_live(s)
        res = self._req(s.jobid, "capture", sid=s.sid, timeout=15)
        text = res.get("text") if isinstance(res, dict) else None
        return self._apply_screen(s, text if isinstance(text, str) else "")

    def _keys(self, s: _Session, keys: list, guard: Optional[dict] = None) -> None:
        """guard：agent 在 send-keys 前就地重抓屏复核（见 node_agent.check_guard），不通过则什么都不按。"""
        params = {"sid": s.sid, "keys": list(keys)}
        if guard is not None:
            params["guard"] = guard
        self._req(s.jobid, "keys", timeout=20, **params)
        with s.lock:
            s.last_activity = time.time()

    def _blocking_dialog(self, s: _Session, st: dict, refresh: bool = True) -> bool:
        """有对话框：屏幕解析出对话框，或 session json 的 status == "waiting"（解析不出也按有对话框处理）。
        屏幕上没有对话框而 status 仍是 waiting 时，可能只是 session 事件滞后：refresh=True 时用 list 读一次最新值。"""
        if st.get("dialog"):
            return True
        with s.lock:
            status = s.status
        if status != "waiting":
            return False
        if not refresh:
            return True
        try:
            items = self._req(s.jobid, "list", timeout=15)
        except OpError:
            return True
        it = next((x for x in items or [] if isinstance(x, dict) and x.get("sid") == s.sid), None)
        sess = it.get("session") if isinstance(it, dict) else None
        if not isinstance(sess, dict):
            return True
        self._apply_session(s, sess)
        return sess.get("status") == "waiting"

    def _submit_text(self, s: _Session, text: str, slash: Optional[str] = None) -> dict:
        """调用方持有 s.oplock，且已确认无对话框、输入框可见且为空。
        粘贴（不回车）→ 抓屏确认：文字进了输入框、没有对话框、session status 不是 waiting；以 / 开头时补全列表
        没出现或高亮项正好是目标命令 → 再抓一帧确认画面稳定 → 带 guard 单独发 Enter（agent 发之前再复核一次）。
        任何一步不满足都**不按 Enter**（文字留在输入框里），抛 OpError。"""
        self._paste(s, text, enter=False)

        def verdict(x):
            if self._blocking_dialog(s, x, refresh=False):   # 调用前已用 refresh 核实过 status
                return "dialog"
            if not (x.get("input") or {}).get("visible"):
                return None
            if not _input_matches((x.get("input") or {}).get("text") or "", text):
                return None
            sel = _popup_selected(x)
            if sel is not None and sel != slash:
                return "popup:" + sel
            return "ok"

        last = None
        ok_streak = 0
        deadline = time.monotonic() + self.step_timeout
        while True:
            st = self._fresh(s)
            v = verdict(st)
            if v == "dialog":
                raise OpError("粘贴后终端里出现了对话框，没有按回车（文字留在输入框里），请先处理对话框",
                              code="dialog_open")
            if v is not None and v.startswith("popup:"):
                raise OpError("终端的命令补全高亮的是 %s 而不是 %s，没有按回车；请在终端面板确认后手动操作"
                              % (v[6:], slash or "（普通消息）"), code="slash_mismatch")
            if v == "ok":
                pop = st.get("popup")
                ok_streak = ok_streak + 1 if ok_streak and pop == last else 1
                last = pop
                if ok_streak >= 2:   # 连续两帧一致才算稳定（补全列表可能晚一帧才出现）
                    break
            else:
                ok_streak = 0
            if time.monotonic() >= deadline:
                raise OpError("已把文字粘贴进终端输入框，但没能确认，没有按回车；请在终端面板检查后按 Enter",
                              code="paste_unconfirmed", status=504)
            time.sleep(self.poll_interval)
        self._keys(s, ["Enter"], guard={"input_box": True, "input_nonempty": True, "popup": slash})
        return st

    def _paste(self, s: _Session, text: str, enter: bool) -> None:
        self._req(s.jobid, "paste", sid=s.sid, text=text, enter=bool(enter), timeout=30)
        with s.lock:
            s.last_activity = time.time()

    def _wait_for(self, s: _Session, pred, timeout: float) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while True:
            st = self._fresh(s)
            try:
                if pred(st):
                    return st
            except (KeyError, TypeError, AttributeError):
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll_interval)

    def _ensure_input_empty(self, s: _Session, st: dict, clear: bool) -> dict:
        txt = (st.get("input") or {}).get("text") or ""
        if not txt.strip():
            return st
        if not clear:
            short = txt if len(txt) <= 60 else txt[:60] + "…"
            raise OpError("终端输入框里已有未发送的文字「%s」，确认后可清空再继续" % short, code="input_not_empty")
        prev = txt
        for _ in range(self.max_clear_steps):
            self._keys(s, ["C-u"], guard={"input_box": True})
            st2 = self._wait_for(s, lambda x: (x.get("input") or {}).get("text") != prev, self.step_timeout)
            if st2 is None:
                self._keys(s, ["BSpace"], guard={"input_box": True})
                st2 = self._fresh(s)
            cur = (st2.get("input") or {}).get("text") or ""
            if not cur.strip():
                return st2
            prev = cur
        raise OpError("无法清空终端输入框，请在终端面板手动处理", code="clear_failed")

    # ================================================================ 用户操作
    def send_message(self, sid, text, clear_input: bool = False) -> dict:
        s = self._get(sid)
        if not isinstance(text, str):
            raise ValueError("text 必须是字符串")
        if not text.strip():
            raise ValueError("消息不能为空")
        if len(text) > MAX_TEXT:
            raise ValueError("消息过长（最多 %d 字符）" % MAX_TEXT)
        if "\x00" in text:
            raise ValueError("消息不能包含 NUL 字符")
        self._user_engaged(s)   # 在这个会话里发消息 = 看过了
        with self._op(s):
            st = self._fresh(s)
            if self._blocking_dialog(s, st) or s.state == "trust":
                raise OpError("请先处理当前对话框", code="dialog_open")
            if not (st.get("input") or {}).get("visible"):
                raise OpError("输入框不可见（Claude 可能还在启动），请稍后再试", code="no_input")
            self._ensure_input_empty(s, st, bool(clear_input))
            self._submit_text(s, text, slash=_slash_target(text))
        self._emit_meta(s)
        return {"ok": True, "local_id": "l" + uuid.uuid4().hex[:12]}

    def send_keys(self, sid, keys) -> dict:
        s = self._get(sid)
        if not isinstance(keys, list) or not keys:
            raise ValueError("keys 必须是非空数组")
        if len(keys) > MAX_KEYS:
            raise ValueError("一次最多发送 %d 个按键" % MAX_KEYS)
        for k in keys:
            if not isinstance(k, str) or k not in KEYS_WHITELIST:
                raise ValueError("不允许的按键：%s" % str(k)[:20])
        with self._op(s, timeout=5):
            self._require_live(s)
            self._keys(s, keys)
        self.notifier.user_acted(s.sid)
        if "Escape" in keys or "C-c" in keys:
            self.notifier.suppress_done(s.sid)   # 终端面板里按 Esc / Ctrl+C = 用户中断，本轮结束不响
            self._user_engaged(s)
        return {"ok": True}

    def interrupt(self, sid) -> dict:
        """Esc。只在忙（或有对话框）时发送：空闲时连按 Esc 会打开 CLI 的回退菜单，不是用户想要的。"""
        s = self._get(sid)
        st = self._fresh(s)
        with s.lock:
            status = s.status
        if not (st.get("busy") or st.get("dialog") or status == "busy"):
            raise OpError("Claude 当前没有在生成", code="not_busy")
        self.notifier.suppress_done(s.sid)   # 用户中断导致的 idle 不响
        self._user_engaged(s)
        self._keys(s, ["Escape"])
        return {"ok": True}

    def cancel_dialog(self, sid) -> dict:
        s = self._get(sid)
        with self._op(s):
            st = self._fresh(s)
            d = st.get("dialog")
            if not d:
                raise OpError("当前没有对话框", code="no_dialog")
            sig = _dialog_sig(d)
            self.notifier.user_acted(s.sid)
            self.notifier.suppress_done(s.sid)   # Esc 取消对话框（权限框 = 拒绝并中断本轮）
            self._user_engaged(s)
            self._keys(s, ["Escape"])
            st2 = self._wait_after(s, lambda x: _dialog_sig(x.get("dialog")) != sig)
        return {"ok": True, "state": st2 or s.screen_state}

    def cycle_mode(self, sid) -> dict:
        s = self._get(sid)
        with self._op(s):
            st = self._fresh(s)
            if self._blocking_dialog(s, st):
                raise OpError("请先处理当前对话框", code="dialog_open")
            old = st.get("mode_line")
            # Shift+Tab 在权限框里有别的含义：只在输入框确实可见（没有对话框）时才发
            self._keys(s, ["BTab"], guard={"input_box": True})
            st2 = self._wait_for(s, lambda x: x.get("mode_line") != old, self.step_timeout)
        with s.lock:
            mode = s.mode
        return {"ok": True, "mode": mode, "mode_line": (st2 or s.screen_state or {}).get("mode_line")}

    def set_mode(self, sid, mode) -> dict:
        """切到指定权限模式（会话头的模式选择栏，SPEC §13）：目标在本次启动的 Shift+Tab 循环里 → 每次发一个 BTab
        （发之前核对最新屏幕：没有对话框、有模式行；带 guard input_box，agent 发键前重抓屏复核），发完等模式行
        变成新模式再决定是否继续，最多 max_mode_steps 次。忙碌时也可以切（CLI 本身支持）。
        目标不在循环里 → 409 mode_needs_restart（前端改走 restart(mode=…)）；中途模式行不变 / 出现对话框 /
        转了一圈没到 → 立即停止，409 并说明当前停在哪个模式。"""
        s = self._get(sid)
        if not isinstance(mode, str) or mode not in PERMISSION_MODES:
            raise ValueError("mode 不合法（可选：%s）" % "/".join(PERMISSION_MODES))
        with self._op(s):
            st = self._fresh(s)
            if self._blocking_dialog(s, st) or s.state == "trust":
                raise OpError("请先处理当前对话框", code="dialog_open")
            if not st.get("mode_line"):
                raise OpError("终端里看不到权限模式行，暂时无法切换（可打开终端面板查看）", code="no_mode_line")
            cur = st.get("mode")
            if cur == mode:
                return {"ok": True, "mode": mode, "steps": 0}
            with s.lock:
                cycle = mode_cycle(s.launch, s.modes_seen)
            if mode not in cycle:
                raise OpError("「%s」不在本次启动的 Shift+Tab 循环里，需要以 --resume 重启 claude 才能切换" % mode,
                              code="mode_needs_restart")
            visited = {cur}
            for step in range(1, self.max_mode_steps + 1):
                prev = cur
                self._keys(s, ["BTab"], guard={"input_box": True})
                st2 = self._wait_for(s, lambda x: bool(x.get("dialog")) or (
                    bool(x.get("mode_line")) and x.get("mode") != prev), self.step_timeout)
                if st2 is None:
                    raise OpError("按了 Shift+Tab 但模式行没有变化，已停止（当前停在 %s）" % prev, code="mode_stuck")
                if st2.get("dialog"):
                    raise OpError("切换途中出现了对话框，已停止（当前停在 %s）" % prev, code="dialog_open")
                cur = st2.get("mode")
                if cur == mode:
                    self._emit_meta(s)
                    return {"ok": True, "mode": mode, "steps": step}
                if cur in visited:
                    raise OpError("转了一圈仍没有切到 %s，已停止（当前停在 %s）" % (mode, cur), code="mode_unreachable")
                visited.add(cur)
            raise OpError("按了 %d 次 Shift+Tab 仍没有切到 %s，已停止（当前停在 %s）" % (self.max_mode_steps, mode, cur),
                          code="mode_unreachable")

    # ---------------------------------------------------------------- CLI 快捷键提示（用户点击/和弦触发，§0.5）
    def _hint_keys(self, s: _Session, hint: str, keys: list, code: str, missing: str) -> dict:
        """屏幕上**此刻**有该提示、且没有对话框时，才发 keys（确认类按键：先核对最新屏幕，再带 guard
        {input_box, hint} 让 agent 在 send-keys 前就地重抓屏复核；不通过一个键都不按，409 screen_changed）。"""
        with self._op(s):
            st = self._fresh(s)
            if self._blocking_dialog(s, st) or s.state == "trust":
                raise OpError("请先处理当前对话框", code="dialog_open")
            if not (st.get("hints") or {}).get(hint):
                raise OpError(missing, code=code)
            self._keys(s, keys, guard={"input_box": True, "hint": hint})
            st2 = self._wait_for(s, lambda x: not (x.get("hints") or {}).get(hint), self.step_timeout)
        self._emit_meta(s)
        return {"ok": True, "state": st2 or s.screen_state}

    def run_in_background(self, sid) -> dict:
        """CLI 提示 "ctrl+b ctrl+b (twice) to run in background"：把正在运行的 Bash 命令转入后台。
        tmux send-keys 不经过 tmux 前缀键，发**一次** C-b 即可（实测）。"""
        s = self._get(sid)
        return self._hint_keys(s, "background", ["C-b"], "no_background_hint",
                               "屏幕上没有「ctrl+b 转到后台运行」的提示（命令可能已经结束），没有发送按键")

    def send_now(self, sid) -> dict:
        """CLI 提示 "Queued · ctrl+x ctrl+s to send now"：中断当前生成、立即发送排队的消息（C-x 再 C-s）。
        不调用 notifier.suppress_done：真机实测被打断的这一轮直接进入排队消息的新一轮（中间没有 idle），
        压制会一直留到新一轮结束、把它的"完成"提示音也吞掉。"""
        s = self._get(sid)
        return self._hint_keys(s, "send_now", ["C-x", "C-s"], "no_send_now_hint",
                               "屏幕上没有「ctrl+x ctrl+s 立即发送」的提示（排队的消息可能已经发出），没有发送按键")

    def choose(self, sid, index, text=None, expect=None) -> dict:
        """把当前对话框的光标从选中项逐步移到 index（每步重新解析），再 Enter。只由用户点击触发。

        expect：前端渲染卡片时的对话框指纹（dialog_fingerprint）。与最新屏幕上的对话框不一致（用户在终端面板
        已答掉/取消了 A、弹出了 B，或另一个窗口已操作）→ 409 dialog_changed，一个键都不按。导航中每一步、
        按 Enter 前都再比对一次，Enter 本身还带 agent 端的对话框区域守卫。"""
        s = self._get(sid)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("index 必须是非负整数")
        if text is not None:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("text 不能为空")
            if len(text) > MAX_TEXT or "\x00" in text:
                raise ValueError("text 不合法")
        expect = validate_expect(expect)
        changed = OpError("对话框已变化，请重新确认", code="dialog_changed")
        with self._op(s):
            st = self._fresh(s)
            d = st.get("dialog")
            if not d:
                raise OpError("当前没有对话框", code="no_dialog")
            free = _free_indexes(d)
            if expect is not None and not fingerprint_matches(d, expect, free):
                raise changed
            if d.get("kind") in ("model", "effort"):
                raise OpError("模型/effort 面板请使用「切换模型/effort」功能", code="wrong_dialog")
            opts = d.get("options") or []
            if index >= len(opts):
                raise ValueError("选项不存在")
            if text is not None and not opts[index].get("free_text"):
                raise ValueError("该选项不接受文字输入")
            fp0 = dialog_fingerprint(d)
            d = self._navigate_options(s, d, index, fp0, free)
            if text is not None:
                self._paste(s, text, enter=False)
                time.sleep(0.15)
            # 按 Enter 前：最新屏幕上必须仍是同一个对话框、光标在目标项上
            st = self._fresh(s)
            d = st.get("dialog")
            if not d or not fingerprint_matches(d, fp0, free) or tui_parse.selected_index(d) != index:
                raise changed
            with s.lock:
                guard = _dialog_tail_guard(s.screen_text)
            if guard is None:
                raise changed
            sig = _dialog_sig(d)
            sel = tui_parse.selected_index(d)
            self.notifier.user_acted(s.sid)   # 用户回答了对话框：之后同样的对话框再出现算新的一次
            self._user_engaged(s)
            try:
                self._keys(s, ["Enter"], guard=guard)
            except OpError as e:
                if e.api_code == "screen_changed":
                    raise changed from None
                raise
            st2 = self._wait_after(s, lambda x: _dialog_sig(x.get("dialog")) != sig
                                   or tui_parse.selected_index(x.get("dialog")) != sel)
        return {"ok": True, "state": st2 or s.screen_state}

    def _wait_after(self, s: _Session, pred) -> Optional[dict]:
        """确认键之后等屏幕变化；claude 因此退出（如信任框选 No, exit）不算错误。"""
        try:
            return self._wait_for(s, pred, self.step_timeout)
        except OpError as e:
            if e.api_code == "exited" or s.exit_evt.is_set():
                return None
            raise

    def _navigate_options(self, s, d, index, fp0, free):
        for _ in range(self.max_nav_steps + 1):
            cur = tui_parse.selected_index(d)
            if cur is None:
                raise OpError("无法确定对话框当前选中项，请在终端面板操作", code="no_cursor")
            if cur == index:
                return d
            key = "Down" if index > cur else "Up"
            self._keys(s, [key])
            st2 = self._wait_for(s, lambda x: tui_parse.selected_index(x.get("dialog")) != cur
                                 or _dialog_sig(x.get("dialog")) != _dialog_sig(d), self.step_timeout)
            if st2 is None:
                raise OpError("对话框没有响应方向键，请在终端面板操作", code="nav_timeout")
            d2 = st2.get("dialog")
            if not d2 or not fingerprint_matches(d2, fp0, free):
                raise OpError("对话框已变化，请重新确认", code="dialog_changed")
            d = d2
        raise OpError("导航步数超过上限", code="nav_limit")

    # ---------------------------------------------------------------- /model
    def _model_dialog(self, st: Optional[dict]) -> Optional[dict]:
        d = (st or {}).get("dialog")
        return d if d and d.get("kind") == "model" else None

    def _model_step(self, s: _Session, key: str, d: dict) -> dict:
        before = tui_parse.selected_model_n(d)
        self._keys(s, [key])
        st2 = self._wait_for(s, lambda x: self._model_dialog(x) is not None
                             and tui_parse.selected_model_n(x["dialog"]) != before, self.step_timeout)
        if st2 is None:
            if self._model_dialog(s.screen_state) is None:
                raise OpError("模型面板已关闭", code="panel_closed")
            raise OpError("模型面板没有响应方向键", code="nav_timeout")
        return st2["dialog"]

    def _model_nav_to(self, s: _Session, d: dict, target_n: int, budget: list) -> dict:
        while True:
            sel = tui_parse.selected_model_n(d)
            if sel is None:
                raise OpError("无法确定模型面板当前选中项", code="no_cursor")
            if sel == target_n:
                return d
            budget[0] -= 1
            if budget[0] < 0:
                raise OpError("导航步数超过上限", code="nav_limit")
            d = self._model_step(s, "Down" if target_n > sel else "Up", d)

    def _collect_models(self, s: _Session, d: dict):
        rows: dict = {}

        def absorb(dd):
            for r in dd.get("rows") or []:
                if isinstance(r.get("n"), int):
                    rows[r["n"]] = {"n": r["n"], "name": r.get("name", ""), "desc": r.get("desc", ""),
                                    "current": bool(r.get("current"))}

        start_n = tui_parse.selected_model_n(d)
        if start_n is None:
            raise OpError("无法确定模型面板当前选中项", code="no_cursor")
        absorb(d)
        budget = [self.max_nav_steps * 2]
        # ↑ 滚到顶（从不在第一行按 ↑，避免回绕）
        while True:
            vis = [r["n"] for r in d.get("rows") or []]
            sel = tui_parse.selected_model_n(d)
            if not vis or (d.get("more_above", 0) == 0 and sel == min(vis)):
                break
            budget[0] -= 1
            if budget[0] < 0:
                raise OpError("模型列表收集步数超过上限", code="nav_limit")
            d = self._model_step(s, "Up", d)
            absorb(d)
        # ↓ 逐行到底
        while True:
            vis = [r["n"] for r in d.get("rows") or []]
            sel = tui_parse.selected_model_n(d)
            if not vis or (d.get("more_below", 0) == 0 and sel == max(vis)):
                break
            budget[0] -= 1
            if budget[0] < 0:
                raise OpError("模型列表收集步数超过上限", code="nav_limit")
            d = self._model_step(s, "Down", d)
            absorb(d)
        # 回到原光标行
        d = self._model_nav_to(s, d, start_n, budget)
        ordered = [rows[n] for n in sorted(rows)]
        for i, r in enumerate(ordered):
            r["index"] = i
        cursor = next((r["index"] for r in ordered if r["n"] == start_n), None)
        return ordered, cursor, d

    def _open_panel(self, s: _Session, command: str, kind: str, clear_input: bool) -> dict:
        st = self._fresh(s)
        d = st.get("dialog")
        if d and d.get("kind") == kind:
            return st
        if d or self._blocking_dialog(s, st):
            raise OpError("请先处理当前对话框", code="dialog_open")
        if st.get("busy"):
            raise OpError("Claude 正在工作，请等待完成或先中断", code="busy")
        if not (st.get("input") or {}).get("visible"):
            raise OpError("输入框不可见，请稍后再试", code="no_input")
        self._ensure_input_empty(s, st, clear_input)
        # 粘贴命令后先确认补全列表高亮的正是该命令（前缀相同的自定义命令不能被 Enter 选中执行）
        self._submit_text(s, command, slash=command)
        st2 = self._wait_for(s, lambda x: (x.get("dialog") or {}).get("kind") == kind, self.panel_timeout)
        if st2 is None:
            raise OpError("%s 面板没有出现，请在终端面板查看" % command, code="panel_timeout")
        return st2

    def open_model_picker(self, sid, clear_input: bool = False) -> dict:
        s = self._get(sid)
        with self._op(s):
            st = self._open_panel(s, "/model", "model", bool(clear_input))
            rows, cursor, d = self._collect_models(s, st["dialog"])
            with s.lock:
                s.model_rows = rows
                for r in rows:
                    if r["current"]:
                        s.model = r["name"]
            out = {"rows": rows, "cursor": cursor, "effort": d.get("effort")}
        self._app(s, dict({"type": "model_list"}, **out))
        self._emit_meta(s)
        return out

    def apply_model(self, sid, index, effort=None, scope="session") -> dict:
        s = self._get(sid)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("index 必须是非负整数")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ValueError("effort 不合法（可选：%s）" % "/".join(EFFORT_LEVELS))
        if scope not in SCOPES:
            raise ValueError("scope 必须是 session 或 save")
        with self._op(s):
            st = self._fresh(s)
            d = self._model_dialog(st)
            if d is None:
                raise OpError("模型面板未打开，请先打开「切换模型」", code="no_model_panel")
            with s.lock:
                rows = list(s.model_rows or [])
            if not rows:
                rows, _c, d = self._collect_models(s, d)
                with s.lock:
                    s.model_rows = rows
            if index >= len(rows):
                raise ValueError("模型不存在")
            budget = [self.max_nav_steps]
            d = self._model_nav_to(s, d, rows[index]["n"], budget)
            if effort is not None:
                d = self._adjust_effort(s, d, effort, lambda dd: (dd.get("effort") or {}).get("level"),
                                        self._model_dialog)
            st2 = self._confirm_panel(s, scope, "model")
        with s.lock:
            s.model_rows = None
        self._emit_meta(s)
        return {"ok": True, "result": st2.get("last_result"), "state": st2}

    def _adjust_effort(self, s: _Session, d: dict, level: str, get_level, get_dialog) -> dict:
        seen = []
        for _ in range(self.max_effort_steps + 1):
            cur = get_level(d)
            if cur is None:
                raise OpError("当前面板无法调整 effort（该模型可能不支持）", code="effort_unsupported")
            if cur == level:
                return d
            if cur in seen:
                raise OpError("无法把 effort 调到 %s（该模型可能不支持此档位）" % level, code="effort_unsupported")
            seen.append(cur)
            key = "Right" if EFFORT_LEVELS.index(level) > EFFORT_LEVELS.index(cur) else "Left"
            self._keys(s, [key])
            st2 = self._wait_for(s, lambda x: get_dialog(x) is not None and get_level(get_dialog(x)) != cur,
                                 self.step_timeout)
            if st2 is None:
                raise OpError("无法把 effort 调到 %s（面板没有响应）" % level, code="effort_unsupported")
            d = get_dialog(st2)
        raise OpError("effort 调整步数超过上限", code="nav_limit")

    @staticmethod
    def _panel_confirm_key(hint: str, scope: str) -> str:
        """按面板提示行决定确认键（绝不退回去盲按 Enter）：
        - 含 "s to use this session only" → 仅本会话 = s；
        - 只有 "Enter to use this session only"（实测 /effort 面板在 max 档）→ 仅本会话 = Enter（CLI 明示的仅本会话）；
        - 保存为默认 = Enter，且提示行必须含 "Enter to save"；
        - 其它情况一律拒绝。"""
        has_s = "s to use this session only" in hint
        enter_session = "Enter to use this session only" in hint
        enter_save = "Enter to save" in hint
        if scope == "session":
            if has_s:
                return "s"
            if enter_session and not enter_save:
                return "Enter"
        elif enter_save:
            return "Enter"
        elif has_s or enter_session:
            raise OpError("该档位在 CLI 里只能「仅本会话」使用，不能保存为默认", code="save_unsupported")
        raise OpError("面板提示行不是预期的「s to use this session only / Enter to save」，没有按键；"
                      "请在终端面板查看", code="unexpected_panel")

    def _confirm_panel(self, s: _Session, scope: str, kind: str) -> dict:
        st = self._fresh(s)
        d = st.get("dialog")
        if not d or d.get("kind") != kind:
            raise OpError("面板已关闭", code="panel_closed")
        hint = d.get("hints") or ""
        key = self._panel_confirm_key(hint, scope)
        with s.lock:
            line = _raw_line(s.screen_text, hint) if hint else None
        if line is None:
            raise OpError("面板提示行无法定位，没有按键；请在终端面板查看", code="unexpected_panel")
        # agent 发键前复核提示行仍是这一行（例如档位被调到 max 后提示行会变）
        self._keys(s, [key], guard={"lines": [line]})
        st2 = self._wait_for(s, lambda x: (x.get("dialog") or {}).get("kind") != kind
                             and (x.get("last_result") is not None or not x.get("dialog")),
                             self.result_timeout)
        if st2 is None:
            raise OpError("已按下确认键，但面板没有在 %g 秒内关闭，结果未知；请在终端面板查看" % self.result_timeout,
                          code="panel_timeout", status=504)
        return st2

    # ---------------------------------------------------------------- /effort
    def apply_effort(self, sid, level, scope="session", clear_input: bool = False) -> dict:
        s = self._get(sid)
        if level not in EFFORT_LEVELS:
            raise ValueError("level 不合法（可选：%s）" % "/".join(EFFORT_LEVELS))
        if scope not in SCOPES:
            raise ValueError("scope 必须是 session 或 save")

        def eff_dialog(x):
            dd = (x or {}).get("dialog")
            return dd if dd and dd.get("kind") == "effort" else None

        with self._op(s):
            st = self._open_panel(s, "/effort", "effort", bool(clear_input))
            d = self._adjust_effort(s, st["dialog"], level, lambda dd: dd.get("level"), eff_dialog)
            st2 = self._confirm_panel(s, scope, "effort")
        with s.lock:
            s.effort = level
        self._emit_meta(s)
        return {"ok": True, "result": st2.get("last_result"), "state": st2}

    # ---------------------------------------------------------------- 关闭 / 重启 / 删除
    def _force_local_exit(self, s: _Session, why: str) -> None:
        """只在本地把会话标为已退出（远端可能仍在运行：用户明确选择了"强制"，或作业已结束）。"""
        self._mark_exited(s, s.exit_status)
        with s.lock:
            s.attached = False
        if why:
            self._notice(s, "warn", why)
        self._dirty.set()

    def _close_session(self, s: _Session, force: bool = False, linger: bool = False) -> None:
        """调用方持有 s.oplock。发 /exit（必要时先 Esc），等退出，否则 kill；无剩余会话时 stop_keeper 并关 link
        （linger=True：马上要在同一作业上重启，保留 keeper/link）。

        与计算节点连不上时：先同步查一次作业状态——作业已结束 → 直接在本地标为已退出（之后可在新作业上 --resume）；
        作业还在（或查不到）→ 409 link_down（force=True 时只在本地标记，远端可能残留）。"""
        self.notifier.mute(s.sid)   # 用户主动关闭/重启：退出过程中的对话框（如后台 shell 确认）不响
        with s.lock:
            s.done_pending = False
            s.done_pending_since = None
            if not s.live():
                s.attached = False
                return
            s.closing = True
        jobid = s.jobid
        try:
            if self._link_state(jobid) == "connecting":
                with self._lock:
                    ev = self._link_ready.get(jobid)
                if ev is not None:
                    ev.wait(self.connect_grace)
            if self._link_state(jobid) != "ready":
                self._op_note(s, "与%s的连接已断开，正在核实%s…" % (_place(jobid), "本机 agent" if remote.is_local(jobid)
                                                                else "作业状态（squeue）"))
                ended = self._job_ended(jobid)
                if ended:
                    self._force_local_exit(s, "")   # _on_job_ended 已发过提示
                    return
                local = remote.is_local(jobid)
                if force:
                    self._force_local_exit(s, "已强制结束本地会话：与本机 agent 的连接断开，本机 tmux 里的 claude 可能仍在运行"
                                           if local else
                                           "已强制结束本地会话：与计算节点的连接断开，远端的 claude 可能仍在运行")
                    return
                if local:
                    raise OpError("与本机 agent 的连接已断开，无法关闭本机 tmux 里的 claude。请稍后重试；"
                                  "确定不再需要的话可以强制操作（本机 claude 进程可能残留）", code="link_down")
                raise OpError("与计算节点的连接已断开（作业 %s %s），无法关闭远端的 claude。请稍后重试；"
                              "确定不再需要的话可以强制操作（远端进程可能残留）"
                              % (jobid, "仍在运行" if ended is False else "状态未知"), code="link_down")
            exited = s.exit_evt.is_set()
            if not exited:
                try:
                    st = self._fresh(s)
                    for _ in range(3):
                        if not (st.get("dialog") or st.get("busy")):
                            break
                        self._keys(s, ["Escape"])
                        st = self._wait_for(s, lambda x: not x.get("dialog") and not x.get("busy"),
                                            self.step_timeout) or self._fresh(s)
                    if (st.get("input") or {}).get("visible") and not self._blocking_dialog(s, st):
                        try:
                            self._ensure_input_empty(s, st, True)
                        except OpError:
                            pass
                        # /exit 同样先确认补全列表高亮的就是 /exit，再带守卫按 Enter
                        self._op_note(s, "在终端里输入 /exit…")
                        self._submit_text(s, "/exit", slash="/exit")
                        self._op_note(s, "等待 claude 退出（最多 %d 秒）…" % int(self.exit_timeout))
                        exited = self._wait_exit(s, self.exit_timeout)
                except OpError as e:
                    if isinstance(e, LinkNotReady):
                        raise
                    log.info("关闭会话 %s：/exit 失败（%s），改为 kill", s.sid, e)
            if not exited:
                self._op_note(s, "claude 没有按时退出，正在结束旧的 tmux 会话…")
            self._req(jobid, "kill", sid=s.sid, timeout=20)
            if not exited:
                self._notice(s, "info", "claude 未在 %d 秒内退出，已结束 tmux 会话" % int(self.exit_timeout))
            self._mark_exited(s, s.exit_status)
            with s.lock:
                s.attached = False
            self._dirty.set()
            if not linger:
                self._maybe_stop_keeper(jobid)
        finally:
            with s.lock:
                s.closing = False

    def _wait_exit(self, s: _Session, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        next_list = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if s.exit_evt.wait(0.2):
                return True
            if time.monotonic() >= next_list:
                next_list = time.monotonic() + 1.5
                try:
                    items = self._req(s.jobid, "list", timeout=15)
                except OpError:
                    continue
                it = next((x for x in items or [] if isinstance(x, dict) and x.get("sid") == s.sid), None)
                if it is None or it.get("pane_dead"):
                    with s.lock:
                        if it is not None and isinstance(it.get("exit_status"), int):
                            s.exit_status = it.get("exit_status")
                    return True
        return False

    def close(self, sid, force: bool = False) -> dict:
        s = self._get(sid)
        with self._op(s, timeout=60):
            try:
                self._close_session(s, force=bool(force))
            except OpError as e:
                if not force:
                    raise
                log.warning("强制关闭会话 %s：%s", sid, e)
                self._force_local_exit(s, "已强制结束本地会话（%s），%s的 claude 可能仍在运行"
                                       % (e, "本机" if remote.is_local(s.jobid) else "远端"))
        self._emit_meta(s)
        return {"ok": True}

    def restart(self, sid, jobid=None, force: bool = False, api_key_id=_KEEP, mode=None, _cont=None) -> dict:
        """关闭旧会话（若仍在）→ 在（新）作业上 start，带 --resume <claude_session_id>，沿用 launch 参数
        （会话里改过的模型/effort/权限模式用当前值，见 _carry_launch_locked）。
        旧作业已结束时不需要连上旧作业：直接在新作业上续接；不给新作业则 409 job_ended。
        api_key_id：不传 = 沿用；None/"" = 改用服务器默认；id = 改用该 key（claude 只在启动时读取 key，
        所以换 API = 以 --resume 续接重启）。key 已被删除 → 409 api_key_missing（旧会话不受影响）。
        本机会话只能在本机重启、服务器会话只能换到服务器作业（transcript 在各自机器上，--resume 跨机器找不到）：
        跨运行位置 → 409 target_mismatch（什么都不动）。
        mode：覆盖权限启动参数（模式选择栏里"需重启"的模式，SPEC §13）：bypassPermissions → --dangerously-skip-permissions；
        其它（dontAsk 等）→ --permission-mode <mode>（去掉跳过权限）。在"会话里改过的模型/effort/模式沿用当前值"之后应用。

        阶段进度：一进来就在 meta.op 里报告（停止旧 claude → 连接 → keeper → 启动 → 等待就绪），经 SSE 推给前端。
        幂等：同一会话已有进行中的重启/续接/新建时不再启动第二次，直接返回当前进度（meta 带 op_reused=true）。
        _cont：内部用（--resume 报 No conversation found 后自动改为新会话启动），接着用这个进度。"""
        s = self._get(sid)
        if mode is not None and (not isinstance(mode, str) or mode not in PERMISSION_MODES):
            raise ValueError("mode 不合法（可选：%s）" % "/".join(PERMISSION_MODES))
        new_job = remote.validate_target(str(jobid).strip()) if jobid else None
        if new_job is not None:
            self._require_server_host(new_job)
        if new_job is not None and remote.is_local(new_job) != remote.is_local(s.jobid):
            raise OpError("本机会话只能在本机续接，服务器会话只能在服务器作业上续接（claude 的对话记录不在同一台机器上）",
                          code="target_mismatch")
        change_key = api_key_id is not _KEEP
        new_kid = ccr_api_keys.validate_id_opt(api_key_id) if change_key else None
        # "服务器默认"条目视同 null（在 s.lock 之外查 api_keys，避免与它的变更回调交叉加锁）
        switching = change_key and self._norm_kid(new_kid) != self._norm_kid((s.launch or {}).get("api_key_id"))
        with s.lock:   # 检查与登记在同一把锁里：并发的重复请求只有一个会真正执行
            cur = self._op_live(s)
            if cur is not None and not (_cont is not None and cur.get("id") == _cont):
                reused = True
            else:
                reused = False
                target = new_job or s.jobid
                live = s.live()
                if cur is not None:   # 接着自动重试（_cont）：沿用种类与开始时间
                    kind, started = cur.get("kind"), cur.get("started_at")
                elif new_job is not None and new_job != s.jobid:
                    kind, started = "resume_new_job", None
                elif mode is not None:
                    kind, started = "mode", None
                elif switching:
                    kind, started = "switch_api", None
                else:
                    kind, started = ("restart" if live else "reconnect"), None
                ids = (["stop"] if live else []) + self._op_link_ids(target) + ["keeper", "start", "ready"]
                op = self._op_begin_locked(s, kind, ids, target, resume=s.claude_session_id, started_at=started,
                                           detail=cur.get("detail") if cur is not None else None)
                op_id = op["id"]
        if reused:
            log.info("会话 %s 已有进行中的重启/续接/启动，不重复执行", sid)
            return dict(self.get_meta(sid), op_reused=True)
        self._emit_meta(s)   # 点下去立刻有反馈（进度经 SSE 推送；HTTP 响应在停止旧会话之后才返回）
        handed_off = False
        try:
            if not s.oplock.acquire(timeout=0.05):
                self._op_note(s, "等待该会话当前的操作结束…", op_id)
                if not s.oplock.acquire(timeout=60):
                    raise OpError("该会话正在执行其它操作（例如发送消息、切换模型），请稍候再试", code="op_in_progress")
            try:
                old_job = s.jobid
                same = new_job is None or new_job == old_job
                if same and self._link_state(old_job) == "ended":
                    raise OpError("作业 %s 已结束，请选择新的作业来续接本会话" % old_job, code="job_ended")
                with s.lock:   # 关闭旧会话之前：先确定新 launch 并取到 key（key 被删时什么都不动）
                    carry = self._carry_launch_locked(s)
                    if change_key:
                        carry["api_key_id"] = new_kid
                    if mode == "bypassPermissions":
                        carry["skip_permissions"], carry["permission_mode"] = True, None
                    elif mode is not None:
                        carry["skip_permissions"], carry["permission_mode"] = False, mode
                    name_hint = s.api_key_name
                carry["api_key_id"] = self._norm_kid(carry.get("api_key_id"))
                key, key_name = self._resolve_api_key(carry.get("api_key_id"), name_hint, restart=True)
                try:
                    # linger：同作业马上要重启，保留 keeper/link；换作业时旧作业的清理放到后台（见下）
                    self._close_session(s, force=bool(force), linger=True)
                except OpError as e:
                    if not force:
                        raise
                    log.warning("强制重启会话 %s：%s", sid, e)
                    self._force_local_exit(s, "已强制结束本地会话（%s），%s的 claude 可能仍在运行"
                                           % (e, "本机 tmux 里" if remote.is_local(old_job) else "旧作业里"))
                if not same:
                    # 旧作业已无会话：停 keeper、关 link（连不上 / 已结束时只关 link）。放到后台：旧作业可能已经
                    # 没有响应（例如正在被 Slurm 回收），不能让它拖住这次续接
                    threading.Thread(target=self._stop_old_job_quiet, args=(old_job,), name="ccr-oldjob-" + old_job,
                                     daemon=True).start()
                if same and self._link_state(old_job) == "ended":
                    raise OpError("作业 %s 已结束，请选择新的作业来续接本会话" % old_job, code="job_ended")
                with s.lock:
                    cid = s.claude_session_id
                    launch = carry
                    # 还没有任何对话（新会话只执行过 /model 等，或实测 --resume 报 No conversation found）：
                    # --resume 会直接失败退出，改为以新会话启动（同目录/模型/权限/新 key，没有可丢的历史）
                    fresh = bool(cid) and (cid in s.tx_noconv or (cid in s.tx_loaded and cid not in s.tx_conv))
                resume_id = None if fresh else cid
                if fresh:
                    log.info("会话 %s（claude %s）还没有对话，重启时以新会话启动（不带 --resume）", sid, cid)
                self._check_unique_resume(resume_id, me=s)
                args = self._build_args(launch, resume_id)
                target = new_job or old_job
                with s.lock:
                    if new_job:
                        s.jobid = new_job
                    s.launch = launch
                    s.api_key_name, s.api_key_missing = key_name, False
                    s.seen_base = {}
                    s.modes_seen = set()
                    s.pane_pid = None
                    s.exited = False
                    s.exit_status = None
                    s.exit_evt = threading.Event()
                    s.attached = True
                    s.starting = True
                    s.error = None
                    s.status = None
                    s.claude_pid = None
                    s.screen_text = s.screen_state = None
                    s.screen_pub_text = None
                    s.model_rows = None
                    s.tracker.stop()   # 新进程：进行中的一轮作废；上下文/花费沿用最后已知值，直到新屏幕到达
                    self._stats_update_locked(s)
                    s.dialog_kind = None
                    s.state = "starting"
                    op = self._op_live(s, op_id)
                    if op is not None:
                        ccr_op.relabel_start(op, resume_id)
                        nxt = (self._op_link_ids(target) or ["keeper"])[0]
                        ccr_op.set_phase(op, nxt)
                self._ensure_link(s.jobid)
                with s.lock:
                    s.link = self._link_state(s.jobid)
                self._emit_meta(s)
                threading.Thread(target=self._start_worker, args=(s, args, key, op_id), name="ccr-start-" + s.sid,
                                 daemon=True).start()
                handed_off = True
            finally:
                if not handed_off:
                    s.oplock.release()
        except OpError as e:
            if e.api_code in ("job_ended", "api_key_missing", "target_mismatch", "link_down"):
                self._op_clear(s, op_id)   # 前端会接着弹出对应的选择（选新作业 / 改选 key / 强制…），进度收起
            elif e.api_code in ("op_in_progress", "already_running"):
                self._op_finish(s, op_id, "failed", error=str(e), code=e.api_code)
            else:
                self._op_fail_from(s, op_id, e, new_job or s.jobid)
            raise
        except Exception as e:  # noqa: BLE001
            self._op_finish(s, op_id, "failed", error=ccr_op.clip(str(e) or type(e).__name__))
            raise
        return self.get_meta(sid)

    def _stop_old_job_quiet(self, jobid: str) -> None:
        try:
            self._maybe_stop_keeper(jobid)
        except Exception:  # noqa: BLE001
            log.exception("清理旧作业 %s 失败（忽略）", jobid)

    def remove(self, sid, force: bool = False) -> dict:
        s = self._get(sid)
        try:
            with self._op(s, timeout=60):
                self._close_session(s, force=bool(force))
        except OpError:
            if not force:
                raise
            log.warning("强制删除会话 %s（远端可能仍在运行）", sid)
        with self._lock:
            self._sessions.pop(sid, None)
        self.notifier.forget(sid)
        self._dirty.set()
        self._maybe_close_link(s.jobid)
        return {"ok": True}

    def rename(self, sid, title) -> dict:
        s = self._get(sid)
        t = _clean_title(title, "")
        with s.lock:
            if t:
                s.title, s.title_user = t, True
            else:   # 清空 = 不再用自己起的名字，改回显示 Claude Code 里的标题
                s.title_user = False
        self._emit_meta(s)
        return {"ok": True}

    def reorder(self, sids) -> dict:
        """侧栏手动排序：sids = 从上到下的会话 id（可以只是一部分，没列出的保持原值）。"""
        if not isinstance(sids, list) or len(sids) > 1000 or not all(isinstance(x, str) for x in sids):
            raise ValueError("sids 必须是会话 id 列表")
        changed = []
        with self._lock:
            for i, sid in enumerate(sids):
                s = self._sessions.get(sid)
                if s is not None and s.order != float(i):
                    s.order = float(i)
                    changed.append(s)
        for s in changed:
            self._emit_meta(s)
        return {"ok": True, "changed": len(changed)}

    def shutdown(self) -> None:
        """app 退出：**不关闭**远端 claude（keeper 保活），只关闭 link、保存状态。"""
        self._shutdown = True
        with self._lock:
            links = list(self._links.values())
            ss = list(self._sessions.values())
            subs = list(self._subs)
        for s in ss:
            with s.lock:
                timers = (s.screen_timer, s.stats_timer)
                s.screen_timer = s.stats_timer = None
            for t in timers:
                if t is not None:
                    t.cancel()
        for link in links:
            try:
                link.close()
            except Exception:  # noqa: BLE001
                log.exception("关闭 agent 连接失败")
        for sub in subs:
            sub.close()
        self._persist_stop = True
        self._dirty.set()
        self._persister.join(5)
        self._persist_now()

    # ================================================================ 持久化
    def _record(self, s: _Session) -> dict:
        with s.lock:
            m = s.meta()
            m.pop("op", None)
            return {"meta": m, "attached": bool(s.attached and not s.exited), "exited": bool(s.exited),
                    "seen_base": dict(s.seen_base), "done_pending": bool(s.done_pending)}

    def _persist_loop(self) -> None:
        while not self._persist_stop:
            self._dirty.wait()
            if self._persist_stop:
                break
            time.sleep(0.1)
            if self._persist_stop:
                break
            self._dirty.clear()
            self._persist_now()

    def _persist_now(self) -> None:
        with self._persist_lock:
            with self._lock:
                ss = list(self._sessions.values())
            data = {"version": 2, "saved_at": time.time(), "sessions": [self._record(s) for s in ss]}
            tmp = self.data_dir / (".sessions.json.%d.%s.tmp" % (os.getpid(), uuid.uuid4().hex[:8]))
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._sessions_path)
            except OSError:
                log.exception("persist sessions.json failed")
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def flush(self) -> None:
        self._persist_now()

    def _load(self) -> None:
        p = self._sessions_path
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            recs = data.get("sessions") if isinstance(data, dict) else None
            if not isinstance(recs, list):
                raise ValueError("bad format")
        except (OSError, ValueError) as e:
            log.warning("sessions.json unreadable (%s), backing up", e)
            try:
                os.replace(p, p.with_name("sessions.json.corrupt-%d" % int(time.time())))
            except OSError:
                pass
            return
        v2 = isinstance(data, dict) and data.get("version") == 2
        for rec in recs:
            try:
                s = self._restore(rec, v2)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            if s is not None:
                self._sessions[s.sid] = s

    def _restore(self, rec: dict, v2: bool) -> Optional[_Session]:
        m = rec["meta"]
        sid = m["sid"]
        if not isinstance(sid, str) or not _SID_RE.match(sid):
            return None
        s = _Session(sid, self._max_events)
        s.title = _clean_title(m.get("title"), "会话")
        ct = m.get("cli_title")
        s.cli_title = ct if isinstance(ct, str) and 0 < len(ct) <= 200 and "\n" not in ct else None
        tu = m.get("title_user")
        s.title_user = tu if isinstance(tu, bool) else None   # 旧记录：下面按"像不像默认标题"判断
        od = m.get("order")
        s.order = float(od) if isinstance(od, (int, float)) and not isinstance(od, bool) and abs(od) < 1e9 else None
        try:
            s.jobid = remote.validate_target(m.get("jobid") or "")   # 作业 jobid 或本机 LOCAL_JOBID
        except ValueError:
            s.jobid = ""
        cwd = m.get("cwd")
        s.cwd = cwd if isinstance(cwd, str) and cwd.startswith(("/", "~")) and "\n" not in cwd else "~"
        if s.title_user is None and _default_like_title(s.title, s.cwd):
            s.title_user = False
        cid = m.get("claude_session_id")
        s.claude_session_id = cid if isinstance(cid, str) and _UUID_RE.match(cid) else None
        for k in ("created_at", "last_activity"):
            v = m.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                setattr(s, k, float(v))
        # 新回复：done_at / seen_at 是秒级时间戳（有限、正、不离谱）；seen_at 没有 done_at 时无意义
        for k in ("done_at", "seen_at"):
            v = m.get(k)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 1e9 < v < 1e11
            setattr(s, k, float(v) if ok else None)
        if s.done_at is None:
            s.seen_at = None
        if v2:
            la = m.get("launch") if isinstance(m.get("launch"), dict) else {}
            kid = la.get("api_key_id")
            if kid is not None and not (isinstance(kid, str) and ccr_api_keys.ID_RE.match(kid)):
                log.warning("会话 %s 的 api_key_id 不合法，改为服务器默认", sid)
                kid = None
            try:
                s.launch = self._validate_launch(la.get("model"), la.get("permission_mode"),
                                                 bool(la.get("skip_permissions")), la.get("effort"), kid)
            except ValueError:
                s.launch = _empty_launch()
            for k in ("mode", "model", "effort", "status"):
                v = m.get(k)
                setattr(s, k, v if isinstance(v, str) and len(v) < 200 else None)
            v = m.get("api_key_name")
            s.api_key_name = v if isinstance(v, str) and len(v) <= ccr_api_keys.MAX_NAME else None
            base = rec.get("seen_base")
            if isinstance(base, dict):
                s.seen_base = {k: v for k, v in base.items()
                               if k in ("model", "effort", "mode") and isinstance(v, str) and len(v) < 200}
            # 统计：恢复最后已知的上下文/花费/上一轮速率（存活会话随后由屏幕/transcript 刷新）
            s.stats = stats.sanitize(m.get("stats"))
            s.tracker.restore(s.stats)
            alive = bool(rec.get("attached")) and not rec.get("exited") and bool(s.jobid)
        else:
            # v1（headless）记录：不继承任何启动参数（红线 §0.2：只用用户在 v2 界面上明确选择的参数）
            s.launch = _empty_launch()
            alive = False
        if alive:
            s.attached = True
            st = m.get("state")
            s.state = st if st in ("trust", "idle", "busy", "waiting") else "starting"
            s.link = "connecting"
            # 停止时还在回复（或上次启动后还没确认完）：重新附着后若已经空闲 = 这期间完成了，补记"新回复"
            s.done_pending = st == "busy" or rec.get("done_pending") is True
        else:
            s.attached = False
            s.exited = True
            s.state = "exited"
            s.link = "down"
        return s
