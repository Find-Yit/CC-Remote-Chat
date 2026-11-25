"""API key 选择（--settings）测试：本地存储、HTTP API 不泄漏明文、会话 create/restart/resume 参数、
node_agent 写 key 文件（0600、O_NOFOLLOW、目录权限、复用、内容严格只有 env）、fake 端到端。

测试用的 key 全是假的（sk-test-…）；不连远端、不运行真实 claude。
"""
import io
import json
import logging
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ccr import api_keys as K
from ccr import node_agent
from ccr import session as S
from tests import test_node_agent as TNA
from tests import test_server as TS
from tests import test_session as TSS

KEY_A = "sk-test-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1111"
KEY_B = "sk-test-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2222"
KEY_C = "sk-test.cccc_cccc-cccc3333"


def mode_of(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


class _CaptureLogs:
    """捕获 ccr.* 全部日志（DEBUG 起），用于断言明文 key 不出现在日志里。"""

    def __init__(self, test):
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setLevel(logging.DEBUG)
        self.lg = logging.getLogger("ccr")
        self.old = self.lg.level
        self.lg.addHandler(self.h)
        self.lg.setLevel(logging.DEBUG)
        test.addCleanup(self.close)

    def close(self):
        self.lg.removeHandler(self.h)
        self.lg.setLevel(self.old)

    def text(self):
        return self.buf.getvalue()


# ============================================================================ 本地存储
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccr-keys-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "data")
        self.path = os.path.join(self.dir, "api_keys.json")

    def test_mask(self):
        self.assertEqual(K.mask_key("sk-ab120123456789abcdef01234567cd34"), "sk-ab12…cd34")
        self.assertEqual(K.mask_key("abcdefgh"), "ab…h")
        self.assertEqual(K.mask_key("abcdefghijklmnop"), "abcd…op")
        for k in (KEY_A, KEY_C, "abcdefgh"):
            m = K.mask_key(k)
            self.assertNotIn(k, m)
            self.assertLess(len(m.replace("…", "")), len(k) // 2 + 1)

    def test_add_list_rename_delete_and_permissions(self):
        st = K.ApiKeyStore(self.path)
        self.assertEqual(st.list_public(), [])
        self.assertFalse(os.path.exists(self.path))
        a = st.add("  key A ", "  " + KEY_A + "\n")      # 去掉首尾空白
        self.assertRegex(a["id"], K.ID_RE)
        self.assertEqual((a["name"], a["masked"]), ("key A", "sk-test…1111"))
        self.assertEqual(set(a), {"id", "name", "masked", "base_url", "is_server_default", "created_at"})
        self.assertEqual((a["base_url"], a["is_server_default"]), (None, False))
        self.assertEqual(mode_of(self.path), 0o600)
        self.assertEqual(mode_of(self.dir), 0o700)
        b = st.add("key B", KEY_B)
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual([x["name"] for x in st.list_public()], ["key A", "key B"])
        self.assertEqual(st.secret(a["id"]), KEY_A)
        for x in st.list_public():
            self.assertNotIn("key", x)
        # 原子写：目录里只有正式文件（没有残留的临时文件）
        self.assertEqual(sorted(os.listdir(self.dir)), ["api_keys.json"])
        data = json.loads(pathlib.Path(self.path).read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual([e["key"] for e in data["keys"]], [KEY_A, KEY_B])
        # 重新加载
        st2 = K.ApiKeyStore(self.path)
        self.assertEqual(st2.list_public(), st.list_public())
        # 重命名 / 冲突
        self.assertEqual(st.rename(a["id"], "主 key")["name"], "主 key")
        with self.assertRaises(K.ApiKeyConflict):
            st.rename(b["id"], "主 key")
        with self.assertRaises(K.ApiKeyConflict) as cm:
            st.add("again", KEY_A)
        self.assertNotIn(KEY_A, str(cm.exception))
        with self.assertRaises(K.ApiKeyConflict):
            st.add("key B", KEY_C)
        # 删除
        events = []
        st.add_listener(lambda ev, kid: events.append((ev, kid)))
        st.delete(a["id"])
        self.assertEqual(events, [("deleted", a["id"])])
        with self.assertRaises(K.ApiKeyNotFound):
            st.secret(a["id"])
        with self.assertRaises(K.ApiKeyNotFound):
            st.delete(a["id"])
        self.assertIsNone(st.name_of(a["id"]))
        self.assertEqual([x["id"] for x in K.ApiKeyStore(self.path).list_public()], [b["id"]])
        self.assertEqual(mode_of(self.path), 0o600)

    def test_validation_never_echoes_key(self):
        st = K.ApiKeyStore(self.path)
        secretish = "sk-live-SECRET$(id)"
        for bad in (secretish, "short", "a" * 257, "has space inside", "sk-中文中文中文中文", None, 123, ""):
            with self.assertRaises(ValueError) as cm:
                st.add("n", bad)
            self.assertNotIn(str(bad), str(cm.exception)) if bad else None
            self.assertIn("API key 格式不正确", str(cm.exception))
        for bad in ("", "   ", "x" * 65, "a\nb", None):
            with self.assertRaises(ValueError):
                st.add(bad, KEY_A)
        for bad in ("", "A", "-x", "x" * 33, "a b", "../x", None, 3):
            with self.assertRaises(ValueError):
                K.validate_id(bad)
        self.assertIsNone(K.validate_id_opt(None))
        self.assertIsNone(K.validate_id_opt(""))
        self.assertEqual(K.validate_id_opt("k0a1b2c3d4e"), "k0a1b2c3d4e")
        self.assertFalse(os.path.exists(self.path))

    def test_atomic_write_failure_keeps_old_file(self):
        st = K.ApiKeyStore(self.path)
        st.add("a", KEY_A)
        before = pathlib.Path(self.path).read_bytes()
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                st.add("b", KEY_B)
        self.assertEqual(pathlib.Path(self.path).read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), ["api_keys.json"])   # 临时文件已清理
        self.assertEqual([x["name"] for x in st.list_public()], ["a"])     # 内存状态未变

    def test_load_tightens_perms_rejects_symlink_and_backs_up_corrupt(self):
        os.makedirs(self.dir)
        good = {"version": 1, "keys": [{"id": "k1", "name": "a", "key": KEY_A, "created_at": 1},
                                       {"id": "BAD", "name": "b", "key": KEY_B, "created_at": 1},
                                       {"id": "k3", "name": "c", "key": "x y", "created_at": 1}]}
        pathlib.Path(self.path).write_text(json.dumps(good))
        os.chmod(self.path, 0o644)
        st = K.ApiKeyStore(self.path)
        self.assertEqual([x["id"] for x in st.list_public()], ["k1"])   # 不合法条目被跳过
        self.assertEqual(mode_of(self.path), 0o600)
        # 软链接：不跟随（O_NOFOLLOW）
        os.unlink(self.path)
        target = os.path.join(self.tmp, "elsewhere.json")
        pathlib.Path(target).write_text(json.dumps(good))
        os.symlink(target, self.path)
        self.assertEqual(K.ApiKeyStore(self.path).list_public(), [])
        os.unlink(self.path)
        # 损坏：备份后当作空
        pathlib.Path(self.path).write_text("{not json " + KEY_A)
        logs = _CaptureLogs(self)
        st = K.ApiKeyStore(self.path)
        self.assertEqual(st.list_public(), [])
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(any(n.startswith("api_keys.json.corrupt-") for n in os.listdir(self.dir)))
        self.assertNotIn(KEY_A, logs.text())


# ============================================================================ HTTP API（桩 manager）
class ServerApiTests(TS.Base):
    def keys_path(self):
        return self.cfg_path.parent / "api_keys.json"

    def test_crud_and_never_returns_plaintext(self):
        logs = _CaptureLogs(self)
        st, js, _, raw = self.req("GET", "/api/api_keys")
        self.assertEqual((st, js), (200, {"keys": [], "default_base_url": ""}))
        st, js, _, raw = self.req("POST", "/api/api_keys", {"name": "key B", "key": KEY_B})
        self.assertEqual(st, 200, raw)
        kid = js["api_key"]["id"]
        self.assertEqual(js["api_key"]["masked"], "sk-test…2222")
        self.assertNotIn(KEY_B.encode(), raw)
        st, js, _, raw = self.req("GET", "/api/api_keys")
        self.assertEqual(js["keys"], [{"id": kid, "name": "key B", "masked": "sk-test…2222", "base_url": None,
                                       "is_server_default": False,
                                       "created_at": js["keys"][0]["created_at"], "in_use": 0}])
        self.assertNotIn(KEY_B.encode(), raw)
        self.assertNotIn(b"sk-test-bbbb", raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.keys_path()).st_mode), 0o600)
        # 重复 / 不合法：错误信息不回显 key
        st, js, _, raw = self.req("POST", "/api/api_keys", {"name": "x", "key": KEY_B})
        self.assertEqual((st, js.get("code")), (409, "duplicate"))
        self.assertNotIn(KEY_B.encode(), raw)
        bad = "sk-bad key $(id)"
        st, js, _, raw = self.req("POST", "/api/api_keys", {"name": "x", "key": bad})
        self.assertErr((st, js, _, raw), 400)
        self.assertNotIn(bad.encode(), raw)
        self.assertErr(self.req("POST", "/api/api_keys", {"name": "", "key": KEY_A}), 400)
        self.assertErr(self.req("POST", "/api/api_keys", {"key": KEY_A}), 400)
        # 重命名
        st, js, _, raw = self.req("PATCH", "/api/api_keys/" + kid, {"name": "新名字"})
        self.assertEqual((st, js["api_key"]["name"]), (200, "新名字"))
        self.assertNotIn(KEY_B.encode(), raw)
        self.assertErr(self.req("PATCH", "/api/api_keys/knope", {"name": "x"}), 404)
        self.assertErr(self.req("PATCH", "/api/api_keys/BAD..", {"name": "x"}), 404)
        self.assertErr(self.req("PATCH", "/api/api_keys", {"name": "x"}), 404)
        self.assertErr(self.req("PUT", "/api/api_keys", {}), 405)
        # 未认证
        self.assertErr(self.req("GET", "/api/api_keys", auth=False), 401)
        self.assertErr(self.req("POST", "/api/api_keys", {"name": "z", "key": KEY_C}, auth=False), 401)
        # 删除
        st, js, _, _ = self.req("DELETE", "/api/api_keys/" + kid)
        self.assertEqual((st, js["ok"]), (200, True))
        self.assertErr(self.req("DELETE", "/api/api_keys/" + kid), 404)
        self.assertEqual(self.req("GET", "/api/api_keys")[1], {"keys": [], "default_base_url": ""})
        self.assertNotIn(KEY_B, logs.text())

    def test_create_and_restart_pass_api_key_id(self):
        calls = []
        mgr = self.mgr

        def create(**kw):
            calls.append(("create", kw))
            return {"sid": "s1"}

        def restart(sid, **kw):
            calls.append(("restart", sid, kw))
            return {"sid": sid}

        mgr.create = create
        mgr.restart = restart
        mgr.metas["s1"] = {"sid": "s1"}
        self.req("POST", "/api/sessions", {"jobid": "276403", "cwd": "/x"})
        self.assertNotIn("api_key_id", calls[-1][1])          # 不选 = 与以前完全一致
        self.req("POST", "/api/sessions", {"jobid": "276403", "cwd": "/x", "api_key_id": None})
        self.assertNotIn("api_key_id", calls[-1][1])
        self.req("POST", "/api/sessions", {"jobid": "276403", "cwd": "/x", "api_key_id": "k123"})
        self.assertEqual(calls[-1][1]["api_key_id"], "k123")
        for bad in ("../etc", "K1", 5, ["k1"]):
            self.assertErr(self.req("POST", "/api/sessions", {"jobid": "276403", "api_key_id": bad}), 400)
        self.req("POST", "/api/sessions/s1/restart", {})
        self.assertNotIn("api_key_id", calls[-1][2])           # 不带 = 沿用
        self.req("POST", "/api/sessions/s1/restart", {"api_key_id": None})
        self.assertEqual(calls[-1][2]["api_key_id"], None)    # null = 改用服务器默认
        self.req("POST", "/api/sessions/s1/restart", {"api_key_id": ""})
        self.assertEqual(calls[-1][2]["api_key_id"], None)
        self.req("POST", "/api/sessions/s1/restart", {"api_key_id": "k9"})
        self.assertEqual(calls[-1][2]["api_key_id"], "k9")
        self.assertErr(self.req("POST", "/api/sessions/s1/restart", {"api_key_id": "a b"}), 400)


# ============================================================================ 会话管理（StubLink）
class SessionKeyTests(TSS.Base):
    def setUp(self):
        super().setUp()
        self.logs = _CaptureLogs(self)

    def keys(self, m):
        a = m.api_keys.add("key A", KEY_A)["id"]
        b = m.api_keys.add("key B", KEY_B)["id"]
        return a, b

    def assert_no_plaintext(self, m):
        m.flush()
        blobs = [json.dumps(m.list_sessions(), ensure_ascii=False), json.dumps(m.events_since(0), ensure_ascii=False),
                 pathlib.Path(self.tmp, "sessions.json").read_text(encoding="utf-8"), self.logs.text()]
        for sid in [x["sid"] for x in m.list_sessions()]:
            blobs.append(json.dumps(m.session_events(sid), ensure_ascii=False))
        for blob in blobs:
            for k in (KEY_A, KEY_B, KEY_C):
                self.assertNotIn(k, blob)

    def test_default_has_no_settings(self):
        m = self.mgr()
        sid = self.start(m)
        st = self.rem.ops("start")[0]
        self.assertNotIn("api_key", st)
        self.assertNotIn("--settings", st["args"])
        meta = m.get_meta(sid)
        self.assertIsNone(meta["launch"]["api_key_id"])
        self.assertIsNone(meta["api_key_name"])
        self.assertFalse(meta["api_key_missing"])

    def test_create_with_key(self):
        m = self.mgr()
        a, _b = self.keys(m)
        sid = self.start(m, api_key_id=a, model="haiku")
        st = self.rem.ops("start")[0]
        self.assertEqual(st["api_key"], KEY_A)                # 明文只在 start 请求里（经 agent stdin）
        self.assertEqual(st["args"], ["--model", "haiku"])    # --settings 由 agent 生成，本地不传
        meta = m.get_meta(sid)
        self.assertEqual(meta["launch"]["api_key_id"], a)
        self.assertEqual(meta["api_key_name"], "key A")
        self.assert_no_plaintext(m)

    def test_create_unknown_key_rejected(self):
        m = self.mgr()
        with self.assertRaises(S.OpError) as cm:
            m.create(jobid="12345", cwd="~", api_key_id="knope")
        self.assertEqual(cm.exception.api_code, "api_key_missing")
        with self.assertRaises(ValueError):
            m.create(jobid="12345", cwd="~", api_key_id="../x")
        self.assertEqual(m.list_sessions(), [])
        self.assertEqual(self.rem.ops("start"), [])

    def test_resume_from_history_with_key(self):
        m = self.mgr()
        _a, b = self.keys(m)
        self.start(m, resume=TSS.UUID2, api_key_id=b)
        st = self.rem.ops("start")[0]
        self.assertEqual((st["args"], st["api_key"]), (["--resume", TSS.UUID2], KEY_B))

    def test_restart_keeps_changes_and_defaults(self):
        m = self.mgr()
        a, b = self.keys(m)
        sid = self.start(m, api_key_id=a)
        self.converse(m, sid)
        m.restart(sid)                                         # 沿用
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 2 and m.get_meta(sid)["state"] == "idle"))
        st = self.rem.ops("start")[1]
        self.assertEqual((st["args"], st["api_key"]), (["--resume", TSS.UUID1], KEY_A))
        self.assertEqual(self.rem.ops("paste")[0]["text"], "/exit")   # 复用 close→start：先 /exit
        m.restart(sid, api_key_id=b)                           # 换 key = --resume 续接重启
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 3 and m.get_meta(sid)["state"] == "idle"))
        st = self.rem.ops("start")[2]
        self.assertEqual((st["args"], st["api_key"]), (["--resume", TSS.UUID1], KEY_B))
        meta = m.get_meta(sid)
        self.assertEqual((meta["sid"], meta["claude_session_id"]), (sid, TSS.UUID1))   # 同一本地会话、同一段对话
        self.assertEqual((meta["launch"]["api_key_id"], meta["api_key_name"]), (b, "key B"))
        m.restart(sid, api_key_id=None)                        # 改回服务器默认
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 4 and m.get_meta(sid)["state"] == "idle"))
        st = self.rem.ops("start")[3]
        self.assertNotIn("api_key", st)
        self.assertEqual(st["args"], ["--resume", TSS.UUID1])
        self.assertIsNone(m.get_meta(sid)["api_key_name"])
        with self.assertRaises(ValueError):
            m.restart(sid, api_key_id="Bad Id")
        self.assert_no_plaintext(m)

    def test_deleted_key_restart_reports_and_leaves_session_alone(self):
        m = self.mgr()
        a, b = self.keys(m)
        sid = self.start(m, api_key_id=a)
        self.assertEqual([u["sid"] for u in m.api_key_users(a)], [sid])
        m.api_keys.delete(a)
        self.assertTrue(TSS.wait_until(lambda: m.get_meta(sid)["api_key_missing"]))
        self.assertEqual(m.get_meta(sid)["api_key_name"], "key A")   # 保留最后已知名称用于显示
        with self.assertRaises(S.OpError) as cm:
            m.restart(sid)
        self.assertEqual(cm.exception.api_code, "api_key_missing")
        self.assertIn("「key A」已被删除", str(cm.exception))
        self.assertIn("改选", str(cm.exception))
        self.assertEqual(self.rem.ops("paste"), [])              # 旧会话没被关闭
        self.assertEqual(m.get_meta(sid)["state"], "idle")
        self.assertEqual(len(self.rem.ops("start")), 1)
        m.restart(sid, api_key_id=b)                              # 改选后可以重启
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 2 and m.get_meta(sid)["state"] == "idle"))
        self.assertEqual(self.rem.ops("start")[1]["api_key"], KEY_B)
        self.assertFalse(m.get_meta(sid)["api_key_missing"])
        # 重命名会刷新显示名
        m.api_keys.rename(b, "key B2")
        self.assertTrue(TSS.wait_until(lambda: m.get_meta(sid)["api_key_name"] == "key B2"))

    def test_persist_reload(self):
        m = self.mgr()
        a, _b = self.keys(m)
        sid = self.start(m, api_key_id=a)
        m.shutdown()
        self.assert_no_plaintext(m)
        m2 = self.mgr()
        meta = m2.get_meta(sid)
        self.assertEqual((meta["launch"]["api_key_id"], meta["api_key_name"], meta["api_key_missing"]),
                         (a, "key A", False))
        m2.api_keys.delete(a)
        m2.shutdown()
        m3 = self.mgr()
        self.assertTrue(m3.get_meta(sid)["api_key_missing"])

    def _push_screen(self, sid):
        link = self.rem.links[-1]
        with self.rem.lock:
            link._emit_all()

    def test_restart_carries_session_model_effort_mode(self):
        """会话里用 /model（仅本会话）、/effort、Shift+Tab 改过的值，重启（含换 API）后保留。"""
        m = self.mgr()
        _a, b = self.keys(m)
        sid = self.start(m)
        self.converse(m, sid)
        self.assertTrue(TSS.wait_until(lambda: m.get_meta(sid)["model"] == "Haiku"))
        t = self.tui(sid)
        with t.lock:
            t.current = 3          # Sonnet
            t.effort = "high"
            t.mode = 1             # plan
        self._push_screen(sid)
        self.assertTrue(TSS.wait_until(lambda: m.get_meta(sid)["model"] == "Sonnet"
                                       and m.get_meta(sid)["effort"] == "high" and m.get_meta(sid)["mode"] == "plan"))
        m.restart(sid, api_key_id=b)
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 2 and m.get_meta(sid)["state"] == "idle"))
        st = self.rem.ops("start")[1]
        self.assertEqual(st["args"], ["--resume", TSS.UUID1, "--model", "sonnet", "--permission-mode", "plan",
                                      "--effort", "high"])
        self.assertEqual(st["api_key"], KEY_B)
        la = m.get_meta(sid)["launch"]
        self.assertEqual((la["model"], la["effort"], la["permission_mode"]), ("sonnet", "high", "plan"))

    def test_restart_unchanged_keeps_launch(self):
        m = self.mgr()
        sid = self.start(m, model="claude-haiku-5-5")
        self.converse(m, sid)
        self.assertTrue(TSS.wait_until(lambda: m.get_meta(sid)["model"] == "Haiku"))
        m.restart(sid)
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 2 and m.get_meta(sid)["state"] == "idle"))
        self.assertEqual(self.rem.ops("start")[1]["args"], ["--resume", TSS.UUID1, "--model", "claude-haiku-5-5"])

    def test_model_arg_mapping(self):
        f = S.SessionManager._model_arg
        self.assertEqual(f("Fable 5.1", "Custom Fable model (claude-fable-5-1[1m])"), "claude-fable-5-1[1m]")
        self.assertEqual(f("Sonnet", "Sonnet 5.5 · Efficient"), "sonnet")
        self.assertEqual(f("Haiku 5.5", None), "haiku")
        self.assertEqual(f("Opus 5.5 (1M)", None), "opus[1m]")
        self.assertEqual(f("claude-opus-5-5", None), "claude-opus-5-5")
        self.assertIsNone(f("Default (recommended)", "Opus 5.5 · Most capable"))
        self.assertIsNone(f("Fable 5.2 (1M)", "claude-fable-5-2，1M 上下文"))
        self.assertIsNone(f(None, None))


# ============================================================================ node_agent：key 文件
class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="ccrkf", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        p = mock.patch.dict(os.environ, {"HOME": self.home})
        p.start()
        self.addCleanup(p.stop)
        self.kdir = os.path.join(self.home, ".cc-remote-chat", "keys")

    def fp(self, key):
        return os.path.join(self.kdir, node_agent.key_fingerprint(key) + ".json")

    def test_write_creates_private_dirs_and_strict_content(self):
        old = os.umask(0o077)   # 再严格的 umask 也要得到 0700 / 0600
        try:
            path = node_agent.write_key_file(KEY_A)
        finally:
            os.umask(old)
        self.assertEqual(path, self.fp(KEY_A))
        self.assertRegex(os.path.basename(path), r"^[0-9a-f]{16}\.json$")
        self.assertTrue(node_agent.settings_path_ok(path))
        self.assertEqual(mode_of(self.kdir), 0o700)
        self.assertEqual(mode_of(os.path.dirname(self.kdir)), 0o700)
        self.assertEqual(mode_of(path), 0o600)
        with open(path, "rb") as f:
            raw = f.read()
        self.assertEqual(json.loads(raw), {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}})
        self.assertEqual(raw, b'{"env":{"ANTHROPIC_AUTH_TOKEN":"%s"}}' % KEY_A.encode())
        self.assertNotIn("aaaa", os.path.basename(path))     # 文件名是指纹，不含 key/名称
        self.assertEqual(os.listdir(self.kdir), [os.path.basename(path)])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude")))

    def test_reuse_same_file_without_rewrite(self):
        p1 = node_agent.write_key_file(KEY_A)
        st1 = os.stat(p1)
        time.sleep(0.02)
        p2 = node_agent.write_key_file(KEY_A)
        st2 = os.stat(p2)
        self.assertEqual(p1, p2)
        self.assertEqual((st1.st_ino, st1.st_mtime_ns), (st2.st_ino, st2.st_mtime_ns))   # 内容一致：不重写
        p3 = node_agent.write_key_file(KEY_B)
        self.assertNotEqual(p1, p3)
        self.assertEqual(sorted(os.listdir(self.kdir)), sorted(os.path.basename(p) for p in (p1, p3)))

    def test_tampered_content_or_mode_is_rewritten(self):
        p = node_agent.write_key_file(KEY_A)
        for bad in ({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}, "hooks": {"x": 1}},
                    {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A, "ANTHROPIC_BASE_URL": "http://evil"}},
                    {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_B}}, {}):
            with open(p, "w") as f:
                json.dump(bad, f)
            node_agent.write_key_file(KEY_A)
            with open(p) as f:
                self.assertEqual(json.load(f), {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}})
        os.chmod(p, 0o644)
        node_agent.write_key_file(KEY_A)
        self.assertEqual(mode_of(p), 0o600)

    def test_base_url_content_and_fingerprint(self):
        base = "http://192.0.2.10:8080"
        p1 = node_agent.write_key_file(KEY_A)
        p2 = node_agent.write_key_file(KEY_A, base)
        self.assertNotEqual(p1, p2)                      # 指纹 = sha256(key + "\n" + base_url)[:16]
        import hashlib
        self.assertEqual(os.path.basename(p2), hashlib.sha256((KEY_A + "\n" + base).encode()).hexdigest()[:16] + ".json")
        self.assertEqual(os.path.basename(p1), hashlib.sha256((KEY_A + "\n").encode()).hexdigest()[:16] + ".json")
        with open(p2, "rb") as f:
            raw = f.read()
        self.assertEqual(json.loads(raw), {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A, "ANTHROPIC_BASE_URL": base}})
        self.assertEqual(list(json.loads(raw)["env"]), ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])
        self.assertEqual(mode_of(p2), 0o600)
        with open(p1, "rb") as f:
            self.assertNotIn(b"BASE_URL", f.read())
        ok = node_agent.key_settings_ok
        good = {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A, "ANTHROPIC_BASE_URL": base}}
        self.assertTrue(ok(good))
        self.assertTrue(ok(good, KEY_A, base))
        self.assertFalse(ok(good, KEY_A, None))          # 期望没有 base_url
        self.assertFalse(ok({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}}, KEY_A, base))
        self.assertFalse(ok({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A, "ANTHROPIC_BASE_URL": "file:///etc"}}))
        self.assertFalse(ok({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A, "ANTHROPIC_BASE_URL": base, "HTTPS_PROXY": "x"}}))
        self.assertFalse(ok({"env": {"ANTHROPIC_BASE_URL": base}}))
        for bad in ("ftp://x", "http://x/?q", "http://a b", "x" * 300):
            with self.assertRaises(node_agent.OpError):
                node_agent.write_key_file(KEY_A, bad)
        with open(p2, "w") as f:                          # 被改成别的地址 → 重写回正确内容
            json.dump({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A, "ANTHROPIC_BASE_URL": "http://evil:1"}}, f)
        node_agent.write_key_file(KEY_A, base)
        with open(p2) as f:
            self.assertEqual(json.load(f), good)

    def test_strict_content_check(self):
        ok = node_agent.key_settings_ok
        self.assertTrue(ok({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}}))
        self.assertTrue(ok({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}}, KEY_A))
        for bad in ({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}, "permissions": {}},
                    {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A, "X": "1"}}, {"env": {}}, {"env": []}, [], None,
                    {"env": {"ANTHROPIC_AUTH_TOKEN": "bad key"}}, {"env": {"ANTHROPIC_AUTH_TOKEN": 5}},
                    {"bad": True}):
            self.assertFalse(ok(bad), bad)
        self.assertFalse(ok({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}}, KEY_B))

    def test_rejects_symlinks(self):
        os.makedirs(os.path.join(self.home, ".cc-remote-chat"), mode=0o700)
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(elsewhere, mode=0o700)
        os.symlink(elsewhere, self.kdir)                                        # keys 目录是软链接
        with self.assertRaises(node_agent.OpError) as cm:
            node_agent.write_key_file(KEY_A)
        self.assertIn("软链接", str(cm.exception))
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(self.kdir)
        os.makedirs(self.kdir, mode=0o700)
        victim = os.path.join(self.home, "victim.txt")
        with open(victim, "w") as f:
            f.write("keep me")
        os.symlink(victim, self.fp(KEY_A))                                      # key 文件是软链接
        with self.assertRaises(node_agent.OpError):
            node_agent.write_key_file(KEY_A)
        with open(victim) as f:
            self.assertEqual(f.read(), "keep me")
        os.unlink(self.fp(KEY_A))
        os.link(victim, self.fp(KEY_A))                                         # 硬链接
        with self.assertRaises(node_agent.OpError):
            node_agent.write_key_file(KEY_A)
        with open(victim) as f:
            self.assertEqual(f.read(), "keep me")
        os.unlink(self.fp(KEY_A))
        shutil.rmtree(os.path.join(self.home, ".cc-remote-chat"))
        os.symlink(elsewhere, os.path.join(self.home, ".cc-remote-chat"))      # 上级目录是软链接
        with self.assertRaises(node_agent.OpError):
            node_agent.write_key_file(KEY_A)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_rejects_bad_dir_permissions(self):
        os.makedirs(self.kdir, mode=0o700)
        os.chmod(self.kdir, 0o755)
        with self.assertRaises(node_agent.OpError) as cm:
            node_agent.write_key_file(KEY_A)
        self.assertIn("700", str(cm.exception))
        self.assertEqual(mode_of(self.kdir), 0o755)     # 不擅自改用户已有目录
        self.assertEqual(os.listdir(self.kdir), [])
        os.chmod(self.kdir, 0o700)
        os.chmod(os.path.dirname(self.kdir), 0o777)
        with self.assertRaises(node_agent.OpError) as cm:
            node_agent.write_key_file(KEY_A)
        self.assertIn("其他用户写入", str(cm.exception))
        os.chmod(os.path.dirname(self.kdir), 0o755)     # 上级只要求其他人不可写
        self.assertTrue(os.path.exists(node_agent.write_key_file(KEY_A)))
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.write_key_file(KEY_B)
            self.assertIn("属主", str(cm.exception))

    def test_bad_keys_never_echoed(self):
        for bad in ("sk-has space", "short", "x" * 257, "sk-中文中文中", None, 5):
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.write_key_file(bad)
            if isinstance(bad, str):
                self.assertNotIn(bad, str(cm.exception))
        self.assertFalse(os.path.exists(self.kdir))

    def test_validate_args_settings(self):
        good = self.fp(KEY_A)
        with self.assertRaises(node_agent.OpError):
            node_agent.validate_args(["--settings", good])               # 本地传入：一律拒绝
        self.assertEqual(node_agent.validate_args(["--model", "haiku", "--settings", good], allow_settings=True),
                         ["--model", "haiku", "--settings", good])
        for bad in ("{}", '{"env":{}}', "/tmp/x.json", good + "x", good.replace(self.home, "/other"),
                    os.path.join(self.kdir, "..", "..", ".claude", "settings.json"), os.path.join(self.kdir, "ABC.json"),
                    "~/.cc-remote-chat/keys/0123456789abcdef.json"):
            with self.assertRaises(node_agent.OpError, msg=bad):
                node_agent.validate_args(["--settings", bad], allow_settings=True)
        with self.assertRaises(node_agent.OpError):
            node_agent.validate_args(["--settings", good, "--settings", good], allow_settings=True)


class FakeTuiSettingsTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="ccrfs", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        os.makedirs(os.path.join(self.home, "proj"))

    def app(self, argv):
        from ccr import fake_tui
        env = {"HOME": self.home, "USER": "u", "CCR_FAKE_DELAY": "0", "CCR_FAKE_THINK": "0"}
        return fake_tui.FakeClaude(argv, env=env, size=(80, 24), cwd=os.path.join(self.home, "proj"))

    def test_settings_mask_on_banner(self):
        p = os.path.join(self.home, "k.json")
        with open(p, "w") as f:
            json.dump({"env": {"ANTHROPIC_AUTH_TOKEN": KEY_A}}, f)
        a = self.app(["--settings", p, "--model", "sonnet"])
        lines = [x.rstrip() for x in a.frame()]
        self.assertIn("           API key: sk-test…1111 (--settings)", lines)
        self.assertNotIn(KEY_A, "\n".join(lines))
        self.assertNotIn("API key:", "\n".join(x.rstrip() for x in self.app([]).frame()))   # 默认：无此行
        import contextlib
        for bad in (os.path.join(self.home, "missing.json"), p + "x"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.app(["--settings", bad])
        with open(p, "w") as f:
            json.dump({"env": {}}, f)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.app(["--settings", p])


@unittest.skipUnless(TNA.HAVE_TMUX, "需要本机 tmux")
class AgentStartKeyTests(unittest.TestCase):
    """node_agent 子进程 + 本机 tmux + fake_tui：start 带 api_key → claude 拿到 --settings，argv 里没有明文。"""

    def setUp(self):
        self.fe = TNA.FakeEnv()
        self.addCleanup(self.fe.cleanup, self)
        self.fe.start_keeper()
        self.a = TNA.AgentProc(self.fe.env)
        self.addCleanup(self.a.close)
        self.home = self.fe.env["HOME"]
        self.proj = os.path.join(self.home, "proj")
        os.makedirs(self.proj)

    def test_start_with_key(self):
        sid = TNA.SID
        # 参数校验：key 格式错误 / 本地自带 --settings → 拒绝，且错误信息里没有 key
        e = self.a.err("start", sid=sid, cwd=self.proj, args=[], api_key="sk-bad key")
        self.assertIn("API key 格式不正确", e)
        self.assertNotIn("sk-bad key", e)
        self.assertIn("不允许", self.a.err("start", sid=sid, cwd=self.proj, api_key=KEY_A,
                                           args=["--settings", os.path.join(self.home, ".claude", "settings.json")]))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cc-remote-chat")))
        r = self.a.ok("start", sid=sid, cwd=self.proj, args=["--model", "haiku"], api_key=KEY_A)
        self.fe.pids.add(r["pane_pid"])
        self.a.ok("watch", sid=sid, on=True)
        self.a.screen_has(sid, "API key: sk-test…1111 (--settings)")
        sess = self.a.wait_event(lambda ev: ev["ev"] == "session")
        pid = sess["data"]["pid"]
        self.fe.pids.add(pid)
        kfile = os.path.join(self.home, ".cc-remote-chat", "keys", node_agent.key_fingerprint(KEY_A) + ".json")
        self.assertEqual(mode_of(kfile), 0o600)
        cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        self.assertIn("--settings " + kfile, cmd)
        self.assertNotIn(KEY_A, cmd)
        for e2 in self.a.events:
            self.assertNotIn(KEY_A, json.dumps(e2, ensure_ascii=False))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))
        self.a.ok("kill", sid=sid)
        # 带 base_url：key 文件里多一个 ANTHROPIC_BASE_URL；base_url 不合法 / 没有 api_key → 拒绝
        self.assertIn("base_url", self.a.err("start", sid=TNA.SID2, cwd=self.proj, args=[], base_url="http://x:1"))
        self.assertIn("base_url", self.a.err("start", sid=TNA.SID2, cwd=self.proj, args=[], api_key=KEY_B,
                                             base_url="http://x/?q=1"))
        base = "http://192.0.2.10:8080"
        r = self.a.ok("start", sid=TNA.SID2, cwd=self.proj, args=[], api_key=KEY_B, base_url=base)
        self.fe.pids.add(r["pane_pid"])
        self.a.ok("watch", sid=TNA.SID2, on=True)
        self.a.screen_has(TNA.SID2, "API key: sk-test…2222 @ %s (--settings)" % base)
        kf = os.path.join(self.home, ".cc-remote-chat", "keys", node_agent.key_fingerprint(KEY_B, base) + ".json")
        with open(kf) as f:
            self.assertEqual(json.load(f), {"env": {"ANTHROPIC_AUTH_TOKEN": KEY_B, "ANTHROPIC_BASE_URL": base}})
        self.a.ok("kill", sid=TNA.SID2)
        self.a.ok("stop_keeper")


# ============================================================================ fake 端到端（SessionManager + 真实 AgentLink）
@unittest.skipUnless(TSS._e2e_available(), "需要本机 tmux")
class FakeEndToEndKeyTests(unittest.TestCase):
    # 复用 test_session.TestFakeEndToEnd 的环境搭建（临时 HOME / TMUX_TMPDIR / 数据目录）与等待工具
    setUp = TSS.TestFakeEndToEnd.setUp
    _cleanup = TSS.TestFakeEndToEnd._cleanup
    mgr = TSS.TestFakeEndToEnd.mgr
    wait_state = TSS.TestFakeEndToEnd.wait_state

    def test_create_and_switch_key(self):
        logs = _CaptureLogs(self)
        m = self.mgr()
        a = m.api_keys.add("key A", KEY_A)["id"]
        b = m.api_keys.add("key B", KEY_B)["id"]
        sid = m.create(jobid="12345", cwd="~/proj", title="k", api_key_id=a)["sid"]
        self.wait_state(m, sid, ("idle", "trust"))
        self.assertTrue(TSS.wait_until(lambda: "API key: sk-test…1111 (--settings)" in m.get_screen(sid)["text"],
                                       10, 0.1), m.get_screen(sid)["text"])
        self.assertEqual(m.get_meta(sid)["api_key_name"], "key A")
        cid = m.get_meta(sid)["claude_session_id"]
        m.restart(sid, api_key_id=b)
        self.assertTrue(TSS.wait_until(lambda: "API key: sk-test…2222 (--settings)" in m.get_screen(sid)["text"],
                                       30, 0.1), m.get_screen(sid)["text"])
        self.wait_state(m, sid, ("idle", "trust"))
        meta = m.get_meta(sid)
        self.assertEqual((meta["sid"], meta["api_key_name"]), (sid, "key B"))
        if cid:
            self.assertEqual(meta["claude_session_id"], cid)       # --resume 续接同一个 claude 会话
        kdir = os.path.join(self.home, ".cc-remote-chat", "keys")
        self.assertEqual(sorted(os.listdir(kdir)), sorted(node_agent.key_fingerprint(k) + ".json" for k in (KEY_A, KEY_B)))
        for n in os.listdir(kdir):
            self.assertEqual(mode_of(os.path.join(kdir, n)), 0o600)
        m.restart(sid, api_key_id=None)                             # 服务器默认：没有 --settings
        self.assertTrue(TSS.wait_until(lambda: "API key:" not in m.get_screen(sid)["text"]
                                       and m.get_meta(sid)["state"] in ("idle", "trust"), 30, 0.1),
                        m.get_screen(sid)["text"])
        m.close(sid)
        m.shutdown()
        data_blob = "".join(p.read_text(encoding="utf-8", errors="replace")
                            for p in pathlib.Path(self.data).rglob("*") if p.is_file() and p.name != "api_keys.json")
        for k in (KEY_A, KEY_B):
            self.assertNotIn(k, data_blob)
            self.assertNotIn(k, logs.text())
            self.assertNotIn(k, json.dumps(m.events_since(0), ensure_ascii=False))


# ============================================================================ base_url / 服务器默认条目 / 会话→key 映射
BASE_X = "http://192.0.2.10:8080"


class BaseUrlAndDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccr-keys2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "api_keys.json")

    def test_base_url_validation(self):
        ok = ["http://192.0.2.10:8080", "https://api.example.com", "https://a.b/c/d_e-f~g.h/",
              "http://h:1", "https://x" + "y" * 180]
        for u in ok:
            self.assertEqual(K.validate_base_url(u), u)
        self.assertIsNone(K.validate_base_url(None))
        self.assertIsNone(K.validate_base_url("  "))
        bad = ["ftp://x", "http://", "http://x:123456", "http://a b", "http://x/?q=1", "http://x/#f", "http://x/%20",
               "http://user:pw@x", "javascript:alert(1)", "http://x/" + "a" * 200, "//x", 5, "http://x\n/y"]
        for u in bad:
            with self.assertRaises(ValueError, msg=repr(u)):
                K.validate_base_url(u)
            if isinstance(u, str):
                self.assertFalse(node_agent.base_url_ok(u.strip()) and len(u) <= 200 and u.strip() == u, u)
        for u in ok:
            self.assertTrue(node_agent.base_url_ok(u), u)

    def test_store_base_url_and_server_default(self):
        st = K.ApiKeyStore(self.path)
        d = st.add("默认网关", KEY_C, "http://192.0.2.20:8080", is_server_default=True)
        self.assertTrue(d["is_server_default"])
        with self.assertRaises(K.ApiKeyConflict) as cm:
            st.add("又一个默认", KEY_B, is_server_default=True)
        self.assertEqual(cm.exception.code, "server_default_exists")
        b = st.add("demo_key_b", KEY_B)                 # 同默认地址
        x1 = st.add("x1", KEY_A, BASE_X)
        st.add("x1-same-key-other-url", KEY_A)        # 同 key 不同地址：允许
        with self.assertRaises(K.ApiKeyConflict):
            st.add("dup", KEY_A, BASE_X)
        with self.assertRaises(ValueError):
            st.add("bad", KEY_B, "http://x/?a=1")
        self.assertEqual(st.credentials(x1["id"]), (KEY_A, BASE_X))
        self.assertEqual(st.credentials(b["id"]), (KEY_B, None))
        self.assertEqual(st.server_default()["id"], d["id"])
        self.assertTrue(st.is_server_default(d["id"]))
        # update：改 base_url / 名称；不传 = 不改；None = 改回默认地址
        self.assertEqual(st.update(x1["id"], base_url="https://gw.example/v1")["base_url"], "https://gw.example/v1")
        self.assertEqual(st.update(x1["id"], name="x1b")["base_url"], "https://gw.example/v1")
        with self.assertRaises(K.ApiKeyConflict):   # 改成与另一条相同的 (key, base_url)
            st.update(x1["id"], base_url=None)
        self.assertEqual(st.update(b["id"], base_url=BASE_X)["base_url"], BASE_X)
        self.assertIsNone(st.update(b["id"], base_url=None)["base_url"])   # None = 改回默认地址
        with self.assertRaises(K.ApiKeyNotFound):
            st.update("knope", base_url=None)
        st2 = K.ApiKeyStore(self.path)
        self.assertEqual(st2.list_public(), st.list_public())
        for e in json.loads(pathlib.Path(self.path).read_text())["keys"]:
            self.assertIn("base_url", e)
        with self.assertRaises(ValueError):
            K.validate_id("usage")                    # 保留字：/api/api_keys/usage

    def test_session_key_map_persistence(self):
        p = os.path.join(self.tmp, "session_keys.json")
        m = K.SessionKeyMap(p)
        self.assertIsNone(m.get(TSS.UUID1))
        self.assertTrue(m.record(TSS.UUID1, "kabc", "key A"))
        self.assertFalse(m.record(TSS.UUID1, "kabc", "key A"))     # 没变化不写盘
        self.assertTrue(m.record(TSS.UUID2.upper(), None, None))   # 服务器默认也要记（≠ 没有记录）
        self.assertFalse(m.record("not-a-uuid", "kabc", "x"))
        self.assertEqual(mode_of(p), 0o600)
        m2 = K.SessionKeyMap(p)
        self.assertEqual(m2.get(TSS.UUID1)["api_key_id"], "kabc")
        self.assertEqual(m2.get(TSS.UUID1)["api_key_name"], "key A")
        self.assertEqual(m2.get(TSS.UUID2)["api_key_id"], None)
        for k in (KEY_A, KEY_B):
            self.assertNotIn(k, pathlib.Path(p).read_text())


class SessionBaseUrlTests(TSS.Base):
    def test_base_url_sent_and_server_default_normalized(self):
        m = self.mgr()
        x = m.api_keys.add("x", KEY_A, BASE_X)["id"]
        d = m.api_keys.add("默认", KEY_C, "http://192.0.2.20:8080", is_server_default=True)["id"]
        sid = self.start(m, api_key_id=x)
        st = self.rem.ops("start")[0]
        self.assertEqual((st["api_key"], st["base_url"], st["args"]), (KEY_A, BASE_X, []))
        # "服务器默认"条目只用于显示/用量：选它 = 不加 --settings
        sid2 = self.start(m, api_key_id=d, resume=TSS.UUID2)
        st2 = self.rem.ops("start")[1]
        self.assertNotIn("api_key", st2)
        self.assertNotIn("base_url", st2)
        self.assertIsNone(m.get_meta(sid2)["launch"]["api_key_id"])
        m.restart(sid, api_key_id=d)
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 3 and m.get_meta(sid)["state"] == "idle"))
        self.assertNotIn("api_key", self.rem.ops("start")[2])

    def test_session_key_recorded_and_history_lookup(self):
        m = self.mgr()
        a = m.api_keys.add("key A", KEY_A)["id"]
        self.assertIsNone(m.api_key_for_claude_session(TSS.UUID1))
        sid = self.start(m, api_key_id=a)
        self.assertTrue(TSS.wait_until(lambda: m.api_key_for_claude_session(TSS.UUID1) is not None))
        self.assertEqual(m.api_key_for_claude_session(TSS.UUID1), {"id": a, "name": "key A", "missing": False})
        m.restart(sid, api_key_id=None)   # 换成服务器默认：记录随之更新
        self.assertTrue(TSS.wait_until(lambda: (m.api_key_for_claude_session(TSS.UUID1) or {}).get("id", 0) is None))
        m.restart(sid, api_key_id=a)
        self.assertTrue(TSS.wait_until(lambda: (m.api_key_for_claude_session(TSS.UUID1) or {}).get("id") == a))
        m.shutdown()
        m2 = self.mgr()                    # 持久化：app 重启后仍能查到
        self.assertEqual(m2.api_key_for_claude_session(TSS.UUID1)["id"], a)
        m2.api_keys.delete(a)
        self.assertEqual(m2.api_key_for_claude_session(TSS.UUID1), {"id": a, "name": "key A", "missing": True})
        raw = pathlib.Path(self.tmp, "session_keys.json").read_text()
        self.assertIn(TSS.UUID1, raw)
        self.assertNotIn(KEY_A, raw)


class HistoryAnnotationTests(TS.Base):
    def test_history_items_carry_last_api_key(self):
        recs = {TS.UUID: {"id": "kabc", "name": "key A", "missing": False}}
        self.mgr.api_key_for_claude_session = lambda cid: recs.get(cid)
        st, js, _, _ = self.req("GET", "/api/history")
        self.assertEqual(st, 200)
        self.assertEqual(js["sessions"][0]["api_key"], {"id": "kabc", "name": "key A", "missing": False})
        recs.clear()
        st, js, _, _ = self.req("GET", "/api/history")
        self.assertNotIn("api_key", js["sessions"][0])    # 没有记录：不加（前端才默认"服务器默认"）


# ============================================================================ 用量（本地假 /v1/usage 网关）
import http.server  # noqa: E402
import threading    # noqa: E402
from ccr import usage as U  # noqa: E402


class _FakeGateway:
    """模拟 sub2api：/v1/usage；version_guard=True 时 UA 版本不对返回 403 claude_code_version_mismatch。"""

    def __init__(self):
        self.requests = []
        self.mode = "ok"
        self.required = "2.1.999"
        self.version_guard = False
        self.daily = [{"date": "2026-11-22", "requests": 8, "cost": 0.0106, "actual_cost": 0.0106},
                      {"date": "2026-11-21", "requests": 3, "cost": 1.5, "actual_cost": 1.0},
                      {"date": "2026-11-16", "requests": 1, "cost": 2.0},                 # 缺 actual_cost → 用 cost
                      {"date": "2026-11-15", "requests": 1, "cost": 100.0, "actual_cost": 100.0}]   # 超出 7 天
        gw = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                gw.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                    "ua": self.headers.get("User-Agent")})
                if gw.mode == "slow":
                    time.sleep(1.5)
                if gw.version_guard and self.headers.get("User-Agent") != "claude-cli/%s (external, cli)" % gw.required:
                    return self._send(403, {"error": {"code": "claude_code_version_mismatch",
                                                      "required_version": gw.required, "message": "need newer"}})
                if gw.mode == "badjson":
                    return self._send(200, b"<html>oops</html>")
                if gw.mode == "401":
                    return self._send(401, {"error": {"code": "invalid_api_key",
                                                      "message": "bad key " + (self.headers.get("Authorization") or "")}})
                if self.path != "/v1/usage" and not self.path.endswith("/v1/usage"):
                    return self._send(404, {"error": "nope"})
                self._send(200, {"isValid": True, "unit": "USD", "balance": 75292.2, "remaining": 75292.2,
                                 "daily_usage": gw.daily, "model_stats": [], "usage": {}})

            def _send(self, code, body):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccr-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gw = _FakeGateway()
        self.addCleanup(self.gw.close)
        self.store = K.ApiKeyStore(os.path.join(self.tmp, "api_keys.json"))
        noproxy = __import__("urllib.request").request.build_opener(__import__("urllib.request").request.ProxyHandler({}))
        self.svc = U.UsageService(self.store, timeout=0.6, opener=noproxy)
        self.logs = _CaptureLogs(self)

    def test_summarize(self):
        s = U.summarize({"daily_usage": self.gw.daily, "remaining": 5}, local_today="2026-11-20")
        self.assertEqual(s["today"], "2026-11-22")          # 网关最大日期比本地日期新 → 以它为今天
        self.assertAlmostEqual(s["week_usd"], 0.0106 + 1.0 + 2.0)
        self.assertAlmostEqual(s["today_usd"], 0.0106)
        self.assertEqual(s["remaining_usd"], 5.0)
        s = U.summarize({"daily_usage": self.gw.daily}, local_today="2026-11-23")
        self.assertEqual(s["today"], "2026-11-23")          # 本地更新 → 今天没有用量
        self.assertAlmostEqual(s["week_usd"], 0.0106 + 1.0)   # 11-17 .. 11-23
        self.assertEqual(s["today_usd"], 0)
        for bad in (None, [], {"isValid": False, "daily_usage": []}, {"remaining": 1}):
            with self.assertRaises(ValueError):
                U.summarize(bad)

    def test_fetch_ok_cache_and_refresh(self):
        kid = self.store.add("x", KEY_A, self.gw.url)["id"]
        r = self.svc.get_all()[kid]
        self.assertIsNone(r["error"], r)
        self.assertEqual(r["remaining_usd"], 75292.2)
        self.assertGreater(r["week_usd"], 1.0)
        self.assertEqual(self.gw.requests[-1]["auth"], "Bearer " + KEY_A)
        self.assertEqual(self.gw.requests[-1]["path"], "/v1/usage")
        self.assertRegex(self.gw.requests[-1]["ua"], r"^claude-cli/\d+\.\d+\.\d+ \(external, cli\)$")
        n = len(self.gw.requests)
        self.svc.get_all()                                   # 5 分钟内走缓存
        self.assertEqual(len(self.gw.requests), n)
        self.svc.get_all(refresh=True)
        self.assertEqual(len(self.gw.requests), n + 1)
        self.store.update(kid, base_url=self.gw.url + "/")   # 地址变了：缓存失效
        self.svc.get_all()
        self.assertEqual(len(self.gw.requests), n + 2)
        self.assertNotIn(KEY_A, json.dumps(self.svc.get_all()))

    def test_version_guard_retry_and_cache(self):
        self.gw.version_guard = True
        kid = self.store.add("x", KEY_A, self.gw.url)["id"]
        r = self.svc.get_all()[kid]
        self.assertIsNone(r["error"], r)
        self.assertEqual([x["ua"] for x in self.gw.requests][-2:],
                         ["claude-cli/%s (external, cli)" % U.DEFAULT_UA_VERSION, "claude-cli/2.1.999 (external, cli)"])
        self.assertEqual(self.svc.ua_version, "2.1.999")
        n = len(self.gw.requests)
        self.svc.get_all(refresh=True)                       # 之后直接用缓存的版本：一次就成功
        self.assertEqual(len(self.gw.requests), n + 1)
        self.gw.required = "2.1.1000"                         # 再变：重试一次
        r = self.svc.get_all(refresh=True)[kid]
        self.assertIsNone(r["error"], r)

    def test_errors_timeout_badjson_401_and_no_base(self):
        a = self.store.add("a", KEY_A, self.gw.url)["id"]
        b = self.store.add("b", KEY_B)["id"]                  # 没有地址、也没有服务器默认条目 → 不查询
        self.gw.mode = "badjson"
        res = self.svc.get_all()
        self.assertEqual(res[a]["error"], "返回的不是 JSON")
        self.assertIsNone(res[b]["base_url"])
        self.assertIn("没有 base_url", res[b]["error"])
        self.gw.mode = "401"                                   # 网关在错误信息里回显了 Authorization：换成掩码
        res = self.svc.get_all(refresh=True)
        self.assertIn("HTTP 401", res[a]["error"])
        self.assertNotIn(KEY_A, res[a]["error"])
        self.assertIn("sk-test…1111", res[a]["error"])
        self.gw.mode = "slow"
        t0 = time.monotonic()
        res = self.svc.get_all(refresh=True)
        self.assertLess(time.monotonic() - t0, 3.5)
        self.assertIn("超时", res[a]["error"])
        dead = self.store.add("dead", KEY_C, "http://127.0.0.1:9")["id"]   # 连接被拒
        self.gw.mode = "ok"
        res = self.svc.get_all(refresh=True)
        self.assertIsNone(res[a]["error"])
        self.assertIn("连接失败", res[dead]["error"])
        blob = json.dumps(res, ensure_ascii=False) + self.logs.text()
        for k in (KEY_A, KEY_B, KEY_C):
            self.assertNotIn(k, blob)

    def test_server_default_base_fallback(self):
        self.store.add("默认", KEY_C, self.gw.url, is_server_default=True)
        b = self.store.add("demo_key_b", KEY_B)["id"]        # 空地址 → 用服务器默认条目的地址
        r = self.svc.get_all()[b]
        self.assertIsNone(r["error"], r)
        self.assertEqual(r["base_url"], self.gw.url)
        self.assertIn("Bearer " + KEY_B, [x["auth"] for x in self.gw.requests])

    def test_http_api(self):
        """GET /api/api_keys/usage（?refresh=1 强制）：每个 id 的用量，不含明文。"""
        tst = TS.Base("run")
        tst.setUp()
        try:
            srv_keys = tst.srv.api_keys
            kid = srv_keys.add("x", KEY_A, self.gw.url)["id"]
            tst.srv.usage = U.UsageService(srv_keys, timeout=0.6, opener=self.svc._opener)
            st, js, _, raw = tst.req("GET", "/api/api_keys/usage")
            self.assertEqual(st, 200, raw)
            self.assertIsNone(js["usage"][kid]["error"])
            self.assertEqual(set(js["usage"][kid]) >= {"week_usd", "today_usd", "remaining_usd", "fetched_at", "error"},
                             True)
            self.assertNotIn(KEY_A.encode(), raw)
            n = len(self.gw.requests)
            tst.req("GET", "/api/api_keys/usage?refresh=1")
            self.assertEqual(len(self.gw.requests), n + 1)
            self.assertEqual(tst.req("POST", "/api/api_keys/usage", {})[0], 405)
            self.assertEqual(tst.req("GET", "/api/api_keys/usage", auth=False)[0], 401)
            # PATCH base_url / POST 带 base_url
            st, js, _, _ = tst.req("PATCH", "/api/api_keys/" + kid, {"base_url": None})
            self.assertEqual((st, js["api_key"]["base_url"]), (200, None))
            st, js, _, _ = tst.req("PATCH", "/api/api_keys/" + kid, {"base_url": "http://x/?bad"})
            self.assertEqual(st, 400)
            st, js, _, _ = tst.req("POST", "/api/api_keys", {"name": "y", "key": KEY_B, "base_url": BASE_X})
            self.assertEqual((st, js["api_key"]["base_url"]), (200, BASE_X))
            st, js, _, _ = tst.req("POST", "/api/api_keys", {"name": "d", "key": KEY_C, "is_server_default": True})
            self.assertEqual((st, js["api_key"]["is_server_default"]), (200, True))
            st, js, _, _ = tst.req("POST", "/api/api_keys", {"name": "d2", "key": KEY_C + "x", "is_server_default": True})
            self.assertEqual((st, js.get("code")), (409, "server_default_exists"))
        finally:
            tst.doCleanups()


# ============================================================================ 设置里的默认 base_url（config.json）
from ccr import config as C  # noqa: E402

DEF_BASE = "http://127.0.0.1:1/gw"   # 测试只用本机地址


class DefaultBaseUrlConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccr-defbase-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = pathlib.Path(self.tmp, "config.json")

    def test_default_empty_validate_save(self):
        self.assertEqual(C.DEFAULT_CONFIG["default_base_url"], "")
        st = C.ConfigStore(self.path)
        self.assertEqual(st.get()["default_base_url"], "")
        self.assertEqual(st.update({"default_base_url": "  " + DEF_BASE + "  "})["default_base_url"], DEF_BASE)
        self.assertEqual(json.loads(self.path.read_text())["default_base_url"], DEF_BASE)
        self.assertEqual(C.ConfigStore(self.path).get()["default_base_url"], DEF_BASE)   # 重启后仍在
        for bad in ("ftp://x", "http://x/?q=1", "http://x y", "https://" + "a" * 200, 5, ["x"]):
            with self.assertRaises(ValueError) as cm:
                st.update({"default_base_url": bad})
            self.assertIn("default_base_url", str(cm.exception))
        self.assertEqual(st.get()["default_base_url"], DEF_BASE)                       # 不合法的不保存
        for empty in ("", None, "   "):
            self.assertEqual(st.update({"default_base_url": empty})["default_base_url"], "")
        # 常见网关地址都能填：端口、路径、结尾斜杠、域名
        for u in ("https://api.example.com", "http://10.0.0.2:8080/", "https://gw.example.org/anthropic"):
            self.assertEqual(st.update({"default_base_url": u})["default_base_url"], u)
        # 文件里写坏了：忽略这一项（= 没设置）
        self.path.write_text(json.dumps({"default_base_url": "javascript:alert(1)"}))
        self.assertEqual(C.ConfigStore(self.path).get()["default_base_url"], "")
        self.assertIn("default_base_url", C.public_config(st.get()))


class DefaultBaseUrlUsageTests(UsageTests):
    """没有自己 base_url 的 key 用设置里的默认 base_url 查询用量；key 自己的优先；不填 = 与以前一致。"""

    def setUp(self):
        super().setUp()
        self.default = ""
        self.svc = U.UsageService(self.store, timeout=0.6, opener=self.svc._opener, get_default_base=lambda: self.default)

    def test_default_base_used_and_key_base_wins(self):
        a = self.store.add("a", KEY_A)["id"]                          # 没有地址
        own = self.store.add("own", KEY_B, "http://127.0.0.1:9")["id"]   # 自己的地址（连不上）优先
        r = self.svc.get_all()
        self.assertIn("没有 base_url", r[a]["error"])                  # 没设置：与以前一致，不查询
        self.assertIsNone(r[a]["base_url"])
        self.assertEqual(self.gw.requests, [])
        self.default = self.gw.url
        r = self.svc.get_all()                                         # 地址变了：缓存失效，马上重查
        self.assertIsNone(r[a]["error"], r[a])
        self.assertEqual(r[a]["base_url"], self.gw.url)
        self.assertGreater(r[a]["week_usd"], 1.0)
        self.assertEqual([x["auth"] for x in self.gw.requests], ["Bearer " + KEY_A])
        self.assertEqual(r[own]["base_url"], "http://127.0.0.1:9")
        self.assertIn("连接失败", r[own]["error"])
        n = len(self.gw.requests)
        self.svc.get_all()                                             # 缓存照旧
        self.assertEqual(len(self.gw.requests), n)
        blob = json.dumps(r, ensure_ascii=False) + self.logs.text()
        for k in (KEY_A, KEY_B):
            self.assertNotIn(k, blob)

    def test_default_base_before_server_default_entry_and_invalid_ignored(self):
        self.store.add("默认", KEY_C, "http://127.0.0.1:9", is_server_default=True)
        b = self.store.add("b", KEY_B)["id"]
        self.default = self.gw.url
        self.assertEqual(self.svc.get_all()[b]["base_url"], self.gw.url)      # 设置里的默认优先于服务器默认条目
        self.default = "not a url"                                            # 不合法 = 没设置 → 服务器默认条目的地址
        self.assertEqual(self.svc.default_base(), None)
        self.assertEqual(self.svc.get_all()[b]["base_url"], "http://127.0.0.1:9")
        self.svc._get_default_base = lambda: (_ for _ in ()).throw(RuntimeError("x"))   # 读取出错也只当没设置
        self.assertIsNone(self.svc.default_base())

    def test_version_guard_with_default_base(self):
        self.gw.version_guard = True
        self.default = self.gw.url
        kid = self.store.add("x", KEY_A)["id"]
        r = self.svc.get_all()[kid]
        self.assertIsNone(r["error"], r)
        self.assertEqual(self.svc.ua_version, "2.1.999")


class DefaultBaseUrlSessionTests(TSS.Base):
    def test_launch_uses_default_base_only_when_key_has_none(self):
        logs = _CaptureLogs(self)
        m = self.mgr()
        a = m.api_keys.add("a", KEY_A)["id"]
        own = m.api_keys.add("own", KEY_B, "http://127.0.0.1:2/own")["id"]
        d = m.api_keys.add("默认", KEY_C, is_server_default=True)["id"]
        # 没设置：与以前一致（不带 base_url，key 文件里没有 ANTHROPIC_BASE_URL）
        self.start(m, api_key_id=a)
        st = self.rem.ops("start")[0]
        self.assertEqual(st["api_key"], KEY_A)
        self.assertNotIn("base_url", st)
        self.cfg["default_base_url"] = DEF_BASE
        sid = self.start(m, api_key_id=a, resume=TSS.UUID2)
        self.assertEqual(self.rem.ops("start")[1]["base_url"], DEF_BASE)
        self.start(m, api_key_id=own, resume="33333333-3333-4333-8333-333333333333")                       # key 自己的地址优先
        self.assertEqual(self.rem.ops("start")[2]["base_url"], "http://127.0.0.1:2/own")
        self.start(m, api_key_id=d, resume="44444444-4444-4444-8444-444444444444")   # 服务器默认条目：仍不加 --settings
        st = self.rem.ops("start")[3]
        self.assertNotIn("api_key", st)
        self.assertNotIn("base_url", st)
        m.restart(sid)                                                        # 重启 / 续接同样用默认地址
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 5 and m.get_meta(sid)["state"] == "idle"))
        self.assertEqual(self.rem.ops("start")[4]["base_url"], DEF_BASE)
        self.cfg["default_base_url"] = "bad"                                  # 不合法 = 没设置
        m.restart(sid)
        self.assertTrue(TSS.wait_until(lambda: len(self.rem.ops("start")) == 6 and m.get_meta(sid)["state"] == "idle"))
        self.assertNotIn("base_url", self.rem.ops("start")[5])
        for st in self.rem.ops("start"):
            self.assertNotIn("--settings", st["args"])                        # 参数白名单不变（--settings 由 agent 追加）
        blob = json.dumps(m.list_sessions(), ensure_ascii=False) + logs.text()
        for k in (KEY_A, KEY_B, KEY_C):
            self.assertNotIn(k, blob)


class DefaultBaseUrlServerTests(TS.Base):
    def test_config_and_api_keys_and_usage(self):
        gw = _FakeGateway()
        self.addCleanup(gw.close)
        logs = _CaptureLogs(self)
        self.srv.usage._opener = __import__("urllib.request").request.build_opener(
            __import__("urllib.request").request.ProxyHandler({}))
        kid = self.srv.api_keys.add("a", KEY_A)["id"]
        st, js, _, _ = self.req("GET", "/api/api_keys")
        self.assertEqual(js["default_base_url"], "")
        st, js, _, raw = self.req("GET", "/api/api_keys/usage")
        self.assertIn("没有 base_url", js["usage"][kid]["error"])
        st, js, _, raw = self.req("POST", "/api/config", {"default_base_url": "ftp://x"})
        self.assertEqual(st, 400)
        self.assertIn("default_base_url", js["error"])
        st, js, _, raw = self.req("POST", "/api/config", {"default_base_url": gw.url})
        self.assertEqual((st, js["default_base_url"]), (200, gw.url), raw)
        st, js, _, _ = self.req("GET", "/api/config")
        self.assertEqual(js["default_base_url"], gw.url)
        st, js, _, raw = self.req("GET", "/api/api_keys")
        self.assertEqual(js["default_base_url"], gw.url)
        self.assertIsNone(js["keys"][0]["base_url"])                         # key 本身的 base_url 不变
        st, js, _, raw = self.req("GET", "/api/api_keys/usage")
        self.assertIsNone(js["usage"][kid]["error"], js)
        self.assertEqual(js["usage"][kid]["base_url"], gw.url)
        self.assertEqual(gw.requests[-1]["auth"], "Bearer " + KEY_A)
        self.assertNotIn(KEY_A.encode(), raw)
        self.assertNotIn(KEY_A, self.cfg_path.read_text() + logs.text())
        self.assertEqual(self.req("POST", "/api/config", {"default_base_url": ""})[1]["default_base_url"], "")


if __name__ == "__main__":
    unittest.main()
