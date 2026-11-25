"""重启 / 续接 / 新建 / 换 API 重启的阶段进度（meta.op）：阶段表、状态推进与中文错误摘要（纯函数，无 I/O）。

只在 CC Remote Chat 自己这侧报告进度：不改变 claude 的启动参数、环境、按键（SPEC §0）。

meta.op 形如：
  {"id", "kind": "create|restart|reconnect|resume_new_job|switch_api|mode",
   "state": "running|done|failed|trust|stalled", "phase": 当前阶段 id,
   "phases": [{"id","label","slow_after","slow_hint","state":"todo|cur|done|fail","t0","t1"}],
   "started_at", "phase_started_at", "finished_at", "detail", "error", "error_code", "jobid", "local", "retries"}
时间都是 time.time()（秒）；前端据此显示已用秒数与"比平时慢"。
"""
from __future__ import annotations

import copy
import re
import time
from typing import Optional

KINDS = ("create", "restart", "reconnect", "resume_new_job", "switch_api", "mode")
RUNNING = "running"
FINAL = ("done", "failed", "trust", "stalled")
ORDER = ("stop", "probe", "connect", "keeper", "start", "ready")

# 各阶段"正常"时长（秒）：超过后前端显示"比平时慢，仍在进行"
SLOW_REMOTE = {"stop": 20, "probe": 15, "connect": 30, "keeper": 30, "start": 15, "ready": 30}
SLOW_LOCAL = {"stop": 20, "connect": 10, "keeper": 10, "start": 10, "ready": 20}
MAX_ERR = 200


def _labels(local: bool, host: str, jobid: str, resume: Optional[str]) -> dict:
    rs = (" --resume %s" % resume[:8]) if resume else ""
    if local:
        return {"stop": "退出旧的 claude（/exit）", "connect": "启动本机 agent",
                "keeper": "确认本机 tmux 常驻进程", "start": "在本机 tmux 里启动 claude" + rs,
                "ready": "等待 claude 就绪（出现输入框）"}
    return {"stop": "退出旧的 claude（/exit）", "probe": "ssh %s：查询作业 %s（squeue）" % (host, jobid),
            "connect": "srun --jobid=%s --overlap：在作业上启动 agent" % jobid,
            "keeper": "确认 tmux 常驻进程（keeper）", "start": "在 tmux 里启动 claude" + rs,
            "ready": "等待 claude 就绪（出现输入框）"}


def _slow_hint(pid: str, local: bool, host: str) -> str:
    named = bool(host) and host != "服务器"   # 没配 ssh 主机（开源版默认）时不写主机名
    if not local and pid == "probe":
        return "比平时慢，仍在进行（登录节点%s偶尔响应慢）" % ((" %s " % host) if named else "")
    if not local and pid in ("connect", "keeper"):
        return "比平时慢，仍在进行（%s的 Slurm 偶尔不稳）" % (("%s " % host) if named else "集群")
    return "比平时慢，仍在进行"


def link_phases(local: bool, link_state: Optional[str]) -> list:
    """连接阶段：没有 link → 服务器 [probe, connect] / 本机 [connect]；有 link 但未就绪 → [connect]；就绪 → []。"""
    if link_state == "ready":
        return []
    if link_state is None:
        return ["connect"] if local else ["probe", "connect"]
    return ["connect"]


def phase(op: dict, pid: str) -> Optional[dict]:
    for p in op.get("phases") or ():
        if p["id"] == pid:
            return p
    return None


def new_op(op_id: int, kind: str, ids: list, *, local: bool, host: str, jobid: str, resume: Optional[str] = None,
           now: Optional[float] = None, started_at: Optional[float] = None, detail: Optional[str] = None) -> dict:
    now = time.time() if now is None else now
    labels = _labels(local, host or "服务器", jobid, resume)
    slow = SLOW_LOCAL if local else SLOW_REMOTE
    ids = [i for i in ORDER if i in ids]
    phases = [{"id": i, "label": labels[i], "slow_after": slow.get(i, 30), "slow_hint": _slow_hint(i, local, host),
               "state": "todo", "t0": None, "t1": None} for i in ids]
    op = {"id": op_id, "kind": kind if kind in KINDS else "restart", "state": RUNNING, "phase": None, "phases": phases,
          "started_at": started_at or now, "phase_started_at": now, "finished_at": None, "detail": detail,
          "error": None, "error_code": None, "jobid": jobid, "local": bool(local), "retries": 0,
          "_labels": labels, "_host": host}
    if phases:
        set_phase(op, phases[0]["id"], now=now, detail=detail)
    return op


def set_phase(op: dict, pid: str, *, now: Optional[float] = None, detail: Optional[str] = None) -> bool:
    """进入阶段 pid：之前的阶段都标为完成。pid 不在表里时按 ORDER 插到合适位置（例如连接中途断开）。
    返回是否有变化。"""
    if op.get("state") != RUNNING or pid not in ORDER:
        return False
    now = time.time() if now is None else now
    if op.get("phase") == pid:
        if detail is not None and op.get("detail") != detail:
            op["detail"] = detail
            return True
        return False
    p = phase(op, pid)
    if p is None:
        local = op.get("local")
        labels = op.get("_labels") or {}
        p = {"id": pid, "label": labels.get(pid, pid), "slow_after": (SLOW_LOCAL if local else SLOW_REMOTE).get(pid, 30),
             "slow_hint": _slow_hint(pid, bool(local), op.get("_host") or ""), "state": "todo", "t0": None, "t1": None}
        rank = ORDER.index(pid)
        pos = len(op["phases"])
        for i, q in enumerate(op["phases"]):
            if ORDER.index(q["id"]) > rank:
                pos = i
                break
        op["phases"].insert(pos, p)
    rank = ORDER.index(pid)
    for q in op["phases"]:
        r = ORDER.index(q["id"])
        if r < rank and q["state"] != "done":
            q["state"] = "done"
            q["t0"] = q["t0"] or now
            q["t1"] = now
        elif r > rank and q["state"] == "cur":   # 不会回退，仅防御
            q["state"] = "todo"
    p["state"] = "cur"
    p["t0"] = now
    p["t1"] = None
    op["phase"] = pid
    op["phase_started_at"] = now
    op["detail"] = detail
    return True


def relabel_start(op: dict, resume: Optional[str]) -> None:
    """重启时才最终确定带不带 --resume（还没有对话的会话以新会话启动）：改"启动 claude"那一项的文字。"""
    labels = op.get("_labels") or {}
    base = (labels.get("start") or "").split(" --resume ")[0]
    if not base:
        return
    labels["start"] = base + ((" --resume %s" % resume[:8]) if resume else "（新会话）")
    p = phase(op, "start")
    if p is not None:
        p["label"] = labels["start"]


def finish(op: dict, state: str, *, error: Optional[str] = None, code: Optional[str] = None,
           detail: Optional[str] = None, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    if state not in FINAL:
        state = "failed"
    cur = op.get("phase")
    for q in op["phases"]:
        if state == "done" or (state in ("trust", "stalled") and q["id"] != "ready"):
            if q["state"] != "done":
                q["state"] = "done"
                q["t0"] = q["t0"] or now
                q["t1"] = now
        elif state == "failed" and q["id"] == cur:
            q["state"] = "fail"
            q["t1"] = now
    if state in ("trust", "stalled"):
        p = phase(op, "ready")
        if p is not None:
            p["state"] = "done" if state == "trust" else "cur"
            p["t1"] = now if state == "trust" else None
    op["state"] = state
    op["finished_at"] = now
    op["error"] = clip(error) if error else None
    op["error_code"] = code
    if detail is not None:
        op["detail"] = detail


def public(op: Optional[dict]) -> Optional[dict]:
    """给 meta 用的副本（去掉内部字段）。"""
    if not op:
        return None
    out = copy.deepcopy({k: v for k, v in op.items() if not k.startswith("_")})
    return out


def clip(text, n: int = MAX_ERR) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n - 1] + "…"


# ---------------------------------------------------------------- 错误摘要
_JOB_GONE = re.compile(r"Invalid job id|has expired|Expired or invalid job|Job has already finished|"
                       r"already completing|job .{0,40}not found|作业 \S+ 已结束", re.I)


def short_reason(text, *, host: str = "", jobid: str = "", local: bool = False) -> str:
    """从 ssh / srun / agent 的原始报错里提炼一句中文原因（不把整段 stderr 甩给用户）。"""
    t = str(text or "")
    low = t.lower()
    h = host or "服务器"
    if not local and _JOB_GONE.search(t):
        return "srun 无法连接作业 %s：作业已结束" % jobid
    if "could not resolve hostname" in low or "nodename nor servname" in low:
        return "ssh 无法解析主机 %s（请检查网络 / ssh 配置）" % h
    if re.search(r"(connection|operation) timed out|timed out during banner|connect to host .{0,80} timed out", low):
        return "ssh 连接 %s 超时" % h
    if "connection refused" in low:
        return "ssh 连接 %s 被拒绝" % h
    if "permission denied" in low:
        return "ssh 登录 %s 被拒绝（Permission denied）" % h
    if "host key verification failed" in low:
        return "ssh 主机密钥校验失败（%s）" % h
    if "network is unreachable" in low or "no route to host" in low:
        return "网络不可达，无法 ssh 到 %s" % h
    if "kex_exchange_identification" in low or re.search(r"connection (closed|reset) by", low):
        return "ssh 连接 %s 被对方断开（登录节点繁忙？）" % h
    m = re.search(r"srun: error: ([^；\n]+)", t)
    if m:
        return clip("srun 报错：" + m.group(1).strip(), 160)
    if "hello" in low and ("超时" in t or "timeout" in low):
        return "agent 启动后没有响应（hello 超时）"
    # 兜底：取最后一段有内容的话（"；" / 换行分隔），去掉"连接计算节点 agent 失败："之类的前缀
    parts = [p.strip() for p in re.split(r"[；\n]", t) if p.strip()]
    last = parts[0] if parts else ""
    last = re.sub(r"^连接(计算节点|本机) agent 失败：", "", last)
    return clip(last or "未知错误", 160)


def describe_failure(pid: Optional[str], err, *, host: str, jobid: str, local: bool, timeout: float = 0,
                     code: Optional[str] = None) -> str:
    """阶段失败时给用户看的一句话。"""
    raw = str(err or "")
    where = "本机" if local else "作业 %s" % jobid
    if code == "job_ended" or (not local and _JOB_GONE.search(raw)):
        return "srun 无法连接作业 %s：作业已结束（请在新作业上续接）" % jobid
    if pid in ("probe", "connect"):
        reason = short_reason(_after_colon(raw), host=host, jobid=jobid, local=local)
        if "超时" in raw and timeout:
            secs = "%g" % round(float(timeout), 1)
            base = ("启动本机 agent 超时（已等 %s 秒）" % secs) if local else \
                ("连接作业 %s 超时（已等 %s 秒）" % (jobid, secs))
            tail = _after_colon(raw)
            return base + ("：" + short_reason(tail, host=host, jobid=jobid, local=local) if tail else "")
        return ("启动本机 agent 失败：" if local else "连接作业 %s 失败：" % jobid) + reason
    if pid == "keeper":
        if "超时" in raw:
            return ("等待本机 tmux 常驻进程启动超时" if local else
                    "等待 tmux 常驻进程启动超时，请确认作业 %s 仍在运行（Slurm 繁忙时可稍后重试）" % jobid)
        return "启动 tmux 常驻进程失败：" + short_reason(_after_colon(raw), host=host, jobid=jobid, local=local)
    if pid == "start":
        return "在%s的 tmux 里启动 claude 失败：%s" % (where, short_reason(_after_colon(raw), host=host, jobid=jobid,
                                                                     local=local))
    if pid == "stop":
        return "退出旧的 claude 失败：" + short_reason(raw, host=host, jobid=jobid, local=local)
    return short_reason(raw, host=host, jobid=jobid, local=local)


def _after_colon(raw: str) -> str:
    """"连接计算节点超时（作业 J）：连接计算节点 agent 失败：<stderr>" → "<stderr>"（保留原文给 short_reason 匹配）。"""
    s = str(raw or "")
    for pre in ("连接计算节点超时", "连接本机 agent 超时", "本机操作失败：", "远端操作失败：", "启动 tmux 常驻进程失败："):
        if s.startswith(pre):
            i = s.find("：")
            return s[i + 1:] if i >= 0 else ""
    return s
