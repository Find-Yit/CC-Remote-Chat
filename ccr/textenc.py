"""文本编码检测（文件预览用，docs/SPEC_V2_TUI.md §10.10）：UTF-8 优先，不是有效 UTF-8 时试 GB18030（GBK 的超集）。

- 像二进制（有 NUL、控制字符过多）→ None，绝不把二进制当文本；
- partial=True：只拿到了文件开头（截断在多字节字符中间也算有效）；
- 只用于给 /preview 响应标正确的 charset（「在浏览器中打开」也能正常显示）与前端不支持 GB18030 时的转码兜底，
  前端自己仍会按字节解码（static/textdecode.js，规则相同）。
"""
from __future__ import annotations

import codecs
from typing import Optional, Tuple

# 文本里常见的控制字符：\t \n \v \f \r 与 ESC（日志里的 ANSI 颜色）
_OK_CTRL = frozenset((9, 10, 11, 12, 13, 27))
_CTRL = bytes(b for b in range(32) if b not in _OK_CTRL) + b"\x7f"
SAMPLE = 64 * 1024
TEXTUAL = ("text/", "application/json", "application/xml", "application/xhtml+xml", "image/svg+xml",
           "application/javascript")
MARKUP = ("text/html", "application/xhtml+xml", "application/xml", "image/svg+xml")


def looks_binary(data: bytes) -> bool:
    """有 NUL，或开头 64KB 里的控制字符（不含 \\t \\n \\r \\f \\v ESC）超过 1%（至少 8 个）。"""
    head = data[:SAMPLE]
    if b"\x00" in head:
        return True
    n = len(head) - len(head.translate(None, _CTRL))
    return n > max(8, len(head) // 100)


def _decodes(data: bytes, enc: str, partial: bool) -> bool:
    """能否严格解码（按 1MB 分块喂给增量解码器、丢掉结果：50MB 的文件也不会一次生成整个字符串）。"""
    try:
        dec = codecs.getincrementaldecoder(enc)("strict")
        step = 1 << 20
        for i in range(0, len(data), step):
            dec.decode(data[i:i + step], final=False)
        dec.decode(b"", final=not partial)
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def detect(data: bytes, partial: bool = False) -> Optional[str]:
    """"utf-8" / "gb18030" / None（像二进制，或两种都解不了）。"""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8"
    if looks_binary(data):
        return None
    if _decodes(data, "utf-8", partial):
        return "utf-8"
    if _decodes(data, "gb18030", partial):
        return "gb18030"
    return None


def is_textual(ctype: str) -> bool:
    base = (ctype or "").split(";")[0].strip().lower()
    return base.startswith(TEXTUAL)


def with_charset(ctype: str, data: bytes, partial: bool = False) -> Tuple[str, Optional[str]]:
    """文本类 Content-Type 按内容改 charset → (新 Content-Type, 检测到的非 UTF-8 编码或 None)。
    HTML/XML 不是 UTF-8 时去掉 charset（让浏览器按页面里的 <meta charset> 决定）；其它类型原样返回。"""
    if not is_textual(ctype):
        return ctype, None
    base = ctype.split(";")[0].strip()
    enc = detect(data, partial)
    if enc == "utf-8":
        return ctype, None
    if base in MARKUP:
        return base, enc     # 页面自己声明编码（GBK 网页一般都有 <meta charset>），不用响应头覆盖
    if enc is None:
        return ctype, None
    return base + "; charset=" + enc, enc


def to_utf8(data: bytes, enc: str) -> bytes:
    """GB18030 → UTF-8（前端不支持 TextDecoder('gb18030') 时的兜底转码；解不了的字节换成 U+FFFD）。"""
    return data.decode(enc, "replace").encode("utf-8")
