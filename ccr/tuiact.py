"""终端里的两组操作（只由用户点击触发，SPEC §4.2「审批时附带说明」「子代理：停止」）：

1. **审批时附带说明**（CLI 2.1.332 权限框的 "Tab to amend"）：用户在权限卡片上点某个选项旁的「附说明…」、写好说明、确认后，
   按序：核对对话框指纹 → 方向键选到该项（每步核对）→ 核对该项有 "Tab to amend" → Tab（guard：对话框区域没变）→ 核对出现
   说明输入（"Yes, <占位文字>"）→ 若有上次留下的说明，Ctrl+U 逐行清掉（每步 guard）→ 粘贴说明（guard）→ 核对说明完整进去了、
   连续两帧一致 → Enter（guard）。任何一步不对就停下报错，不再按键（**绝不按 Esc**：真机实测 Esc 会取消整个对话框 = 拒绝）。
2. **停止子代理**（CLI 2.1.332 底部子代理列表：x = 停止选中的、输入框里 Ctrl+X Ctrl+K 两次 = 停止全部后台子代理）：
   只在输入框为空、没有对话框时进行；↓ 进入列表（有后台 shell 时先经过任务胶囊）→ 逐步移动并核对选中行就是目标
   （列表里的描述会被 CLI 换成进度摘要，按启动顺序对位 + 类型名 + 能认出的描述交叉核对，对不上就拒绝）→ x（guard
   agents_sel：选中行位置 / 行数 / 类型名都没变）→ 核对该行变成 stopped 或消失 → Esc 回到输入框（guard：焦点仍在底部）。

纯函数（解析 / 对位）在上半部分，流程函数接收 SessionManager（session.py 里只有薄薄的转发）。
"""
import hashlib
import re
import time
from typing import Dict, List, Optional

from . import subagents, tui_parse

MAX_AMEND = 2000
AMEND_CLEAR_STEPS = 12      # 清掉上次留下的说明：Ctrl+U 每次只删一个显示行（真机实测），最多按这么多次
MAX_ENTER_STEPS = 3         # 从输入框进入子代理列表最多按几次 ↓（输入框 → [任务胶囊] → 列表）
MAX_ROW_STEPS = 24
KILL_HINT_WAIT = 1.5        # 第一次 Ctrl+X Ctrl+K 后等确认提示的时间（CLI 的确认窗口约 3 秒）
STOP_WAIT = 6.0
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
KILL_HINT_RE = re.compile(r"^\s*Press ctrl\+x ctrl\+k again to stop background agents\s*$")   # = node_agent.GUARD_HINTS
NO_BG_RE = re.compile(r"^\s*No background agents running\s*$")
STOP_HINT_RE = re.compile(r"^enter to view · x to stop\b")      # = node_agent.G_STOP_HINT_RE


# ============================================================================ 纯函数：附带说明
def validate_amend_text(text) -> str:
    if not isinstance(text, str):
        raise ValueError("说明必须是字符串")
    t = text.strip()
    if not t:
        raise ValueError("说明不能为空")
    if len(t) > MAX_AMEND:
        raise ValueError("说明太长（最多 %d 字）" % MAX_AMEND)
    if _CTRL_RE.search(t):
        raise ValueError("说明里不能有换行或控制字符")
    if t in tui_parse.AMEND_PLACEHOLDERS.values():
        raise ValueError("说明不能和终端里的占位文字完全相同（无法核对是否输入成功）")
    return t


def amend_base(index: int, d: Optional[dict]) -> Optional[str]:
    opts = (d or {}).get("options") or []
    if index == 0 and opts:
        return "Yes"
    if opts and index == len(opts) - 1:
        return "No"
    return None


def _norm(x: Optional[str]) -> str:
    return re.sub(r"\s+", "", x or "")


def _base_dialog(d: dict, index: int, base: str) -> dict:
    """把 index 这一项的 "Yes, …" / "No, …"（正在输入的说明）还原成 "Yes" / "No"，用来和输入说明之前的指纹比对。"""
    opts = []
    for o in d.get("options") or []:
        o = dict(o)
        if o.get("index") == index and str(o.get("label") or "").startswith(base + ", "):
            o["label"] = base
        opts.append(o)
    return dict(d, options=opts)


def tail_guard(text: Optional[str]) -> Optional[dict]:
    lines = tui_parse.screen_lines(text)
    if tui_parse._find_input_box(lines) is not None:
        return None
    ds = tui_parse._find_dialog_start(lines)
    if ds is None:
        return None
    tail = lines[ds:]
    return {"tail": {"n": len(tail), "sha1": hashlib.sha1("\n".join(tail).encode("utf-8", "replace")).hexdigest()}}


# ============================================================================ 纯函数：子代理列表
def kill_hint_visible(text: Optional[str]) -> bool:
    lines = tui_parse.screen_lines(text)
    box = tui_parse._find_input_box(lines)
    return box is not None and any(KILL_HINT_RE.match(l) for l in lines[:box[0]])


def no_bg_visible(text: Optional[str]) -> bool:
    return any(NO_BG_RE.match(l) for l in tui_parse.screen_lines(text))


def panel_rows(st: Optional[dict]) -> Optional[List[dict]]:
    """屏幕底部的子代理列表（第 0 行必须是 main，否则认不出版式 → None）。"""
    rows = (st or {}).get("agents_panel") or []
    if not rows or rows[0].get("name") != "main":
        return None
    return rows


def _mentions(desc: str, name: str) -> bool:
    """列表行的文字里能认出这个描述：整段就是它，或 "waiting on 1 shell · <描述> · since 9:20" 这种 · 分隔的一段。"""
    if not name:
        return False
    d = desc.strip()
    return d == name or name in [x.strip() for x in d.split(" · ")]


def match_agent_row(rows: Optional[List[dict]], running: List[dict], target: str) -> Optional[int]:
    """目标子代理在屏幕列表里的行号（含 main 的下标）；认不准 → None（宁可拒绝，也不停错）。

    真机实测：列表顺序 = 启动顺序；行里的描述过几秒会被 CLI 换成 AI 生成的进度摘要（"Running first 20-second sleep step"），
    后台 shell 时是 "waiting on 1 shell · <描述> · since 9:20"。所以按位置对位：列表里没停止的行与我们认为在运行的子代理
    （按启动顺序）一一对应，数量必须相等、类型名必须一致；能认出描述的行必须正好对上同一个子代理。"""
    if rows is None:
        return None
    live = [(i, r) for i, r in enumerate(rows) if i > 0 and not r.get("state")]
    if len(live) != len(running) or not live:
        return None
    ids = [a.get("agent_id") for a in running]
    if target not in ids:
        return None
    descs = [str(a.get("description") or "") for a in running]
    for k, (_, r) in enumerate(live):
        a = running[k]
        if a.get("agent_type") and r.get("name") != a["agent_type"]:
            return None
        hits = [j for j, dsc in enumerate(descs) if _mentions(str(r.get("desc") or ""), dsc)]
        if len(hits) == 1 and descs.count(descs[hits[0]]) == 1 and hits[0] != k:
            return None
    return live[ids.index(target)][0]


def sorted_running(tracker, exited: bool = False) -> List[dict]:
    """正在运行的子代理，按启动顺序（主 transcript 里 Agent 调用的顺序；不知道时按开始时间）。"""
    order = {tid: i for i, tid in enumerate(tracker.calls)}
    run = [x for x in tracker.summaries(exited) if x.get("status") == "running"]
    if all(x.get("tool_use_id") in order for x in run):
        run.sort(key=lambda x: order[x["tool_use_id"]])
    else:
        run.sort(key=lambda x: (x.get("started_at") or float("inf"), x.get("agent_id") or ""))
    return run


def selected_row(rows: Optional[List[dict]]) -> Optional[int]:
    sel = [i for i, r in enumerate(rows or []) if r.get("selected")]
    return sel[0] if len(sel) == 1 else None


def footer_focused(st: Optional[dict]) -> bool:
    return bool((st or {}).get("agents_focus") or (st or {}).get("tasks_focus"))


# ============================================================================ 流程：附带说明
def amend(mgr, s, index, text, expect) -> dict:
    from . import session as S
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("index 必须是非负整数")
    text = validate_amend_text(text)
    expect = S.validate_expect(expect)
    changed = S.OpError("对话框已变化，请重新确认", code="dialog_changed")
    left_open = "（终端里的说明输入框可能还开着：在终端面板按 Tab 收起；Esc 会拒绝本次操作）"
    with mgr._op(s):
        st = mgr._fresh(s)
        d = st.get("dialog")
        if not d:
            raise S.OpError("当前没有对话框", code="no_dialog")
        if d.get("kind") != "permission":
            raise S.OpError("只有权限确认框可以附带说明", code="amend_unsupported")
        free = S._free_indexes(d)
        if expect is not None and not S.fingerprint_matches(d, expect, free):
            raise changed
        if d.get("amend"):
            raise S.OpError("终端里已经在输入说明了，请在终端面板里处理", code="amend_busy")
        if index not in (d.get("amendable") or []):
            raise S.OpError("这个选项不能附带说明", code="amend_unsupported")
        base = amend_base(index, d)
        fp0 = S.dialog_fingerprint(d)

        def same(x) -> bool:
            dd = (x or {}).get("dialog")
            return bool(dd) and S.fingerprint_matches(_base_dialog(dd, index, base), fp0, free)

        def editing(x) -> bool:
            a = ((x or {}).get("dialog") or {}).get("amend")
            return bool(a) and a.get("index") == index and a.get("base") == base and same(x)

        d = mgr._navigate_options(s, d, index, fp0, free)
        st = mgr._fresh(s)
        d = st.get("dialog")
        if not d or not S.fingerprint_matches(d, fp0, free) or tui_parse.selected_index(d) != index:
            raise changed
        if tui_parse.AMEND_HINT not in (d.get("hints") or ""):
            raise S.OpError("终端里这个选项没有「Tab to amend」提示，不能附带说明", code="amend_unsupported")
        mgr.notifier.user_acted(s.sid)
        mgr._user_engaged(s)

        def press(keys, why_stop):
            with s.lock:
                g = tail_guard(s.screen_text)
            if g is None:
                raise S.OpError(why_stop, code="dialog_changed")
            try:
                mgr._keys(s, keys, guard=g)
            except S.OpError as e:
                if e.api_code == "screen_changed":
                    raise S.OpError(why_stop, code="dialog_changed") from None
                raise

        # ① Tab：进入该项的说明输入
        press(["Tab"], "对话框已变化，没有按 Tab")
        st = mgr._wait_for(s, editing, mgr.step_timeout)
        if st is None:
            raise S.OpError("按了 Tab 但终端里没有出现说明输入，已停止" + left_open, code="amend_no_input")
        # ② CLI 会恢复上次 Tab 时留下的说明：Ctrl+U 每次删一个显示行，删到只剩占位文字
        for _ in range(AMEND_CLEAR_STEPS):
            a = st["dialog"]["amend"]
            if a.get("placeholder"):
                break
            prev = a.get("text")
            press(["C-u"], "对话框已变化，已停止" + left_open)
            st = mgr._wait_for(s, lambda x: editing(x) and x["dialog"]["amend"].get("text") != prev
                               or not editing(x), mgr.step_timeout)
            if st is None or not editing(st):
                raise S.OpError("清除终端里原有的说明时画面不对，已停止" + left_open, code="amend_unconfirmed")
        else:
            raise S.OpError("终端里原有的说明清不干净，已停止" + left_open, code="amend_unconfirmed")
        # ③ 粘贴说明（guard：对话框区域与刚才核对时一致）
        with s.lock:
            g = tail_guard(s.screen_text)
        if g is None:
            raise changed
        try:
            mgr._req(s.jobid, "paste", sid=s.sid, text=text, enter=False, guard=g, timeout=30)
        except S.OpError as e:
            if e.api_code == "screen_changed":
                raise S.OpError("对话框已变化，没有粘贴说明" + left_open, code="dialog_changed") from None
            raise
        with s.lock:
            s.last_activity = time.time()
        # ④ 说明完整进去了（忽略折行/空白），且连续两帧对话框区域一致
        want = _norm(text)
        deadline = time.monotonic() + mgr.step_timeout
        prev_g = None
        ok = False
        while time.monotonic() < deadline:
            x = mgr._fresh(s)
            a = (x.get("dialog") or {}).get("amend") if editing(x) else None
            if a and not a.get("placeholder") and _norm(a.get("text")) == want:
                with s.lock:
                    gg = tail_guard(s.screen_text)
                if gg is not None and gg == prev_g:
                    ok = True
                    break
                prev_g = gg
            else:
                prev_g = None
            time.sleep(mgr.poll_interval)
        if not ok:
            raise S.OpError("说明没有完整出现在终端的输入里，已停止（没有按 Enter）" + left_open, code="amend_unconfirmed")
        # ⑤ Enter（guard：对话框区域 = 刚核对过的那一帧）
        with s.lock:
            sig = S._dialog_sig((s.screen_state or {}).get("dialog"))
        press(["Enter"], "对话框在按 Enter 前发生了变化，没有按 Enter" + left_open)
        st2 = mgr._wait_after(s, lambda x: S._dialog_sig(x.get("dialog")) != sig
                              or tui_parse.selected_index(x.get("dialog")) != index)
    return {"ok": True, "state": st2 or s.screen_state}


# ============================================================================ 流程：停止子代理
def _require_input_ok(S, mgr, s, st) -> None:
    """没有对话框、输入框可见且是空的（焦点在底部列表上时也要求输入框为空：用户的文字一律不冒险）。"""
    if mgr._blocking_dialog(s, st) or s.state == "trust":
        raise S.OpError("请先处理当前对话框", code="dialog_open")
    inp = st.get("input") or {}
    if not inp.get("visible"):
        raise S.OpError("终端里看不到输入框，暂时不能操作子代理列表", code="no_input")
    if (inp.get("text") or "").strip() or inp.get("placeholder") or inp.get("images"):
        raise S.OpError("终端输入框里有文字（或排队的消息），为了不误操作，请先发送或清空后再停止子代理",
                        code="input_not_empty")
    if st.get("popup"):
        raise S.OpError("终端里开着补全列表，请先关闭", code="input_not_empty")


def _running(s) -> List[dict]:
    with s.lock:
        return sorted_running(s.subs, s.exited)


def _leave_footer(S, mgr, s) -> Optional[dict]:
    """焦点在底部（子代理列表 / 任务胶囊）时按 Esc 回到输入框（guard footer_focus：底部的 Esc = 取消选择，不会中断回复）。"""
    st = mgr._fresh(s)
    if not footer_focused(st):
        return st
    mgr._keys(s, ["Escape"], guard={"footer_focus": True})
    return mgr._wait_for(s, lambda x: not footer_focused(x) and (x.get("input") or {}).get("visible"), mgr.step_timeout)


def _enter_list(S, mgr, s, st) -> dict:
    for _ in range(MAX_ENTER_STEPS):
        if st.get("agents_focus"):
            return st
        before = (st.get("tasks_focus"), st.get("mode_line"), st.get("agents_hint"))
        if st.get("tasks_focus"):
            mgr._keys(s, ["Down"], guard={"footer_focus": True})
        else:
            mgr._keys(s, ["Down"], guard={"input_box": True, "input_empty": True})
        st2 = mgr._wait_for(s, lambda x: (x.get("tasks_focus"), x.get("mode_line"), x.get("agents_hint")) != before
                            or x.get("dialog"), mgr.step_timeout)
        if st2 is None or st2.get("dialog") or not footer_focused(st2):
            raise S.OpError("按 ↓ 之后焦点没有进入子代理列表，已停止", code="agents_nav")
        st = st2
    if st.get("agents_focus"):
        return st
    raise S.OpError("按 ↓ 之后焦点没有进入子代理列表，已停止", code="agents_nav")


def stop_agent(mgr, s, agent_id) -> dict:
    from . import session as S
    if not isinstance(agent_id, str) or not subagents.AGENT_ID_RE.match(agent_id):
        raise ValueError("agent_id 不合法")
    ambiguous = S.OpError("终端的子代理列表里认不准是哪一行（CLI 显示的是进度摘要，或列表和记录对不上），为了不停错，"
                          "请在终端面板里操作", code="agent_ambiguous")
    with mgr._op(s):
        mgr._require_live(s)
        st = mgr._fresh(s)
        _require_input_ok(S, mgr, s, st)
        running = _running(s)
        if agent_id not in [a["agent_id"] for a in running]:
            raise S.OpError("这个子代理已经不在运行", code="agent_not_running")
        rows = panel_rows(st)
        if rows is None:
            raise S.OpError("终端里看不到子代理列表", code="no_agents_panel")
        if match_agent_row(rows, running, agent_id) is None:
            raise ambiguous
        mgr.notifier.user_acted(s.sid)
        entered = False
        warning = None
        try:
            st = _enter_list(S, mgr, s, st)
            entered = True
            for _ in range(MAX_ROW_STEPS):
                rows = panel_rows(st)
                pos = match_agent_row(rows, _running(s), agent_id)
                if pos is None:
                    raise ambiguous
                cur = selected_row(rows)
                if cur is None:
                    raise S.OpError("认不出子代理列表里选中的是哪一行，已停止", code="agents_nav")
                if cur == pos:
                    break
                mgr._keys(s, ["Down" if pos > cur else "Up"], guard={"footer_focus": True})
                st2 = mgr._wait_for(s, lambda x: selected_row(panel_rows(x)) != cur or not x.get("agents_focus"),
                                    mgr.step_timeout)
                if st2 is None or not st2.get("agents_focus"):
                    raise S.OpError("子代理列表没有响应方向键，已停止", code="agents_nav")
                st = st2
            else:
                raise S.OpError("移动到目标子代理的步数超过上限", code="agents_nav")
            if not STOP_HINT_RE.match(st.get("agents_hint") or ""):
                raise S.OpError("终端提示行不是「x to stop」，没有按 x", code="agents_nav")
            row = rows[pos]
            try:
                mgr._keys(s, ["x"], guard={"agents_sel": {"pos": pos, "count": len(rows), "name": row["name"]}})
            except S.OpError as e:
                if e.api_code == "screen_changed":
                    raise S.OpError("子代理列表在按 x 前发生了变化，没有按 x；请重试", code="screen_changed") from None
                raise

            def stopped(x) -> bool:
                rr = panel_rows(x)
                if rr is None or len(rr) < len(rows):
                    return True
                return pos < len(rr) and rr[pos].get("state") == "stopped"
            if mgr._wait_for(s, stopped, STOP_WAIT) is None:
                warning = "按了 x，但终端里没有看到该子代理变成 stopped，请看一眼终端面板"
        finally:
            # 进了底部列表就一定尝试回到输入框（guard footer_focus：底部的 Esc = 取消选择，不会中断回复、不会取消对话框）
            if entered or footer_focused(s.screen_state):
                try:
                    back = _leave_footer(S, mgr, s)
                except S.OpError:
                    back = None
                if back is None and warning is None:
                    warning = "没能回到终端输入框（焦点还在子代理列表上，请在终端面板按 Esc）"
    out = {"ok": True, "agent_id": agent_id}
    if warning:
        out["warning"] = warning
    return out


def stop_all_agents(mgr, s) -> dict:
    from . import session as S
    with mgr._op(s):
        mgr._require_live(s)
        st = mgr._fresh(s)
        _require_input_ok(S, mgr, s, st)
        if footer_focused(st):
            st = _leave_footer(S, mgr, s)
            if st is None:
                raise S.OpError("没能回到终端输入框，已停止", code="agents_nav")
            _require_input_ok(S, mgr, s, st)
        rows = panel_rows(st) or []
        live = [r for r in rows[1:] if not r.get("state")]
        if not live and not _running(s):
            raise S.OpError("现在没有运行中的子代理", code="no_running_agents")
        mgr.notifier.user_acted(s.sid)
        guard = {"input_box": True, "input_empty": True}
        hint = None
        for _ in range(2):   # 第一次 Ctrl+X Ctrl+K → 确认提示；真机偶尔没反应（画面不变）时再按一次
            mgr._keys(s, ["C-x", "C-k"], guard=guard)
            hint = mgr._wait_for(s, lambda x: kill_hint_visible(s.screen_text) or no_bg_visible(s.screen_text),
                                 KILL_HINT_WAIT)
            if hint is not None:
                break
        if hint is None:
            raise S.OpError("按了 Ctrl+X Ctrl+K 但终端没有出现确认提示，已停止", code="kill_unconfirmed")
        with s.lock:
            text = s.screen_text
        if not kill_hint_visible(text):
            raise S.OpError("终端说现在没有运行中的后台子代理", code="no_running_agents")
        try:
            mgr._keys(s, ["C-x", "C-k"], guard=dict(guard, hint="kill_agents"))
        except S.OpError as e:
            if e.api_code == "screen_changed":
                raise S.OpError("确认提示已经消失（超过 3 秒），没有停止；请重试", code="screen_changed") from None
            raise

        def done(x) -> bool:
            rr = panel_rows(x) or []
            return not [r for r in rr[1:] if not r.get("state")]
        st2 = mgr._wait_for(s, done, STOP_WAIT)
    out = {"ok": True, "stopped": len(live)}
    if st2 is None:
        out["warning"] = "已按两次 Ctrl+X Ctrl+K，但终端里还有子代理没显示 stopped，请看一眼终端面板"
    return out
