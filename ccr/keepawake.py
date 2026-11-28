"""防休眠（SPEC §12.2）：有会话「在干活」时不让 Mac 空闲睡眠；全部空闲 IDLE_GRACE 秒后放开。

- 「在干活」与侧栏一致（SPEC §15）：meta.working = 主会话 busy，或主会话已回复完但子代理还在跑；等审批 / 等回答
  （waiting / trust）、启动中、已退出、出错、连接断开（link down / ended，侧栏显示「连接断开」/「作业已结束」）都不算。
  本机会话与服务器会话都算（服务器会话在跑时 Mac 睡着了，ssh 会断）。
- 做法：本地后端运行 `/usr/bin/caffeinate -i -w <后端 pid>`（argv、不经 shell、新会话、stdin/stdout/stderr 都是 /dev/null）。
  -i = 阻止**空闲**系统睡眠（合上盖子、手动睡眠照样会睡；显示器照常熄灭）；-w = 后端退出（哪怕被 kill -9）时 caffeinate
  自己结束。放开时结束该进程；后端正常退出时 stop() 一并结束。
- 设置：config.json 的 keep_awake（默认 True）。关掉立即放开（server 的 POST /api/config 会 poke()）。
- 不碰 Claude Code 的任何东西：只读 SessionManager.list_sessions() 的 meta；不发按键、不改配置、不注入环境变量。
- 测试绝不真的运行 caffeinate：exe / popen / clock 都可注入；dry_run（app.py --fake 时默认）= 状态机照常、只记日志不起进程。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable, Iterable, Optional

log = logging.getLogger("ccr.keepawake")

CAFFEINATE = "/usr/bin/caffeinate"
IDLE_GRACE = 60.0       # 全部空闲这么多秒后放开
POLL_INTERVAL = 5.0     # 检查间隔（空闲睡眠以分钟计，几秒的延迟无所谓；设置变化 / 退出时 poke 立即处理）
RESPAWN_GAP = 30.0      # caffeinate 意外退出后，最快这么久重新起一次（防止反复失败时空转）
STOP_WAIT = 2.0         # 结束 caffeinate：SIGTERM 后最多等这么久，再 SIGKILL

WORK_STATES = ("busy", "idle")         # meta.working 只会在这两种状态下为 True（idle = 子代理还在跑）
DOWN_LINKS = ("down", "ended")         # 侧栏显示「连接断开」/「作业已结束」：不算在干活


def is_working(meta) -> bool:
    """这个会话是否算「在干活」（与侧栏一致，见模块说明）。"""
    if not isinstance(meta, dict) or meta.get("working") is not True:
        return False
    if meta.get("state") not in WORK_STATES:
        return False
    return meta.get("link") not in DOWN_LINKS


def working_count(metas: Iterable) -> int:
    return sum(1 for m in metas or () if is_working(m))


def enabled_in(cfg) -> bool:
    return not (isinstance(cfg, dict) and cfg.get("keep_awake") is False)


class KeepAwake:
    """后台线程每 interval 秒 tick() 一次（poke() 立即 tick）。状态都在 _lock 里；list_sessions 在锁外调用。"""

    def __init__(self, get_config: Callable[[], dict], list_sessions: Callable[[], list], *,
                 exe: str = CAFFEINATE, popen: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 pid: Optional[int] = None, dry_run: bool = False, grace: float = IDLE_GRACE,
                 interval: float = POLL_INTERVAL, respawn_gap: float = RESPAWN_GAP):
        self._get_config = get_config
        self._list = list_sessions
        self.exe = exe
        self._popen = popen or subprocess.Popen
        self._clock = clock
        self.pid = int(pid if pid is not None else os.getpid())
        self.dry_run = bool(dry_run)
        self.grace = float(grace)
        self.interval = float(interval)
        self.respawn_gap = float(respawn_gap)
        self._lock = threading.Lock()
        self._proc = None              # 正在运行的 caffeinate（dry_run 时为 None）
        self._active = False           # 当前是否在阻止睡眠（dry_run 时也会变 True）
        self._since: Optional[float] = None        # 开始阻止的时刻
        self._idle_since: Optional[float] = None   # 阻止期间全部空闲的起点
        self._fail_at: Optional[float] = None      # 上次没起来 / 意外退出的时刻（respawn_gap 内不再试）
        self._working = 0
        self._enabled = True
        self._warned_missing = False
        self.spawns = 0                # 起过几次进程（测试 / 调试）
        self.history: list = []        # 最近的 start / stop 记录（最多 50 条）
        self._wake = threading.Event()
        self._stopping = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> "KeepAwake":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="ccr-keepawake", daemon=True)
            self._thread.start()
        return self

    def poke(self) -> None:
        """设置变了（或想立即重新判断）：让后台线程马上 tick 一次。没有后台线程时直接 tick。"""
        if self._thread is None:
            self.tick()
        else:
            self._wake.set()

    def stop(self) -> None:
        """后端退出：停后台线程并结束 caffeinate。"""
        self._stopping = True
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(STOP_WAIT + 1.0)
        with self._lock:
            self._release_locked("后端退出")

    def _loop(self) -> None:
        while not self._stopping:
            try:
                self.tick()
            except Exception:  # noqa: BLE001  防休眠出错不能影响后端
                log.exception("防休眠检查出错")
            self._wake.wait(self.interval)
            self._wake.clear()

    # ------------------------------------------------------------ 判断
    def available(self) -> bool:
        return self.dry_run or os.access(self.exe, os.X_OK)

    def tick(self) -> None:
        if self._stopping:
            return
        try:
            cfg = dict(self._get_config() or {})
        except Exception:  # noqa: BLE001
            cfg = {}
        enabled = enabled_in(cfg)
        n = 0
        if enabled:
            try:
                n = working_count(self._list())
            except Exception:  # noqa: BLE001
                log.exception("防休眠：读取会话状态失败")
                n = 0
        with self._lock:
            if self._stopping:
                return
            self._enabled = enabled
            self._working = n
            now = self._clock()
            if not enabled:
                self._idle_since = None
                self._release_locked("设置已关闭")
                return
            if n > 0:
                self._idle_since = None
                self._hold_locked(now)
                return
            if not self._active:
                return
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since >= self.grace:
                self._release_locked("全部空闲 %d 秒" % int(self.grace))

    def _hold_locked(self, now: float) -> None:
        if self._active:
            p = self._proc
            if p is None or p.poll() is None:
                return   # 已在阻止（反复切换不重复起进程）
            # caffeinate 意外退出（例如被手动结束）：当作一次失败，隔 respawn_gap 秒再起
            log.warning("防休眠：caffeinate 意外退出（返回码 %s）", p.poll())
            self._proc = None
            self._active = False
            self._since = None
            self._fail_at = now
            self._note("died", code=p.poll())
        if self._fail_at is not None and now - self._fail_at < self.respawn_gap:
            return
        if self.dry_run:
            self._active = True
            self._since = now
            self._note("start", dry_run=True)
            log.info("防休眠（演示模式，不运行 caffeinate）：开始（%d 个会话在干活）", self._working)
            return
        if not os.access(self.exe, os.X_OK):
            if not self._warned_missing:
                self._warned_missing = True
                log.warning("防休眠：找不到 %s，无法阻止空闲睡眠", self.exe)
            self._fail_at = now
            self._note("fail", reason="missing")
            return
        argv = [self.exe, "-i", "-w", str(self.pid)]
        try:
            p = self._popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except OSError as e:
            log.warning("防休眠：无法运行 %s：%s", self.exe, e)
            self._fail_at = now
            self._note("fail", reason=str(e)[:200])
            return
        self.spawns += 1
        self._fail_at = None
        self._proc = p
        self._active = True
        self._since = now
        self._note("start", pid=getattr(p, "pid", None))
        log.info("防休眠：开始（%d 个会话在干活；caffeinate pid %s）", self._working, getattr(p, "pid", "?"))

    def _release_locked(self, why: str) -> None:
        if not self._active and self._proc is None:
            return
        p, self._proc = self._proc, None
        self._active = False
        self._since = None
        self._idle_since = None
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=STOP_WAIT)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=STOP_WAIT)
            except Exception:  # noqa: BLE001
                log.exception("防休眠：结束 caffeinate 出错")
        self._note("stop", reason=why)
        log.info("防休眠：已放开（%s）", why)

    def _note(self, what: str, **kw) -> None:
        self.history.append(dict(kw, what=what, t=self._clock()))
        del self.history[:-50]

    # ------------------------------------------------------------ 状态（GET /api/keepawake）
    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            release_in = None
            if self._active and self._idle_since is not None:
                release_in = max(0.0, round(self.grace - (now - self._idle_since), 1))
            return {
                "enabled": self._enabled,
                "active": self._active,
                "working": self._working,
                "since": None if self._since is None else round(now - self._since, 1),   # 已经阻止了多少秒
                "release_in": release_in,      # 全部空闲后再过多少秒放开（None = 不在倒计时）
                "grace": self.grace,
                "dry_run": self.dry_run,
                "available": self.available(),
            }
