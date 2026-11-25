#!/usr/bin/env python3
"""cc-remote-chat v2 计算节点 agent（docs/SPEC_V2_TUI.md 第 2 节）。

独立脚本：只用标准库、兼容 Python 3.8+、不 import ccr。真实模式下源码作为
`python3 -I -u -c <源码>` 的参数在计算节点（srun step）里执行；本地 fake 模式直接运行本文件。
本机模式（命令行参数 `--local`，见 SPEC §9）：app 在本地 Mac 上直接运行本文件，驱动本机的交互式 claude；
用 argv 标志而不是环境变量传递，避免任何 CCR_* 变量经 tmux server 漏进 claude 的环境。

协议：stdin/stdout 每行一个 JSON。
  请求 {"id":int,"op":str,...} -> 响应 {"id":int,"ok":true,"result":...} / {"id":int,"ok":false,"error":"..."}
  事件（无 id）{"ev":str,...}

红线：除 ~/.cc-remote-chat/keys/<指纹>.json（用户选择了 API key 时由 start 写入，0600、O_NOFOLLOW，
内容严格为 {"env":{"ANTHROPIC_AUTH_TOKEN":…[,"ANTHROPIC_BASE_URL":…]}}，见 write_key_file）外不写任何文件，
绝不写 ~/.claude*；
不设置任何环境变量；tmux 一律 -L ccr -f /dev/null（本机模式可由 --tmux-label 换成别的名字，内置模式用 ccr-app）；
claude 参数逐项白名单；按键白名单；文本经 load-buffer 的 stdin 传入，绝不经 shell 拼接。
明文 API key 只经 stdin 的 JSON 请求到达，不进 argv/env/日志/错误信息（argv 里只有 key 文件路径）。
fake 分支只在环境变量 CCR_FAKE=1 时启用（真实模式经 ssh 启动时不会有该变量）。
"""
import glob
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import unicodedata

IDLE_EXIT = 90.0
MAX_LINE = 8 * 1024 * 1024
MAX_PASTE = 200000
MAX_KEYS = 40
MAX_STR = 20000
MAX_BATCH = 300
MAX_TRANSCRIPT_READ = 16 * 1024 * 1024
KEEPER = "_ccr_keeper"
PREFIX = "ccr-"

SID_RE = re.compile(r"s[0-9a-f]{12}\Z")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
MODEL_RE = re.compile(r"[A-Za-z0-9._:\[\]][A-Za-z0-9._:\-\[\]]{0,79}\Z")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# flag -> 取值校验（None = 不带值）
ARG_SPEC = {
    "--resume": lambda v: bool(UUID_RE.match(v)),
    "--model": lambda v: bool(MODEL_RE.match(v)),
    "--permission-mode": lambda v: v in PERMISSION_MODES,
    "--dangerously-skip-permissions": None,
    "--effort": lambda v: v in EFFORTS,
    # 只允许 agent 自己生成的 key 文件路径（validate_args(allow_settings=True)）；本地传入的 --settings 一律拒绝
    "--settings": lambda v: settings_path_ok(v),
}
# API key（start 的 api_key 字段）与 key 文件
API_KEY_RE = re.compile(r"[A-Za-z0-9._-]{8,256}\Z")
BASE_URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?\Z")
MAX_BASE_URL = 200
KEY_DIR_PARTS = (".cc-remote-chat", "keys")
MAX_KEY_FILE = 4096
# C-b：把正在运行的 Bash 命令转入后台（CLI 提示 "ctrl+b ctrl+b (twice)"：send-keys 不经过 tmux 前缀键，一次即可）；
# C-x C-s：立即发送排队的消息（中断当前生成）。只由用户点击/和弦触发（§0.5）。
KEYS = frozenset("Enter Escape Up Down Left Right Tab BTab BSpace Space C-c C-u C-b C-x C-s "
                 "1 2 3 4 5 6 7 8 9 s y n".split())
SESSION_FIELDS = ("pid", "sessionId", "status", "cwd", "kind", "entrypoint")
TRANSCRIPT_TYPES = ("user", "assistant", "system", "custom-title", "ai-title", "summary")   # 后三种只用来取会话标题
# 子代理（<transcript 目录>/<sessionId>/subagents/agent-<id>.jsonl + .meta.json）
SUBAGENT_TYPES = ("user", "assistant")
SUBAGENT_FILE_RE = re.compile(r"agent-([A-Za-z0-9_-]{1,64})\.(jsonl|meta\.json)\Z")
MAX_SUBAGENTS = 50                 # 每个会话最多跟踪最近活动的 50 个子代理
MAX_SUBAGENT_FILES = 5000          # 目录里最多看这么多个文件名（防御）
MAX_META_BYTES = 64 * 1024
MAX_META_KEYS = 40
MAX_META_STR = 2000
MAX_SUB_ENTRY_BYTES = 256 * 1024   # 单条子代理记录序列化后的上限（超出则更狠地截断字符串）
MAX_SUB_READ = 4 * 1024 * 1024     # 每个子代理每次轮询最多读这么多字节
SUB_SCAN_INTERVAL = 2.0            # 目录扫描间隔；已结束的子代理也只在扫描时检查
# 图片引用（可视化）：大图片块的 base64 不随条目转发，换成 ccr_ref 引用，本地需要时用 blob op 按引用重读
INLINE_IMAGE_MAX = 16384           # base64 短于它的图片继续内联
MAX_BLOB_LINE = 48 * 1024 * 1024   # blob op 重读一行的上限（按块读到换行为止，不会先读满）
BLOB_CHUNK = 256 * 1024            # blob op 每次读多少
MAX_BLOB_WORKERS = 2               # blob op 在独立线程里执行，最多同时 2 个（不占用主循环：按键/截屏/ping 不排队）
MAX_BLOB_QUEUE = 16                # 排队中的 blob 请求上限（超出直接回 busy）
MAX_BLOB_DATA = 16 * 1024 * 1024   # 单张图片 base64 上限
MAX_REF_BLOCKS = 200               # 每条记录最多检查这么多个内容块
MAX_KNOWN_SESSIONS = 32            # 每个会话记住最近这么多个 claude sessionId（blob 授权）
AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
REF_PATH_RE = re.compile(r"[0-9]{1,4}(?:-[0-9]{1,4})?\Z")
REF_HASH_RE = re.compile(r"[0-9a-f]{16}\Z")
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

FAKE = os.environ.get("CCR_FAKE") == "1"
# 本机模式：只由命令行参数 --local 开启（真实远端以 `python3 -I -u -c <源码>` 运行，sys.argv == ["-c"]）
LOCAL = "--local" in sys.argv[1:]
# 本机模式下 claude 由用户的登录 shell 以"登录 + 交互式"启动（= 在终端.app 里敲 claude）；
# 只接受 POSIX 风格的 shell（命令串里用了 "$@"），否则退回 /bin/zsh。
LOCAL_SHELLS = ("zsh", "bash", "sh", "ksh", "dash", "mksh")
LOCAL_SHELL_FALLBACK = ("/bin/zsh", "/bin/bash")
LOCAL_TMUX_CANDIDATES = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux")
DEFAULT_TMUX_LABEL = "ccr"
TMUX_LABEL_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
# macOS 14 起系统自带 tmux-256color 的 terminfo；12/13 没有，本机会话改用 screen-256color（见 op_start）
TMUX_TERMINFO = "/usr/share/terminfo/74/tmux-256color"


def parse_local_opts(argv):
    """本机模式的可选参数（只由本地 app 经 argv 传入，内置模式用）：
    --tmux=<绝对路径>：用这个 tmux（.app 里自带的那份），不在 PATH / Homebrew 里找；
    --tmux-label=<名字>：tmux -L 的套接字名（默认 ccr）。
    返回 (tmux 路径或 None, 标签)；取值不合法抛 ValueError（宁可起不来，也不能退回默认的 -L ccr）。"""
    path, label = None, DEFAULT_TMUX_LABEL
    for a in argv:
        if a.startswith("--tmux="):
            path = a[len("--tmux="):]
            if not path.startswith("/") or "\x00" in path or "/../" in path + "/":
                raise ValueError("--tmux 必须是绝对路径")
        elif a.startswith("--tmux-label="):
            label = a[len("--tmux-label="):]
            if not TMUX_LABEL_RE.match(label):
                raise ValueError("--tmux-label 不合法")
    return path, label


try:
    ARG_TMUX, TMUX_LABEL = parse_local_opts(sys.argv[1:]) if LOCAL else (None, DEFAULT_TMUX_LABEL)
except ValueError as _e:
    sys.stderr.write("node_agent：%s\n" % _e)
    sys.exit(2)
if FAKE and os.environ.get("CCR_FAKE_IDLE_EXIT"):  # 仅 fake 测试：缩短空闲退出时间
    IDLE_EXIT = max(0.5, float(os.environ["CCR_FAKE_IDLE_EXIT"]))


class OpError(Exception):
    def __init__(self, msg, code=None):
        Exception.__init__(self, msg)
        self.code = code   # 可选的机器可读错误码（随响应 "code" 字段返回）


# ------------------------------------------------------------------ 输出
_out_lock = threading.Lock()


def emit(obj):
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "replace")
    with _out_lock:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass


# ------------------------------------------------------------------ tmux
_tmux = {"path": None, "version": None}


def home_dir():
    return os.path.expanduser("~")


def resolve_tmux():
    if _tmux["path"]:
        return _tmux["path"]
    path = None
    if LOCAL and ARG_TMUX:   # 本地 app 指定的 tmux（内置模式）：只用它，不再到别处找
        if not _executable(ARG_TMUX):
            raise OpError("找不到指定的 tmux：%s" % ARG_TMUX)
        path = ARG_TMUX
    elif FAKE:
        path = os.environ.get("CCR_FAKE_TMUX") or shutil.which("tmux")
    elif LOCAL:
        path = local_tmux()
    else:
        cand = os.path.join(home_dir(), ".local", "x86_64", "bin", "tmux")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            path = cand
        else:
            try:
                r = subprocess.run(["bash", "-lc", "command -v tmux"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
                lines = [x.strip() for x in r.stdout.decode("utf-8", "replace").splitlines() if x.strip()]
                if r.returncode == 0 and lines and lines[-1].startswith("/"):
                    path = lines[-1]
            except (OSError, subprocess.TimeoutExpired):
                path = None
    if not path:
        raise OpError("本机找不到 tmux（可用 brew install tmux 安装）" if LOCAL else "计算节点上找不到 tmux")
    _tmux["path"] = path
    return path


def _executable(p):
    return isinstance(p, str) and p.startswith("/") and os.path.isfile(p) and os.access(p, os.X_OK)


def local_tmux():
    """本机 tmux：PATH 里的 tmux，其次 Homebrew（Apple 芯片 / Intel）的默认位置；找不到返回 None。"""
    p = shutil.which("tmux")
    if p and not p.startswith("/"):
        p = os.path.abspath(p)
    if _executable(p):
        return p
    for c in LOCAL_TMUX_CANDIDATES:
        if _executable(c):
            return c
    return None


def _listed_shells():
    try:
        with open("/etc/shells", "rb") as f:
            raw = f.read(64 * 1024).decode("utf-8", "replace")
    except OSError:
        return set()
    return set(x.strip() for x in raw.splitlines() if x.strip() and not x.strip().startswith("#"))


def local_shell():
    """本机启动 claude 用的 shell：$SHELL（必须是 /etc/shells 里列出的绝对路径、POSIX 风格、可执行），否则 /bin/zsh。"""
    sh = os.environ.get("SHELL") or ""
    if sh in _listed_shells() and os.path.basename(sh) in LOCAL_SHELLS and _executable(sh):
        return sh
    for c in LOCAL_SHELL_FALLBACK:
        if _executable(c):
            return c
    return LOCAL_SHELL_FALLBACK[0]


def tmux(args, input_bytes=None, timeout=15):
    """运行 tmux -L ccr -f /dev/null <args>（标签见 TMUX_LABEL），返回 (rc, stdout, stderr)（文本）。"""
    argv = [resolve_tmux(), "-L", TMUX_LABEL, "-f", "/dev/null"] + list(args)
    try:
        r = subprocess.run(argv, input=input_bytes if input_bytes is not None else b"",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "tmux 命令超时"
    except OSError as e:
        return 127, "", "无法执行 tmux：%s" % e
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def tmux_version():
    if _tmux["version"] is None:
        try:
            r = subprocess.run([resolve_tmux(), "-V"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=10)
            _tmux["version"] = r.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.TimeoutExpired):
            _tmux["version"] = ""
    return _tmux["version"]


def tmux_err(stderr, what):
    msg = (stderr or "").strip().splitlines()
    tail = msg[-1] if msg else ""
    if "can't find" in tail or "no server running" in tail or "no such" in tail.lower():
        return OpError("会话不存在或已结束")
    return OpError("%s失败：%s" % (what, tail[:300] or "未知错误"))


def list_session_names():
    rc, out, err = tmux(["list-sessions", "-F", "#{session_name}"])
    if rc != 0:
        return None  # server 未运行
    return [x for x in out.splitlines() if x]


def server_gone(stderr):
    """tmux 报错是否**确定**表示 server 不存在（其它错误如超时只是暂时失败）。"""
    e = stderr or ""
    return "no server running" in e or ("error connecting to" in e and (
        "No such file" in e or "Connection refused" in e))


def list_panes():
    """{session_name: (pane_pid, pane_dead, exit_status)}（每个会话取第一个 pane）；失败（含 server 未运行）返回 None。"""
    return list_panes_ex()[0]


def list_panes_ex():
    """(panes|None, server_gone)：失败时第二项表示是否确定是 server 不存在（区别于 tmux 暂时失败/超时）。"""
    rc, out, err = tmux(["list-panes", "-a", "-F",
                         "#{session_name}\t#{pane_pid}\t#{pane_dead}\t#{pane_dead_status}"])
    if rc != 0:
        return None, server_gone(err)
    return _parse_panes(out), False


def _parse_panes(out):
    res = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or parts[0] in res:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            pid = None
        try:
            st = int(parts[3]) if parts[3] != "" else None
        except ValueError:
            st = None
        dead = parts[2] == "1"
        res[parts[0]] = (pid, dead, st if dead else None)
    return res


def server_alive():
    return list_session_names() is not None


def check_sid(params):
    sid = params.get("sid")
    if not isinstance(sid, str) or not SID_RE.match(sid):
        raise OpError("无效的会话 id")
    return sid


def target(sid):
    return "=" + PREFIX + sid + ":"


# ------------------------------------------------------------------ 进程树 / session json
def child_map():
    """{ppid: [pid,...]}：Linux 用 /proc，否则 ps -A -o pid=,ppid=。"""
    m = {}
    if os.path.isdir("/proc/self"):
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % d, "rb") as f:
                    s = f.read().decode("utf-8", "replace")
                ppid = int(s[s.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            m.setdefault(ppid, []).append(int(d))
        return m
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return m
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            m.setdefault(int(parts[1]), []).append(int(parts[0]))
    return m


def descendants(pid, cmap=None):
    cmap = child_map() if cmap is None else cmap
    out, todo, seen = [], [pid], set()
    while todo and len(out) < 2000:
        p = todo.pop(0)
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        todo.extend(cmap.get(p, ()))
    return out


def sessions_dir():
    return os.path.join(home_dir(), ".claude", "sessions")


def read_session_json(pid):
    try:
        with open(os.path.join(sessions_dir(), "%d.json" % pid), "rb") as f:
            raw = f.read(1024 * 1024)
        d = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return {k: d.get(k) for k in SESSION_FIELDS}


def find_claude(pane_pid, cmap=None):
    """在 pane 进程树（含 pane_pid 自身）里找有 ~/.claude/sessions/<pid>.json 的进程。"""
    if not pane_pid:
        return None, None
    for p in descendants(pane_pid, cmap):
        d = read_session_json(p)
        if d is not None:
            return p, d
    return None, None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def find_transcript(session_id, cwd=None):
    if not isinstance(session_id, str) or not UUID_RE.match(session_id):
        return None
    base = os.path.join(home_dir(), ".claude", "projects")
    if isinstance(cwd, str) and cwd:
        p = os.path.join(base, re.sub(r"[^A-Za-z0-9]", "-", cwd), session_id + ".jsonl")
        if os.path.isfile(p):
            return p
    hits = glob.glob(os.path.join(glob.escape(base), "*", session_id + ".jsonl"))
    if not hits:
        return None
    hits.sort(key=lambda h: os.path.getmtime(h) if os.path.exists(h) else 0, reverse=True)
    return hits[0]


def truncate_strings(obj, flag, limit=MAX_STR):
    if isinstance(obj, str):
        if len(obj) > limit:
            flag[0] = True
            return obj[:limit]
        return obj
    if isinstance(obj, list):
        return [truncate_strings(x, flag, limit) for x in obj]
    if isinstance(obj, dict):
        return {k: truncate_strings(v, flag, limit) for k, v in obj.items()}
    return obj


def _json_len(obj):
    try:
        return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace"))
    except (TypeError, ValueError):
        return 0


def _b64_size(data):
    pad = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
    return max(0, len(data) * 3 // 4 - pad)


def _ref_block(block, ref_base, path):
    """大图片块（base64 ≥ INLINE_IMAGE_MAX）→ ccr_ref 引用块；不需要替换返回 None。

    ref = <kind>.<标识>.<该行字节偏移>.<块路径>.<data 的 sha256 前 16 位>：
      kind=m（主 transcript，标识 = sessionId）/ a（子代理，标识 = sessionId.agentId）；
      块路径 = message.content 下标，tool_result 内的图片为 "i-j"（content[i].content[j]）。"""
    if not isinstance(block, dict) or block.get("type") != "image":
        return None
    src = block.get("source")
    if not isinstance(src, dict) or src.get("type") != "base64":
        return None
    data = src.get("data")
    if not isinstance(data, str) or len(data) < INLINE_IMAGE_MAX:
        return None
    kind, ident, offset = ref_base
    digest = hashlib.sha256(data.encode("ascii", "replace")).hexdigest()[:16]
    ref = "%s.%s.%d.%s.%s" % (kind, ident, offset, "-".join(str(i) for i in path), digest)
    mt = src.get("media_type")
    out = dict(block)
    out["source"] = {"type": "ccr_ref", "media_type": mt if isinstance(mt, str) and len(mt) <= 64 else None,
                     "ref": ref, "bytes": _b64_size(data)}
    return out


def _drop_tur_dups(tur):
    """toolUseResult 里与 message.content 重复的图片 base64（Read 图片的 file.base64、MCP 截图列表）
    以及超长的 originalFile/originalFileContents（显示用不到，diff 用 structuredPatch）直接丢掉。"""
    if isinstance(tur, dict):
        out = None
        f = tur.get("file")
        if isinstance(f, dict) and isinstance(f.get("base64"), str):
            f = dict(f)
            del f["base64"]
            f["ccr_dropped"] = "base64"
            out = dict(tur, file=f)
        for k in ("originalFile", "originalFileContents"):
            v = tur.get(k)
            if isinstance(v, str) and len(v) > MAX_STR:
                out = dict(out if out is not None else tur)
                out[k] = None
                out.setdefault("ccr_dropped", k)
        return tur if out is None else out
    if isinstance(tur, list):
        changed = False
        items = []
        for x in tur[:MAX_REF_BLOCKS]:
            src = x.get("source") if isinstance(x, dict) and x.get("type") == "image" else None
            if isinstance(src, dict) and src.get("type") == "base64":
                x = dict(x, source={"type": "ccr_dropped", "media_type": src.get("media_type")})
                changed = True
            items.append(x)
        return items + tur[MAX_REF_BLOCKS:] if changed else tur
    return tur


def refify_entry(d, ref_base):
    """把条目里的大图片换成引用（message.content 的图片块、tool_result 内的图片块），并丢掉 toolUseResult 里的重复数据。"""
    msg = d.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        content = list(msg["content"])
        changed = False
        for i, b in enumerate(content[:MAX_REF_BLOCKS]):
            nb = _ref_block(b, ref_base, (i,))
            if nb is None and isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                inner = list(b["content"])
                hit = False
                for j, x in enumerate(inner[:MAX_REF_BLOCKS]):
                    nx = _ref_block(x, ref_base, (i, j))
                    if nx is not None:
                        inner[j] = nx
                        hit = True
                if hit:
                    nb = dict(b, content=inner)
            if nb is not None:
                content[i] = nb
                changed = True
        if changed:
            d = dict(d, message=dict(msg, content=content))
    if "toolUseResult" in d:
        tur = _drop_tur_dups(d["toolUseResult"])
        if tur is not d["toolUseResult"]:
            d = dict(d, toolUseResult=tur)
    return d


def note_attachment(d):
    """主 transcript 的 attachment 条目 → 精简版；只认 CLI 在回复中途吸收的子代理/后台任务通知
    （`attachment.type == "queued_command"` 且 `commandMode` 或 `origin.kind` 为 task-notification，CLI 2.1.332 实测：
    这种通知不再另写 user 条目），其它 attachment（环境、技能清单、排队的用户消息…）一律返回 None。
    只保留 type/uuid/parentUuid/timestamp/isSidechain 与 attachment 的 type/commandMode/origin.kind/prompt。"""
    a = d.get("attachment")
    if not isinstance(a, dict) or a.get("type") != "queued_command":
        return None
    origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
    kind = origin.get("kind") if isinstance(origin.get("kind"), str) else None
    if a.get("commandMode") != "task-notification" and kind != "task-notification":
        return None
    prompt = a.get("prompt")
    if isinstance(prompt, list):   # 防御：内容块形式只取文字
        prompt = "\n".join(str(b.get("text") or "") for b in prompt if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    out = {k: d[k] for k in ("type", "uuid", "parentUuid", "timestamp", "isSidechain") if k in d}
    att = {"type": "queued_command", "commandMode": "task-notification", "prompt": prompt}
    if kind:
        att["origin"] = {"kind": kind[:64]}
    out["attachment"] = att
    return out


def filter_entry(line, types=TRANSCRIPT_TYPES, sidechain=False, max_bytes=None, ref=None):
    """解析一行 transcript；只保留 types 里的类型（主 transcript 另外跳过 isSidechain）。
    主 transcript（types 为默认值）另外转发回复中途吸收的子代理通知（attachment，见 note_attachment）。

    给了 ref=(kind, 标识, 该行字节偏移) 时先把大图片换成引用（refify_entry），再截断：
    字符串字段超过 MAX_STR 截断并加 "truncated":true；给了 max_bytes 时（子代理），
    整条序列化后仍超限则把字符串进一步截到 2000 / 200 字符（工具结果等大文本）。"""
    try:
        d = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    if (isinstance(d, dict) and d.get("type") == "attachment" and types is TRANSCRIPT_TYPES
            and not d.get("isSidechain")):
        d = note_attachment(d)
        if d is None:
            return None
    elif not isinstance(d, dict) or d.get("type") not in types or (d.get("isSidechain") and not sidechain):
        return None
    if ref is not None:
        d = refify_entry(d, ref)
    flag = [False]
    d = truncate_strings(d, flag)
    if max_bytes is not None:
        for limit in (2000, 200):
            if _json_len(d) <= max_bytes:
                break
            d = truncate_strings(d, flag, limit)
    if flag[0]:
        d["truncated"] = True
    return d


def lstat_regular(path):
    """(size, mtime_ns)；不存在、是软链接或不是普通文件时返回 None。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st.st_size, st.st_mtime_ns


def read_nofollow(path, offset, limit):
    """以 O_NOFOLLOW 只读打开普通文件，从 offset 读至多 limit 字节 → (size, data)；失败返回 None。"""
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        size = st.st_size
        chunks = []
        left = min(max(0, size - offset), max(0, limit))
        if left:
            os.lseek(fd, offset, os.SEEK_SET)
            while left > 0:
                b = os.read(fd, min(left, 1024 * 1024))
                if not b:
                    break
                chunks.append(b)
                left -= len(b)
        return size, b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def subagents_dir(transcript_path, session_id):
    """由已知 sessionId 推出 <transcript 目录>/<sessionId>/subagents；任一级不是真实目录（含软链接）返回 None。"""
    if not transcript_path or not isinstance(session_id, str) or not UUID_RE.match(session_id):
        return None
    d1 = os.path.join(os.path.dirname(transcript_path), session_id)
    d2 = os.path.join(d1, "subagents")
    for d in (d1, d2):
        try:
            st = os.lstat(d)
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
    return d2


def list_subagents(dirpath):
    """{agent_id: 最近修改时间}：只看名字匹配 agent-<id>.(jsonl|meta.json) 的普通文件（不跟随软链接）。"""
    out = {}
    try:
        it = os.scandir(dirpath)
    except OSError:
        return out
    n = 0
    with it:
        for e in it:
            n += 1
            if n > MAX_SUBAGENT_FILES:
                break
            m = SUBAGENT_FILE_RE.match(e.name)
            if not m:
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                mt = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            aid = m.group(1)
            out[aid] = max(out.get(aid, 0.0), mt)
    return out


def clean_meta(d):
    """meta.json 只转发小的标量字段（字符串截断）。"""
    if not isinstance(d, dict):
        return None
    out = {}
    for k, v in d.items():
        if len(out) >= MAX_META_KEYS:
            break
        if not isinstance(k, str) or len(k) > 64:
            continue
        if isinstance(v, str):
            out[k] = v[:MAX_META_STR]
        elif v is None or isinstance(v, (bool, int, float)):
            out[k] = v
    return out


def split_lines_at(data, start):
    """data（文件偏移 start 起）里的完整行 → ([(该行起始偏移, 行)], 剩余半行)。"""
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], data
    out = []
    pos = start
    for x in data[:cut].split(b"\n"):
        if x.strip():
            out.append((pos, x))
        pos += len(x) + 1
    return out, data[cut + 1:]


def read_lines_at(path, offset, buf, limit):
    """增量读取 → (新 offset, 剩余半行, [(行起始偏移, 完整行)], size)；读失败返回 None。"""
    r = read_nofollow(path, offset, limit)
    if r is None:
        return None
    size, chunk = r
    lines, rest = split_lines_at(buf + chunk, offset - len(buf))
    offset += len(chunk)
    if len(rest) > 64 * 1024 * 1024:  # 防御：异常超长的半行
        rest = b""
    return offset, rest, lines, size


def read_lines(path, offset, buf, limit):
    """增量读取 → (新 offset, 剩余半行, [完整行], size)；读失败返回 None。"""
    r = read_lines_at(path, offset, buf, limit)
    if r is None:
        return None
    return r[0], r[1], [x for _, x in r[2]], r[3]


# ------------------------------------------------------------------ watch
class Watched(object):
    def __init__(self, sid):
        self.sid = sid
        self.pane_pid = None    # 代际信息：首次看到的 pane_pid（随 exited 事件发出，本地据此丢弃旧代际的事件）
        self.screen = None
        self.last_change = 0.0
        self.next_due = 0.0
        self.busy = False
        self.claude_pid = None
        self.next_scan = 0.0
        self.session = None
        self.session_id = None
        self.tpath = None
        self.toffset = 0
        self.tbuf = b""
        self.tsize = 0
        self.next_tfind = 0.0
        self.subs = {}          # agent_id -> SubTail
        self.next_subscan = 0.0


class SubTail(object):
    """一个子代理的增量读取状态。"""

    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.offset = 0
        self.buf = b""
        self.size = 0
        self.jsig = None        # jsonl 的 (size, mtime_ns)
        self.msig = None        # meta.json 的 (size, mtime_ns)
        self.meta = None        # 最近一次发出的 meta
        self.sent = False       # 是否已发过首个（reset）事件
        self.mtime = 0.0


class Watcher(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}
        self.wake = threading.Event()
        self.stopping = False
        self.list_fails = 0      # list-panes 连续"非确定"失败次数（超时等）
        self.thread = threading.Thread(target=self.run, name="ccr-watch")
        self.thread.daemon = True
        self.thread.start()

    def set(self, sid, on):
        """on=True 总是换成全新的 Watched（屏幕缓存/transcript 位置从头来过，保证会发出首帧与 reset）。"""
        with self.lock:
            if on:
                self.items[sid] = Watched(sid)
            else:
                self.items.pop(sid, None)
        self.wake.set()

    def stop(self):
        self.stopping = True
        self.wake.set()
        self.thread.join(5)

    def run(self):
        while not self.stopping:
            with self.lock:
                items = list(self.items.values())
            now = time.time()
            due = [w for w in items if w.next_due <= now]
            if due:
                try:
                    self.tick(due)
                except Exception as e:  # 监视线程不能死
                    sys.stderr.write("watch error: %r\n" % (e,))
            with self.lock:
                items = list(self.items.values())
            if items:
                delay = max(0.02, min(w.next_due for w in items) - time.time())
            else:
                delay = 5.0
            self.wake.wait(min(delay, 5.0))
            self.wake.clear()

    def tick(self, due):
        panes, gone = list_panes_ex()
        if panes is None and not gone:
            # tmux 暂时失败（超时等）：不据此判定退出；连续两次才算（本地还会再用 list 核实）
            self.list_fails += 1
            if self.list_fails < 2:
                for w in due:
                    w.next_due = time.time() + 1.0
                return
            self.list_fails = 0
        else:
            self.list_fails = 0
        cmap = None
        for w in due:
            with self.lock:
                if self.items.get(w.sid) is not w:
                    continue
            info = panes.get(PREFIX + w.sid) if panes is not None else None
            if info is not None and w.pane_pid is None:
                w.pane_pid = info[0]
            if info is None or info[1]:
                self.poll_session(w, info[0] if info else None, cmap, final=True)
                with self.lock:
                    current = self.items.get(w.sid) is w
                    if current:
                        del self.items[w.sid]
                if current:  # 期间被 kill/start/watch 换掉的旧 Watched 不再发 exited
                    emit({"ev": "exited", "sid": w.sid, "exit_status": info[2] if info else None,
                          "pane_pid": info[0] if info else w.pane_pid})
                continue
            if w.claude_pid is None and time.time() >= w.next_scan and cmap is None:
                cmap = child_map()
            self.poll_session(w, info[0], cmap)
            self.poll_screen(w)
            now = time.time()
            fast = w.busy or (w.session or {}).get("status") == "busy" or now - w.last_change < 3.0
            w.next_due = now + (0.25 if fast else 1.0)

    def poll_screen(self, w):
        rc, out, err = tmux(["capture-pane", "-p", "-J", "-t", target(w.sid)])
        if rc != 0:
            return
        w.busy = "esc to interrupt" in out
        if out != w.screen:
            w.screen = out
            w.last_change = time.time()
            emit({"ev": "screen", "sid": w.sid, "text": out, "ts": time.time()})

    def poll_session(self, w, pane_pid, cmap, final=False):
        now = time.time()
        d = None
        if w.claude_pid is not None:
            d = read_session_json(w.claude_pid)
            if d is None and not pid_alive(w.claude_pid):
                w.claude_pid = None
        if d is None and w.claude_pid is None and pane_pid and not final and now >= w.next_scan:
            w.next_scan = now + 1.0
            pid, d = find_claude(pane_pid, cmap if cmap is not None else child_map())
            w.claude_pid = pid
        if d is not None and d != w.session:
            w.session = d
            emit({"ev": "session", "sid": w.sid, "data": d})
        sess_id = (w.session or {}).get("sessionId")
        if isinstance(sess_id, str) and UUID_RE.match(sess_id):
            self.poll_transcript(w, sess_id, (w.session or {}).get("cwd"))

    def poll_transcript(self, w, sess_id, cwd):
        reset = self.poll_main_transcript(w, sess_id, cwd)
        self.poll_subagents(w, sess_id, reset)

    def poll_main_transcript(self, w, sess_id, cwd):
        """返回本次是否 reset（首次 / 切换 sessionId / 文件被截断重写）。"""
        reset = False
        if sess_id != w.session_id:
            w.session_id = sess_id
            w.tpath, w.toffset, w.tbuf, w.tsize, w.next_tfind = None, 0, b"", 0, 0.0
            reset = True
            note_session(w.sid, sess_id)
        if w.tpath is None and time.time() >= w.next_tfind:
            w.next_tfind = time.time() + 1.0
            w.tpath = find_transcript(sess_id, cwd)
        entries = []
        if w.tpath is not None:
            try:
                size = os.path.getsize(w.tpath)
            except OSError:
                size = None
            if size is not None:
                if size < w.tsize:  # 文件被截断/重写：从头再来
                    w.toffset, w.tbuf, reset = 0, b"", True
                w.tsize = size
                if size > w.toffset:
                    try:
                        with open(w.tpath, "rb") as f:
                            f.seek(w.toffset)
                            chunk = f.read(min(size - w.toffset, MAX_TRANSCRIPT_READ))
                    except OSError:
                        chunk = b""
                    lines, w.tbuf = split_lines_at(w.tbuf + chunk, w.toffset - len(w.tbuf))
                    w.toffset += len(chunk)
                    for pos, line in lines:
                        e = filter_entry(line, ref=("m", sess_id, pos))
                        if e is not None:
                            entries.append(e)
                    if len(w.tbuf) > 64 * 1024 * 1024:  # 防御：异常超长的半行
                        w.tbuf = b""
        if not entries and not reset:
            return False
        first = True
        i = 0
        while first or i < len(entries):
            batch = entries[i:i + MAX_BATCH]
            i += MAX_BATCH
            emit({"ev": "transcript", "sid": w.sid, "session_id": sess_id, "entries": batch,
                  "reset": bool(reset and first)})
            first = False
        return reset

    # -------------------------------------------------------------- 子代理
    def poll_subagents(self, w, sess_id, reset):
        """监视 <transcript 目录>/<sessionId>/subagents/（只读、不跟随软链接）。

        主 transcript reset（首次 / 换 sessionId / 重写）时子代理一并从头重发：每个子代理的首个事件 reset=true。
        未结束的子代理每次 tick 检查；已结束的只在目录扫描（SUB_SCAN_INTERVAL）时检查。"""
        if reset:
            w.subs = {}
            w.next_subscan = 0.0
        if w.tpath is None:
            return
        now = time.time()
        scan = now >= w.next_subscan
        d = None
        if scan:
            w.next_subscan = now + SUB_SCAN_INTERVAL
            d = subagents_dir(w.tpath, sess_id)
            found = list_subagents(d) if d else {}
            keep = sorted(found, key=lambda a: (found[a], a), reverse=True)[:MAX_SUBAGENTS]
            subs = {}
            for aid in keep:
                t = w.subs.get(aid) or SubTail(aid)
                t.mtime = found[aid]
                subs[aid] = t
            w.subs = subs
        if not w.subs:
            return
        if d is None:
            d = subagents_dir(w.tpath, sess_id)
            if d is None:
                return
        order = sorted(w.subs.values(), key=lambda t: (t.mtime, t.agent_id))
        for t in order:
            if not scan and t.sent and t.offset >= t.size and (t.meta or {}).get("finished") is True:
                continue
            self.poll_subagent(w, sess_id, d, t)

    def poll_subagent(self, w, sess_id, d, t):
        meta_changed = False
        mpath = os.path.join(d, "agent-%s.meta.json" % t.agent_id)
        msig = lstat_regular(mpath)
        if msig is not None and msig != t.msig:
            r = read_nofollow(mpath, 0, MAX_META_BYTES + 1)
            meta = None
            if r is not None and len(r[1]) <= MAX_META_BYTES:
                try:
                    meta = clean_meta(json.loads(r[1].decode("utf-8", "replace")))
                except ValueError:
                    meta = None   # 可能正在写：下次再读
            if meta is not None:
                t.msig = msig
                if meta != t.meta:
                    t.meta = meta
                    meta_changed = True
        reset = not t.sent
        entries = []
        jpath = os.path.join(d, "agent-%s.jsonl" % t.agent_id)
        jsig = lstat_regular(jpath)
        if jsig is not None and jsig != t.jsig:
            if jsig[0] < t.size:  # 被截断/重写：从头再来
                t.offset, t.buf, t.size = 0, b"", 0
                reset = True
            res = read_lines_at(jpath, t.offset, t.buf, MAX_SUB_READ)
            if res is not None:
                t.offset, t.buf, lines, t.size = res
                if t.offset >= t.size:
                    t.jsig = jsig      # 读完了才记签名（没读完下次继续）
                for pos, line in lines:
                    e = filter_entry(line, SUBAGENT_TYPES, sidechain=True, max_bytes=MAX_SUB_ENTRY_BYTES,
                                     ref=("a", "%s.%s" % (sess_id, t.agent_id), pos))
                    if e is not None and not e.get("isMeta"):
                        entries.append(e)
        if not entries and not meta_changed and not reset:
            return
        first = True
        i = 0
        while first or i < len(entries):
            batch = entries[i:i + MAX_BATCH]
            i += MAX_BATCH
            emit({"ev": "subagent", "sid": w.sid, "session_id": sess_id, "agent_id": t.agent_id,
                  "meta": t.meta if first and (meta_changed or reset) else None, "entries": batch,
                  "reset": bool(reset and first)})
            first = False
        t.sent = True


# ------------------------------------------------------------------ API key 文件（唯一允许写的文件）
def key_dir_path():
    return os.path.join(os.path.normpath(home_dir()), *KEY_DIR_PARTS)


def settings_path_ok(v):
    """--settings 的值必须正好是 <HOME>/.cc-remote-chat/keys/<16 位 hex>.json。"""
    return isinstance(v, str) and re.match(re.escape(key_dir_path()) + r"/[0-9a-f]{16}\.json\Z", v) is not None


def base_url_ok(v):
    return isinstance(v, str) and len(v) <= MAX_BASE_URL and BASE_URL_RE.match(v) is not None


def key_fingerprint(key, base_url=None):
    """文件名 = sha256(key + "\\n" + base_url)[:16]（base_url 为空按 ""）：同一 key+地址复用同一文件，不泄漏名称。"""
    return hashlib.sha256((key + "\n" + (base_url or "")).encode("ascii")).hexdigest()[:16]


def key_env(key, base_url=None):
    env = {"ANTHROPIC_AUTH_TOKEN": key}
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    return {"env": env}


def key_file_content(key, base_url=None):
    return json.dumps(key_env(key, base_url), separators=(",", ":")).encode("ascii")


def key_settings_ok(obj, key=None, base_url=False):
    """内容必须**严格**是 {"env":{"ANTHROPIC_AUTH_TOKEN":<key>[,"ANTHROPIC_BASE_URL":<url>]}}：
    不能有任何其它设置（hooks、permissions、其它环境变量…）。给了 key 时还要求与期望内容完全一致
    （base_url=None 表示不能有 ANTHROPIC_BASE_URL；base_url=False 表示不检查期望值）。"""
    if not isinstance(obj, dict) or list(obj) != ["env"]:
        return False
    env = obj["env"]
    if not isinstance(env, dict) or not env or not set(env) <= {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}:
        return False
    v = env.get("ANTHROPIC_AUTH_TOKEN")
    if not isinstance(v, str) or not API_KEY_RE.match(v):
        return False
    if "ANTHROPIC_BASE_URL" in env and not base_url_ok(env["ANTHROPIC_BASE_URL"]):
        return False
    if key is not None and v != key:
        return False
    return base_url is False or env.get("ANTHROPIC_BASE_URL") == (base_url or None)


def _open_private_dir(parent_fd, name, where, strict):
    """在 parent_fd 下打开（不存在则以 0700 创建）目录 name：O_NOFOLLOW 拒绝软链接，校验属主与权限。
    strict：权限必须正好是 0700（keys 目录）；否则只要求其他人不可写（~/.cc-remote-chat）。"""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        raise OpError("%s 不是目录（拒绝软链接）" % where)
    try:
        st = os.fstat(fd)
        if created and stat.S_IMODE(st.st_mode) != 0o700:   # umask 可能去掉了属主位
            os.fchmod(fd, 0o700)
            st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise OpError("%s 不是目录" % where)
        if st.st_uid != os.getuid():
            raise OpError("%s 的属主不是当前用户，拒绝写入 API key" % where)
        mode = stat.S_IMODE(st.st_mode)
        if strict and mode != 0o700:
            raise OpError("%s 的权限是 %03o（应为 700），拒绝写入 API key" % (where, mode))
        if not strict and mode & 0o022:
            raise OpError("%s 可被其他用户写入（权限 %03o），拒绝写入 API key" % (where, mode))
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_key_dir():
    """返回 ~/.cc-remote-chat/keys 的目录 fd（逐级 O_NOFOLLOW 打开；HOME 本身允许是软链接）。"""
    hfd = os.open(os.path.normpath(home_dir()), os.O_RDONLY | O_DIRECTORY)
    try:
        bfd = _open_private_dir(hfd, KEY_DIR_PARTS[0], "~/.cc-remote-chat", strict=False)
    finally:
        os.close(hfd)
    try:
        return _open_private_dir(bfd, KEY_DIR_PARTS[1], "~/.cc-remote-chat/keys", strict=True)
    finally:
        os.close(bfd)


def read_key_file(dfd, name):
    """O_NOFOLLOW 读取 key 文件并解析；不存在 → None。不是属主自己的 0600 普通单链接文件 → OpError。"""
    try:
        fd = os.open(name, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError:
        raise OpError("API key 文件不是普通文件（拒绝软链接）")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            raise OpError("API key 文件不是当前用户的普通文件，拒绝使用")
        if stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > MAX_KEY_FILE:
            return {"bad": True}
        raw = os.read(fd, MAX_KEY_FILE + 1)
    finally:
        os.close(fd)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"bad": True}


def write_key_file(key, base_url=None):
    """把 key（及可选 base_url）写进 ~/.cc-remote-chat/keys/<指纹>.json（0600），返回绝对路径。
    同一 key+地址复用同一文件：内容已完全一致就不重写。"""
    if not isinstance(key, str) or not API_KEY_RE.match(key):
        raise OpError("API key 格式不正确")
    if base_url is not None and not base_url_ok(base_url):
        raise OpError("base_url 格式不正确")
    name = key_fingerprint(key, base_url) + ".json"
    data = key_file_content(key, base_url)
    dfd = open_key_dir()
    try:
        if not key_settings_ok(read_key_file(dfd, name), key, base_url):
            fd = os.open(name, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                    raise OpError("API key 文件不是当前用户的普通文件，拒绝写入")
                if stat.S_IMODE(st.st_mode) != 0o600:
                    os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            if not key_settings_ok(read_key_file(dfd, name), key, base_url):   # 写后复核：严格只有这两个 env
                raise OpError("API key 文件写入后校验失败")
    finally:
        os.close(dfd)
    path = os.path.join(key_dir_path(), name)
    if not settings_path_ok(path):
        raise OpError("API key 文件路径不合法")
    return path


# ------------------------------------------------------------------ ops
def validate_args(args, allow_settings=False):
    """claude 参数逐项白名单。allow_settings=False（本地传入的 args）时 --settings 一律拒绝：
    它只能由 op_start 根据 api_key 写好 key 文件后自己追加，再以 allow_settings=True 复核。"""
    if not isinstance(args, list) or len(args) > 12:
        raise OpError("claude 参数格式不正确")
    out, seen, i = [], set(), 0
    while i < len(args):
        a = args[i]
        if not isinstance(a, str) or a not in ARG_SPEC or (a == "--settings" and not allow_settings):
            raise OpError("不允许的 claude 参数：%s" % (str(a)[:60],))
        if a in seen:
            raise OpError("claude 参数重复：%s" % a)
        seen.add(a)
        check = ARG_SPEC[a]
        if check is None:
            out.append(a)
            i += 1
            continue
        if i + 1 >= len(args):
            raise OpError("claude 参数 %s 缺少取值" % a)
        v = args[i + 1]
        if not isinstance(v, str) or not check(v):
            raise OpError("claude 参数 %s 的取值不合法" % a)
        out.extend([a, v])
        i += 2
    return out


def resolve_cwd(cwd):
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096:
        raise OpError("工作目录不合法")
    if "\x00" in cwd or "\n" in cwd or "\r" in cwd:
        raise OpError("工作目录不能包含换行或 NUL")
    if cwd == "~" or cwd.startswith("~/"):
        cwd = home_dir() + cwd[1:]
    if not cwd.startswith("/"):
        raise OpError("工作目录必须是绝对路径")
    cwd = os.path.normpath(cwd)
    if not os.path.isdir(cwd):
        raise OpError("工作目录不存在：%s" % cwd)
    return cwd


def claude_command():
    """启动 claude 的命令前缀（后面只接 §0.2 白名单参数，作为 "$@" 原样传给 claude，不经 shell 拼接）。
    远端：bash -l -i -c；本机：<登录 shell> -l -i -c。都是"登录 + 交互式"，= 用户在终端里登录后敲 claude：
    ~/.profile → ~/.bashrc 完整执行（实测远端 ~/.bashrc 对非交互 shell 会提前 return，conda 初始化等都在其后；
    以前用 bash -lc 时 claude 的 Bash 工具拿到的是 /usr/bin/python 而不是用户平时的 conda python）。"""
    if FAKE:
        cmd = os.environ.get("CCR_CLAUDE_CMD") or ""
        argv = shlex.split(cmd)
        if not argv:
            raise OpError("fake 模式缺少 CCR_CLAUDE_CMD")
        return argv
    if LOCAL:
        return [local_shell(), "-l", "-i", "-c", "exec claude \"$@\"", "claude"]
    return ["bash", "-l", "-i", "-c", "exec claude \"$@\"", "claude"]


def op_hello(p):
    names = list_session_names()
    # slurm_node：本 step 所在的 Slurm 节点名（只读 SLURMD_NODENAME；多节点作业据此用 srun -w 钉住节点）
    # local：本机模式（--local），本地据此确认 agent 跑在哪里
    return {"node": socket.gethostname(),
            "slurm_node": None if LOCAL else (os.environ.get("SLURMD_NODENAME") or None),
            "tmux_path": resolve_tmux(), "tmux_version": tmux_version(),
            "server_alive": names is not None, "home": home_dir(), "sessions": names or [], "local": LOCAL}


def op_list(p):
    panes, gone = list_panes_ex()
    if panes is None:
        if gone:
            return []
        # 暂时失败（超时等）不能当作"没有会话"，否则本地会把仍在运行的会话判为已退出
        raise OpError("读取 tmux 会话列表失败（tmux 暂时无响应），请稍后重试", code="tmux_busy")
    cmap = None
    res = []
    for name in sorted(panes):
        if not name.startswith(PREFIX) or not SID_RE.match(name[len(PREFIX):]):
            continue
        pid, dead, st = panes[name]
        cpid, sess = None, None
        if pid and not dead:
            if cmap is None:
                cmap = child_map()
            cpid, sess = find_claude(pid, cmap)
        if sess is not None:
            sess = {k: sess.get(k) for k in ("sessionId", "status", "cwd", "kind", "entrypoint")}
        res.append({"sid": name[len(PREFIX):], "name": name, "pane_pid": pid, "pane_dead": dead,
                    "exit_status": st, "claude_pid": cpid, "session": sess})
    return res


def tmux_literal(s):
    """作为 tmux 命令参数原样传递：-c 会做 format 展开（# 要写成 ##）；以 ; 结尾的参数会被当成命令分隔符
    （结尾写成 \\; 即得到字面的 ;）。"""
    s = "##".join(s.split("#"))
    if s.endswith(";"):
        s = s[:-1] + "\\;"
    return s


def default_terminal_args(local=None, terminfo=TMUX_TERMINFO):
    """本机会话：系统没有 tmux-256color 的 terminfo（macOS 12/13）时，新会话改用 screen-256color，
    否则 claude 里的 shell / less 等不认识 TERM。macOS 14+（文件存在）与远端都返回 []，命令与以前相同。
    不打包 terminfo、不设 TERMINFO_DIRS。"""
    if LOCAL if local is None else local:
        if not os.path.exists(terminfo):
            return ["set-option", "-g", "default-terminal", "screen-256color", ";"]
    return []


def op_start(p):
    sid = check_sid(p)
    cwd = resolve_cwd(p.get("cwd"))
    args = validate_args(p.get("args", []))   # 本地传入的 --settings 一律拒绝
    api_key = p.get("api_key")   # 可选：明文只在这个 stdin 请求里，绝不进 argv/env/日志/错误信息
    if api_key is not None and (not isinstance(api_key, str) or not API_KEY_RE.match(api_key)):
        raise OpError("API key 格式不正确（只能包含字母、数字和 . _ -，长度 8–256）")
    base_url = p.get("base_url")   # 可选：为空 = 沿用 settings.json 的 ANTHROPIC_BASE_URL
    if base_url is not None and (api_key is None or not base_url_ok(base_url)):
        raise OpError("base_url 格式不正确（或缺少 api_key）")
    names = list_session_names()
    if names is None:
        raise OpError("tmux 常驻进程未启动")
    name = PREFIX + sid
    if name in names:
        raise OpError("会话已存在：%s" % name)
    if api_key is not None:
        try:
            path = write_key_file(api_key, base_url)
        except OSError as e:
            raise OpError("写入 API key 文件失败：%s" % (e.strerror or type(e).__name__))
        args = validate_args(args + ["--settings", path], allow_settings=True)
    if WATCHER is not None:
        WATCHER.set(sid, False)  # 同一 sid 重启：丢掉旧代际的 Watched（旧屏幕缓存/迟到的 exited）
    argv = default_terminal_args() + ["set-option", "-g", "history-limit", "10000", ";",
            "new-session", "-d", "-P", "-F", "#{pane_pid}", "-s", name, "-x", "200", "-y", "50",
            "-c", tmux_literal(cwd)]
    argv += claude_command() + args
    argv += [";", "set-option", "-t", target(sid), "remain-on-exit", "on"]
    rc, out, err = tmux(argv)
    if rc != 0:
        raise OpError("启动 claude 失败：%s" % ((err.strip().splitlines() or ["未知错误"])[-1][:300]))
    pane_pid = None
    for line in out.splitlines():
        if line.strip().isdigit():
            pane_pid = int(line.strip())
            break
    return {"pane_pid": pane_pid}


def op_paste(p):
    sid = check_sid(p)
    text = p.get("text")
    enter = p.get("enter", False)
    if not isinstance(text, str):
        raise OpError("text 必须是字符串")
    if not isinstance(enter, bool):
        raise OpError("enter 必须是布尔值")
    if len(text) > MAX_PASTE:
        raise OpError("文本过长（最多 %d 字符）" % MAX_PASTE)
    if "\x00" in text:
        raise OpError("文本不能包含 NUL")
    if text == "" and not enter:
        return {}
    if text:
        buf = "ccrp-" + sid
        rc, out, err = tmux(["load-buffer", "-b", buf, "-"], input_bytes=text.encode("utf-8", "surrogatepass"))
        if rc != 0:
            raise tmux_err(err, "写入粘贴缓冲区")
        rc, out, err = tmux(["paste-buffer", "-p", "-d", "-b", buf, "-t", target(sid)])
        if rc != 0:
            tmux(["delete-buffer", "-b", buf])
            raise tmux_err(err, "粘贴")
    if enter:
        if text:
            time.sleep(0.08)
        rc, out, err = tmux(["send-keys", "-t", target(sid), "Enter"])
        if rc != 0:
            raise tmux_err(err, "发送回车")
    return {}


# ---- 发键前的屏幕守卫（guard）：本地已核对过屏幕，agent 在 send-keys 之前再抓一次屏、就地复核，
#      把"核对 → 按键"之间的竞态窗口从一次往返缩短到几毫秒（防止 Enter 打进刚弹出的权限框）。
G_RULE_RE = re.compile(r"^─{20,}(?: [^─\n]{1,200} ─{1,8})?\s*$")   # 与 tui_parse._RULE_RE 相同（含 /rename 后嵌着会话名的上边线）
G_TOP_RE = re.compile(r"^▔{20,}\s*$")
G_OPT_RE = re.compile(r"^\s*(❯\s*)?\d{1,2}\.\s+\S")
G_DIALOG_RE = re.compile(r"Esc to cancel|Enter to select|Enter to confirm|Do you want to")
G_AGENT_ROW_RE = re.compile(r"^\s*(?:❯\s+)?[●◯○◉◐⏺]\s+\S")   # 与 tui_parse._FOOTER_AGENT_ROW_RE 相同（2.1.332：⏺、焦点行 "❯ "）
G_POPUP_RE = re.compile(r"^\s{2,4}(❯\s)?\s*(/[\w:.\-]+)(?:\s{2,}.*)?$")
G_HINT_RE = re.compile(r"^\s{10,}\S")
POPUP_CMD_RE = re.compile(r"/[\w:.\-]{1,64}\Z")
GUARD_KEYS = ("lines", "input_box", "input_nonempty", "popup", "tail", "hint")
# guard.hint：屏幕上（输入框之上，不含对话框）此刻仍有该 CLI 提示行。**白名单**：只接受这两个固定 id，
# 不接受任意正则。整行匹配，与 ccr.tui_parse.HINT_BACKGROUND_RE / HINT_SEND_NOW_RE 一致。
GUARD_HINTS = {
    "background": re.compile(
        r"^\s*\(?\s*ctrl\+b(?:\s+ctrl\+b)?(?:\s+\(twice\))?\s+to run in background\s*\)?\s*$", re.I),
    "send_now": re.compile(r"^\s*(?:⎿\s+)?(?:Queued\s+·\s+)?ctrl\+x\s+ctrl\+s\s+to send now\s*$", re.I),
}
MAX_GUARD_LINES = 20
SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")


# 右侧 diff 面板（CLI 2.1.331 起，fullscreen 渲染器下在 git 仓库里改文件后自动出现在屏幕右侧，左栏 + "│" + 面板）：
# 复核前先裁掉，与 ccr.tui_parse.strip_side_panel **逐字一致**（本地按裁后的行算 tail sha1 / guard.lines，
# 两端不一致会让所有对话框操作都 409；tests 对全部 fixtures 与面板画面逐帧比对两份实现）。
PANEL_MIN_COL = 40
PANEL_MAX_TOP = 2
PANEL_MIN_ROWS = 3
PANEL_SLOP = 2
PANEL_HEAD_ROWS = 4
PANEL_FILES_RE = re.compile(r"^(\d+) files? changed\b(?:\s+\+(\d+))?(?:\s+-(\d+))?")
PANEL_HEAD_RE = re.compile(r"(?:^|\s)/diff to close(?:\s|$)|(?:^|\s)source: \S.*▾\s*$")
PANEL_CLOSE_RE = re.compile(r"^\s*✕\s*$")


def _cell_width(ch):
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _bar_cols(line):
    out = []
    col = 0
    for i, ch in enumerate(line):
        if ch == "│":
            out.append((col, i))
        col += _cell_width(ch)
    return out


def strip_side_panel(lines):
    """裁掉右侧 diff 面板（判定规则见 ccr.tui_parse._split_side_panel）；没有面板时原样返回同一个列表。"""
    top = min(PANEL_MAX_TOP + 1, len(lines))
    if not any("│" in lines[i] for i in range(top)):
        return lines
    cache = {}

    def bars(r):
        if r not in cache:
            cache[r] = _bar_cols(lines[r]) if "│" in lines[r] else []
        return cache[r]

    for s in range(top):
        for c, _ in bars(s):
            if c < PANEL_MIN_COL:
                continue
            cuts = []
            r = s
            while r < len(lines):
                bc = bars(r)
                best = min(bc, key=lambda x: (abs(x[0] - c), -x[0])) if bc else None
                if best is None or abs(best[0] - c) > PANEL_SLOP:
                    break
                cuts.append(best[1])
                r += 1
            if len(cuts) < PANEL_MIN_ROWS:
                continue
            heads = [lines[s + k][cuts[k] + 1:] for k in range(min(PANEL_HEAD_ROWS, len(cuts)))]
            if any("│" in h for h in heads):
                continue
            if not (any(PANEL_FILES_RE.match(h) for h in heads) or PANEL_CLOSE_RE.match(heads[0])
                    or any(PANEL_HEAD_RE.search(h) for h in heads)):
                continue
            out = list(lines[:s])
            out += [lines[s + k][:cut].rstrip() for k, cut in enumerate(cuts)]
            out += lines[s + len(cuts):]
            return out
    return lines


def screen_lines(text):
    """rstrip、NBSP→空格、去掉末尾空行、裁掉右侧 diff 面板（与 ccr.tui_parse.screen_lines 逐字一致）。"""
    lines = [l.rstrip() for l in re.sub("\xa0", " ", re.sub("\r", "", text or "")).split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    out = strip_side_panel(lines)
    if out is not lines:
        while out and not out[-1].strip():
            out.pop()
    return out


def find_input_box(lines):
    """(上横线, 下横线) 或 None：输入框必须在屏幕底部（下横线之后只有 ≤4 行状态栏/模式行，可另有子代理行）。"""
    rules = [i for i, l in enumerate(lines) if G_RULE_RE.match(l)]
    for k in range(len(rules) - 1, 0, -1):
        lo, hi = rules[k - 1], rules[k]
        if hi - lo >= 2 and lines[lo + 1].startswith("❯"):
            ne = [l for l in lines[hi + 1:] if l.strip()]
            for l in ne:
                if G_RULE_RE.match(l) or G_TOP_RE.match(l) or G_OPT_RE.match(l) or G_DIALOG_RE.search(l):
                    return None
            if len([l for l in ne if not G_AGENT_ROW_RE.match(l)]) > 4:
                return None
            return lo, hi
    return None


def popup_selected(lines, lo):
    """输入框上横线之上 slash 补全列表的高亮命令（"  ❯ /model   …"）；没有补全列表返回 None。"""
    k = lo - 1
    skipped = 0
    while k >= 0 and skipped < 4 and (not lines[k].strip() or G_HINT_RE.match(lines[k])):
        k -= 1
        skipped += 1
    while k >= 0 and lines[k].strip():
        m = G_POPUP_RE.match(lines[k])
        if not m:
            break
        if m.group(1):
            return m.group(2)
        k -= 1
    return None


def validate_guard(g):
    if not isinstance(g, dict) or not set(g) <= set(GUARD_KEYS):
        raise OpError("guard 格式不正确")
    lines = g.get("lines", [])
    if not isinstance(lines, list) or len(lines) > MAX_GUARD_LINES or not all(
            isinstance(x, str) and len(x) <= 4000 and "\x00" not in x for x in lines):
        raise OpError("guard.lines 不合法")
    for k in ("input_box", "input_nonempty"):
        if not isinstance(g.get(k, False), bool):
            raise OpError("guard.%s 必须是布尔值" % k)
    if "popup" in g and g["popup"] is not None and not (
            isinstance(g["popup"], str) and POPUP_CMD_RE.match(g["popup"])):
        raise OpError("guard.popup 不合法")
    if "tail" in g:
        t = g["tail"]
        if not isinstance(t, dict) or set(t) != {"n", "sha1"} or isinstance(t["n"], bool) or \
                not isinstance(t["n"], int) or not 1 <= t["n"] <= 1000 or \
                not (isinstance(t["sha1"], str) and SHA1_RE.match(t["sha1"])):
            raise OpError("guard.tail 不合法")
    if "hint" in g and (not isinstance(g["hint"], str) or g["hint"] not in GUARD_HINTS):
        raise OpError("guard.hint 不合法")
    return g


def tail_sha1(lines, n):
    return hashlib.sha1("\n".join(lines[-n:]).encode("utf-8", "replace")).hexdigest()


def check_guard(text, g):
    """None = 通过；否则返回中文原因。
    lines：这些行都还在；input_box：输入框在底部（没有对话框）；popup：补全列表没出现或高亮项就是它；
    tail：屏幕最后 n 行（对话框区域）的 sha1 没变（对话框没被换成另一个）。"""
    lines = screen_lines(text)
    have = set(lines)
    if "tail" in g:
        n = g["tail"]["n"]
        if len(lines) < n or tail_sha1(lines, n) != g["tail"]["sha1"]:
            return "对话框已变化"
    for want in g.get("lines") or ():
        if re.sub("\xa0", " ", want).rstrip() not in have:
            return "屏幕内容已变化"
    box = find_input_box(lines) if (g.get("input_box") or "popup" in g or "hint" in g) else None
    if g.get("input_box"):
        if box is None:
            return "输入框不可见（可能弹出了对话框）"
        if g.get("input_nonempty") and not "".join(lines[box[0] + 1:box[1]]).lstrip("❯").strip():
            return "输入框是空的"
    if "popup" in g and box is not None:
        sel = popup_selected(lines, box[0])
        if sel is not None and sel != g["popup"]:
            return "补全列表高亮的是 %s" % sel
    if "hint" in g:
        if box is None:
            return "输入框不可见（可能弹出了对话框）"
        pat = GUARD_HINTS[g["hint"]]
        if not any(pat.match(l) for l in lines[:box[0]]):
            return "屏幕上已经没有对应的提示"
    return None


def op_keys(p):
    sid = check_sid(p)
    keys = p.get("keys")
    if not isinstance(keys, list) or not keys:
        raise OpError("keys 必须是非空列表")
    if len(keys) > MAX_KEYS:
        raise OpError("一次最多发送 %d 个按键" % MAX_KEYS)
    for k in keys:
        if not isinstance(k, str) or k not in KEYS:
            raise OpError("不允许的按键：%s" % (str(k)[:20],))
    guard = p.get("guard")
    if guard is not None:
        validate_guard(guard)
        rc, out, err = tmux(["capture-pane", "-p", "-J", "-t", target(sid)])
        if rc != 0:
            raise tmux_err(err, "读取屏幕")
        why = check_guard(out, guard)
        if why:
            raise OpError("未发送按键：%s" % why, code="guard")
    for i, k in enumerate(keys):
        if i:
            time.sleep(0.04)
        rc, out, err = tmux(["send-keys", "-t", target(sid), k])
        if rc != 0:
            raise tmux_err(err, "发送按键")
    return {}


def op_capture(p):
    sid = check_sid(p)
    h = p.get("history", 0)
    if isinstance(h, bool) or not isinstance(h, int) or h < 0 or h > 100000:
        raise OpError("history 必须是 0–100000 的整数")
    argv = ["capture-pane", "-p", "-J", "-t", target(sid)]
    if h > 0:
        argv += ["-S", "-%d" % h]
    rc, out, err = tmux(argv)
    if rc != 0:
        raise tmux_err(err, "读取屏幕")
    return {"text": out}


def op_watch(p):
    sid = check_sid(p)
    on = p.get("on", True)
    if not isinstance(on, bool):
        raise OpError("on 必须是布尔值")
    WATCHER.set(sid, on)
    return {}


def op_kill(p):
    sid = check_sid(p)
    if WATCHER is not None:
        WATCHER.set(sid, False)  # 先停监视：kill 自己造成的退出不再发 exited
    rc, out, err = tmux(["kill-session", "-t", "=" + PREFIX + sid])
    if rc != 0 and not ("can't find" in err or "no server" in err or "error connecting" in err):
        raise tmux_err(err, "结束会话")
    return {}


def op_stop_keeper(p):
    panes, gone = list_panes_ex()
    if panes is None:
        if gone:
            return {"stopped": True, "remaining": 0}
        raise OpError("读取 tmux 会话列表失败（tmux 暂时无响应），未停止常驻进程", code="tmux_busy")
    ours = [n for n in panes if n.startswith(PREFIX)]
    remaining = len([n for n in ours if not panes[n][1]])
    if remaining:
        return {"stopped": False, "remaining": remaining}
    # 只剩已退出（remain-on-exit 保留的死 pane）会话：一并清掉，否则 server 不会退出
    for n in ours:
        tmux(["kill-session", "-t", "=" + n])
    rc, out, err = tmux(["kill-session", "-t", "=" + KEEPER])
    if rc != 0 and "can't find" not in err and "no server" not in err and "error connecting" not in err:
        raise tmux_err(err, "停止常驻进程")
    return {"stopped": True, "remaining": 0}


# ------------------------------------------------------------------ blob：按引用重读大图片（只读）
_known_lock = threading.Lock()
_known = {}   # sid -> [该会话监视期间见过的 claude sessionId…]（blob 授权：只读这些 sessionId 的 transcript）


def note_session(sid, session_id):
    with _known_lock:
        lst = [x for x in _known.get(sid, ()) if x != session_id] + [session_id]
        _known[sid] = lst[-MAX_KNOWN_SESSIONS:]


def known_sessions(sid):
    with _known_lock:
        return list(_known.get(sid, ()))


def parse_ref(ref):
    """ccr_ref 引用 → (kind, sessionId, agentId|None, offset, [块路径], hash16)；不合法抛 OpError。"""
    if not isinstance(ref, str) or len(ref) > 200:
        raise OpError("图片引用不合法")
    parts = ref.split(".")
    if parts[0] == "m" and len(parts) == 5:
        kind, sess, agent, off, path, digest = parts[0], parts[1], None, parts[2], parts[3], parts[4]
    elif parts[0] == "a" and len(parts) == 6:
        kind, sess, agent, off, path, digest = parts
        if not AGENT_ID_RE.match(agent):
            raise OpError("图片引用不合法")
    else:
        raise OpError("图片引用不合法")
    if not UUID_RE.match(sess) or not off.isdigit() or len(off) > 15 or not REF_PATH_RE.match(path) \
            or not REF_HASH_RE.match(digest):
        raise OpError("图片引用不合法")
    return kind, sess, agent, int(off), [int(x) for x in path.split("-")], digest


def _watched_tpath(sid, sess):
    w = WATCHER.items.get(sid) if WATCHER is not None else None
    if w is not None and w.session_id == sess and w.tpath:
        return w.tpath
    return None


def read_line_at(path, offset, limit, chunk=BLOB_CHUNK):
    """O_NOFOLLOW 只读打开普通文件，从 offset 起按块读到第一个换行为止 → (size, 该行不含换行)；
    超过 limit 仍没有换行、或到文件尾都没有换行 → (size, None)；打开失败 / 不是普通文件返回 None。
    远端 transcript 在 NFS 上：只读需要的那一行，不先读满上限。"""
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        size = st.st_size
        if offset >= size:
            return size, None
        os.lseek(fd, offset, os.SEEK_SET)
        parts = []
        total = 0
        while total <= limit:
            b = os.read(fd, min(chunk, limit + 1 - total))
            if not b:
                return size, None
            nl = b.find(b"\n")
            if nl >= 0:
                parts.append(b[:nl])
                return size, b"".join(parts)
            parts.append(b)
            total += len(b)
        return size, None
    except OSError:
        return None
    finally:
        os.close(fd)


def op_blob(p):
    """{sid, ref, session_id?} → {media_type, data}：按引用重读该行、校验 sha256 后返回图片 base64。

    只读该会话自己的文件：ref 里的 sessionId 必须是该 sid 监视期间见过的（或本地随请求给出的当前
    claude sessionId），路径只由 find_transcript / subagents_dir 推出（~/.claude/projects/*/<uuid>.jsonl
    及其 <uuid>/subagents/agent-<id>.jsonl），O_NOFOLLOW 打开普通文件，绝不接受任意路径。"""
    sid = check_sid(p)
    kind, sess, agent, offset, path, digest = parse_ref(p.get("ref"))
    allowed = set(known_sessions(sid))
    hint = p.get("session_id")
    if isinstance(hint, str) and UUID_RE.match(hint):
        allowed.add(hint)
    if sess not in allowed:
        raise OpError("该图片不属于这个会话", code="forbidden")
    tpath = _watched_tpath(sid, sess) or find_transcript(sess)
    if tpath is None:
        raise OpError("找不到该会话的 transcript", code="not_found")
    if kind == "a":
        d = subagents_dir(tpath, sess)
        if d is None:
            raise OpError("找不到子代理目录", code="not_found")
        fpath = os.path.join(d, "agent-%s.jsonl" % agent)
    else:
        fpath = tpath
    r = read_line_at(fpath, offset, MAX_BLOB_LINE)
    if r is None or offset >= r[0]:
        raise OpError("读取 transcript 失败", code="not_found")
    if r[1] is None:
        raise OpError("该行过长或不完整", code="too_large")
    try:
        obj = json.loads(r[1].decode("utf-8", "replace"))
        blk = obj["message"]["content"][path[0]]
        if len(path) == 2:
            blk = blk["content"][path[1]]
        src = blk["source"]
        b64 = src["data"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise OpError("引用的图片已不在原位置", code="mismatch")
    if not isinstance(b64, str) or blk.get("type") != "image" or src.get("type") != "base64":
        raise OpError("引用的图片已不在原位置", code="mismatch")
    if hashlib.sha256(b64.encode("ascii", "replace")).hexdigest()[:16] != digest:
        raise OpError("图片内容校验失败", code="mismatch")
    if len(b64) > MAX_BLOB_DATA:
        raise OpError("图片过大", code="too_large")
    mt = src.get("media_type")
    return {"media_type": mt if isinstance(mt, str) and len(mt) <= 64 else None, "data": b64}


OPS = {
    "hello": op_hello, "ping": lambda p: {}, "list": op_list, "start": op_start, "paste": op_paste,
    "keys": op_keys, "capture": op_capture, "watch": op_watch, "kill": op_kill,
    "stop_keeper": op_stop_keeper, "blob": op_blob,
}


# 慢操作（blob：读 NFS 上的大行 + 回传可能数 MB 的 base64）放到独立线程，信号量限制并发；
# 主循环继续处理 ping/keys/capture…。响应仍经 emit（同一把输出锁，保证每行完整写出）。
ASYNC_OPS = frozenset(("blob",))
_async_sem = threading.BoundedSemaphore(MAX_BLOB_WORKERS)
_async_lock = threading.Lock()
_async_pending = [0]


def respond(rid, fn, req):
    try:
        res = fn(req)
        emit({"id": rid, "ok": True, "result": res})
    except OpError as e:
        resp = {"id": rid, "ok": False, "error": str(e)}
        if getattr(e, "code", None):
            resp["code"] = e.code
        emit(resp)
    except Exception as e:  # 兜底：任何异常都要回响应
        emit({"id": rid, "ok": False, "error": "agent 内部错误：%r" % (e,)})


def _run_async(rid, fn, req):
    try:
        with _async_sem:
            respond(rid, fn, req)
    finally:
        with _async_lock:
            _async_pending[0] -= 1


def handle(line):
    try:
        req = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        emit({"ev": "error", "error": "无法解析的请求"})
        return
    if not isinstance(req, dict):
        emit({"ev": "error", "error": "请求必须是 JSON 对象"})
        return
    rid = req.get("id")
    if isinstance(rid, bool) or not isinstance(rid, int):
        emit({"ev": "error", "error": "请求缺少整数 id"})
        return
    fn = OPS.get(req.get("op"))
    if fn is None:
        emit({"id": rid, "ok": False, "error": "未知操作：%s" % (str(req.get("op"))[:40],)})
        return
    if req.get("op") in ASYNC_OPS:
        with _async_lock:
            if _async_pending[0] >= MAX_BLOB_QUEUE:
                emit({"id": rid, "ok": False, "error": "图片请求过多，请稍后重试", "code": "busy"})
                return
            _async_pending[0] += 1
        t = threading.Thread(target=_run_async, args=(rid, fn, req), name="ccr-async")
        t.daemon = True
        try:
            t.start()
        except RuntimeError:
            with _async_lock:
                _async_pending[0] -= 1
            emit({"id": rid, "ok": False, "error": "agent 忙，请稍后重试", "code": "busy"})
        return
    respond(rid, fn, req)


WATCHER = None


def main():
    global WATCHER
    WATCHER = Watcher()
    fd = sys.stdin.fileno()
    buf = b""
    last = time.time()
    try:
        while True:
            left = IDLE_EXIT - (time.time() - last)
            if left <= 0:
                break
            r, _, _ = select.select([fd], [], [], min(left, 5.0))
            if not r:
                continue
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line, buf = buf[:nl], buf[nl + 1:]
                if line.strip():
                    last = time.time()
                    handle(line)
            if len(buf) > MAX_LINE:
                emit({"ev": "error", "error": "请求过长"})
                buf = b""
    except KeyboardInterrupt:
        pass
    finally:
        WATCHER.stop()


if __name__ == "__main__":
    main()
