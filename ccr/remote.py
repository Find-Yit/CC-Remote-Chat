"""远端执行层（v2）：参数校验、ssh 参数、claude 白名单参数、远端只读执行。

红线（docs/SPEC_V2_TUI.md 第 0 节）：
- claude 只产出 §0.2 白名单参数（build_claude_args 强制 + assert_claude_args 二次防线）；不加任何 headless 参数。
- 不给远端设置任何环境变量；不使用 ssh SendEnv/SetEnv；本地 subprocess 不传 env。
- 本地 subprocess 一律 argv 列表、shell=False；远端字符串逐层 shlex.quote。
- 本应用数据只在 ~/.cc-remote-chat/（这里只会创建 ssh 控制套接字目录）。
"""
from __future__ import annotations

import os
import pathlib
import posixpath
import re
import shlex
import subprocess
import threading
import time

__all__ = [
    "PERMISSION_MODES", "EFFORT_LEVELS", "CLAUDE_ARG_FLAGS",
    "LOCAL_JOBID", "is_local", "validate_target",
    "validate_jobid", "validate_session_uuid", "validate_model", "validate_permission_mode",
    "validate_effort", "validate_path", "validate_host", "validate_sid", "parse_extra_srun_args",
    "ssh_base_argv", "close_control_master", "build_claude_args", "assert_claude_args",
    "resolve_remote_path", "run_remote", "run_remote_python", "SRUN_ARG_WHITELIST",
    "fake_home", "fake_tmpdir", "parse_job_steps", "validate_settings_path",
]

# 本应用数据目录（默认）。ssh 控制套接字目录 = APP_DIR/ssh。
APP_DIR = pathlib.Path("~/.cc-remote-chat")
_DEFAULT_CONTROL_PATH = "~/.cc-remote-chat/ssh/cm-%C"

# §0.2 白名单里的权限模式；"default" 是 manual 的旧名，校验时规范化为 manual。
PERMISSION_MODES = ("manual", "acceptEdits", "auto", "plan", "bypassPermissions", "dontAsk")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# §0.2 白名单：flag -> 是否带值。除此之外的任何参数一律禁止。
CLAUDE_ARG_FLAGS = {
    "--resume": True,
    "--model": True,
    "--permission-mode": True,
    "--dangerously-skip-permissions": False,
    "--effort": True,
    # 仅当用户选择了非默认 API key：值只能是 node_agent 自己生成的 <HOME>/.cc-remote-chat/keys/<16hex>.json
    # （内容严格为 {"env":{"ANTHROPIC_AUTH_TOKEN":…}}）；本地/前端不能传任意路径，见 validate_settings_path
    "--settings": True,
}
_SETTINGS_TAIL = r"/\.cc-remote-chat/keys/[0-9a-f]{16}\.json"

_JOBID_RE = re.compile(r"\d{1,12}(_\d{1,8})?", re.ASCII)
# 运行位置：服务器上的 Slurm 作业（jobid），或本机（本地 Mac 上的 claude）。本机用这个特殊"jobid"，
# 让按 jobid 分组的逻辑（AgentLink、session.jobid、sessions.json、SSE…）原样复用。
# validate_jobid 仍只接受真正的 Slurm 作业号（srun/squeue 命令只用它）；会话的运行位置一律用 validate_target。
LOCAL_JOBID = "local"
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", re.ASCII)
_MODEL_RE = re.compile(r"[A-Za-z0-9._:\-\[\]]{1,80}", re.ASCII)
_HOST_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@-]{0,252}", re.ASCII)
# extra_srun_args 精确白名单（docs/FIXES.md F2）：只接受完整长选项名、必须 --name=value 形式。
SRUN_ARG_WHITELIST = (
    "--gres", "--gpus", "--gpus-per-node", "--gpus-per-task", "--cpus-per-task", "--cpus-per-gpu",
    "--mem", "--mem-per-cpu", "--mem-per-gpu", "--time", "--cpu-bind", "--gpu-bind",
)
_SRUN_ARG_SET = frozenset(SRUN_ARG_WHITELIST)
_SRUN_VALUE_RE = re.compile(r"[A-Za-z0-9_:,.+%@-]{1,64}", re.ASCII)
# 本地会话 id（session.py 生成 "s" + 12 位小写 hex）；tmux 会话名 = "ccr-" + sid
_SID_RE = re.compile(r"s[0-9a-f]{12}", re.ASCII)
# squeue -s 输出的 step id：<jobid>[_<array>].<step>
_STEPID_RE = re.compile(r"\d+(_\d+)?\.\d+", re.ASCII)
# unix 域套接字路径上限：macOS sun_path 104 字节（含 NUL）；ssh 建立 master 时会在 ControlPath 后
# 追加 "." + 16 位随机字符的临时后缀，因此 ControlPath 本身最长 103 - 17 = 86 字符。
_MAX_CONTROL_PATH = 86

_MAX_PATH = 4096


# ---------------------------------------------------------------- 校验
def _as_str(s, what: str) -> str:
    if not isinstance(s, str):
        raise ValueError(f"{what} 必须是字符串")
    return s


def validate_jobid(s) -> str:
    s = _as_str(s, "jobid").strip()
    if not _JOBID_RE.fullmatch(s):
        raise ValueError(f"无效的 jobid：{s!r}（应为数字，或 数字_数字）")
    return s


def is_local(target) -> bool:
    """运行位置是否为本机（LOCAL_JOBID）。"""
    return target == LOCAL_JOBID


def validate_target(s) -> str:
    """会话的运行位置：本机 = LOCAL_JOBID（"local"）；否则必须是合法的 Slurm jobid。"""
    s = _as_str(s, "运行位置").strip()
    if s == LOCAL_JOBID:
        return s
    if not _JOBID_RE.fullmatch(s):
        raise ValueError(f"无效的运行位置：{s!r}（应为作业 jobid，或 {LOCAL_JOBID!r} 表示本机）")
    return s


def validate_session_uuid(s) -> str:
    s = _as_str(s, "会话 id").strip()
    if not _UUID_RE.fullmatch(s):
        raise ValueError(f"无效的 claude 会话 id：{s!r}")
    return s.lower()


def validate_model(s) -> str | None:
    if s is None:
        return None
    s = _as_str(s, "模型").strip()
    if s == "":
        return None
    if not _MODEL_RE.fullmatch(s) or s.startswith("-"):
        raise ValueError(f"无效的模型名：{s!r}")
    return s


def validate_permission_mode(s) -> str:
    s = _as_str(s, "权限模式").strip()
    if s == "default":
        return "manual"
    if s not in PERMISSION_MODES:
        raise ValueError(f"无效的权限模式：{s!r}（可选：{', '.join(PERMISSION_MODES)}）")
    return s


def validate_effort(s) -> str | None:
    """effort 档位；None/空串 = 不指定。"""
    if s is None:
        return None
    s = _as_str(s, "effort").strip()
    if s == "":
        return None
    if s not in EFFORT_LEVELS:
        raise ValueError(f"无效的 effort：{s!r}（可选：{', '.join(EFFORT_LEVELS)}）")
    return s


def validate_path(s) -> str:
    """绝对路径或 ~ / ~/...；禁 NUL、换行、长度>4096。原样返回（不展开）。"""
    s = _as_str(s, "路径")
    if not s:
        raise ValueError("路径不能为空")
    if len(s) > _MAX_PATH:
        raise ValueError("路径过长")
    if "\x00" in s or "\n" in s or "\r" in s:
        raise ValueError("路径不能包含 NUL 或换行")
    if not (s.startswith("/") or s == "~" or s.startswith("~/")):
        raise ValueError(f"路径必须是绝对路径或以 ~/ 开头：{s!r}")
    return s


def validate_host(s) -> str:
    s = _as_str(s, "主机").strip()
    if not s:
        raise ValueError("还没有设置服务器：请在「设置」里填写 SSH 主机别名（~/.ssh/config 里的 Host）")
    if not _HOST_RE.fullmatch(s):
        raise ValueError(f"无效的主机名：{s!r}")
    return s


def _validate_abs_path(s) -> str:
    s = validate_path(s)
    if not s.startswith("/"):
        raise ValueError(f"需要已展开的绝对路径：{s!r}")
    return s


def _srun_whitelist_hint() -> str:
    return "只允许以下 srun 选项（必须写成 --选项=值）：" + "、".join(SRUN_ARG_WHITELIST)


def parse_extra_srun_args(s) -> list[str]:
    """extra_srun_args 精确白名单校验（config 与 remote 共用）。返回 token 列表。

    - 只接受 SRUN_ARG_WHITELIST 中的完整长选项名，且必须是 --name=value；
    - 不接受缩写、短选项、无值形式；value 必须匹配 ^[A-Za-z0-9_:,.+%@-]{1,64}$（禁止 '/'）。
    """
    if s is None:
        return []
    if isinstance(s, (list, tuple)):
        tokens = list(s)
    else:
        s = _as_str(s, "extra_srun_args")
        if len(s) > 2000:
            raise ValueError("extra_srun_args 过长")
        if "\x00" in s or "\n" in s or "\r" in s:
            raise ValueError("extra_srun_args 不能包含换行或空字符")
        tokens = s.split()
    out = []
    for tok in tokens:
        if not isinstance(tok, str):
            raise ValueError("extra_srun_args 必须是字符串")
        name, eq, value = tok.partition("=")
        if name not in _SRUN_ARG_SET:
            raise ValueError(f"不允许的 srun 参数：{tok[:80]!r}。{_srun_whitelist_hint()}")
        if not eq or not _SRUN_VALUE_RE.fullmatch(value):
            raise ValueError(f"srun 参数取值不合法：{tok[:80]!r}（必须是 {name}=值，值只能含字母数字和 _:,.+%@-，"
                             f"不能含 /，长度 1–64）")
        out.append(tok)
    return out


def validate_sid(s) -> str:
    """本地会话 id：s + 12 位小写 hex。用于 srun step 名 ccr-<sid>。"""
    s = _as_str(s, "会话 id")
    if not _SID_RE.fullmatch(s):
        raise ValueError(f"无效的本地会话 id：{s[:80]!r}")
    return s


# ---------------------------------------------------------------- ssh
def _control_dir(control_dir=None) -> pathlib.Path:
    if control_dir is not None:
        return pathlib.Path(control_dir).expanduser()
    return (APP_DIR / "ssh").expanduser()


def _ensure_private_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir(parents=True) 只对最末级目录应用 mode；应用数据根目录也要收紧为 0700
    targets = [p]
    if p.parent == APP_DIR.expanduser():
        targets.append(p.parent)
    for d in targets:
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def ssh_base_argv(cfg: dict, *, control_dir=None) -> list[str]:
    """["ssh","-T","-o","BatchMode=yes",...,host]。只允许这些选项，绝不加 SendEnv/SetEnv。

    control_dir：ssh 控制套接字目录。优先用参数，其次运行时配置 cfg["control_dir"]（app.py 按
    --data-dir 注入，不持久化），都没有时用默认 ~/.cc-remote-chat/ssh。
    目录路径过长（unix 套接字路径上限）时不启用连接复用，避免 ssh 直接失败。
    """
    host = validate_host(cfg.get("host") or "")
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4"]
    if control_dir is None:
        control_dir = cfg.get("control_dir") or None
    if cfg.get("ssh_multiplex", True):
        d = _control_dir(control_dir)
        cp = _DEFAULT_CONTROL_PATH if control_dir is None else str(d / "cm-%C")
        # %C 展开为 40 位 hex；过长则不启用复用（否则 ssh 报 path too long 直接失败）
        if len(str(d)) + len("/cm-") + 40 <= _MAX_CONTROL_PATH:
            _ensure_private_dir(d)
            argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={cp}", "-o", "ControlPersist=600"]
    argv.append(host)
    return argv


def close_control_master(cfg: dict, timeout: float = 10.0) -> bool:
    """app 退出时关闭本应用自己的 ssh 复用主连接（`ssh -O exit`，只针对本应用的 ControlPath），
    避免 ControlPersist 让后台 ssh 进程多留 10 分钟。失败忽略；fake 模式或未启用复用时 no-op。"""
    if cfg.get("fake") or not cfg.get("ssh_multiplex", True):
        return False
    try:
        argv = ssh_base_argv(cfg)
    except ValueError:
        return False
    if "ControlMaster=auto" not in argv:
        return False
    argv = argv[:-1] + ["-O", "exit", argv[-1]]
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout, shell=False)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------- claude 参数（§0.2）
def validate_settings_path(path, home) -> str:
    """--settings 的值只能是 `<home>/.cc-remote-chat/keys/<16 位 hex>.json`（node_agent 生成的 key 文件）。
    home = 远端（计算节点）$HOME；没有 home 就无法校验 → 一律拒绝。"""
    if not isinstance(home, str) or not home.startswith("/") or "\n" in home or "\x00" in home:
        raise ValueError("缺少远端 HOME，无法校验 --settings 路径")
    home = posixpath.normpath(home)
    if home.startswith("//"):
        home = "/" + home.lstrip("/")
    root = "" if home == "/" else home
    if not isinstance(path, str) or not re.fullmatch(re.escape(root) + _SETTINGS_TAIL, path):
        raise ValueError("--settings 只允许 agent 生成的 API key 文件（~/.cc-remote-chat/keys/<指纹>.json）")
    return path


def build_claude_args(model=None, permission_mode=None, skip_permissions=False, effort=None,
                      resume=None, *, settings_path=None, home=None) -> list[str]:
    """交互式 claude 的参数（不含 "claude" 本身）。只产出 §0.2 白名单参数；全部不选时返回 []。

    settings_path/home：只用于校验 node_agent 生成的 key 文件路径（本地会话代码不传：API key 由
    start 请求的 api_key 字段交给 agent，agent 写好 0600 文件后自己追加 --settings）。"""
    args: list[str] = []
    if resume is not None and resume != "":
        args += ["--resume", validate_session_uuid(resume)]
    m = validate_model(model)
    if m is not None:
        args += ["--model", m]
    if permission_mode is not None and permission_mode != "":
        args += ["--permission-mode", validate_permission_mode(permission_mode)]
    if not isinstance(skip_permissions, bool):
        raise ValueError("skip_permissions 必须是布尔值")
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    e = validate_effort(effort)
    if e is not None:
        args += ["--effort", e]
    if settings_path is not None:
        args += ["--settings", validate_settings_path(settings_path, home)]
    assert_claude_args(args, home=home)
    return args


def assert_claude_args(args, home=None) -> None:
    """二次防线：逐项检查，任何非白名单参数或不合法取值直接报错（AssertionError）。
    --settings 只有在给出远端 home 且值为 agent 生成的 key 文件路径时才允许。"""
    if not isinstance(args, list):
        raise AssertionError("claude 参数必须是列表")
    seen = set()
    i = 0
    while i < len(args):
        a = args[i]
        if not isinstance(a, str) or a not in CLAUDE_ARG_FLAGS or a in seen:
            raise AssertionError(f"禁止的 claude 参数：{a!r}")
        seen.add(a)
        if not CLAUDE_ARG_FLAGS[a]:
            i += 1
            continue
        if i + 1 >= len(args) or not isinstance(args[i + 1], str):
            raise AssertionError(f"claude 参数缺少取值：{a}")
        v = args[i + 1]
        try:
            if a == "--resume":
                ok = validate_session_uuid(v) == v
            elif a == "--model":
                ok = validate_model(v) == v
            elif a == "--permission-mode":
                ok = v in PERMISSION_MODES
            elif a == "--settings":
                ok = home is not None and validate_settings_path(v, home) == v
            else:
                ok = v in EFFORT_LEVELS
        except ValueError:
            ok = False
        if not ok:
            raise AssertionError(f"claude 参数取值不合法：{a} {v!r}")
        i += 2


# ---------------------------------------------------------------- 远端只读执行
def _run_interruptible(argv: list[str], *, input_text, timeout, should_stop) -> tuple[int, str, str]:
    """subprocess.run 的可中止版本：每 0.25s 检查 should_stop()，为真则杀掉 ssh 并返回 rc=130。"""
    try:
        p = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                             start_new_session=True)
    except OSError as e:
        return (127, "", f"无法执行 ssh：{e}")
    deadline = time.monotonic() + timeout
    inp = input_text if input_text is not None else ""
    while True:
        try:
            out, err = p.communicate(input=inp, timeout=0.25)
            return (p.returncode, out or "", err or "")
        except subprocess.TimeoutExpired:
            inp = None  # 输入只能在第一次 communicate 时传
            stop = should_stop()
            if not stop and time.monotonic() < deadline:
                continue
            try:
                os.killpg(p.pid, 9)
            except OSError:
                p.kill()
            try:
                out, err = p.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError):
                out, err = "", ""
            if stop:
                return (130, out or "", (err or "") + "\n已中止")
            return (124, out or "", (err or "") + f"\n远端命令超时（{timeout}s）")


def _run_with_retry(argv: list[str], *, input_text, timeout, retries=2,
                    should_stop=None) -> tuple[int, str, str]:
    last = (255, "", "")
    for attempt in range(retries + 1):
        if should_stop is not None:
            if should_stop():
                return (130, "", "已中止")
            last = _run_interruptible(argv, input_text=input_text, timeout=timeout, should_stop=should_stop)
            if last[0] != 255 or attempt == retries:
                return last
            time.sleep(1.0 + attempt)
            continue
        try:
            cp = subprocess.run(argv, shell=False, input=input_text if input_text is not None else "",
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, start_new_session=True)
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            err = e.stderr or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            return (124, out, err + f"\n远端命令超时（{timeout}s）")
        except OSError as e:
            return (127, "", f"无法执行 ssh：{e}")
        last = (cp.returncode, cp.stdout, cp.stderr)
        # 255 = ssh 自身失败（连接被拒、MaxStartups 等），可重试
        if cp.returncode != 255 or attempt == retries:
            return last
        time.sleep(1.0 + attempt)
    return last


def run_remote(cfg: dict, argv: list[str], timeout=30, *, should_stop=None) -> tuple[int, str, str]:
    """在远端 bash -lc 中执行 argv（逐个 shlex.quote）。should_stop() 为真时尽快杀掉 ssh（rc=130）。"""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv 必须是非空字符串列表")
    for a in argv:
        if "\x00" in a:
            raise ValueError("参数不能包含 NUL")
    inner = " ".join(shlex.quote(a) for a in argv)
    remote_cmd = "bash -lc " + shlex.quote(inner)
    return _run_with_retry(ssh_base_argv(cfg) + [remote_cmd], input_text=None, timeout=timeout,
                           should_stop=should_stop)


def run_remote_python(cfg: dict, script: str, args: list[str], timeout=60) -> tuple[int, str, str]:
    """ssh host "python3 -I - <quoted args>"，script 经 stdin 传入，远端不落文件。"""
    if not isinstance(script, str):
        raise ValueError("script 必须是字符串")
    args = list(args or ())
    if not all(isinstance(a, str) and "\x00" not in a for a in args):
        raise ValueError("args 必须是不含 NUL 的字符串")
    remote_cmd = "python3 -I -" + "".join(" " + shlex.quote(a) for a in args)
    return _run_with_retry(ssh_base_argv(cfg) + [remote_cmd], input_text=script, timeout=timeout)


_home_cache: dict[str, str] = {}
_home_lock = threading.Lock()


def _remote_home(cfg: dict) -> str:
    if cfg.get("fake"):
        return fake_home(cfg)
    host = validate_host(cfg.get("host") or "")
    with _home_lock:
        if host in _home_cache:
            return _home_cache[host]
    rc, out, err = run_remote(cfg, ["printenv", "HOME"], timeout=30)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    home = lines[-1] if lines else ""
    if rc != 0 or not home.startswith("/") or "\n" in home:
        raise RuntimeError(f"无法获取远端 $HOME（rc={rc}）：{err.strip()[-500:]}")
    with _home_lock:
        _home_cache[host] = home
    return home


def resolve_remote_path(cfg: dict, path) -> str:
    """把 ~ 展开为远端 $HOME（每个主机缓存一次），返回规范化后的绝对路径。"""
    p = validate_path(path)
    if p == "~" or p.startswith("~/"):
        p = _remote_home(cfg) + p[1:]
    p = posixpath.normpath(p)
    if p.startswith("//"):
        p = "/" + p.lstrip("/")
    return _validate_abs_path(p)


# ---------------------------------------------------------------- 工具
def parse_job_steps(output: str) -> list[tuple[str, str]]:
    """解析 `squeue -h -s -j <J> -o '%i|%j'` 输出为 [(step_id, step_name)]（只保留合法 step id）。"""
    res = []
    for line in (output or "").splitlines():
        stepid, sep, name = line.strip().partition("|")
        stepid, name = stepid.strip(), name.strip()
        if sep and _STEPID_RE.fullmatch(stepid):
            res.append((stepid, name))
    return res


def _interruptible_sleep(seconds: float, should_stop) -> bool:
    """睡 seconds 秒（每 0.2s 检查一次 should_stop）。被中止返回 True。"""
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if should_stop is not None and should_stop():
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(0.2, left))


# ---------------------------------------------------------------- fake 模式路径
def _data_dir(cfg: dict) -> pathlib.Path:
    cd = cfg.get("control_dir")
    if cd:
        return pathlib.Path(cd).expanduser().parent
    return APP_DIR.expanduser()


def fake_home(cfg: dict, local: bool = False) -> str:
    """fake 模式下假 claude 的 HOME（cfg["fake_home"]，默认 <数据目录>/fake/home）。
    local=True：fake 的"本机"运行位置单独一套 HOME（cfg["fake_local_home"]，默认与 fake_home 同级的 local-home），
    模拟另一台机器（历史、key 文件、sessions json 都与"服务器"分开）。"""
    p = cfg.get("fake_home") or str(_data_dir(cfg) / "fake" / "home")
    p = os.path.realpath(os.path.expanduser(p))
    if local:
        p = os.path.realpath(os.path.expanduser(cfg.get("fake_local_home") or os.path.join(os.path.dirname(p),
                                                                                         "local-home")))
    return p


def fake_tmpdir(cfg: dict, local: bool = False) -> str:
    """fake 模式下本机 tmux 的 TMUX_TMPDIR（cfg["fake_tmpdir"]，默认 <数据目录>/fake/tmux）；
    local=True：fake "本机"运行位置自己的 tmux server（cfg["fake_local_tmpdir"]，默认与 fake_tmpdir 同级的 local-tmux）。"""
    p = cfg.get("fake_tmpdir") or str(_data_dir(cfg) / "fake" / "tmux")
    p = os.path.realpath(os.path.expanduser(p))
    if local:
        p = os.path.realpath(os.path.expanduser(cfg.get("fake_local_tmpdir") or os.path.join(os.path.dirname(p),
                                                                                           "local-tmux")))
    return p
