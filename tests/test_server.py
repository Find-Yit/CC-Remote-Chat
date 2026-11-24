"""ccr.server / ccr.config 测试（桩 SessionManager + 桩 HistoryService，不连远端）。"""

import http.client
import json
import os
import pathlib
import queue
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from ccr import config as C
from ccr import server as S
from ccr.history import HistoryError

try:  # 优先使用开发者 A 的真实校验函数；尚未就绪时用最小桩
    import ccr.remote  # noqa: F401
    _REMOTE_STUB = None
except ImportError:  # pragma: no cover
    import re
    import types

    _REMOTE_STUB = types.SimpleNamespace(
        validate_jobid=lambda s: s if re.fullmatch(r"\d{1,12}(_\d{1,8})?", s) else (_ for _ in ()).throw(
            ValueError("无效的 jobid")),
        validate_model=lambda s: (s or None) if not s or re.fullmatch(r"[A-Za-z0-9._:\-\[\]]{1,80}", s) else (
            _ for _ in ()).throw(ValueError("无效的模型")),
        validate_permission_mode=lambda s: s if s in C.PERMISSION_MODES + ["default"] else (
            _ for _ in ()).throw(ValueError("无效的权限模式")),
        validate_path=lambda s: s if (s.startswith("/") or s == "~" or s.startswith("~/")) and "\n" not in s else (
            _ for _ in ()).throw(ValueError("路径不合法")),
    )

UUID = "285bd6a9-08c3-46e0-b111-3fe228694c59"


def patch_remote(test):
    if _REMOTE_STUB is not None:  # pragma: no cover
        p = mock.patch.object(C, "_remote", return_value=_REMOTE_STUB)
        p.start()
        test.addCleanup(p.stop)


# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------
class StubSub:
    def __init__(self, mgr):
        self.q = queue.Queue()
        self.mgr = mgr
        self.closed = False

    def get(self, timeout=None):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True
        with self.mgr.lock:
            if self in self.mgr.subs:
                self.mgr.subs.remove(self)


class StubManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.subs = []
        self.metas = {}
        self.calls = []
        self.gseq = 0
        self.n = 0
        self.floor = 0
        self.truncated = set()
        self.restart_error = None

    # 测试辅助
    def emit(self, sid, data, kind="app"):
        with self.lock:
            self.gseq += 1
            ev = {"gseq": self.gseq, "sid": sid, "ts": time.time(), "kind": kind, "data": data}
            self.events.append(ev)
            subs = list(self.subs)
        for s in subs:
            s.q.put(ev)
        return ev

    def _need(self, sid):
        if sid not in self.metas:
            raise KeyError(sid)

    # SessionManager 接口
    def subscribe(self):
        s = StubSub(self)
        with self.lock:
            self.subs.append(s)
        return s

    def events_since(self, gseq):
        with self.lock:
            return [e for e in self.events if e["gseq"] > gseq and e["gseq"] >= self.floor]

    def replay_since(self, gseq):
        """floor = 缓冲中最早可回放的 gseq（模拟淘汰）。"""
        with self.lock:
            gap = gseq < self.gseq and gseq < self.floor - 1
        return self.events_since(gseq), gap

    def session_events_ex(self, sid):
        return self.session_events(sid), sid in self.truncated

    def session_events(self, sid):
        self._need(sid)
        with self.lock:
            return [e for e in self.events if e["sid"] == sid]

    def list_sessions(self):
        return [dict(m) for m in self.metas.values()]

    def get_meta(self, sid):
        self._need(sid)
        return dict(self.metas[sid])

    def create(self, *, jobid, cwd, title=None, model=None, permission_mode=None, skip_permissions=False,
               effort=None, resume=None):
        self.calls.append(("create", dict(jobid=jobid, cwd=cwd, title=title, model=model,
                                          permission_mode=permission_mode, skip_permissions=skip_permissions,
                                          effort=effort, resume=resume)))
        if getattr(self, "create_error", None) is not None:
            raise self.create_error
        if not jobid.isdigit():
            raise ValueError("无效的 jobid")
        self.n += 1
        sid = "s%d" % self.n
        meta = {"sid": sid, "title": title or "新会话", "jobid": jobid, "cwd": cwd,
                "launch": {"model": model, "permission_mode": permission_mode,
                           "skip_permissions": skip_permissions, "effort": effort},
                "claude_session_id": resume, "claude_pid": None, "state": "starting", "status": None,
                "mode": None, "busy": False, "dialog_kind": None, "created_at": time.time(),
                "last_activity": time.time(), "error": None, "link": "connecting"}
        self.metas[sid] = meta
        self.emit(sid, {"type": "meta", "meta": dict(meta)})
        return dict(meta)

    def _act(self, name, sid, *args):
        self._need(sid)
        self.calls.append((name, sid) + args)
        err = self.errors.pop(name, None) if hasattr(self, "errors") else None
        if err is not None:
            raise err

    def send_message(self, sid, text, clear_input=False):
        self._act("send_message", sid, text, clear_input)
        return {"ok": True, "local_id": "L1"}

    def send_keys(self, sid, keys):
        self._act("send_keys", sid, keys)
        return {"ok": True}

    def choose(self, sid, index, text=None, expect=None):
        if expect is not None:
            self.expects = getattr(self, "expects", []) + [expect]
        self._act("choose", sid, index, text)
        return {"ok": True, "state": {"dialog": None}}

    def cancel_dialog(self, sid):
        self._act("cancel_dialog", sid)
        return {"ok": True, "state": {"dialog": None}}

    def cycle_mode(self, sid):
        self._act("cycle_mode", sid)
        return {"ok": True, "mode": "plan", "mode_line": "⏸ plan mode on"}

    def open_model_picker(self, sid, clear_input=False):
        self._act("open_model_picker", sid, clear_input)
        return {"rows": [{"index": 0, "n": 1, "name": "Haiku", "desc": "d", "current": True}], "cursor": 0,
                "effort": {"level": "low", "label": "Low effort", "is_default": False}}

    def apply_model(self, sid, index, effort=None, scope="session"):
        self._act("apply_model", sid, index, effort, scope)
        return {"ok": True, "result": "Set model to Haiku", "state": {}}

    def apply_effort(self, sid, level, scope="session", clear_input=False):
        self._act("apply_effort", sid, level, scope, clear_input)
        return {"ok": True, "result": "Set effort level to %s" % level, "state": {}}

    def get_screen(self, sid):
        self._act("get_screen", sid)
        return {"text": "screen", "state": {"busy": False}, "ts": 1.0}

    def interrupt(self, sid):
        self._act("interrupt", sid)
        return {"ok": True}

    def restart(self, sid, jobid=None, force=False):
        self._act("restart", sid, jobid)
        if force:
            self.calls.append(("restart_force", sid))
        if jobid:
            self.metas[sid]["jobid"] = jobid
        return dict(self.metas[sid])

    def close(self, sid, force=False):
        self._act("close", sid)
        if force:
            self.calls.append(("close_force", sid))
        self.metas[sid]["state"] = "exited"
        return {"ok": True}

    def remove(self, sid, force=False):
        self._act("remove", sid, force)
        del self.metas[sid]
        return {"ok": True}

    def rename(self, sid, title):
        self._need(sid)
        self.calls.append(("rename", sid, title))
        self.metas[sid]["title"] = title

    def mark_seen(self, sid, done_at):
        self._act("mark_seen", sid, done_at)
        if not isinstance(done_at, (int, float)) or not 0 < done_at < 1e11:   # 与 SessionManager 相同的范围校验
            raise ValueError("done_at 必须是有效的时间戳（秒）")
        return {"ok": True, "done_at": 1.7e9, "seen_at": done_at, "unread": False}

    def mark_unseen(self, sid):
        self._act("mark_unseen", sid)
        return {"ok": True, "done_at": 1.7e9, "seen_at": 1.7e9 - 0.001, "unread": True}

    def reorder(self, sids):
        if not isinstance(sids, list):
            raise ValueError("sids 必须是会话 id 列表")
        self.calls.append(("reorder", sids))
        return {"ok": True, "changed": len(sids)}

    def shutdown(self):
        self.calls.append(("shutdown",))


class StubHistory:
    def __init__(self):
        self.slow = threading.Event()
        self.release = threading.Event()

    def list_jobs(self):
        return {"jobs": [{"jobid": "1", "state": "RUNNING", "nodes": "n", "name": "b", "elapsed": "1", "left": "2"}]}

    def list_dirs(self, path, hidden=False):
        if path == "/missing":
            raise HistoryError(404, "目录不存在")
        return {"path": path or "/home/u", "parent": "/", "dirs": ["a"] + ([".h"] if hidden else [])}

    def list_history(self, limit=100):
        if self.slow.is_set():
            self.release.wait(10)
        return {"sessions": [{"claude_session_id": UUID, "limit": limit}]}

    def read_history(self, uid):
        if uid != UUID:
            raise HistoryError(400, "会话 id 不合法")
        return {"claude_session_id": UUID, "cwd": "/x", "entries": []}


# ---------------------------------------------------------------------------
# 客户端辅助
# ---------------------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        patch_remote(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = pathlib.Path(self.tmp.name)
        self.static = d / "static"
        (self.static / "dev").mkdir(parents=True)
        (self.static / "index.html").write_text("<!doctype html><title>ccr</title>", encoding="utf-8")
        (self.static / "app.js").write_text("console.log(1)", encoding="utf-8")
        (self.static / "dev" / "fixture.html").write_text("<p>dev</p>", encoding="utf-8")
        (self.static / ".secret").write_text("hidden", encoding="utf-8")
        (d / "outside.txt").write_text("TOP-SECRET", encoding="utf-8")
        os.symlink(str(d / "outside.txt"), str(self.static / "link.txt"))
        self.cfg_path = d / "data" / "config.json"
        self.store = C.ConfigStore(self.cfg_path)
        self.store.update({"jobid": "276403"})
        self.mgr = StubManager()
        self.hist = StubHistory()
        self.srv = S.CCRServer(self.mgr, self.store, self.hist, static_dir=self.static, port=0)
        self.srv.start()
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.port
        self.host = "127.0.0.1:%d" % self.port
        self.tok = self.srv.issue_token()

    def login(self, code):
        return self.req("POST", "/api/login", {"code": code}, auth=False)

    def req(self, method, path, body=None, headers=None, auth=True, raw_body=None, host=None):
        h = {"Host": host or self.host}
        if auth:
            h["X-CCR-Token"] = self.tok
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        if data is not None and "Content-Length" not in h:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        if data:
            conn.send(data)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            js = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            js = None
        return r.status, js, r, raw

    def assertErr(self, res, status):
        st, js, _, raw = res
        self.assertEqual(st, status, raw[:300])
        self.assertIsInstance(js, dict)
        self.assertIn("error", js)
        self.assertTrue(any("一" <= ch <= "鿿" for ch in js["error"]), js["error"])


# ---------------------------------------------------------------------------
class TestOpErrors(Base):
    """session.OpError → http_status + code；LinkNotReady → 503；普通 RuntimeError → 409 无 code。"""

    def test_error_mapping(self):
        from ccr.session import LinkNotReady, OpError
        sid = self.mgr.create(jobid="276403", cwd="/x")["sid"]
        self.mgr.errors = {"send_message": OpError("请先处理当前对话框", code="dialog_open")}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "hi"})
        self.assertEqual((st, js), (409, {"error": "请先处理当前对话框", "code": "dialog_open"}))
        self.mgr.errors = {"choose": LinkNotReady()}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/choose" % sid, {"index": 0})
        self.assertEqual(st, 503)
        self.assertEqual(js["code"], "link_not_ready")
        self.mgr.errors = {"open_model_picker": OpError("远端操作失败：x", code="remote_error", status=502)}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/model_picker" % sid, {})
        self.assertEqual((st, js["code"]), (502, "remote_error"))
        self.mgr.errors = {"restart": RuntimeError("服务正在关闭")}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/restart" % sid, {})
        self.assertEqual((st, js), (409, {"error": "服务正在关闭"}))
        self.mgr.errors = {"send_keys": OpError("会话已结束", code="exited")}
        self.assertErr(self.req("POST", "/api/sessions/%s/keys" % sid, {"keys": ["Enter"]}), 409)
        self.mgr.create_error = OpError("该 Claude 会话已在本地会话「x」中运行，请先关闭它", code="already_running")
        st, js, _, _ = self.req("POST", "/api/sessions", {"resume": UUID})
        self.assertEqual((st, js["code"]), (409, "already_running"))


class TestAuth(Base):
    def test_login_body_limit(self):
        """未认证的 /api/login 请求体上限 4KB：超限直接 413，不读取请求体。"""
        code = self.srv.new_login_code()
        big = json.dumps({"code": code, "pad": "x" * 5000}).encode()
        self.assertErr(self.req("POST", "/api/login", raw_body=big, auth=False,
                                headers={"Content-Type": "application/json"}), 413)
        # 只声明很大的 Content-Length、不真正发送：服务器不应等待读取
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/login", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", self.host)
        conn.putheader("Content-Length", str(10 * 1024 * 1024))
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 413)
        r.read()
        conn.close()
        # 码未被消耗，正常大小仍可登录
        self.assertEqual(self.login(code)[0], 200)

    def test_login_code_one_time(self):
        code = self.srv.new_login_code()
        self.assertGreaterEqual(len(code), 43)
        st, js, r, _ = self.login(code)
        self.assertEqual(st, 200)
        self.assertEqual(set(js), {"token"})
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        tok = js["token"]
        self.assertNotEqual(tok, code)
        st, boot, _, _ = self.req("GET", "/api/bootstrap", auth=False, headers={"X-CCR-Token": tok})
        self.assertEqual(st, 200)
        # 第二次兑换失败
        st, js, _, _ = self.login(code)
        self.assertEqual((st, js["error"]), (401, "登录码无效或已过期，请从应用重新打开窗口"))
        # 多个会话 token 同时有效
        self.assertEqual(self.req("GET", "/api/config")[0], 200)

    def test_login_code_expired(self):
        code = self.srv.new_login_code()
        with mock.patch.object(S.time, "monotonic", return_value=time.monotonic() + S.LOGIN_CODE_TTL + 1):
            self.assertErr(self.login(code), 401)
        self.assertErr(self.login(code), 401)  # 过期后即作废
        code2 = self.srv.new_login_code()
        with mock.patch.object(S.time, "monotonic", return_value=time.monotonic() + S.LOGIN_CODE_TTL - 5):
            self.assertEqual(self.login(code2)[0], 200)

    def test_login_bad_inputs(self):
        for body in ({"code": "wrong"}, {"code": ""}, {}, {"code": 5}, {"code": None}, {"code": "x" * 1000}):
            self.assertErr(self.req("POST", "/api/login", body, auth=False), 401)
        self.assertErr(self.req("GET", "/api/login", auth=False), 405)
        self.assertErr(self.req("POST", "/api/login", raw_body=b"{bad", auth=False), 400)

    def test_login_checks_host_origin_fetch_site(self):
        code = self.srv.new_login_code()
        self.assertErr(self.req("POST", "/api/login", {"code": code}, auth=False, host="evil.com:%d" % self.port), 403)
        self.assertErr(self.req("POST", "/api/login", {"code": code}, auth=False,
                                headers={"Origin": "http://evil.com"}), 403)
        self.assertErr(self.req("POST", "/api/login", {"code": code}, auth=False,
                                headers={"Sec-Fetch-Site": "same-site"}), 403)
        # 被拒绝的请求不消耗登录码
        self.assertEqual(self.req("POST", "/api/login", {"code": code}, auth=False,
                                  headers={"Origin": "http://" + self.host, "Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_login_code_limit(self):
        codes = [self.srv.new_login_code() for _ in range(S.MAX_LOGIN_CODES + 3)]
        self.assertErr(self.login(codes[0]), 401)  # 最旧的被挤掉
        self.assertEqual(self.login(codes[-1])[0], 200)

    def test_no_header_rejected(self):
        for m, p, b in (("GET", "/api/bootstrap", None), ("GET", "/api/stream", None), ("GET", "/api/config", None),
                        ("GET", "/api/sessions", None), ("GET", "/api/jobs", None), ("GET", "/api/history", None),
                        ("POST", "/api/sessions", {}), ("POST", "/api/config", {"jobid": "1"}),
                        ("DELETE", "/api/sessions/s1", None), ("PATCH", "/api/sessions/s1", {"title": "x"}),
                        ("GET", "/api/nope", None)):
            st, js, _, _ = self.req(m, p, b, auth=False)
            self.assertEqual(st, 401, (m, p))
            self.assertIn("重新打开窗口", js["error"])
        self.assertEqual(self.mgr.calls, [])

    def test_cookie_not_accepted(self):
        for ck in ("ccr_token=" + self.tok, "ccr_token=" + self.srv.new_login_code()):
            self.assertErr(self.req("GET", "/api/bootstrap", auth=False, headers={"Cookie": ck}), 401)
            self.assertErr(self.req("POST", "/api/sessions", {}, auth=False, headers={"Cookie": ck}), 401)
        # 正确的头 + 任意 Cookie：Cookie 被忽略
        st, _, r, _ = self.req("GET", "/api/bootstrap", headers={"Cookie": "ccr_token=zzz"})
        self.assertEqual(st, 200)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertEqual(self.mgr.calls, [])

    def test_wrong_or_code_as_token_rejected(self):
        code = self.srv.new_login_code()
        for bad in ("x" * 43, code, self.tok + "x", self.tok[:-1], " ", "\u00e9" * 3):
            self.assertErr(self.req("GET", "/api/bootstrap", auth=False, headers={"X-CCR-Token": bad}), 401)
        self.assertEqual(self.login(code)[0], 200)  # 登录码不能当 token 用，也未被消耗

    def test_old_login_url_gone(self):
        st, _, r, raw = self.req("GET", "/?t=" + self.tok, auth=False)
        self.assertEqual(st, 200)  # 只是普通的 index.html
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertEqual(raw, b"<!doctype html><title>ccr</title>")

    def test_login_url_uses_fragment(self):
        url = self.srv.new_login_url()
        self.assertTrue(url.startswith("http://127.0.0.1:%d/#code=" % self.port), url)
        code = url.split("#code=", 1)[1]
        self.assertEqual(self.login(code)[0], 200)
        self.assertNotEqual(self.srv.new_login_url(), url)

    def test_static_needs_no_auth(self):
        self.assertEqual(self.req("GET", "/", auth=False)[0], 200)
        self.assertEqual(self.req("GET", "/static/app.js", auth=False)[0], 200)

    def test_sec_fetch_site(self):
        for v in ("cross-site", "same-site", "Same-Site", "bogus", ""):
            for m, p, b in (("GET", "/api/bootstrap", None), ("GET", "/api/stream", None),
                            ("POST", "/api/sessions", {}), ("GET", "/api/config", None), ("GET", "/", None)):
                self.assertErr(self.req(m, p, b, headers={"Sec-Fetch-Site": v}), 403)
        for v in ("same-origin", "none"):
            self.assertEqual(self.req("GET", "/api/config", headers={"Sec-Fetch-Site": v})[0], 200)
        self.assertEqual(self.mgr.calls, [])

    def test_bad_host_rejected(self):
        for h in ("evil.com:%d" % self.port, "127.0.0.1:%d" % (self.port + 1), "127.0.0.1",
                  "attacker.127.0.0.1.nip.io:%d" % self.port, ""):
            self.assertErr(self.req("GET", "/api/bootstrap", host=h or " "), 403)
        self.assertEqual(self.req("GET", "/api/bootstrap", host="localhost:%d" % self.port)[0], 200)

    def test_cross_origin_rejected(self):
        for o in ("http://evil.com", "null", "http://127.0.0.1:%d" % (self.port + 1),
                  "https://127.0.0.1:%d" % self.port):
            self.assertErr(self.req("POST", "/api/sessions/s1/interrupt", {}, headers={"Origin": o}), 403)
            self.assertErr(self.req("GET", "/api/bootstrap", headers={"Origin": o}), 403)
            self.assertErr(self.req("GET", "/api/stream", headers={"Origin": o}), 403)
        st, _, _, _ = self.req("GET", "/api/config", headers={"Origin": "http://" + self.host})
        self.assertEqual(st, 200)
        self.assertErr(self.req("GET", "/api/config", headers={"Origin": "http://" + self.host,
                                                                 "Sec-Fetch-Site": "cross-site"}), 403)

    def test_body_limit(self):
        st, js, _, _ = self.req("POST", "/api/config", raw_body=b"",
                                headers={"Content-Length": str(S.MAX_BODY + 1)})
        self.assertEqual(st, 413)
        self.assertIn("32MB", js["error"])

    def test_bad_json(self):
        self.assertErr(self.req("POST", "/api/config", raw_body=b"{not json"), 400)
        self.assertErr(self.req("POST", "/api/config", raw_body=b"[1,2]"), 400)

    def test_unknown_and_options(self):
        self.assertErr(self.req("GET", "/api/nope"), 404)
        self.assertErr(self.req("OPTIONS", "/api/bootstrap"), 405)
        self.assertErr(self.req("PUT", "/api/config", {}), 405)

    def test_security_headers(self):
        st, _, r, _ = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.httpd.server_address[0], "127.0.0.1")

    def test_port_in_use_increments(self):
        blocker = socket.socket()
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        self.addCleanup(blocker.close)
        p = blocker.getsockname()[1]
        srv2 = S.CCRServer(self.mgr, self.store, self.hist, static_dir=self.static, port=p, port_tries=5)
        self.addCleanup(srv2.shutdown)
        self.assertNotEqual(srv2.port, p)
        self.assertTrue(p < srv2.port <= p + 4)
        self.assertNotEqual(srv2.instance_id, self.srv.instance_id)
        self.assertFalse(srv2.check_token(self.tok))  # token 不跨实例


class TestStatic(Base):
    def test_index_and_files(self):
        st, _, r, raw = self.req("GET", "/")
        self.assertEqual((st, raw), (200, b"<!doctype html><title>ccr</title>"))
        self.assertTrue(r.getheader("Content-Type").startswith("text/html"))
        st, _, r, raw = self.req("GET", "/static/app.js")
        self.assertEqual(st, 200)
        self.assertIn("javascript", r.getheader("Content-Type"))
        self.assertEqual(self.req("GET", "/static/dev/fixture.html")[3], b"<p>dev</p>")

    def test_traversal_blocked(self):
        bad = ["/static/../outside.txt", "/static/%2e%2e/outside.txt", "/static/..%2foutside.txt",
               "/static/dev/../../outside.txt", "/static/dev/%2e%2e/%2e%2e/outside.txt",
               "/static//etc/passwd", "/static/%2fetc%2fpasswd", "/static/.secret", "/static/link.txt",
               "/static/app.js%00.png", "/static/..\\outside.txt", "/static/dev", "/static/", "/outside.txt",
               "/static/%252e%252e/outside.txt", "/../outside.txt"]
        for p in bad:
            st, js, _, raw = self.req("GET", p)
            self.assertIn(st, (400, 404), p)
            self.assertNotIn(b"TOP-SECRET", raw, p)
            self.assertNotIn(b"hidden", raw, p)

    def test_raw_socket_traversal(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(("GET /static/../../outside.txt HTTP/1.1\r\nHost: %s\r\nX-CCR-Token: %s\r\nConnection: close\r\n\r\n"
                   % (self.host, self.tok)).encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        self.assertNotIn(b"TOP-SECRET", data)
        self.assertTrue(data.startswith(b"HTTP/1.1 404"), data[:80])


class TestFixturesRoute(Base):
    def test_fixtures_disabled_by_default(self):
        self.assertErr(self.req("GET", "/fixtures/tui_screens_keys.ndjson"), 404)
        self.assertErr(self.req("GET", "/fixtures/tui_screens_keys.ndjson", auth=False), 404)

    def test_fixtures_served_for_dev_page(self):
        self.srv.shutdown()
        self.srv = S.CCRServer(self.mgr, self.store, self.hist, static_dir=self.static, port=0, dev=True)
        self.srv.start()
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.port
        self.host = "127.0.0.1:%d" % self.port
        self.tok = self.srv.issue_token()
        st, _, r, raw = self.req("GET", "/fixtures/tui_screens_keys.ndjson")
        self.assertEqual(st, 200)
        self.assertTrue(raw.startswith(b"{"))
        for p in ("/fixtures/../ccr/server.py", "/fixtures/%2e%2e%2fccr%2fserver.py", "/fixtures/x/../a.ndjson",
                  "/fixtures/nope.ndjson", "/fixtures/", "/fixtures/..%2f..%2fetc%2fpasswd.jsonl"):
            self.assertIn(self.req("GET", p)[0], (400, 404), p)
        self.assertEqual(self.req("GET", "/fixtures/tui_screens_dialogs.ndjson", auth=False)[0], 401)
        self.assertEqual(self.req("GET", "/fixtures/tui_screens_dialogs.ndjson", auth=False,
                                  headers={"Cookie": "ccr_token=" + self.tok})[0], 401)


class TestApi(Base):
    def create(self, **body):
        st, js, _, raw = self.req("POST", "/api/sessions", body)
        self.assertEqual(st, 200, raw)
        return js

    def test_bootstrap(self):
        self.create(title="t")
        st, js, _, _ = self.req("GET", "/api/bootstrap")
        self.assertEqual(st, 200)
        self.assertEqual(js["instance_id"], self.srv.instance_id)
        self.assertRegex(js["instance_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(js["config"]["jobid"], "276403")
        self.assertEqual(len(js["sessions"]), 1)
        self.assertEqual(js["gseq"], self.mgr.gseq)
        self.assertEqual(js["permission_modes"], ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk",
                                                  "plan"])
        self.assertEqual(js["effort_levels"], ["low", "medium", "high", "xhigh", "max"])
        self.assertIn("BTab", js["keys"])
        self.assertNotIn("C-d", js["keys"])
        self.assertEqual(set(js), {"instance_id", "config", "sessions", "gseq", "permission_modes", "effort_levels",
                                   "keys"})

    def test_runtime_config_not_exposed(self):
        store = C.ConfigStore(self.cfg_path, overrides={"control_dir": "/x/ssh", "fake": True})
        self.srv.config_store = store
        for path in ("/api/config", "/api/bootstrap"):
            js = self.req("GET", path)[1]
            cfg = js if path == "/api/config" else js["config"]
            self.assertNotIn("control_dir", cfg)
            self.assertTrue(cfg["fake"])
        js = self.req("POST", "/api/config", {"jobid": "5"})[1]
        self.assertNotIn("control_dir", js)
        self.assertNotIn("control_dir", json.loads(self.cfg_path.read_text()))
        self.assertErr(self.req("POST", "/api/config", {"control_dir": "/tmp/evil"}), 400)

    def test_events_truncated_flag(self):
        sid = self.create()["sid"]
        js = self.req("GET", "/api/sessions/%s/events" % sid)[1]
        self.assertEqual(set(js), {"events", "truncated"})
        self.assertFalse(js["truncated"])
        self.mgr.truncated.add(sid)
        self.assertTrue(self.req("GET", "/api/sessions/%s/events" % sid)[1]["truncated"])

    def test_create_defaults_from_config(self):
        # v2：只有 jobid/cwd 用配置默认；claude 参数只取请求体（红线：不选就不加）
        self.store.update({"default_cwd": "~/work", "default_model": "opus", "default_permission_mode": "plan",
                           "default_effort": "high"})
        meta = self.create()
        self.assertEqual(meta["sid"], "s1")
        self.assertEqual(self.mgr.calls[-1], ("create", dict(
            jobid="276403", cwd="~/work", title=None, model=None, permission_mode=None, skip_permissions=False,
            effort=None, resume=None)))
        self.create(jobid="999", cwd="/abs/dir", permission_mode="manual", model=" sonnet ", resume=UUID, title="续",
                    effort="xhigh", skip_permissions=True)
        self.assertEqual(self.mgr.calls[-1], ("create", dict(
            jobid="999", cwd="/abs/dir", title="续", model="sonnet", permission_mode="manual",
            skip_permissions=True, effort="xhigh", resume=UUID)))
        self.create(jobid=12, model="", permission_mode="", effort="")
        self.assertEqual(self.mgr.calls[-1][1]["jobid"], "12")
        self.assertIsNone(self.mgr.calls[-1][1]["permission_mode"])

    def test_create_errors(self):
        self.assertErr(self.req("POST", "/api/sessions", {"jobid": "1;rm -rf ~"}), 400)
        self.assertErr(self.req("POST", "/api/sessions", {"cwd": 5}), 400)
        for bad in ({"permission_mode": "default"}, {"permission_mode": "yolo"}, {"effort": "huge"},
                    {"skip_permissions": "true"}, {"skip_permissions": 1}, {"model": 5}, {"title": ["x"]}):
            self.assertErr(self.req("POST", "/api/sessions", bad), 400)
        self.store.update({"jobid": ""})
        self.assertErr(self.req("POST", "/api/sessions", {}), 400)

    def test_session_actions(self):
        sid = self.create()["sid"]
        P = "/api/sessions/%s/" % sid
        st, js, _, _ = self.req("POST", P + "message", {"text": "你好\n第二行"})
        self.assertEqual((st, js), (200, {"ok": True, "local_id": "L1"}))
        self.assertEqual(self.mgr.calls[-1], ("send_message", sid, "你好\n第二行", False))
        self.req("POST", P + "message", {"text": "x", "clear_input": True})
        self.assertEqual(self.mgr.calls[-1], ("send_message", sid, "x", True))
        for bad in ({"text": ""}, {"text": "  "}, {"text": 5}, {"text": "a\x00b"}, {"text": "x", "clear_input": "1"},
                    {"text": "x", "images": [{"media_type": "image/png", "data": "iVBORw0KGgo="}]},
                    {"text": "x" * 200001}):
            self.assertErr(self.req("POST", P + "message", bad), 400)

        self.assertEqual(self.req("POST", P + "keys", {"keys": ["Up", "Enter", "BTab", "C-c", "1", "s"]})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("send_keys", sid, ["Up", "Enter", "BTab", "C-c", "1", "s"]))
        for bad in ({}, {"keys": []}, {"keys": "Enter"}, {"keys": ["Enter", "rm -rf"]}, {"keys": ["C-d"]},
                    {"keys": ["a"]}, {"keys": [1]}, {"keys": ["Enter"] * 41}, {"keys": ["Enter; ls"]}):
            self.assertErr(self.req("POST", P + "keys", bad), 400)

        st, js, _, _ = self.req("POST", P + "choose", {"index": 2})
        self.assertEqual(st, 200)
        self.assertEqual(self.mgr.calls[-1], ("choose", sid, 2, None))
        self.req("POST", P + "choose", {"index": 2, "text": "绿色"})
        self.assertEqual(self.mgr.calls[-1], ("choose", sid, 2, "绿色"))
        for bad in ({}, {"index": "1"}, {"index": -1}, {"index": True}, {"index": 1.5}, {"index": 5000},
                    {"index": 0, "text": ""}, {"index": 0, "text": 3}):
            self.assertErr(self.req("POST", P + "choose", bad), 400)

        self.assertEqual(self.req("POST", P + "cancel_dialog")[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("cancel_dialog", sid))
        self.assertEqual(self.req("POST", P + "interrupt")[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("interrupt", sid))
        st, js, _, _ = self.req("POST", P + "mode/cycle")
        self.assertEqual((st, js["mode"]), (200, "plan"))
        self.assertEqual(self.mgr.calls[-1], ("cycle_mode", sid))

        st, js, _, _ = self.req("POST", P + "model_picker", {})
        self.assertEqual(st, 200)
        self.assertEqual(set(js), {"rows", "cursor", "effort"})
        self.assertEqual(self.mgr.calls[-1], ("open_model_picker", sid, False))
        self.req("POST", P + "model_picker", {"clear_input": True})
        self.assertEqual(self.mgr.calls[-1], ("open_model_picker", sid, True))

        st, js, _, _ = self.req("POST", P + "model", {"index": 3, "effort": "high", "scope": "session"})
        self.assertEqual((st, js["result"]), (200, "Set model to Haiku"))
        self.assertEqual(self.mgr.calls[-1], ("apply_model", sid, 3, "high", "session"))
        self.req("POST", P + "model", {"index": 0, "scope": "save"})
        self.assertEqual(self.mgr.calls[-1], ("apply_model", sid, 0, None, "save"))
        self.req("POST", P + "model", {"index": 0, "effort": None})
        self.assertEqual(self.mgr.calls[-1], ("apply_model", sid, 0, None, "session"))
        for bad in ({}, {"model": "sonnet"}, {"index": 0, "effort": "ultra"}, {"index": 0, "scope": "global"},
                    {"index": "0"}):
            self.assertErr(self.req("POST", P + "model", bad), 400)

        st, js, _, _ = self.req("POST", P + "effort", {"level": "max", "scope": "save"})
        self.assertEqual(st, 200)
        self.assertEqual(self.mgr.calls[-1], ("apply_effort", sid, "max", "save", False))
        self.req("POST", P + "effort", {"level": "low"})
        self.assertEqual(self.mgr.calls[-1], ("apply_effort", sid, "low", "session", False))
        for bad in ({}, {"level": "LOW"}, {"level": "low", "scope": "x"}, {"level": "low", "clear_input": 1}):
            self.assertErr(self.req("POST", P + "effort", bad), 400)

        st, js, _, _ = self.req("GET", P + "screen")
        self.assertEqual((st, js["text"]), (200, "screen"))
        self.assertEqual(self.mgr.calls[-1], ("get_screen", sid))

        # 已删除的 v1 接口
        for path, body in (("permission", {"request_id": "r1", "behavior": "allow"}), ("set_model", {"model": "x"})):
            self.assertErr(self.req("POST", P + path, body), 404)
        # v1 的 mode（{"permission_mode"}）已删除；§13 的 mode 接口是 {"mode": X}：旧写法 400
        self.assertErr(self.req("POST", P + "mode", {"permission_mode": "plan"}), 400)

        st, js, _, _ = self.req("POST", P + "restart", {"jobid": "300000"})
        self.assertEqual((st, js["jobid"]), (200, "300000"))
        self.assertEqual(self.mgr.calls[-1], ("restart", sid, "300000"))
        self.req("POST", P + "restart", {})
        self.assertEqual(self.mgr.calls[-1], ("restart", sid, None))
        self.assertErr(self.req("POST", P + "restart", {"jobid": ["1"]}), 400)

        self.assertEqual(self.req("PATCH", "/api/sessions/%s" % sid, {"title": " 新名字 "})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("rename", sid, "新名字"))
        # 空标题 = 改回使用 Claude Code 里的标题
        self.assertEqual(self.req("PATCH", "/api/sessions/%s" % sid, {"title": "  "})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("rename", sid, ""))
        self.assertErr(self.req("PATCH", "/api/sessions/%s" % sid, {"title": None}), 400)
        self.assertErr(self.req("PATCH", "/api/sessions/%s" % sid, {"title": "x" * 201}), 400)
        # 侧栏手动排序
        self.assertEqual(self.req("POST", "/api/sessions/order", {"sids": [sid]})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("reorder", [sid]))
        self.assertErr(self.req("POST", "/api/sessions/order", {"sids": "x"}), 400)

        st, js, _, _ = self.req("GET", "/api/sessions/%s/events" % sid)
        self.assertEqual(st, 200)
        self.assertEqual(js["events"][0]["data"]["type"], "meta")

        self.assertEqual(self.req("POST", P + "close")[0], 200)
        self.assertEqual(self.mgr.metas[sid]["state"], "exited")
        self.assertEqual(self.req("DELETE", "/api/sessions/%s" % sid)[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("remove", sid, False))
        self.assertNotIn(sid, self.mgr.metas)
        sid2 = self.create()["sid"]
        self.assertErr(self.req("DELETE", "/api/sessions/%s" % sid2, {"force": "yes"}), 400)
        self.assertEqual(self.req("DELETE", "/api/sessions/%s?force=1" % sid2)[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("remove", sid2, True))

    def test_choose_expect_and_force_options(self):
        """中 4：choose 透传 expect 指纹，dialog_changed → 409；高 1：close/restart 的 force、job_ended/link_down → 409。"""
        from ccr.session import OpError
        sid = self.create()["sid"]
        P = "/api/sessions/%s/" % sid
        fp = {"kind": "permission", "title": "Bash command", "question": "Do you want to proceed?",
              "preview": ["touch /tmp/x"], "labels": ["Yes", "No"]}
        self.assertEqual(self.req("POST", P + "choose", {"index": 1, "expect": fp})[0], 200)
        self.assertEqual(self.mgr.expects[-1], fp)
        self.assertErr(self.req("POST", P + "choose", {"index": 1, "expect": "x"}), 400)
        self.mgr.errors = {"choose": OpError("对话框已变化，请重新确认", code="dialog_changed")}
        st, js, _, _ = self.req("POST", P + "choose", {"index": 1, "expect": fp})
        self.assertEqual((st, js["code"]), (409, "dialog_changed"))
        self.mgr.errors = {"close": OpError("连接已断开", code="link_down")}
        st, js, _, _ = self.req("POST", P + "close", {})
        self.assertEqual((st, js["code"]), (409, "link_down"))
        self.assertEqual(self.req("POST", P + "close", {"force": True})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("close_force", sid))
        self.assertErr(self.req("POST", P + "close", {"force": "yes"}), 400)
        self.mgr.errors = {"restart": OpError("作业 1 已结束", code="job_ended")}
        st, js, _, _ = self.req("POST", P + "restart", {})
        self.assertEqual((st, js["code"]), (409, "job_ended"))
        self.assertEqual(self.req("POST", P + "restart", {"jobid": "123456", "force": True})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("restart_force", sid))

    def test_new_endpoints_require_token(self):
        sid = self.create()["sid"]
        P = "/api/sessions/%s/" % sid
        n = len(self.mgr.calls)
        for m, path, body in (("POST", "keys", {"keys": ["Enter"]}), ("POST", "choose", {"index": 0}),
                              ("POST", "cancel_dialog", {}), ("POST", "mode/cycle", {}),
                              ("POST", "model_picker", {}), ("POST", "model", {"index": 0}),
                              ("POST", "effort", {"level": "low"}), ("GET", "screen", None),
                              ("POST", "message", {"text": "x"})):
            self.assertErr(self.req(m, P + path, body, auth=False), 401)
            self.assertErr(self.req(m, P + path, body, headers={"X-CCR-Token": "wrong"}, auth=False), 401)
            self.assertErr(self.req(m, P + path, body, headers={"Origin": "http://evil.com"}), 403)
            self.assertErr(self.req(m, P + path, body, headers={"Sec-Fetch-Site": "cross-site"}), 403)
            self.assertErr(self.req(m, P + path, body, host="evil.com:%d" % self.port), 403)
        self.assertEqual(len(self.mgr.calls), n)   # 一个都没有到达 manager

    def test_seen_unseen(self):
        """侧栏"新回复"：/seen {done_at} 与 /unseen；参数校验、鉴权、未知会话。"""
        sid = self.create()["sid"]
        P = "/api/sessions/%s/" % sid
        st, js, _, _ = self.req("POST", P + "seen", {"done_at": 1.7e9})
        self.assertEqual((st, js["unread"]), (200, False))
        self.assertEqual(self.mgr.calls[-1], ("mark_seen", sid, 1.7e9))
        n = len(self.mgr.calls)
        for bad in ({}, {"done_at": None}, {"done_at": "1"}, {"done_at": True}, {"done_at": [1]}):
            self.assertErr(self.req("POST", P + "seen", bad), 400)
        self.assertEqual(len(self.mgr.calls), n)               # 类型不对：没有到达 manager
        self.assertErr(self.req("POST", P + "seen", {"done_at": -1}), 400)   # 范围不对：manager 的 ValueError → 400
        st, js, _, _ = self.req("POST", P + "unseen", {})
        self.assertEqual((st, js["unread"]), (200, True))
        self.assertEqual(self.mgr.calls[-1], ("mark_unseen", sid))
        self.mgr.errors = {"mark_unseen": ValueError("这个会话还没有完成过回复，不能标为未读")}
        self.assertErr(self.req("POST", P + "unseen", {}), 400)
        n = len(self.mgr.calls)
        for path, body in (("seen", {"done_at": 1.7e9}), ("unseen", {})):
            self.assertErr(self.req("POST", P + path, body, auth=False), 401)
            self.assertErr(self.req("POST", P + path, body, headers={"X-CCR-Token": "wrong"}, auth=False), 401)
            self.assertErr(self.req("POST", P + path, body, headers={"Origin": "http://evil.com"}), 403)
            self.assertErr(self.req("POST", P + path, body, headers={"Sec-Fetch-Site": "cross-site"}), 403)
            self.assertErr(self.req("POST", "/api/sessions/zz/" + path, body), 404)
        self.assertErr(self.req("GET", P + "seen"), 404)
        self.assertEqual(len(self.mgr.calls), n)

    def test_unknown_session_404(self):
        for m, p, b in (("GET", "/api/sessions/zz/events", None), ("POST", "/api/sessions/zz/message", {"text": "x"}),
                        ("POST", "/api/sessions/zz/interrupt", {}), ("DELETE", "/api/sessions/zz", None),
                        ("PATCH", "/api/sessions/zz", {"title": "a"}), ("POST", "/api/sessions/..%2f/close", {}),
                        ("POST", "/api/sessions/s1/bogus", {}), ("GET", "/api/sessions/zz/screen", None),
                        ("POST", "/api/sessions/zz/keys", {"keys": ["Enter"]}),
                        ("POST", "/api/sessions/zz/choose", {"index": 0}),
                        ("POST", "/api/sessions/zz/mode/cycle", {}), ("POST", "/api/sessions/s1/mode/bogus", {}),
                        ("POST", "/api/sessions/zz/model_picker", {}), ("POST", "/api/sessions/zz/effort",
                                                                        {"level": "low"})):
            self.assertErr(self.req(m, p, b), 404)

    def test_config_get_post(self):
        st, js, _, _ = self.req("GET", "/api/config")
        self.assertEqual(st, 200)
        self.assertEqual(set(js), set(C.DEFAULT_CONFIG))
        st, js, _, _ = self.req("POST", "/api/config", {"jobid": "123456",
                                                        "extra_srun_args": "--gres=gpu:1  --cpus-per-task=4"})
        self.assertEqual(st, 200)
        self.assertEqual(js["jobid"], "123456")
        self.assertEqual(js["extra_srun_args"], "--gres=gpu:1 --cpus-per-task=4")
        on_disk = json.loads(self.cfg_path.read_text())
        self.assertEqual(on_disk["jobid"], "123456")
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg_path).st_mode), 0o600)
        for bad in ({"jobid": "1;rm -rf ~"}, {"extra_srun_args": "--x=$(id)"}, {"extra_srun_args": "; ls"},
                    {"extra_srun_args": "--output=/tmp/x"}, {"extra_srun_args": "-l"}, {"extra_srun_args": "-c4"},
                    {"extra_srun_args": "--chd=/tmp"}, {"extra_srun_args": "--task-prolog=x"},
                    {"host": "-oProxyCommand=evil"}, {"default_cwd": "relative"}, {"ssh_multiplex": "yes"},
                    {"default_permission_mode": "root"}, {"default_permission_mode": "default"},
                    {"default_effort": "huge"}, {"nope": 1}, {"default_model": "a b"}):
            self.assertErr(self.req("POST", "/api/config", bad), 400)
        self.assertEqual(json.loads(self.cfg_path.read_text())["jobid"], "123456")

    def test_remote_query_endpoints(self):
        self.assertEqual(self.req("GET", "/api/jobs")[1]["jobs"][0]["jobid"], "1")
        st, js, _, _ = self.req("GET", "/api/dirs?path=%2Fhome%2Fu&hidden=1")
        self.assertEqual((st, js["path"], js["dirs"]), (200, "/home/u", ["a", ".h"]))
        self.assertErr(self.req("GET", "/api/dirs?path=/missing"), 404)
        st, js, _, _ = self.req("GET", "/api/history?limit=7")
        self.assertEqual(js["sessions"][0]["limit"], 7)
        self.assertErr(self.req("GET", "/api/history?limit=abc"), 400)
        self.assertEqual(self.req("GET", "/api/history/" + UUID)[1]["claude_session_id"], UUID)
        self.assertErr(self.req("GET", "/api/history/not-a-uuid"), 400)

    def test_slow_remote_does_not_block(self):
        self.hist.slow.set()
        out = {}

        def slow():
            out["r"] = self.req("GET", "/api/history")

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        self.assertEqual(self.req("GET", "/api/bootstrap")[0], 200)
        self.assertEqual(self.req("POST", "/api/sessions/none/interrupt")[0], 404)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(t.is_alive())
        self.hist.release.set()
        t.join(10)
        self.assertEqual(out["r"][0], 200)


class TestSSE(Base):
    def open_stream(self, path="/api/stream", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host, "X-CCR-Token": self.tok, "Accept": "text/event-stream"}
        h.update(headers or {})
        conn.request("GET", path, headers=h)
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/event-stream"))
        return conn, r

    def read_event(self, r):
        """读到下一个完整的 data 事件，返回 (id, obj, 期间收到的注释行)。"""
        eid = None
        comments = []
        while True:
            line = r.fp.readline()
            if not line:
                raise EOFError
            line = line.decode("utf-8").rstrip("\n")
            if line.startswith(":"):
                comments.append(line)
            elif line.startswith("id: "):
                eid = int(line[4:])
            elif line.startswith("data: "):
                obj = json.loads(line[6:])
                assert r.fp.readline() == b"\n"
                return eid, obj, comments

    def test_since_replay_then_live(self):
        for i in range(5):
            self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "n%d" % i})
        conn, r = self.open_stream("/api/stream?since=2")
        got = [self.read_event(r)[0] for _ in range(3)]
        self.assertEqual(got, [3, 4, 5])
        self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "live"})
        eid, obj, _ = self.read_event(r)
        self.assertEqual(eid, 6)
        self.assertEqual(obj["data"]["text"], "live")
        self.assertEqual(set(obj), {"gseq", "sid", "ts", "kind", "data"})
        conn.close()

    def test_last_event_id(self):
        for i in range(4):
            self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "n%d" % i})
        conn, r = self.open_stream("/api/stream?since=0", headers={"Last-Event-ID": "3"})
        self.assertEqual(self.read_event(r)[0], 4)
        conn.close()

    def test_no_since_only_live_and_no_duplicates(self):
        self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "old"})
        conn, r = self.open_stream("/api/stream")
        time.sleep(0.2)
        self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "new"})
        eid, obj, _ = self.read_event(r)
        self.assertEqual((eid, obj["data"]["text"]), (2, "new"))
        conn.close()

    def test_since_larger_than_server_sends_reset(self):
        self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "a"})
        conn, r = self.open_stream("/api/stream", headers={"Last-Event-ID": "999"})
        lines = []
        while True:
            line = r.fp.readline().decode().rstrip("\n")
            if line == "" and lines and lines[-1].startswith("data: "):
                break
            if line:
                lines.append(line)
        self.assertIn("event: reset", lines)
        self.assertIn("id: 1", lines)
        data = json.loads([x for x in lines if x.startswith("data: ")][0][6:])
        self.assertEqual((data["reason"], data["gseq"], data["since"]), ("gseq_reset", 1, 999))
        # 之后继续推送实时事件（不重复回放旧事件）
        self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "b"})
        eid, obj, _ = self.read_event(r)
        self.assertEqual((eid, obj["data"]["text"]), (2, "b"))
        conn.close()

    def read_reset(self, r):
        lines = []
        while True:
            line = r.fp.readline().decode().rstrip("\n")
            if line == "" and lines and lines[-1].startswith("data: "):
                break
            if line:
                lines.append(line)
        self.assertIn("event: reset", lines)
        return lines, json.loads([x for x in lines if x.startswith("data: ")][0][6:])

    def test_instance_mismatch_sends_reset(self):
        for i in range(3):
            self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "n%d" % i})
        conn, r = self.open_stream("/api/stream?since=1&instance=deadbeefdeadbeef")
        lines, data = self.read_reset(r)
        self.assertIn("id: 3", lines)
        self.assertEqual((data["reason"], data["gseq"], data["since"], data["instance_id"]),
                         ("instance_changed", 3, 1, self.srv.instance_id))
        # reset 后不回放旧事件，只推送实时事件
        self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "live"})
        eid, obj, _ = self.read_event(r)
        self.assertEqual((eid, obj["data"]["text"]), (4, "live"))
        conn.close()
        # instance 一致 → 正常回放
        conn, r = self.open_stream("/api/stream?since=2&instance=" + self.srv.instance_id)
        self.assertEqual(self.read_event(r)[0], 3)
        conn.close()

    def test_gap_sends_reset(self):
        for i in range(10):
            self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "n%d" % i})
        self.mgr.floor = 6  # gseq 1..5 已被淘汰
        conn, r = self.open_stream("/api/stream?since=3&instance=" + self.srv.instance_id)
        lines, data = self.read_reset(r)
        self.assertEqual((data["reason"], data["gseq"], data["since"]), ("gap", 10, 3))
        self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "live"})
        self.assertEqual(self.read_event(r)[0], 11)
        conn.close()
        # since 恰好在缓冲头之前一位：无缺口
        conn, r = self.open_stream("/api/stream?since=5")
        self.assertEqual(self.read_event(r)[0], 6)
        conn.close()

    def test_stream_requires_header(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/stream?since=0", headers={"Host": self.host, "Cookie": "ccr_token=" + self.tok})
        r = conn.getresponse()
        self.assertEqual(r.status, 401)
        conn.close()

    def test_config_event_pushed(self):
        conn, r = self.open_stream("/api/stream")
        time.sleep(0.3)
        self.srv.notify_config_changed()
        lines = []
        deadline = time.time() + 5
        while time.time() < deadline:
            line = r.fp.readline().decode().rstrip("\n")
            if line:
                lines.append(line)
            if line.startswith("data: ") and "event: config" in lines:
                break
        self.assertIn("event: config", lines)
        data = json.loads(lines[-1][6:])
        self.assertEqual(data["config"]["jobid"], "276403")
        self.assertNotIn("control_dir", data["config"])
        conn.close()

    def test_bad_since(self):
        self.assertErr(self.req("GET", "/api/stream?since=abc"), 400)
        self.assertErr(self.req("GET", "/api/stream?since=-1"), 400)

    def test_ping_and_disconnect_cleanup(self):
        with mock.patch.object(S, "SSE_PING_INTERVAL", 0.3):
            conn, r = self.open_stream("/api/stream?since=0")
            time.sleep(0.2)
            self.assertEqual(len(self.mgr.subs), 1)
            self.mgr.emit("s1", {"type": "notice", "level": "info", "text": "x"})
            _, _, _ = self.read_event(r)
            # 等待 ping
            deadline = time.time() + 5
            ping = False
            while time.time() < deadline and not ping:
                line = r.fp.readline()
                ping = line.startswith(b": ping")
            self.assertTrue(ping)
            r.close()
            conn.close()
            deadline = time.time() + 5
            while time.time() < deadline and self.mgr.subs:
                time.sleep(0.1)
            self.assertEqual(self.mgr.subs, [])

    def test_shutdown_ends_stream(self):
        conn, r = self.open_stream("/api/stream?since=0")
        time.sleep(0.2)
        self.srv.shutdown()
        deadline = time.time() + 5
        while time.time() < deadline and self.mgr.subs:
            time.sleep(0.1)
        self.assertEqual(self.mgr.subs, [])
        conn.close()


try:
    from ccr.session import SessionManager as _RealManager
except ImportError:  # pragma: no cover
    _RealManager = None


def _fake_e2e_available():
    try:
        import ccr.agent_link  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    import shutil
    root = pathlib.Path(__file__).resolve().parent.parent
    return (_RealManager is not None and (root / "ccr" / "fake_tui.py").exists()
            and bool(shutil.which("tmux") or os.path.exists("/opt/homebrew/bin/tmux")))


@unittest.skipUnless(_fake_e2e_available(), "需要 ccr.session + ccr.agent_link + ccr/fake_tui.py + 本机 tmux")
class TestIntegrationFake(Base):
    """服务器 + 真实 SessionManager + 真实 AgentLink（fake 模式：本机 tmux + fake_tui，不连远端）。"""

    def setUp(self):
        import shutil
        import subprocess
        super().setUp()
        self.srv.shutdown()
        self.ftmp = tempfile.mkdtemp(prefix="ccr-srv-e2e-", dir="/tmp")   # tmux 套接字路径不能太长
        home = os.path.join(self.ftmp, "home")
        tdir = os.path.join(self.ftmp, "tmux")
        os.makedirs(os.path.join(home, "proj"))
        os.makedirs(tdir)
        tmux = shutil.which("tmux") or "/opt/homebrew/bin/tmux"

        def cleanup():
            subprocess.run([tmux, "-L", "ccr", "-f", "/dev/null", "kill-server"], env=dict(os.environ, TMUX_TMPDIR=tdir),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            shutil.rmtree(self.ftmp, True)
        self.addCleanup(cleanup)
        self.store = C.ConfigStore(self.cfg_path, overrides={"fake": True, "fake_home": home, "fake_tmpdir": tdir,
                                                             "fake_tmux": tmux})
        self.store.update({"jobid": "276403", "default_cwd": "~/proj"})
        self.mgr = _RealManager(self.store.get, pathlib.Path(self.tmp.name) / "data")
        self.addCleanup(self.mgr.shutdown)
        self.srv = S.CCRServer(self.mgr, self.store, self.hist, static_dir=self.static, port=0)
        self.srv.start()
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.port
        self.host = "127.0.0.1:%d" % self.port
        self.tok = self.login(self.srv.new_login_code())[1]["token"]

    def wait_state(self, sid, states, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            js = self.req("GET", "/api/sessions/%s" % sid)[1]
            if js["state"] in states:
                return js
            time.sleep(0.1)
        self.fail("状态未到达 %s：%s" % (states, self.req("GET", "/api/sessions/%s/screen" % sid)[1]))

    def test_create_message_stream_replay(self):
        st, boot, _, _ = self.req("GET", "/api/bootstrap")
        self.assertEqual((st, boot["gseq"]), (200, 0))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", "/api/stream?since=0&instance=" + boot["instance_id"],
                     headers={"Host": self.host, "X-CCR-Token": self.tok})
        r = conn.getresponse()
        st, meta, _, raw = self.req("POST", "/api/sessions", {"title": "集成"})
        self.assertEqual(st, 200, raw)
        sid = meta["sid"]
        js = self.wait_state(sid, ("idle", "trust"))
        if js["state"] == "trust":
            d = self.req("GET", "/api/sessions/%s/screen" % sid)[1]["state"]["dialog"]
            yes = next(o["index"] for o in d["options"] if o["label"].startswith("Yes"))
            self.assertEqual(self.req("POST", "/api/sessions/%s/choose" % sid, {"index": yes})[0], 200)
            self.wait_state(sid, ("idle",))
        st, js, _, raw = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "你好"})
        self.assertEqual(st, 200, raw)
        seen = []
        deadline = time.time() + 30
        while time.time() < deadline:
            line = r.fp.readline()
            if line.startswith(b"data: "):
                ev = json.loads(line[6:])
                seen.append(ev)
                if ev["kind"] == "transcript" and any(e.get("type") == "assistant" for e in ev["data"]["entries"]):
                    break
        conn.close()
        kinds = {e["kind"] for e in seen}
        self.assertTrue({"app", "screen", "transcript"} <= kinds, kinds)
        gs = [e["gseq"] for e in seen]
        self.assertEqual(gs, sorted(set(gs)))
        # 断线后用中间的 gseq 回放：screen 只回放最新一帧，其余事件恰好是其后的事件
        mid = gs[len(gs) // 2]
        evs = self.mgr.events_since(mid)
        self.assertLessEqual(sum(1 for e in evs if e["kind"] == "screen"), 1)
        st, js, _, _ = self.req("GET", "/api/sessions/%s/events" % sid)
        self.assertEqual(st, 200)
        self.assertTrue(any(e["kind"] == "transcript" for e in js["events"]))
        self.assertLessEqual(sum(1 for e in js["events"] if e["kind"] == "screen"), 1)
        self.assertIs(js["truncated"], False)
        st, js, _, _ = self.req("GET", "/api/sessions/%s/screen" % sid)
        self.assertEqual(st, 200)
        self.assertIn("你好", js["text"])
        self.assertTrue(js["state"]["input"]["visible"])
        # 模型面板
        st, js, _, raw = self.req("POST", "/api/sessions/%s/model_picker" % sid, {})
        self.assertEqual(st, 200, raw)
        self.assertEqual(len(js["rows"]), 6)
        st, js, _, raw = self.req("POST", "/api/sessions/%s/model" % sid, {"index": js["cursor"], "scope": "session"})
        self.assertEqual(st, 200, raw)
        self.assertEqual(self.req("POST", "/api/sessions/%s/close" % sid)[0], 200)
        self.assertEqual(self.req("GET", "/api/sessions/%s" % sid)[1]["state"], "exited")


class TestConfigStore(unittest.TestCase):
    def setUp(self):
        patch_remote(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "sub" / "config.json"

    def test_defaults_when_missing(self):
        st = C.ConfigStore(self.path)
        self.assertEqual(st.get(), C.DEFAULT_CONFIG)
        self.assertFalse(self.path.exists())

    def test_update_atomic_and_private(self):
        st = C.ConfigStore(self.path)
        st.update({"jobid": "123", "default_cwd": "~/proj", "ssh_multiplex": False})
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.json"])
        again = C.ConfigStore(self.path).get()
        self.assertEqual((again["jobid"], again["default_cwd"], again["ssh_multiplex"]), ("123", "~/proj", False))

    def test_corrupt_or_invalid_file(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{bad")
        self.assertEqual(C.ConfigStore(self.path).get(), C.DEFAULT_CONFIG)
        self.path.write_text(json.dumps({"jobid": "1;rm", "host": "myhost", "fake": "x", "zzz": 1}))
        cfg = C.ConfigStore(self.path).get()
        self.assertEqual((cfg["jobid"], cfg["host"], cfg["fake"]), ("", "myhost", False))
        self.assertNotIn("zzz", cfg)

    def test_overrides_not_persisted(self):
        st = C.ConfigStore(self.path, overrides={"fake": True})
        self.assertTrue(st.get()["fake"])
        st.update({"jobid": "5"})
        self.assertFalse(json.loads(self.path.read_text())["fake"])

    def test_extra_srun_args(self):
        self.assertEqual(C.validate_extra_srun_args(""), "")
        self.assertEqual(C.validate_extra_srun_args(None), "")
        self.assertEqual(C.validate_extra_srun_args(" --gres=gpu:1 --cpus-per-task=4 --mem=8G "),
                         "--gres=gpu:1 --cpus-per-task=4 --mem=8G")
        self.assertEqual(C.validate_extra_srun_args("--gres=gpu:1"), "--gres=gpu:1")
        for bad in ("--x=$(id)", "x", "--a b", "-", "--a=b;c", "--a='b'", "--a=\nb", "--a=b c=d", 5,
                    "--output=", "--output=x", "--chd=/tmp", "-l", "--task-prolog=x", "-c4", "--gres",
                    "--gres=a/b", "--gre=gpu:1", "--mem=8G -v"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                C.validate_extra_srun_args(bad)

    def test_extra_srun_args_shares_remote_validator(self):
        with mock.patch("ccr.remote.parse_extra_srun_args", return_value=["--mem=1G"]) as P:
            self.assertEqual(C.validate_extra_srun_args("anything"), "--mem=1G")
            P.assert_called_once_with("anything")

    def test_reload(self):
        st = C.ConfigStore(self.path, overrides={"control_dir": "/x"})
        st.update({"jobid": "1"})
        C.ConfigStore(self.path).update({"jobid": "777"})  # 另一个进程（app.py --jobid）改写
        self.assertEqual(st.get()["jobid"], "1")
        self.assertEqual(st.reload()["jobid"], "777")
        self.assertEqual(st.get()["control_dir"], "/x")

    def test_validate_partial_messages_chinese(self):
        with self.assertRaises(ValueError) as cm:
            C.validate_partial({"jobid": "abc"})
        self.assertIn("jobid", str(cm.exception))
        with self.assertRaises(ValueError):
            C.validate_partial({"host": "h1\n00"})
        self.assertEqual(C.validate_partial({"host": " myhost "}), {"host": "myhost"})
        with self.assertRaises(ValueError):
            C.validate_partial([])


if __name__ == "__main__":
    unittest.main()
