"""本地 API key 存储：<数据目录>/api_keys.json（docs/SPEC_V2_TUI.md §4.2 / §4.3）。

- 目录 0700、文件 0600，原子写（临时文件 + fsync + os.replace）；读取用 O_NOFOLLOW（拒绝软链接）。
- 条目 {id, name, key, base_url|null, created_at[, is_server_default:true]}；明文 key **只**存在这个文件里，
  只在启动/重启 claude 时作为 start 请求的 api_key 字段经 ssh stdin 发给计算节点 agent（不进 argv/env/日志），
  以及由本地后端直接查询该 key 的网关用量（ccr.usage）。
  base_url 为空 = 沿用服务器 settings.json 的 ANTHROPIC_BASE_URL；非空时一并写进远端 key 文件。
- is_server_default：最多一条，代表服务器 ~/.claude/settings.json 里的那个 key，**仅用于查询用量与显示名称**；
  选择"服务器默认"启动会话时依然不加 --settings（这条不能被当作 api_key_id 选中）。
- 对外（HTTP API、会话 meta、sessions.json、SSE、日志、错误信息）只用 public()：id / name / 掩码
  （如 `sk-ab12…cd34`）/ base_url，**永不返回明文 key**；校验失败的错误信息也绝不回显 key。
- SessionKeyMap：<数据目录>/session_keys.json（0600），记录每个 claude 会话（claude_session_id）上次用的
  API key id（null = 服务器默认），供"从历史续接"默认选中同一个 key。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import stat
import threading
import time
from typing import Callable, List, Optional

from . import config as ccr_config

log = logging.getLogger("ccr.api_keys")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{8,256}\Z")
BASE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?\Z")
MAX_BASE_URL = 200
MAX_NAME = 64
MAX_KEYS = 100
MAX_FILE = 1024 * 1024
MAX_SESSION_KEYS = 5000
RESERVED_IDS = frozenset({"usage"})   # /api/api_keys/usage 是查询用量的接口
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
KEY_FORMAT_HINT = "API key 格式不正确（只能包含字母、数字和 . _ -，长度 8–256）"
_UNSET = object()


class ApiKeyNotFound(KeyError):
    """id 不存在（可能已被删除）。"""

    def __str__(self):
        return "API key 不存在（可能已被删除）"


class ApiKeyConflict(ValueError):
    """名称或 key 与已保存的条目重复（server 映射为 409）。"""

    def __init__(self, message: str, code: str = "duplicate"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 校验
def validate_id(s) -> str:
    if not isinstance(s, str) or not ID_RE.match(s) or s in RESERVED_IDS:
        raise ValueError("api_key_id 不合法")
    return s


def validate_id_opt(s) -> Optional[str]:
    """None / 空串 = 服务器默认（不加 --settings）。"""
    if s is None or s == "":
        return None
    return validate_id(s)


def validate_key(s) -> str:
    """去掉首尾空白后校验；错误信息绝不回显 key。"""
    if not isinstance(s, str):
        raise ValueError(KEY_FORMAT_HINT)
    s = s.strip()
    if not KEY_RE.match(s):
        raise ValueError(KEY_FORMAT_HINT)
    return s


def validate_base_url(s) -> Optional[str]:
    """None / 空串 = 沿用服务器 settings.json 的 ANTHROPIC_BASE_URL。"""
    if s is None:
        return None
    if not isinstance(s, str):
        raise ValueError("base_url 必须是字符串")
    s = s.strip()
    if s == "":
        return None
    if len(s) > MAX_BASE_URL or not BASE_URL_RE.match(s):
        raise ValueError("base_url 格式不正确（形如 http://host:port 或 https://host/path，最长 %d 字符）"
                         % MAX_BASE_URL)
    return s


def validate_name(s) -> str:
    if not isinstance(s, str):
        raise ValueError("名称必须是字符串")
    s = s.strip()
    if not s:
        raise ValueError("名称不能为空")
    if len(s) > MAX_NAME:
        raise ValueError("名称过长（最多 %d 字）" % MAX_NAME)
    if _CTRL_RE.search(s):
        raise ValueError("名称不能包含控制字符")
    return s


def mask_key(key: str) -> str:
    """`sk-ab12…cd34`：长 key 露前 7 后 4；短 key 按比例少露（8 位只露 2+1）。"""
    n = len(key)
    if n >= 24:
        head, tail = 7, 4
    else:
        head, tail = max(1, n // 4), max(1, n // 8)
    return key[:head] + "…" + key[-tail:]


def _public(e: dict) -> dict:
    return {"id": e["id"], "name": e["name"], "masked": mask_key(e["key"]), "base_url": e.get("base_url"),
            "is_server_default": bool(e.get("is_server_default")), "created_at": e["created_at"]}


# ---------------------------------------------------------------- 安全读写（共用）
def _read_private_json(p: pathlib.Path):
    """O_NOFOLLOW 读取并解析；不存在 → None；软链接/非普通文件 → None（记录警告）；
    权限被放宽 → 收紧为 0600；无法解析 → 改名为 *.corrupt-<ts> 后返回 None（日志不含内容）。"""
    try:
        fd = os.open(str(p), os.O_RDONLY | _O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:   # 软链接（ELOOP）等：不跟随，当作没有
        log.warning("无法读取 %s（%s），忽略", p.name, e.strerror or type(e).__name__)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            log.warning("%s 不是普通文件，忽略", p.name)
            return None
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.fchmod(fd, 0o600)   # 收紧被放宽的权限
        raw = b""
        while len(raw) <= MAX_FILE:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    try:
        if len(raw) > MAX_FILE:
            raise ValueError("too large")
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        bak = p.with_name("%s.corrupt-%d" % (p.name, int(time.time())))
        log.warning("%s 无法解析，已改名为 %s", p.name, bak.name)
        try:
            os.replace(str(p), str(bak))
        except OSError:
            pass
        return None


def _write_private_json(p: pathlib.Path, data) -> None:
    # atomic_write_json：同目录 mkstemp（0600）+ fsync + os.replace，并把目录收紧为 0700
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ccr_config.atomic_write_json(p, data, mode=0o600)


# ---------------------------------------------------------------- API key
class ApiKeyStore:
    """线程安全；listener(event, id) 在变更落盘后、锁外调用（event = added|renamed|deleted）。"""

    def __init__(self, path):
        self.path = pathlib.Path(path).expanduser()
        self._lock = threading.RLock()
        self._listeners: List[Callable[[str, str], None]] = []
        self._entries: List[dict] = self._load()

    def _load(self) -> List[dict]:
        data = _read_private_json(self.path)
        if data is None:
            return []
        items = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(items, list):
            log.warning("%s 格式不正确，忽略", self.path.name)
            return []
        out, ids, pairs, names, have_default = [], set(), set(), set(), False
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                kid = validate_id(it.get("id"))
                name = validate_name(it.get("name"))
                key = validate_key(it.get("key"))
                base = validate_base_url(it.get("base_url"))
            except ValueError:
                continue
            sd = it.get("is_server_default") is True
            if kid in ids or name in names or (key, base) in pairs or (sd and have_default):
                continue
            ca = it.get("created_at")
            ca = float(ca) if isinstance(ca, (int, float)) and not isinstance(ca, bool) else 0.0
            ids.add(kid)
            names.add(name)
            pairs.add((key, base))
            have_default = have_default or sd
            e = {"id": kid, "name": name, "key": key, "base_url": base, "created_at": ca}
            if sd:
                e["is_server_default"] = True
            out.append(e)
        return out[:MAX_KEYS]

    def _save_locked(self, entries: List[dict]) -> None:
        _write_private_json(self.path, {"version": 1, "keys": entries})

    def _notify(self, event: str, kid: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(event, kid)
            except Exception:  # noqa: BLE001
                log.exception("api key 变更回调出错")

    def add_listener(self, cb: Callable[[str, str], None]) -> None:
        self._listeners.append(cb)

    # ------------------------------------------------------------ 查询（不含明文）
    def list_public(self) -> List[dict]:
        with self._lock:
            return [_public(e) for e in self._entries]

    def _find(self, kid) -> Optional[dict]:
        for e in self._entries:
            if e["id"] == kid:
                return e
        return None

    def public(self, kid) -> dict:
        with self._lock:
            e = self._find(kid)
            if e is None:
                raise ApiKeyNotFound(kid)
            return _public(e)

    def name_of(self, kid) -> Optional[str]:
        with self._lock:
            e = self._find(kid)
            return e["name"] if e else None

    def is_server_default(self, kid) -> bool:
        with self._lock:
            e = self._find(kid)
            return bool(e and e.get("is_server_default"))

    def server_default(self) -> Optional[dict]:
        with self._lock:
            for e in self._entries:
                if e.get("is_server_default"):
                    return _public(e)
        return None

    # ------------------------------------------------------------ 明文（只给本地后端：启动 claude / 查询用量）
    def secret(self, kid) -> str:
        return self.credentials(kid)[0]

    def credentials(self, kid):
        """(明文 key, base_url | None)。"""
        with self._lock:
            e = self._find(kid)
            if e is None:
                raise ApiKeyNotFound(kid)
            return e["key"], e.get("base_url")

    def all_credentials(self) -> List[dict]:
        """[{id, key, base_url, is_server_default}]（只给 ccr.usage 用）。"""
        with self._lock:
            return [{"id": e["id"], "key": e["key"], "base_url": e.get("base_url"),
                     "is_server_default": bool(e.get("is_server_default"))} for e in self._entries]

    # ------------------------------------------------------------ 修改
    def _check_dup_locked(self, kid, name, key, base) -> None:
        for e in self._entries:
            if e["id"] == kid:
                continue
            if e["name"] == name:
                raise ApiKeyConflict("已有同名的 API key「%s」" % name)
            if e.get("base_url") == base and hmac.compare_digest(e["key"].encode(), key.encode()):
                raise ApiKeyConflict("这个 key（同一 base_url）已保存为「%s」" % e["name"])

    def add(self, name, key, base_url=None, is_server_default=False) -> dict:
        name = validate_name(name)
        key = validate_key(key)
        base = validate_base_url(base_url)
        if not isinstance(is_server_default, bool):
            raise ValueError("is_server_default 必须是布尔值")
        with self._lock:
            self._check_dup_locked(None, name, key, base)
            if is_server_default and any(e.get("is_server_default") for e in self._entries):
                raise ApiKeyConflict("已经有一条「服务器默认」key，请先删除或编辑它", code="server_default_exists")
            if len(self._entries) >= MAX_KEYS:
                raise ValueError("最多保存 %d 个 API key" % MAX_KEYS)
            ids = {e["id"] for e in self._entries}
            kid = "k" + secrets.token_hex(5)
            while kid in ids:
                kid = "k" + secrets.token_hex(5)
            e = {"id": kid, "name": name, "key": key, "base_url": base, "created_at": time.time()}
            if is_server_default:
                e["is_server_default"] = True
            new = self._entries + [e]
            self._save_locked(new)
            self._entries = new
            pub = _public(e)
        self._notify("added", kid)
        return pub

    def update(self, kid, name=None, base_url=_UNSET) -> dict:
        """改名称和/或 base_url（base_url=None/"" = 改回服务器默认地址；不传 = 不改）。key 本身不可改（删了重加）。"""
        kid = validate_id(kid)
        with self._lock:
            e = self._find(kid)
            if e is None:
                raise ApiKeyNotFound(kid)
            nn = e["name"] if name is None else validate_name(name)
            nb = e.get("base_url") if base_url is _UNSET else validate_base_url(base_url)
            self._check_dup_locked(kid, nn, e["key"], nb)
            new = [dict(x, name=nn, base_url=nb) if x["id"] == kid else x for x in self._entries]
            self._save_locked(new)
            self._entries = new
            pub = _public(self._find(kid))
        self._notify("renamed", kid)
        return pub

    def rename(self, kid, name) -> dict:
        if name is None:
            raise ValueError("名称不能为空")
        return self.update(kid, name=name)

    def delete(self, kid) -> dict:
        kid = validate_id(kid)
        with self._lock:
            e = self._find(kid)
            if e is None:
                raise ApiKeyNotFound(kid)
            pub = _public(e)
            new = [x for x in self._entries if x["id"] != kid]
            self._save_locked(new)
            self._entries = new
        self._notify("deleted", kid)
        return pub


# ---------------------------------------------------------------- claude 会话 → 上次用的 key
class SessionKeyMap:
    """claude_session_id → {api_key_id|null, api_key_name, updated_at}（null = 服务器默认）。
    没有记录 ≠ 服务器默认：没有记录时"从历史续接"才默认选"服务器默认"。"""

    def __init__(self, path):
        self.path = pathlib.Path(path).expanduser()
        self._lock = threading.Lock()
        self._map = self._load()

    def _load(self) -> dict:
        data = _read_private_json(self.path)
        m = data.get("sessions") if isinstance(data, dict) else None
        out = {}
        if not isinstance(m, dict):
            return out
        for cid, rec in m.items():
            if not (isinstance(cid, str) and _UUID_RE.match(cid) and isinstance(rec, dict)):
                continue
            kid = rec.get("api_key_id")
            if kid is not None and not (isinstance(kid, str) and ID_RE.match(kid)):
                continue
            name = rec.get("api_key_name")
            ts = rec.get("updated_at")
            out[cid] = {"api_key_id": kid,
                        "api_key_name": name if isinstance(name, str) and len(name) <= MAX_NAME else None,
                        "updated_at": float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0}
        return out

    def get(self, cid) -> Optional[dict]:
        if not isinstance(cid, str):
            return None
        with self._lock:
            r = self._map.get(cid.lower())
            return dict(r) if r else None

    def record(self, cid, kid, name) -> bool:
        """记录（有变化才落盘）；返回是否写入。"""
        if not isinstance(cid, str) or not _UUID_RE.match(cid.lower()):
            return False
        cid = cid.lower()
        with self._lock:
            old = self._map.get(cid)
            if old and old["api_key_id"] == kid and (old["api_key_name"] == name or name is None):
                return False
            self._map[cid] = {"api_key_id": kid, "api_key_name": name, "updated_at": time.time()}
            if len(self._map) > MAX_SESSION_KEYS:
                for k, _v in sorted(self._map.items(), key=lambda kv: kv[1]["updated_at"])[
                        :len(self._map) - MAX_SESSION_KEYS]:
                    del self._map[k]
            try:
                _write_private_json(self.path, {"version": 1, "sessions": self._map})
            except OSError as e:
                log.warning("保存 %s 失败：%s", self.path.name, e.strerror or type(e).__name__)
        return True
