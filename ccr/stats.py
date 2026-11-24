"""会话统计：上下文 / 会话总花费 / token/s（docs/SPEC_V2_TUI.md §4.2 meta.stats）。

只做**被动读取**：输入是 ccr.tui_parse.parse_screen 的结果（状态栏、spinner）与 transcript 条目（usage），
不发任何按键、不改 Claude 的任何配置（红线 §0）。纯 Python、无 I/O；由 ccr.session 在 s.lock 下调用。

数据来源与口径
- 上下文：优先状态栏（用户自己的 statusline 里的 "25k/1M"、"3%"）；
  后备 = 最近一条主链 assistant 的 input_tokens + cache_creation_input_tokens + cache_read_input_tokens，标 approx；
  窗口（分母）按优先级取（context_limit_source）：
    1. context_cmd：CLI 自己输出的 /context（transcript 的 <local-command-stdout> / isMeta Markdown，或屏幕上的 /context）
       里的 "已用/窗口 tokens"，按该会话、该模型记下（最权威，覆盖下面的推断）；
    2. model：transcript 最新主链 assistant 的 message.model 查 MODEL_CONTEXT（CLI 2.1.331 内置模型目录）；
       hint：模型名 / 状态栏 / /model 面板描述 / 启动参数里有 [1m] 或大写 1M → 1,000,000；
       unknown_model：模型名不在表里（第三方网关的叫法、比 2.1.331 新的模型）→ 1,000,000，与 CLI 一致
       （CLI 2.1.331 对不认识的模型默认 1M，见 MODEL_CONTEXT 上方的注释）；
    3. observed：同一会话同一模型观测到的上下文 tokens 曾经超过推断的窗口 → 至少 1,000,000（只升不降；换模型重新判断）；
    4. default：200,000（还没有任何主链 assistant 条目、不知道模型时；此时也没有已用值，实际不显示）。
  已知限制：CLI 的 CLAUDE_CODE_DISABLE_1M_CONTEXT（一律 200k）/ CLAUDE_CODE_MAX_CONTEXT_TOKENS（指定窗口）等环境变量
  我们不读（红线：不碰 claude 的环境），只能靠 /context 的输出纠正。
  推断出来的分母不会让百分比超过 100%（超过时封顶 100）。
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
import re
from typing import Dict, Iterable, Optional, Tuple

from . import tui_parse

CTX_DEFAULT_LIMIT = 200_000
CTX_1M_LIMIT = 1_000_000

# 上下文窗口表：来自 Claude Code CLI 2.1.331 打包程序里的内置模型目录（bin/claude.exe 的
# "Hand-maintained baked-in model catalog"，models[].context）。id → (显示名, 默认窗口)。
# 默认窗口 = context.window；没有 context 字段的老模型 CLI 按 200k 算。
# 原生 1M（context.native_1m:true，window:1e6）：Haiku 5.5、Sonnet 5 / 5.5、Opus 4.7 / 4.8 / 5 / 5.5、Fable 5.x、Mythos 5.x。
# 其余是 200k；它们在 CLI 里变成 1M 的条件（同版本 nG()/XLr()）：模型串带 "[1m]" 后缀（/model 选 "… (1M context)"、
# --model x[1m]、ANTHROPIC_DEFAULT_*_MODEL=x[1m]），或请求带 context-1m beta 头且模型 supports_1m_beta
# （Sonnet 4 / 4.5 / 4.6、Opus 4.6）；CLAUDE_CODE_DISABLE_1M_CONTEXT=1 时一律 200k。transcript 里的 message.model
# 是 API 返回的 id、不带 [1m]，所以这些情况只能靠 [1m]/1M 提示、/context 或观测值（> 200k）来判断。
# 不在目录里的模型（同版本 ik()，偏移约 308,059,578："if(!ak(e))return aOt;let y=s.CLAUDE_CODE_MAX_CONTEXT_TOKENS;
# if(y!==void 0&&y>0)return y;return KF()?aOt:QB"，aOt=200000、QB=1e6、KF()=CLAUDE_CODE_DISABLE_1M_CONTEXT）：
# ak(e)（约 308,057,567）= 别名/覆盖解析后仍不是已知模型；h6()（约 308,043,122）= 目录 id 或 qv() 的
# claude-3-opus / claude-3-sonnet / claude-3-haiku。已知但目录没给窗口的 → 200k；不认识的 → 1M（/context 里写作
# "… tokens (default for an unrecognized model)"）。
MODEL_CONTEXT: Dict[str, Tuple[str, int]] = {
    "claude-3-opus": ("Opus 3", 200_000),
    "claude-3-sonnet": ("Sonnet 3", 200_000),
    "claude-3-haiku": ("Haiku 3", 200_000),
    "claude-3-5-haiku": ("Haiku 3.5", 200_000),
    "claude-haiku-4-5": ("Haiku 4.5", 200_000),
    "claude-haiku-5-5": ("Haiku 5.5", 1_000_000),
    "claude-3-5-sonnet": ("Sonnet 3.5", 200_000),
    "claude-3-7-sonnet": ("Sonnet 3.7", 200_000),
    "claude-sonnet-4-0": ("Sonnet 4", 200_000),
    "claude-sonnet-4-5": ("Sonnet 4.5", 200_000),
    "claude-sonnet-4-6": ("Sonnet 4.6", 200_000),
    "claude-sonnet-5": ("Sonnet 5", 1_000_000),
    "claude-sonnet-5-5": ("Sonnet 5.5", 1_000_000),
    "claude-opus-4-0": ("Opus 4", 200_000),
    "claude-opus-4-1": ("Opus 4.1", 200_000),
    "claude-opus-4-5": ("Opus 4.5", 200_000),
    "claude-opus-4-6": ("Opus 4.6", 200_000),
    "claude-opus-4-7": ("Opus 4.7", 1_000_000),
    "claude-opus-4-8": ("Opus 4.8", 1_000_000),
    "claude-opus-5": ("Opus 5", 1_000_000),
    "claude-opus-5-5": ("Opus 5.5", 1_000_000),
    "claude-fable-5": ("Fable 5", 1_000_000),
    "claude-fable-5-1": ("Fable 5.1", 1_000_000),
    "claude-fable-5-2": ("Fable 5.2", 1_000_000),
    "claude-mythos-5": ("Mythos 5", 1_000_000),
    "claude-mythos-5-1": ("Mythos 5.1", 1_000_000),
    "claude-mythos-5-2": ("Mythos 5.2", 1_000_000),
}
# 目录里 first_party id 与目录 id 不同的老模型（去掉日期后）
_MODEL_ALIASES = {"claude-opus-4": "claude-opus-4-0", "claude-sonnet-4": "claude-sonnet-4-0"}
LIMIT_SOURCES = ("context_cmd", "model", "hint", "unknown_model", "observed", "default")
_MODEL_LABEL_RE = re.compile(r"^[\w .:@/\[\]()-]{1,64}$")


def norm_model(model) -> Optional[str]:
    """模型串 → 归一化 id（小写，去掉 [1m]、Bedrock/Vertex 前后缀与日期）：
    'us.anthropic.claude-haiku-4-5-20251001-v1:0' → 'claude-haiku-4-5'、'claude-opus-5-5[1m]' → 'claude-opus-5-5'。"""
    if not isinstance(model, str):
        return None
    m = model.strip().lower()
    if not m or len(m) > 200:
        return None
    m = re.sub(r"\[\d+m\]$", "", m)
    m = re.sub(r"^(?:[a-z]{2,6}\.)?anthropic\.", "", m)
    m = re.sub(r"@\d{8}$", "", m)
    m = re.sub(r"-v\d+(?::\d+)?$", "", m)
    m = re.sub(r"-\d{8}$", "", m)
    return _MODEL_ALIASES.get(m, m) or None


def model_context(model) -> Optional[Tuple[str, int]]:
    """模型串 → (显示名, 默认窗口)；不在表里返回 None。"""
    return MODEL_CONTEXT.get(norm_model(model) or "")


def _label(v) -> Optional[str]:
    return v if isinstance(v, str) and _MODEL_LABEL_RE.match(v) else None

# meta.stats 的字段（持久化/恢复时按此校验）
NUM_FIELDS = ("context_used", "context_limit", "context_pct", "cost_usd", "tok_s_live", "tok_s_last",
              "last_turn_tokens", "last_turn_elapsed_s", "updated_at")
STR_FIELDS = {"context_source": ("status", "transcript"), "cost_source": ("status",),
              "context_limit_source": LIMIT_SOURCES}


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


def _context_output(entry) -> Optional[Dict]:
    """主链里 /context 的输出条目（system local_command 的 <local-command-stdout>，或 isMeta user 的 Markdown 版）
    → tui_parse.parse_context_usage 的结果。"""
    if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("type") not in ("system", "user"):
        return None
    c = entry.get("content")
    if c is None and isinstance(entry.get("message"), dict):
        c = entry["message"].get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
    if not isinstance(c, str) or len(c) > 200_000 or "ontext" not in c[:400]:
        return None
    if not ("<local-command-stdout>" in c or c.lstrip().startswith("## Context Usage")):
        return None
    return tui_parse.parse_context_usage(c)


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
    out["context_model"] = _label(d.get("context_model"))
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
        # 上下文窗口的依据（同一个 claude 进程里不随 /clear 变化，transcript reset 时保留）
        self.ctx_windows: Dict[str, Tuple[int, Optional[str]]] = {}   # 归一化模型 → (/context 给出的窗口, 显示名)
        self.ctx_last: Optional[Tuple[int, Optional[str], Optional[str]]] = None  # 最近一次 /context：(窗口, 显示名, 当时的 tr 模型)
        self.peaks: Dict[str, int] = {}          # 归一化模型 → 观测到的最大上下文 tokens（只升不降）
        self.rs_limit: Optional[Tuple[int, str, Optional[str]]] = None   # 持久化恢复的 (窗口, 来源, 模型名)，transcript 到达前用
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
            if d.get("context_limit") and d.get("context_limit_source"):
                self.rs_limit = (d["context_limit"], d["context_limit_source"], d.get("context_model"))
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
                self.rs_limit = None
                m = (e.get("message") or {}).get("model")
                self.tr_model = m if isinstance(m, str) and len(m) < 200 else None
                k = norm_model(self.tr_model) or ""
                if c > self.peaks.get(k, 0):
                    self.peaks[k] = c
                continue
            cu = _context_output(e)
            if cu is not None:
                self._note_context(cu)

    def _note_context(self, cu: Dict) -> None:
        """CLI 的 /context 输出 → 记下该模型的窗口（按 /context 里的模型 id；也按当时 transcript 的模型，
        因为第三方网关返回的 message.model 可能和 CLI 自己的模型名不一样）。"""
        limit = cu.get("limit")
        if not isinstance(limit, int) or limit < 1000:
            return
        k = norm_model(cu.get("model"))
        known = MODEL_CONTEXT.get(k or "")
        label = (_label(cu.get("display")) or (known[0] if known else None)
                 or (self.ctx_windows.get(k or "") or (0, None))[1] or _label(cu.get("model")))
        if k:
            self.ctx_windows[k] = (limit, label)
        self.ctx_last = (limit, label, norm_model(self.tr_model))

    def on_screen(self, st: Optional[Dict], t: float) -> None:
        """一帧屏幕的解析结果。t = 本地单调时钟（秒）。"""
        if not isinstance(st, dict):
            return
        cu = st.get("context_usage")
        if isinstance(cu, dict):
            self._note_context(cu)
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
    def _infer_limit(self, hints: Iterable) -> Tuple[int, str, Optional[str]]:
        """transcript 后备的上下文窗口 → (窗口, 来源 LIMIT_SOURCES 之一, 模型显示名或 None)。"""
        if self.tr_model is None and self.rs_limit is not None:
            return self.rs_limit
        k = norm_model(self.tr_model)
        known = MODEL_CONTEXT.get(k or "")
        label = known[0] if known else _label(self.tr_model)
        limit = src = None
        if k and k in self.ctx_windows:
            limit, lb = self.ctx_windows[k]
            src, label = "context_cmd", lb or label
        elif self.ctx_last is not None and self.ctx_last[2] == k and known is None:
            # transcript 的模型名不在表里（第三方网关的叫法）而 /context 用的是 CLI 自己的模型名：按"当时的模型"对上
            limit, lb, _ = self.ctx_last
            src, label = "context_cmd", lb or label
        if limit is None:
            texts = [self.status_text, self.tr_model] + list(hints or ())
            if any(tui_parse.has_1m_hint(x) for x in texts if isinstance(x, str)):
                limit, src = CTX_1M_LIMIT, "hint"
            if known and (limit is None or known[1] >= limit):
                limit, src = known[1], "model"
            if limit is None:
                # 不在表里的模型：CLI 2.1.331 默认 1M（见 MODEL_CONTEXT 上方注释）；连模型名都没有时才用 200k
                limit, src = (CTX_1M_LIMIT, "unknown_model") if k else (CTX_DEFAULT_LIMIT, "default")
        peak = self.peaks.get(k or "", 0)
        if peak > limit:
            limit, src = max(CTX_1M_LIMIT, limit), "observed"
        return limit, src, label

    def snapshot(self, hints: Iterable = ()) -> Optional[Dict]:
        """meta.stats（不含 updated_at；什么都不知道时返回 None）。hints = 推断上下文上限用的模型名/描述文本。"""
        used = limit = pct = None
        src = lsrc = model = None
        approx = False
        if self.st_pct is not None or self.st_used is not None:
            used, limit, pct, src = self.st_used, self.st_limit, self.st_pct, "status"
        elif self.tr_used is not None:
            used, src, approx = self.tr_used, "transcript", True
            limit, lsrc, model = self._infer_limit(hints)
            pct = min(100.0, round(used * 100.0 / limit, 1))   # 推断的分母不让百分比超过 100%
        live = self.live if self.in_turn else None
        out = {
            "context_used": used, "context_limit": limit,
            "context_pct": round(pct, 1) if pct is not None else None,
            "context_source": src, "context_approx": approx,
            "context_limit_source": lsrc, "context_model": model,
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
