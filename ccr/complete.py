"""输入框补全的数据（SPEC §7「输入框：/ 命令与 @ 文件补全」、§4.3）：**只读**列出会话可用的自定义命令、skills、
已启用插件的命令 / skills，以及会话 cwd 下的文件与文件夹。

- CLI 内置命令（含随 CLI 附带的 skills）是前端按 CLI 版本的静态表（static/slashcmds.js，真机 2.1.332 实测），
  这里只列磁盘上的：`~/.claude/commands/**/*.md`、`<cwd 及上级>/.claude/commands/**/*.md`（子目录用 `:` 连成命名空间，
  与 CLI 一致）、`~/.claude/skills/*/SKILL.md`、`<cwd 及上级>/.claude/skills/*/SKILL.md`、已启用插件（settings 的
  enabledPlugins = true，安装位置取 `~/.claude/plugins/installed_plugins.json`）的 `commands/` 与 `skills/`。
- 对 `~/.claude` 与项目 `.claude` **只读、不跟随软链接**：每一级目录都 lstat 确认是真目录；文件用 O_NOFOLLOW 打开、
  fstat 确认是普通文件，只读开头 16KB（frontmatter 在最前面）；软链接的命令 / skill 只列名字、不读内容。
  数量、深度、大小都有上限。settings.json 只取 enabledPlugins，其它键（可能有 env / key）读进来立刻丢掉、绝不返回。
- 文件列表：从 cwd 广度优先、不跟随软链接、跳过 .git / node_modules 等，最多 8 层 / 5000 项 / 2.5 秒。
- 同一份脚本（SCRIPT）：本机会话在本进程里直接调用；服务器会话像 ccr/history.py 一样经 ssh 在登录节点
  `python3 -I -` 执行（home 共享；不经计算节点上的 agent，不改 node_agent）；fake 模式用 fake 的 HOME 在本进程里调用。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT = r'''
import json
import os
import re
import stat
import sys
import time
from collections import deque

MAX_ITEMS = 400            # 命令 + skills 合计
MAX_READS = 400            # 最多读多少个 .md / SKILL.md
HEAD_BYTES = 16384         # 每个文件只读开头（frontmatter 在最前面）
MAX_JSON_BYTES = 1 << 20   # settings.json / installed_plugins.json / plugin.json 上限
MAX_DEPTH = 4              # commands 目录下子目录（命名空间）层数
MAX_DIR_ENTRIES = 2000     # 每个命令 / skills 目录最多看多少项
PROJECT_LEVELS = 6         # cwd 往上找 .claude 的层数（到 home 为止，home 的 .claude 是用户级）
DESC_MAX = 300
HINT_MAX = 120
NAME_RE = re.compile(r"^[^\s/\\:\x00-\x1f\x7f]{1,80}$")
CMD_RE = re.compile(r"^[^\s/\\\x00-\x1f\x7f]{1,160}$")

FILE_MAX = 5000
FILE_DEPTH = 8
FILE_DIRS = 3000
FILE_DIR_ENTRIES = 20000
FILE_BUDGET = 2.5
IGNORE_DIRS = frozenset((
    ".git", ".hg", ".svn", ".jj", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".gradle", ".idea",
    ".cache", ".eggs", ".ipynb_checkpoints", "bower_components", ".terraform", ".DS_Store",
))
IGNORE_FILES = frozenset((".DS_Store", ".git"))
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class Budget(object):
    def __init__(self):
        self.reads = 0
        self.items = 0
        self.truncated = False


def _lstat_dir(p):
    """p 是真目录（不是软链接）→ True"""
    try:
        return stat.S_ISDIR(os.lstat(p).st_mode)
    except OSError:
        return False


def _real_chain(base, parts):
    """base 之下逐级 lstat，全部是真目录 → 拼好的路径；否则 None"""
    p = base
    for part in parts:
        if not part or part in (".", "..") or "/" in part:
            return None
        p = os.path.join(p, part)
        if not _lstat_dir(p):
            return None
    return p


def _read_head(path, limit, budget=None):
    """O_NOFOLLOW 打开、fstat 是普通文件才读开头 limit 字节；否则 None。"""
    if budget is not None:
        if budget.reads >= MAX_READS:
            budget.truncated = True
            return None
        budget.reads += 1
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        chunks = []
        left = limit
        while left > 0:
            b = os.read(fd, min(65536, left))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_json(path):
    data = _read_head(path, MAX_JSON_BYTES + 1)
    if data is None or len(data) > MAX_JSON_BYTES:
        return None
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def parse_frontmatter(text):
    """--- 之间的 YAML（只认顶层 key: value、引号、> / | 块、缩进续行）→ dict（值都是字符串）。"""
    if not text.startswith("---"):
        return {}
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}
    out = {}
    key = None
    mode = None      # ">" 折叠 / "|" 保留换行 / "plain" 续行 / "list"
    buf = []

    def flush():
        if key is None or mode is None:
            return
        if mode == "|":
            val = "\n".join(buf).strip("\n")
        else:
            val = " ".join(x for x in buf if x)
        if mode == "plain" and key in out:
            val = (out[key] + " " + val).strip() if val else out[key]
        out[key] = val

    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if line.strip() == "---" and not line.startswith((" ", "\t")):
            break
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:(?:\s+(.*?))?\s*$", line)
        if m and not line.startswith((" ", "\t")):
            flush()
            key, v = m.group(1), (m.group(2) or "")
            buf = []
            if v[:1] in (">", "|") and re.match(r"^[>|][+-]?\d*$", v):
                mode = v[0]
            elif v == "":
                mode = "plain"
                out[key] = ""
            else:
                out[key] = _unquote(v)
                mode = "plain"
            continue
        if key is not None and mode is not None and (line.startswith((" ", "\t")) or not line.strip()):
            s = line.strip()
            if s.startswith("- ") and mode == "plain" and not out.get(key):
                continue   # 列表（allowed-tools 等）：不需要
            buf.append(s)
            continue
        key, mode, buf = None, None, []
    flush()
    return out


def _desc_from(fm, body):
    d = fm.get("description") or fm.get("when_to_use") or ""
    if not d:
        for line in body.split("\n"):
            s = line.strip().lstrip("#").strip()
            if s and s != "---":
                d = s
                break
    d = re.sub(r"\s+", " ", d).strip()
    if len(d) > DESC_MAX:
        d = d[:DESC_MAX - 1] + "…"
    return d


def _split_fm(text):
    """frontmatter dict 与正文（去掉 frontmatter）"""
    fm = parse_frontmatter(text)
    body = text
    if fm or text.startswith("---\n") or text.startswith("---\r\n"):
        lines = text.split("\n")
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body = "\n".join(lines[i + 1:])
                break
    return fm, body


def _entry(name, src, kind, fm=None, body="", plugin=None, link=False):
    e = {"name": name, "src": src, "kind": kind}
    if link:
        e["link"] = True
        e["desc"] = ""
    else:
        fm = fm or {}
        e["desc"] = _desc_from(fm, body)
        hint = re.sub(r"\s+", " ", str(fm.get("argument-hint") or "")).strip()
        if hint:
            e["hint"] = hint[:HINT_MAX]
        dn = str(fm.get("name") or "").strip()
        if dn and dn != name.split(":")[-1] and len(dn) <= 80:
            e["display"] = dn
    if plugin:
        e["plugin"] = plugin
    return e


def _invocable(fm):
    return str(fm.get("user-invocable", "")).strip().lower() not in ("false", "no", "off", "0")


def _load_md(path, budget):
    data = _read_head(path, HEAD_BYTES, budget)
    if data is None:
        return None
    return _split_fm(data.decode("utf-8", "replace"))


def _scandir(path):
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                out.append(e)
                if len(out) >= MAX_DIR_ENTRIES:
                    break
    except OSError:
        return []
    out.sort(key=lambda e: e.name)
    return out


def scan_commands(base, src, out, budget, prefix="", plugin=None, depth=0, ns=()):
    """commands 目录：*.md → 命令（子目录 = 命名空间，用 : 连接）；目录里有 SKILL.md → 以目录为名的 skill。"""
    if depth > MAX_DEPTH or not _lstat_dir(base):
        return
    for e in _scandir(base):
        if budget.items >= MAX_ITEMS:
            budget.truncated = True
            return
        n = e.name
        if n.startswith(".") or not NAME_RE.match(n):
            continue
        p = os.path.join(base, n)
        try:
            is_link = e.is_symlink()
            is_dir = (not is_link) and e.is_dir(follow_symlinks=False)
            is_file = (not is_link) and e.is_file(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            sk = os.path.join(p, "SKILL.md")
            try:
                has_skill = stat.S_ISREG(os.lstat(sk).st_mode)
            except OSError:
                has_skill = False
            if has_skill:
                name = prefix + ":".join(ns + (n,))
                r = _load_md(sk, budget)
                if r is not None and _invocable(r[0]) and CMD_RE.match(name):
                    out.append(_entry(name, src, "skill", r[0], r[1], plugin))
                    budget.items += 1
            else:
                scan_commands(p, src, out, budget, prefix, plugin, depth + 1, ns + (n,))
            continue
        if not n.lower().endswith(".md") or len(n) <= 3:
            continue
        name = prefix + ":".join(ns + (n[:-3],))
        if not CMD_RE.match(name):
            continue
        if is_link:
            out.append(_entry(name, src, "command", plugin=plugin, link=True))
            budget.items += 1
            continue
        if not is_file:
            continue
        r = _load_md(p, budget)
        if r is None or not _invocable(r[0]):
            continue
        out.append(_entry(name, src, "command", r[0], r[1], plugin))
        budget.items += 1


def scan_skills(base, src, out, budget, prefix="", plugin=None):
    """skills 目录：<名字>/SKILL.md（只一层）；软链接的 skill 目录只列名字、不读内容。"""
    if not _lstat_dir(base):
        return
    for e in _scandir(base):
        if budget.items >= MAX_ITEMS:
            budget.truncated = True
            return
        n = e.name
        if n.startswith(".") or not NAME_RE.match(n):
            continue
        name = prefix + n
        try:
            if e.is_symlink():
                out.append(_entry(name, src, "skill", plugin=plugin, link=True))
                budget.items += 1
                continue
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        r = _load_md(os.path.join(base, n, "SKILL.md"), budget)
        if r is None or not _invocable(r[0]):
            continue
        out.append(_entry(name, src, "skill", r[0], r[1], plugin))
        budget.items += 1


def _project_dirs(cwd, home):
    """cwd 以及往上的目录（不含 home 和 /，home 的 .claude 是用户级），近的在前"""
    out = []
    p = os.path.normpath(cwd)
    home = os.path.normpath(home)
    for _ in range(PROJECT_LEVELS):
        if p == home or p == "/" or not p:
            break
        out.append(p)
        q = os.path.dirname(p)
        if q == p:
            break
        p = q
    return out


def _enabled_plugins(claude_dir, proj_claude_dirs):
    en = {}
    srcs = [os.path.join(claude_dir, "settings.json")] if claude_dir else []
    for d in reversed(proj_claude_dirs):   # 远的先、近的后（近的覆盖）
        srcs.append(os.path.join(d, "settings.json"))
        srcs.append(os.path.join(d, "settings.local.json"))
    for f in srcs:
        js = _read_json(f)
        ep = js.get("enabledPlugins") if isinstance(js, dict) else None
        js = None   # 其它键（env 等）立刻丢掉
        if isinstance(ep, dict):
            for k, v in ep.items():
                if isinstance(k, str) and isinstance(v, bool):
                    en[k] = v
    return [k for k, v in en.items() if v]


def _plugin_roots(home, claude_dir, enabled):
    """已启用插件 → [(插件名, 安装目录)]；安装目录必须在 ~/.claude/plugins/ 之下且每一级都是真目录"""
    if not claude_dir or not enabled:
        return []
    plugins_dir = os.path.join(claude_dir, "plugins")
    if not _lstat_dir(plugins_dir):
        return []
    js = _read_json(os.path.join(plugins_dir, "installed_plugins.json"))
    table = js.get("plugins") if isinstance(js, dict) else None
    if not isinstance(table, dict):
        return []
    out = []
    for pid in enabled[:50]:
        ent = table.get(pid)
        if isinstance(ent, list):
            ent = ent[-1] if ent else None
        if not isinstance(ent, dict):
            continue
        ip = ent.get("installPath")
        if not isinstance(ip, str) or "\x00" in ip:
            continue
        ip = os.path.normpath(ip)
        rel = os.path.relpath(ip, plugins_dir) if ip.startswith(plugins_dir + os.sep) else None
        if not rel or rel.startswith(".."):
            continue
        root = _real_chain(plugins_dir, rel.split(os.sep))
        if root is None:
            continue
        name = pid.split("@", 1)[0]
        meta = _read_json(os.path.join(root, ".claude-plugin", "plugin.json")) if _lstat_dir(
            os.path.join(root, ".claude-plugin")) else None
        if isinstance(meta, dict) and isinstance(meta.get("name"), str) and NAME_RE.match(meta["name"]):
            name = meta["name"]
        if NAME_RE.match(name):
            out.append((name, root))
    return out


def _expand(p, home):
    if p == "~" or p.startswith("~/"):
        return home + p[1:]
    return p


def list_commands(home, cwd):
    home = os.path.normpath(os.path.expanduser(home or "~"))
    budget = Budget()
    out = []
    notes = []
    claude_dir = os.path.join(home, ".claude")
    if not _lstat_dir(claude_dir):
        if os.path.islink(claude_dir):
            notes.append("~/.claude 是软链接，没有读取（不跟随软链接）")
        claude_dir = None
    proj = []
    cwd = _expand(cwd or "", home)
    if cwd.startswith("/"):
        for d in _project_dirs(cwd, home):
            c = os.path.join(d, ".claude")
            if _lstat_dir(c):
                proj.append(c)
    for c in proj:
        scan_commands(os.path.join(c, "commands"), "project", out, budget)
        scan_skills(os.path.join(c, "skills"), "project", out, budget)
    if claude_dir:
        scan_commands(os.path.join(claude_dir, "commands"), "user", out, budget)
        scan_skills(os.path.join(claude_dir, "skills"), "user", out, budget)
        for name, root in _plugin_roots(home, claude_dir, _enabled_plugins(claude_dir, proj)):
            scan_commands(os.path.join(root, "commands"), "plugin", out, budget, name + ":", name)
            scan_skills(os.path.join(root, "skills"), "plugin", out, budget, name + ":", name)
    seen = set()
    uniq = []
    for e in out:
        if e["name"] in seen:
            continue
        seen.add(e["name"])
        uniq.append(e)
    res = {"commands": uniq}
    if budget.truncated:
        res["truncated"] = True
    if notes:
        res["notes"] = notes
    return res


def list_files(cwd, home=None):
    home = os.path.normpath(os.path.expanduser(home or "~"))
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd or len(cwd) > 4096:
        return {"__error__": "bad_request", "message": "cwd 不合法"}
    root = os.path.normpath(_expand(cwd, home))
    if not root.startswith("/"):
        return {"__error__": "bad_request", "message": "cwd 必须是绝对路径"}
    if not os.path.isdir(root):
        return {"__error__": "not_found", "message": "会话目录不存在（或这台机器上看不到）：%s" % root}
    t0 = time.monotonic()
    entries = []
    truncated = False
    q = deque([("", 0)])
    scanned = 0
    while q:
        rel, depth = q.popleft()
        if scanned >= FILE_DIRS or time.monotonic() - t0 > FILE_BUDGET:
            truncated = True
            break
        scanned += 1
        names = []
        try:
            with os.scandir(os.path.join(root, rel) if rel else root) as it:
                for e in it:
                    names.append(e)
                    if len(names) >= FILE_DIR_ENTRIES:
                        truncated = True
                        break
        except OSError:
            continue
        names.sort(key=lambda e: (e.name.lower(), e.name))
        subdirs = []
        for e in names:
            n = e.name
            if n in IGNORE_FILES or _BAD_CHARS.search(n):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if n in IGNORE_DIRS:
                    continue
                entries.append(rel + n + "/")
                subdirs.append(rel + n + "/")
            else:
                entries.append(rel + n)   # 普通文件与软链接（不跟随）
            if len(entries) >= FILE_MAX:
                truncated = True
                break
        if len(entries) >= FILE_MAX:
            break
        if depth + 1 < FILE_DEPTH:
            for sd in subdirs:
                q.append((sd, depth + 1))
        elif subdirs:
            truncated = True
    res = {"root": root, "entries": entries}
    if truncated:
        res["truncated"] = True
    return res


def main(argv):
    if len(argv) == 3 and argv[0] == "commands":
        return list_commands(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "files":
        return list_files(argv[2], argv[1])
    return {"__error__": "bad_request", "message": "未知命令"}


def _run():
    try:
        res = main(sys.argv[1:])
    except Exception as e:  # noqa
        res = {"__error__": "error", "message": "补全脚本异常：%s: %s" % (type(e).__name__, e)}
    data = json.dumps(res, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()


if __name__ == "__main__":
    _run()
'''

# 本进程里 exec 同一份脚本，导出纯函数（__name__ 不是 "__main__"，不会执行 _run）
_NS: Dict[str, Any] = {"__name__": "ccr_complete_remote_lib"}
exec(compile(SCRIPT, "<ccr.complete.SCRIPT>", "exec"), _NS)

parse_frontmatter = _NS["parse_frontmatter"]
list_commands = _NS["list_commands"]
list_files = _NS["list_files"]
script_main = _NS["main"]

LOCAL_JOBID = "local"
KINDS = ("commands", "files")
_ERR_STATUS = {"bad_request": 400, "not_found": 404, "forbidden": 403, "error": 502}


class CompleteError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _remote():
    import importlib
    return importlib.import_module("ccr.remote")


RunPython = Callable[[dict, str, List[str], int], Tuple[int, str, str]]


def _default_run_python(cfg, script, args, timeout):
    return _remote().run_remote_python(cfg, script, args, timeout=timeout)


class CompleteService:
    """GET /api/sessions/<sid>/complete/{commands,files} 背后的逻辑（只读）。"""

    def __init__(self, get_config: Callable[[], dict], *, run_python: Optional[RunPython] = None,
                 max_remote: int = 2):
        self.get_config = get_config
        self._run_python = run_python or _default_run_python
        self._sem = threading.BoundedSemaphore(max(1, max_remote))

    def _home(self, cfg: dict, local: bool) -> Optional[str]:
        """本进程里执行时用的 HOME：fake = 假 claude 的 HOME（本机 / 服务器各一套）；本机 = 本机用户；服务器 = None（走 ssh）"""
        if cfg.get("fake"):
            return _remote().fake_home(cfg, local=local)
        if local:
            return os.path.expanduser("~")
        return None

    def get(self, meta: dict, kind: str) -> Dict[str, Any]:
        if kind not in KINDS:
            raise CompleteError(404, "接口不存在")
        if not isinstance(meta, dict):
            raise CompleteError(404, "会话不存在")
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not cwd.startswith(("/", "~")) or "\x00" in cwd or "\n" in cwd or len(cwd) > 4096:
            raise CompleteError(409, "会话目录未知，暂时无法补全")
        cfg = self.get_config() or {}
        local = meta.get("jobid") == LOCAL_JOBID
        home = self._home(cfg, local)
        if home is not None:
            try:
                res = script_main([kind, home, cwd])
            except Exception as e:  # noqa: BLE001 — 只读扫描出意外也只报错，不影响服务
                raise CompleteError(502, "读取失败：%s: %s" % (type(e).__name__, e)) from None
        else:
            res = self._remote_call(cfg, [kind, "~", cwd])
        if not isinstance(res, dict):
            raise CompleteError(502, "补全数据格式不正确")
        if "__error__" in res:
            raise CompleteError(_ERR_STATUS.get(res.get("__error__"), 502), str(res.get("message") or "读取失败"))
        res["target"] = "local" if local else "server"
        return res

    def _remote_call(self, cfg: dict, args: List[str]) -> Dict[str, Any]:
        if not self._sem.acquire(timeout=60):
            raise CompleteError(503, "服务器查询排队超时，请稍后重试")
        try:
            try:
                rc, out, err = self._run_python(cfg, SCRIPT, args, 40)
            except subprocess.TimeoutExpired:
                raise CompleteError(504, "服务器查询超时") from None
            except (OSError, ValueError) as e:
                raise CompleteError(502, "无法执行服务器命令：%s" % e) from None
        finally:
            self._sem.release()
        for line in reversed((out or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    break
        tail = (err or "").strip().splitlines()[-1:] or [""]
        if rc == 255:
            raise CompleteError(502, "ssh 连接失败：%s" % tail[0][:300])
        raise CompleteError(502, "服务器查询失败（退出码 %s）：%s" % (rc, tail[0][:300]))
