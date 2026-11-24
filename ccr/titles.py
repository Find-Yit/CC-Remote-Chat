"""会话标题：与 Claude Code CLI 自己显示的标题一致（/resume 列表里的那个）。

优先级（与 ccr.history.summarize_lines 相同）：custom-title（CLI 里 /rename）> ai-title（CLI 自动生成）
> summary > 首条用户输入（去掉 CLI 注入的标签）> 首条斜杠命令。数据只来自 transcript（只读）。
"""
from __future__ import annotations

from typing import Iterable, Optional

from .history import clean_title_text, is_system_injected, message_text

TITLE_TYPES = ("custom-title", "ai-title", "summary")


class TitleTracker:
    __slots__ = ("custom", "ai", "summary", "first_user", "first_cmd")

    def __init__(self):
        self.clear()

    def clear(self) -> None:
        self.custom = self.ai = self.summary = None
        self.first_user = self.first_cmd = None

    def absorb(self, entries: Iterable, reset: bool = False) -> None:
        if reset:
            self.clear()
        for e in entries or ():
            if not isinstance(e, dict) or e.get("isSidechain"):
                continue
            t = e.get("type")
            if t == "custom-title":
                v = clean_title_text(e.get("customTitle"))
                if v:
                    self.custom = v
            elif t == "ai-title":
                v = clean_title_text(e.get("aiTitle"))
                if v:
                    self.ai = v
            elif t == "summary":
                v = clean_title_text(e.get("summary"))
                if v:
                    self.summary = v
            elif t == "user" and self.first_user is None:
                if e.get("isMeta") or e.get("isCompactSummary") or is_system_injected(e):
                    continue
                msg = e.get("message")
                if not isinstance(msg, dict):
                    continue
                raw = message_text(msg.get("content"))
                v = clean_title_text(raw)
                if not v:
                    continue
                if "<command-name>" in raw:
                    if self.first_cmd is None:
                        self.first_cmd = v
                else:
                    self.first_user = v

    def title(self) -> Optional[str]:
        return self.custom or self.ai or self.summary or self.first_user or self.first_cmd or None
