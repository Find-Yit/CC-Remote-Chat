"""API key 用量查询（可选；兼容 sub2api 等网关的 GET <base_url>/v1/usage，Authorization: Bearer <key>）。

- 请求从本地 Mac 直接发往 base_url。条目自己没填 base_url 时依次用：设置里的默认 base_url（config.json 的
  default_base_url）→ "服务器默认"条目的 base_url；都没有就不查询。
- 明文 key 只在本进程里用来拼请求头；**绝不**返回给前端、不写日志，错误信息里出现 key 也会被换成掩码。
- 7 天用量 = daily_usage 里 date ≥ 今天−6 天 的 actual_cost 之和（缺失用 cost）；"今天" = daily_usage 最大日期
  与本地日期中较大者（按网关返回的 YYYY-MM-DD 字符串比较）。另给今日用量与余额 remaining（次要信息）。
- 有的网关有 Claude Code 版本守卫：UA 不对会 403 claude_code_version_mismatch + required_version；
  收到就用 required_version 重试一次并缓存（之后的请求直接用它）。
- 结果按 id 缓存 5 分钟（key/base_url 改了自动失效）；查询失败不影响其它功能。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from . import api_keys as ccr_api_keys

log = logging.getLogger("ccr.usage")

DEFAULT_UA_VERSION = "2.1.330"
TTL = 300.0
TIMEOUT = 25.0   # 用量多的 key 首查可能 >10s
MAX_BODY = 4 * 1024 * 1024
_VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}\Z")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def summarize(data, local_today: Optional[str] = None) -> dict:
    """/v1/usage 的 JSON → {week_usd, today_usd, remaining_usd, unit, today}；格式不对抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("返回的不是 JSON 对象")
    if data.get("isValid") is False:
        raise ValueError("网关报告这个 key 无效")
    daily = data.get("daily_usage")
    if not isinstance(daily, list):
        raise ValueError("返回里没有 daily_usage")
    rows = []
    for d in daily:
        if isinstance(d, dict) and isinstance(d.get("date"), str) and _DATE_RE.match(d["date"]):
            c = _num(d.get("actual_cost"))
            if c is None:
                c = _num(d.get("cost"))
            rows.append((d["date"], c or 0.0))
    today = max([r[0] for r in rows] + [local_today or datetime.date.today().isoformat()])
    try:
        start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=6)).isoformat()
    except ValueError:
        raise ValueError("daily_usage 里的日期不合法") from None
    week = sum(c for d, c in rows if start <= d <= today)
    day = sum(c for d, c in rows if d == today)
    remaining = _num(data.get("remaining"))
    if remaining is None:
        remaining = _num(data.get("balance"))
    unit = data.get("unit") if isinstance(data.get("unit"), str) and len(data.get("unit")) <= 8 else None
    return {"week_usd": round(week, 6), "today_usd": round(day, 6), "remaining_usd": remaining, "unit": unit,
            "today": today}


class UsageService:
    def __init__(self, store, ttl: float = TTL, timeout: float = TIMEOUT, opener=None, get_default_base=None):
        self.store = store
        self._get_default_base = get_default_base   # () -> 设置里的默认 base_url（"" / None = 没设置）
        self.ttl = ttl
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        self._lock = threading.Lock()
        self._cache: dict = {}     # id -> (指纹, 结果)
        self.ua_version = DEFAULT_UA_VERSION

    # ------------------------------------------------------------ 对外
    def get_all(self, refresh: bool = False) -> dict:
        """{id: {week_usd, today_usd, remaining_usd, unit, fetched_at, error, base_url}}；过期/refresh 的并发重查。"""
        creds = self.store.all_credentials()
        default_base = self.default_base() or next((c["base_url"] for c in creds if c["is_server_default"]), None)
        now = time.time()
        out, todo = {}, []
        with self._lock:
            ids = {c["id"] for c in creds}
            for k in [k for k in self._cache if k not in ids]:
                del self._cache[k]
            for c in creds:
                base = c["base_url"] or default_base
                fp = hashlib.sha256((c["key"] + "\n" + (base or "")).encode()).hexdigest()
                hit = self._cache.get(c["id"])
                if hit and hit[0] == fp and not refresh and now - hit[1]["fetched_at"] < self.ttl:
                    out[c["id"]] = dict(hit[1])
                else:
                    todo.append((c["id"], c["key"], base, fp))
        results: dict = {}

        def work(kid, key, base, fp):
            r = self._fetch(key, base)
            with self._lock:
                self._cache[kid] = (fp, r)
            results[kid] = r

        threads = [threading.Thread(target=work, args=t, name="ccr-usage", daemon=True) for t in todo]
        for t in threads:
            t.start()
        deadline = time.monotonic() + self.timeout * 2 + 2
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        for kid, _key, base, _fp in todo:
            out[kid] = dict(results.get(kid) or self._err(base, "查询超时"))
        return out

    def default_base(self) -> Optional[str]:
        """设置里的默认 base_url（读取失败或不合法 = 没设置）。"""
        if not callable(self._get_default_base):
            return None
        try:
            return ccr_api_keys.validate_base_url(self._get_default_base())
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _err(base, msg) -> dict:
        return {"week_usd": None, "today_usd": None, "remaining_usd": None, "unit": None,
                "fetched_at": time.time(), "error": msg, "base_url": base}

    def _fetch(self, key: str, base: Optional[str]) -> dict:
        if not base:
            return self._err(None, "没有 base_url（可以在「设置」里填写默认 base_url），无法查询")
        mask = ccr_api_keys.mask_key(key)

        def clean(s):   # 万一网关回显了请求头：把明文 key 换成掩码
            return str(s).replace(key, mask)[:300]

        url = base.rstrip("/") + "/v1/usage"
        for attempt in range(2):
            ver = self.ua_version
            req = urllib.request.Request(url, method="GET", headers={
                "Authorization": "Bearer " + key, "Accept": "application/json",
                "User-Agent": "claude-cli/%s (external, cli)" % ver})
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    raw = r.read(MAX_BODY + 1)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read(65536)
                except OSError:
                    body = b""
                info = {}
                try:
                    info = (json.loads(body.decode("utf-8", "replace")) or {}).get("error") or {}
                except (ValueError, AttributeError):
                    info = {}
                if not isinstance(info, dict):
                    info = {"message": str(info)}
                rv = info.get("required_version")
                if (e.code == 403 and info.get("code") == "claude_code_version_mismatch" and attempt == 0
                        and isinstance(rv, str) and _VERSION_RE.match(rv) and rv != ver):
                    self.ua_version = rv   # 缓存：之后的请求直接用网关要求的版本
                    continue
                detail = info.get("message") or info.get("code") or (e.reason if isinstance(e.reason, str) else "")
                return self._err(base, clean("HTTP %d%s" % (e.code, ("：" + str(detail)) if detail else "")))
            except (socket.timeout, TimeoutError):
                return self._err(base, "超时（%gs）" % self.timeout)
            except urllib.error.URLError as e:
                reason = e.reason
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    return self._err(base, "超时（%gs）" % self.timeout)
                return self._err(base, clean("连接失败：%s" % (getattr(reason, "strerror", None) or reason)))
            except (OSError, ValueError) as e:
                return self._err(base, clean("请求失败：%s" % (getattr(e, "strerror", None) or type(e).__name__)))
            if len(raw) > MAX_BODY:
                return self._err(base, "返回内容过大")
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._err(base, "返回的不是 JSON")
            try:
                s = summarize(data)
            except ValueError as e:
                return self._err(base, clean(e))
            s.update({"fetched_at": time.time(), "error": None, "base_url": base})
            return s
        return self._err(base, "网关版本守卫重试后仍被拒绝")
