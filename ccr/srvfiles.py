"""服务器会话的文件预览与下载（只读）——docs/SPEC_V2_TUI.md §10.10。

服务器会话（计算节点上的 claude）里 transcript 提到的文件，经该作业的 node agent 只读 op `file_stat` / `file_read`
（SPEC §2）取回，再按本机预览（ccr/preview.py，§10.8）**完全相同**的响应头 / CSP 交给前端现有的预览标签：

- 授权与地址：沿用本机预览的 cap 表与 `/preview/<cap>/<远端绝对路径>` 路径式地址（HTML 的相对资源照常加载）；
  cap 的根目录由 agent 按与本机相同的规则算出（会话 cwd，宽泛目录下只授权文件所在目录 / 文件本身），在表里带
  `srv:` 前缀，与本机 cap 互不通用；会话被删 / 重启时随 `preview.forget(sid)` 一起作废。
- 路径策略在 agent 端执行（远端文件系统只有它看得到）；本地再做一层字符串预检（dot 段、`..`、敏感目录），
  fake 模式（agent 就在本机）另按 (st_dev, st_ino) 复核本程序数据目录。
- 预览上限 20MB（整份读进内存，分块 2MB）；文本可只取开头（`?ccr_head=N`，≤4MB，前端只显示前 2MB）。
- 下载（`?download=1`，用户点「下载」时才请求）：上限 512MB，`Content-Disposition: attachment` + 文件名，
  先取到第一块再发响应头（失败能回正常的错误），之后按 2MB 一块顺序取、边取边写；中途文件变了 / 断线 → 断开连接
  （浏览器 / 原生窗口显示下载失败）。同一时间最多 2 个下载。
- 缓存：最近打开的完整文件（≤20MB，合计 64MB，10 分钟）；再次打开时把签名 (size, mtime_ns, dev, ino) 交给 agent，
  没变就不传内容（`same`），变了重新取。
- 并发：同时最多 4 个远端读取，排队超过 16 个回 503（沙箱页面里的脚本也拿得到自己的预览地址，防止刷爆作业链路）。
- 只读：不写任何文件；agent 端同样只读（SPEC §2）。
"""
from __future__ import annotations

import base64
import collections
import logging
import os
import threading
import time
import urllib.parse
from typing import Optional

from ccr import preview as ccr_preview
from ccr.preview import PreviewError

log = logging.getLogger("ccr.srvfiles")

SRV = "srv:"                         # cap 根目录前缀：服务器文件
PREVIEW_MAX = 20 * 1024 * 1024
DOWNLOAD_MAX = 512 * 1024 * 1024
CHUNK = 2 * 1024 * 1024
HEAD_MAX = 4 * 1024 * 1024
ANY_SIZE = 1 << 53                   # 只取开头时不限文件大小（agent 每次最多读 4MB）
STAT_TIMEOUT = 20.0
READ_TIMEOUT = 60.0
CACHE_BYTES = 64 * 1024 * 1024
CACHE_ITEMS = 64
CACHE_TTL = 600.0
MAX_ACTIVE = 4
MAX_WAITING = 16
MAX_DOWNLOADS = 2
MAX_STAT_PATHS = 200


def _mb(n: int) -> str:
    return "%gMB" % round(n / 1048576.0, 1)


def content_disposition(name: str) -> str:
    """attachment + ASCII 兜底文件名 + RFC 5987 的 UTF-8 文件名（原生窗口 / Chrome 都认）。"""
    name = name or "download"
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name).strip() or "download"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(name, safe=""))


class ServerFiles:
    def __init__(self, manager, preview: "ccr_preview.PreviewService", clock=time.monotonic):
        self.mgr = manager
        self.pv = preview
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()   # (sid, path) -> (sig, real, data, at)
        self._cache_bytes = 0
        self._slots = threading.BoundedSemaphore(MAX_ACTIVE)
        self._waiting = 0
        self._downloads = threading.BoundedSemaphore(MAX_DOWNLOADS)

    # ------------------------------------------------------------ 会话
    def applies(self, meta) -> bool:
        """服务器会话（不是本机会话；fake 模式下 jobid 不是 local 的会话也算，agent 是本机子进程）。"""
        return isinstance(meta, dict) and isinstance(meta.get("jobid"), str) and bool(meta.get("jobid")) \
            and not self.pv.is_local(meta)

    def _op(self, sid: str, op: str, timeout: float, **params):
        fn = getattr(self.mgr, "file_op", None)
        if not callable(fn):
            raise PreviewError(404, "接口不存在")
        return fn(sid, op, timeout=timeout, **params)

    def _local_check(self, real: str) -> Optional[str]:
        """本地再查一层：字符串预检（dot 段、敏感目录）；fake 模式（agent 在本机）另按身份查数据目录等。"""
        why = self.pv.denied(real)
        if why is None and self.pv.cfg().get("fake"):
            why = self.pv.denied_by_identity(real)
        return why

    def owns(self, cap: str) -> bool:
        hit = self.pv.lookup(cap)
        return bool(hit and isinstance(hit[1], str) and hit[1].startswith(SRV))

    def url_for(self, sid: str, root: str, real: str) -> str:
        return "/preview/%s%s" % (self.pv.cap_for(sid, SRV + root), urllib.parse.quote(real, safe="/"))

    # ------------------------------------------------------------ stat（POST /api/sessions/<sid>/srvfile/stat）
    def api_stat(self, sid: str, body: dict) -> dict:
        meta = self.mgr.get_meta(sid)
        if not self.applies(meta):
            raise PreviewError(403, "只有服务器会话用这个接口（本机会话走 /local/stat）")
        return self.stat(sid, meta, body.get("paths"))

    def stat(self, sid: str, meta: dict, paths) -> dict:
        if not isinstance(paths, list) or len(paths) > MAX_STAT_PATHS:
            raise PreviewError(400, "paths 必须是至多 %d 个路径的数组" % MAX_STAT_PATHS)
        cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
        res = self._op(sid, "file_stat", STAT_TIMEOUT, paths=[p if isinstance(p, str) else "" for p in paths], cwd=cwd)
        got = res.get("results") if isinstance(res, dict) else None
        if not isinstance(got, list) or len(got) != len(paths):
            raise PreviewError(502, "agent 返回的结果不完整")
        out = []
        for p, r in zip(paths, got):
            out.append(self._entry(sid, p, r if isinstance(r, dict) else {}))
        return {"results": out, "remote": True, "preview_max": PREVIEW_MAX, "download_max": DOWNLOAD_MAX}

    def _entry(self, sid: str, path, r: dict) -> dict:
        e = {"path": path if isinstance(path, str) else None, "abs": None, "real": None, "exists": False, "type": None,
             "size": None, "mtime": None, "ok": False, "kind": None, "url": None, "download_url": None,
             "reason": r.get("reason") if isinstance(r.get("reason"), str) else None, "openable": False,
             "remote": True, "readable": False, "too_large": False, "downloadable": False}
        ab, real = r.get("abs"), r.get("real")
        if isinstance(ab, str) and ab.startswith("/"):
            e["abs"] = ab
        if not r.get("exists") or not isinstance(real, str) or not real.startswith("/"):
            if not e["reason"]:
                e["reason"] = "文件不存在"
            return e
        why = self._local_check(real)
        if why:
            e["reason"] = why
            return e
        e.update(exists=True, real=real, mtime=r.get("mtime") if isinstance(r.get("mtime"), (int, float)) else None)
        if r.get("type") == "dir":
            e["type"] = "dir"
            return e
        root, size = r.get("root"), r.get("size")
        if r.get("type") != "file" or not isinstance(size, int) or not isinstance(root, str) or not root.startswith("/"):
            e.update(exists=False, reason="不预览设备/管道等特殊文件")
            return e
        e.update(type="file", size=size, kind=ccr_preview.kind_of(real), readable=bool(r.get("readable")))
        if not e["readable"]:
            e["reason"] = "没有读取权限"
            return e
        url = self.url_for(sid, root, real)
        e.update(ok=True, url=url, too_large=size > PREVIEW_MAX, downloadable=size <= DOWNLOAD_MAX)
        if e["downloadable"]:
            e["download_url"] = url + "?download=1"
        if e["too_large"]:
            e["reason"] = "文件较大（%s），超过服务器文件预览上限 %s" % (_mb(size), _mb(PREVIEW_MAX))
        return e

    # ------------------------------------------------------------ 读取
    def _slot(self):
        with self._lock:
            if self._waiting >= MAX_WAITING:
                raise PreviewError(503, "服务器文件请求过多，请稍后重试")
            self._waiting += 1
        if not self._slots.acquire(timeout=READ_TIMEOUT):
            with self._lock:
                self._waiting -= 1
            raise PreviewError(503, "服务器文件请求过多，请稍后重试")

    def _unslot(self):
        self._slots.release()
        with self._lock:
            self._waiting -= 1

    def _read_chunk(self, sid, root, path, offset, length, max_size, sig=None) -> dict:
        try:
            r = self._op(sid, "file_read", READ_TIMEOUT, path=path, root=root, offset=offset, length=length,
                         max_size=max_size, **({"sig": sig} if sig else {}))
        except KeyError:
            raise PreviewError(404, "会话不存在") from None
        except RuntimeError as e:   # session.OpError / LinkNotReady：连接断开、超时、作业已结束……
            st = getattr(e, "http_status", 502)
            raise PreviewError(st if isinstance(st, int) and 400 <= st <= 599 else 502, str(e) or "读取失败") from None
        if not isinstance(r, dict):
            raise PreviewError(502, "agent 返回的结果不合法")
        if "status" in r:
            st = r.get("status")
            why = r.get("reason") if isinstance(r.get("reason"), str) else "读取失败"
            if st == 413:
                why = ("文件过大（服务器文件预览上限 %s），可以点「下载」保存到本机" % _mb(max_size) if max_size <= PREVIEW_MAX
                       else "文件过大（服务器文件下载上限 %s），请用 scp / rsync" % _mb(max_size))
            raise PreviewError(st if isinstance(st, int) and 400 <= st <= 599 else 502, why)
        real, size, rsig = r.get("real"), r.get("size"), r.get("sig")
        if not isinstance(real, str) or not isinstance(size, int) or not isinstance(rsig, list):
            raise PreviewError(502, "agent 返回的结果不合法")
        why = self._local_check(real)
        if why:
            raise PreviewError(403, why)
        if not r.get("same"):
            try:
                r["bytes"] = base64.b64decode(r.get("data") or "", validate=True)
            except (ValueError, TypeError):
                raise PreviewError(502, "agent 返回的数据不合法") from None
        return r

    def _cache_get(self, key):
        with self._lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            if self.clock() - hit[3] > CACHE_TTL:
                self._cache.pop(key)
                self._cache_bytes -= len(hit[2])
                return None
            self._cache.move_to_end(key)
            return hit

    def _cache_put(self, key, sig, real, data):
        if len(data) > PREVIEW_MAX:
            return
        with self._lock:
            old = self._cache.pop(key, None)
            if old is not None:
                self._cache_bytes -= len(old[2])
            self._cache[key] = (sig, real, data, self.clock())
            self._cache_bytes += len(data)
            while self._cache and (self._cache_bytes > CACHE_BYTES or len(self._cache) > CACHE_ITEMS):
                _k, v = self._cache.popitem(last=False)
                self._cache_bytes -= len(v[2])

    def read(self, sid: str, root: str, path: str, head: int = 0):
        """→ (real, size, data, partial)。head>0：只要开头 head 字节（文本预览）；否则整份（≤ 预览上限）。"""
        key = (sid, path)
        hit = self._cache_get(key)
        mx = ANY_SIZE if head else PREVIEW_MAX
        self._slot()
        try:
            for attempt in (0, 1):
                first = self._read_chunk(sid, root, path, 0, min(CHUNK, head) if head else CHUNK, mx,
                                         hit[0] if hit and attempt == 0 else None)
                size, sig = first["size"], first["sig"]
                if first.get("same") and hit is not None:
                    data = hit[2]
                    with self._lock:
                        if key in self._cache:
                            self._cache[key] = (hit[0], hit[1], hit[2], self.clock())
                    want = min(size, head) if head else size
                    return first["real"], size, data[:want], want < size
                want = min(size, head) if head else size
                parts, got = [first["bytes"]], len(first["bytes"])
                changed = False
                while got < want:
                    r = self._read_chunk(sid, root, path, got, min(CHUNK, want - got), mx)
                    if r["sig"] != sig or not r["bytes"]:
                        changed = True
                        break
                    parts.append(r["bytes"])
                    got += len(r["bytes"])
                if changed:
                    hit = None
                    continue
                data = b"".join(parts)[:want]
                if want == size:
                    self._cache_put(key, sig, first["real"], data)
                return first["real"], size, data, want < size
            raise PreviewError(409, "文件在读取过程中发生了变化，请重试")
        finally:
            self._unslot()

    # ------------------------------------------------------------ GET|HEAD /preview/<cap>/…（服务器文件）
    def _target(self, cap: str, rest: str):
        hit = self.pv.lookup(cap)
        if hit is None or not hit[1].startswith(SRV):
            raise PreviewError(404, "预览地址无效或已过期（app 重启后需重新打开）")
        sid, root = hit[0], hit[1][len(SRV):]
        try:
            meta = self.mgr.get_meta(sid)
        except KeyError:
            self.pv.forget(sid)
            raise PreviewError(404, "会话不存在") from None
        if not self.applies(meta):
            raise PreviewError(404, "该会话已不是服务器会话")
        try:
            path = urllib.parse.unquote(rest, errors="strict") if rest else ""
        except UnicodeDecodeError:
            raise PreviewError(400, "路径不合法") from None
        path = "/" + path.lstrip("/")
        if any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise PreviewError(400, "路径不合法")
        if any(seg in (".", "..") for seg in path.split("/")):
            raise PreviewError(403, "路径不合法")
        why = self.pv.denied(path)
        if why:
            raise PreviewError(403, why)
        return sid, root, path

    def serve(self, handler, method: str, cap: str, rest: str, query: dict) -> None:
        sid, root, path = self._target(cap, rest)
        q = lambda k: (query.get(k) or [""])[0]   # noqa: E731
        if q("download") == "1":
            return self._download(handler, method, sid, root, path)
        head = 0
        if q("ccr_head"):
            try:
                head = max(1, min(HEAD_MAX, int(q("ccr_head"))))
            except ValueError:
                raise PreviewError(400, "ccr_head 必须是整数") from None
        real, size, data, partial = self.read(sid, root, path, head)
        ctype, body, extra = ccr_preview.text_body(real, data, utf8=q("ccr_utf8") == "1", partial=partial)
        hdrs = ccr_preview.response_headers(ctype, handler.headers) + extra + [("X-CCR-Size", str(size))]
        if partial:
            hdrs.append(("X-CCR-Partial", "1"))
        self._write(handler, method, 200, hdrs, body)

    @staticmethod
    def _write(handler, method, status, headers, body: bytes) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            handler.send_header(k, v)
        if handler.close_connection:
            handler.send_header("Connection", "close")
        handler.end_headers()
        if method != "HEAD":
            handler.wfile.write(body)

    def _download(self, handler, method, sid, root, path) -> None:
        if not self._downloads.acquire(blocking=False):
            raise PreviewError(503, "同时最多下载 %d 个服务器文件，请等前面的下载完成" % MAX_DOWNLOADS)
        try:
            self._slot()
            try:
                hit = self._cache_get((sid, path))
                first = self._read_chunk(sid, root, path, 0, CHUNK, DOWNLOAD_MAX, hit[0] if hit else None)
            finally:
                self._unslot()
            size, sig, real = first["size"], first["sig"], first["real"]
            body0 = hit[2] if first.get("same") and hit else first["bytes"]
            hdrs = [(k, v) for k, v in ccr_preview.response_headers("application/octet-stream", handler.headers)
                    if k != "Content-Disposition"]
            hdrs += [("Content-Disposition", content_disposition(os.path.basename(real))), ("X-CCR-Size", str(size))]
            handler.send_response(200)
            handler.send_header("Content-Length", str(size))
            for k, v in hdrs:
                handler.send_header(k, v)
            handler.close_connection = True   # 中途失败时只能断开连接；下载完也不复用这条连接
            handler.send_header("Connection", "close")
            handler.end_headers()
            if method == "HEAD":
                return
            handler.wfile.write(body0)
            got = len(body0)
            while got < size:
                self._slot()
                try:
                    r = self._read_chunk(sid, root, path, got, min(CHUNK, size - got), DOWNLOAD_MAX)
                except PreviewError as e:
                    log.warning("服务器文件下载中断（%s）：%s", e.status, e.message)
                    return
                finally:
                    self._unslot()
                if r["sig"] != sig or not r["bytes"]:
                    log.warning("服务器文件下载中断：文件在下载过程中发生了变化")
                    return
                handler.wfile.write(r["bytes"])
                got += len(r["bytes"])
        finally:
            self._downloads.release()
