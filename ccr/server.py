"""HTTP API + SSE + 静态文件 + 安全（docs/SPEC_V2_TUI.md §4.3；认证/安全沿用 v1 docs/FIXES.md F1）。

- 标准库 ThreadingHTTPServer，只绑定 127.0.0.1；每个请求一个线程，慢的远端查询不阻塞其他请求。
- 安全（docs/FIXES.md F1）：打开窗口时生成一次性登录码（120s、只能兑换一次，放在 URL fragment
  `#code=`）；前端 POST /api/login 换取会话 token（内存保存）；除 /api/login 外所有 /api/*（含 GET、SSE）
  与 /fixtures/*（仅 --dev）都必须带 X-CCR-Token 请求头。不使用 Cookie。静态资源无需认证。
  Host 白名单（防 DNS rebinding）；有 Origin 必须同源；有 Sec-Fetch-Site 且不是 same-origin/none 一律拒绝；
  请求体上限 32MB；静态目录穿越防护。
- 错误统一 JSON：{"error": "中文说明"}。
"""

from __future__ import annotations

import errno
import hmac
import http.server
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import sys
import threading
import time
import collections
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from ccr import api_keys as ccr_api_keys
from ccr import blobs as ccr_blobs
from ccr import config as ccr_config
from ccr import macdialog as ccr_macdialog
from ccr import preview as ccr_preview
from ccr import notify as ccr_notify
from ccr import usage as ccr_usage
from ccr.history import HistoryError, HistoryService

log = logging.getLogger("ccr.server")

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
FIXTURES_DIR = PROJECT_ROOT / "fixtures"

MAX_BODY = 32 * 1024 * 1024
MAX_LOGIN_BODY = 4 * 1024   # 未认证的 /api/login 只读小请求体
TOKEN_HEADER = "X-CCR-Token"
LOGIN_CODE_TTL = 120.0      # 一次性登录码有效期（秒）
MAX_LOGIN_CODES = 16        # 同时有效的登录码上限（超出丢弃最旧的）
MAX_SESSION_TOKENS = 64     # 同时有效的会话 token 上限（超出丢弃最旧的）
LOGIN_FAILED_MSG = "登录码无效或已过期，请从应用重新打开窗口"
UNAUTH_MSG = ("会话已失效，请在终端运行 python3 app.py（或打开 CC Remote Chat.app）重新打开窗口")
SSE_PING_INTERVAL = 15.0
SID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}\Z")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
# 与 ccr.session.KEYS_WHITELIST / node_agent.KEYS 一致（终端面板/对话框按钮可发的键）
KEYS_WHITELIST = ("Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BTab", "BSpace", "Space", "C-c",
                  "C-u", "C-b", "C-x", "C-s", "1", "2", "3", "4", "5", "6", "7", "8", "9", "s", "y", "n")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
V2_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
MAX_KEYS = 40
MAX_TEXT = 200000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ndjson": "application/x-ndjson; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}

# frame-src：右侧预览面板的沙箱 iframe（/preview/… 同源但 CSP sandbox = 不透明 origin；本机 localhost 其它端口的页面）
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
       "frame-src 'self' http://127.0.0.1:* http://localhost:*; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _opt_bool(body: dict, key: str) -> bool:
    v = body.get(key, False)
    if v is None:
        return False
    if not isinstance(v, bool):
        raise ApiError(400, "%s 必须是布尔值" % key)
    return v


def _req_int(body: dict, key: str) -> int:
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ApiError(400, "%s 必须是整数" % key)
    if v < 0 or v > 1000:
        raise ApiError(400, "%s 超出范围" % key)
    return v


def _opt_str(body: dict, key: str, maxlen: int = 4096) -> Optional[str]:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ApiError(400, "%s 必须是字符串" % key)
    if len(v) > maxlen:
        raise ApiError(400, "%s 过长" % key)
    return v


def _api_key_id(body: dict) -> Optional[str]:
    """请求体里的 api_key_id：null/"" = 服务器默认（返回 None）；否则必须是合法 id。"""
    v = body.get("api_key_id")
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not ccr_api_keys.ID_RE.match(v):
        raise ApiError(400, "api_key_id 不合法")
    return v


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class _HTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # 允许复用 TIME_WAIT 端口（重启后仍用同一端口）；真正被占用由 CCRServer._bind 先探测
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):  # 客户端断开等噪声不打印堆栈
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
            return
        log.exception("请求处理异常")


class CCRServer:
    """把 SessionManager / ConfigStore / HistoryService 暴露成 HTTP API。"""

    def __init__(self, manager, config_store, history: Optional[HistoryService] = None, *,
                 static_dir: Optional[pathlib.Path] = None, port: int = 8765, port_tries: int = 20,
                 dev: bool = False, fixtures_dir: Optional[pathlib.Path] = FIXTURES_DIR, api_keys=None,
                 updater=None):
        self.manager = manager
        # dmg 版自动更新（ccr/updater.py，SPEC §14.11）：只有内置模式且配置了更新源时才有；None 时 /api/update* 不存在、
        # bootstrap 没有 update 键、SSE 不推 event: update（与以前逐字相同）
        self.updater = updater
        self.update_version = 0
        self.config_store = config_store
        self.history = history or HistoryService(config_store.get)
        # 已保存的 API key：与 SessionManager 共用同一个实例（没有时按配置目录建一个，测试桩 manager 用）
        if api_keys is None:
            api_keys = getattr(manager, "api_keys", None)
        if api_keys is None:
            base = pathlib.Path(getattr(config_store, "path", ccr_config.DEFAULT_DATA_DIR / "config.json")).parent
            api_keys = ccr_api_keys.ApiKeyStore(base / "api_keys.json")
        self.api_keys = api_keys
        self.usage = ccr_usage.UsageService(api_keys)   # 各 key 近 7 天用量（本地直接查网关，缓存 5 分钟）
        # 可视化：大图片引用的服务端 LRU；本机会话的文件预览（/preview/<cap>/…，只读）
        self.blobs = ccr_blobs.BlobCache()
        data_dir = getattr(manager, "data_dir", None) or pathlib.Path(
            getattr(config_store, "path", ccr_config.DEFAULT_DATA_DIR / "config.json")).parent
        self.preview = ccr_preview.PreviewService(config_store.get, data_dir=data_dir)
        self.static_dir = pathlib.Path(static_dir or STATIC_DIR).resolve()
        # /fixtures/* 仅在 --dev 时启用（默认 404）
        self.dev = bool(dev)
        self.fixtures_dir = pathlib.Path(fixtures_dir).resolve() if (fixtures_dir and self.dev) else None
        # 每次进程启动随机生成；前端据此发现"服务器已重启"（SSE instance 不一致 → reset）
        self.instance_id = secrets.token_hex(8)
        self._auth_lock = threading.Lock()
        self._codes: "collections.OrderedDict[str, float]" = collections.OrderedDict()  # code -> 过期时刻
        self._tokens: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        # 配置被外部修改（SIGUSR1 重新加载 / POST /api/config）时递增，SSE 据此推送 event: config
        self.config_version = 0
        self.stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._serving = False
        self.httpd = self._bind(port, port_tries)
        self.httpd.ccr = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}
        self.allowed_origins = {"http://" + h for h in self.allowed_hosts}

    # -- 生命周期 ------------------------------------------------------------
    def _bind(self, port: int, tries: int) -> _HTTPServer:
        if port == 0:  # 由系统分配（测试用）
            return _HTTPServer(("127.0.0.1", 0), _Handler)
        last = None
        for p in range(port, min(65536, port + max(1, tries))):
            if _port_listening(p):  # macOS 上 SO_REUSEADDR 可与 0.0.0.0 监听者共存，先探测避免"共用"端口
                last = OSError(errno.EADDRINUSE, "端口已被占用")
                continue
            try:
                return _HTTPServer(("127.0.0.1", p), _Handler)
            except OSError as e:
                last = e
                if e.errno in (errno.EADDRINUSE, errno.EACCES):
                    continue
                raise
        raise OSError("无法绑定端口 %d–%d：%s" % (port, port + tries - 1, last))

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    # -- 认证：一次性登录码 → 会话 token ---------------------------------------
    def new_login_code(self) -> str:
        """生成一次性登录码（32 字节随机，url-safe；LOGIN_CODE_TTL 秒内有效，只能兑换一次）。"""
        code = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._auth_lock:
            for c, exp in list(self._codes.items()):
                if exp <= now:
                    del self._codes[c]
            self._codes[code] = now + LOGIN_CODE_TTL
            while len(self._codes) > MAX_LOGIN_CODES:
                self._codes.popitem(last=False)
        return code

    def new_login_url(self) -> str:
        """打开窗口用的 URL：码放在 fragment 里，不会出现在 HTTP 请求行/日志中。"""
        return "%s/#code=%s" % (self.base_url, self.new_login_code())

    def redeem_login_code(self, code: Any) -> Optional[str]:
        """兑换登录码：成功返回新的会话 token（码随即作废），失败返回 None。"""
        if not isinstance(code, str) or not code or len(code) > 256:
            return None
        cb = code.encode("utf-8", "replace")
        now = time.monotonic()
        with self._auth_lock:
            hit = None
            for c in list(self._codes):
                if hmac.compare_digest(cb, c.encode("utf-8")):
                    hit = c
            if hit is None:
                return None
            exp = self._codes.pop(hit)
            if exp <= now:
                return None
            token = secrets.token_urlsafe(32)
            self._tokens[token] = None
            while len(self._tokens) > MAX_SESSION_TOKENS:
                self._tokens.popitem(last=False)
            return token

    def issue_token(self) -> str:
        """直接签发会话 token（测试用；等价于生成并兑换一个登录码）。"""
        tok = self.redeem_login_code(self.new_login_code())
        assert tok
        return tok

    def notify_config_changed(self) -> None:
        with self._auth_lock:
            self.config_version += 1

    def notify_update_changed(self) -> None:
        with self._auth_lock:
            self.update_version += 1

    def serve_forever(self):
        self._serving = True
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self._serving = False

    def start(self) -> "CCRServer":
        self._serving = True
        self._thread = threading.Thread(target=self.serve_forever, name="ccr-http", daemon=True)
        self._thread.start()
        return self

    def shutdown(self):
        self.stopping.set()
        if self._serving:  # 未启动 serve_forever 时调用 httpd.shutdown() 会永久阻塞
            try:
                self.httpd.shutdown()
            except Exception:  # noqa
                pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa
            pass

    # -- 工具 ----------------------------------------------------------------
    def check_token(self, value: Optional[str]) -> bool:
        """常量时间比较请求头中的会话 token。"""
        if not value or not isinstance(value, str) or len(value) > 256:
            return False
        vb = value.strip().encode("utf-8", "replace")
        with self._auth_lock:
            toks = list(self._tokens)
        ok = False
        for t in toks:
            if hmac.compare_digest(vb, t.encode("utf-8")):
                ok = True
        return ok

    def current_gseq(self) -> int:
        m = self.manager
        for name in ("current_gseq", "gseq", "last_gseq"):
            v = getattr(m, name, None)
            if callable(v):
                try:
                    v = v()
                except TypeError:
                    v = None
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        try:
            evs = m.events_since(0)
        except Exception:  # noqa
            return 0
        best = 0
        for ev in evs[-1:] if evs else []:
            g = ev.get("gseq")
            if isinstance(g, int):
                best = g
        return best

    def config(self) -> dict:
        return ccr_config.public_config(self.config_store.get())


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cc-remote-chat"
    sys_version = ""
    timeout = 300  # 空闲 keep-alive 连接超时

    # -- 基础 ----------------------------------------------------------------
    @property
    def app(self) -> CCRServer:
        return self.server.ccr  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 不记录查询串
        try:
            path = urllib.parse.urlsplit(self.path).path
        except Exception:  # noqa
            path = "?"
        log.debug("%s %s %s", self.command, path, args[1] if len(args) > 1 else "")

    def log_request(self, code="-", size="-"):
        self.log_message("%s", "", code)

    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")

    def _send_bytes(self, status: int, body: bytes, ctype: str, extra: Optional[List[Tuple[str, str]]] = None,
                    cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._common_headers()
        for k, v in extra or []:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, obj: Any):
        self._send_bytes(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str, code: Optional[str] = None):
        body = {"error": message}
        if code:
            body["code"] = code
        self._send_json(status, body)

    def send_error(self, code, message=None, explain=None):  # 覆盖默认 HTML 错误页（如请求行解析失败）
        msgs = {400: "请求格式不正确", 404: "未找到", 405: "不支持的方法", 408: "请求超时",
                414: "请求地址过长", 431: "请求头过大", 501: "不支持的方法", 505: "不支持的 HTTP 版本"}
        self.close_connection = True
        try:
            self._send_error_json(code, msgs.get(code, "请求错误"))
        except Exception:  # noqa
            pass

    # -- 方法入口 ------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_HEAD(self):
        if urllib.parse.urlsplit(self.path).path.startswith("/preview/"):
            return self._dispatch("HEAD")
        self.close_connection = True
        self._send_error_json(405, "不支持的方法")

    def do_OPTIONS(self):
        self.close_connection = True
        self._send_error_json(405, "不支持跨域请求")

    def _dispatch(self, method: str):
        try:
            self._handle(method)
        except ApiError as e:
            self._finish_body_on_error()
            self._send_error_json(e.status, e.message, e.code)
        except HistoryError as e:
            self._send_error_json(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception:  # noqa
            log.exception("内部错误：%s %s", method, urllib.parse.urlsplit(self.path).path)
            self.close_connection = True
            try:
                self._send_error_json(500, "服务器内部错误")
            except Exception:  # noqa
                pass

    def _finish_body_on_error(self):
        # 若请求体未被读取，保持连接复用会读乱下一请求；直接关闭连接最简单可靠。
        if not getattr(self, "_body_consumed", True):
            self.close_connection = True

    # -- 安全检查 ------------------------------------------------------------
    def _check_host(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.app.allowed_hosts:
            raise ApiError(403, "Host 不被允许（仅限 127.0.0.1/localhost 本机访问）")

    def _check_origin(self):
        """有 Origin 必须同源；有 Sec-Fetch-Site 且不是 same-origin/none 一律拒绝（含 GET）。"""
        origin = self.headers.get("Origin")
        if origin is not None:
            origin = origin.strip().lower()
            host = (self.headers.get("Host") or "").strip().lower()
            if origin != "http://" + host or origin not in self.app.allowed_origins:
                raise ApiError(403, "跨源请求被拒绝")
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.strip().lower() not in ("same-origin", "none"):
            raise ApiError(403, "跨站请求被拒绝")

    def _require_token(self):
        """只认 X-CCR-Token 请求头（不接受 Cookie）。"""
        if not self.app.check_token(self.headers.get(TOKEN_HEADER)):
            raise ApiError(401, UNAUTH_MSG)

    # -- 请求体 --------------------------------------------------------------
    def _read_json(self, max_body: int = MAX_BODY) -> dict:
        self._body_consumed = False
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "不支持分块传输，请提供 Content-Length")
        cl = self.headers.get("Content-Length")
        if cl is None or cl.strip() == "":
            length = 0
        else:
            try:
                length = int(cl)
            except ValueError:
                raise ApiError(400, "Content-Length 不合法") from None
        if length < 0:
            raise ApiError(400, "Content-Length 不合法")
        if length > max_body:
            self.close_connection = True   # 不读超限的请求体，直接断开
            raise ApiError(413, "请求体过大（上限 %s）" % ("32MB" if max_body == MAX_BODY else "%dKB" % (max_body // 1024)))
        data = b""
        remaining = length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        self._body_consumed = True
        if remaining > 0:
            self.close_connection = True
            raise ApiError(400, "请求体不完整")
        if not data.strip():
            return {}
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "请求体不是合法的 JSON") from None
        if not isinstance(obj, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return obj

    # -- 路由 ----------------------------------------------------------------
    def _handle(self, method: str):
        self._body_consumed = True
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            # 先标记：若在读取请求体前就出错，需要关闭连接
            self._body_consumed = not (self.headers.get("Content-Length") not in (None, "", "0")
                                       or self.headers.get("Transfer-Encoding"))
        self._check_host()
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)

        if path.startswith("/preview/"):
            # 本机文件预览：iframe 子资源带不了 token 头，也不是同源（沙箱 = 不透明 origin），靠不可猜的 cap 授权；
            # Host 校验已在上面做过；只允许 GET/HEAD（ccr.preview）
            return self._serve_preview(method, path)
        self._check_origin()
        is_api = path.startswith("/api/") or path == "/api"
        if is_api:
            if path == "/api/login":
                if method != "POST":
                    raise ApiError(405, "不支持的方法")
                return self._login()
            self._require_token()
            self._route_api(method, path, query)
            return
        if method != "GET":
            raise ApiError(405, "不支持的方法")
        if path.startswith("/fixtures/"):
            if self.app.fixtures_dir is None:  # 仅 --dev 启用
                raise ApiError(404, "未找到")
            self._require_token()
        self._serve_static(path)

    def _login(self):
        body = self._read_json(MAX_LOGIN_BODY)
        token = self.app.redeem_login_code(body.get("code"))
        if token is None:
            raise ApiError(401, LOGIN_FAILED_MSG)
        self._send_json(200, {"token": token})

    def _route_api(self, method: str, path: str, query: Dict[str, List[str]]):
        segs = [s for s in path.split("/") if s != ""][1:]  # 去掉 "api"
        app = self.app
        mgr = app.manager

        def q(name, default=None):
            v = query.get(name)
            return v[0] if v else default

        if segs and segs[0] == "update" and app.updater is not None:
            return self._update_route(method, segs[1:])

        if segs and segs[0] == "api_keys":
            return self._api_keys_route(method, segs[1:], q("force", "0") in ("1", "true", "yes"),
                                        q("refresh", "0") in ("1", "true", "yes"))

        if method == "GET":
            if segs == ["bootstrap"]:
                gseq = app.current_gseq()
                boot = {
                    "instance_id": app.instance_id,
                    "config": app.config(),
                    "sessions": mgr.list_sessions(),
                    "gseq": gseq,
                    "permission_modes": list(V2_PERMISSION_MODES),
                    "effort_levels": list(EFFORT_LEVELS),
                    "keys": list(KEYS_WHITELIST),
                }
                if app.updater is not None:
                    boot["update"] = app.updater.status()
                return self._send_json(200, boot)
            if segs == ["stream"]:
                return self._sse(q("since"), q("instance"))
            if segs == ["config"]:
                return self._send_json(200, app.config())
            if segs == ["sessions"]:
                return self._send_json(200, {"sessions": mgr.list_sessions()})
            if len(segs) == 2 and segs[0] == "sessions":
                return self._send_json(200, self._meta(segs[1]))
            if len(segs) == 3 and segs[0] == "sessions" and segs[2] == "screen":
                sid = self._sid(segs[1])
                return self._send_json(200, self._call(lambda sd, _b: mgr.get_screen(sd), sid, {}))
            if len(segs) == 3 and segs[0] == "sessions" and segs[2] == "events":
                sid = self._sid(segs[1])
                try:
                    ex = getattr(mgr, "session_events_ex", None)
                    if callable(ex):
                        evs, truncated = ex(sid)
                    else:
                        evs, truncated = mgr.session_events(sid), False
                except KeyError:
                    raise ApiError(404, "会话不存在") from None
                return self._send_json(200, {"events": evs, "truncated": bool(truncated)})
            if len(segs) == 4 and segs[0] == "sessions" and segs[2] == "blob":
                return self._blob(self._sid(segs[1]), segs[3])
            if len(segs) == 4 and segs[0] == "sessions" and segs[2:] == ["local", "info"]:
                sid = self._sid(segs[1])
                return self._send_json(200, self._call(self._local_info, sid, {}))
            if len(segs) in (3, 4) and segs[0] == "sessions" and segs[2] == "subagents":
                return self._send_json(200, self._subagents(self._sid(segs[1]), segs[3] if len(segs) == 4 else None))
            if segs == ["notify"]:
                return self._send_json(200, self._notify_info())
            if segs == ["jobs"]:
                return self._send_json(200, app.history.list_jobs())
            if segs == ["dirs"]:
                hidden = q("hidden", "0") in ("1", "true", "yes")
                return self._send_json(200, app.history.list_dirs(q("path"), hidden, **self._target_kw(q("target"))))
            if segs == ["history"]:
                lim = q("limit", "100")
                try:
                    limit = int(lim)
                except (TypeError, ValueError):
                    raise ApiError(400, "limit 必须是整数") from None
                return self._send_json(200, self._with_api_keys(
                    app.history.list_history(limit, **self._target_kw(q("target")))))
            if len(segs) == 2 and segs[0] == "history":
                return self._send_json(200, app.history.read_history(segs[1], **self._target_kw(q("target"))))
            raise ApiError(404, "接口不存在")

        if method == "POST":
            if segs == ["sessions"]:
                return self._send_json(200, self._create(self._read_json()))
            if segs == ["sessions", "order"]:   # 侧栏手动排序：{"sids": [从上到下]}
                body = self._read_json(256 * 1024)
                try:
                    return self._send_json(200, app.manager.reorder(body.get("sids")))
                except ValueError as e:
                    raise ApiError(400, str(e) or "参数不合法") from None
            if segs == ["notify", "test"]:
                return self._send_json(200, self._notify_test(self._read_json()))
            if segs == ["local", "choose_folder"]:
                return self._send_json(200, self._choose_folder(self._read_json()))
            if segs == ["config"]:
                body = self._read_json()
                try:
                    cfg = app.config_store.update(body)
                except ValueError as e:
                    raise ApiError(400, str(e) or "配置不合法") from None
                except ImportError:
                    raise ApiError(500, "校验模块不可用（ccr.remote 未就绪）") from None
                except OSError as e:
                    raise ApiError(500, "保存配置失败：%s" % e.strerror) from None
                app.notify_config_changed()
                return self._send_json(200, ccr_config.public_config(cfg))
            if len(segs) in (3, 4) and segs[0] == "sessions":
                sid = self._sid(segs[1])
                action = "/".join(segs[2:])
                handler = {
                    "message": self._act_message,
                    "keys": self._act_keys,
                    "choose": self._act_choose,
                    "cancel_dialog": self._act_cancel_dialog,
                    "interrupt": self._act_interrupt,
                    "mode/cycle": self._act_mode_cycle,
                    "mode": self._act_mode,
                    "model_picker": self._act_model_picker,
                    "model": self._act_model,
                    "effort": self._act_effort,
                    "restart": self._act_restart,
                    "close": self._act_close,
                    "local/stat": self._act_local_stat,
                    "local/open": self._act_local_open,
                    "background": lambda sd, _b: self.app.manager.run_in_background(sd),
                    "send_now": lambda sd, _b: self.app.manager.send_now(sd),
                    "seen": self._act_seen,
                    "unseen": lambda sd, _b: self.app.manager.mark_unseen(sd),
                }.get(action)
                if handler is None:
                    raise ApiError(404, "接口不存在")
                body = self._read_json()
                return self._send_json(200, self._call(handler, sid, body))
            raise ApiError(404, "接口不存在")

        if method == "PATCH":
            if len(segs) == 2 and segs[0] == "sessions":
                sid = self._sid(segs[1])
                body = self._read_json()
                return self._send_json(200, self._call(self._act_rename, sid, body))
            raise ApiError(404, "接口不存在")

        if method == "DELETE":
            if len(segs) == 2 and segs[0] == "sessions":
                sid = self._sid(segs[1])
                body = self._read_json()
                if q("force", "0") in ("1", "true", "yes"):
                    body = dict(body, force=True)
                return self._send_json(200, self._call(self._act_remove, sid, body))
            raise ApiError(404, "接口不存在")

        raise ApiError(405, "不支持的方法")

    @staticmethod
    def _target_kw(target: Optional[str]) -> dict:
        """历史/目录查询的运行位置（?target=）：缺省/server = 服务器（调用与以前完全一致，不带 target 参数）；
        local = 本机（SPEC §9）；其它取值 400。"""
        if target is None or target in ("", "server"):
            return {}
        if target == "local":
            return {"target": "local"}
        raise ApiError(400, "target 必须是 server 或 local")

    # -- 可视化：图片引用 / 本机文件预览 ----------------------------------------
    def _blob(self, sid: str, ref: str):
        """GET /api/sessions/<sid>/blob/<ref>：按 ccr_ref 取回图片字节（image/png|jpeg|gif|webp + nosniff）。"""
        ref = urllib.parse.unquote(ref)
        if not ccr_blobs.valid_ref(ref):
            raise ApiError(404, "图片引用不合法")
        hit = self.app.blobs.get(sid, ref)
        if hit is None:
            fn = getattr(self.app.manager, "get_blob", None)
            if not callable(fn):
                raise ApiError(404, "接口不存在")
            res = self._call(lambda sd, _b: fn(sd, ref), sid, {})
            try:
                ctype, data = ccr_blobs.decode_image((res or {}).get("data"))
            except ValueError as e:
                raise ApiError(415, str(e)) from None
            self.app.blobs.put(sid, ref, ctype, data)
        else:
            ctype, data = hit
        self._send_bytes(200, data, ctype, [("Content-Security-Policy", "default-src 'none'; sandbox")],
                         cache="private, max-age=86400, immutable")

    def _local_meta(self, sid: str) -> dict:
        meta = self.app.manager.get_meta(sid)
        if not self.app.preview.is_local(meta):
            raise ApiError(403, "只有本机会话可以访问本机文件（服务器会话只提供复制路径）", "not_local")
        return meta

    def _preview_call(self, fn):
        try:
            return fn()
        except ccr_preview.PreviewError as e:
            raise ApiError(e.status, e.message) from None

    def _local_info(self, sid: str, body: dict):
        meta = self.app.manager.get_meta(sid)
        pv = self.app.preview
        local = pv.is_local(meta)
        return {"local": local, "cwd": pv.cwd_abs(meta.get("cwd")) if local else None,
                "home": pv.session_home() if local else None}

    def _act_local_stat(self, sid: str, body: dict):
        meta = self._local_meta(sid)
        return self._preview_call(lambda: self.app.preview.stat(sid, meta, body.get("paths")))

    def _act_local_open(self, sid: str, body: dict):
        meta = self._local_meta(sid)
        return self._preview_call(lambda: self.app.preview.open(meta, body.get("path"), body.get("action")))

    def _serve_preview(self, method: str, path: str):
        if method not in ("GET", "HEAD"):
            raise ApiError(405, "预览地址只支持 GET/HEAD")
        rest = path[len("/preview/"):]
        cap, _, tail = rest.partition("/")
        mgr = self.app.manager
        try:
            status, headers, body = self.app.preview.serve(cap, tail, mgr.get_meta, self.headers)
        except ccr_preview.PreviewError as e:
            raise ApiError(e.status, e.message) from None
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)
    # -- 本机：系统"选择文件夹"对话框（SPEC §13） ---------------------------------
    def _choose_folder(self, body: dict) -> dict:
        """弹出 macOS 自带的选择文件夹对话框（osascript choose folder），阻塞到用户选择 / 取消（≤10 分钟）。
        只在用户点击时由前端调用；默认目录只经 osascript 的 argv 传入。"""
        d = body.get("default")
        if d is not None and not isinstance(d, str):
            raise ApiError(400, "default 必须是字符串")
        try:
            return ccr_macdialog.choose_folder(d, cfg=self.app.config_store.get())
        except ccr_macdialog.Unavailable as e:
            raise ApiError(501, str(e), "unavailable") from None
        except ccr_macdialog.Busy as e:
            raise ApiError(409, str(e), "dialog_busy") from None
        except ccr_macdialog.DialogError as e:
            raise ApiError(504 if e.timeout else 502, str(e), "timeout" if e.timeout else "dialog_error") from None

    # -- 提示音 --------------------------------------------------------------
    def _notify_info(self) -> dict:
        """设置面板用：可选的系统提示音、默认值、本机 CLI 的 Stop hook 是否已经会 afplay（只读检查，供说明文字）。"""
        return {"sounds": ccr_notify.list_sounds(), "default_sound": ccr_notify.DEFAULT_SOUND,
                "local_stop_hook": bool(ccr_notify.local_stop_hook_has_afplay()),
                "available": callable(getattr(getattr(self.app.manager, "notifier", None), "test", None))}

    def _notify_test(self, body: dict) -> dict:
        """「试听」：立即播放一次（sound 不给 = 当前设置；只接受 /System/Library/Sounds 下真实存在的文件名）。"""
        fn = getattr(getattr(self.app.manager, "notifier", None), "test", None)
        if not callable(fn):
            raise ApiError(503, "提示音不可用")
        sound = body.get("sound")
        if sound is not None and not isinstance(sound, str):
            raise ApiError(400, "sound 必须是字符串")
        try:
            return {"ok": True, "sound": fn(sound or None)}
        except ValueError as e:
            raise ApiError(400, str(e) or "无法试听") from None

    # -- 会话操作 ------------------------------------------------------------
    def _sid(self, sid: str) -> str:
        sid = urllib.parse.unquote(sid)
        if not SID_RE.match(sid):
            raise ApiError(404, "会话不存在")
        return sid

    def _meta(self, sid: str) -> dict:
        sid = self._sid(sid)
        try:
            return self.app.manager.get_meta(sid)
        except KeyError:
            raise ApiError(404, "会话不存在") from None

    def _subagents(self, sid: str, agent_id: Optional[str]) -> dict:
        """GET /api/sessions/<sid>/subagents（摘要列表）与 /subagents/<agent_id>（该子代理全部已缓存条目）。"""
        mgr = self.app.manager
        fn = getattr(mgr, "list_subagents" if agent_id is None else "get_subagent", None)
        if not callable(fn):
            raise ApiError(404, "接口不存在")
        if agent_id is not None:
            agent_id = urllib.parse.unquote(agent_id)
            if not AGENT_ID_RE.match(agent_id):
                raise ApiError(404, "子代理不存在")
        try:
            res = fn(sid) if agent_id is None else fn(sid, agent_id)
        except KeyError:
            raise ApiError(404, "会话不存在") from None
        if res is None:
            raise ApiError(404, "子代理不存在")
        return res

    def _call(self, fn: Callable, sid: str, body: dict):
        try:
            return fn(sid, body)
        except ApiError:
            raise
        except KeyError:
            raise ApiError(404, "会话不存在") from None
        except ValueError as e:
            raise ApiError(400, str(e) or "参数不合法") from None
        except RuntimeError as e:  # 含 ccr.session.OpError / LinkNotReady（带 http_status、api_code）
            status = getattr(e, "http_status", 409)
            if not isinstance(status, int) or not 400 <= status <= 599:
                status = 409
            raise ApiError(status, str(e) or "当前状态不允许该操作", getattr(e, "api_code", None)) from None
        except OSError as e:
            raise ApiError(502, "操作失败：%s" % (e.strerror or e)) from None

    def _update_route(self, method: str, rest: list):
        """dmg 版自动更新（SPEC §14.11）：GET /api/update；POST /api/update/{check,settings,snooze,install,cancel,
        dismiss,ack,open_dmg}。所有动作都只来自用户点击（检查本身由后端定时做，只读网络）。"""
        from ccr.updater import UpdateError
        upd = self.app.updater
        if method == "GET" and not rest:
            return self._send_json(200, upd.status())
        if method != "POST" or len(rest) != 1:
            raise ApiError(404, "接口不存在")
        body = self._read_json()
        act = rest[0]
        try:
            if act == "check":
                r = upd.check_now()
            elif act == "settings":
                v = body.get("auto_check")
                if not isinstance(v, bool):
                    raise ApiError(400, "auto_check 必须是 true/false")
                r = upd.set_auto(v)
            elif act == "snooze":
                r = upd.snooze(body.get("version"))
            elif act == "install":
                r = upd.install()
            elif act == "cancel":
                r = upd.cancel()
            elif act == "dismiss":
                r = upd.dismiss()
            elif act == "ack":
                r = upd.ack()
            elif act == "open_dmg":
                r = upd.open_dmg()
            else:
                raise ApiError(404, "接口不存在")
        except (UpdateError, ValueError) as e:
            raise ApiError(409, str(e) or "操作失败") from None
        return self._send_json(200, r)

    def _create(self, body: dict) -> dict:
        """新建会话。claude 启动参数只取请求体里用户明确选择的值（红线 §0.2：不选就不加，不套用配置默认）。"""
        cfg = self.app.config()
        kwargs = {}
        jobid = body.get("jobid")
        if jobid is None or jobid == "":
            jobid = cfg.get("jobid") or ""
        if isinstance(jobid, int) and not isinstance(jobid, bool):
            jobid = str(jobid)
        if not isinstance(jobid, str):
            raise ApiError(400, "jobid 必须是字符串")
        if not jobid:
            raise ApiError(400, "未设置 jobid：请先在左上角填写当前作业 jobid")
        kwargs["jobid"] = jobid.strip()   # 作业 jobid，或 "local" = 本机（SPEC §9；由 session 校验）
        # 不给 cwd：服务器用 config 的 default_cwd；本机用 ~（default_cwd 是服务器上的目录）
        local = kwargs["jobid"] == "local"
        kwargs["cwd"] = _opt_str(body, "cwd") or ("~" if local else cfg.get("default_cwd") or "~")
        mode = _opt_str(body, "permission_mode", 64)
        if mode and mode not in V2_PERMISSION_MODES:
            raise ApiError(400, "permission_mode 不合法（可选：%s，或留空）" % "/".join(V2_PERMISSION_MODES))
        kwargs["permission_mode"] = mode or None
        kwargs["model"] = (_opt_str(body, "model", 128) or "").strip() or None
        effort = _opt_str(body, "effort", 16)
        if effort and effort not in EFFORT_LEVELS:
            raise ApiError(400, "effort 不合法（可选：%s，或留空）" % "/".join(EFFORT_LEVELS))
        kwargs["effort"] = effort or None
        kwargs["skip_permissions"] = _opt_bool(body, "skip_permissions")
        kwargs["resume"] = _opt_str(body, "resume", 64) or None
        kwargs["title"] = _opt_str(body, "title", 200) or None
        kid = _api_key_id(body)
        if kid:   # 不选 = 服务器默认（settings.json），与以前完全一致：不加 --settings
            kwargs["api_key_id"] = kid
        return self._call(lambda _s, _b: self.app.manager.create(**kwargs), "", {})

    def _act_message(self, sid: str, body: dict):
        text = body.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ApiError(400, "text 必须是字符串")
        if body.get("images"):
            raise ApiError(400, "交互模式暂不支持图片附件")
        if not text.strip():
            raise ApiError(400, "消息不能为空")
        if len(text) > MAX_TEXT:
            raise ApiError(400, "消息过长（最多 %d 字符）" % MAX_TEXT)
        if "\x00" in text:
            raise ApiError(400, "消息不能包含 NUL 字符")
        return self.app.manager.send_message(sid, text, clear_input=_opt_bool(body, "clear_input"))

    def _act_keys(self, sid: str, body: dict):
        keys = body.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ApiError(400, "keys 必须是非空数组")
        if len(keys) > MAX_KEYS:
            raise ApiError(400, "一次最多发送 %d 个按键" % MAX_KEYS)
        for k in keys:
            if not isinstance(k, str) or k not in KEYS_WHITELIST:
                raise ApiError(400, "不允许的按键：%s" % str(k)[:20])
        return self.app.manager.send_keys(sid, list(keys))

    def _act_choose(self, sid: str, body: dict):
        index = _req_int(body, "index")
        text = body.get("text")
        if text is not None:
            if not isinstance(text, str) or not text.strip():
                raise ApiError(400, "text 必须是非空字符串")
            if len(text) > MAX_TEXT or "\x00" in text:
                raise ApiError(400, "text 不合法")
        kwargs = {}
        if text is not None:
            kwargs["text"] = text
        # expect：前端渲染卡片时的对话框指纹；与最新屏幕上的对话框不一致 → 409 dialog_changed（不按任何键）
        expect = body.get("expect")
        if expect is not None:
            if not isinstance(expect, dict):
                raise ApiError(400, "expect 必须是对话框指纹对象")
            kwargs["expect"] = expect
        return self.app.manager.choose(sid, index, **kwargs)

    def _act_cancel_dialog(self, sid: str, body: dict):
        return self.app.manager.cancel_dialog(sid)

    def _act_interrupt(self, sid: str, body: dict):
        self.app.manager.interrupt(sid)
        return {"ok": True}

    def _act_mode_cycle(self, sid: str, body: dict):
        return self.app.manager.cycle_mode(sid)

    def _act_model_picker(self, sid: str, body: dict):
        return self.app.manager.open_model_picker(sid, clear_input=_opt_bool(body, "clear_input"))

    @staticmethod
    def _scope(body: dict) -> str:
        scope = body.get("scope", "session")
        if scope is None:
            scope = "session"
        if scope not in ("session", "save"):
            raise ApiError(400, "scope 必须是 session 或 save")
        return scope

    def _act_model(self, sid: str, body: dict):
        index = _req_int(body, "index")
        effort = body.get("effort")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ApiError(400, "effort 不合法（可选：%s）" % "/".join(EFFORT_LEVELS))
        return self.app.manager.apply_model(sid, index, effort=effort, scope=self._scope(body))

    def _act_effort(self, sid: str, body: dict):
        level = body.get("level")
        if level not in EFFORT_LEVELS:
            raise ApiError(400, "level 必须是 %s 之一" % "/".join(EFFORT_LEVELS))
        return self.app.manager.apply_effort(sid, level, self._scope(body),
                                             clear_input=_opt_bool(body, "clear_input"))

    def _act_mode(self, sid: str, body: dict):
        """切到指定权限模式（Shift+Tab 逐步切换并确认，SPEC §13）。"""
        mode = body.get("mode")
        if mode not in V2_PERMISSION_MODES:
            raise ApiError(400, "mode 必须是 %s 之一" % "/".join(V2_PERMISSION_MODES))
        return self.app.manager.set_mode(sid, mode)

    def _act_restart(self, sid: str, body: dict):
        jobid = body.get("jobid")
        if isinstance(jobid, int) and not isinstance(jobid, bool):
            jobid = str(jobid)
        if jobid is not None and not isinstance(jobid, str):
            raise ApiError(400, "jobid 必须是字符串")
        kwargs = {"jobid": (jobid.strip() or None) if jobid else None}
        if _opt_bool(body, "force"):   # 旧作业连不上（409 link_down）时用户确认后强制
            kwargs["force"] = True
        if "api_key_id" in body:   # 不带 = 沿用当前 API；null/"" = 服务器默认；id = 改用该 key（换 API = 续接重启）
            kwargs["api_key_id"] = _api_key_id(body)
        mode = body.get("mode")
        if mode is not None:   # 覆盖权限启动参数（模式选择栏里"需重启"的模式）
            if mode not in V2_PERMISSION_MODES:
                raise ApiError(400, "mode 必须是 %s 之一" % "/".join(V2_PERMISSION_MODES))
            kwargs["mode"] = mode
        res = self.app.manager.restart(sid, **kwargs)
        self.app.preview.forget(sid)   # 重启后作业/cwd 可能变了：旧的预览 cap 作废（前端下次 stat 重新领取）
        return res

    def _with_api_keys(self, res):
        """历史列表：给每个 claude 会话附上它上次用的 API（session_keys.json；没有记录则不加）。"""
        fn = getattr(self.app.manager, "api_key_for_claude_session", None)
        if not callable(fn) or not isinstance(res, dict) or not isinstance(res.get("sessions"), list):
            return res
        out = []
        for it in res["sessions"]:
            if isinstance(it, dict):
                k = fn(it.get("claude_session_id"))
                if k is not None:
                    it = dict(it, api_key=k)
            out.append(it)
        return dict(res, sessions=out)

    # -- API key（明文只进不出：GET 只返回 id/name/掩码/base_url） -------------
    def _api_keys_route(self, method: str, rest: List[str], force: bool, force_refresh: bool = False):
        store = self.app.api_keys
        mgr = self.app.manager
        users = getattr(mgr, "api_key_users", None)

        def in_use(kid):
            return users(kid) if callable(users) else []

        if len(rest) > 1:
            raise ApiError(404, "接口不存在")
        if rest == ["usage"]:   # 各 key 近 7 天用量（本地后端直接查网关；明文 key 不出本进程）
            if method != "GET":
                raise ApiError(405, "不支持的方法")
            return self._send_json(200, {"usage": self.app.usage.get_all(refresh=force_refresh)})
        kid = None
        if rest:
            kid = urllib.parse.unquote(rest[0])
            if not ccr_api_keys.ID_RE.match(kid):
                raise ApiError(404, "API key 不存在")
        try:
            if method == "GET" and kid is None:
                keys = [dict(k, in_use=len(in_use(k["id"]))) for k in store.list_public()]
                return self._send_json(200, {"keys": keys})
            if method == "POST" and kid is None:
                body = self._read_json(64 * 1024)
                pub = store.add(body.get("name"), body.get("key"), body.get("base_url"),
                                is_server_default=_opt_bool(body, "is_server_default"))
                return self._send_json(200, {"api_key": dict(pub, in_use=0)})
            if method == "PATCH" and kid is not None:
                body = self._read_json(64 * 1024)
                kw = {}
                if "name" in body:
                    kw["name"] = body.get("name")
                    if kw["name"] is None:
                        raise ValueError("名称不能为空")
                if "base_url" in body:   # null / "" = 改回默认地址（服务器 settings.json）
                    kw["base_url"] = body.get("base_url")
                if not kw:
                    raise ValueError("没有要修改的字段（name / base_url）")
                pub = store.update(kid, **kw)
                return self._send_json(200, {"api_key": dict(pub, in_use=len(in_use(kid)))})
            if method == "DELETE" and kid is not None:
                self._read_json(64 * 1024)
                store.public(kid)   # 不存在 → 404
                u = in_use(kid)
                if u and not force:
                    titles = "、".join("「%s」" % x.get("title", "") for x in u[:5]) + ("等" if len(u) > 5 else "")
                    raise ApiError(409, "有 %d 个会话在用这个 API key（%s）。删除后已在运行的 claude 不受影响，"
                                        "但这些会话重启/续接时需要改选其它 API key。" % (len(u), titles),
                                   "api_key_in_use")
                store.delete(kid)
                return self._send_json(200, {"ok": True, "in_use": len(u)})
        except ccr_api_keys.ApiKeyNotFound:
            raise ApiError(404, "API key 不存在（可能已被删除）") from None
        except ccr_api_keys.ApiKeyConflict as e:
            raise ApiError(409, str(e), e.code) from None
        except ValueError as e:   # 校验失败：错误信息绝不回显 key
            raise ApiError(400, str(e) or "参数不合法") from None
        except OSError as e:
            raise ApiError(500, "保存 API key 失败：%s" % (e.strerror or type(e).__name__)) from None
        if kid is None and method in ("PATCH", "DELETE"):
            raise ApiError(404, "接口不存在")
        raise ApiError(405, "不支持的方法")

    def _act_close(self, sid: str, body: dict):
        if _opt_bool(body, "force"):
            self.app.manager.close(sid, force=True)
        else:
            self.app.manager.close(sid)
        return {"ok": True}

    def _act_remove(self, sid: str, body: dict):
        force = _opt_bool(body, "force")
        if force:
            self.app.manager.remove(sid, force=True)
        else:
            self.app.manager.remove(sid)
        self.app.preview.forget(sid)   # 删除会话：它的预览 cap 一并作废
        return {"ok": True}

    def _act_seen(self, sid: str, body: dict):
        """侧栏"新回复"已看：{"done_at": 客户端看到的 done_at}（数字，必填）。后端只往前推、不超过当前 done_at。"""
        v = body.get("done_at")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ApiError(400, "done_at 必须是数字")
        return self.app.manager.mark_seen(sid, v)

    def _act_rename(self, sid: str, body: dict):
        title = body.get("title")
        if not isinstance(title, str):
            raise ApiError(400, "标题必须是字符串（空字符串 = 改回使用 Claude Code 里的标题）")
        if len(title) > 200:
            raise ApiError(400, "标题过长（最多 200 字）")
        self.app.manager.rename(sid, title.strip())
        return {"ok": True}

    # -- SSE -----------------------------------------------------------------
    def _sse(self, since_q: Optional[str], instance_q: Optional[str] = None):
        """SSE：`id:`/`event:`/`data:`/`: ping`。

        先发 `event: reset`（随后从当前位置继续推送实时事件、不回放）的情形：
        - 请求带 instance 且与本进程 instance_id 不一致（服务器重启过）；
        - since 大于当前 gseq；
        - since 早于缓冲区中最早可回放事件（中间事件已被淘汰，存在缺口）。
        配置被外部修改时推送 `event: config`（data = {"config": {...}}，无 id）。
        """
        app = self.app
        mgr = app.manager
        since: Optional[int] = None
        for raw in (self.headers.get("Last-Event-ID"), since_q):
            if raw is None or str(raw).strip() == "":
                continue
            try:
                since = int(str(raw).strip())
            except ValueError:
                raise ApiError(400, "since / Last-Event-ID 必须是整数") from None
            if since < 0:
                raise ApiError(400, "since 不能为负数")
            break
        if instance_q is not None and len(instance_q) > 128:
            raise ApiError(400, "instance 不合法")

        sub = mgr.subscribe()
        try:
            cur = app.current_gseq()
            reason = None
            replay: list = []
            if instance_q not in (None, "") and not hmac.compare_digest(
                    instance_q.encode("utf-8", "replace"), app.instance_id.encode("utf-8")):
                reason = "instance_changed"
            elif since is not None and since > cur:
                reason = "gseq_reset"
            elif since is not None:
                rs = getattr(mgr, "replay_since", None)
                if callable(rs):
                    replay, gap = rs(since)
                else:
                    replay, gap = mgr.events_since(since), False
                if gap:
                    reason, replay = "gap", []
        except Exception:
            sub.close()
            raise
        eff = cur if (since is None or reason) else since
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._common_headers()
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            if reason:
                self.wfile.write(b"id: %d\nevent: reset\ndata: " % cur + _json_bytes(
                    {"reason": reason, "gseq": cur, "since": since, "instance_id": app.instance_id})
                    + b"\n\n")
            self.wfile.flush()
            last = eff
            for ev in replay:
                g = ev.get("gseq")
                if isinstance(g, int):
                    if g <= last:
                        continue
                    last = g
                self._sse_write(ev)
            last_write = time.monotonic()
            cfg_ver = app.config_version
            upd_ver = app.update_version
            while not app.stopping.is_set():
                if app.config_version != cfg_ver:
                    cfg_ver = app.config_version
                    self.wfile.write(b"event: config\ndata: " + _json_bytes({"config": app.config()}) + b"\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                if app.updater is not None and app.update_version != upd_ver:
                    upd_ver = app.update_version
                    self.wfile.write(b"event: update\ndata: " + _json_bytes({"update": app.updater.status()}) + b"\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                try:
                    ev = sub.get(timeout=1.0)
                except Exception as e:  # noqa  如 SubscriptionDropped（消费过慢）：断开让客户端带 since 重连
                    log.info("SSE 订阅结束：%s", e)
                    break
                now = time.monotonic()
                if ev is None:
                    if getattr(sub, "closed", False) is True:  # 订阅已被关闭（如 manager.shutdown）
                        break
                    if now - last_write >= SSE_PING_INTERVAL:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_write = now
                    continue
                g = ev.get("gseq")
                if isinstance(g, int):
                    if g <= last:
                        continue
                    last = g
                self._sse_write(ev)
                last_write = now
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout, OSError):
            pass
        finally:
            try:
                sub.close()
            except Exception:  # noqa
                pass

    def _sse_write(self, ev: dict):
        g = ev.get("gseq")
        buf = b""
        if isinstance(g, int):
            buf += b"id: %d\n" % g
        buf += b"data: " + _json_bytes(ev) + b"\n\n"
        self.wfile.write(buf)
        self.wfile.flush()

    # -- 静态文件 ------------------------------------------------------------
    def _serve_static(self, path: str):
        base = self.app.static_dir
        if path == "/" or path == "/index.html":
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
        elif path.startswith("/fixtures/") and self.app.fixtures_dir is not None:
            # 仅 --dev：供 static/dev/fixture.html 自测回放（需 X-CCR-Token）；只读、只允许 fixtures/ 顶层的 .ndjson/.jsonl
            rel = path[len("/fixtures/"):]
            if "/" in urllib.parse.unquote(rel) or not rel.endswith((".ndjson", ".jsonl")):
                raise ApiError(404, "未找到")
            base = self.app.fixtures_dir
        else:
            raise ApiError(404, "未找到")
        target = self._safe_static_path(rel, base)
        if target is None:
            raise ApiError(404, "未找到")
        try:
            with open(target, "rb") as f:
                data = f.read()
        except OSError:
            raise ApiError(404, "未找到") from None
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        extra = []
        if ctype.startswith("text/html"):
            extra.append(("Content-Security-Policy", CSP))
        self._send_bytes(200, data, ctype, extra, cache="no-cache")

    def _safe_static_path(self, rel: str, base: pathlib.Path) -> Optional[pathlib.Path]:
        rel = urllib.parse.unquote(rel)
        if not rel or "\x00" in rel or "\\" in rel or len(rel) > 1024:
            return None
        parts = rel.split("/")
        for p in parts:
            if p in ("", ".", "..") or p.startswith("."):
                return None
        try:
            target = (base / "/".join(parts)).resolve()
        except (OSError, RuntimeError):
            return None
        try:
            if os.path.commonpath([str(base), str(target)]) != str(base):
                return None
        except ValueError:
            return None
        if not target.is_file():
            return None
        return target
