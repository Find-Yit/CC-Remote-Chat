"""图片引用（ccr_ref）的本地取回：校验 ref 格式、按文件头识别图片类型、服务端小 LRU（docs/SPEC_V2_TUI.md §9.1）。

node agent 把 transcript 里的大图片 base64 换成引用（见 node_agent.refify_entry）；前端需要显示时
GET /api/sessions/<sid>/blob/<ref> → server 先查这里的 LRU，未命中再经 SessionManager.get_blob 调 agent 的
blob op 重读、校验 hash，解码后按文件头确定 Content-Type（只认 png/jpeg/gif/webp，其它一律拒绝）。
"""
from __future__ import annotations

import base64
import binascii
import collections
import re
import threading
from typing import Optional, Tuple

# m.<sessionId>.<偏移>.<块路径>.<hash16> / a.<sessionId>.<agentId>.<偏移>.<块路径>.<hash16>
REF_RE = re.compile(r"^(?:m\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                    r"|a\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                    r"\.[A-Za-z0-9_-]{1,64})\.[0-9]{1,15}\.[0-9]{1,4}(?:-[0-9]{1,4})?\.[0-9a-f]{16}\Z")
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def valid_ref(ref) -> bool:
    return isinstance(ref, str) and len(ref) <= 200 and REF_RE.match(ref) is not None


def ref_session_id(ref: str) -> Optional[str]:
    return ref.split(".")[1] if valid_ref(ref) else None


def sniff_image(data: bytes) -> Optional[str]:
    """按文件头识别图片类型（transcript 里的 media_type 可能与实际内容不符，以内容为准）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image(b64) -> Tuple[str, bytes]:
    """agent 返回的 base64 → (Content-Type, 字节)；不是受支持的图片抛 ValueError。"""
    if not isinstance(b64, str) or not b64:
        raise ValueError("图片数据为空")
    try:
        raw = base64.b64decode(b64.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise ValueError("图片数据不是合法的 base64") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片过大")
    ctype = sniff_image(raw)
    if ctype is None:
        raise ValueError("不支持的图片格式（只显示 png/jpeg/gif/webp）")
    return ctype, raw


class BlobCache:
    """(sid, ref) → (Content-Type, 字节) 的小 LRU（按总字节数淘汰）。ref 带内容 hash，命中即正确。"""

    def __init__(self, max_bytes: int = 64 * 1024 * 1024, max_items: int = 256):
        self.max_bytes = max_bytes
        self.max_items = max_items
        self._d: "collections.OrderedDict[tuple, Tuple[str, bytes]]" = collections.OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, sid: str, ref: str) -> Optional[Tuple[str, bytes]]:
        with self._lock:
            v = self._d.get((sid, ref))
            if v is not None:
                self._d.move_to_end((sid, ref))
            return v

    def put(self, sid: str, ref: str, ctype: str, data: bytes) -> None:
        if len(data) > self.max_bytes // 4:
            return
        with self._lock:
            old = self._d.pop((sid, ref), None)
            if old is not None:
                self._size -= len(old[1])
            self._d[(sid, ref)] = (ctype, data)
            self._size += len(data)
            while self._d and (self._size > self.max_bytes or len(self._d) > self.max_items):
                _, (_, b) = self._d.popitem(last=False)
                self._size -= len(b)

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)
