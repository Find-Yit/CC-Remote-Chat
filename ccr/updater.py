"""dmg 版（内置模式）的自动更新（docs/SPEC_V2_TUI.md §14.11）。

只在内置模式、且 .app 里有 tools/make_dmg.sh --update-repo 写入的 <Resources>/app/UPDATE.json 时启用；
从源码运行（私有版、make_app.sh 的 app）不会创建本模块的任何对象，行为逐字不变。

- 检查：后端启动约 30 秒后一次、之后每 24 小时一次（设置里可关）；GET
  https://github.com/<repo>/releases/latest/download/update.json（不用 API，没有 60 次/小时的限制）。
  显式 SSL 上下文（优先 /etc/ssl/cert.pem、校验主机名、TLS ≥ 1.2）；每一跳（含重定向）都必须是 https 且主机在白名单；
  大小上限、超时；只发标准请求头（UA = CC-Remote-Chat/<版本>），不带任何用户信息；仓库没有 Release（404）安静处理。
- 清单 update.json：{version, build, dmg: {name, sha256, size}, notes, min_macos}；dmg 地址由 repo + tag(v<version>) + name
  拼出，不信任清单里的任何 URL；版本必须严格大于当前版本。
- 一键更新（用户点击后才开始；有会话在忙时先等它们空闲）：下载到 <数据目录>/updates/<版本>/（0700）→ 校验 size + sha256 →
  hdiutil attach -nobrowse -readonly -noautoopen → 检查 app（bundle id、CCRBundled、CCRVersion、BUILD 与清单一致；
  codesign --verify --deep --strict 且满足钉死的要求 `identifier "local.cc-remote-chat.bundled" and certificate leaf = H"<sha1>"`；
  再提取叶证书比对 SHA-256）→ ditto 到当前 .app 同一目录下的隐藏临时名并再次校验 → detach →
  rename 当前 .app → 隐藏旧名、rename 新 .app → 原名（第二步失败回滚）→ SIGTERM 本 bundle 的原生壳 → /usr/bin/open 新版；
  新壳运行新 app.py，按 §14.10 的「升级」逻辑停掉本进程（旧后台）并接管；tmux 里的 claude 不受影响。
  EPERM / EACCES（非管理员、或 macOS 13+「App 管理」权限拦截）→ 回滚，退回手动方案（用户点击时 open 已下载的 dmg）。
- 新版本启动后：提示一次「已更新到 X」，删掉隐藏的旧 .app 与下载件。

测试钩子：Updater 的 source / launcher / 时间参数可注入；app.py 只在 --fake 下接受 --update-test <json>（更新源、CA、
启动器记录文件），配置文件不能改更新源。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import pathlib
import platform
import plistlib
import re
import shutil
import signal
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional

log = logging.getLogger("ccr.updater")

BUNDLE_ID = "local.cc-remote-chat.bundled"
UPDATE_FILE = "UPDATE.json"
STATE_DIR = "updates"
STATE_FILE = "state.json"
GITHUB = "https://github.com"
# github.com 的 releases/latest/download/… 先 302 到 releases/download/<tag>/…（仍是 github.com），再 302 到资源 CDN
ALLOWED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                           "github-releases.githubusercontent.com"})
MANIFEST_MAX = 64 * 1024
DMG_MAX = 512 * 1024 * 1024
NOTES_MAX = 8000
HTTP_TIMEOUT = 30
FIRST_CHECK_DELAY = 30.0
CHECK_INTERVAL = 24 * 3600.0
SNOOZE_SECONDS = 24 * 3600.0
BUSY_POLL = 2.0
SHELL_EXIT_TIMEOUT = 5.0
RELAUNCH_GRACE = 60.0
CA_FILE = "/etc/ssl/cert.pem"

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}\Z")
VERSION_RE = re.compile(r"^[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}(?:-[A-Za-z0-9]{1,20}(?:\.[A-Za-z0-9]{1,20}){0,3})?\Z")
DMG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.dmg\Z")
HEX40_RE = re.compile(r"^[0-9a-f]{40}\Z")
HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
MACOS_RE = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,3}){0,2}\Z")


class UpdateError(Exception):
    """更新失败（消息给用户看）。manual=True：退回手动方案（拖进「应用程序」）。"""

    def __init__(self, msg: str, manual: bool = False):
        super().__init__(msg)
        self.manual = manual


class Cancelled(Exception):
    pass


# ============================================================================ 版本
def parse_version(v) -> Optional[tuple]:
    """"2.12" → (2, 12)；"2.12.1-test" → (2, 12, 1)（后缀不参与比较）；不合法 → None。末尾的 .0 去掉（2.12.0 == 2.12）。"""
    if not isinstance(v, str) or not VERSION_RE.match(v):
        return None
    nums = [int(x) for x in v.split("-", 1)[0].split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def is_newer(candidate, current) -> bool:
    """candidate 的数字部分严格大于 current 才算新版（数字相同、只是后缀不同 → 不算）。"""
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def _printable(s, limit) -> bool:
    return isinstance(s, str) and 0 < len(s) <= limit and all(" " <= c <= "~" for c in s)


# ============================================================================ 更新源（UPDATE.json）
class Source:
    """更新源：repo + 钉死的证书指纹。base / allowed_hosts / cafile 只有测试会改。"""

    def __init__(self, repo: str, cert_sha1: str, cert_sha256: str, base: str = GITHUB,
                 allowed_hosts=ALLOWED_HOSTS, cafile: Optional[str] = None):
        if not REPO_RE.match(repo or "") or ".." in repo:
            raise ValueError("repo 不合法")
        if not HEX40_RE.match(cert_sha1 or "") or not HEX64_RE.match(cert_sha256 or ""):
            raise ValueError("证书指纹不合法")
        self.repo, self.cert_sha1, self.cert_sha256 = repo, cert_sha1, cert_sha256
        self.base = base.rstrip("/")
        self.allowed_hosts = frozenset(allowed_hosts)
        self.cafile = cafile

    def manifest_url(self) -> str:
        return "%s/%s/releases/latest/download/update.json" % (self.base, self.repo)

    def dmg_url(self, version: str, name: str) -> str:
        return "%s/%s/releases/download/v%s/%s" % (self.base, self.repo, urllib.parse.quote(version, safe=""),
                                                   urllib.parse.quote(name, safe=""))

    def page_url(self, version: Optional[str] = None) -> str:
        if version:
            return "%s/%s/releases/tag/v%s" % (self.base, self.repo, urllib.parse.quote(version, safe=""))
        return "%s/%s/releases/latest" % (self.base, self.repo)

    def requirement(self) -> str:
        return 'identifier "%s" and certificate leaf = H"%s"' % (BUNDLE_ID, self.cert_sha1)


def load_source(app_root) -> Optional[Source]:
    """<app_root>/UPDATE.json → Source；没有（别人自己打包、从源码运行）或不合法 → None（更新功能关闭）。"""
    p = pathlib.Path(app_root) / UPDATE_FILE
    try:
        with open(p, "rb") as f:
            raw = f.read(4097)
    except OSError:
        return None
    try:
        if len(raw) > 4096:
            raise ValueError("太大")
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("不是对象")
        return Source(obj.get("repo"), obj.get("cert_sha1"), obj.get("cert_sha256"))
    except (ValueError, TypeError, UnicodeDecodeError) as e:
        log.warning("忽略不合法的 %s：%s", UPDATE_FILE, e)
        return None


# ============================================================================ 清单
def validate_manifest(obj, current: str, macos: Optional[str] = None) -> dict:
    """严格校验 update.json；返回规范化后的 dict（多一个 newer 字段）。不合法 → UpdateError。"""
    if not isinstance(obj, dict):
        raise UpdateError("更新清单格式不对")
    version = obj.get("version")
    if parse_version(version) is None:
        raise UpdateError("更新清单里的版本号不合法")
    build = obj.get("build", "")
    if build != "" and not _printable(build, 200):
        raise UpdateError("更新清单里的 build 不合法")
    dmg = obj.get("dmg")
    if not isinstance(dmg, dict):
        raise UpdateError("更新清单缺少 dmg")
    name, sha, size = dmg.get("name"), dmg.get("sha256"), dmg.get("size")
    if not isinstance(name, str) or not DMG_NAME_RE.match(name):
        raise UpdateError("更新清单里的 dmg 文件名不合法")
    if not isinstance(sha, str) or not HEX64_RE.match(sha):
        raise UpdateError("更新清单里的 sha256 不合法")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= DMG_MAX:
        raise UpdateError("更新清单里的 dmg 大小不合法")
    notes = obj.get("notes", "")
    if not isinstance(notes, str) or len(notes) > NOTES_MAX:
        raise UpdateError("更新清单里的更新说明不合法")
    min_macos = obj.get("min_macos", "")
    if min_macos != "" and (not isinstance(min_macos, str) or not MACOS_RE.match(min_macos)):
        raise UpdateError("更新清单里的 min_macos 不合法")
    supported = True
    if min_macos:
        mv = macos if macos is not None else (platform.mac_ver()[0] or "")
        try:
            supported = tuple(int(x) for x in mv.split(".")) >= tuple(int(x) for x in min_macos.split("."))
        except ValueError:
            supported = True
    return {"version": version, "build": build, "dmg": {"name": name, "sha256": sha, "size": size},
            "notes": notes, "min_macos": min_macos, "supported": supported, "newer": is_newer(version, current)}


# ============================================================================ HTTP（只 https、白名单主机）
class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, check):
        super().__init__()
        self._check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(self, source: Source, user_agent: str):
        self.source = source
        self.ua = user_agent
        cafile = source.cafile or (CA_FILE if os.path.isfile(CA_FILE) else None)
        ctx = ssl.create_default_context(cafile=cafile)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPSHandler(context=ctx),
                                                  _CheckedRedirect(self.check_url))
        self.opener.addheaders = []

    def check_url(self, url: str) -> None:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or (u.hostname or "") not in self.source.allowed_hosts or u.username or u.password:
            raise UpdateError("更新地址不在允许范围内（%s）" % (u.hostname or u.scheme or "?"))

    def open(self, url: str):
        self.check_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "*/*"})
        return self.opener.open(req, timeout=HTTP_TIMEOUT)

    def get_small(self, url: str, limit: int) -> bytes:
        with self.open(url) as r:
            data = r.read(limit + 1)
        if len(data) > limit:
            raise UpdateError("更新清单太大")
        return data


# ============================================================================ 进程（原生壳）
def _libproc():
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        return lib
    except (OSError, AttributeError):
        return None


def process_path(pid: int) -> Optional[str]:
    """进程可执行文件的路径（libproc proc_pidpath）；拿不到 → None。"""
    lib = _libproc()
    if lib is None:
        return None
    buf = ctypes.create_string_buffer(4096)
    n = lib.proc_pidpath(int(pid), buf, 4096)
    return buf.raw[:n].decode("utf-8", "replace") if n > 0 else None


def all_pids() -> List[int]:
    lib = _libproc()
    if lib is None:
        return []
    n = lib.proc_listallpids(None, 0)
    if n <= 0:
        return []
    arr = (ctypes.c_int * (n + 256))()
    n = lib.proc_listallpids(arr, ctypes.sizeof(arr))
    return [p for p in arr[:max(0, n)] if p > 1]


def find_processes(exe: str, hints=()) -> List[int]:
    """可执行文件正好是 exe 的进程（不含自己）。hints（如 window_request 里的壳 pid）只是优先看，同样要核对路径。"""
    want = os.path.realpath(exe)
    out = []
    for pid in list(hints) + all_pids():
        if not isinstance(pid, int) or pid <= 1 or pid == os.getpid() or pid in out:
            continue
        p = process_path(pid)
        if p and os.path.realpath(p) == want:
            out.append(pid)
    return out


def _alive(pid: int) -> bool:
    """进程还在（僵尸进程算已退出：proc_pidpath 对僵尸进程取不到路径）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return _libproc() is None or process_path(pid) is not None


def default_launcher(argv: List[str]) -> None:
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


# ============================================================================ bundle
def bundle_of(app_root) -> Optional[pathlib.Path]:
    """<X.app>/Contents/Resources/app → X.app；不是这个布局 → None。"""
    r = pathlib.Path(app_root)
    if r.name == "app" and r.parent.name == "Resources" and r.parent.parent.name == "Contents" \
            and r.parent.parent.parent.suffix == ".app":
        return r.parent.parent.parent
    return None


def read_plist(app: pathlib.Path) -> dict:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            d = plistlib.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}


def _run(argv, timeout=300):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def verify_app(app: pathlib.Path, source: Source, manifest: dict, current: str) -> None:
    """dmg 里（或拷出来）的 app：bundle id、CCRBundled、版本、BUILD、钉死的签名要求、叶证书 SHA-256。不符 → UpdateError。"""
    info = read_plist(app)
    if info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("CCRBundled") is not True:
        raise UpdateError("下载的不是 CC Remote Chat（dmg 版）")
    ver = info.get("CCRVersion")
    if ver != manifest["version"] or not is_newer(ver, current):
        raise UpdateError("下载的 app 版本（%s）与更新清单不符" % (ver if isinstance(ver, str) else "?"))
    if manifest.get("build"):
        try:
            with open(app / "Contents" / "Resources" / "app" / "BUILD", "r", encoding="ascii") as f:
                build = f.readline(512).strip()
        except (OSError, ValueError):
            build = ""
        if build != manifest["build"]:
            raise UpdateError("下载的 app 构建标识与更新清单不符")
    r = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", "=" + source.requirement(), str(app)])
    if r.returncode != 0:
        log.warning("codesign 校验失败：%s", (r.stderr or "").strip()[-500:])
        raise UpdateError("签名校验失败：不是用发布证书签名的 app（已拒绝安装）")
    with tempfile.TemporaryDirectory(prefix="ccr-upd-cert.") as td:
        r = subprocess.run(["/usr/bin/codesign", "-d", "--extract-certificates=" + os.path.join(td, "c"), str(app)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        try:
            with open(os.path.join(td, "c0"), "rb") as f:
                leaf = f.read(1 << 20)
        except OSError:
            leaf = b""
    if not leaf or hashlib.sha256(leaf).hexdigest() != source.cert_sha256:
        raise UpdateError("签名证书与发布证书不一致（已拒绝安装）")


class Mount:
    def __init__(self, dmg: str):
        self.dmg = dmg
        self.point = None

    def __enter__(self):
        self.point = tempfile.mkdtemp(prefix="ccr-upd-mnt.", dir="/tmp")
        r = _run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", self.point,
                  self.dmg], timeout=180)
        if r.returncode != 0:
            log.warning("hdiutil attach 失败：%s", (r.stderr or "").strip()[-500:])
            try:
                os.rmdir(self.point)
            except OSError:
                pass
            raise UpdateError("无法打开下载的 dmg（可能已损坏）")
        return pathlib.Path(self.point)

    def __exit__(self, *exc):
        if self.point:
            r = _run(["/usr/bin/hdiutil", "detach", self.point], timeout=120)
            if r.returncode != 0:
                _run(["/usr/bin/hdiutil", "detach", "-force", self.point], timeout=120)
            try:
                os.rmdir(self.point)
            except OSError:
                pass
        return False


def swap_bundles(cur: pathlib.Path, new: pathlib.Path, old: pathlib.Path) -> None:
    """cur → old（隐藏名），new → cur；第二步失败把 old 挪回 cur。PermissionError 原样抛出（调用方退回手动方案）。"""
    os.rename(cur, old)
    try:
        os.rename(new, cur)
    except OSError:
        try:
            os.rename(old, cur)
        except OSError:
            log.exception("回滚失败：%s → %s", old, cur)
        raise


def _perm(e: OSError) -> bool:
    import errno as _errno
    return isinstance(e, PermissionError) or getattr(e, "errno", None) in (_errno.EPERM, _errno.EACCES, _errno.EROFS)


# ============================================================================ Updater
class Updater:
    def __init__(self, data_dir, app_root, source: Source, current: str, build: str = "",
                 busy_fn: Optional[Callable[[], List[str]]] = None, launcher: Optional[Callable] = None,
                 first_delay: float = FIRST_CHECK_DELAY, interval: float = CHECK_INTERVAL,
                 shell_exit_timeout: float = SHELL_EXIT_TIMEOUT, busy_poll: float = BUSY_POLL,
                 now: Callable[[], float] = time.time, macos: Optional[str] = None):
        self.data_dir = pathlib.Path(data_dir)
        self.dir = self.data_dir / STATE_DIR
        self.app_root = pathlib.Path(app_root)
        self.bundle = bundle_of(app_root)
        self.source = source
        self.current = current
        self.build = build
        self.busy_fn = busy_fn or (lambda: [])
        self.launcher = launcher or default_launcher
        self.first_delay, self.interval = first_delay, interval
        self.shell_exit_timeout, self.busy_poll = shell_exit_timeout, busy_poll
        self.now = now
        self.macos = macos
        self.http = Http(source, "CC-Remote-Chat/%s" % current)
        self.on_change: Optional[Callable[[], None]] = None
        self.lock = threading.RLock()
        self.stop_ev = threading.Event()
        self.wake = threading.Event()
        self.cancel_ev = threading.Event()
        self.shell_hints: List[int] = []
        self._thread = None
        self._worker = None
        self.checking = False
        self.available: Optional[dict] = None
        self.phase = "idle"
        self.progress = None
        self.error = None
        self.busy: List[str] = []
        self.manual_dmg = None
        self.just_updated = None
        self.state = self._load_state()
        self._after_update()

    # ---------------------------------------------------------------- 持久状态
    def _load_state(self) -> dict:
        try:
            with open(self.dir / STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        from ccr import config as ccr_config
        self._mkdir(self.dir)
        ccr_config.atomic_write_json(self.dir / STATE_FILE, self.state)

    @staticmethod
    def _mkdir(p: pathlib.Path) -> None:
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)

    @property
    def auto_check(self) -> bool:
        return self.state.get("auto_check") is not False

    def _after_update(self) -> None:
        """新版本第一次启动：记下「已更新到 X」（未确认前一直提示），删掉隐藏的旧 .app 与下载件。"""
        inst = self.state.get("installed")
        if not isinstance(inst, dict) or inst.get("version") != self.current:
            return
        if not inst.get("acked"):
            self.just_updated = {"version": self.current, "from": inst.get("from") or ""}
        old = inst.get("old_bundle")
        if isinstance(old, str) and old and self.bundle is not None:
            p = pathlib.Path(old)
            if p.parent == self.bundle.parent and p.name.startswith("." + self.bundle.name + ".old-") \
                    and p.is_dir() and not p.is_symlink():
                try:
                    shutil.rmtree(p)
                    log.info("已删除更新前的旧版本：%s", p)
                except OSError as e:
                    log.warning("删除旧版本失败：%s", e)
            inst.pop("old_bundle", None)
        for d in self.dir.glob("*"):
            if d.is_dir() and not d.is_symlink() and parse_version(d.name) is not None:
                shutil.rmtree(d, ignore_errors=True)
        try:
            self._save_state()
        except OSError:
            pass

    # ---------------------------------------------------------------- 对外
    def status(self) -> dict:
        with self.lock:
            av = None
            if self.available:
                sn = self.state.get("snooze") or {}
                av = {"version": self.available["version"], "notes": self.available["notes"],
                      "page": self.source.page_url(self.available["version"]),
                      "size": self.available["dmg"]["size"],
                      "snoozed": sn.get("version") == self.available["version"]
                      and float(sn.get("until") or 0) > self.now()}
            lc = self.state.get("last_check")
            return {"enabled": True, "current": self.current, "auto_check": self.auto_check,
                    "checking": self.checking, "last_check": lc if isinstance(lc, dict) else None,
                    "available": av, "phase": self.phase, "progress": self.progress, "error": self.error,
                    "busy": list(self.busy), "manual": self.manual_dmg is not None,
                    "page": self.source.page_url(self.available["version"] if self.available else None),
                    "just_updated": self.just_updated}

    def _changed(self) -> None:
        cb = self.on_change
        if cb:
            try:
                cb()
            except Exception:  # noqa
                log.exception("on_change 出错")

    def note_shell_pid(self, pid: int) -> None:
        if isinstance(pid, int) and pid > 1:
            with self.lock:
                if pid in self.shell_hints:
                    self.shell_hints.remove(pid)
                self.shell_hints.insert(0, pid)
                del self.shell_hints[4:]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ccr-updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_ev.set()
        self.cancel_ev.set()
        self.wake.set()

    def set_auto(self, on: bool) -> dict:
        with self.lock:
            self.state["auto_check"] = bool(on)
            self._save_state()
        self.wake.set()
        self._changed()
        return self.status()

    def snooze(self, version: str) -> dict:
        with self.lock:
            if not isinstance(version, str) or parse_version(version) is None:
                raise ValueError("版本号不合法")
            self.state["snooze"] = {"version": version, "until": self.now() + SNOOZE_SECONDS}
            self._save_state()
        self._changed()
        return self.status()

    def ack(self) -> dict:
        with self.lock:
            self.just_updated = None
            inst = self.state.get("installed")
            if isinstance(inst, dict):
                inst["acked"] = True
                self._save_state()
        self._changed()
        return self.status()

    def dismiss(self) -> dict:
        with self.lock:
            if self.phase in ("failed", "manual"):
                self.phase, self.error, self.progress = "idle", None, None
        self._changed()
        return self.status()

    def check_now(self) -> dict:
        """用户点「立即检查」：后台检查一次（不看自动检查开关）。"""
        with self.lock:
            if not self.checking:
                self.checking = True
                threading.Thread(target=self._check_thread, name="ccr-update-check", daemon=True).start()
        self._changed()
        return self.status()

    def _check_thread(self):
        try:
            self.check()
        finally:
            with self.lock:
                self.checking = False
            self._changed()

    def install(self) -> dict:
        with self.lock:
            if self.phase in ("waiting", "downloading", "verifying", "installing", "relaunching"):
                return self.status()
            if not self.available:
                raise UpdateError("没有可用的新版本")
            self.cancel_ev.clear()
            self.phase, self.error, self.progress, self.manual_dmg = "waiting", None, None, None
            self._worker = threading.Thread(target=self._install_thread, name="ccr-update-install", daemon=True)
            self._worker.start()
        self._changed()
        return self.status()

    def cancel(self) -> dict:
        with self.lock:
            if self.phase in ("waiting", "downloading"):
                self.cancel_ev.set()
        self._changed()
        return self.status()

    def open_dmg(self) -> dict:
        """手动方案：用户点击「打开已下载的安装包」→ open dmg（Finder 里显示，用户自己拖进「应用程序」）。"""
        with self.lock:
            dmg = self.manual_dmg
        if not dmg or not os.path.isfile(dmg):
            raise UpdateError("找不到已下载的安装包，请点「打开下载页」手动下载")
        self.launcher(["/usr/bin/open", dmg])
        return self.status()

    # ---------------------------------------------------------------- 检查
    def _loop(self):
        if self.stop_ev.wait(self.first_delay):
            return
        while not self.stop_ev.is_set():
            if self.auto_check:
                self._check_thread_sync()
            self.wake.clear()
            self.wake.wait(self.interval)   # 打开自动检查开关 / stop() 会提前叫醒

    def _check_thread_sync(self):
        with self.lock:
            if self.checking:
                return
            self.checking = True
        self._changed()
        try:
            self.check()
        finally:
            with self.lock:
                self.checking = False
            self._changed()

    def check(self) -> str:
        """检查一次：返回 "available" / "uptodate" / "none"（仓库还没有 Release）/ "error"。不抛异常。"""
        result, err = "error", None
        try:
            data = self.http.get_small(self.source.manifest_url(), MANIFEST_MAX)
            try:
                obj = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise UpdateError("更新清单不是合法的 JSON") from None
            m = validate_manifest(obj, self.current, self.macos)
            if m["newer"] and m["supported"]:
                with self.lock:
                    self.available = m
                result = "available"
            else:
                with self.lock:
                    if self.phase not in ("waiting", "downloading", "verifying", "installing", "relaunching"):
                        self.available = None
                result = "uptodate" if m["supported"] or not m["newer"] else "unsupported"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                result = "none"
            else:
                err = "服务器返回 HTTP %d" % e.code
        except UpdateError as e:
            err = str(e)
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            reason = getattr(e, "reason", None) or e
            if isinstance(reason, UpdateError):
                err = str(reason)
            else:
                err = "网络错误：%s" % (reason if isinstance(reason, str) else type(reason).__name__)
        except Exception as e:  # noqa
            log.exception("检查更新出错")
            err = "检查更新出错：%s" % type(e).__name__
        with self.lock:
            self.state["last_check"] = {"at": self.now(), "result": result, "error": err}
            try:
                self._save_state()
            except OSError:
                pass
        log.info("检查更新：%s%s", result, "（%s）" % err if err else "")
        self._changed()
        return result

    # ---------------------------------------------------------------- 安装
    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
        self._changed()

    def _install_thread(self):
        try:
            self._install()
        except Cancelled:
            self._set(phase="idle", progress=None, busy=[], error=None)
            log.info("用户取消了更新")
        except UpdateError as e:
            log.warning("更新失败：%s", e)
            self._set(phase="manual" if e.manual else "failed", error=str(e), progress=None, busy=[])
        except Exception as e:  # noqa
            log.exception("更新出错")
            self._set(phase="failed", error="更新出错：%s" % type(e).__name__, progress=None, busy=[])

    def _wait_idle(self):
        while True:
            if self.cancel_ev.is_set() or self.stop_ev.is_set():
                raise Cancelled()
            try:
                busy = list(self.busy_fn() or [])
            except Exception:  # noqa
                busy = []
            if not busy:
                self._set(busy=[])
                return
            if busy != self.busy:
                self._set(busy=busy)
            self.cancel_ev.wait(self.busy_poll)

    def _install(self):
        m = self.available
        self._wait_idle()
        if self.bundle is None:
            raise UpdateError("没有在 .app 里运行，无法自动更新", manual=False)
        self._set(phase="downloading", progress={"done": 0, "total": m["dmg"]["size"]})
        dmg = self._download(m)
        self._set(phase="verifying", progress=None)
        cur = self.bundle
        stage = cur.parent / (".%s.new-%s" % (cur.name, m["version"]))
        old = cur.parent / (".%s.old-%s" % (cur.name, self.current))
        for p in (stage, old):
            if p.exists() or p.is_symlink():
                try:
                    shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else os.unlink(p)
                except OSError as e:
                    if _perm(e):
                        self._manual(dmg, "没有权限清理 %s" % p.parent)
                    raise UpdateError("无法清理上次留下的临时文件：%s" % e)
        with Mount(dmg) as mnt:
            apps = [p for p in mnt.iterdir() if p.suffix == ".app" and p.is_dir() and not p.is_symlink()]
            if len(apps) != 1:
                raise UpdateError("下载的 dmg 里没有找到 app")
            verify_app(apps[0], self.source, m, self.current)
            r = _run(["/usr/bin/ditto", str(apps[0]), str(stage)], timeout=600)
            if r.returncode != 0:
                shutil.rmtree(stage, ignore_errors=True)
                if "ermission" in (r.stderr or "") or "not permitted" in (r.stderr or ""):
                    self._manual(dmg, "没有权限写入「%s」" % cur.parent)
                raise UpdateError("复制新版本失败：%s" % (r.stderr or "").strip()[-200:])
        try:
            verify_app(stage, self.source, m, self.current)
        except UpdateError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        self._set(phase="installing")
        exe = cur / "Contents" / "MacOS" / str(read_plist(cur).get("CFBundleExecutable") or "CC Remote Chat")
        with self.lock:
            hints = list(self.shell_hints)
        shells = find_processes(str(exe), hints + [os.getppid()])
        try:
            swap_bundles(cur, stage, old)
        except OSError as e:
            shutil.rmtree(stage, ignore_errors=True)
            if _perm(e):
                self._manual(dmg, "macOS 不允许 CC Remote Chat 替换自己（%s）" % (e.strerror or e))
            raise UpdateError("替换 app 失败：%s" % (e.strerror or e))
        log.info("已安装 %s（旧版本移到 %s），重新打开窗口", m["version"], old)
        with self.lock:
            self.state["installed"] = {"version": m["version"], "from": self.current, "old_bundle": str(old),
                                       "at": self.now(), "acked": False}
            self.state.pop("snooze", None)
            self._save_state()
        self._set(phase="relaunching", progress=None)
        self._relaunch(cur, shells)

    def _manual(self, dmg: str, why: str):
        with self.lock:
            self.manual_dmg = dmg
        raise UpdateError("无法自动替换（%s）。请在「系统设置 → 隐私与安全性 → App 管理」里允许 CC Remote Chat 后点「重试」，"
                          "或者打开已下载的安装包，把新版拖进「应用程序」（选「替换」）" % why, manual=True)

    def _download(self, m: dict) -> str:
        d = self.dir / m["version"]
        self._mkdir(self.dir)
        self._mkdir(d)
        dst = d / m["dmg"]["name"]
        want, size = m["dmg"]["sha256"], m["dmg"]["size"]
        if dst.is_file() and not dst.is_symlink() and dst.stat().st_size == size and _sha256(dst) == want:
            self._set(progress={"done": size, "total": size})
            return str(dst)
        part = d / (m["dmg"]["name"] + ".part")
        h = hashlib.sha256()
        done = 0
        last = 0.0
        try:
            with self.http.open(self.source.dmg_url(m["version"], m["dmg"]["name"])) as r:
                cl = r.headers.get("Content-Length")
                if cl is not None and cl.isdigit() and int(cl) != size:
                    raise UpdateError("下载的文件大小与更新清单不符")
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as f:
                    while True:
                        if self.cancel_ev.is_set() or self.stop_ev.is_set():
                            raise Cancelled()
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > size:
                            raise UpdateError("下载的文件比更新清单里写的大")
                        h.update(chunk)
                        f.write(chunk)
                        t = time.monotonic()
                        if t - last >= 0.5:
                            last = t
                            self._set(progress={"done": done, "total": size})
            if done != size:
                raise UpdateError("下载不完整（%d / %d 字节）" % (done, size))
            if h.hexdigest() != want:
                raise UpdateError("下载的文件校验失败（sha256 不符）")
            os.replace(part, dst)
        except urllib.error.HTTPError as e:
            raise UpdateError("下载失败：HTTP %d" % e.code) from None
        except (urllib.error.URLError, ssl.SSLError) as e:
            reason = getattr(e, "reason", None)
            raise UpdateError(str(reason) if isinstance(reason, UpdateError) else "下载失败：网络错误") from None
        finally:
            if part.exists():
                try:
                    os.unlink(part)
                except OSError:
                    pass
        self._set(progress={"done": size, "total": size})
        return str(dst)

    def _relaunch(self, bundle: pathlib.Path, shells: List[int]):
        """SIGTERM 本 bundle 的原生壳（只发给核对过可执行路径的进程）→ 等它退出 → open 新版。"""
        for pid in shells:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        end = time.monotonic() + self.shell_exit_timeout
        while any(_alive(p) for p in shells) and time.monotonic() < end:
            time.sleep(0.1)
        if any(_alive(p) for p in shells):
            raise UpdateError("新版本已安装，但旧窗口没有退出：请退出 CC Remote Chat（⌘Q）后重新打开")
        try:
            self.launcher(["/usr/bin/open", str(bundle)])
        except OSError as e:
            raise UpdateError("新版本已安装，但无法自动打开（%s）：请从「应用程序」重新打开 CC Remote Chat" % e) from None
        log.info("已请求打开新版本：%s", bundle)
        # 新壳运行新 app.py，会停掉本进程；万一一直没人接管，过一会儿给出提示
        if not self.stop_ev.wait(RELAUNCH_GRACE):
            self._set(phase="failed", error="新版本已安装，但没有自动打开：请从「应用程序」重新打开 CC Remote Chat")


def _sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================================ app.py 用
def busy_titles(sessions) -> List[str]:
    """有会话在回复 / 等审批（含信任目录）/ 启动中 / 有子代理在干活 → 这些会话的标题。"""
    out = []
    for m in sessions or []:
        if not isinstance(m, dict):
            continue
        sub = m.get("subagents") if isinstance(m.get("subagents"), dict) else {}
        if m.get("working") or m.get("busy") or m.get("state") in ("busy", "waiting", "trust", "starting") \
                or (sub.get("running") or 0) > 0:
            out.append(str(m.get("display_title") or m.get("title") or m.get("sid") or "?"))
    return out


def create(data_dir, app_root, build: Optional[str], fake: bool = False, test_cfg: Optional[str] = None,
           busy_fn=None) -> Optional[Updater]:
    """内置模式下由 app.py 调用：UPDATE.json + BUILD 都在才启用。--fake 时只用 --update-test 指定的本机测试源
    （不会访问 GitHub）；没给测试源 → 关闭。"""
    src = load_source(app_root)
    if src is None or not build:
        return None
    current = build.split()[0]
    if parse_version(current) is None:
        log.warning("BUILD 里的版本号不合法，自动更新关闭")
        return None
    kw = {}
    if fake:
        if not test_cfg:
            return None
        with open(test_cfg, "r", encoding="utf-8") as f:
            t = json.load(f)
        src = Source(src.repo, t.get("cert_sha1") or src.cert_sha1, t.get("cert_sha256") or src.cert_sha256,
                     base=t["base"], allowed_hosts=t.get("allowed_hosts") or ["127.0.0.1"], cafile=t.get("cafile"))
        if t.get("launch_log"):
            path = t["launch_log"]

            def launcher(argv, _p=path):
                with open(_p, "a", encoding="utf-8") as f:
                    f.write(json.dumps(argv) + "\n")
            kw["launcher"] = launcher
        for k in ("first_delay", "interval", "shell_exit_timeout", "busy_poll"):
            if k in t:
                kw[k] = float(t[k])
    elif test_cfg:
        log.warning("--update-test 只在 --fake 下生效，已忽略")
    return Updater(data_dir, app_root, src, current, build, busy_fn=busy_fn, **kw)
