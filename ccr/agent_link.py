"""本地 AgentLink（docs/SPEC_V2_TUI.md 第 3 节）：经 ssh + srun 在计算节点上运行 ccr/node_agent.py，
按行 JSON 收发请求/响应/事件；断线指数退避重连；ensure_keeper 启动 tmux 常驻进程。

真实模式：
  ssh -T <复用参数> <host> "bash -lc 'exec srun --jobid=J --overlap -n1 -N1 -u -J ccr-agent python3 -I -u -c <源码>'"
  （远端字符串逐层 shlex.quote；本地 shell=False；不传 env。）
fake 模式（cfg["fake"]）：本地 python3 -I -u ccr/node_agent.py，环境变量 CCR_FAKE=1、HOME=<fake_home>、
  TMUX_TMPDIR=<fake_tmpdir>、CCR_CLAUDE_CMD=<python3 ccr/fake_tui.py>；keeper = 本机 tmux。
本机模式（jobid == remote.LOCAL_JOBID，SPEC §9）：本地 python3 -I -u ccr/node_agent.py --local（argv 标志，
  不用环境变量），env = app 进程环境去掉 TMUX/TMUX_PANE/CCR_*（不加任何变量）；不查 squeue（作业永不"结束"）；
  keeper = 同一份 keeper_script 由 /bin/bash 在新会话（start_new_session）里后台运行，app 退出后仍在。
  fake 模式下的"本机"仍走 fake_tui，只是 HOME / TMUX_TMPDIR 换成单独一套（fake_home(cfg, local=True)）。

线程模型：每个 AgentLink 一个 supervisor 线程（连接/ping/重连），每个连接一个 stdout 读线程（响应分发、
事件回调）和一个 stderr 读线程。on_event / on_state 在这些后台线程里被调用，回调里不要长时间阻塞。
"""
from __future__ import annotations

import collections
import itertools
import json
import logging
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from . import remote

log = logging.getLogger("ccr.agent_link")

__all__ = ["AgentLink", "AgentError", "AgentDown", "agent_source", "build_agent_remote_command",
           "build_agent_argv", "build_keeper_remote_command", "fake_env", "local_env", "LOCAL_AGENT_FLAG"]

AGENT_PATH = pathlib.Path(__file__).resolve().with_name("node_agent.py")
FAKE_TUI_PATH = pathlib.Path(__file__).resolve().with_name("fake_tui.py")
KEEPER_SESSION = "_ccr_keeper"
KEEPER_STEP = "ccr-keeper"
AGENT_STEP = "ccr-agent"
LOCAL_AGENT_FLAG = "--local"   # node_agent 的本机模式（只经 argv 传递）
LOCAL_KEEPER_SHELL = "/bin/bash"   # macOS 自带 bash 3.2；keeper_script 只用 POSIX/bash 3.2 语法
# 远端 sshd 用 `$SHELL -c <命令>` 执行，Linux 单个参数上限 MAX_ARG_STRLEN = 128 KiB
MAX_REMOTE_COMMAND = 120 * 1024
MAX_AGENT_LINE = 96 * 1024 * 1024   # agent 单行输出上限（最大的是 blob 响应：base64 ≤16MB + JSON 转义）
# 允许空格：内置模式（dmg）的 tmux 在 "/Applications/CC Remote Chat.app/Contents/Resources/bin/tmux"；
# 路径进 keeper 脚本前一律 shlex.quote，空格不会被 shell 拆开
_TMUX_PATH_RE = re.compile(r"/[A-Za-z0-9._/+ -]{1,4095}", re.ASCII)
# 本机 tmux 的 -L 套接字名（运行时配置 tmux_label；默认 ccr，内置模式 ccr-app，与从源码运行的实例互不干扰）
DEFAULT_TMUX_LABEL = "ccr"
_TMUX_LABEL_RE = re.compile(r"[A-Za-z0-9_-]{1,32}", re.ASCII)
_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
# keeper 空闲自停：tmux 里连续这么久没有任何 ccr- 会话就杀掉 _ccr_keeper（keeper step 随之结束）。
# 兜底"启动超时后 keeper step 才姗姗来迟""app 崩溃没来得及 stop_keeper"等情况留下的空闲 keeper。
KEEPER_IDLE_EXIT = 600
KEEPER_POLL = 15
# 作业已结束的判定（以 squeue 为准）：squeue 报这些错或输出为空 → 已结束
JOB_GONE_RE = re.compile(r"Invalid job id|has expired|Expired or invalid job", re.I)
JOB_ENDED_STATES = frozenset((
    "BOOT_FAIL", "CANCELLED", "COMPLETED", "COMPLETING", "DEADLINE", "FAILED", "NODE_FAIL", "OUT_OF_MEMORY",
    "PREEMPTED", "REVOKED", "SPECIAL_EXIT", "TIMEOUT"))


class AgentError(Exception):
    """agent 请求失败（中文说明）。"""


class AgentDown(AgentError):
    """与 agent 的连接未建立或已断开。"""


# ---------------------------------------------------------------- 命令构造
_source_cache: dict = {}


def agent_source() -> str:
    st = AGENT_PATH.stat()
    key = (st.st_mtime_ns, st.st_size)
    if _source_cache.get("key") != key:
        _source_cache["src"] = AGENT_PATH.read_text(encoding="utf-8")
        _source_cache["key"] = key
    return _source_cache["src"]


def validate_node(node) -> str:
    if not isinstance(node, str) or not _NODE_RE.fullmatch(node):
        raise ValueError("节点名不合法：%r" % (str(node)[:80],))
    return node


def _node_opt(node) -> str:
    return "" if node is None else " -w " + shlex.quote(validate_node(node))


def first_node(nodelist) -> Optional[str]:
    """Slurm 节点列表表达式的第一个节点：'gpu[0001-0002,0010],x9' → 'gpu0001'；解析不了返回 None。"""
    if not isinstance(nodelist, str):
        return None
    s = nodelist.strip()
    depth, cut = 0, len(s)
    for i, ch in enumerate(s):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            cut = i
            break
    s = s[:cut]
    m = re.fullmatch(r"([A-Za-z0-9._-]*)\[(\d+)[^\]]*\]([A-Za-z0-9._-]*)", s)
    if m:
        s = m.group(1) + m.group(2) + m.group(3)
    return s if _NODE_RE.fullmatch(s) else None


def is_multi_node(nodelist) -> Optional[bool]:
    if not isinstance(nodelist, str) or not nodelist.strip() or nodelist.strip() in ("(null)", "None"):
        return None
    s = nodelist.strip()
    if "," in s:
        return True
    m = re.search(r"\[([^\]]*)\]", s)
    return bool(m and ("," in m.group(1) or "-" in m.group(1)))


def build_agent_remote_command(jobid, source: Optional[str] = None, node: Optional[str] = None) -> str:
    """传给 ssh 的远端命令字符串（两层 shlex.quote）。node：多节点作业时用 srun -w 钉到该节点。"""
    j = remote.validate_jobid(str(jobid))
    src = agent_source() if source is None else source
    if "\x00" in src:
        raise ValueError("agent 源码不能包含 NUL")
    inner = ("exec srun --jobid=%s --overlap -n1 -N1%s -u -J %s python3 -I -u -c %s"
             % (j, _node_opt(node), AGENT_STEP, shlex.quote(src)))
    cmd = "bash -lc " + shlex.quote(inner)
    if len(cmd.encode("utf-8")) > MAX_REMOTE_COMMAND:
        raise ValueError("agent 远端命令过长（%d 字节）" % len(cmd.encode("utf-8")))
    return cmd


def _validate_tmux_path(p) -> str:
    if not isinstance(p, str) or not _TMUX_PATH_RE.fullmatch(p) or "/../" in p + "/":
        raise AgentError("agent 报告的 tmux 路径不合法：%r" % (str(p)[:200],))
    return p


def validate_tmux_label(label) -> str:
    if not isinstance(label, str) or not _TMUX_LABEL_RE.fullmatch(label):
        raise ValueError("tmux 标签不合法：%r" % (str(label)[:80],))
    return label


def local_tmux_label(cfg: dict) -> str:
    """本机（含 fake 的本机）tmux 的 -L 名：运行时配置 tmux_label（内置模式 = ccr-app），没有就是 ccr。"""
    lab = cfg.get("tmux_label")
    return DEFAULT_TMUX_LABEL if not lab else validate_tmux_label(lab)


def local_tmux_args(cfg: dict) -> list:
    """传给 node_agent.py --local 的 tmux 参数（只在运行时配置给了 local_tmux / tmux_label 时才有，
    即内置模式；没给时返回 []，argv 与以前逐字相同）。"""
    out = []
    t = cfg.get("local_tmux")
    if t:
        out.append("--tmux=" + _validate_tmux_path(str(t)))
    if cfg.get("tmux_label"):
        out.append("--tmux-label=" + validate_tmux_label(cfg["tmux_label"]))
    return out


def py_flags(unbuffered: bool = True) -> list:
    """本进程用 sys.executable 再起 Python 子进程时的解释器参数。内置模式（壳以 python3 -I -B 启动后端）
    下 sys.flags.dont_write_bytecode 为真：子进程也加 -B，绝不往 .app 里写 __pycache__；平时不加（与以前相同）。"""
    flags = ["-I"]
    if sys.flags.dont_write_bytecode:
        flags.append("-B")
    if unbuffered:
        flags.append("-u")
    return flags


def keeper_script(tmux_path: str, idle_exit: int = KEEPER_IDLE_EXIT, poll: int = KEEPER_POLL,
                  label: str = DEFAULT_TMUX_LABEL) -> str:
    """keeper step 里 bash -c 执行的脚本：
    - 建 _ccr_keeper 会话（tmux server 由此 step 启动，之后的 ccr- 会话都属于这个 step 的 cgroup）；
      建不了（多半是已有 keeper：duplicate session）就立即退出，不留第二个空转的 keeper step；
    - 只要 _ccr_keeper 在就一直等；list-sessions **成功**且连续 idle_exit 秒没有 ccr- 会话 → 杀掉 _ccr_keeper（自停）。"""
    t = shlex.quote(_validate_tmux_path(tmux_path)) + " -L " + shlex.quote(validate_tmux_label(label)) + " -f /dev/null"
    return ('%s new-session -d -s %s "sleep 2147483647" || exit 0; i=0; '
            'while %s has-session -t %s 2>/dev/null; do sleep %d; '
            "if s=$(%s list-sessions -F '#{session_name}' 2>/dev/null); then "
            "if printf '%%s\\n' \"$s\" | grep -q '^ccr-'; then i=0; else i=$((i+%d)); fi; "
            'if [ "$i" -ge %d ]; then %s kill-session -t %s; fi; fi; done'
            % (t, KEEPER_SESSION, t, KEEPER_SESSION, int(poll), t, int(poll), int(idle_exit), t, KEEPER_SESSION))


def build_keeper_remote_command(jobid, tmux_path: str, node: Optional[str] = None) -> str:
    """在登录节点执行的 keeper 启动命令（§1）：setsid nohup srun ... -J ccr-keeper [-w 节点] bash -c '<脚本>' &"""
    j = remote.validate_jobid(str(jobid))
    script = keeper_script(tmux_path)
    inner = ("setsid nohup srun --jobid=%s --overlap -n1 -N1%s -J %s bash -c %s </dev/null >/dev/null 2>&1 &"
             % (j, _node_opt(node), KEEPER_STEP, shlex.quote(script)))
    return "bash -lc " + shlex.quote(inner)


def _fake_tmux(cfg: dict) -> str:
    t = cfg.get("fake_tmux") or cfg.get("local_tmux") or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    return str(t)


# 本机 claude 的环境：**白名单**，等同在终端.app 新开窗口（login 提供的最小环境），其余全部交给
# `$SHELL -l -i` 按用户自己的 ~/.zprofile、~/.zshrc 等加载。绝不原样继承 app 进程环境——app 可能是从
# 另一个 Claude Code 会话里启动的（实测带 CLAUDECODE=1、CLAUDE_CODE_SESSION_ID、CLAUDE_CODE_MESSAGING_SOCKET、
# ANTHROPIC_AUTH_TOKEN 等），原样继承会让本机 claude 以为自己是别的会话的子进程。
LOCAL_ENV_KEEP = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "SSH_AUTH_SOCK", "__CF_USER_TEXT_ENCODING")
LOCAL_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"   # 与 login(1) 相同；/etc/zprofile 的 path_helper 再补 /etc/paths
_LOCALE_RE = re.compile(r"[A-Za-z]{2,3}(_[A-Za-z]{2,4})?\.UTF-8", re.ASCII)


def _system_locale() -> str:
    """终端.app 默认"启动时设置语言环境变量"：LANG = <系统地区>.UTF-8。取不到时用 en_US.UTF-8。"""
    try:
        r = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLocale"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        loc = r.stdout.decode("utf-8", "replace").strip().split("@")[0] + ".UTF-8"
        if r.returncode == 0 and _LOCALE_RE.fullmatch(loc):
            return loc
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "en_US.UTF-8"


def local_env() -> dict:
    """本机模式下 agent / keeper（tmux server，进而 claude）的环境：只保留 LOCAL_ENV_KEEP 与 LANG/LC_*，
    PATH 重置为系统默认（其余由登录 shell 自己加载）；app 环境里没有 UTF-8 的 LANG 时按系统地区补
    LANG（与终端.app 行为一致，否则 tmux 会把中文显示成 _）。"""
    src = os.environ
    env = {k: src[k] for k in LOCAL_ENV_KEEP if src.get(k)}
    for k, v in src.items():
        if (k == "LANG" or k.startswith("LC_")) and v and "\x00" not in v:
            env[k] = v
    env["PATH"] = LOCAL_BASE_PATH
    if not env.get("HOME"):
        env["HOME"] = os.path.expanduser("~")
    if "UTF-8" not in (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "").upper().replace("UTF8", "UTF-8"):
        env.pop("LC_ALL", None)
        env.pop("LC_CTYPE", None)
        env["LANG"] = _system_locale()
    return env


def fake_env(cfg: dict, local: bool = False) -> dict:
    """fake 模式下 agent / 本机 tmux keeper 的环境变量（只在 fake 模式使用）。
    local=True：fake 的"本机"运行位置（单独的 HOME / TMUX_TMPDIR，模拟另一台机器）。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR", "HOME") and not k.startswith("CCR_")}
    env.update({
        "CCR_FAKE": "1",
        "HOME": remote.fake_home(cfg, local=local),
        "TMUX_TMPDIR": remote.fake_tmpdir(cfg, local=local),
        "CCR_CLAUDE_CMD": shlex.join([sys.executable] + py_flags(unbuffered=False) + [str(FAKE_TUI_PATH)]),
        "CCR_FAKE_TMUX": _fake_tmux(cfg),
    })
    extra = cfg.get("fake_env") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and k.startswith("CCR_FAKE_") and isinstance(v, str):
                env[k] = v
    return env


def build_agent_argv(cfg: dict, jobid, node: Optional[str] = None) -> tuple:
    """返回 (argv, env)。真实远端模式 env=None（不传 env，继承本进程环境）。
    本机（jobid == LOCAL_JOBID）：本地直接运行 node_agent.py --local，env = local_env()（白名单）；
    fake 下的本机同样带 --local，但环境是 fake_env(local=True)（fake 分支优先，用 fake_tui）。"""
    if remote.is_local(jobid):
        argv = [sys.executable] + py_flags() + [str(AGENT_PATH), LOCAL_AGENT_FLAG] + local_tmux_args(cfg)
        return argv, (fake_env(cfg, local=True) if cfg.get("fake") else local_env())
    if cfg.get("fake"):
        remote.validate_jobid(str(jobid))
        return [sys.executable] + py_flags() + [str(AGENT_PATH)], fake_env(cfg)
    return remote.ssh_base_argv(cfg) + [build_agent_remote_command(jobid, node=node)], None


def _kill_tree(proc: subprocess.Popen, grace: float = 2.0) -> None:
    if proc.poll() is None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                try:
                    proc.send_signal(sig)
                except OSError:
                    pass
            try:
                proc.wait(grace)
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------- 单个连接
class _Pending:
    __slots__ = ("event", "resp")

    def __init__(self):
        self.event = threading.Event()
        self.resp = None


class _Conn:
    def __init__(self, link: "AgentLink", argv: list, env: Optional[dict]):
        self.link = link
        self.pending: dict = {}
        self.lock = threading.Lock()
        self.wlock = threading.Lock()
        self.ids = itertools.count(1)
        self.dead = threading.Event()
        self.reason: Optional[str] = None
        self.stderr_tail = collections.deque(maxlen=30)
        self.last_rx = time.monotonic()   # 最近一次从 agent stdout 收到数据（大图片 blob 一行可能要传好几秒）
        kw = {}
        if env is not None:
            kw["env"] = env
        self.proc = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0, close_fds=True,
                                     start_new_session=True, **kw)
        self.t_out = threading.Thread(target=self._read_stdout, name="ccr-agent-out", daemon=True)
        self.t_err = threading.Thread(target=self._read_stderr, name="ccr-agent-err", daemon=True)
        self.t_out.start()
        self.t_err.start()

    def _read_stdout(self):
        """按块读 stdout 并切行（每收到一块都更新 last_rx：传输一条很长的 blob 响应时，ping 不会被误判为超时）。"""
        fd = self.proc.stdout.fileno()
        parts: list = []
        size = 0
        try:
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                self.last_rx = time.monotonic()
                start = 0
                while True:
                    nl = chunk.find(b"\n", start)
                    if nl < 0:
                        if start < len(chunk):
                            parts.append(chunk[start:])
                            size += len(chunk) - start
                        break
                    parts.append(chunk[start:nl])
                    line = b"".join(parts)
                    parts, size = [], 0
                    start = nl + 1
                    self._handle_line(line)
                if size > MAX_AGENT_LINE:   # 防御：异常超长的一行直接丢弃
                    self.stderr_tail.append("（丢弃超长输出行）")
                    parts, size = [], 0
        except (OSError, ValueError):
            pass
        finally:
            self._mark_dead("agent 连接已断开")

    def _handle_line(self, raw: bytes):
        line = raw.strip()
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            self.stderr_tail.append("（无法解析的输出）" + line[:200].decode("utf-8", "replace"))
            return
        if not isinstance(msg, dict):
            return
        rid = msg.get("id")
        if isinstance(rid, int) and not isinstance(rid, bool):
            with self.lock:
                p = self.pending.pop(rid, None)
            if p is not None:
                p.resp = msg
                p.event.set()
        elif isinstance(msg.get("ev"), str):
            self.link._dispatch_event(msg)

    def receiving(self, window: float) -> bool:
        """最近 window 秒内是否收到过数据（链路慢、正在传大图片时 ping 响应会排在后面，不算断开）。"""
        return time.monotonic() - self.last_rx < window

    def _read_stderr(self):
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                s = raw.decode("utf-8", "replace").rstrip()
                if s:
                    self.stderr_tail.append(s[:500])
        except (OSError, ValueError):
            pass

    def _mark_dead(self, reason: str):
        with self.lock:
            if self.reason is None:
                self.reason = reason
            pend = list(self.pending.values())
            self.pending.clear()
        self.dead.set()
        for p in pend:
            p.event.set()  # resp 仍为 None → AgentDown

    def stderr_text(self) -> str:
        return "；".join(list(self.stderr_tail)[-5:])

    def call(self, op: str, params: dict, timeout: float):
        if self.dead.is_set():
            raise AgentDown("与%s agent 的连接已断开" % self.link.where)
        rid = next(self.ids)
        p = _Pending()
        msg = dict(params)
        msg["id"] = rid
        msg["op"] = op
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self.lock:
            self.pending[rid] = p
        try:
            with self.wlock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            with self.lock:
                self.pending.pop(rid, None)
            self._mark_dead("写入 agent 失败")
            raise AgentDown("与%s agent 的连接已断开" % self.link.where)
        if not p.event.wait(timeout):
            with self.lock:
                self.pending.pop(rid, None)
            raise AgentError("agent 请求超时（%s，%gs）" % (op, timeout))
        resp = p.resp
        if resp is None:
            raise AgentDown("与%s agent 的连接已断开" % self.link.where)
        if resp.get("ok"):
            return resp.get("result")
        err = AgentError(str(resp.get("error") or "agent 返回未知错误"))
        code = resp.get("code")
        err.code = code if isinstance(code, str) else None   # 如 "guard"：发键前屏幕守卫未通过
        raise err

    def close(self):
        try:
            self.proc.stdin.close()  # agent 读到 EOF 自行退出
        except OSError:
            pass
        try:
            self.proc.wait(1.0)
        except subprocess.TimeoutExpired:
            pass
        _kill_tree(self.proc)
        self._mark_dead("已关闭")
        for t in (self.t_out, self.t_err):
            if t is not threading.current_thread():
                t.join(5)
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except OSError:
                pass


# ---------------------------------------------------------------- AgentLink
class AgentLink:
    PING_INTERVAL = 30.0
    PING_TIMEOUT = 20.0
    HELLO_TIMEOUT = 90.0
    BACKOFF_FIRST = 2.0
    BACKOFF_MAX = 60.0

    def __init__(self, cfg: dict, jobid: str, on_event: Callable[[dict], None],
                 on_state: Callable[[str, Optional[str]], None]):
        self.cfg = dict(cfg)
        self.jobid = remote.validate_target(str(jobid))   # Slurm jobid，或本机 LOCAL_JOBID
        self.local = remote.is_local(self.jobid)
        self.where = "本机" if self.local else "计算节点"   # 中文提示用
        self.fake = bool(self.cfg.get("fake"))
        self.on_event = on_event
        self.on_state = on_state
        self.state = "down"                  # "connecting" | "ready" | "down" | "ended"（作业已结束，不再重连）
        self.hello: Optional[dict] = None   # 最近一次 hello 结果
        self.last_error: Optional[str] = None
        self.ended = False
        self.job_state: Optional[str] = None      # 最近一次 squeue 看到的作业状态（RUNNING…）
        self.multi_node: Optional[bool] = None    # 作业是否跨多个节点（None=未知）
        self.node: Optional[str] = None           # 多节点作业：agent/keeper 用 srun -w 钉到这个节点
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._conn: Optional[_Conn] = None
        self._ready = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._keeper_lock = threading.Lock()
        self._watched: set = set()

    # ------------------------------------------------------------ 公共接口
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="ccr-agent-link-%s" % self.jobid,
                                            daemon=True)
            self._thread.start()

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """（附加接口）等待连接就绪；超时或已关闭返回 False。"""
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._ready and not self._stop.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return self._ready

    def request(self, op: str, timeout: float = 30, **params):
        with self._lock:
            conn = self._conn if self._ready else None
        if conn is None:
            if self.ended:
                raise AgentDown("作业 %s 已结束" % self.jobid)
            raise AgentDown("与%s agent 的连接未就绪（%s）" % (self.where, self.state))
        if op == "kill" and isinstance(params.get("sid"), str):
            with self._lock:  # agent 端 kill 会先停监视；重连后也不要再恢复监视（否则会发迟到的 exited）
                self._watched.discard(params["sid"])
        res = conn.call(op, params, timeout)
        if op == "watch" and isinstance(params.get("sid"), str):
            with self._lock:
                if params.get("on", True):
                    self._watched.add(params["sid"])
                else:
                    self._watched.discard(params["sid"])
        elif op == "hello" and isinstance(res, dict):
            self.hello = res
        return res

    def ensure_keeper(self, timeout: float = 120, progress: Optional[Callable[[str], None]] = None) -> None:
        """确保 tmux 常驻进程（keeper）在运行：server_alive 为真直接返回，否则启动并轮询 hello。
        progress(text)：可选，报告子步骤（界面上的进度说明；只是文字，不影响流程）。"""
        def note(text):
            if progress is not None:
                try:
                    progress(text)
                except Exception:  # noqa: BLE001
                    log.exception("progress 回调出错")
        deadline = time.monotonic() + timeout
        with self._keeper_lock:
            note("检查 tmux 常驻进程是否在运行…")
            self._fake_delay("keeper")
            h = self.request("hello", timeout=min(30.0, max(5.0, timeout)))
            if h.get("server_alive"):
                return
            if self.fake:
                self._start_fake_keeper()
            elif self.local:
                note("启动本机 tmux 常驻进程…")
                self._start_local_keeper(h.get("tmux_path"))
            else:
                note("查询作业 %s 上已有的 keeper step（squeue -s）…" % self.jobid)
                if self._keeper_steps():
                    # 已有 keeper step（可能正在启动）：先等一会儿，避免重复启动
                    note("已有 keeper step，等待它就绪…")
                    if self._poll_alive(min(deadline, time.monotonic() + 30.0)):
                        return
                # 多节点作业：keeper 钉到 agent 所在节点（tmux 套接字在节点本地 /tmp）
                note("srun --jobid=%s --overlap：启动 keeper…" % self.jobid)
                cmd = build_keeper_remote_command(self.jobid, h.get("tmux_path"), node=self._keeper_node(h))
                rc, _out, err = remote._run_with_retry(remote.ssh_base_argv(self.cfg) + [cmd],
                                                      input_text=None, timeout=60)
                if rc != 0:
                    raise AgentError("启动 tmux 常驻进程失败（rc=%s）：%s" % (rc, (err or "").strip()[-300:]))
            note("等待 tmux 常驻进程就绪…")
            if self._poll_alive(deadline):
                return
            if self.local:
                raise AgentError("等待本机 tmux 常驻进程启动超时（%gs）" % timeout)
            extra = ""
            if not self.fake and self._keeper_steps():
                # keeper step 已提交但还没跑起来（多半在排队等资源）：它若稍后才启动，空闲后会自行退出
                extra = "；keeper step 已提交但尚未就绪，若稍后才启动，无会话时 %d 分钟后会自行退出" % (
                    KEEPER_IDLE_EXIT // 60)
            raise AgentError("等待 tmux 常驻进程启动超时（%gs），请确认作业 %s 仍在运行%s"
                             % (timeout, self.jobid, extra))

    def close(self) -> None:
        self._stop.set()
        with self._cv:
            conn = self._conn
            self._cv.notify_all()
        if conn is not None:
            conn.close()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(15)

    def check_job(self) -> Optional[bool]:
        """同步查询作业是否已结束（以 `squeue -h -j J -o %T|%N` 为准）。
        True = 已结束（同时进入 ended 状态、停止重连）；False = 仍在；None = 查不到（ssh 失败等）。
        本机：永远 False（没有作业，不跑 ssh/squeue）。"""
        if self.local:
            return False
        if self.ended:
            return True
        info = self.probe_job()
        if info["ended"]:
            self._mark_ended(self._ended_text(info))
        return info["ended"]

    # ------------------------------------------------------------ 作业状态 / 节点
    def _squeue_job(self):
        """(rc, out, err) 或 None（fake 模式 / 本机 / 参数不合法）。
        fake 模式（仅 cfg["fake"]）：<数据目录>/fake/ended_jobs 里列出的 jobid 视为已结束（squeue 空输出），
        供测试 / 演示"作业已结束 → 在新作业上续接"；真实模式不读这个文件。"""
        if self.local:
            return None
        if self.fake:
            self._fake_delay("probe")
            try:
                ended = (remote._data_dir(self.cfg) / "fake" / "ended_jobs").read_text(encoding="utf-8").split()
            except OSError:
                return None
            return (0, "", "") if self.jobid in ended else None
        try:
            return remote.run_remote(self.cfg, ["squeue", "-h", "-j", self.jobid, "-o", "%T|%N"], timeout=30,
                                     should_stop=self._stop.is_set)
        except ValueError:
            return None

    def probe_job(self, hint: Optional[str] = None) -> dict:
        """{"ended": True|False|None, "state", "nodes"}。squeue 空输出或报 Invalid job id → 已结束；
        squeue 查不了时退而看 srun 的报错（hint）里有没有 "has expired" / "Expired or invalid job"。
        本机：永远"未结束"（ended=False），不跑 ssh。"""
        if self.local:
            return {"ended": False, "state": None, "nodes": None}
        ended, state, nodes = None, None, None
        r = self._squeue_job()
        if r is not None:
            rc, out, err = r
            if rc == 0:
                rows = [m for m in (re.match(r"^([A-Z_]+)\b[^|]*\|(.*)$", x.strip()) for x in out.splitlines()) if m]
                if not rows:
                    ended = True
                else:
                    state, nodes = rows[-1].group(1), rows[-1].group(2).strip() or None
                    ended = state in JOB_ENDED_STATES
            elif rc != 130 and JOB_GONE_RE.search((err or "") + (out or "")):
                ended = True
        if ended is None and hint and JOB_GONE_RE.search(hint):
            ended = True
        if state:
            self.job_state = state
        if nodes:
            mn = is_multi_node(nodes)
            if mn is not None:
                self.multi_node = mn
            if mn and self.node is None:
                self.node = first_node(nodes)
        return {"ended": ended, "state": state, "nodes": nodes}

    def _ended_text(self, info: dict) -> str:
        st = info.get("state")
        return "作业 %s 已结束%s" % (self.jobid, "（%s）" % st if st else "（squeue 查不到该作业）")

    def _mark_ended(self, detail: str) -> None:
        with self._cv:
            if self.ended:
                return
            self.ended = True
        log.info("%s，停止重连", detail)
        self._set_state("ended", detail)
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    def _keeper_node(self, h: Optional[dict]) -> Optional[str]:
        """keeper 要钉到的节点 = agent 实际所在节点（单节点作业不加 -w，与以前一致）。"""
        if self.multi_node is False:
            return None
        h = h or self.hello or {}
        n = h.get("slurm_node") or (h.get("node") or "").split(".")[0] or None
        if n is None or not _NODE_RE.fullmatch(str(n)):
            return None
        return n

    # ------------------------------------------------------------ keeper
    def _poll_alive(self, deadline: float) -> bool:
        while True:
            h = self.request("hello", timeout=30)
            if h.get("server_alive"):
                return True
            if time.monotonic() >= deadline or self._stop.wait(1.0):
                return False

    def _keeper_steps(self) -> list:
        if self.local or self.fake:
            return []
        try:
            rc, out, _err = remote.run_remote(self.cfg, ["squeue", "-h", "-s", "-j", self.jobid, "-o", "%i|%j"],
                                              timeout=30)
        except ValueError:
            return []
        if rc != 0:
            return []
        return [s for s, name in remote.parse_job_steps(out) if name == KEEPER_STEP]

    def _start_local_keeper(self, tmux_path) -> None:
        """本机 keeper：与远端同一份 keeper_script，由 /bin/bash（3.2）在新会话里后台运行（start_new_session：
        不在 app 的进程组里，app 退出/重启后 tmux server 与 claude 照常运行，app 再启动时重新接回）。
        env = local_env()（白名单），cwd = HOME，标准输入输出全部 /dev/null。"""
        script = keeper_script(tmux_path, label=local_tmux_label(self.cfg))   # 校验 tmux 路径与标签
        try:
            proc = subprocess.Popen([LOCAL_KEEPER_SHELL, "-c", script], shell=False, env=local_env(),
                                    cwd=os.path.expanduser("~"), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                    start_new_session=True)
        except OSError as e:
            raise AgentError("启动本机 tmux 常驻进程失败：%s" % e)
        # keeper 结束时回收（避免留下僵尸进程）；app 先退出的话 keeper 由 launchd 接管
        threading.Thread(target=proc.wait, name="ccr-keeper-reap", daemon=True).start()

    def _start_fake_keeper(self) -> None:
        env = fake_env(self.cfg, local=self.local)
        for d in (env["HOME"], env["TMUX_TMPDIR"]):
            os.makedirs(d, mode=0o700, exist_ok=True)
        label = local_tmux_label(self.cfg) if self.local else DEFAULT_TMUX_LABEL
        argv = [env["CCR_FAKE_TMUX"], "-L", label, "-f", "/dev/null", "new-session", "-d", "-s", KEEPER_SESSION,
                "sleep", "2147483647"]
        try:
            r = subprocess.run(argv, shell=False, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=20, cwd=env["HOME"])
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AgentError("启动本机 tmux 常驻进程失败：%s" % e)
        if r.returncode != 0 and b"duplicate session" not in r.stderr:
            raise AgentError("启动本机 tmux 常驻进程失败：%s" % r.stderr.decode("utf-8", "replace").strip()[-300:])

    # ------------------------------------------------------------ 内部
    def _fake_delay(self, phase: str) -> None:
        """仅 fake 模式：按 cfg["fake_delays"]（app.py --fake-delays）在某阶段前等一会儿，用来观察进度界面。
        默认（没配）不等待；真实模式永远不等待。"""
        if not self.fake:
            return
        d = self.cfg.get("fake_delays")
        v = d.get(phase) if isinstance(d, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 600:
            self._stop.wait(float(v))

    def _dispatch_event(self, msg: dict) -> None:
        if msg.get("ev") == "exited":
            with self._lock:
                self._watched.discard(msg.get("sid"))
        try:
            self.on_event(msg)
        except Exception:  # noqa: BLE001 回调异常不能打断读线程
            log.exception("on_event 回调出错")

    def _set_state(self, state: str, detail: Optional[str]) -> None:
        with self._cv:
            changed = state != self.state or detail is not None
            self.state = state
            self._ready = state == "ready"
            if detail:
                self.last_error = detail
            self._cv.notify_all()
        if changed:
            try:
                self.on_state(state, detail)
            except Exception:  # noqa: BLE001
                log.exception("on_state 回调出错")

    def _connect(self) -> Optional[_Conn]:
        # 多节点作业：agent 钉到固定节点（首次 = 作业节点列表的第一个；之后 = 上次 agent 所在节点），
        # 保证重连后和 keeper/tmux 在同一节点。单节点作业不加 -w（命令与以前完全相同）。
        argv, env = build_agent_argv(self.cfg, self.jobid, node=self.node if self.multi_node else None)
        if self.fake:   # fake（含 fake 的本机）：临时 HOME / TMUX_TMPDIR
            for d in (env["HOME"], env["TMUX_TMPDIR"]):
                os.makedirs(d, mode=0o700, exist_ok=True)
        conn = _Conn(self, argv, env)
        with self._lock:
            if self._stop.is_set():
                stopped = True
            else:
                stopped = False
                self._conn = conn
        if stopped:
            conn.close()
            return None
        try:
            h = conn.call("hello", {}, self.HELLO_TIMEOUT)
            if not isinstance(h, dict):
                raise AgentError("agent hello 返回格式不正确")
            self.hello = h
            hn = self._keeper_node(h)
            if self.multi_node and hn:
                self.node = hn   # 以 agent 实际所在节点为准，之后的重连与 keeper 都钉到这里
            with self._lock:
                watched = sorted(self._watched)
            for sid in watched:  # 重连后恢复监视（新 agent 没有旧状态）
                try:
                    conn.call("watch", {"sid": sid, "on": True}, 30)
                except AgentDown:
                    raise
                except AgentError:
                    with self._lock:
                        self._watched.discard(sid)
        except AgentError as e:
            err = conn.stderr_text()
            conn.close()
            with self._lock:
                if self._conn is conn:
                    self._conn = None
            raise AgentError("%s%s" % (e, ("：" + err) if err else ""))
        return conn

    def _run(self) -> None:
        delay = self.BACKOFF_FIRST
        probed = False
        while not self._stop.is_set():
            if not probed:
                probed = True
                info = self.probe_job()   # 首次连接前：作业是否还在 + 节点列表（多节点作业要 -w）
                if info["ended"]:
                    self._mark_ended(self._ended_text(info))
                    break
            self._set_state("connecting", None)
            reason = None
            conn = None
            try:
                self._fake_delay("connect")
                fd = self.cfg.get("fake_delays") if self.fake else None
                if isinstance(fd, dict) and self.jobid in (fd.get("unreachable") or ()):
                    # 仅 fake：模拟登录节点连不上（演示"第 n 次连接失败，稍后自动重试"）
                    raise AgentError("ssh: connect to host %s port 22: Operation timed out"
                                     % (self.cfg.get("host") or "server"))
                conn = self._connect()
            except (AgentError, OSError, ValueError) as e:
                reason = "连接%s agent 失败：%s" % (self.where, e)
            if conn is not None:
                delay = self.BACKOFF_FIRST
                self._set_state("ready", None)
                while not self._stop.is_set():
                    if conn.dead.wait(self.PING_INTERVAL):
                        break
                    try:
                        conn.call("ping", {}, self.PING_TIMEOUT)
                    except AgentDown:
                        break
                    except AgentError:
                        # ping 超时：如果数据一直在流进来（例如正在传一张大图片），说明链路活着，只是排在后面
                        if not conn.receiving(self.PING_TIMEOUT):
                            break
                err = conn.stderr_text()
                reason = (conn.reason or "agent 连接已断开") + (("：" + err) if err else "")
                with self._lock:
                    if self._conn is conn:
                        self._conn = None
                    self._ready = False
                conn.close()
            if self._stop.is_set():
                break
            # 连不上 / 断开：先查作业是否已结束（到时限被回收等）——结束了就不再无限重连
            info = self.probe_job(hint=reason)
            if info["ended"]:
                self._mark_ended(self._ended_text(info) + ("：" + reason if reason else ""))
                break
            self._set_state("down", reason)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.BACKOFF_MAX)
        with self._lock:
            conn = self._conn
            self._conn = None
        if conn is not None:
            conn.close()
        if not self.ended:
            self._set_state("down", None)
