"""会话统计：上下文 / 会话总花费 / token/s（docs/SPEC_V2_TUI.md §4.2 meta.stats）。

只做**被动读取**：输入是 ccr.tui_parse.parse_screen 的结果（状态栏、spinner）与 transcript 条目（usage），
不发任何按键、不改 Claude 的任何配置（红线 §0）。纯 Python、无 I/O；由 ccr.session 在 s.lock 下调用。

数据来源与口径
- 上下文：优先状态栏（用户自己的 statusline 里的 "25k/1M"、"3%"）；
  后备 = 最近一条主链 assistant 的 input_tokens + cache_creation_input_tokens + cache_read_input_tokens，
  上限按模型推断（状态栏 / 模型名 / /model 面板描述里有 1M 或 [1m] → 1,000,000，否则 200,000），标 approx。
- 总花费：只用状态栏里的 "$x"（Claude Code 自己算好交给 statusline 的会话花费）；没有就是 None，绝不自己估价。
- token/s：spinner "(5s · ↓ 175 tokens …)"。
  * 生成中（tok_s_live）：相邻帧 tokens 增量 ÷ 本地收到帧的时间差，在最近 LIVE_WINDOW 秒的单调段上平滑；
    计数回落（新请求开始时 spinner 的计数动画会先往下走）或 elapsed 归零 = 新的一段，重新开始。
  * 上一轮平均（tok_s_last）：本轮每一段（一次请求，elapsed 归零即换段）最后看到的 tokens 之和 ÷ 对应 elapsed 之和
    （= spinner 显示的"该轮最终 tokens / 该轮用时"；含 thinking token 与首 token 等待）。
"""
from __future__ import annotations

import collections
import math
from typing import Dict, Iterable, Optional

from . import tui_parse

CTX_DEFAULT_LIMIT = 200_000
CTX_1M_LIMIT = 1_000_000

# meta.stats 的字段（持久化/恢复时按此校验）
NUM_FIELDS = ("context_used", "context_limit", "context_pct", "cost_usd", "tok_s_live", "tok_s_last",
              "last_turn_tokens", "last_turn_elapsed_s", "updated_at")
STR_FIELDS = {"context_source": ("status", "transcript"), "cost_source": ("status",)}


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _usage_context(entry) -> Optional[int]:
    """主链 assistant 条目的 usage → 本次请求的上下文 token 数（input + cache_creation + cache_read）。"""
    if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
        return None
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None
    total = 0
    for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = u.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
            total += int(v)
    return total or None


def sanitize(d) -> Optional[Dict]:
    """持久化的 meta.stats → 只保留合法字段（app 重启后恢复最后已知值；生成中速率不恢复）。"""
    if not isinstance(d, dict):
        return None
    out: Dict = {}
    for k in NUM_FIELDS:
        v = _num(d.get(k))
        out[k] = v if v is not None and v >= 0 else None
    for k, allowed in STR_FIELDS.items():
        v = d.get(k)
        out[k] = v if v in allowed else None
    ct = d.get("cost_text")
    out["cost_text"] = ct if isinstance(ct, str) and len(ct) <= 32 and ct.startswith("$") else None
    out["context_approx"] = bool(d.get("context_approx"))
    out["tok_s_live"] = None
    for k in ("context_used", "context_limit", "last_turn_tokens"):
        if out[k] is not None:
            out[k] = int(out[k])
    if all(out[k] is None for k in ("context_used", "context_pct", "cost_usd", "tok_s_last")):
        return None
    return out


class StatsTracker:
    LIVE_WINDOW = 4.0     # 生成中速率的平滑窗口（秒）
    LIVE_MIN_SPAN = 0.5   # 单调段跨度至少这么长才给出（更新）速率（抓屏 250ms 一帧 → 至少 3 帧）
    LIVE_HOLD = 3.0       # 忙但 spinner 暂时不在屏幕上（长输出时实测会消失几秒）：最近速率最多保留这么久

    def __init__(self):
        # 状态栏：最近一次解析到的值（某帧解析不到 = 沿用上次，例如对话框遮住了状态栏）
        self.st_used: Optional[int] = None
        self.st_limit: Optional[int] = None
        self.st_pct: Optional[float] = None
        self.st_cost: Optional[float] = None
        self.st_cost_text: Optional[str] = None
        self.status_text: Optional[str] = None
        # transcript：最近一条主链 assistant 的上下文
        self.tr_used: Optional[int] = None
        self.tr_model: Optional[str] = None
        # 本轮（busy 期间）的 spinner 采样
        self.in_turn = False
        self.segs: list = []          # 每段 [tokens, elapsed_s]（该段最后一次看到的值）
        self.prev = None              # 上一个采样 (elapsed_s, tokens, token_dir)
        self.run: "collections.deque" = collections.deque()   # 当前单调段内的 (t, tokens)
        self.live: Optional[float] = None
        self.live_t = 0.0
        # 上一轮
        self.last: Optional[float] = None
        self.last_tokens: Optional[int] = None
        self.last_elapsed: Optional[float] = None

    # ------------------------------------------------------------ 输入
    def restore(self, d: Optional[Dict]) -> None:
        """用持久化的 meta.stats 预置"最后已知值"（屏幕/transcript 到达后会被覆盖）。"""
        d = sanitize(d)
        if not d:
            return
        if d.get("context_source") == "status":
            self.st_used = d.get("context_used")
            self.st_limit = d.get("context_limit")
            self.st_pct = d.get("context_pct")
        elif d.get("context_source") == "transcript":
            self.tr_used = d.get("context_used")
        if d.get("cost_source") == "status" and d.get("cost_usd") is not None:
            self.st_cost = d.get("cost_usd")
            self.st_cost_text = d.get("cost_text")
        self.last = d.get("tok_s_last")
        self.last_tokens = d.get("last_turn_tokens")
        self.last_elapsed = d.get("last_turn_elapsed_s")

    def on_transcript(self, entries: Iterable, reset: bool = False) -> None:
        if reset:
            self.tr_used = None
            self.tr_model = None
        for e in entries or ():
            c = _usage_context(e)
            if c is not None:
                self.tr_used = c
                m = (e.get("message") or {}).get("model")
                self.tr_model = m if isinstance(m, str) and len(m) < 200 else None

    def on_screen(self, st: Optional[Dict], t: float) -> None:
        """一帧屏幕的解析结果。t = 本地单调时钟（秒）。"""
        if not isinstance(st, dict):
            return
        ss = st.get("status_stats")
        if isinstance(ss, dict):
            if ss.get("context_pct") is not None or ss.get("context_used") is not None:
                self.st_used = ss.get("context_used")
                self.st_limit = ss.get("context_limit")
                self.st_pct = ss.get("context_pct")
            if ss.get("cost_usd") is not None:
                self.st_cost = ss.get("cost_usd")
                self.st_cost_text = ss.get("cost_text")
        if st.get("status_line"):
            self.status_text = st["status_line"][:400]
        if st.get("dialog"):
            return   # 对话框（权限/提问/面板）：本轮暂停，既不采样也不结束
        if st.get("busy"):
            if not self.in_turn:
                self._begin_turn()
            info = st.get("spinner_info")
            if isinstance(info, dict) and isinstance(info.get("tokens"), int):
                self._sample(t, info)
            elif self.live is not None and t - self.live_t > self.LIVE_HOLD:
                self.live = None
            return
        if self.in_turn and ((st.get("input") or {}).get("visible") or st.get("turn_done")):
            self._end_turn()

    def stop(self) -> None:
        """进程已退出 / 重启：丢掉进行中的这一轮（不计入上一轮平均）。"""
        self.in_turn = False
        self.segs = []
        self.prev = None
        self.run.clear()
        self.live = None

    # ------------------------------------------------------------ 速率
    def _begin_turn(self) -> None:
        self.stop()
        self.in_turn = True

    def _sample(self, t: float, info: Dict) -> None:
        tok = info["tokens"]
        el = info.get("elapsed_s")
        el = float(el) if isinstance(el, (int, float)) and not isinstance(el, bool) else None
        d = info.get("token_dir")
        prev = self.prev
        new_seg = (prev is None or d != prev[2]
                   or (el is not None and prev[0] is not None and el < prev[0]))
        restart = False
        if new_seg:
            self.segs.append([tok, el])
            restart = prev is not None   # 本轮的第二段起（新的一次请求）：此刻并没有在生成
            self.run.clear()
        else:
            seg = self.segs[-1]
            seg[0] = tok
            if el is not None:
                seg[1] = el
        if self.run and (tok < self.run[-1][1] or t <= self.run[-1][0]):
            if tok < self.run[-1][1]:
                self.run.clear()          # 计数回落（新请求开始时的计数动画）：新的单调段
                restart = True
            else:
                self.run.pop()            # 同一时刻的重复帧：只留最新
        self.run.append((t, tok))
        if restart:
            self.live, self.live_t = 0.0, t
            self.prev = (el, tok, d)
            return
        while len(self.run) > 2 and t - self.run[1][0] >= self.LIVE_WINDOW:
            self.run.popleft()
        t0, k0 = self.run[0]
        span = t - t0
        if span >= self.LIVE_MIN_SPAN:
            self.live = max(0.0, (tok - k0) / span)
            self.live_t = t
        elif self.live is not None and t - self.live_t > self.LIVE_HOLD:
            self.live = None
        self.prev = (el, tok, d)

    def _end_turn(self) -> None:
        toks = sum(s[0] for s in self.segs if isinstance(s[0], int))
        els = sum(s[1] for s in self.segs if isinstance(s[1], float))
        if toks > 0 and els > 0:
            self.last = toks / els
            self.last_tokens = toks
            self.last_elapsed = els
        self.stop()

    # ------------------------------------------------------------ 输出
    def _infer_limit(self, hints: Iterable) -> int:
        texts = [self.status_text, self.tr_model] + list(hints or ())
        return CTX_1M_LIMIT if any(tui_parse.has_1m_hint(x) for x in texts if isinstance(x, str)) \
            else CTX_DEFAULT_LIMIT

    def snapshot(self, hints: Iterable = ()) -> Optional[Dict]:
        """meta.stats（不含 updated_at；什么都不知道时返回 None）。hints = 推断上下文上限用的模型名/描述文本。"""
        used = limit = pct = None
        src = None
        approx = False
        if self.st_pct is not None or self.st_used is not None:
            used, limit, pct, src = self.st_used, self.st_limit, self.st_pct, "status"
        elif self.tr_used is not None:
            used, src, approx = self.tr_used, "transcript", True
            limit = self._infer_limit(hints)
            pct = round(used * 100.0 / limit, 1)
        live = self.live if self.in_turn else None
        out = {
            "context_used": used, "context_limit": limit,
            "context_pct": round(pct, 1) if pct is not None else None,
            "context_source": src, "context_approx": approx,
            "cost_usd": self.st_cost, "cost_text": self.st_cost_text if self.st_cost is not None else None,
            "cost_source": "status" if self.st_cost is not None else None,
            "tok_s_live": round(live, 1) if live is not None else None,
            "tok_s_last": round(self.last, 1) if self.last is not None else None,
            "last_turn_tokens": self.last_tokens,
            "last_turn_elapsed_s": self.last_elapsed,
        }
        if all(out[k] is None for k in ("context_used", "context_pct", "cost_usd", "tok_s_live", "tok_s_last")):
            return None
        return out
