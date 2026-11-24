"""本机会话的文件预览与辅助操作（只读）——docs/SPEC_V2_TUI.md §10「可视化」。

- 预览地址 `/preview/<cap>/<绝对路径>`：cap = 每个（本机会话, 根目录）随机生成的不可猜令牌（32 字符），
  只在内存里、app 重启即失效、只发给本机会话；根目录 = 会话 cwd（文件在 cwd 下且 cwd 不是 home、/ 这类宽泛目录时），
  否则 = 文件所在目录（宽泛目录下的文件只授权该文件本身）。用路径式 URL，HTML 的相对资源（css/js/图片/字体）
  能按原样加载；cap 只覆盖它的根目录，页面里的第三方脚本即使读到自己的地址也碰不到根目录以外的文件。
- 路径策略（先 realpath 再检查，请求路径与 realpath 都查）：拒绝任何以 `.` 开头的路径段（`.ssh`、`.claude`、
  `.cc-remote-chat`、`.git`、`..`…）、`~/Library`（含 Keychains）、`/Library/Keychains`、`/dev`、`/private/var/db`、
  本程序数据目录（fake 模式的假 HOME 除外）、目录（不列目录）、设备/FIFO/套接字等特殊文件、超过 50MB 的文件；
  realpath 必须仍在 cap 的根目录内（软链接逃逸 → 拒绝）。
- 响应：按扩展名给 Content-Type + `X-Content-Type-Options: nosniff` + `Referrer-Policy: no-referrer` +
  `Cache-Control: no-store`；HTML/SVG/XML 带 CSP `sandbox allow-scripts allow-forms allow-popups allow-modals
  allow-downloads`（不透明 origin，碰不到本 app 的存储与 API），其它类型（PDF 除外：Chrome 的 PDF 查看器不能在沙箱里运行）带
  `default-src 'none'; style-src 'unsafe-inline'; sandbox`。只对沙箱页面（Origin: null）以 script/style/font 方式请求的
  JS/CSS/字体回 `Access-Control-Allow-Origin: null`（模块脚本与 @font-face 需要 CORS），fetch/XHR 读不到任何文件内容。
- 只读：绝不写任何用户文件、不跑 git；唯一的副作用是用户点击后执行的 `open -R <路径>` / `open <路径>`（argv，不经 shell；
  可执行文件、.app/.command/.pkg 等不会被 open 直接运行）。
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
import subprocess
import threading
import urllib.parse
from typing import Callable, Optional

try:
    from ccr import remote as _remote
except ImportError:  # pragma: no cover
    _remote = None

LOCAL_JOBID = getattr(_remote, "LOCAL_JOBID", "local")
MAX_FILE = 50 * 1024 * 1024
MAX_PATH = 4096
MAX_STAT_PATHS = 200
MAX_CAPS_PER_SESSION = 500
CAP_LEN = 32

HTML_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
STRICT_CSP = "default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; sandbox"

TEXT = "text/plain; charset=utf-8"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".xhtml": "application/xhtml+xml; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".cjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".map": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml", ".xml": "application/xml; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".m4v": "video/mp4",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
    ".pdf": "application/pdf",
}
TEXT_EXTS = frozenset((
    ".txt .md .markdown .rst .csv .tsv .log .ini .cfg .conf .toml .yaml .yml .py .pyi .ipynb .sh .bash .zsh "
    ".fish .c .h .cc .cpp .hpp .cxx .m .mm .swift .java .kt .kts .scala .go .rs .rb .php .pl .lua .r .jl .sql "
    ".ts .tsx .jsx .vue .svelte .astro .scss .sass .less .tex .bib .diff .patch .env.example .gitignore .txt "
    ".dockerfile .makefile .cmake .gradle .proto .graphql .gql .tf .hcl .nix .zig .dart .ex .exs .erl .hs .ml "
    ".clj .el .vim .bat .ps1 .lock .properties .srt .vtt").split())
SANDBOX_HTML_TYPES = ("text/html", "application/xhtml+xml", "image/svg+xml", "application/xml")
CORS_DESTS = ("script", "style", "font")
CORS_TYPES = ("text/javascript", "text/css", "font/")
# 「用默认应用打开」（`open <路径>`）只允许这些类型（白名单）：图片、PDF、文本/代码、Markdown、HTML（用浏览器打开）、
# 音视频、常见 Office 文档、CSV/JSON。其它类型（可执行、安装包、脚本、.tcl/.jnlp/.vncloc/.webarchive…）只能"在 Finder 中显示"。
# 刻意不含 .py/.sh/.rb/.pl/.php/.tcl/.lua 等：默认打开方式可能是解释器启动器（如 Python Launcher）= 直接运行。
OPEN_ALLOW_EXTS = frozenset((
    ".png .jpg .jpeg .gif .webp .avif .bmp .tif .tiff .heic .heif .ico .svg "
    ".pdf "
    ".txt .text .log .md .markdown .rst .csv .tsv .json .jsonl .ndjson .yaml .yml .toml .ini .cfg .conf .xml "
    ".diff .patch .tex .bib .srt .vtt "
    ".js .mjs .cjs .ts .tsx .jsx .css .scss .sass .less .vue .svelte .c .h .cc .cpp .cxx .hpp .m .mm .swift .java "
    ".kt .kts .scala .go .rs .sql .proto .graphql .gql .dart .hs .ml .ex .exs .erl .clj .jl .r .ipynb "
    ".html .htm .xhtml "
    ".mp3 .wav .m4a .aac .flac .ogg .mp4 .m4v .mov .webm .mkv .avi "
    ".doc .docx .xls .xlsx .ppt .pptx .rtf .odt .ods .odp .key .pages .numbers").split())
# macOS 的 firmlink：/System/Volumes/Data/<x> 与 /<x> 是同一个目录（字符串比较前先去掉这个前缀）
DATA_VOLUME_PREFIX = "/system/volumes/data/"


class PreviewError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _segments(path: str) -> list:
    return [p for p in path.split("/") if p != ""]


def _within(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root if root.endswith("/") else root + "/")


def _fold(path: str) -> str:
    """字符串比较用的规范形式：APFS 默认不区分大小写（realpath 也不规范大小写）→ casefold；
    去掉 firmlink 前缀 /System/Volumes/Data。只用于"拒绝"方向的预检，最终以 (st_dev, st_ino) 为准。"""
    p = path.casefold()
    if p.startswith(DATA_VOLUME_PREFIX):
        p = "/" + p[len(DATA_VOLUME_PREFIX):]
    return p


def _ident(path: str):
    """目录/文件的身份 (st_dev, st_ino)；不存在返回 None。大小写不同、firmlink、硬链接目录…都是同一个身份。"""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _lineage(real: str) -> list:
    """realpath 自身及每一级上级目录的身份（从自身到 /）。"""
    out = []
    p = real
    while True:
        i = _ident(p)
        if i is not None:
            out.append(i)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return out


def open_allowed(path: str) -> bool:
    """「用默认应用打开」白名单（按扩展名，不区分大小写）。"""
    return os.path.splitext(path)[1].lower() in OPEN_ALLOW_EXTS


def kind_of(path: str) -> str:
    """前端预览方式：image / svg / html / markdown / pdf / video / audio / text / binary。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico"):
        return "image"
    if ext == ".svg":
        return "svg"
    if ext in (".html", ".htm", ".xhtml"):
        return "html"
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext == ".pdf":
        return "pdf"
    if ext in (".mp4", ".webm", ".mov", ".m4v"):
        return "video"
    if ext in (".mp3", ".wav", ".ogg", ".m4a"):
        return "audio"
    if ext in TEXT_EXTS or ext in (".css", ".js", ".mjs", ".cjs", ".json", ".map", ".xml") or ext == "":
        return "text"
    return "binary"


def content_type(path: str, head: bytes = b"") -> str:
    ext = os.path.splitext(path)[1].lower()
    ct = CONTENT_TYPES.get(ext)
    if ct:
        return ct
    if ext in TEXT_EXTS:
        return TEXT
    # 未知扩展名：像文本（无 NUL、能按 UTF-8 解码）就当纯文本，否则二进制下载
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
            return TEXT
        except UnicodeDecodeError:
            # 截断在多字节字符中间也算文本
            try:
                head[:-3].decode("utf-8")
                return TEXT
            except UnicodeDecodeError:
                pass
    return "application/octet-stream"


def _fd_path(fd: int) -> Optional[str]:
    """打开的文件现在的路径（macOS fcntl F_GETPATH）；不支持的平台返回 None（只靠打开前的检查 + fstat 复核）。"""
    try:
        import fcntl
        op = getattr(fcntl, "F_GETPATH", None)
        if op is None:
            return None
        raw = fcntl.fcntl(fd, op, b"\0" * 1024)
        return os.fsdecode(raw.split(b"\0", 1)[0]) or None
    except (OSError, ImportError, ValueError):
        return None


def is_local(meta: Optional[dict], cfg: Optional[dict] = None) -> bool:
    """本机会话：jobid == LOCAL_JOBID（本机模式），或 fake 模式（fake claude 本来就在本机跑）。"""
    if not isinstance(meta, dict):
        return False
    if meta.get("jobid") == LOCAL_JOBID:
        return True
    return bool((cfg or {}).get("fake"))


class PreviewService:
    def __init__(self, get_config: Callable[[], dict], data_dir=None, home: Optional[str] = None,
                 runner: Optional[Callable] = None):
        self.get_config = get_config
        self.data_dir = os.path.realpath(os.path.expanduser(str(data_dir))) if data_dir else None
        self.home = os.path.realpath(home or os.path.expanduser("~"))
        self.runner = runner or subprocess.run
        self._lock = threading.Lock()
        self._caps: "dict[str, tuple]" = {}        # cap -> (sid, root)
        self._by: "dict[tuple, str]" = {}          # (sid, root) -> cap

    # ------------------------------------------------------------ 会话
    def cfg(self) -> dict:
        try:
            return dict(self.get_config() or {})
        except Exception:  # noqa: BLE001
            return {}

    def is_local(self, meta) -> bool:
        return is_local(meta, self.cfg())

    def fake_home(self) -> Optional[str]:
        cfg = self.cfg()
        if not cfg.get("fake"):
            return None
        try:
            return _remote.fake_home(cfg) if _remote is not None else None
        except Exception:  # noqa: BLE001
            return None

    def session_home(self) -> str:
        """`~` 在会话里指什么：fake 模式 = 假 claude 的 HOME，本机模式 = 用户的 home。"""
        return self.fake_home() or self.home

    def expand(self, path, cwd: Optional[str] = None) -> str:
        """`~`/相对路径 → 绝对路径（相对路径按会话 cwd 解析）；不合法抛 PreviewError(400)。"""
        if not isinstance(path, str) or not path or len(path) > MAX_PATH:
            raise PreviewError(400, "路径不合法")
        if any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise PreviewError(400, "路径不合法")
        home = self.session_home()
        if path == "~" or path.startswith("~/"):
            path = home + path[1:]
        elif not path.startswith("/"):
            base = self.cwd_abs(cwd)
            if not base:
                raise PreviewError(400, "相对路径需要会话目录")
            path = os.path.join(base, path)
        path = os.path.normpath(path)
        return "/" + path.lstrip("/")

    def cwd_abs(self, cwd) -> Optional[str]:
        if not isinstance(cwd, str) or not cwd:
            return None
        home = self.session_home()
        if cwd == "~" or cwd.startswith("~/"):
            cwd = home + cwd[1:]
        if not cwd.startswith("/"):
            return None
        return os.path.normpath(cwd)

    # ------------------------------------------------------------ 路径策略
    def _deny_roots(self) -> list:
        out = [os.path.join(self.home, "Library"), "/Library/Keychains", "/System/Library/Keychains",
               "/dev", "/private/var/db", "/private/var/root", "/var/db", "/var/root", "/proc", "/sys"]
        fh = self.fake_home()
        if fh:
            out.append(os.path.join(fh, "Library"))
        return out

    def denied(self, path: str) -> Optional[str]:
        """字符串层面的预检（请求的路径与 realpath 都查）：返回原因（中文）或 None。
        比较前两边都做大小写折叠并去掉 /System/Volumes/Data 前缀；真正的判定见 denied_by_identity。"""
        for seg in _segments(path):
            if seg.startswith("."):
                return "不预览隐藏文件/目录（以 . 开头的路径段）"
        fp = _fold(path)
        for root in self._deny_roots():
            if _within(fp, _fold(root)):
                return "不预览系统敏感目录"
        if self.data_dir and _within(fp, _fold(self.data_dir)):
            fh = self.fake_home()
            if not (fh and _within(fp, _fold(fh))):
                return "不预览本程序的数据目录"
        return None

    def denied_by_identity(self, real: str) -> Optional[str]:
        """按 (st_dev, st_ino) 逐级向上比对拒绝目录：不受大小写、firmlink、别名路径影响。
        从文件往上走：先碰到敏感目录 → 拒绝；先碰到 fake HOME（允许）再碰到数据目录 → 放行，否则数据目录 → 拒绝。"""
        deny = {i for i in (_ident(p) for p in self._deny_roots()) if i is not None}
        data = _ident(self.data_dir) if self.data_dir else None
        fh = self.fake_home()
        allow = _ident(fh) if fh else None
        seen_allow = False
        for i in _lineage(real):
            if i in deny:
                return "不预览系统敏感目录"
            if allow is not None and i == allow:
                seen_allow = True
            if data is not None and i == data and not seen_allow:
                return "不预览本程序的数据目录"
        return None

    def check_real(self, real: str) -> None:
        """对已解析的真实路径做完整检查（字符串预检 + 身份比对），不通过抛 PreviewError(403)。"""
        why = self.denied(real) or self.denied_by_identity(real)
        if why:
            raise PreviewError(403, why)

    def resolve(self, path: str, want: str = "file"):
        """绝对路径 → (realpath, lstat)。want="file" 只接受普通文件（≤50MB），"any" 还接受目录。"""
        if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH or "\x00" in path:
            raise PreviewError(400, "路径不合法")
        why = self.denied(path)
        if why:
            raise PreviewError(403, why)
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            raise PreviewError(404, "文件不存在") from None
        self.check_real(real)
        try:
            st = os.lstat(real)
        except OSError:
            raise PreviewError(404, "文件不存在") from None
        if stat.S_ISDIR(st.st_mode):
            if want != "any":
                raise PreviewError(404, "这是目录（不列目录）")
        elif not stat.S_ISREG(st.st_mode):
            raise PreviewError(403, "不预览设备/管道等特殊文件")
        elif st.st_size > MAX_FILE:
            raise PreviewError(413, "文件过大（预览上限 50MB）")
        return real, st

    def read_file(self, real: str, st0, root_ident=None) -> bytes:
        """O_NOFOLLOW|O_NONBLOCK 打开，fstat 复核仍是同一个普通文件（防检查后被替换）。"""
        try:
            fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as e:
            raise PreviewError(404 if e.errno in (errno.ENOENT, errno.ELOOP) else 403, "无法读取文件") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (st0.st_dev, st0.st_ino):
                raise PreviewError(409, "文件已变化，请重试")
            # 打开之后再按内核给出的当前路径（macOS F_GETPATH）复核一遍：防"检查后、打开前"上级目录被换掉
            now = _fd_path(fd)
            if now is not None:
                self.check_real(now)
                if root_ident is not None and root_ident not in _lineage(now):
                    raise PreviewError(403, "该文件不在此预览地址的授权目录内")
            if st.st_size > MAX_FILE:
                raise PreviewError(413, "文件过大（预览上限 50MB）")
            chunks, left = [], MAX_FILE + 1
            while left > 0:
                b = os.read(fd, min(left, 1 << 20))
                if not b:
                    break
                chunks.append(b)
                left -= len(b)
            data = b"".join(chunks)
            if len(data) > MAX_FILE:
                raise PreviewError(413, "文件过大（预览上限 50MB）")
            return data
        finally:
            os.close(fd)

    # ------------------------------------------------------------ cap
    def _broad(self) -> set:
        h = self.session_home()
        return {"/", self.home, os.path.dirname(self.home), h, os.path.dirname(h), "/Users", "/home", "/tmp",
                "/private", "/private/tmp", "/var", "/private/var", "/Volumes", "/opt", "/usr", "/etc"}

    def root_for(self, real: str, cwd_real: Optional[str]) -> str:
        """cap 的根目录：按 (st_dev, st_ino) 判断"宽泛目录"与"在 cwd 下"（不受大小写/firmlink 写法影响）。"""
        broad = {i for i in (_ident(p) for p in self._broad()) if i is not None}
        line = _lineage(real)
        cwd_i = _ident(cwd_real) if cwd_real else None
        if cwd_i is not None and cwd_i not in broad and cwd_i in line:
            return cwd_real
        parent = os.path.dirname(real)
        return real if _ident(parent) in broad or parent == real else parent

    def cap_for(self, sid: str, root: str) -> str:
        with self._lock:
            cap = self._by.get((sid, root))
            if cap:
                return cap
            mine = [k for k, v in self._caps.items() if v[0] == sid]
            if len(mine) >= MAX_CAPS_PER_SESSION:
                old = mine[0]
                self._by.pop(self._caps.pop(old), None)
            cap = secrets.token_urlsafe(24)[:CAP_LEN]
            while len(cap) < CAP_LEN or cap in self._caps:
                cap = secrets.token_urlsafe(24)[:CAP_LEN]
            self._caps[cap] = (sid, root)
            self._by[(sid, root)] = cap
            return cap

    def lookup(self, cap) -> Optional[tuple]:
        if not isinstance(cap, str) or len(cap) != CAP_LEN:
            return None
        with self._lock:
            return self._caps.get(cap)

    def forget(self, sid: str) -> None:
        with self._lock:
            for k in [k for k, v in self._caps.items() if v[0] == sid]:
                self._by.pop(self._caps.pop(k), None)

    def url_for(self, sid: str, real: str, cwd: Optional[str]) -> str:
        cwd_real = None
        ca = self.cwd_abs(cwd)
        if ca:
            try:
                cwd_real = os.path.realpath(ca)
            except (OSError, ValueError):
                cwd_real = None
        root = self.root_for(real, cwd_real)
        return "/preview/%s%s" % (self.cap_for(sid, root), urllib.parse.quote(real, safe="/"))

    # ------------------------------------------------------------ 请求
    def serve(self, cap: str, rest: str, get_meta: Callable[[str], dict], headers) -> tuple:
        """GET/HEAD /preview/<cap>/<rest> → (status, [(头, 值)…], body)。cap 错误 / 会话不在 / 非本机 / 越界 → 404。"""
        hit = self.lookup(cap)
        if hit is None:
            raise PreviewError(404, "预览地址无效或已过期（app 重启后需重新打开）")
        sid, root = hit
        try:
            meta = get_meta(sid)
        except KeyError:
            self.forget(sid)   # 会话已删除：顺手清掉它的 cap
            raise PreviewError(404, "会话不存在") from None
        if not self.is_local(meta):
            raise PreviewError(404, "只有本机会话可以预览文件")
        try:
            path = urllib.parse.unquote(rest, errors="strict") if rest else ""
        except UnicodeDecodeError:
            raise PreviewError(400, "路径不合法") from None
        path = "/" + path.lstrip("/")
        if "\x00" in path or any(ord(c) < 32 for c in path):
            raise PreviewError(400, "路径不合法")
        if any(seg in (".", "..") for seg in path.split("/")):
            raise PreviewError(403, "路径不合法")
        real, st = self.resolve(path, "file")
        root_ident = _ident(root)
        if root_ident is None or root_ident not in _lineage(real):   # 软链接逃逸 / 根目录以外（按身份，不按字符串）
            raise PreviewError(403, "该文件不在此预览地址的授权目录内")
        data = self.read_file(real, st, root_ident)
        ctype = content_type(real, data[:4096])
        out = [("Content-Type", ctype), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
               ("Cache-Control", "no-store"), ("Cross-Origin-Resource-Policy", "cross-origin")]
        base = ctype.split(";")[0]
        if base in SANDBOX_HTML_TYPES:
            out.append(("Content-Security-Policy", HTML_CSP))
        elif base != "application/pdf":
            out.append(("Content-Security-Policy", STRICT_CSP))
        if base == "application/octet-stream":
            out.append(("Content-Disposition", "attachment"))
        origin = (headers.get("Origin") or "").strip()
        dest = (headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if origin == "null" and dest in CORS_DESTS and base.startswith(CORS_TYPES):
            out.append(("Access-Control-Allow-Origin", "null"))
        out.append(("Vary", "Origin, Sec-Fetch-Dest"))
        return 200, out, data

    def stat(self, sid: str, meta: dict, paths) -> dict:
        """批量查询（前端据此决定文件 chip 能不能点）：{results:[{path, abs, exists, type, size, mtime, ok, kind,
        url, reason}]}；被策略拒绝的路径一律 exists=false（不透露隐藏文件是否存在）。"""
        if not isinstance(paths, list) or len(paths) > MAX_STAT_PATHS:
            raise PreviewError(400, "paths 必须是至多 %d 个路径的数组" % MAX_STAT_PATHS)
        cwd = meta.get("cwd") if isinstance(meta, dict) else None
        out = []
        for p in paths:
            r = {"path": p if isinstance(p, str) else None, "abs": None, "exists": False, "type": None, "size": None,
                 "mtime": None, "ok": False, "kind": None, "url": None, "reason": None, "openable": False}
            out.append(r)
            try:
                ab = self.expand(p, cwd)
                r["abs"] = ab
                real, st = self.resolve(ab, "any")
            except PreviewError as e:
                r["reason"] = e.message
                continue
            r["exists"] = True
            r["real"] = real
            r["mtime"] = st.st_mtime
            if stat.S_ISDIR(st.st_mode):
                r["type"] = "dir"
                continue
            r["type"] = "file"
            r["size"] = st.st_size
            r["kind"] = kind_of(real)
            r["openable"] = open_allowed(real) and not (st.st_mode & 0o111)
            r["ok"] = True
            r["url"] = self.url_for(sid, real, cwd)
        return {"results": out}

    def open(self, meta: dict, path, action) -> dict:
        """用户点击「在 Finder 中显示」（reveal：open -R）/「用默认应用打开」（open）。argv 方式，不经 shell。"""
        if action not in ("reveal", "open"):
            raise PreviewError(400, "action 只能是 reveal 或 open")
        cwd = meta.get("cwd") if isinstance(meta, dict) else None
        real, st = self.resolve(self.expand(path, cwd), "any" if action == "reveal" else "file")
        if action == "open":
            if st.st_mode & 0o111 or not open_allowed(real):
                raise PreviewError(403, "这种类型的文件不会被直接打开（只允许图片/PDF/文本代码/HTML/音视频/Office 文档等），"
                                        "请用“在 Finder 中显示”")
            argv = ["open", real]          # real 是绝对路径（以 / 开头），不会被当成选项或 URL
        else:
            argv = ["open", "-R", real]
        try:
            r = self.runner(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, timeout=15, close_fds=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise PreviewError(502, "执行 open 失败：%s" % e) from None
        rc = getattr(r, "returncode", 0)
        if rc:
            err = getattr(r, "stderr", b"") or b""
            raise PreviewError(502, "open 返回 %s：%s" % (rc, err.decode("utf-8", "replace")[:200]))
        return {"ok": True, "path": real}
