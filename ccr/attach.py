"""本机会话发图片的纯工具（SPEC §4.2「图片」、§9.8）。

做法与用户在终端里操作完全等价，不另存任何文件、不读系统剪贴板：
- 粘贴截图：用户在输入框按 ⌘V 且浏览器的 paste 事件里有图片 → 给 claude 发 Ctrl+V（claude 自己读剪贴板）；
- 拖入图片：原生窗口拿到被拖入文件的路径 → 按终端拖放的规则转义 → 作为一次 bracketed paste 粘进 claude 输入框，
  claude 自己把图片路径识别成 "[Image #N]"（CLI 2.1.332 实测：单独一次粘贴一个路径才会识别；路径 + 文字、
  换行分隔的多个路径都只是普通文字）。

本模块只做路径校验与转义；读文件也只读开头 16 字节核对魔数（避免把改了扩展名的非图片交给 claude）。
另：图片条的缩略图 / 灯箱要看拖入的图片（read_image_file）：只读本程序刚校验并粘贴过的那个文件（同一个 dev/inode），
再核对一次魔数，不是任意文件读取（白名单在 session 里，见 SessionManager.read_drop_image）。
"""
from __future__ import annotations

import os
import re
import stat
from typing import Optional

# claude 认作图片路径的扩展名（实测 png/jpg/gif/webp 与大写 .PNG 都会变成 [Image #N]；.txt 不会）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_PATHS = 10            # 一次最多拖入的图片数
MAX_PATH_LEN = 4096
MAX_IMAGE_BYTES = 100 * 1024 * 1024   # 再大的截图也到不了这里；超过多半是拖错了文件
MAX_SERVE_BYTES = 50 * 1024 * 1024    # 缩略图 / 灯箱最多读这么大（更大的只显示图标）
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# 终端拖放的转义（终端.app / iTerm2 把文件拖进来时，路径里的空格和 shell 特殊字符前面加反斜杠）：
# 安全字符原样保留，其余 ASCII 字符前加 "\"；非 ASCII（中文文件名、macOS 截图名里的窄空格 U+202F 等）不转义。
# 实测 claude 2.1.332 对 "my\ shot.png"、"a\ \(1\)\&b.png"、"截屏2026-11-25\ 21.00.00.png" 都识别为图片。
_UNSAFE_RE = re.compile(r"([^A-Za-z0-9_./+,:@%=\-\u0080-\U0010ffff])")


def image_kind(head: bytes) -> Optional[str]:
    """按文件开头的魔数判断图片类型：png / jpeg / gif / webp；都不是 → None。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def is_image_name(path: str) -> bool:
    return isinstance(path, str) and path.lower().endswith(IMAGE_EXTS)


def escape_drop_path(path: str) -> str:
    """终端拖放式转义：/a/my shot (1).png → /a/my\\ shot\\ \\(1\\).png"""
    return _UNSAFE_RE.sub(r"\\\1", path)


def check_image_path(path) -> str:
    """校验一个被拖入的本机文件路径，返回原路径；不合格抛 ValueError（中文原因，不含文件内容）。
    要求：绝对路径、没有控制字符（换行会把一次粘贴拆成多行）、扩展名是 claude 认的图片、是普通文件（跟随软链接）、
    非空且不超过上限、开头魔数是 png/jpeg/gif/webp。"""
    return check_image_file(path)[0]


class ImageFileError(ValueError):
    """read_image_file 失败：status = 建议的 HTTP 状态（404 文件不在了 / 变了，413 太大，415 不是图片）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def read_image_file(path: str, ident, max_bytes: int = MAX_SERVE_BYTES) -> tuple:
    """读一个之前经 check_image_file 校验过的图片文件（缩略图 / 灯箱用）→ (Content-Type, 字节)。
    ident = 校验时记下的 (st_dev, st_ino)：文件被删、被换成别的文件（inode 不同）→ 404；不是普通文件 → 404；
    超过 max_bytes → 413；开头魔数不是 png/jpeg/gif/webp → 415。只读、不写任何东西。"""
    name = os.path.basename(path) or path
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    except OSError:
        raise ImageFileError(404, "「%s」已经不在了" % name) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != tuple(ident):
            raise ImageFileError(404, "「%s」已被移动或替换" % name)
        if st.st_size > max_bytes:
            raise ImageFileError(413, "「%s」太大，不显示缩略图" % name)
        chunks, total = [], 0
        while total <= max_bytes:
            b = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
            if not b:
                break
            chunks.append(b)
            total += len(b)
        if total > max_bytes:
            raise ImageFileError(413, "「%s」太大，不显示缩略图" % name)
    except OSError:
        raise ImageFileError(404, "无法读取「%s」" % name) from None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    kind = image_kind(data[:16])
    if kind is None:
        raise ImageFileError(415, "「%s」的内容不是图片" % name)
    return "image/" + kind, data


def check_image_file(path) -> tuple:
    """check_image_path 的完整版：返回 (路径, (st_dev, st_ino))——身份取自校验时打开的那个文件描述符。"""
    if not isinstance(path, str) or not path:
        raise ValueError("路径必须是非空字符串")
    if len(path) > MAX_PATH_LEN:
        raise ValueError("路径过长")
    if _CTRL_RE.search(path):
        raise ValueError("路径里有控制字符（如换行），无法作为图片粘贴")
    if not path.startswith("/"):
        raise ValueError("必须是绝对路径")
    name = os.path.basename(path) or path
    if not is_image_name(path):
        raise ValueError("「%s」不是图片（只支持 %s）" % (name, " / ".join(e[1:] for e in IMAGE_EXTS)))
    try:
        st = os.stat(path)
    except OSError:
        raise ValueError("找不到文件「%s」" % name) from None
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("「%s」不是普通文件" % name)
    if st.st_size == 0:
        raise ValueError("「%s」是空文件" % name)
    if st.st_size > MAX_IMAGE_BYTES:
        raise ValueError("「%s」太大（超过 %d MB）" % (name, MAX_IMAGE_BYTES // (1024 * 1024)))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    except OSError:
        raise ValueError("无法读取「%s」" % name) from None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise ValueError("「%s」不是普通文件" % name)
        head = os.read(fd, 16)
    except OSError:
        raise ValueError("无法读取「%s」" % name) from None
    finally:
        os.close(fd)
    if image_kind(head) is None:
        raise ValueError("「%s」的内容不是 png / jpeg / gif / webp 图片" % name)
    return path, (fst.st_dev, fst.st_ino)
