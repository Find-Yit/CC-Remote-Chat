"""本地配置读写：~/.cc-remote-chat/config.json（SPEC 2.1）。

- 只写本应用自己的数据目录，原子写（临时文件 + fsync + os.replace），权限 0600。
- 字段校验复用 ccr.remote 的 validate_*（开发者 A 提供，延迟导入）。
- 校验失败抛 ValueError（中文说明），由 server 转成 400。
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import pathlib
import re
import tempfile
import threading
from typing import Any, Callable, Dict, Optional

DEFAULT_DATA_DIR = pathlib.Path.home() / ".cc-remote-chat"

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "",                      # 服务器模式的 ssh 主机别名（~/.ssh/config 的 Host）；空 = 未配置，只能用本机模式
    "jobid": "",
    "default_cwd": "~",
    "default_model": "",
    "default_permission_mode": "",   # v2：空 = 不加 --permission-mode（仅供前端预填，后端不自动套用）
    "default_effort": "",            # v2：空 = 不加 --effort（同上）
    "ssh_multiplex": True,
    "extra_srun_args": "",
    "fake": False,
    # 提示音（ccr/notify.py）：总开关、完成时、需要操作时；声音 = /System/Library/Sounds/<名>.aiff（默认与 CLI 的
    # Stop hook 相同的 Blow）
    "notify_enabled": True,
    "notify_on_done": True,
    "notify_on_attention": True,
    "notify_sound": "Blow",
    # 默认 base_url（只存在本程序的 config.json，不读写 ~/.claude）：没有单独 base_url 的已保存 API key
    # 用它查询近 7 天用量（ccr/usage.py），选这样的 key 启动会话时也写进 key 文件的 ANTHROPIC_BASE_URL；
    # 空 = 不设置（与以前完全一致）。格式与 api_keys.BASE_URL_RE 相同（计算节点 agent 用同一个正则校验）
    "default_base_url": "",
    # 防休眠（ccr/keepawake.py，SPEC §12.2）：有会话在干活时运行 /usr/bin/caffeinate -i，阻止 Mac 空闲睡眠
    "keep_awake": True,
}

PERMISSION_MODES = ["manual", "acceptEdits", "auto", "plan", "bypassPermissions", "dontAsk"]
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]

# ssh 主机别名：不得以 '-' 开头（防止被 ssh 当成选项），只允许常见字符。
_HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@-]{0,252}\Z")


def _remote():
    """延迟导入开发者 A 的 ccr.remote（测试中可 patch 本函数）。"""
    return importlib.import_module("ccr.remote")


def _notify():
    return importlib.import_module("ccr.notify")


def permission_modes() -> list:
    mod = None
    try:
        mod = _remote()
    except ImportError:
        pass
    modes = getattr(mod, "PERMISSION_MODES", None) if mod else None
    if isinstance(modes, (list, tuple)) and modes:
        return [str(m) for m in modes]
    return list(PERMISSION_MODES)


def validate_extra_srun_args(s: Any) -> str:
    """精确白名单校验（docs/FIXES.md F2），与 ccr.remote 共用同一个函数 parse_extra_srun_args。"""
    if s is None:
        return ""
    if not isinstance(s, str):
        raise ValueError("extra_srun_args 必须是字符串")
    return " ".join(_remote().parse_extra_srun_args(s))


def validate_host(s: Any) -> str:
    if not isinstance(s, str) or not _HOST_RE.match(s.strip()):
        raise ValueError("host 不合法（只允许 ssh 主机别名，如 my-cluster）")
    s = s.strip()
    try:
        mod = _remote()
    except ImportError:
        mod = None
    if mod is not None and hasattr(mod, "validate_host"):
        s = mod.validate_host(s)
    return s


def _as_bool(name: str, v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError("%s 必须是布尔值" % name)


def _validate_field(key: str, value: Any) -> Any:
    if key == "host":
        if value is None or (isinstance(value, str) and not value.strip()):
            return ""   # 未配置服务器
        return validate_host(value)
    if key == "jobid":
        if value is None or value == "":
            return ""
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError("jobid 不合法")
        return _remote().validate_jobid(str(value).strip())
    if key == "default_cwd":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("default_cwd 不能为空")
        return _remote().validate_path(value.strip())
    if key == "default_model":
        if value is None or value == "":
            return ""
        if not isinstance(value, str):
            raise ValueError("default_model 必须是字符串")
        m = _remote().validate_model(value.strip())
        return m or ""
    if key == "default_permission_mode":
        if value is None or value == "":
            return ""
        if not isinstance(value, str) or value.strip() not in PERMISSION_MODES:
            raise ValueError("取值不合法（可选：%s，或留空）" % "/".join(PERMISSION_MODES))
        return value.strip()
    if key == "default_effort":
        if value is None or value == "":
            return ""
        if not isinstance(value, str) or value.strip() not in EFFORT_LEVELS:
            raise ValueError("取值不合法（可选：%s，或留空）" % "/".join(EFFORT_LEVELS))
        return value.strip()
    if key in ("ssh_multiplex", "fake", "notify_enabled", "notify_on_done", "notify_on_attention"):
        return _as_bool(key, value)
    if key == "keep_awake":
        return _as_bool(key, value)
    if key == "notify_sound":
        # 只接受 /System/Library/Sounds 下真实存在的 .aiff 文件名（不含扩展名）
        return _notify().validate_sound(value)
    if key == "extra_srun_args":
        return validate_extra_srun_args(value)
    if key == "default_base_url":
        from . import api_keys as ccr_api_keys   # 延迟导入：api_keys 依赖本模块
        return ccr_api_keys.validate_base_url(value) or ""
    raise ValueError("未知配置项：%s" % str(key)[:60])


def validate_partial(partial: Any) -> Dict[str, Any]:
    """校验部分配置字段，返回规范化后的 dict。任何字段不合法都抛 ValueError。"""
    if not isinstance(partial, dict):
        raise ValueError("配置必须是 JSON 对象")
    out = {}
    for k, v in partial.items():
        try:
            out[k] = _validate_field(k, v)
        except ValueError as e:
            msg = str(e) or "取值不合法"
            if k in DEFAULT_CONFIG and not msg.startswith(k):
                msg = "%s：%s" % (k, msg)
            raise ValueError(msg) from None
        except TypeError:
            raise ValueError("%s：取值类型不合法" % k) from None
    return out


def atomic_write_json(path: pathlib.Path, data: Any, mode: int = 0o600) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConfigStore:
    """线程安全的配置存储。get() 返回副本；update() 校验后原子保存。

    overrides：仅在内存中生效、不写盘的覆盖项（如 app.py --fake，以及按 --data-dir 注入的
    control_dir）。前端拿到的配置经 public_config() 过滤，不含 control_dir。
    """

    def __init__(self, path: Optional[pathlib.Path] = None, overrides: Optional[Dict[str, Any]] = None):
        self.path = pathlib.Path(path) if path else DEFAULT_DATA_DIR / "config.json"
        self.overrides = dict(overrides or {})
        self._lock = threading.Lock()
        self._cfg = self._load()

    def _load(self) -> Dict[str, Any]:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return cfg
        if not isinstance(raw, dict):
            return cfg
        for k in DEFAULT_CONFIG:
            if k not in raw:
                continue
            try:
                cfg[k] = _validate_field(k, raw[k])
            except (ValueError, TypeError, ImportError):
                pass  # 文件里单个字段坏了就用默认值，不整体失败
        return cfg

    def reload(self) -> Dict[str, Any]:
        """重新从磁盘读取（其它进程如 `app.py --jobid` 改写了 config.json 之后）。"""
        cfg = self._load()
        with self._lock:
            self._cfg = cfg
        return self.get()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            cfg = copy.deepcopy(self._cfg)
        cfg.update(self.overrides)
        return cfg

    def update(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        clean = validate_partial(partial)
        with self._lock:
            new = copy.deepcopy(self._cfg)
            new.update(clean)
            atomic_write_json(self.path, new)
            self._cfg = new
        return self.get()

    # 便于作为 get_config 回调传给 SessionManager
    def __call__(self) -> Dict[str, Any]:
        return self.get()


def public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """返回给前端的配置：只含 DEFAULT_CONFIG 中的字段（去掉 control_dir 等运行时项）。
    例外：内置模式（app.py --bundled）的运行时项 tmux_label（本机 tmux 的 -L 名）也给前端，界面上的说明据此显示；
    平时没有这个运行时项，返回内容与以前相同。"""
    out = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    if cfg.get("tmux_label"):
        out["tmux_label"] = cfg["tmux_label"]
    return out


def make_getter(store: ConfigStore) -> Callable[[], Dict[str, Any]]:
    return store.get
