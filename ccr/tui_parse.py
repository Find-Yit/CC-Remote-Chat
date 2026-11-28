"""交互式 Claude Code 屏幕解析（docs/SPEC_V2_TUI.md §4.1）。

parse_screen(text) 是纯函数：输入 `tmux capture-pane -p -J` 的纯文本，输出结构化状态。
不做任何 I/O、无副作用；解析不了的对话框统一 kind="generic"（保留 lines/options/hints）。

屏幕自上而下：会话内容 → [slash 补全弹层] → 输入框（两条 ──── 之间、以 ❯ 开头）→ 状态栏 → 模式行；
对话框出现时，输入框及其下方被对话框（以 ──── 或 ▔▔▔▔ 开头、直到屏幕底部）取代。
CLI 2.1.331 起 fullscreen 渲染器下右侧可能有 diff 面板（左栏 + "│" + 面板）：解析前先裁掉（见 _split_side_panel）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

_RULE_RE = re.compile(r"^─{20,}(?: [^─\n]{1,200} ─{1,8})?\s*$")   # 输入框/对话框分隔线（第 0 列起）；/rename 后输入框上边线右侧嵌着会话名
_TOP_RULE_RE = re.compile(r"^▔{20,}\s*$")       # /model、/effort 面板顶边
_DASH_RE = re.compile(r"^\s*╌{10,}\s*$")        # 权限框预览上下边
_OPT_RE = re.compile(r"^(\s*)(❯\s*)?(\d{1,2})\.\s+(\S.*)$")
_SPINNER_RE = re.compile(r"^([·✢✳✶✻✽*✺✹✷✸⁕∗])\s+(\S+….*)$")
_DONE_RE = re.compile(r"^✻\s+(\S+ for \d+(?:\.\d+)?[smh]\b.*)$")   # "Baked for 9s · done …"；不含 "Waiting for 1 background agent…"
_TIP_RE = re.compile(r"^\s+⎿\s+Tip:")
_ECHO_RE = re.compile(r"^❯(?:[ \u00a0](.*))?$")
_INTERRUPTED_RE = re.compile(r"⎿\s+Interrupted\b")
_HINT_ABOVE_INPUT_RE = re.compile(r"^\s{10,}(ctrl|alt|shift|meta|esc|tab|option|cmd)\+?\S*\s.*$", re.I)
_RIGHT_HINT_RE = re.compile(r"^\s{40,}\S")   # 输入框正上方右对齐的提示（"○ low · /effort"、"tmux focus-events off · …"）
# 模式行下方的子代理列表："● main" / "◯ general-purpose  前台计算 1+1      2s · ↑ 14.3k tokens"；
# CLI 2.1.332（macOS）圆点是 "⏺"，焦点在列表上时选中行前有 "❯ "（"❯ ◯ general-purpose  …"）
_AGENT_ROW_RE = re.compile(r"^(?:\s*|(❯)\s+)([●◯○◉◐⏺])\s+(\S.*)$")
# 子代理行最后一段：用时（"2s · ↑ 14.3k tokens"），已停止的前面多一个状态词（"stopped · 11s · ↓ 11.3k tokens"）
_AGENT_INFO_RE = re.compile(r"^(?:([a-z]+)\s+·\s+)?\d+(?:\.\d+)?[smh]\b")
# 焦点在子代理列表上时，模式行被换成列表的操作提示（CLI 2.1.332 实测）："↑/↓ to select"（光标在 main 上）、
# "enter to view · x to stop · ctrl+x ctrl+k to stop all agents"、"enter to view · x to clear"（已停止的行）
_AGENTS_FOCUS_RE = re.compile(r"^(?:↑/↓ to select|enter to view)\b")
_POPUP_RE = re.compile(r"^\s{2,4}(❯\s)?\s*(/[\w:.\-]+)(?:\s{2,}(.*))?$")
_POPUP_CONT_RE = re.compile(r"^\s{10,}\S")   # 补全项说明的折行（与 node_agent.G_HINT_RE 同一写法）
_MORE_RE = re.compile(r"^\s*([↑↓])\s*(\d+)\s+more\s*$")
# /model 面板的 effort 行（实测）："  ○ Low effort ←/→ to adjust"、"  ◉ xHigh effort …"、"  ◈ Max effort …"；
# 图标会变且可省略，档位名整体识别（xHigh / Extra high → xhigh），识别不了 level=None
_MODEL_EFFORT_RE = re.compile(
    r"^\s{0,4}(?:(\S)\s+)?(\S.*?)\s+effort(\s*\(default\))?(?:\s+←/→.*)?\s*$", re.I)
_SET_MODEL_RE = re.compile(r"⎿\s+((?:Set model to|Kept model as)\b.*)$")
# CLI 2.1.332：effort 被环境变量锁定时 /effort 的结果是 "CLAUDE_CODE_EFFORT_LEVEL=max overrides this session — …"
_SET_EFFORT_RE = re.compile(r"⎿\s+((?:Set effort|Kept effort|Effort level)\b.*|[A-Z][A-Z0-9_]*=\S+ overrides this session\b.*)$",
                            re.I)
_RESULT_LINE_RE = re.compile(r"^\s*⎿\s+(\S.*)$")
_RESULT_WORD_RE = re.compile(r"effort|model", re.I)
_BANNER_RE = re.compile(r"^\S*\s+(.+?) with (\S+) effort\b")
_STATUS_MODEL_RE = re.compile(r"\[([^\]·]+?)\s*·.*?effort:(\w+)\]")
# 输入框正上方的 effort 提示（实测右对齐 "○ low · /effort"；也兼容只有两个空格缩进的写法）
_EFFORT_HINT_RE = re.compile(r"^\s*[○◐●◉◈]\s+(low|medium|high|xhigh|max)\s+·\s+/effort\s*$", re.I)

# CLI 的快捷键提示（v2.1.330 实测，见 fixtures/tui_screens_keys.ndjson）。整行匹配，避免把回复正文里的同样文字当成提示；
# 与 node_agent.GUARD_HINTS 必须一致（agent 发键前据此复核）。
#   正在运行的 Bash 命令下方："(ctrl+b ctrl+b (twice) to run in background)"（也兼容 "ctrl+b to run in background"）
#   排队的消息下方："⎿  Queued · ctrl+x ctrl+s to send now"
HINT_BACKGROUND_RE = re.compile(
    r"^\s*\(?\s*ctrl\+b(?:\s+ctrl\+b)?(?:\s+\(twice\))?\s+to run in background\s*\)?\s*$", re.I)
HINT_SEND_NOW_RE = re.compile(r"^\s*(?:⎿\s+)?(?:Queued\s+·\s+)?ctrl\+x\s+ctrl\+s\s+to send now\s*$", re.I)
_QUEUED_RE = re.compile(r"^\s+⎿\s+Queued\b")
# 排队时输入框里的占位文字（不是用户输入的内容）
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
# 输入框里的图片占位（CLI 2.1.332 真机实测，fixtures/image_paste/）：粘贴一个图片路径（= 终端里拖入文件）或按 Ctrl+V
# 读剪贴板图片后，输入框显示 "[Image #N]"（N 在整个 claude 进程里递增，不随消息重置）；连续两张之间 CLI 自己补一个空格；
# 提交后回显 "❯ [Image #1] 文字" 下面每张一行 "  ⎿  [Image #1]"。
IMAGE_TOKEN_RE = re.compile(r"\[Image #(\d{1,6})\]")
_ECHO_IMAGE_RE = re.compile(r"^\s+⎿\s+\[Image #\d{1,6}\]\s*$")


def split_input_images(text: Optional[str]) -> Tuple[List[int], str]:
    """输入框文字 → ([图片编号…], 去掉所有 "[Image #N]" 后的其余文字（首尾空白去掉）)。"""
    t = text or ""
    ids = [int(m.group(1)) for m in IMAGE_TOKEN_RE.finditer(t)]
    rest = IMAGE_TOKEN_RE.sub(" ", t).strip() if ids else t.strip()
    return ids, rest


# 输入框里的光标（删除单张图片用，SPEC §4.2「图片」；CLI 2.1.332 真机实测）：CLI 把终端的硬件光标停在输入框的插入点上
# （光标是隐藏的，但位置准确：tmux 的 #{cursor_x} #{cursor_y} 读得到，中文两列宽、多行输入都对得上）。
# ← / → 在 "[Image #N]" 上整块跳过、其余字符一步一个，行首按 ← 到上一行行尾；退格整块删掉光标前的 "[Image #N]"，
# 两边的空格都留着；编号不重排。光标的屏幕坐标要配 **不带 -J** 的 capture-pane（行号 = 终端行号）。
# **与 node_agent.caret_view 逐字一致**（agent 按同一规则就地复核 guard.cursor_image / cursor_space）。
INPUT_PREFIX_CELLS = 2      # 输入框每行开头的 "❯ " / "  "
# ← / → 的一步：图片占位（实测整块）与 "[Pasted text #N …]" 都按 1 步算（少算只会少走、再读屏补走，绝不多走）
_CARET_TOKEN_RE = re.compile(r"\[Image #\d{1,6}\]|\[Pasted text #\d{1,6}[^\]\n]{0,40}\]|.", re.S)


def _cells_to_index(s: str, cells: int) -> Optional[int]:
    """显示列 → 字符下标（落在宽字符中间 → None；超出文字 → None）。"""
    w = 0
    for i, ch in enumerate(s):
        if w == cells:
            return i
        w += _cell_width(ch)
        if w > cells:
            return None
    return len(s) if w == cells else None


def _input_body(row: str) -> str:
    """输入框一行去掉开头 2 列的 "❯ " / "  "。"""
    i = _cells_to_index(row, INPUT_PREFIX_CELLS)
    return row[i:] if i is not None else ""


def input_caret(lines: List[str], cx, cy) -> Optional[Dict]:
    """屏幕行（screen_lines，行号 = 终端行号）+ 终端光标 (cx, cy) → 光标在 claude 输入框里的位置：
    {"box": (上横线, 下横线), "row": 输入框内第几行（0 起）, "rows": 输入框行数, "col": 本行文字里的第几列,
     "before": 本行光标前的输入文字, "after": 本行光标后的输入文字}；光标不在输入框的文字区里 → None。
    行尾 rstrip 掉的空格：光标在文字之后几列 → before 末尾补同样多的空格（那一定是真的空格：光标只停在文字位置上）。"""
    if isinstance(cx, bool) or isinstance(cy, bool) or not isinstance(cx, int) or not isinstance(cy, int):
        return None
    box = _find_input_box(lines)
    if box is None:
        return None
    lo, hi = box
    if not lo < cy < hi or cx < INPUT_PREFIX_CELLS:
        return None
    body = _input_body(lines[cy])
    col = cx - INPUT_PREFIX_CELLS
    w = sum(_cell_width(ch) for ch in body)
    if col >= w:
        before, after = body + " " * (col - w), ""
    else:
        i = _cells_to_index(body, col)
        if i is None:
            return None
        before, after = body[:i], body[i:]
    return {"box": box, "row": cy - lo - 1, "rows": hi - lo - 1, "col": col, "before": before, "after": after}


def input_image_cells(lines: List[str], n: int) -> List[Tuple[int, int]]:
    """输入框里每个 "[Image #n]" 后面那一格的终端坐标 (cx, cy)（光标停在这里按退格 = 删掉这一张）。"""
    box = _find_input_box(lines)
    if box is None:
        return []
    out = []
    for cy in range(box[0] + 1, box[1]):
        body = _input_body(lines[cy])
        for m in IMAGE_TOKEN_RE.finditer(body):
            if int(m.group(1)) == n:
                out.append((INPUT_PREFIX_CELLS + sum(_cell_width(ch) for ch in body[:m.end()]), cy))
    return out


def input_end_cell(lines: List[str]) -> Optional[Tuple[int, int]]:
    """输入框最后一行文字之后那一格 (cx, cy)（rstrip 过：真有行尾空格时光标会比这里再靠右，不必再往右走）。"""
    box = _find_input_box(lines)
    if box is None:
        return None
    cy = box[1] - 1
    return INPUT_PREFIX_CELLS + sum(_cell_width(ch) for ch in _input_body(lines[cy])), cy


def _caret_steps(body: str, a: int, b: int) -> int:
    """本行显示列 [a, b) 之间按 ← / → 要走几步（图片占位等整块算 1 步；按开头所在的列算，宁少勿多）。"""
    if b <= a:
        return 0
    n, w = 0, 0
    for m in _CARET_TOKEN_RE.finditer(body):
        tw = sum(_cell_width(ch) for ch in m.group(0))
        if w >= b:
            break
        if w >= a and w + tw <= b:
            n += 1
        w += tw
    if w < b:   # 光标在行尾补的空格里（真有行尾空格）：每列一步
        n += b - max(w, a)
    return n


def caret_moves(lines: List[str], cur: Tuple[int, int], tgt: Tuple[int, int], limit: int = 40) -> List[str]:
    """从光标 cur 走到 tgt（都是终端坐标 (cx, cy)、都在输入框里）的一批 ← / →（最多 limit 个）。
    同一行：按步数精确走；不同行：先走到本行行首 / 行尾再多一步跨行，下一轮读屏再接着走。
    宁少勿多：→ 绝不会越过 tgt（输入框末尾按 → 可能另有含义，不能多按）；走不动（[] ）由调用方判定失败。"""
    (cx, cy), (tx, ty) = cur, tgt
    if (cx, cy) == (tx, ty):
        return []
    body = _input_body(lines[cy]) if 0 <= cy < len(lines) else ""
    a, t = cx - INPUT_PREFIX_CELLS, tx - INPUT_PREFIX_CELLS
    if cy == ty:
        n = _caret_steps(body, t, a) if t < a else _caret_steps(body, a, t)
        key = "Left" if t < a else "Right"
    elif ty < cy:
        n, key = _caret_steps(body, 0, a) + 1, "Left"
    else:
        w = sum(_cell_width(ch) for ch in body)
        n, key = _caret_steps(body, a, max(a, w)) + 1, "Right"
    return [key] * max(0, min(n, limit))
# 底栏 "⏵⏵ auto mode on · 1 shell · esc to interrupt · ↓ to manage" / 结束行 "… · 1 shell still running"
_SHELLS_RE = re.compile(r"(?<![\w.])(\d{1,3})\s+shells?\b")

_MODE_PATTERNS = (
    ("bypass permissions", "bypassPermissions"),
    ("accept edits", "acceptEdits"),
    ("auto mode", "auto"),
    ("plan mode", "plan"),
    ("don't ask", "dontAsk"),
    ("dont ask", "dontAsk"),
    ("manual mode", "manual"),
)


# --------------------------------------------------------------------------- 小工具
def _dwidth(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


# --------------------------------------------------------------------------- 右侧 diff 面板（CLI 2.1.331 起）
# fullscreen 渲染器（CLAUDE_CODE_NO_FLICKER=1）下、git 仓库里本会话第一次改文件后，宽度 ≥144（用户 /diff 打开过则 ≥110）
# 时屏幕右侧会自动出现 diff 面板：左栏聊天区 + "│" + 面板，面板从屏幕第 0 行一直到整行宽的输入框上横线之前
# （对话框画在左栏时一直到屏幕底部）。解析前先把这些行裁到 "│" 之前，其余规则都按裁后的行工作。
# **与 node_agent.strip_side_panel 逐字一致**（agent 发键前的 guard 按同样裁过的行复核，tail sha1 两端才对得上）。
PANEL_MIN_COL = 40      # 面板竖线所在显示列至少这么靠右（左栏太窄的不是面板）
PANEL_MAX_TOP = 2       # 面板竖线必须从屏幕前 3 行内开始（实测总是第 0 行）
PANEL_MIN_ROWS = 3      # 至少连续这么多行
PANEL_SLOP = 2          # 个别行显示宽度算不准（emoji 变体选择符等）时允许的列偏差
PANEL_HEAD_ROWS = 4     # 面板头（✕ / "N files changed" / "/diff to close" / "source: … ▾"）只在前几行里找
_PANEL_FILES_RE = re.compile(r"^(\d+) files? changed\b(?:\s+\+(\d+))?(?:\s+-(\d+))?")
_PANEL_HEAD_RE = re.compile(r"(?:^|\s)/diff to close(?:\s|$)|(?:^|\s)source: \S.*▾\s*$")
_PANEL_CLOSE_RE = re.compile(r"^\s*✕\s*$")


def _cell_width(ch: str) -> int:
    """终端显示宽度（裁面板专用：变体选择符/零宽连接符等格式字符算 0）。"""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _bar_cols(line: str) -> List[Tuple[int, int]]:
    """行里每个 "│" 的 (显示列, 字符下标)。"""
    out = []
    col = 0
    for i, ch in enumerate(line):
        if ch == "│":
            out.append((col, i))
        col += _cell_width(ch)
    return out


def _split_side_panel(lines: List[str]) -> Tuple[List[str], Optional[Dict]]:
    """识别右侧 diff 面板并裁掉：返回 (裁后的行, {"files","add","del"} | None)。没有面板时原样返回同一个列表。

    判定（全部满足才裁）：屏幕前 PANEL_MAX_TOP+1 行内某行在显示列 c（≥ PANEL_MIN_COL）有 "│"；自该行起连续
    ≥ PANEL_MIN_ROWS 行在 c（±PANEL_SLOP，优先正好在 c）都有 "│"；并且面板前 PANEL_HEAD_ROWS 行里有面板头——
    "│" 紧跟 "N file(s) changed"，或含 "/diff to close"、"source: … ▾"，或首行面板部分只有右对齐的 "✕"；
    且这几行竖线右侧不再有 "│"。正文里的表格/代码框竖线不会同时满足"从屏幕顶端开始 + 列 ≥40 + 面板头 +
    右侧没有竖线"（表格单元格有空格内边距且右侧还有边框）。"""
    top = min(PANEL_MAX_TOP + 1, len(lines))
    if not any("│" in lines[i] for i in range(top)):
        return lines, None
    cache: Dict[int, List[Tuple[int, int]]] = {}

    def bars(r: int) -> List[Tuple[int, int]]:
        if r not in cache:
            cache[r] = _bar_cols(lines[r]) if "│" in lines[r] else []
        return cache[r]

    for s in range(top):
        for c, _ in bars(s):
            if c < PANEL_MIN_COL:
                continue
            cuts: List[int] = []
            r = s
            while r < len(lines):
                bc = bars(r)
                best = min(bc, key=lambda x: (abs(x[0] - c), -x[0])) if bc else None
                if best is None or abs(best[0] - c) > PANEL_SLOP:
                    break
                cuts.append(best[1])
                r += 1
            if len(cuts) < PANEL_MIN_ROWS:
                continue
            heads = [lines[s + k][cuts[k] + 1:] for k in range(min(PANEL_HEAD_ROWS, len(cuts)))]
            if any("│" in h for h in heads):
                continue   # 面板头几行（✕ / 标题 / 文件名）里不会再有竖线；表格行右侧还有 "│"
            info = None
            for h in heads:
                m = _PANEL_FILES_RE.match(h)
                if m:
                    info = {"files": int(m.group(1)), "add": int(m.group(2) or 0), "del": int(m.group(3) or 0)}
                    break
            if info is None:
                if not (_PANEL_CLOSE_RE.match(heads[0]) or any(_PANEL_HEAD_RE.search(h) for h in heads)):
                    continue
                info = {"files": None, "add": None, "del": None}
            out = list(lines[:s])
            out += [lines[s + k][:cut].rstrip() for k, cut in enumerate(cuts)]
            out += lines[s + len(cuts):]
            return out, info
    return lines, None


def strip_side_panel(lines: List[str]) -> List[str]:
    """裁掉右侧 diff 面板（见 _split_side_panel）；没有面板时原样返回。"""
    return _split_side_panel(lines)[0]


def _pop_blank_tail(lines: List[str]) -> List[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _screen_lines_panel(text: Optional[str]) -> Tuple[List[str], Optional[Dict]]:
    lines = [l.rstrip() for l in (text or "").replace("\r", "").replace("\u00a0", " ").split("\n")]
    _pop_blank_tail(lines)
    out, panel = _split_side_panel(lines)
    if panel is not None:
        out = _pop_blank_tail(out)
    return out, panel


def screen_lines(text: Optional[str]) -> List[str]:
    """屏幕规范化：rstrip、NBSP→空格、去掉末尾空行、裁掉右侧 diff 面板。与 node_agent.screen_lines 逐字一致。"""
    return _screen_lines_panel(text)[0]


# 宽度不够时（<110 列）CLI 在输入框上方画的提示框（不是回复内容，不进实时草稿）：
#   ╭────…✕─╮ / │ Resize your terminal to at least 110 columns to show the diff panel │ / ╰────…╯
_RESIZE_TOP_RE = re.compile(r"^╭─+(?:✕─*)?╮$")
_RESIZE_MID_RE = re.compile(r"^│ Resize your terminal to at least \d+ columns to show the diff panel\s*│$")
_RESIZE_BOT_RE = re.compile(r"^╰─+╯$")


def _blank_resize_notice(lines: List[str]) -> List[str]:
    for i in range(1, len(lines) - 1):
        if (_RESIZE_MID_RE.match(lines[i]) and _RESIZE_TOP_RE.match(lines[i - 1])
                and _RESIZE_BOT_RE.match(lines[i + 1])):
            lines = list(lines)
            lines[i - 1] = lines[i] = lines[i + 1] = ""
    return lines


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _strip_blank_edges(lines: List[str]) -> List[str]:
    a, b = 0, len(lines)
    while a < b and not lines[a].strip():
        a += 1
    while b > a and not lines[b - 1].strip():
        b -= 1
    return lines[a:b]


def _is_rule(line: str) -> bool:
    return bool(_RULE_RE.match(line))


def _is_top_rule(line: str) -> bool:
    return bool(_TOP_RULE_RE.match(line))


def _mode_of(line: str) -> Optional[str]:
    low = line.lower()
    for pat, mode in _MODE_PATTERNS:
        if pat in low:
            return mode
    return None


def _level_word(word: str) -> Optional[str]:
    w = re.sub(r"[^a-z]", "", word.lower())
    if w in EFFORT_LEVELS:
        return w
    if w in ("extrahigh", "xhi"):
        return "xhigh"
    if w in ("maximum",):
        return "max"
    if w in ("med",):
        return "medium"
    return None


# --------------------------------------------------------------------------- 区域划分
_FOOTER_AGENT_ROW_RE = re.compile(r"^\s*(?:❯\s+)?[●◯○◉◐⏺]\s+\S")   # 模式行下方的子代理列表行（与 node_agent.G_AGENT_ROW_RE 一致）
_DIALOG_MARK_RE = re.compile(r"Esc to cancel|Enter to select|Enter to confirm|Do you want to")
MAX_FOOTER_LINES = 4   # 输入框下横线之后最多这么多行状态栏/模式行（不含子代理列表行）


def _footer_ok(after: List[str]) -> bool:
    """输入框位置约束：下横线之后只能是状态栏/模式行（≤4 行，另可有子代理列表行），不能有对话框内容。"""
    ne = [l for l in after if l.strip()]
    for l in ne:
        if _is_rule(l) or _is_top_rule(l) or _OPT_RE.match(l) or _DIALOG_MARK_RE.search(l):
            return False
    return len([l for l in ne if not _FOOTER_AGENT_ROW_RE.match(l)]) <= MAX_FOOTER_LINES


def _find_input_box(lines: List[str]) -> Optional[Tuple[int, int]]:
    """返回 (上横线行号, 下横线行号)：最后一个"横线 + ❯ 开头 + … + 横线"的组合，且必须位于屏幕底部
    （下横线之后只有状态栏/模式行，见 _footer_ok）；否则视为没有输入框（多半是对话框）。"""
    rules = [i for i, l in enumerate(lines) if _is_rule(l)]
    for k in range(len(rules) - 1, 0, -1):
        lo, hi = rules[k - 1], rules[k]
        if hi - lo >= 2 and lines[lo + 1].startswith("❯"):
            return (lo, hi) if _footer_ok(lines[hi + 1:]) else None
    return None


def _find_dialog_start(lines: List[str]) -> Optional[int]:
    """无输入框时：对话框起点 = ▔ 顶边（最后一个），或自下而上第一个"后面不是编号选项行"的 ──── 横线。"""
    tops = [i for i, l in enumerate(lines) if _is_top_rule(l)]
    if tops:
        return tops[-1]
    rules = [i for i, l in enumerate(lines) if _is_rule(l)]
    for i in reversed(rules):
        nxt = next((l for l in lines[i + 1:] if l.strip()), None)
        if nxt is not None and _OPT_RE.match(nxt):
            continue  # 对话框内部的分隔线（如 AskUserQuestion 的 "4. Chat about this" 之上）
        return i
    return None


# --------------------------------------------------------------------------- 对话框
def _parse_numbered_options(lines: List[str], cont_into: str) -> List[Dict]:
    """编号选项；缩进续行并入上一项（cont_into="desc" 或 "label"）。空行/分隔线终止续行。"""
    opts: List[Dict] = []
    cur = None
    cur_col = 0
    for raw in lines:
        if not raw.strip() or _is_rule(raw) or _DASH_RE.match(raw):
            cur = None
            continue
        m = _OPT_RE.match(raw)
        if m:
            label = m.group(4).rstrip()
            cur = {"index": len(opts), "n": int(m.group(3)), "label": label, "desc": "",
                   "selected": bool(m.group(2))}
            opts.append(cur)
            cur_col = len(raw) - len(raw[m.start(4):])
            continue
        if cur is not None and _indent(raw) >= max(2, cur_col - 1):
            text = raw.strip()
            if cont_into == "label":
                cur["label"] = (cur["label"] + " " + text).strip()
            else:
                cur["desc"] = (cur["desc"] + " " + text).strip() if cur["desc"] else text
            continue
        cur = None
    return opts


def _parse_plain_options(lines: List[str]) -> List[Dict]:
    """无编号选项（如信任框）：包含 ❯ 行、文字起始列相同的连续非空行块。"""
    sel = None
    for i, l in enumerate(lines):
        if re.match(r"^\s*❯\s+\S", l):
            sel = i
            break
    if sel is None:
        return []

    def text_col(l: str) -> int:
        m = re.match(r"^(\s*(?:❯\s+)?)", l)
        return len(m.group(1)) if m else 0

    col = text_col(lines[sel])
    a = sel
    while a - 1 >= 0 and lines[a - 1].strip() and text_col(lines[a - 1]) == col:
        a -= 1
    b = sel
    while b + 1 < len(lines) and lines[b + 1].strip() and text_col(lines[b + 1]) == col:
        b += 1
    out = []
    for i in range(a, b + 1):
        l = lines[i]
        out.append({"index": len(out), "n": None, "label": l[col:].strip(), "desc": "",
                    "selected": "❯" in l[:col]})
    return out


def _hints_of(lines: List[str]) -> Tuple[str, int]:
    for i in range(len(lines) - 1, -1, -1):
        t = lines[i].strip()
        if not t:
            continue
        if re.search(r"\b(Esc|Enter|Tab)\b|↑|←", t) and (" · " in t or " to " in t):
            return t, i
        break
    return "", -1


def _parse_model(body: List[str], d: Dict) -> None:
    d["kind"] = "model"
    d["title"] = "Select model"
    rows: List[Dict] = []
    more_above = more_below = 0
    effort = None
    cur = None
    desc_col = 0
    for raw in body:
        if not raw.strip():
            cur = None
            continue
        mm = _MORE_RE.match(raw)
        if mm:
            cur = None
            if mm.group(1) == "↑":
                more_above = int(mm.group(2))
            else:
                more_below = int(mm.group(2))
            continue
        m = _OPT_RE.match(raw)
        if m:
            rest = m.group(4)
            parts = re.split(r"\s{2,}", rest.strip(), maxsplit=1)
            name = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            current = "✔" in name
            name = name.replace("✔", "").strip()
            cur = {"n": int(m.group(3)), "name": name, "desc": desc, "current": current,
                   "selected": bool(m.group(2))}
            rows.append(cur)
            if len(parts) > 1:
                desc_col = raw.find(parts[1][:8]) if parts[1] else 0
            else:
                desc_col = len(raw) - len(rest)
            continue
        em = _MODEL_EFFORT_RE.match(raw)
        if em and ("effort" in raw.lower()) and not raw.lstrip().startswith(("s to", "Enter")):
            cur = None
            word = em.group(2).strip()
            # 整个档位名一起识别（"Extra high" → xhigh，不能只取最后一个词误判成 high）；识别不了 → None
            effort = {"level": _level_word(word) if word else None,
                      "label": (word + " effort").strip(),
                      "is_default": bool(em.group(3))}
            continue
        if cur is not None and _indent(raw) >= max(4, desc_col - 2):
            cur["desc"] = (cur["desc"] + " " + raw.strip()).strip()
            continue
        cur = None
    d["rows"] = rows
    d["more_above"] = more_above
    d["more_below"] = more_below
    d["effort"] = effort
    d["options"] = [{"index": i, "n": r["n"], "label": r["name"], "desc": r["desc"],
                     "selected": r["selected"]} for i, r in enumerate(rows)]


def _parse_effort(body: List[str], d: Dict) -> None:
    d["kind"] = "effort"
    d["title"] = "Effort"
    level = None
    for i, raw in enumerate(body):
        if "▲" not in raw:
            continue
        col = _dwidth(raw[:raw.index("▲")])
        best = None
        for j in range(i + 1, min(len(body), i + 3)):
            names = [(m.start(), m.group(1)) for m in re.finditer(r"\b(low|medium|high|xhigh|max)\b", body[j])]
            if len(names) >= 2:
                for start, word in names:
                    c0 = _dwidth(body[j][:start])
                    center = c0 + (len(word) - 1) / 2.0
                    dist = abs(center - col)
                    if best is None or dist < best[0]:
                        best = (dist, word)
                break
        if best is not None:
            level = best[1]
        break
    d["level"] = level
    uc = None
    for raw in body:
        m = re.search(r"Ultracode\s+(on|off)\b", raw)
        if m:
            uc = m.group(1)
            break
    d["ultracode"] = uc
    d["options"] = []


def _parse_permission(body: List[str], d: Dict) -> None:
    d["kind"] = "permission"
    ne = [l for l in body if l.strip()]
    d["tool"] = ne[0].strip() if ne else ""
    d["title"] = d["tool"]
    q_idx = next((i for i, l in enumerate(body) if l.strip().startswith("Do you want to")), None)
    d["question"] = body[q_idx].strip() if q_idx is not None else ""
    dashes = [i for i, l in enumerate(body) if _DASH_RE.match(l)]
    preview: List[str] = []
    if len(dashes) >= 2:
        preview = body[dashes[0] + 1:dashes[1]]
    elif q_idx is not None:
        pass
    preview = _strip_blank_edges(preview)
    if preview and all(l.startswith(" ") or not l for l in preview):
        preview = [l[1:] if l.startswith(" ") else l for l in preview]
    d["preview"] = preview
    # 工具名与预览/问题之间的说明文字（含 Tip 段落）
    first = next((i for i, l in enumerate(body) if l.strip()), 0)
    end = dashes[0] if dashes else (q_idx if q_idx is not None else first + 1)
    mid = [l.strip() for l in body[first + 1:end] if l.strip()]
    tip = ""
    if mid and mid[0].startswith("Tip:") and len(mid) >= 2:
        tip = " ".join(mid[:-1])
        mid = mid[-1:]
    elif mid and mid[0].startswith("Tip:"):
        tip, mid = mid[0], []
    d["description"] = " ".join(mid)
    d["tip"] = tip
    after = body[q_idx + 1:] if q_idx is not None else body
    d["options"] = _parse_numbered_options(after, "label")


def _parse_question(body: List[str], d: Dict) -> None:
    d["kind"] = "question"
    ne = [l for l in body if l.strip()]
    head = ne[0].strip() if ne else ""
    header = re.sub(r"^[←→\s]*[☐☒✔✓■□]\s*", "", head).strip()
    d["header"] = header
    d["title"] = header
    first_opt = next((i for i, l in enumerate(body) if _OPT_RE.match(l)), len(body))
    qlines = []
    seen_head = False
    for l in body[:first_opt]:
        if not l.strip():
            continue
        if not seen_head:
            seen_head = True
            continue
        qlines.append(l.strip())
    d["question"] = " ".join(qlines)
    opts = _parse_numbered_options(body[first_opt:], "desc")
    for o in opts:
        o["free_text"] = o["label"].startswith("Type something")
    d["options"] = opts


def _parse_trust(body: List[str], d: Dict) -> None:
    d["kind"] = "trust"
    path = None
    for i, l in enumerate(body):
        if "Accessing workspace" in l:
            nxt = next((x for x in body[i + 1:] if x.strip()), None)
            if nxt is not None:
                path = nxt.strip()
            break
    d["path"] = path
    d["title"] = next((l.strip() for l in body if l.strip()), "")
    d["options"] = _parse_plain_options(body)


def _parse_dialog(region: List[str]) -> Dict:
    body = [l.rstrip() for l in region[1:]]  # 去掉顶边横线
    body = _strip_blank_edges(body)
    hints, hidx = _hints_of(body)
    content = body[:hidx] if hidx >= 0 else body
    d: Dict = {"kind": "generic", "title": "", "lines": body, "options": [], "hints": hints}
    stripped = [l.strip() for l in content]
    try:
        if "Select model" in stripped:
            _parse_model(content, d)
        elif "Effort" in stripped and any("▲" in l or ("low" in l and "max" in l) for l in content):
            _parse_effort(content, d)
        elif any("trust this folder" in l for l in content) or any("Accessing workspace" in l for l in content):
            _parse_trust(content, d)
        elif any(s.startswith("Do you want to") for s in stripped) and any(_OPT_RE.match(l) for l in content):
            _parse_permission(content, d)
        elif stripped and re.match(r"^[←→\s]*[☐☒✔✓■□]\s*\S", stripped[next(
                (i for i, s in enumerate(stripped) if s), 0)]) and any(_OPT_RE.match(l) for l in content):
            _parse_question(content, d)
        else:
            raise ValueError("unknown")
    except (ValueError, IndexError, AttributeError):
        d = {"kind": "generic", "title": next((s for s in stripped if s), ""), "lines": body,
             "options": [], "hints": hints}
        opts = _parse_numbered_options(content, "desc")
        d["options"] = opts or _parse_plain_options(content)
    return d


# --------------------------------------------------------------------------- 内容区
def _turn_boundary_above(content: List[str], j: int) -> bool:
    """content[j]（排队块往上第一行非 ❯ 块的内容）是不是"上一轮已经结束"的标志：结束行（✻ Baked for 9s · done …）、
    "⎿  Interrupted …"、或启动横幅（/clear 之后同样）。到了屏幕顶端（j < 0，可能是滚出去了）不算。"""
    if j < 0:
        return False
    line = content[j]
    if _DONE_RE.match(line) or _INTERRUPTED_RE.search(line):
        return True
    return j < 8 and any("Claude Code v" in l for l in content[max(0, j - 4):j + 1])


def _queued_blocks(content: List[str]) -> List[Tuple[int, int, str]]:
    """排队的消息 → [(块起始行号, 所属 Queued 行号, 文字)]，按屏幕顺序。

    真机版式（v2.1.330 实测）：多条排队消息是连续的 "❯ …" 块（多行消息的续行两空格缩进），后面**只有一行**
    共用的 "  ⎿  Queued · ctrl+x ctrl+s to send now"；也兼容每条下面各有一行 Queued。
    从每个 Queued 行往上收集连续的 ❯ 块：空行跳过；两空格缩进的行先当作候选续行，往上遇到 ❯ 行才归入该块；
    遇到其它内容（"● "、"⎿"、spinner、顶格文字…）就停，没归属的候选行（助手正文）丢弃。"""
    out: List[Tuple[int, int, str]] = []
    taken = set()
    for qi, l in enumerate(content):
        if not _QUEUED_RE.match(l):
            continue
        blocks = []
        pending: List[str] = []   # 当前 ❯ 块的候选续行（自上而下）
        j = qi - 1
        while j >= 0 and j not in taken:
            line = content[j]
            m = _ECHO_RE.match(line)
            if m:
                body = [(m.group(1) or "").rstrip()] + pending
                while len(body) > 1 and not body[-1].strip():   # 块与块之间的空行不属于消息
                    body.pop()
                blocks.append((j, "\n".join(body).strip()))
                pending = []
            elif not line.strip():
                pending.insert(0, "")
            elif line.startswith("  ") and not line.lstrip().startswith("⎿"):
                pending.insert(0, line.strip())
            else:
                break
            j -= 1
        if len(blocks) >= 2 and _turn_boundary_above(content, j):
            # 本轮还没有任何输出（还在 thinking）时，真正发出去的那条消息紧挨在排队块上方、再往上就是上一轮的结束行 /
            # Interrupted / 启动横幅：最上面那块是已发送的消息（当回显），不是排队消息（真机 v2.1.331 实测）
            blocks.pop()
        for start, text in reversed(blocks):
            taken.update(range(start, qi + 1))
            out.append((start, qi, text))
    out.sort(key=lambda x: x[0])
    return out


def _parse_hints(content: List[str], mode_line: Optional[str], turn_done: Optional[str],
                 queued: List[Tuple[int, int, str]]) -> Dict:
    """CLI 快捷键提示：background（ctrl+b 转后台）、send_now（ctrl+x ctrl+s 立即发送排队消息）、
    queued（排队中的消息文字）、shells（后台 shell 数，底栏 "N shell"，其次结束行 "N shell still running"）。"""
    shells = None
    for src in (mode_line, turn_done):
        m = _SHELLS_RE.search(src or "")
        if m:
            shells = int(m.group(1))
            break
    return {"background": any(HINT_BACKGROUND_RE.match(l) for l in content),
            "send_now": any(HINT_SEND_NOW_RE.match(l) for l in content),
            "queued": [q[2] for q in queued], "shells": shells}


def _parse_content(content: List[str], busy: bool, queued: Optional[List[Tuple[int, int, str]]] = None) -> Dict:
    out: Dict = {"spinner": None, "live": None, "echo": None, "turn_done": None,
                 "interrupted": False, "last_result": None}
    # 排队的消息（"❯ …" 块 + "⎿  Queued · …"）不是已发送的用户消息：不当回显，也不进实时草稿
    skip = set()
    for a, b, _t in queued or ():
        skip.update(range(a, b + 1))
    # 最后一个用户消息回显
    echo_idx = None
    for i in range(len(content) - 1, -1, -1):
        if i not in skip and _ECHO_RE.match(content[i]):
            echo_idx = i
            break
    spinner_idx = tip_idx = None
    for i in range(len(content) - 1, -1, -1):
        if echo_idx is not None and i <= echo_idx:
            break
        m = _SPINNER_RE.match(content[i])
        if m and not _DONE_RE.match(content[i]):
            spinner_idx = i
            out["spinner"] = m.group(2).strip()
            if i + 1 < len(content) and _TIP_RE.match(content[i + 1]):
                tip_idx = i + 1
            break
    if echo_idx is not None:
        m = _ECHO_RE.match(content[echo_idx])
        echo = [(m.group(1) or "").rstrip()]
        j = echo_idx + 1
        while (j < len(content) and content[j].startswith("  ") and content[j].strip()
               and not content[j].lstrip().startswith("⎿")):
            echo.append(content[j].strip())
            j += 1
        out["echo"] = "\n".join(echo)
        # 带图片的消息：回显下面每张图一行 "  ⎿  [Image #N]"（实测），属于回显，不进实时草稿
        while j < len(content) and _ECHO_IMAGE_RE.match(content[j]):
            j += 1
        body = content[j:]
        base = j
    else:
        body = content if busy else []
        base = 0
    kept = []
    for k, l in enumerate(body):
        idx = base + k
        if idx == spinner_idx or idx == tip_idx or idx in skip or HINT_BACKGROUND_RE.match(l):
            continue   # 快捷键提示由界面上的按钮呈现，不进实时草稿
        if _DONE_RE.match(l):
            continue
        kept.append(l.rstrip())
    kept = _strip_blank_edges(kept)
    out["live"] = "\n".join(kept) if kept else None
    for l in content[(echo_idx or 0):]:
        dm = _DONE_RE.match(l)
        if dm:
            out["turn_done"] = dm.group(1).strip()
    tail = [l for l in content if l.strip()]
    if tail and _INTERRUPTED_RE.search(tail[-1]):
        out["interrupted"] = True
    # /model、/effort 的结果：回显之后的第一条 "⎿" 行就是 CLI 的回报原文（文案随版本变化，不只认已知开头；
    # 但要提到 effort / model，"Cancelled" 之类不算结果）；其余情况沿用已知文案的匹配
    cmd = (out["echo"] or "").split("\n", 1)[0].strip().split(" ", 1)[0]
    if cmd in ("/model", "/effort"):
        for l in content[echo_idx + 1:echo_idx + 1 + RESULT_SCAN_LINES]:
            m = _RESULT_LINE_RE.match(l)
            if m:
                if _RESULT_WORD_RE.search(m.group(1)):
                    out["last_result"] = m.group(1).strip()
                break
    if out["last_result"] is None:
        for l in reversed(content[(echo_idx or 0):]):
            m = _SET_MODEL_RE.search(l) or _SET_EFFORT_RE.search(l)
            if m:
                out["last_result"] = m.group(1).strip()
                break
    return out


# ---------------------------------------------------------------- /model、/effort 的结果文案
# CLI 2.1.332 实测（fixtures/cli_2_1_332/）：
#   "Set effort level to medium (this session only): Balanced approach …"
#   "Set model to Haiku 5.5 for this session only"、"… for this session only with high effort"
#   effort 被环境变量锁定：/effort → "CLAUDE_CODE_EFFORT_LEVEL=max overrides this session — clear it and xhigh takes over"
#                          /model  → "Set model to Haiku 5.5 for this session only · effort stays max (set by
#                                     CLAUDE_CODE_EFFORT_LEVEL); xhigh isn't saved"
#   更早的版本 / transcript：“Set model to Sonnet 5.5 with high effort (this session only)”、"Kept model as `Haiku 5.5`"
RESULT_SCAN_LINES = 4   # 回显之后最多往下找这么多行
_RES_MODEL_RE = re.compile(
    r"^(?:Set model to|Kept model as)\s+(?:`([^`\n]+)`|\*\*([^*\n]+)\*\*|(.+?))"
    r"(?=\s+for this session\b|\s+and saved\b|\s+with\s+\S+\s+effort\b|\s+\(|\s+·|\s*;|\s*$)")
_RES_EFFORT_RE = re.compile(r"\beffort(?: level)? (?:to|as|at)\s+`?(\w+)`?|\bwith (\w+) effort\b", re.I)
_RES_ENV_OVERRIDE_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*EFFORT[A-Z0-9_]*)=(\w+) overrides this session\b(?:.*?\band (\w+) takes over\b)?")
_RES_ENV_STAYS_RE = re.compile(
    r"\beffort stays (\w+) \(set by ([A-Z][A-Z0-9_]*)\)(?:\s*;\s*(\w+) isn't saved\b)?")
# 被模型名正则（旧版）误吞进来的后缀：清洗持久化的旧值
_MODEL_TAIL_RE = re.compile(r"\s+(?:for this session\b|and saved\b|with\s+\S+\s+effort\b)|\s+·\s|\s*;")


def clean_model_name(name: Optional[str]) -> Optional[str]:
    """模型显示名清洗：去掉反引号 / 粗体标记，截掉 " for this session only"、" · effort stays …"、" with high effort"
    之类的结果文案后缀（旧版解析把整句当成了模型名，sessions.json 里可能存着这样的值）。空 → None。"""
    if not isinstance(name, str):
        return None
    t = name.strip().strip("`").strip()
    if t.startswith("**") and t.endswith("**") and len(t) > 4:
        t = t[2:-2].strip()
    m = _MODEL_TAIL_RE.search(t)
    if m:
        t = t[:m.start()]
    t = t.strip().strip("`").strip()
    return t or None


def parse_cmd_result(text: Optional[str]) -> Optional[Dict]:
    """/model、/effort 结果原文 → {"text","model","effort","effort_locked"}；不是字符串 / 空 → None（绝不抛异常）。
    model：模型显示名（只取名字本身，后缀一律不要）；effort：CLI 回报的**实际**档位（被环境变量锁定时是锁定的那一档）；
    effort_locked：{"level": 实际档位, "source": 环境变量名, "wanted": 用户选的档位|None} | None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    t = strip_ansi(text).strip()
    out: Dict = {"text": t, "model": None, "effort": None, "effort_locked": None}
    m = _RES_MODEL_RE.match(t)
    if m:
        out["model"] = clean_model_name(next((g for g in m.groups() if g), None))
    lk = _RES_ENV_OVERRIDE_RE.search(t)
    if lk:
        level, src, wanted = _level_word(lk.group(2)), lk.group(1), lk.group(3)
    else:
        lk = _RES_ENV_STAYS_RE.search(t)
        if lk:
            level, src, wanted = _level_word(lk.group(1)), lk.group(2), lk.group(3)
    if lk:
        out["effort"] = level
        out["effort_locked"] = {"level": level, "source": src, "wanted": _level_word(wanted) if wanted else None}
        return out
    for e in _RES_EFFORT_RE.finditer(t):
        lv = _level_word(e.group(1) or e.group(2) or "")
        if lv:
            out["effort"] = lv
            break
    return out


def _hint_model(lines: List[str], status: Optional[str]) -> Optional[Dict]:
    """当前模型/effort 的线索：状态栏（随切换更新）优先，其次启动横幅（不随 /model 更新）。"""
    if status:
        m = _STATUS_MODEL_RE.search(status)
        if m:
            return {"model": m.group(1).strip(), "effort": _level_word(m.group(2)), "source": "status"}
    for l in lines[:6]:
        m = _BANNER_RE.match(l.strip()) if "effort" in l else None
        if m and "Claude Code" not in l:
            return {"model": m.group(1).strip(), "effort": _level_word(m.group(2)), "source": "banner"}
    return None


# --------------------------------------------------------------------------- 统计（spinner / 状态栏）
# 只解析屏幕上已有的文字：spinner 是 Claude Code 自己画的；状态栏是用户自己的 statusline 脚本输出（格式因人而异，
# 解析必须宽松，解析不到就返回 None，绝不抛异常）。
_DUR_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)(?![a-z])", re.I)
_SPIN_TOKENS_RE = re.compile(r"(?:([↑↓])\s*)?(\d+(?:[.,]\d+)?)\s*([kKmM])?\s*tokens?\b")
_COST_RE = re.compile(r"(?<![\w$])\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?!\d)")
# "25k/1M"、"0/1M"、"25.3k / 200k"、"25000/200000"（上限无单位时须 ≥ 10000，排除 "3/4"、日期之类）
_CTX_FRAC_RE = re.compile(r"(?<![\w./$\-])(\d+(?:[.,]\d+)?)\s*([kKmM])?\s*/\s*(\d+(?:\.\d+)?)\s*([kKmM])?(?![\w/.])")
_PCT_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*%")
_CTX_WORD_RE = re.compile(r"(ctx|context|上下文|[▱▰█░▒▓■□▮▯▏▎▍▌▋▊▉])\W{0,3}$", re.I)
# "[1m]" 不分大小写；"1M" 必须大写（"1m 5s" 是时长）
_ONE_M_RE = re.compile(r"(?i:\[1m\])|(?<![\w.])1M(?![a-zA-Z])")


def _si_number(num: str, suffix: Optional[str]) -> Optional[float]:
    """'1.2' + 'k' → 1200；'1,234' → 1234；'25,5'（逗号当小数点）→ 25.5。"""
    try:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", num):
            v = float(num.replace(",", ""))
        else:
            v = float(num.replace(",", "."))
    except ValueError:
        return None
    mul = {"k": 1e3, "m": 1e6}.get((suffix or "").lower(), 1.0)
    return v * mul


def parse_duration(text: Optional[str]) -> Optional[float]:
    """'9s' / '1m 5s' / '1m5s' / '2h 3m' / '12.5s' / '800ms' → 秒；整段不是时长 → None。"""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    total, pos = 0.0, 0
    for m in _DUR_TOKEN_RE.finditer(t):
        if t[pos:m.start()].strip():
            return None
        v = float(m.group(1))
        total += {"ms": 0.001, "h": 3600.0, "m": 60.0, "s": 1.0}[m.group(2).lower()] * v
        pos = m.end()
    if pos == 0 or t[pos:].strip():
        return None
    return total


def parse_spinner(spinner: Optional[str]) -> Optional[Dict]:
    """spinner 文本 → {verb, elapsed_s, tokens, token_dir:"down"|"up"|None, note}；没有 spinner → None。
    实测写法：'Schlepping… (5s · ↓ 175 tokens · thinking with low effort)'、'Shimmying… (6s · ↓ 277 tokens · thought
    for 1s)'、'Cogitating… (3s · ↓ 156 tokens)'、'Wandering…'；另兼容 '↑'、'1.2k tokens'、'1m 5s'、括号被截断。"""
    if not isinstance(spinner, str) or not spinner.strip():
        return None
    text = spinner.strip()
    verb, _, rest = text.partition("…")
    out: Dict = {"verb": verb.strip() or None, "elapsed_s": None, "tokens": None, "token_dir": None, "note": None}
    rest = rest.strip()
    if rest.startswith("("):
        rest = rest[1:]
        if rest.endswith(")"):
            rest = rest[:-1]
    notes = []
    for part in (p.strip() for p in re.split(r"\s+·\s+|·", rest)):
        if not part:
            continue
        if out["elapsed_s"] is None and out["tokens"] is None:
            d = parse_duration(part)
            if d is not None:
                out["elapsed_s"] = d
                continue
        tm = _SPIN_TOKENS_RE.search(part)
        if tm and out["tokens"] is None:
            v = _si_number(tm.group(2), tm.group(3))
            if v is not None:
                out["tokens"] = int(round(v))
                out["token_dir"] = {"↓": "down", "↑": "up"}.get(tm.group(1) or "")
                continue
        notes.append(part)
    out["note"] = " · ".join(notes) or None
    return out


def parse_status_stats(status: Optional[str]) -> Optional[Dict]:
    """状态栏（用户自定义 statusline）→ {context_used, context_limit, context_pct, cost_usd, cost_text}；
    什么都没解析到 → None。例：'… ▱▱▱ 3% 25k/1M $0.0037 cache r:2551/w:9564 [Haiku 5.5 · 1M · effort:low]'。"""
    if not isinstance(status, str) or not status.strip():
        return None
    text = status.replace("\u00a0", " ")
    used = limit = pct = cost = None
    cost_text = None
    frac_span = None
    for m in _CTX_FRAC_RE.finditer(text):
        lim = _si_number(m.group(3), m.group(4))
        u = _si_number(m.group(1), m.group(2))
        if lim is None or u is None:
            continue
        if not m.group(4) and lim < 10000:
            continue
        if lim < 1000 or u > lim * 2:
            continue
        used, limit, frac_span = int(round(u)), int(round(lim)), m.span()
        break
    pcts = []
    for m in _PCT_RE.finditer(text):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if 0 <= v <= 100:
            pcts.append((m, v))
    if pcts:
        pick = None
        if frac_span is not None:   # 紧挨着 "25k/1M" 的那个百分比（前后 24 个字符内取最近的）
            near = [(min(abs(m.end() - frac_span[0]), abs(m.start() - frac_span[1])), v) for m, v in pcts]
            near = [x for x in near if x[0] <= 24]
            if near:
                pick = min(near, key=lambda x: x[0])[1]
        if pick is None:
            ctx = [v for m, v in pcts if _CTX_WORD_RE.search(text[max(0, m.start() - 16):m.start()])]
            if ctx:
                pick = ctx[0]
            elif frac_span is None:
                pick = pcts[0][1]
        pct = pick
    if pct is None and used is not None and limit:
        pct = round(used * 100.0 / limit, 1)
    cm = _COST_RE.search(text)
    if cm:
        v = _si_number(cm.group(1), None)
        if v is not None:
            cost = v
            raw = cm.group(1)
            # 有的 statusline 直接输出浮点 repr（实测本机 "$0.006114425000000001"）：小数超过 4 位时按 4 位显示
            frac = raw.split(".", 1)[1] if "." in raw else ""
            cost_text = "$" + (("%.4f" % v) if len(frac) > 4 and frac.isdigit() else raw)
    if used is None and pct is None and cost is None:
        return None
    return {"context_used": used, "context_limit": limit, "context_pct": pct, "cost_usd": cost,
            "cost_text": cost_text}


def has_1m_hint(text: Optional[str]) -> bool:
    """模型名/描述/状态栏里有 1M 上下文的标记（'[1m]'、'· 1M ·'、'(1M)'、'1M context'）。"""
    return isinstance(text, str) and bool(_ONE_M_RE.search(text))


# ---------------------------------------------------------------- /context 的输出
# CLI 2.1.331 实测：屏幕 fixtures/context_cmd/，transcript fixtures/transcript_context_*.jsonl。
# 交互式（屏幕 / transcript 的 <local-command-stdout>，后者带 ANSI 颜色）：
#   "Context usage" 标题，下面左边是 ⛁ ⛀ ⛶ ⛝ 方块网格，右边一列依次是 显示名 / 模型 id / "16k/1m tokens (2%)"；
#   窄终端时右列整体折到网格下方。数字是 CLI 的紧凑写法（16k、950.4k、1m = 1,000,000）。
# Markdown（同一次 /context 另写进 transcript 的 isMeta user 条目，也是 -p 模式的输出）：
#   "## Context Usage" / "**Model:** claude-haiku-5-5" / "**Tokens:** 16k / 1m (2%)"
_ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTX_HEAD_RE = re.compile(r"^(?:##\s*)?Context usage\s*$", re.I)
_CTX_TOKENS_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([kKmM])?\s*/\s*(\d+(?:\.\d+)?)\s*([kKmM])?"
                            r"(?:\s+tokens)?\s*\(\s*<?\s*(\d+(?:\.\d+)?)\s*%\s*\)")
_CTX_MD_MODEL_RE = re.compile(r"^\*\*Model:\*\*\s*(\S+)")
_CTX_GRID_PREFIX_RE = re.compile(r"^[\s⎿⛀⛁⛶⛝]*")
_CTX_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][\w.:@/\[\]-]{0,99}$")
CTX_SCAN_LINES = 14     # 标题之后最多往下找这么多行（窄终端右列折到网格下方时 "x/y tokens" 在第 12 行左右）


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text) if isinstance(text, str) else ""


def parse_context_usage(text: Optional[str]) -> Optional[Dict]:
    """/context 的输出（屏幕、transcript 的 ANSI 文本或 Markdown 版）→ {used, limit, pct, model, display}；
    有多份时取最后一份；找不到 "Context usage" 标题与其后的 "已用/窗口 tokens (p%)" 时返回 None（绝不抛异常）。"""
    if not isinstance(text, str) or "ontext" not in text:
        return None
    text = re.sub(r"</?local-command-(?:stdout|stderr)>", "\n", strip_ansi(text))
    lines = text.replace("\u00a0", " ").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        head = _CTX_GRID_PREFIX_RE.sub("", lines[i]).strip()
        if not _CTX_HEAD_RE.match(head):
            continue
        model = display = None
        for j in range(i + 1, min(len(lines), i + 1 + CTX_SCAN_LINES)):
            body = _CTX_GRID_PREFIX_RE.sub("", lines[j]).strip()
            mm = _CTX_MD_MODEL_RE.match(body)
            if mm:
                model = mm.group(1)
                continue
            m = _CTX_TOKENS_RE.search(body)
            if not m or (m.start() > 2 and not body.startswith("**Tokens:**")):
                continue
            used = _si_number(m.group(1), m.group(2))
            limit = _si_number(m.group(3), m.group(4))
            if used is None or limit is None or limit < 1000 or used < 0:
                break
            if model is None:
                # 交互式：tokens 行上面两行是 模型 id / 显示名（右列，去掉左边的网格）
                prev = [_CTX_GRID_PREFIX_RE.sub("", lines[k]).strip() for k in range(max(i + 1, j - 2), j)]
                if prev and _CTX_MODEL_ID_RE.match(prev[-1]):
                    model = prev[-1]
                    if len(prev) > 1 and prev[0] and len(prev[0]) <= 64:
                        display = prev[0]
            if model is not None and not _CTX_MODEL_ID_RE.match(model):
                model = None
            return {"used": int(used), "limit": int(limit), "pct": float(m.group(5)),
                    "model": model, "display": display}
        # 这一份不完整（例如画面上只露出了标题）：继续往前找上一份
    return None


def _turn_done_seconds(done: Optional[str]) -> Optional[float]:
    """'Baked for 9s · done 7:50 PM' → 9.0；'Cogitated for 1m 5s' → 65.0。"""
    if not done:
        return None
    m = re.match(r"^\S+ for (.+?)(?:\s+·|$)", done)
    return parse_duration(m.group(1)) if m else None


# --------------------------------------------------------------------------- 入口
def parse_screen(text: str) -> Dict:
    """解析一帧屏幕纯文本，返回 §4.1 结构（另附 echo/turn_done/interrupted/popup/... 辅助字段）。"""
    if not isinstance(text, str):
        text = ""
    # 终端里常见 NBSP（如输入框 "❯\u00a0"、"⎿\u00a0 "）：统一当普通空格处理
    # 右侧 diff 面板先裁掉（任何规则匹配之前），之后的输入框/对话框/正文都按裁后的左栏 + 整行宽的行解析
    lines, panel = _screen_lines_panel(text)
    lines = _blank_resize_notice(lines)

    res: Dict = {
        "busy": False, "spinner": None, "mode": "manual", "mode_line": None,
        "status_line": None, "input": {"visible": False, "text": ""}, "live": None, "dialog": None,
        "echo": None, "turn_done": None, "interrupted": False, "popup": None, "last_result": None,
        "model_hint": None, "cmd_result": None, "agents_focus": False, "agents_hint": None,
        "hints": {"background": False, "send_now": False, "queued": [], "shells": None},
        "side_panel": panel,
    }
    box = _find_input_box(lines)
    footer: List[str] = []
    if box is not None:
        lo, hi = box
        inp = lines[lo + 1:hi]
        first = re.sub(r"^❯[ \u00a0]?", "", inp[0]) if inp else ""
        rest = [l[2:] if l.startswith("  ") else l.lstrip() for l in inp[1:]]
        itext = "\n".join([first] + rest).rstrip("\n ").rstrip()
        if itext == QUEUED_PLACEHOLDER:
            # 有排队消息时 CLI 在空输入框里画的占位提示，不是用户输入的内容（否则会被当成"输入框里有残留文字"）
            res["input"] = {"visible": True, "text": "", "placeholder": itext}
        else:
            res["input"] = {"visible": True, "text": itext}
            ids, _rest = split_input_images(itext)
            if ids:   # 输入框里的图片（粘贴的图片路径 / Ctrl+V）：前端据此显示"claude 输入框里的图片"
                res["input"]["images"] = ids
        footer = lines[hi + 1:]
        content = lines[:lo]
    else:
        ds = _find_dialog_start(lines)
        if ds is not None:
            res["dialog"] = _parse_dialog(lines[ds:])
            content = lines[:ds]
        else:
            content = lines

    # 状态栏 + 模式行（模式行下方可能还有子代理列表 "● main" / "◯ general-purpose  描述   2s"）
    status = []
    agents = []
    focus = False
    for l in footer:
        if not l.strip():
            continue
        mode = _mode_of(l)
        if mode is not None or "shift+tab" in l or l.strip().startswith("esc to interrupt"):
            res["mode_line"] = l.strip()
            if mode is not None:
                res["mode"] = mode
            continue
        if not focus and res["mode_line"] is None and _AGENTS_FOCUS_RE.match(l.strip()):
            # 焦点在子代理列表上：这一行顶替了模式行，此刻看不出权限模式（mode=None，调用方沿用上一次的值）
            focus = True
            res["agents_hint"] = l.strip()
            res["mode"] = None
            continue
        am = _AGENT_ROW_RE.match(l) if (res["mode_line"] is not None or focus) else None
        if am:
            parts = re.split(r"\s{2,}", am.group(3).strip())
            rest = parts[1:]
            im = _AGENT_INFO_RE.match(rest[-1]) if rest else None
            info = rest.pop() if im else ""
            agents.append({"name": parts[0], "desc": "  ".join(rest), "info": info,
                           "state": im.group(1) if im else None,
                           "current": am.group(2) in ("●", "⏺"), "selected": bool(am.group(1))})
        else:
            status.append(l.strip())
    res["agents_focus"] = focus
    if status:
        res["status_line"] = "\n".join(status)
    res["agents_panel"] = agents or None
    tail_lines = footer if footer else [l for l in lines if l.strip()][-2:]
    res["busy"] = any("esc to interrupt" in l for l in tail_lines)

    # 输入框正上方的提示行（ctrl+g to edit…）与 slash 补全弹层
    content = list(content)
    while content and not content[-1].strip():
        content.pop()
    if box is not None:
        for _ in range(2):
            if content and (_HINT_ABOVE_INPUT_RE.match(content[-1]) or _RIGHT_HINT_RE.match(content[-1])
                            or _EFFORT_HINT_RE.match(content[-1])):
                content.pop()
                while content and not content[-1].strip():
                    content.pop()
        pop = []
        k = len(content) - 1
        # 说明太长时 CLI 把它折到下一行（缩进到说明列，实测 2.1.332 的 skill）：续行也属于补全列表
        while k >= 0 and content[k].strip() and (_POPUP_RE.match(content[k]) or _POPUP_CONT_RE.match(content[k])):
            pop.append(content[k])
            k -= 1
        while pop and not _POPUP_RE.match(pop[-1]):   # 最上面必须是一项命令，多扫的续行样子的行还回去
            pop.pop()
            k += 1
        if pop and any(re.match(r"^\s{2,4}❯\s", p) for p in pop):
            items = []
            for p in reversed(pop):
                m = _POPUP_RE.match(p)
                if not m:
                    items[-1]["desc"] = (items[-1]["desc"] + " " + p.strip()).strip()
                    continue
                items.append({"cmd": m.group(2), "desc": (m.group(3) or "").strip(),
                              "selected": bool(m.group(1))})
            res["popup"] = items
            content = content[:k + 1]
    queued = _queued_blocks(content)
    res.update(_parse_content(content, res["busy"], queued))
    res["cmd_result"] = parse_cmd_result(res["last_result"])
    # CLI 快捷键提示（对话框区域不算：有对话框时这些动作都不可用）
    res["hints"] = _parse_hints(content, res["mode_line"], res["turn_done"], queued)
    res["model_hint"] = _hint_model(lines, res["status_line"])
    # 统计用的辅助字段（只读屏幕文字）：spinner 的用时/tokens、本轮结束行的用时、状态栏里的上下文/花费
    res["spinner_info"] = parse_spinner(res["spinner"])
    res["turn_done_s"] = _turn_done_seconds(res["turn_done"])
    res["status_stats"] = parse_status_stats(res["status_line"])
    # 屏幕上的 /context 输出（fullscreen 渲染器下一直留在画面里；只读，用来知道 CLI 自己认定的上下文窗口）
    res["context_usage"] = parse_context_usage("\n".join(lines)) if "ontext usage" in text else None
    return res


def selected_index(dialog: Optional[Dict]) -> Optional[int]:
    """对话框当前选中项的 index（无则 None）。"""
    if not dialog:
        return None
    for o in dialog.get("options") or []:
        if o.get("selected"):
            return o.get("index")
    return None


def selected_model_n(dialog: Optional[Dict]) -> Optional[int]:
    if not dialog or dialog.get("kind") != "model":
        return None
    for r in dialog.get("rows") or []:
        if r.get("selected"):
            return r.get("n")
    return None
