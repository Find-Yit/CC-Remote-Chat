"""本机：用 macOS 系统自带的"选择文件夹"对话框（Finder 同款）选 claude 的启动目录（SPEC §13）。

- 只在用户点击「浏览…」时由 `POST /api/local/choose_folder` 调用；同一时间只允许一个对话框（再点 → 409）。
- 执行 `/usr/bin/osascript -e <固定脚本行>… [默认目录]`：argv 方式、不经 shell；默认目录作为 `on run argv`
  的参数传入，**绝不**拼进 AppleScript 源码（防注入）。
- 只用 StandardAdditions 的 `choose folder`（`tell me to activate` 让对话框出现在最前面）；不用
  `tell application "Finder"` / `"System Events"`（会触发 TCC 自动化授权弹窗）。
- 结果：{"path": "/Users/…/dir"}（去掉末尾 "/"，根目录 "/" 除外）；用户取消（osascript 报 -128）→ {"cancelled": true}。
- 超时 10 分钟（杀掉 osascript）；fake 模式或非 macOS / 没有 osascript → 不可用（501）。
"""
from __future__ import annotations

import os
import posixpath
import subprocess
import sys
import threading
from typing import Optional

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT = 600.0
PROMPT = "选择 claude 的启动目录"
# 固定的脚本（每行一个 -e）；默认目录只经 argv 传入
SCRIPT_LINES = (
    "on run argv",
    "tell me to activate",
    "if (count of argv) > 0 then",
    'set f to choose folder with prompt "%s" default location (POSIX file (item 1 of argv))' % PROMPT,
    "else",
    'set f to choose folder with prompt "%s"' % PROMPT,
    "end if",
    "return POSIX path of f",
    "end run",
)
MAX_PATH = 4096

_busy = threading.Lock()


class Unavailable(Exception):
    """当前环境不能弹系统对话框（fake 模式 / 非 macOS / 没有 osascript）→ 501。"""


class Busy(Exception):
    """已经有一个选择文件夹对话框开着 → 409。"""


class DialogError(Exception):
    """osascript 失败 / 超时（中文说明）；timeout=True → 504。"""

    def __init__(self, message: str, timeout: bool = False):
        super().__init__(message)
        self.timeout = timeout


def available(cfg: Optional[dict] = None) -> bool:
    if cfg and cfg.get("fake"):
        return False
    return sys.platform == "darwin" and os.path.isfile(OSASCRIPT) and os.access(OSASCRIPT, os.X_OK)


def default_dir(path) -> Optional[str]:
    """前端给的默认目录（"~"、"~/x"、绝对路径）→ 存在的绝对目录；不合法 / 不存在 → None（对话框不带默认位置）。"""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH or "\x00" in path or "\n" in path \
            or "\r" in path:
        return None
    if path == "~" or path.startswith("~/"):
        path = os.path.expanduser("~") + path[1:]
    if not path.startswith("/"):
        return None
    path = posixpath.normpath(path)
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    return path if os.path.isdir(path) else None


def build_argv(default: Optional[str]) -> list:
    argv = [OSASCRIPT]
    for line in SCRIPT_LINES:
        argv += ["-e", line]
    if default:
        argv.append(default)   # on run argv 的 item 1（数据，不是脚本源码）
    return argv


def normalize_result(out: str) -> Optional[str]:
    """osascript 的输出（POSIX path，目录带末尾 "/"）→ 去掉末尾 "/"（根目录除外）；不像绝对路径 → None。"""
    lines = [x for x in (out or "").splitlines() if x.strip()]
    if not lines:
        return None
    p = lines[-1].rstrip("\r")
    if not p.startswith("/") or "\x00" in p or len(p) > MAX_PATH:
        return None
    while len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def choose_folder(default=None, *, cfg: Optional[dict] = None, run=None, timeout: float = TIMEOUT) -> dict:
    """弹出系统"选择文件夹"对话框（阻塞直到用户选择 / 取消 / 超时）。返回 {"path"} 或 {"cancelled": True}。"""
    run = run or subprocess.run
    if not available(cfg):
        raise Unavailable("只有本机（macOS，非演示模式）才能打开系统的选择文件夹对话框")
    if not _busy.acquire(blocking=False):
        raise Busy("已经打开了一个选择文件夹对话框，请先在那个对话框里选择或取消")
    try:
        argv = build_argv(default_dir(default))
        try:
            r = run(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=timeout, text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise DialogError("选择文件夹超时（%d 分钟没有选择），已关闭对话框" % int(timeout // 60), timeout=True)
        except OSError as e:
            raise DialogError("无法打开选择文件夹对话框：%s" % (e.strerror or e))
        if r.returncode != 0:
            if "-128" in (r.stderr or ""):   # User canceled.
                return {"cancelled": True}
            tail = (r.stderr or "").strip().splitlines()[-1:] or ["未知错误"]
            raise DialogError("选择文件夹失败：%s" % tail[0][:300])
        p = normalize_result(r.stdout)
        if p is None:
            raise DialogError("选择文件夹失败：没有拿到目录路径")
        return {"path": p}
    finally:
        _busy.release()
