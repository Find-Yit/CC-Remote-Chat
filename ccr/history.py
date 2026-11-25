"""远端只读查询：历史会话列表/读取、squeue、列目录（SPEC 2.5）。

设计：
- 远端解析代码写在 REMOTE_SCRIPT 常量里（兼容 python3.8+，只用标准库，只读）。
  远端执行方式：ccr.remote.run_remote_python(cfg, REMOTE_SCRIPT, args)，即
  `ssh host "python3 -I - <args>"`，脚本走 stdin，远端不落任何文件。
- 本模块在导入时把同一份 REMOTE_SCRIPT exec 到私有命名空间，导出其中的纯函数，
  本地单测（对 fixtures）与远端执行用的是完全相同的代码。
- fake 模式下不 ssh：在本地用 `python3 -I -` 跑同一份脚本（历史读 fixtures/，目录列本机）。
- 运行位置 target（SPEC §9）："server"（默认，经 ssh 在登录节点上跑）或 "local"（本机：同一份只读脚本用本机
  `python3 -I -` 运行，读本机真实的 ~/.claude/projects；fake 模式下读 fake "本机" HOME 的 ~/.claude/projects）。
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "fixtures"

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

# ---------------------------------------------------------------------------
# 远端脚本（同时在本地 exec 供单测/fake 使用）。必须兼容 python3.8，且只读。
# ---------------------------------------------------------------------------
REMOTE_SCRIPT = r'''
import json, os, re, sys

UUID_PAT = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile("^" + UUID_PAT + "\\Z")
NESTED_FILE_RE = re.compile("^(" + UUID_PAT + ")\\.jsonl\\Z")
FLAT_FILE_RE = re.compile("^(?:transcript_)?(" + UUID_PAT + ")\\.jsonl\\Z")
MAX_FIELD_BYTES = 20 * 1024
TITLE_MAX = 80
MAX_DIRS = 5000
MAX_LINE_FOR_META = 256 * 1024

# 整块去掉（连同内容）的标签
_DROP_TAGS = ("system-reminder", "command-message", "local-command-stdout", "local-command-stderr",
              "local-command-caveat", "bash-stdout", "bash-stderr", "user-prompt-submit-hook",
              "task-notification", "persisted-output", "ide_opened_file", "ide_selection")
_DROP_RE = re.compile(r"<(" + "|".join(re.escape(t) for t in _DROP_TAGS) + r")(?:\s[^>]*)?>.*?</\1\s*>",
                      re.S)
# 去掉标签本身、保留内容（如 <command-name>/foo</command-name> -> /foo）
_TAG_RE = re.compile(r"</?(?:command-[A-Za-z0-9_-]+|bash-input|local-command-[A-Za-z0-9_-]+)(?:\s[^>]*)?>")
_UNCLOSED_RE = re.compile(r"<(?:" + "|".join(re.escape(t) for t in _DROP_TAGS) + r")(?:\s[^>]*)?>.*\Z", re.S)
_WS_RE = re.compile(r"\s+")
_SKIP_PREFIXES = ("[Request interrupted", "Caveat: The messages below")


def clean_title_text(text):
    """去掉 Claude Code 注入的标签，压缩空白，截到 80 字。返回 '' 表示不可用作标题。"""
    if not isinstance(text, str):
        return ""
    t = _DROP_RE.sub(" ", text)
    t = _UNCLOSED_RE.sub(" ", t)
    t = _TAG_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    if not t:
        return ""
    for p in _SKIP_PREFIXES:
        if t.startswith(p):
            return ""
    if len(t) > TITLE_MAX:
        t = t[:TITLE_MAX - 1] + "…"
    return t


def message_text(content):
    """user message.content -> 纯文本（跳过 tool_result / image）。"""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
    return "\n".join(parts)


def _line_type(line):
    """快速取顶层 type（CC 写 jsonl 时 type 常在行首），取不到返回 None。"""
    m = re.match(br'\{"type"\s*:\s*"([A-Za-z0-9_-]{1,40})"', line)
    if m:
        return m.group(1).decode("ascii")
    return None


def _loads(line):
    try:
        d = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def summarize_lines(lines):
    """扫描 transcript 行（bytes 迭代器），返回 {"cwd","title"}。

    标题优先级：custom-title > ai-title > summary > 首条非 meta 用户文本。
    """
    cwd = None
    first_user = None
    first_cmd = None  # 仅由斜杠命令构成的首条消息（如 /model），作为最后的兜底
    custom = ai = summary = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        lt = _line_type(line)
        if lt in ("custom-title", "ai-title", "summary", "agent-name"):
            if len(line) > MAX_LINE_FOR_META:
                continue
            d = _loads(line)
            if not d:
                continue
            if lt == "custom-title" and isinstance(d.get("customTitle"), str) and d["customTitle"].strip():
                custom = d["customTitle"]
            elif lt == "ai-title" and isinstance(d.get("aiTitle"), str) and d["aiTitle"].strip():
                ai = d["aiTitle"]
            elif lt == "summary" and isinstance(d.get("summary"), str) and d["summary"].strip():
                summary = d["summary"]
            continue
        if lt is not None and lt not in ("user", "assistant"):
            continue
        need_user = first_user is None and not (custom or ai)
        need_cwd = cwd is None
        if not (need_user or need_cwd):
            continue
        if lt is None and b'"user"' not in line and b'"cwd"' not in line:
            continue
        d = _loads(line)
        if not d:
            continue
        if need_cwd and isinstance(d.get("cwd"), str) and d["cwd"]:
            cwd = d["cwd"]
        if (need_user and d.get("type") == "user" and not d.get("isMeta") and not d.get("isSidechain")
                and not d.get("isCompactSummary") and not is_system_injected(d)):
            msg = d.get("message")
            if isinstance(msg, dict):
                raw = message_text(msg.get("content"))
                t = clean_title_text(raw)
                if t:
                    if "<command-name>" in raw:
                        if first_cmd is None:
                            first_cmd = t
                    else:
                        first_user = t
    title = None
    for cand in (custom, ai, summary):
        if cand:
            title = clean_title_text(cand) or None
            if title:
                break
    if not title:
        title = first_user or first_cmd or ""
    return {"cwd": cwd, "title": title}


def _cut(s):
    """按 UTF-8 字节截断到 MAX_FIELD_BYTES。返回 (新字符串, 是否截断)。"""
    if not isinstance(s, str):
        return s, False
    if len(s) * 4 <= MAX_FIELD_BYTES:
        return s, False
    b = s.encode("utf-8", "replace")
    if len(b) <= MAX_FIELD_BYTES:
        return s, False
    return b[:MAX_FIELD_BYTES].decode("utf-8", "ignore") + "\n…[已截断]", True


def _jsize(o):
    try:
        return len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _trim_value(v, flag):
    """递归截断过长字符串（用于 tool_use.input 等）。"""
    if isinstance(v, str):
        nv, t = _cut(v)
        if t:
            flag[0] = True
        return nv
    if isinstance(v, list):
        return [_trim_value(x, flag) for x in v]
    if isinstance(v, dict):
        return dict((k, _trim_value(x, flag)) for k, x in v.items())
    return v


def _image_placeholder(b):
    src = b.get("source") if isinstance(b.get("source"), dict) else {}
    mt = src.get("media_type") or "image"
    return {"type": "text", "text": "[图片 %s 已省略]" % mt}


def _trim_block(b, flag):
    if not isinstance(b, dict):
        return b
    t = b.get("type")
    if t == "text":
        nb = dict(b)
        nb["text"], tr = _cut(b.get("text"))
        if tr:
            flag[0] = True
        return nb
    if t == "thinking":
        nb = dict((k, v) for k, v in b.items() if k != "signature")
        nb["thinking"], tr = _cut(b.get("thinking"))
        if tr:
            flag[0] = True
        return nb
    if t == "redacted_thinking":
        return {"type": "redacted_thinking"}
    if t == "image":
        if _jsize(b) > MAX_FIELD_BYTES:
            flag[0] = True
            return _image_placeholder(b)
        return b
    if t == "tool_use":
        nb = dict(b)
        if _jsize(b.get("input")) > MAX_FIELD_BYTES:
            nb["input"] = _trim_value(b.get("input"), flag)
        return nb
    if t == "tool_result":
        nb = dict(b)
        c = b.get("content")
        if _jsize(c) > MAX_FIELD_BYTES:
            flag[0] = True
            if isinstance(c, list):
                parts = []
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "text":
                        parts.append(x.get("text") or "")
                    elif isinstance(x, dict) and x.get("type") == "image":
                        parts.append(_image_placeholder(x)["text"])
                c = "\n".join(parts)
            elif not isinstance(c, str):
                c = json.dumps(c, ensure_ascii=False)
            nb["content"], _ = _cut(c)
        return nb
    if _jsize(b) > MAX_FIELD_BYTES:
        flag[0] = True
        return {"type": t if isinstance(t, str) else "unknown", "truncated": True}
    return b


def is_system_injected(d):
    """CLI 自己注入的"用户"消息（如子代理结束的 <task-notification>）：promptSource=system 或 origin.kind 非 human。"""
    if not isinstance(d, dict):
        return False
    origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
    kind = origin.get("kind")
    if d.get("promptSource") == "system":
        return True
    return isinstance(kind, str) and kind not in ("", "human") and d.get("promptSource") != "typed"


def _trim_note_attachment(d):
    """回复中途被吸收的子代理/后台任务通知（attachment / queued_command / commandMode=task-notification，
    CLI 2.1.332 实测不再另写 user 条目）→ 精简的 attachment entry；其它 attachment 返回 None。"""
    a = d.get("attachment")
    if not isinstance(a, dict) or a.get("type") != "queued_command" or d.get("isSidechain"):
        return None
    origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
    kind = origin.get("kind") if isinstance(origin.get("kind"), str) else None
    if a.get("commandMode") != "task-notification" and kind != "task-notification":
        return None
    prompt = a.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    prompt, tr = _cut(prompt)
    att = {"type": "queued_command", "commandMode": "task-notification", "prompt": prompt}
    if kind:
        att["origin"] = {"kind": kind[:64]}
    entry = {"type": "attachment", "uuid": d.get("uuid"), "timestamp": d.get("timestamp"), "attachment": att}
    if tr:
        entry["truncated"] = True
    return entry


def trim_entry(d):
    """把一条 transcript 记录变成 API entry；非 user/assistant、meta、sidechain 返回 None
    （回复中途吸收的子代理通知 attachment 除外，见 _trim_note_attachment）。"""
    if not isinstance(d, dict):
        return None
    typ = d.get("type")
    if typ == "attachment":
        return _trim_note_attachment(d)
    if typ not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
        return None
    msg = d.get("message")
    if not isinstance(msg, dict):
        return None
    flag = [False]
    out_msg = {"role": msg.get("role") or typ}
    for k in ("id", "model", "stop_reason"):
        if k in msg:
            out_msg[k] = msg[k]
    content = msg.get("content")
    if isinstance(content, str):
        content, tr = _cut(content)
        if tr:
            flag[0] = True
    elif isinstance(content, list):
        content = [_trim_block(b, flag) for b in content]
    else:
        content = ""
    out_msg["content"] = content
    entry = {"type": typ, "uuid": d.get("uuid"), "timestamp": d.get("timestamp"), "message": out_msg}
    # 前端据此把系统注入的消息（<task-notification> 等）渲染成系统行而不是用户气泡（与实时视图一致）
    origin = d.get("origin") if isinstance(d.get("origin"), dict) else None
    if origin and isinstance(origin.get("kind"), str):
        entry["origin"] = {"kind": origin["kind"][:64]}
    if isinstance(d.get("promptSource"), str):
        entry["promptSource"] = d["promptSource"][:32]
    tur = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else None
    if tur and isinstance(tur.get("agentId"), str) and len(tur["agentId"]) <= 64:
        entry["toolUseResult"] = {"agentId": tur["agentId"], "status": str(tur.get("status") or "")[:40]}
    if flag[0]:
        entry["truncated"] = True
    return entry


def read_lines(lines):
    """transcript 行 -> {"cwd","entries"}。"""
    cwd = None
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        lt = _line_type(line)
        if lt is not None and lt not in ("user", "assistant") and not (
                lt == "attachment" and b"task-notification" in line):
            continue
        d = _loads(line)
        if not d:
            continue
        if cwd is None and isinstance(d.get("cwd"), str) and d["cwd"]:
            cwd = d["cwd"]
        e = trim_entry(d)
        if e is not None:
            entries.append(e)
    return {"cwd": cwd, "entries": entries}


def iter_transcripts(root, flat=False):
    """列出 (path, uuid, project_dir)。默认布局 root/<project>/<uuid>.jsonl。"""
    out = []
    if flat:
        try:
            names = os.listdir(root)
        except OSError:
            return out
        for n in names:
            m = FLAT_FILE_RE.match(n)
            if m:
                out.append((os.path.join(root, n), m.group(1).lower(), os.path.basename(root.rstrip("/"))))
        return out
    try:
        projects = os.listdir(root)
    except OSError:
        return out
    for p in projects:
        pd = os.path.join(root, p)
        if not os.path.isdir(pd):
            continue
        try:
            names = os.listdir(pd)
        except OSError:
            continue
        for n in names:
            m = NESTED_FILE_RE.match(n)
            if m:
                out.append((os.path.join(pd, n), m.group(1).lower(), p))
    return out


def _open_lines(path):
    return open(path, "rb")


def list_history(root, limit=100, flat=False):
    items = []
    for path, uid, proj in iter_transcripts(root, flat):
        try:
            st = os.stat(path)
        except OSError:
            continue
        items.append((st.st_mtime, st.st_size, path, uid, proj))
    items.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    out = []
    for mtime, size, path, uid, proj in items:
        if uid in seen:
            continue
        seen.add(uid)
        if len(out) >= limit:
            break
        try:
            with _open_lines(path) as f:
                info = summarize_lines(f)
        except OSError:
            continue
        out.append({"claude_session_id": uid, "cwd": info["cwd"], "title": info["title"],
                    "mtime": mtime, "size": size, "project_dir": proj})
    return {"sessions": out}


def read_history(root, uid, flat=False):
    if not UUID_RE.match(uid or ""):
        return {"__error__": "bad_request", "message": "会话 id 不合法"}
    uid = uid.lower()
    best = None
    for path, u, proj in iter_transcripts(root, flat):
        if u != uid:
            continue
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, path)
    if best is None:
        return {"__error__": "not_found", "message": "未找到该历史会话"}
    with _open_lines(best[1]) as f:
        r = read_lines(f)
    return {"claude_session_id": uid, "cwd": r["cwd"], "entries": r["entries"]}


def list_dirs(path, hidden=False):
    if not isinstance(path, str) or not path or "\x00" in path or "\n" in path or len(path) > 4096:
        return {"__error__": "bad_request", "message": "路径不合法"}
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        return {"__error__": "bad_request", "message": "路径必须是绝对路径或以 ~ 开头"}
    p = os.path.normpath(p)
    if p.startswith("//"):
        p = "/" + p.lstrip("/")
    if not os.path.isdir(p):
        return {"__error__": "not_found", "message": "目录不存在"}
    dirs = []
    truncated = False
    try:
        it = os.scandir(p)
    except PermissionError:
        return {"__error__": "forbidden", "message": "无权限读取该目录"}
    except OSError as e:
        return {"__error__": "error", "message": "无法读取目录：%s" % e.strerror}
    with it:
        for e in it:
            n = e.name
            if not hidden and n.startswith("."):
                continue
            try:
                if not e.is_dir(follow_symlinks=True):
                    continue
            except OSError:
                continue
            dirs.append(n)
            if len(dirs) >= MAX_DIRS:
                truncated = True
                break
    dirs.sort(key=lambda s: (s.lower(), s))
    parent = None if p == "/" else os.path.dirname(p)
    out = {"path": p, "parent": parent, "dirs": dirs}
    if truncated:
        out["truncated"] = True
    return out


def main(argv):
    root = os.path.expanduser("~/.claude/projects")
    flat = False
    args = list(argv)
    if len(args) >= 3 and args[0] == "--root":
        root = args[1]
        args = args[2:]
    if args and args[0] == "--flat":
        flat = True
        args = args[1:]
    if not args:
        return {"__error__": "bad_request", "message": "缺少命令"}
    cmd = args[0]
    if cmd == "list" and len(args) == 2:
        try:
            limit = max(1, min(int(args[1]), 1000))
        except ValueError:
            limit = 100
        return list_history(root, limit, flat)
    if cmd == "read" and len(args) == 2:
        return read_history(root, args[1], flat)
    if cmd == "dirs" and len(args) == 3:
        return list_dirs(args[1], args[2] == "1")
    return {"__error__": "bad_request", "message": "未知命令"}


def _run():
    try:
        res = main(sys.argv[1:])
    except Exception as e:  # noqa
        res = {"__error__": "error", "message": "远端脚本异常：%s: %s" % (type(e).__name__, e)}
    data = json.dumps(res, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()


if __name__ == "__main__":
    _run()
'''

# 本地 exec 同一份脚本，导出纯函数（__name__ 不是 "__main__"，不会执行 _run）。
_NS: Dict[str, Any] = {"__name__": "ccr_history_remote_lib"}
exec(compile(REMOTE_SCRIPT, "<ccr.history.REMOTE_SCRIPT>", "exec"), _NS)

clean_title_text = _NS["clean_title_text"]
message_text = _NS["message_text"]
summarize_lines = _NS["summarize_lines"]
read_lines = _NS["read_lines"]
trim_entry = _NS["trim_entry"]
is_system_injected = _NS["is_system_injected"]
list_history = _NS["list_history"]
read_history = _NS["read_history"]
list_dirs = _NS["list_dirs"]
remote_main = _NS["main"]
MAX_FIELD_BYTES = _NS["MAX_FIELD_BYTES"]


# ---------------------------------------------------------------------------
# squeue 输出解析（本地纯函数）
# ---------------------------------------------------------------------------
SQUEUE_FORMAT = "%i|%T|%N|%j|%M|%L"


def parse_squeue(text: str) -> List[Dict[str, str]]:
    jobs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        jobid = parts[0].strip()
        if not re.match(r"^\d{1,12}(_\d{1,8})?(\[[0-9,\-%]+\])?\Z", jobid):
            continue
        jobs.append({
            "jobid": jobid,
            "state": parts[1].strip(),
            "nodes": parts[2].strip(),
            "name": "|".join(parts[3:-2]).strip(),
            "elapsed": parts[-2].strip(),
            "left": parts[-1].strip(),
        })
    return jobs


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------
class HistoryError(Exception):
    """带 HTTP 状态码与中文说明的错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_ERR_STATUS = {"bad_request": 400, "not_found": 404, "forbidden": 403, "error": 502}

RunPython = Callable[[dict, str, List[str], int], Tuple[int, str, str]]
RunCmd = Callable[[dict, List[str], int], Tuple[int, str, str]]


def _remote():
    return importlib.import_module("ccr.remote")


def _default_run_python(cfg, script, args, timeout):
    return _remote().run_remote_python(cfg, script, args, timeout=timeout)


def _default_run_cmd(cfg, argv, timeout):
    return _remote().run_remote(cfg, argv, timeout=timeout)


TARGET_SERVER = "server"
TARGET_LOCAL = "local"
TARGETS = (TARGET_SERVER, TARGET_LOCAL)


def validate_target(target) -> str:
    """历史/目录查询的运行位置：None/"" = 服务器（与以前完全一致）；"local" = 本机。"""
    if target is None or target == "":
        return TARGET_SERVER
    if target not in TARGETS:
        raise HistoryError(400, "target 必须是 server 或 local")
    return target


def _local_run_python(cfg, script, args, timeout):
    """fake 模式 / 本机：在本机用 python3 -I - 跑同一份脚本（同样只读；不改环境，HOME 即本机用户的 HOME）。"""
    # 内置模式（后端以 -B 运行）子进程也加 -B，不往 .app 里写 __pycache__；平时与以前相同
    flags = ["-I", "-B"] if sys.flags.dont_write_bytecode else ["-I"]
    p = subprocess.run([sys.executable] + flags + ["-"] + list(args), input=script.encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, shell=False)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


FAKE_JOBS = [
    {"jobid": "100001", "state": "RUNNING", "nodes": "fake-node01", "name": "fake-bash",
     "elapsed": "1:23:45", "left": "2-22:36:15"},
    {"jobid": "100002", "state": "PENDING", "nodes": "", "name": "fake-train",
     "elapsed": "0:00", "left": "1-00:00:00"},
]


def _validate_path(p: str) -> str:
    try:
        return _remote().validate_path(p)
    except ImportError:
        if not isinstance(p, str) or not p or "\x00" in p or "\n" in p or "\r" in p or len(p) > 4096:
            raise ValueError("路径不合法")
        if not (p.startswith("/") or p == "~" or p.startswith("~/")):
            raise ValueError("路径必须是绝对路径或以 ~ 开头")
        return p


class HistoryService:
    """/api/history、/api/history/<uuid>、/api/jobs、/api/dirs 背后的逻辑。

    所有方法可能耗时数秒（ssh），由 server 在各自请求线程中调用；
    max_concurrency 限制同时进行的远端调用数，避免触发 sshd MaxStartups。
    """

    def __init__(self, get_config: Callable[[], dict], *, run_python: Optional[RunPython] = None,
                 run_cmd: Optional[RunCmd] = None, fixtures_dir: Optional[pathlib.Path] = None,
                 max_concurrency: int = 4, run_local_python: Optional[RunPython] = None):
        self.get_config = get_config
        self._run_python = run_python or _default_run_python
        self._run_cmd = run_cmd or _default_run_cmd
        # 本机（target=local，非 fake）：本机 python3 -I - 跑同一份脚本（测试可注入）
        self._run_local_python = run_local_python or _local_run_python
        self.fixtures_dir = pathlib.Path(fixtures_dir) if fixtures_dir else FIXTURES_DIR
        self._sem = threading.BoundedSemaphore(max(1, max_concurrency))
        self._local_sem = threading.BoundedSemaphore(max(1, max_concurrency))   # 本机查询不占 ssh 名额

    # -- 内部 ---------------------------------------------------------------
    def _acquire(self, sem=None):
        if not (sem or self._sem).acquire(timeout=120):
            raise HistoryError(503, "远端查询排队超时，请稍后重试")

    def _script(self, cfg: dict, args: List[str], timeout: int, target: str = TARGET_SERVER) -> Dict[str, Any]:
        fake = bool(cfg.get("fake"))
        local = target == TARGET_LOCAL
        runner = _local_run_python if fake else (self._run_local_python if local else self._run_python)
        sem = self._local_sem if local else self._sem
        where = "本机" if local else "远端"
        self._acquire(sem)
        try:
            try:
                rc, out, err = runner(cfg, REMOTE_SCRIPT, args, timeout)
            except subprocess.TimeoutExpired:
                raise HistoryError(504, "%s查询超时" % where) from None
            except ImportError:
                raise HistoryError(500, "远端模块不可用（ccr.remote 未就绪）") from None
            except OSError as e:
                raise HistoryError(502, "无法执行%s命令：%s" % (where, e)) from None
            except ValueError as e:   # 配置不完整（如还没设置服务器 SSH 主机）
                raise HistoryError(400, str(e) or "配置不完整") from None
        finally:
            sem.release()
        res = None
        if out:
            for line in reversed(out.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        res = json.loads(line)
                    except ValueError:
                        res = None
                    break
        if not isinstance(res, dict):
            tail = (err or "").strip().splitlines()[-1:] or [""]
            if rc == 124:
                raise HistoryError(504, "%s查询超时" % where)
            if rc == 255 and not local:
                raise HistoryError(502, "ssh 连接失败：%s" % tail[0][:300])
            raise HistoryError(502, "%s查询失败（退出码 %s）：%s" % (where, rc, tail[0][:300]))
        if "__error__" in res:
            raise HistoryError(_ERR_STATUS.get(res.get("__error__"), 502),
                               str(res.get("message") or "%s查询失败" % where))
        return res

    def _fake_root_args(self) -> List[str]:
        return ["--root", str(self.fixtures_dir), "--flat"]

    def _root_args(self, cfg: dict, target: str) -> List[str]:
        """历史目录：真实模式不传（脚本默认 ~/.claude/projects，远端 = 服务器、本机 = 本机）；
        fake 服务器 = fixtures/（平铺）；fake 本机 = fake "本机" HOME 的 ~/.claude/projects（fake_tui 写的）。"""
        if not cfg.get("fake"):
            return []
        if target == TARGET_LOCAL:
            return ["--root", os.path.join(_remote().fake_home(cfg, local=True), ".claude", "projects")]
        return self._fake_root_args()

    # -- 公开 API ------------------------------------------------------------
    def list_history(self, limit: int = 100, target: Optional[str] = None) -> Dict[str, Any]:
        target = validate_target(target)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise HistoryError(400, "limit 必须是整数") from None
        if limit < 1 or limit > 1000:
            raise HistoryError(400, "limit 取值范围 1–1000")
        cfg = self.get_config()
        pre = self._root_args(cfg, target)
        res = self._script(cfg, pre + ["list", str(limit)], 90, target)
        sessions = res.get("sessions")
        if not isinstance(sessions, list):
            raise HistoryError(502, "远端返回格式不正确")
        return {"sessions": sessions}

    def read_history(self, uid: str, target: Optional[str] = None) -> Dict[str, Any]:
        target = validate_target(target)
        if not isinstance(uid, str) or not UUID_RE.match(uid):
            raise HistoryError(400, "会话 id 不合法（需要标准 UUID）")
        cfg = self.get_config()
        pre = self._root_args(cfg, target)
        res = self._script(cfg, pre + ["read", uid.lower()], 120, target)
        if not isinstance(res.get("entries"), list):
            raise HistoryError(502, "远端返回格式不正确")
        return {"claude_session_id": res.get("claude_session_id"), "cwd": res.get("cwd"),
                "entries": res["entries"]}

    def list_dirs(self, path: Optional[str], hidden: bool = False, target: Optional[str] = None) -> Dict[str, Any]:
        target = validate_target(target)
        cfg = self.get_config()
        if path is None or path == "":
            # 本机默认 ~（config 的 default_cwd 是服务器上的目录）
            path = "~" if target == TARGET_LOCAL else (cfg.get("default_cwd") or "~")
        try:
            path = _validate_path(path)
        except ValueError as e:
            raise HistoryError(400, str(e) or "路径不合法") from None
        res = self._script(cfg, ["dirs", path, "1" if hidden else "0"], 45, target)
        if not isinstance(res.get("dirs"), list) or not isinstance(res.get("path"), str):
            raise HistoryError(502, "远端返回格式不正确")
        out = {"path": res["path"], "parent": res.get("parent"), "dirs": res["dirs"]}
        if res.get("truncated"):
            out["truncated"] = True
        return out

    def list_jobs(self) -> Dict[str, Any]:
        cfg = self.get_config()
        if cfg.get("fake"):
            return {"jobs": [dict(j) for j in FAKE_JOBS]}
        argv = ["squeue", "--me", "-h", "-o", SQUEUE_FORMAT]
        self._acquire()
        try:
            try:
                rc, out, err = self._run_cmd(cfg, argv, 45)
            except subprocess.TimeoutExpired:
                raise HistoryError(504, "squeue 超时") from None
            except ImportError:
                raise HistoryError(500, "远端模块不可用（ccr.remote 未就绪）") from None
            except OSError as e:
                raise HistoryError(502, "无法执行远端命令：%s" % e) from None
            except ValueError as e:   # 配置不完整（如还没设置服务器 SSH 主机）
                raise HistoryError(400, str(e) or "配置不完整") from None
        finally:
            self._sem.release()
        if rc != 0:
            tail = (err or "").strip().splitlines()[-1:] or [""]
            if rc == 124:
                raise HistoryError(504, "squeue 超时")
            if rc == 255:
                raise HistoryError(502, "ssh 连接失败：%s" % tail[0][:300])
            raise HistoryError(502, "squeue 失败（退出码 %s）：%s" % (rc, tail[0][:300]))
        return {"jobs": parse_squeue(out)}
