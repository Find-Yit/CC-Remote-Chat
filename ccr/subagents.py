"""子代理（subagent）状态汇总（纯数据、无 I/O；由 ccr.session 在 s.lock 下调用）。

数据来源（都是 Claude Code 自己写的文件，经 node agent 只读转发）：
- 子代理 transcript `<sessionId>/subagents/agent-<id>.jsonl`（只含 user/assistant 条目）与 `agent-<id>.meta.json`
  （`{"agentType","description","toolUseId","model","requestShape","finished",…}`）；
- 主 transcript 里的 Agent/Task 工具调用（description/prompt/subagent_type/model/run_in_background）、
  启动结果（`toolUseResult.agentId` 或 tool_result 文本里的 `agentId: …`），以及子代理结束时注入的
  `<task-notification>…<status>completed</status>…<result>…</result><usage>…</usage></task-notification>`
  （条目带 `origin.kind == "task-notification"`、`promptSource == "system"`）。

状态：有 task-notification 时以其 status 为准（之后若又有新活动 = 被 SendMessage 续跑 → running）；
否则 meta.finished 为真 → completed；否则 running。会话已退出时 running 视为 killed。
"""
from __future__ import annotations

import collections
import datetime
import json
import re
from typing import Dict, List, Optional, Tuple

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
AGENT_TOOLS = ("Agent", "Task")
STATUSES = ("running", "completed", "failed", "killed")
MAX_AGENTS = 200                 # 每个会话最多保留的子代理
MAX_ENTRIES = 2000               # 每个子代理最多缓存的条目
MAX_BYTES = 8 * 1024 * 1024      # 每个子代理缓存条目的（序列化）字节上限
MAX_CALLS = 2000
MAX_PROMPT = 4000
MAX_RESULT = 8000
_AGENT_ID_TEXT_RE = re.compile(r"\bagentId:\s*([A-Za-z0-9_-]{1,64})")
_TAG_RE = {k: re.compile(r"<%s>([\s\S]*?)</%s>" % (k, k)) for k in
           ("task-id", "tool-use-id", "status", "summary", "output-file", "subagent_tokens", "tool_uses",
            "duration_ms", "total_tokens")}
_RESULT_RE = re.compile(r"<result>([\s\S]*)</result>")


def iso_ts(v) -> Optional[float]:
    """ISO 时间串（如 2026-11-22T12:48:52.425Z）→ epoch 秒；不合法返回 None。"""
    if not isinstance(v, str) or not v or len(v) > 40:
        return None
    s = v.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def _str(v, n=None) -> str:
    s = v if isinstance(v, str) else ("" if v is None else str(v))
    return s[:n] if n is not None else s


def _first_line(s: str, n: int = 80) -> str:
    s = _str(s).strip()
    line = s.split("\n", 1)[0].strip()
    if len(line) > n:
        line = line[:n] + "…"
    elif "\n" in s:
        line += " …"
    return line


def _basename(p) -> str:
    p = _str(p).rstrip("/")
    return p.rsplit("/", 1)[-1] if p else ""


def describe_action(name, inp) -> str:
    """工具调用 → 一行简短描述（"Read foo.py"、"Bash: pytest -q"…）。"""
    n = _str(name) or "工具"
    i = inp if isinstance(inp, dict) else {}
    if n in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return ("%s %s" % (n, _basename(i.get("file_path") or i.get("notebook_path")))).strip()
    if n in ("Bash", "BashOutput", "KillShell"):
        return "%s: %s" % (n, _first_line(i.get("command") or i.get("bash_id") or i.get("shell_id") or "", 70))
    if n in ("Grep", "Glob"):
        return "%s %s" % (n, _first_line(i.get("pattern"), 60))
    if n == "WebFetch":
        return "WebFetch %s" % _first_line(i.get("url"), 70)
    if n == "WebSearch":
        return "WebSearch %s" % _first_line(i.get("query"), 60)
    if n in AGENT_TOOLS:
        return "%s: %s" % (n, _first_line(i.get("description"), 60))
    if n == "TodoWrite":
        return "更新待办清单"
    m = re.match(r"^mcp__(.+?)__(.+)$", n)
    label = "%s · %s" % (m.group(1), m.group(2)) if m else n
    for v in i.values():
        if isinstance(v, str) and v.strip():
            return "%s %s" % (label, _first_line(v, 50))
    return label


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_str(b.get("text")) for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def notification_text(entry: dict) -> Optional[str]:
    """主 transcript 里的 <task-notification> 用户条目 → 原文；其它返回 None。"""
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return None
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    text = _content_text(msg.get("content")).strip()
    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
    if text.startswith("<task-notification>") or (origin.get("kind") == "task-notification"
                                                  and "<task-notification>" in text):
        return text
    return None


def _norm_status(raw: str) -> str:
    s = _str(raw).strip().lower()
    if s in ("completed", "complete", "done", "success", "succeeded", "finished"):
        return "completed"
    if any(w in s for w in ("fail", "error")):
        return "failed"
    if any(w in s for w in ("kill", "stop", "cancel", "abort", "interrupt", "terminat")):
        return "killed"
    return "completed"


def _int(v) -> Optional[int]:
    try:
        return int(_str(v).strip())
    except ValueError:
        return None


def parse_notification(text: str) -> Optional[dict]:
    if not isinstance(text, str) or "<task-notification>" not in text:
        return None

    res = _RESULT_RE.search(text)
    head = text[:res.start()] if res else text      # 头部字段在 <result> 之前，usage 在其后
    tail = text[res.end():] if res else text

    def tag(k, src=head):
        m = _TAG_RE[k].search(src)
        return m.group(1).strip() if m else None

    usage = {}
    for k in ("subagent_tokens", "total_tokens", "tool_uses", "duration_ms"):
        v = tag(k, tail)
        v = _int(v) if v is not None else None
        if v is not None:
            usage[k] = v
    raw = tag("status") or ""
    result = res.group(1).strip() if res else None
    return {
        "task_id": tag("task-id"), "tool_use_id": tag("tool-use-id"), "raw_status": raw[:40],
        "status": _norm_status(raw), "summary": _str(tag("summary"), 500) or None,
        "result": result[:MAX_RESULT] if result is not None else None,
        "result_truncated": bool(result and len(result) > MAX_RESULT),
        "usage": usage or None,
    }


def _jlen(o) -> int:
    try:
        return len(json.dumps(o, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


# ============================================================================ 单个子代理
class Sub:
    def __init__(self, agent_id: str, gen: int):
        self.agent_id = agent_id
        self.gen = gen
        self.meta: Optional[dict] = None
        self.entries: List[dict] = []
        self.sizes: List[int] = []
        self.base = 0                 # 因缓存上限从前面丢掉的条目数
        self.nbytes = 0
        self.has_transcript = False   # 是否收到过来自子代理文件的事件
        self.tool_ids: set = set()
        self.tool_uses = 0
        self.last_action: Optional[str] = None
        self.started_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.prompt: Optional[str] = None
        self.last_text: Optional[str] = None
        self.uuids: set = set()

    @property
    def total(self) -> int:
        return self.base + len(self.entries)

    def absorb(self, entries: List[dict]) -> List[dict]:
        out = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            u = e.get("uuid")
            if isinstance(u, str) and u:
                if u in self.uuids:
                    continue
                if len(self.uuids) < 20000:
                    self.uuids.add(u)
            out.append(e)
            ts = iso_ts(e.get("timestamp"))
            if ts is not None:
                self.started_ts = ts if self.started_ts is None else min(self.started_ts, ts)
                self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content")
            if e.get("type") == "user":
                if self.prompt is None and not self.entries and self.base == 0:
                    t = _content_text(content)
                    if t.strip() and not (isinstance(content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
                        self.prompt = t[:MAX_PROMPT]
            elif e.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt in ("tool_use", "server_tool_use", "mcp_tool_use"):
                        tid = b.get("id")
                        key = tid if isinstance(tid, str) and tid else id(b)
                        if key not in self.tool_ids:
                            self.tool_ids.add(key)
                            self.tool_uses += 1
                        self.last_action = describe_action(b.get("name"), b.get("input"))
                    elif bt == "text" and _str(b.get("text")).strip():
                        self.last_text = _str(b.get("text"))[:MAX_RESULT]
                        self.last_action = "回复：" + _first_line(b.get("text"), 60)
            size = _jlen(e)
            self.entries.append(e)
            self.sizes.append(size)
            self.nbytes += size
        drop = 0
        while (len(self.entries) - drop > MAX_ENTRIES or self.nbytes > MAX_BYTES) and drop < len(self.entries) - 1:
            self.nbytes -= self.sizes[drop]
            drop += 1
        if drop:
            del self.entries[:drop]
            del self.sizes[:drop]
            self.base += drop
        return out


# ============================================================================ 每个会话的汇总
class Tracker:
    def __init__(self):
        self.subs: "collections.OrderedDict[str, Sub]" = collections.OrderedDict()
        self.calls: "collections.OrderedDict[str, dict]" = collections.OrderedDict()   # tool_use_id -> 调用
        self.launch: Dict[str, str] = {}          # tool_use_id -> agent_id
        self.launch_info: Dict[str, dict] = {}    # agent_id -> toolUseResult 摘要（resolvedModel 等）
        self.notes: Dict[str, dict] = {}          # agent_id -> 最近一次通知
        self.notes_tool: Dict[str, dict] = {}     # tool_use_id -> 最近一次通知（无 task-id 时）
        self.note_count: Dict[str, int] = {}
        self.killed: Dict[str, int] = {}          # agent_id -> 会话退出时的条目数（跨 reset 保留）
        self._gen = 0

    # ------------------------------------------------------------ 基本
    def clear(self) -> None:
        """主 transcript reset：子代理状态从头重建（killed 记录保留）。"""
        self.subs.clear()
        self.calls.clear()
        self.launch.clear()
        self.launch_info.clear()
        self.notes.clear()
        self.notes_tool.clear()
        self.note_count.clear()

    def _next_gen(self) -> int:
        self._gen += 1
        return self._gen

    def _ensure(self, aid: str) -> Sub:
        sub = self.subs.get(aid)
        if sub is None:
            sub = Sub(aid, self._next_gen())
            self.subs[aid] = sub
        return sub

    def _evict(self, exited: bool) -> List[str]:
        out = []
        while len(self.subs) > MAX_AGENTS:
            done = [(self._last(a), a) for a in self.subs if self.status(a, exited) != "running"]
            victim = min(done)[1] if done else next(iter(self.subs))
            self.subs.pop(victim, None)
            out.append(victim)
        return out

    def _last(self, aid: str) -> float:
        sub = self.subs.get(aid)
        note = self._note(aid)
        vals = [x for x in ((sub.last_ts if sub else None), (note or {}).get("ts"),
                            (sub.started_ts if sub else None)) if x is not None]
        return max(vals) if vals else 0.0

    def tool_use_id(self, aid: str) -> Optional[str]:
        sub = self.subs.get(aid)
        tid = ((sub.meta if sub else None) or {}).get("toolUseId")
        if isinstance(tid, str) and tid:
            return tid
        for t, a in self.launch.items():
            if a == aid:
                return t
        note = self.notes.get(aid)
        if note and note.get("tool_use_id"):
            return note["tool_use_id"]
        return None

    def _note(self, aid: str) -> Optional[dict]:
        n = self.notes.get(aid)
        if n is not None:
            return n
        tid = self.tool_use_id(aid)
        return self.notes_tool.get(tid) if tid else None

    # ------------------------------------------------------------ 主 transcript
    def absorb_main(self, entries) -> set:
        """吸收主 transcript 条目里的 Agent 调用 / 启动结果 / task-notification；返回摘要可能变化的 agent_id。"""
        changed = set()
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            typ = e.get("type")
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content")
            ts = iso_ts(e.get("timestamp"))
            if typ == "assistant" and isinstance(content, list):
                for b in content:
                    if (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in AGENT_TOOLS
                            and isinstance(b.get("id"), str)):
                        i = b.get("input") if isinstance(b.get("input"), dict) else {}
                        self.calls[b["id"]] = {
                            "description": _str(i.get("description"), 300) or None,
                            "prompt": _str(i.get("prompt"), MAX_PROMPT) or None,
                            "subagent_type": _str(i.get("subagent_type"), 100) or None,
                            "model": _str(i.get("model"), 100) or None,
                            "background": i.get("run_in_background") if isinstance(
                                i.get("run_in_background"), bool) else None,
                            "ts": ts, "tool": b.get("name"),
                        }
                        while len(self.calls) > MAX_CALLS:
                            self.calls.popitem(last=False)
                        aid = self.launch.get(b["id"])
                        if aid:
                            changed.add(aid)
                continue
            if typ != "user":
                continue
            text = notification_text(e)
            if text is not None:
                note = parse_notification(text)
                if note is None:
                    continue
                note["ts"] = ts
                aid = note.get("task_id") if AGENT_ID_RE.match(note.get("task_id") or "") else None
                tid = note.get("tool_use_id")
                if aid is None and tid:
                    aid = self.launch.get(tid)
                if tid and aid and tid not in self.launch:
                    self.launch[tid] = aid
                if aid:
                    self.notes[aid] = note
                    self.note_count[aid] = self.note_count.get(aid, 0) + 1
                    self._ensure(aid)
                    changed.add(aid)
                elif tid:
                    self.notes_tool[tid] = note
                continue
            if not isinstance(content, list):
                continue
            tur = e.get("toolUseResult") if isinstance(e.get("toolUseResult"), dict) else {}
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)):
                    continue
                tid = b["tool_use_id"]
                if tid not in self.calls and not tur.get("agentId"):
                    continue
                aid = tur.get("agentId") if isinstance(tur.get("agentId"), str) else None
                if not aid:
                    m = _AGENT_ID_TEXT_RE.search(_content_text(b.get("content")))
                    aid = m.group(1) if m else None
                if not aid or not AGENT_ID_RE.match(aid):
                    continue
                self.launch[tid] = aid
                info = {"resolved_model": _str(tur.get("resolvedModel"), 100) or None,
                        "async": tur.get("isAsync") is True or tur.get("status") == "async_launched"}
                self.launch_info[aid] = info
                st = tur.get("status")
                if not info["async"] and isinstance(st, str) and st and aid not in self.notes:
                    # 同步完成（旧版前台 Agent）：tool_result 本身就是结果
                    usage = {}
                    for src, dst in (("totalTokens", "total_tokens"), ("totalToolUseCount", "tool_uses"),
                                     ("totalDurationMs", "duration_ms")):
                        if isinstance(tur.get(src), int) and not isinstance(tur.get(src), bool):
                            usage[dst] = tur[src]
                    result = _content_text(b.get("content"))
                    self.notes[aid] = {"task_id": aid, "tool_use_id": tid, "raw_status": st[:40],
                                       "status": "failed" if b.get("is_error") else _norm_status(st),
                                       "summary": None, "result": result[:MAX_RESULT],
                                       "result_truncated": len(result) > MAX_RESULT, "usage": usage or None,
                                       "ts": ts}
                self._ensure(aid)
                changed.add(aid)
        return changed

    # ------------------------------------------------------------ 子代理事件
    def apply_event(self, aid: str, meta, entries, reset: bool, exited: bool = False
                    ) -> Optional[Tuple[Sub, List[dict], int, List[str]]]:
        """node agent 的 subagent 事件 → (Sub, 新条目, 新条目起始序号, 被淘汰的 agent_id)。"""
        if not isinstance(aid, str) or not AGENT_ID_RE.match(aid):
            return None
        sub = self.subs.get(aid)
        if sub is None:
            sub = self._ensure(aid)
        elif reset and (sub.entries or sub.base or sub.has_transcript):
            fresh = Sub(aid, self._next_gen())
            fresh.meta = sub.meta
            self.subs[aid] = sub = fresh
        sub.has_transcript = True
        if isinstance(meta, dict):
            sub.meta = {k: v for k, v in meta.items() if isinstance(k, str)}
        new = sub.absorb(entries if isinstance(entries, list) else [])
        if new and len(new) > len(sub.entries):   # 一批就超过缓存上限：只转发保留下来的部分
            new = new[-len(sub.entries):]
        start = sub.total - len(new)
        evicted = self._evict(exited)
        return sub, new, start, evicted

    def mark_exited(self) -> List[str]:
        """会话退出：记下仍在运行的子代理（之后没有新活动就一直显示为 killed）。"""
        out = []
        for aid, sub in self.subs.items():
            if self.status(aid, False) == "running":
                self.killed[aid] = sub.total
                out.append(aid)
        return out

    # ------------------------------------------------------------ 摘要
    def status(self, aid: str, exited: bool) -> str:
        sub = self.subs.get(aid)
        meta = (sub.meta if sub else None) or {}
        note = self._note(aid)
        if note is not None:
            st = note.get("status") or "completed"
            nts = note.get("ts")
            if (sub is not None and sub.last_ts is not None and nts is not None and sub.last_ts > nts + 1.0
                    and meta.get("finished") is not True):
                st = "running"   # 通知之后又有新活动：被 SendMessage 续跑
        elif meta.get("finished") is True:
            st = "completed"
        else:
            st = "running"
        if st == "running":
            if exited:
                return "killed"
            k = self.killed.get(aid)
            if k is not None and (sub is None or sub.total <= k):
                return "killed"
        return st

    def summary(self, aid: str, exited: bool = False) -> Optional[dict]:
        sub = self.subs.get(aid)
        if sub is None:
            return None
        meta = sub.meta or {}
        tid = self.tool_use_id(aid)
        call = self.calls.get(tid) if tid else None
        call = call or {}
        note = self._note(aid) or {}
        linfo = self.launch_info.get(aid) or {}
        status = self.status(aid, exited)
        started = sub.started_ts if sub.started_ts is not None else call.get("ts")
        last = max([x for x in (sub.last_ts, note.get("ts"), started) if x is not None], default=None)
        usage = note.get("usage") if isinstance(note.get("usage"), dict) else None
        ended = None
        if status != "running":
            ended = note.get("ts") if note.get("ts") is not None and status == note.get("status") else last
        dur = (usage or {}).get("duration_ms")
        if dur is None and started is not None and ended is not None and ended >= started:
            dur = int((ended - started) * 1000)
        result = note.get("result") if note and status == note.get("status") else None
        if result is None and status == "completed" and sub.last_text:
            result = sub.last_text
        desc = _str(meta.get("description"), 300) or call.get("description")
        if not desc and note.get("summary"):
            m = re.match(r'^Agent "(.*)" \w+$', note["summary"])
            desc = m.group(1) if m else note["summary"]
        bg = call.get("background")
        if bg is None and isinstance(meta.get("requestShape"), str):
            bg = meta["requestShape"] == "background"
        tool_uses = sub.tool_uses
        if usage and isinstance(usage.get("tool_uses"), int):
            tool_uses = max(tool_uses, usage["tool_uses"])
        sd = meta.get("spawnDepth")
        return {
            "agent_id": aid,
            "tool_use_id": tid,
            "description": desc or None,
            "agent_type": _str(meta.get("agentType"), 100) or call.get("subagent_type") or None,
            "model": _str(meta.get("model"), 100) or call.get("model") or None,
            "resolved_model": _str(meta.get("pickedEffortModel"), 100) or linfo.get("resolved_model") or None,
            "background": bg,
            "prompt": sub.prompt or call.get("prompt"),
            "status": status,
            "raw_status": note.get("raw_status") if note else None,
            "started_at": started,
            "last_activity": last,
            "ended_at": ended,
            "duration_ms": dur,
            "tool_uses": tool_uses,
            "last_action": sub.last_action,
            "result": result,
            "result_truncated": bool(note.get("result_truncated")) if result is not None and note else False,
            "summary": note.get("summary") if note else None,
            "usage": usage,
            "entries": sub.total,
            "notifications": self.note_count.get(aid, 0),
            "has_transcript": sub.has_transcript,
            "spawn_depth": sd if isinstance(sd, int) and not isinstance(sd, bool) else None,
        }

    def summaries(self, exited: bool = False) -> List[dict]:
        out = [self.summary(a, exited) for a in self.subs]
        return [x for x in out if x is not None]

    def counts(self, exited: bool = False) -> dict:
        running = sum(1 for a in self.subs if self.status(a, exited) == "running")
        return {"running": running, "total": len(self.subs)}

    def detail(self, aid: str, exited: bool = False) -> Optional[dict]:
        sub = self.subs.get(aid)
        if sub is None:
            return None
        return {"agent_id": aid, "gen": sub.gen, "summary": self.summary(aid, exited), "meta": sub.meta,
                "entries": list(sub.entries), "from": sub.base, "total": sub.total,
                "truncated": sub.base > 0}
