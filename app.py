#!/usr/bin/env python3
"""cc-remote-chat 入口。

用法：python3 app.py [--port 8765] [--fake] [--dev] [--no-browser] [--data-dir DIR] [--jobid J] [--stop]

- 只监听 127.0.0.1；启动后用 Chrome 应用窗口打开 http://127.0.0.1:PORT/#code=<一次性登录码>
  （码 120 秒内有效、只能兑换一次；放在 fragment 里，不会出现在请求行/日志中）。
- 原生 Mac 应用（tools/macapp，SPEC §14）：它先创建 <数据目录>/window_request 再运行本程序；开窗时若该文件存在
  （60 秒内创建的普通文件），就删掉它、把登录链接原子写入 <数据目录>/window_login_url（0600），**不再**打开 Chrome，
  由原生窗口读走后加载。新启动与 SIGUSR1 重新开窗两条路径相同。
- 单实例：已有实例在跑时，再次启动只会让旧实例生成新登录码并重新打开窗口（SIGUSR1）。
  `--jobid J`：校验后写入 config 的"当前作业"；已有实例时写 config 后通知它重新加载并推送给页面
  （同时带 --no-browser 时只重新加载、不开新窗口：SIGUSR2）。
- `--bundled`：内置模式（dmg 里的 .app，SPEC §14.10）：本机会话用 .app 自带的 tmux（<Resources>/bin/tmux）、
  tmux 标签 ccr-app；数据目录默认 ~/.cc-remote-chat-app（原生壳总会用 --data-dir 明确传入）。不带时行为与以前完全相同。
  升级：app.py 旁边的 BUILD 文件（tools/make_dmg.sh 写入）是构建标识，实例启动时写进 app.pid；已有实例的标识与自己的
  不同（含旧版根本没有标识）→ 先 SIGTERM 旧后台、最多等 15 秒拿到实例锁，再由本进程接管（tmux 里的 claude 不受影响）。
- `--dev`：启用 /fixtures/*（供 static/dev/fixture.html 回放自测），默认关闭。
- Ctrl-C / SIGTERM / SIGHUP：只断开与计算节点的连接后退出；远端 tmux 里的交互式 claude **不会**被关闭
  （keeper 保活），下次启动自动重新附着。要结束某个会话请在界面上点"关闭"。
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import logging.handlers
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ccr import config as ccr_config  # noqa: E402

try:  # 手机远程访问：私有版才有这个模块（export-ignore，开源版里没有 → 整个功能不存在）
    from ccr import remote_access as ccr_remote_access  # noqa: E402
except ImportError:  # pragma: no cover
    ccr_remote_access = None

log = logging.getLogger("ccr.app")


def open_browser(url: str) -> None:
    """优先 Chrome 应用窗口模式，失败则 `open URL`。不经过 shell。"""
    try:
        r = subprocess.run(["open", "-na", "Google Chrome", "--args", "--app=" + url],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if r.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        subprocess.run(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        print("请在浏览器中打开：%s" % url, file=sys.stderr)


# 原生 Mac 应用（tools/macapp/main.swift，SPEC §14）取登录链接用的两个文件（都在数据目录里，目录 0700）
WINDOW_REQUEST = "window_request"        # 原生应用创建：表示"有窗口在等登录链接"
WINDOW_LOGIN_URL = "window_login_url"    # 本程序原子写入登录链接（0600），原生应用读走后删除
WINDOW_REQUEST_MAX_AGE = 60.0            # 更早创建的请求视为残留（原生应用最多等 25 秒）


def take_window_request(data_dir: pathlib.Path, info: dict = None) -> bool:
    """原生应用是否在等登录链接：<data_dir>/window_request 存在且是本人的普通文件、60 秒内创建 → 删除并返回 True。
    残留的（过期 / 软链接等）同样删除，但返回 False（按原行为开窗），避免旧请求劫持之后在终端里手动运行的实例。
    info：给了 dict 时，把请求文件的内容（原生壳写的是它自己的 pid）解析到 info["pid"]（dmg 版自动更新用来找壳）。"""
    p = pathlib.Path(data_dir) / WINDOW_REQUEST
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if info is not None and stat.S_ISREG(st.st_mode):
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                raw = os.read(fd, 32)
            finally:
                os.close(fd)
            txt = raw.decode("ascii").strip()
            if txt.isdigit() and 1 < int(txt) < 1 << 31:
                info["pid"] = int(txt)
        except (OSError, ValueError):
            pass
    try:
        os.unlink(p)   # 软链接只删链接本身
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("无法删除 %s：%s", WINDOW_REQUEST, e)
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        log.warning("忽略不合法的 %s（不是本人的普通文件）", WINDOW_REQUEST)
        return False
    age = time.time() - st.st_mtime
    if not -5.0 <= age <= WINDOW_REQUEST_MAX_AGE:
        log.info("忽略残留的 %s（%.0f 秒前创建）", WINDOW_REQUEST, age)
        return False
    return True


def write_window_login_url(data_dir: pathlib.Path, url: str) -> None:
    """把登录链接原子写入 <data_dir>/window_login_url（0600）：先写同目录临时文件，再 os.replace（不会读到半个文件；
    目标若是软链接，替换的是链接本身，不会写到链接指向的地方）。"""
    data_dir = pathlib.Path(data_dir)
    fd, tmp = tempfile.mkstemp(prefix="." + WINDOW_LOGIN_URL + ".", suffix=".tmp", dir=str(data_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(url + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, data_dir / WINDOW_LOGIN_URL)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_window(server, data_dir: pathlib.Path, no_browser: bool, opener=None) -> str:
    """每次开窗都生成新的一次性登录码。返回去向：
    "native"（原生应用在等：写入 window_login_url，不开 Chrome）/ "printed"（--no-browser：打印）/ "browser" /
    "error"（原生应用在等但写文件失败：只记日志，原生窗口会超时并显示错误页）。"""
    from ccr.server import LOGIN_CODE_TTL
    url = server.new_login_url()
    info = {}
    if take_window_request(data_dir, info):
        upd = getattr(server, "updater", None)
        if upd is not None and info.get("pid"):
            upd.note_shell_pid(info["pid"])
        try:
            write_window_login_url(data_dir, url)
        except OSError as e:
            log.error("无法写入 %s：%s", WINDOW_LOGIN_URL, e)
            return "error"
        log.info("已把登录链接交给原生应用窗口")   # 不记录链接本身（含登录码）
        return "native"
    if no_browser:
        print("登录链接（%d 秒内有效、只能使用一次）：%s" % (int(LOGIN_CODE_TTL), url), flush=True)
        return "printed"
    threading.Thread(target=opener or open_browser, args=(url,), daemon=True).start()
    return "browser"


def setup_logging(data_dir: pathlib.Path, verbose: bool) -> None:
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    os.chmod(logs, 0o700)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(logs / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


class InstanceLock:
    """~/.cc-remote-chat/app.lock（flock）+ app.pid，保证单实例。
    build：内置模式的构建标识（见 read_build_id）；有时 app.pid 写 {"pid": N, "build": "…"}，没有时与以前一样只写 {"pid": N}。"""

    def __init__(self, data_dir: pathlib.Path, build=None):
        self.lock_path = data_dir / "app.lock"
        self.pid_path = data_dir / "app.pid"
        self.build = build
        self.fd = None

    def acquire(self) -> bool:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            raise
        self.fd = fd
        info = {"pid": os.getpid()}
        if self.build:
            info["build"] = self.build
        ccr_config.atomic_write_json(self.pid_path, info)
        return True

    def other_pid(self):
        try:
            import json
            with open(self.pid_path, "r", encoding="utf-8") as f:
                pid = int(json.load(f).get("pid"))
            return pid if pid > 1 else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def other_build(self):
        """正在运行的实例写在 app.pid 里的构建标识；没有（例如 2.9 及更早的版本、从源码运行）→ None。"""
        try:
            import json
            with open(self.pid_path, "r", encoding="utf-8") as f:
                b = json.load(f).get("build")
            return b if isinstance(b, str) and b else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def release(self):
        if self.fd is not None:
            try:
                os.unlink(self.pid_path)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


BUNDLED_TMUX_LABEL = "ccr-app"
BUNDLED_DATA_DIR = "~/.cc-remote-chat-app"


def bundled_overrides(app_root: pathlib.Path = PROJECT_ROOT) -> dict:
    """内置模式的运行时配置（不写盘）。.app 的布局：Contents/Resources/{app/app.py, bin/tmux, python/}，
    所以 tmux = <app.py 所在目录>/../bin/tmux。"""
    return {"local_tmux": str(pathlib.Path(app_root).parent / "bin" / "tmux"),
            "tmux_label": BUNDLED_TMUX_LABEL}


BUILD_FILE = "BUILD"          # tools/make_dmg.sh 写在 <Resources>/app/BUILD：版本 + 构建时间 + 代码哈希（同版本重新打包也不同）
TAKEOVER_TIMEOUT = 15.0       # 内置模式升级：等旧后台退出（释放实例锁）的最长时间，与原生壳的 stopStale 相同


def read_build_id(app_root: pathlib.Path = PROJECT_ROOT):
    """内置模式的构建标识：<app.py 所在目录>/BUILD 的第一行（可打印 ASCII，最长 200 字）；没有 / 不合法 → None。"""
    try:
        with open(pathlib.Path(app_root) / BUILD_FILE, "r", encoding="ascii") as f:
            b = f.readline(512).strip()
    except (OSError, ValueError):
        return None
    if not b or len(b) > 200 or not all(" " <= c <= "~" for c in b):
        return None
    return b


def replace_running_instance(lock: "InstanceLock", pid: int, timeout: float = None) -> str:
    """内置模式升级：SIGTERM 正在运行的旧后台，最多等 timeout 秒拿到实例锁（旧实例退出时释放）。
    tmux 里的 claude 不受影响（与 tools/deploy.sh 重启后台同理）；超时**不**强杀。返回 ""（已拿到锁）或错误说明。"""
    if timeout is None:
        timeout = TAKEOVER_TIMEOUT
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass   # 刚好自己退出了：照样等锁
    except OSError as e:
        return "无法停止旧版本的后台（pid %d）：%s" % (pid, e)
    deadline = time.monotonic() + timeout
    while True:
        if lock.acquire():
            return ""
        if time.monotonic() >= deadline:
            return ("旧版本的后台（pid %d）%d 秒内没有退出；没有强行结束它（tmux 里的 claude 不受影响）。"
                    "请稍后重试" % (pid, int(timeout)))
        time.sleep(0.2)


def default_data_dir(bundled: bool) -> str:
    """没有 --data-dir 时的数据目录：平时 ~/.cc-remote-chat（与以前相同）；内置模式 ~/.cc-remote-chat-app。"""
    return BUNDLED_DATA_DIR if bundled else str(ccr_config.DEFAULT_DATA_DIR)


def parse_fake_delays(text: str) -> dict:
    """--fake-delays "probe=2,connect=6,keeper=3,boot=4,unreachable=777777+888888" → dict（非法项忽略）。"""
    out: dict = {}
    for part in str(text or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "unreachable":
            out[k] = [j for j in v.split("+") if j.isdigit()]
        elif k in ("probe", "connect", "keeper", "boot"):
            try:
                f = float(v)
            except ValueError:
                continue
            if 0 < f <= 600:
                out[k] = f
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cc-remote-chat：本地聊天界面驱动远端 Claude Code")
    ap.add_argument("--port", type=int, default=8765, help="监听端口（被占用则递增），默认 8765")
    ap.add_argument("--fake", action="store_true", help="使用本地假 claude（演示/测试，不连远端）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--data-dir", default=None,
                    help="数据目录（默认 ~/.cc-remote-chat；--bundled 时默认 ~/.cc-remote-chat-app）")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的实例")
    ap.add_argument("--jobid", default=None, help="设置当前作业 jobid（写入 config；已有实例时通知其重新加载）")
    ap.add_argument("--dev", action="store_true", help="开发模式：启用 /fixtures/*（自测回放页用）")
    ap.add_argument("--verbose", action="store_true", help="输出调试日志")
    ap.add_argument("--bundled", action="store_true",
                    help="内置模式（dmg 里的 .app 用）：用 .app 自带的 tmux，tmux 标签 ccr-app")
    ap.add_argument("--no-sound", action="store_true", help="提示音只记日志、不真正播放（开发/测试用）")
    # 测试钩子（不出现在 --help）：dmg 版自动更新的本机测试源，只在 --bundled --fake 下生效（ccr/updater.py create）
    ap.add_argument("--update-test", default=None, help=argparse.SUPPRESS)
    # 测试钩子（不出现在 --help，只在 --fake 下生效）：各启动阶段的人为延迟，用来观察重启/续接进度界面，
    # 例如 probe=2,connect=6,keeper=3,boot=4,unreachable=777777（boot = 假 claude 出第一帧前的等待；
    # unreachable = 这些 fake 作业"连不上"，多个用 + 分隔）
    ap.add_argument("--fake-delays", default=None, help=argparse.SUPPRESS)
    if ccr_remote_access is not None:
        ap.add_argument("--remote-pair", action="store_true",
                        help="手机远程访问：生成一个配对码并打印（10 分钟内有效，只能用一次）")
    args = ap.parse_args(argv)

    if args.data_dir is None:
        args.data_dir = default_data_dir(args.bundled)
    data_dir = pathlib.Path(os.path.expanduser(args.data_dir)).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    if getattr(args, "remote_pair", False):
        return ccr_remote_access.cli_pair(data_dir)

    cfg_path = data_dir / "config.json"
    if args.jobid is not None and not args.stop:
        try:
            ccr_config.validate_partial({"jobid": args.jobid})
        except (ValueError, ImportError) as e:
            print("jobid 不合法：%s" % e, file=sys.stderr)
            return 2

    # 构建标识只在内置模式读（从源码运行时 app.pid 仍只有 pid，逐字不变）
    lock = InstanceLock(data_dir, build=read_build_id() if args.bundled else None)
    replaced = None   # 内置模式升级：被本进程替换掉的旧后台 (pid, 它的构建标识)
    if not lock.acquire():
        pid = lock.other_pid()
        if pid is None:
            print("已有实例在运行，但无法确定其进程号", file=sys.stderr)
            return 1
        if args.jobid is not None and not args.stop:
            try:
                ccr_config.ConfigStore(cfg_path).update({"jobid": args.jobid})
            except (ValueError, OSError) as e:
                print("写入 config 失败：%s" % e, file=sys.stderr)
                return 1
        if args.stop:
            sig = signal.SIGTERM
        elif args.no_browser and args.jobid is not None:
            sig = signal.SIGUSR2  # 只重新加载配置
        else:
            sig = signal.SIGUSR1  # 重新加载配置 + 生成新登录码并打开窗口
        old_build = lock.other_build()
        if sig == signal.SIGUSR1 and lock.build and old_build != lock.build:
            # 内置模式：新 .app 原地替换了旧 .app（路径相同，原生壳认不出），正在跑的还是旧版后台 → 换掉它
            err = replace_running_instance(lock, pid)
            if err:
                print(err, file=sys.stderr)
                return 1
            replaced = (pid, old_build)
        else:
            try:
                os.kill(pid, sig)
            except OSError as e:
                print("无法通知已运行的实例（pid %d）：%s" % (pid, e), file=sys.stderr)
                return 1
            if args.stop:
                print("已通知正在运行的实例（pid %d）停止" % pid)
            elif sig == signal.SIGUSR2:
                print("已有实例在运行（pid %d），已让它重新加载配置（jobid=%s）" % (pid, args.jobid))
            else:
                print("已有实例在运行（pid %d），已让它重新打开窗口" % pid)
            return 0
    if args.stop:
        lock.release()
        print("没有正在运行的实例")
        return 0

    # 拿到实例锁（pid 文件已写）后立即安装信号处理：避免其它进程过早发来 SIGUSR1/2 按默认动作杀掉本进程
    stop = threading.Event()
    reopen = threading.Event()
    reload_cfg = threading.Event()

    def _on_stop(signum, frame):
        stop.set()

    def _on_reopen(signum, frame):
        reload_cfg.set()
        reopen.set()

    def _on_reload(signum, frame):
        reload_cfg.set()

    signal.signal(signal.SIGINT, _on_stop)
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGHUP, _on_stop)
    signal.signal(signal.SIGUSR1, _on_reopen)
    signal.signal(signal.SIGUSR2, _on_reload)

    setup_logging(data_dir, args.verbose)
    if replaced is not None:
        log.info("内置模式：已停止旧版本的后台（pid %d，构建 %s），由本进程（构建 %s）接管；tmux 里的 claude 不受影响",
                 replaced[0], replaced[1] or "无标识", lock.build)

    from ccr.history import HistoryService
    from ccr.notify import Notifier
    from ccr.server import CCRServer
    from ccr.session import SessionManager

    # 运行时配置（不写盘）：ssh 控制套接字目录跟随 --data-dir，所有 ssh 统一使用
    overrides = {"control_dir": str(data_dir / "ssh")}
    if args.fake:
        overrides["fake"] = True
        if args.fake_delays:
            fd = parse_fake_delays(args.fake_delays)
            overrides["fake_delays"] = fd
            if fd.get("boot"):
                overrides["fake_env"] = {"CCR_FAKE_BOOT": str(fd["boot"])}
    if args.no_sound:
        overrides["notify_dry_run"] = True   # 运行时覆盖（不写盘、不给前端）
    if args.bundled:
        overrides.update(bundled_overrides())
        if not os.access(overrides["local_tmux"], os.X_OK):
            log.error("内置模式：找不到 .app 自带的 tmux（%s），本机会话将无法启动", overrides["local_tmux"])
    store = ccr_config.ConfigStore(cfg_path, overrides=overrides)
    if args.jobid is not None:
        try:
            store.update({"jobid": args.jobid})
        except (ValueError, OSError) as e:
            log.error("设置 jobid 失败：%s", e)
            lock.release()
            return 2
    # 提示音：回合完成 / 需要操作时在本机 afplay（与 CLI 的 Stop hook 同一个声音），设置在 config.json
    manager = SessionManager(store.get, data_dir, notifier=Notifier(store.get))
    history = HistoryService(store.get)
    # 防休眠（ccr/keepawake.py）：有会话在干活时 caffeinate -i -w <本进程>；fake 模式只记日志、不起真进程
    from ccr.keepawake import KeepAwake
    keepawake = KeepAwake(store.get, manager.list_sessions, dry_run=args.fake)
    updater = None
    if args.bundled:   # dmg 版自动更新（SPEC §14.11）：.app 里有 UPDATE.json + BUILD 才启用；从源码运行时不创建
        try:
            from ccr import updater as ccr_updater
            updater = ccr_updater.create(data_dir, PROJECT_ROOT, lock.build, fake=args.fake,
                                         test_cfg=args.update_test,
                                         busy_fn=lambda: ccr_updater.busy_titles(manager.list_sessions()))
        except Exception:  # noqa  更新功能出问题不能影响启动
            log.exception("自动更新初始化失败，已关闭")
            updater = None
    try:
        server = CCRServer(manager, store, history, port=args.port, dev=args.dev, keepawake=keepawake,
                           **({"updater": updater} if updater is not None else {}))
    except OSError as e:
        log.error("启动服务器失败：%s", e)
        manager.shutdown()
        lock.release()
        return 1

    server.start()
    keepawake.start()
    remote = None
    if ccr_remote_access is not None:   # 只有 <data>/remote.json 开启时才起第二个监听口；失败不影响本机口
        try:
            remote = ccr_remote_access.start_from_config(server, data_dir)
        except Exception:  # noqa
            log.exception("手机远程访问启动失败，已跳过")
    if updater is not None:
        updater.on_change = server.notify_update_changed
        updater.start()
        log.info("自动更新已启用（当前版本 %s）", updater.current)
    log.info("cc-remote-chat 已启动：%s%s%s%s", server.base_url, "（fake 模式）" if args.fake else "",
             "（dev 模式）" if args.dev else "", "（内置模式）" if args.bundled else "")
    print("cc-remote-chat 已启动：%s（Ctrl-C 退出）" % server.base_url, flush=True)
    open_window(server, data_dir, args.no_browser)

    try:
        while not stop.is_set():
            stop.wait(0.5)
            if reload_cfg.is_set():
                reload_cfg.clear()
                try:
                    store.reload()
                    server.notify_config_changed()
                    log.info("已重新加载配置")
                except Exception:  # noqa
                    log.exception("重新加载配置失败")
            if reopen.is_set():
                reopen.clear()
                open_window(server, data_dir, args.no_browser)
    finally:
        log.info("正在退出：断开与计算节点的连接（远端 claude 保持运行）…")
        if updater is not None:
            updater.stop()
        if remote is not None:
            remote.shutdown()
        server.shutdown()
        try:
            keepawake.stop()   # 结束 caffeinate（-w 也会兜底）
        except Exception:  # noqa
            log.exception("结束防休眠出错")
        try:
            manager.shutdown()
        except Exception:  # noqa
            log.exception("断开连接出错")
        try:
            from ccr import remote as _remote
            if _remote.close_control_master(store.get()):
                log.info("已关闭 ssh 复用连接")
        except Exception:  # noqa
            log.exception("关闭 ssh 复用连接出错")
        lock.release()
        log.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
