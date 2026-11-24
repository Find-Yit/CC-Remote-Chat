"""提示音：回合完成、或需要用户操作（权限审批 / AskUserQuestion / 计划审批 / 信任目录…）时，在这台 Mac 上播放
与 Claude Code CLI 的 Stop hook 相同的系统声音（默认 `/usr/bin/afplay /System/Library/Sounds/Blow.aiff`）。

- 由本地后端执行 afplay（argv 方式、不经 shell、后台进程、不阻塞）；浏览器不播放（播不了 aiff）。
- 只被动观察会话状态（SessionManager 在状态 / 对话框变化时调用 observe），不发任何按键、不改 Claude 的任何配置；
  本机 ~/.claude/settings.json 只做**只读**检查（本机会话的 Stop hook 是否已经会 afplay，避免重复响）。
- 设置（app 的 config.json）：notify_enabled（总开关）、notify_on_done（完成时）、notify_on_attention（需要操作时）、
  notify_sound（/System/Library/Sounds 下真实存在的 .aiff 文件名，不含扩展名）。运行时覆盖项 notify_dry_run
  （app.py --no-sound）：只记日志、不真正播放（开发 / 测试用）。

触发（每个会话按状态变化判断，见 Notifier.observe）：
- 完成：busy → idle，且这一轮在基线之后确实看到过 busy（不是启动 / 重连恢复）；用户中断（Esc、取消对话框、
  终端面板 Esc / Ctrl+C）或屏幕显示 Interrupted 的不响。（"立即发送排队消息"不压制：被打断的一轮直接进入新一轮，
  中间没有 idle，压制会吞掉新一轮的完成提示。）
- 需要操作：从非 waiting 进入 waiting / trust（对话框种类不是 /model、/effort 面板）；同一个对话框（指纹相同、
  期间用户没有操作过）已经提醒过就不再响。"已提醒"只在真正播放（或因设置关闭而跳过）时记录，候选阶段不记。
- 去抖：完成 = CONFIRM_DELAY 秒内状态一次都没变才播放（马上又忙起来不响）；需要操作 = CONFIRM_DELAY 秒后**此刻**
  仍在同一个对话框（指纹相同）就播放，中间短暂闪成 busy 再回来不影响（真机实测会闪）；全局 GLOBAL_GAP 秒内最多
  响一次；reset()（启动 / 重启 / 重新附着时）之后的首次状态同步、以及 QUIET_AFTER_RESET 秒内的状态都只当基线、不响。
- 回调（add_listener）：完成确认后（不论最终响不响）在锁外回调 fn(sid, "done")，SessionManager 据此记 done_at
  （侧栏"新回复"），保证"响一声"与"变红"出自同一个判定。"需要操作"不回调。
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from typing import Callable, Dict, Optional

log = logging.getLogger("ccr.notify")

SOUND_DIR = "/System/Library/Sounds"
AFPLAY = "/usr/bin/afplay"
DEFAULT_SOUND = "Blow"
SOUND_EXT = ".aiff"
_SOUND_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}\Z")
EVENTS = ("done", "attention")
PANEL_KINDS = ("model", "effort")          # 本程序（或用户自己）打开的 /model、/effort 面板：不算"需要操作"
WAIT_STATES = ("waiting", "trust")
BASELINE_STATES = (None, "starting", "exited", "error")
LOCAL_JOBID = "local"                       # 本机会话（另一分支的本机模式；这里只预留判断）
CLAUDE_SETTINGS = "~/.claude/settings.json"
MAX_SETTINGS_BYTES = 1024 * 1024


# ============================================================================ 声音文件
def list_sounds(sound_dir: Optional[str] = None) -> list:
    """sound_dir（默认 /System/Library/Sounds）下真实存在的 *.aiff 普通文件名（不含扩展名，不跟随软链接），排序。"""
    d = sound_dir or SOUND_DIR
    out = []
    try:
        it = os.scandir(d)
    except OSError:
        return out
    with it:
        for e in it:
            if not e.name.endswith(SOUND_EXT):
                continue
            name = e.name[:-len(SOUND_EXT)]
            if not _SOUND_NAME_RE.match(name):
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            out.append(name)
    out.sort(key=lambda x: (x.lower(), x))
    return out


def validate_sound(name, sound_dir: Optional[str] = None) -> str:
    """只接受 sound_dir 下真实存在的 <name>.aiff（普通文件）；否则 ValueError（中文）。"""
    if not isinstance(name, str) or not _SOUND_NAME_RE.match(name):
        raise ValueError("提示音名称不合法")
    try:
        st = os.lstat(os.path.join(sound_dir or SOUND_DIR, name + SOUND_EXT))
    except OSError:
        raise ValueError("没有这个系统提示音：%s" % name) from None
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("没有这个系统提示音：%s" % name)
    return name


def sound_path(name, sound_dir: Optional[str] = None) -> str:
    return os.path.join(sound_dir or SOUND_DIR, validate_sound(name, sound_dir) + SOUND_EXT)


def play_file(path: str) -> bool:
    """后台运行 `/usr/bin/afplay <path>`（argv、不经 shell、新会话、不阻塞），后台线程回收进程。"""
    try:
        p = subprocess.Popen([AFPLAY, path], shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except OSError as e:
        log.warning("无法播放提示音（%s）：%s", AFPLAY, e)
        return False
    threading.Thread(target=p.wait, name="ccr-afplay", daemon=True).start()
    return True


# ============================================================================ 本机 Stop hook（只读检查）
_hook_cache: Dict[str, tuple] = {}
_hook_lock = threading.Lock()


def _commands_of(hooks_entry) -> list:
    out = []
    for group in hooks_entry if isinstance(hooks_entry, list) else []:
        for h in (group.get("hooks") if isinstance(group, dict) else None) or []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                out.append(h["command"])
    return out


def local_stop_hook_has_afplay(path: Optional[str] = None) -> bool:
    """本机 ~/.claude/settings.json 的 hooks.Stop 里是否已经有 afplay（CLI 自己会在完成时响）。
    **只读**；按 (mtime, size) 缓存；文件不存在 / 读不了 / 格式不对 → False。不记录文件内容。"""
    p = os.path.expanduser(path or CLAUDE_SETTINGS)
    try:
        st = os.stat(p)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    with _hook_lock:
        hit = _hook_cache.get(p)
        if hit is not None and hit[0] == key:
            return hit[1]
    res = False
    if st.st_size <= MAX_SETTINGS_BYTES:
        try:
            with open(p, "rb") as f:
                d = json.loads(f.read(MAX_SETTINGS_BYTES + 1).decode("utf-8", "replace"))
            hooks = d.get("hooks") if isinstance(d, dict) else None
            stop = hooks.get("Stop") if isinstance(hooks, dict) else None
            res = any("afplay" in c for c in _commands_of(stop))
        except (OSError, ValueError):
            res = False
    with _hook_lock:
        _hook_cache[p] = (key, res)
    return res


# ============================================================================ 状态机
def _timer(delay: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


class _SessNotify:
    __slots__ = ("prev", "sig", "gen", "saw_busy", "suppress", "acted", "last_wait_sig", "quiet_until")

    def __init__(self):
        self.prev = None            # 上一次观察到的状态；None = 需要基线
        self.sig = None             # 当前对话框指纹
        self.gen = 0                # 状态每变一次 +1：候选事件确认时据此判断"期间状态没变"
        self.saw_busy = False       # 基线之后本轮确实看到过 busy
        self.suppress = False       # 用户中断：本轮结束不响
        self.acted = True           # 上次"需要操作"提示之后用户操作过对话框（同样的对话框再出现算新的一次）
        self.last_wait_sig = None
        self.quiet_until = 0.0


class Notifier:
    GLOBAL_GAP = 1.5          # 全局：这么多秒内最多响一次
    CONFIRM_DELAY = 0.8       # 候选事件确认：状态保持这么久才响（屏幕抖动不响）
    QUIET_AFTER_RESET = 2.0   # reset 之后这么多秒内只更新基线
    TEST_GAP = 0.5            # 试听按钮的最小间隔

    def __init__(self, get_config: Callable[[], dict], *, play: Optional[Callable[[str], bool]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 schedule: Optional[Callable[[float, Callable[[], None]], None]] = None,
                 local_hook: Optional[Callable[[], bool]] = None, sound_dir: Optional[str] = None):
        self._get_config = get_config
        self._play = play or play_file
        self._clock = clock
        self._schedule = schedule or _timer
        self._local_hook = local_hook or local_stop_hook_has_afplay
        self._sound_dir = sound_dir
        self._lock = threading.Lock()
        self._st: Dict[str, _SessNotify] = {}
        self._last_play: Optional[float] = None
        self._last_test: Optional[float] = None
        self._listeners: list = []   # on_event(sid, kind)：本轮完成确认后回调（侧栏"新回复"，见 add_listener）
        self.history: list = []   # 最近的播放 / 跳过记录（调试与测试用，最多 50 条）

    # ------------------------------------------------------------ 事件回调
    def add_listener(self, fn: Callable[[str, str], None]) -> None:
        """注册 fn(sid, kind)。kind 目前只有 "done"：本轮完成**确认**（通过 muted / 状态没变两步检查）时调用，
        与提示音同一个触发点；声音被设置关掉、被全局间隔合并、或本机由 CLI 自己的 Stop hook 去响时照样调用。
        在 Notifier._lock 之外调用（定时器线程里），回调里可以再拿 SessionManager / 会话的锁。"""
        self._listeners.append(fn)

    def _emit(self, sid: str, kind: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(sid, kind)
            except Exception:  # noqa: BLE001  回调出错不能影响提示音
                log.exception("提示音事件回调出错")

    # ------------------------------------------------------------ 设置
    def settings(self) -> dict:
        try:
            cfg = dict(self._get_config() or {})
        except Exception:  # noqa: BLE001
            cfg = {}
        return {"enabled": cfg.get("notify_enabled", True) is not False,
                "on_done": cfg.get("notify_on_done", True) is not False,
                "on_attention": cfg.get("notify_on_attention", True) is not False,
                "sound": cfg.get("notify_sound") or DEFAULT_SOUND,
                "dry_run": bool(cfg.get("notify_dry_run"))}

    def _note(self, what: str, **kw) -> None:
        rec = dict(kw, what=what, t=self._clock())
        self.history.append(rec)
        del self.history[:-50]

    # ------------------------------------------------------------ 会话生命周期（SessionManager 调用）
    def reset(self, sid: str) -> None:
        """启动 / 重启 / 重新附着（watch on）：下一次观察只作基线，QUIET_AFTER_RESET 秒内的变化也不响。"""
        with self._lock:
            st = self._st.setdefault(sid, _SessNotify())
            st.prev = None
            st.gen += 1
            st.saw_busy = False
            st.suppress = False
            st.quiet_until = self._clock() + self.QUIET_AFTER_RESET

    def forget(self, sid: str) -> None:
        with self._lock:
            self._st.pop(sid, None)

    def mute(self, sid: str, seconds: float = 3600.0) -> None:
        """用户主动关闭/重启会话：之后的状态变化（例如退出时 CLI 弹出的"后台 shell 仍在运行"确认框）一律不响；
        已排队的候选也作废。重新启动时 reset() 会恢复。"""
        with self._lock:
            st = self._st.setdefault(sid, _SessNotify())
            st.gen += 1
            st.quiet_until = self._clock() + seconds

    def suppress_done(self, sid: str) -> None:
        """用户中断（Esc / 取消对话框 / 立即发送排队消息…）：这一轮结束时不响。"""
        with self._lock:
            st = self._st.setdefault(sid, _SessNotify())
            st.suppress = True
            st.gen += 1   # 已排队待确认的"完成"也作废

    def user_acted(self, sid: str) -> None:
        """用户操作了对话框（选项 / 按键）：之后同样的对话框再出现算新的一次。"""
        with self._lock:
            self._st.setdefault(sid, _SessNotify()).acted = True

    # ------------------------------------------------------------ 观察
    def observe(self, sid: str, jobid: str, state: str, dialog_kind: Optional[str] = None, dialog_sig=None,
                interrupted: bool = False) -> None:
        """会话状态（或对话框）变化时调用。快速返回：只更新内存并可能排一个确认定时器。"""
        fire = None
        with self._lock:
            st = self._st.setdefault(sid, _SessNotify())
            prev = st.prev
            now = self._clock()
            if prev in BASELINE_STATES or now < st.quiet_until:
                if state != prev:
                    st.gen += 1
                st.prev, st.sig, st.saw_busy, st.suppress = state, dialog_sig, False, False
                if state in WAIT_STATES:
                    st.last_wait_sig, st.acted = dialog_sig, False   # 基线里已有的对话框：之后不再为它响
                return
            if state != prev:
                st.gen += 1
            st.prev = state
            if state == "busy":
                st.saw_busy = True
            if prev == "busy" and state == "idle":
                if st.saw_busy and not st.suppress and not interrupted:
                    fire = ("done", st.gen)
                st.saw_busy = False
                st.suppress = False
            elif state in ("idle", "exited", "error", "starting"):
                st.suppress = False
            if state in WAIT_STATES:
                entered = prev not in WAIT_STATES or (dialog_sig != st.sig and st.acted)
                # 候选阶段**不**记录"已提醒"（last_wait_sig / acted）：只在真正播放或因设置关闭而跳过时记录，
                # 否则候选被闪烁作废后，再回到同一个对话框会被当成"已提醒过"而永远不响
                if entered and dialog_kind not in PANEL_KINDS and not (dialog_sig == st.last_wait_sig
                                                                       and not st.acted):
                    fire = ("attention", st.gen)
            st.sig = dialog_sig
        if fire is not None:
            event, gen = fire
            self._schedule(self.CONFIRM_DELAY, lambda: self._confirm(sid, jobid, event, gen, dialog_sig))

    def _confirm(self, sid: str, jobid: str, event: str, gen: int, sig=None) -> None:
        """确认候选事件并（可能）播放。判断与记录都在同一把锁里，多个候选并发确认时也只会响一次。
        "完成"通过 muted / 状态没变两步检查即为本轮完成确认：在锁外回调 listeners（侧栏"新回复"），
        之后的 disabled / local_stop_hook / global_gap 只决定响不响。"""
        cfg = self.settings()
        dup_local = False
        if event == "done" and jobid == LOCAL_JOBID:
            try:
                dup_local = bool(self._local_hook())   # 只读检查（结果按 mtime 缓存）
            except Exception:  # noqa: BLE001
                dup_local = False
        done = play = False
        with self._lock:
            st = self._st.get(sid)
            if st is None:
                return
            if self._clock() < st.quiet_until:   # 已静音（用户关闭/重启会话）或刚 reset
                self._note("skip", sid=sid, event=event, reason="muted")
                return
            if event == "done":
                if st.gen != gen:   # 确认延迟内状态变过（例如马上又忙起来）
                    self._note("skip", sid=sid, event=event, reason="state_changed")
                    return
                done = True
            else:
                # 此刻仍停在同一个对话框即可（中间闪成 busy 再回来不影响）
                if st.prev not in WAIT_STATES or st.sig != sig:
                    self._note("skip", sid=sid, event=event, reason="state_changed")
                    return
                if sig == st.last_wait_sig and not st.acted:
                    self._note("skip", sid=sid, event=event, reason="duplicate")
                    return
            now = self._clock()
            if not cfg["enabled"] or not cfg["on_" + event]:
                if event == "attention":
                    st.last_wait_sig, st.acted = sig, False
                self._note("skip", sid=sid, event=event, reason="disabled")
            elif dup_local:   # 本机 CLI 自己的 Stop hook 已经会 afplay：不重复响（"需要操作时"照常）
                self._note("skip", sid=sid, event=event, reason="local_stop_hook")
            elif self._last_play is not None and now - self._last_play < self.GLOBAL_GAP:
                self._note("skip", sid=sid, event=event, reason="global_gap")
            else:
                play = True
                self._last_play = now
                if event == "attention":
                    st.last_wait_sig, st.acted = sig, False
        if done:
            self._emit(sid, "done")   # 锁外：回调里会拿会话锁（锁顺序 s.lock → Notifier._lock，反过来会死锁）
        if play:
            self._do_play(cfg, sid=sid, event=event)

    def _do_play(self, cfg: dict, **kw) -> Optional[str]:
        try:
            path = sound_path(cfg["sound"], self._sound_dir)
        except ValueError:
            try:
                path = sound_path(DEFAULT_SOUND, self._sound_dir)
            except ValueError:
                self._note("skip", reason="no_sound", **kw)
                return None
        if cfg["dry_run"]:
            log.info("提示音（不播放）：%s %s", kw.get("event") or "test", os.path.basename(path))
            self._note("dry_run", path=path, **kw)
            return path
        self._note("play", path=path, **kw)
        self._play(path)
        return path

    def test(self, sound: Optional[str] = None) -> str:
        """「试听」：立即播放一次（不看开关；sound 不给 = 当前设置）。返回播放的文件名（不含目录）。"""
        cfg = self.settings()
        if sound is not None:
            cfg["sound"] = validate_sound(sound, self._sound_dir)
        with self._lock:
            now = self._clock()
            if self._last_test is not None and now - self._last_test < self.TEST_GAP:
                raise ValueError("点得太快了，请稍后再试听")
            self._last_test = now
        path = self._do_play(cfg, event="test")
        if path is None:
            raise ValueError("找不到可播放的系统提示音")
        return os.path.basename(path)


class NullNotifier(Notifier):
    """不发声（SessionManager 未传入 notifier 时：测试等）。状态机与回调（侧栏"新回复"）照常工作，
    只是永远不播放：设置视为关闭、播放函数为空操作；没有「试听」。"""
    test = None   # server 的 /api/notify 据此报告 available=False

    def __init__(self):
        super().__init__(lambda: {"notify_enabled": False}, play=lambda _p: False, local_hook=lambda: False)
