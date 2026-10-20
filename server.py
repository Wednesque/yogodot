#!/usr/bin/env python3
"""
YOGO 控制台 —— 本地服务

  1. 托管 yogo.html（固定来源，WebHID 授权只需一次）
  2. 拉服务器上 Claude Code 的状态，**按 tmux 窗口分别跟踪**
  3. SSE 推给页面（GET /events）
  4. 接收 Coding 伴侣标准事件（POST /events）
  5. 接收本地热键动作（POST /action），并回答"该跳去哪个窗口"（GET /jump）

服务器状态不走反向隧道：那边的 hook 只往 ~/.claude/yogo/events.jsonl 追加一行，
这边常驻一条 `ssh <你的服务器> tail -F` 把流拉回来（见 ssh_tail）。
换服务器就改环境变量 YOGO_SSH_HOST。

用法：  py -3 server.py
"""
import hashlib
import json
import os

import paths
import queue
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

PORT = int(os.environ.get("YOGO_PORT", "8787"))
HOOK_PORT = int(os.environ.get("YOGO_HOOK_PORT", "9099"))
ROOT = paths.RES                    # 页面等只读资源
DATA = paths.DATA                  # 设置 / token 等要写的东西

# hook 事件 -> 状态。只用安全的事件，不介入权限决策。
EVENT_STATE = {
    "SessionStart":       "idle",
    "UserPromptSubmit":   "busy",
    "PreToolUse":         "busy",
    "PostToolBatch":      "busy",
    "Notification":       "wait",     # Claude 需要权限或输入时触发
    "PermissionRequest":  "wait",
    "Stop":               "done",
    "SubagentStop":       "busy",     # 子代理结束，主任务还在跑
    "PostToolUseFailure": "error",
    "StopFailure":        "error",
    "SessionEnd":         "idle",
}
PRIORITY = {"wait": 5, "ask": 5, "error": 4, "busy": 3, "done": 2, "idle": 1}
IDLE_AFTER = 15 * 60      # 15 分钟没动静就当空闲
DROP_AFTER = 2 * 3600     # 2 小时没动静就从列表里去掉，免得攒一堆死会话

# ── Coding 伴侣：标准事件 v1 ────────────────────────────────────
# 外部工具往 POST /events 发标准事件；适配器负责把各家原生 hook 翻译成这 7 个事件。
# session_started / session_ended 用于主动生命周期管理，其余 5 个是任务状态。
COMPANION_EVENTS = {
    "session_started": None,
    "session_ended": None,
    "user_message_submitted": "busy",
    "processing_resumed": "busy",
    "approval_requested": "wait",
    "supplement_requested": "ask",
    "turn_completed": None,          # outcome=failed -> error，其余 done
}
COMPANION_EVENT_ALIASES = {
    "session_start": "session_started",
    "session_end": "session_ended",
    "user_message": "user_message_submitted",
    "prompt_submitted": "user_message_submitted",
    "processing_started": "processing_resumed",
    "approval_required": "approval_requested",
    "supplement_required": "supplement_requested",
    "turn_complete": "turn_completed",
}
AGENT_ALIASES = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "claude-code": "claude-code",
    "claude code": "claude-code",
    "codex": "codex",
    "openai-codex": "codex",
    "openai_codex": "codex",
    "opencode": "opencode",
    "open-code": "opencode",
    "open_code": "opencode",
    "trae": "trae",
    "trae-cn": "trae",
    "trae_cn": "trae",
}
COMPANION_SEEN_MAX = 1024     # eventId 去重窗口
COMPANION_GRACE_MS = 15000    # 15 秒内的乱序仍然接受；超过就当过期事件丢掉
COMPANION_TURNS_MAX = 8       # 每个会话记住最近几轮已完成，挡住迟到事件
COMPANION_PENDING_MAX = 8     # 每个会话最多挂几个待处理授权

_lock = threading.Lock()
_subs = []
_sessions = {}            # key(tmux 窗口名或 session_id) -> dict
_seen_events = OrderedDict()   # eventId -> 收到时间；挡住 hook 重试
_requests = OrderedDict()      # requestId -> session key；授权/补充的待处理账本
_ended = OrderedDict()         # sessionId -> session_ended 的毫秒时间；挡住迟到事件
_hud_keep = {"at": 0.0}   # HUD 报「鼠标还在我身上」，用来推迟自动收起
_want = {}                # 页面请求宿主做的事（比如把控制台窗口叫出来），托盘程序来取
_geom = {}                # 旧版胶囊留下的，现在没人用了
CAP_FILE = paths.data("capsule.json")
_cap = {"skin": "", "anim": "island", "pet": "octopus"}   # 空 skin = 跟随键盘主题
try:
    _cap.update(json.load(open(CAP_FILE, encoding="utf-8")))
except Exception:
    pass
# ── 总开关 ──────────────────────────────────────────────────────
# claude=False 时这就是一个纯键盘灯效工具：没有胶囊、没有 vibe、不连服务器、不问额度。
# 只用键盘的人不用面对那一堆跟 Claude 有关的东西。默认关。
SET_FILE = paths.data("settings.json")
_settings = {"claude": False, "ssh_host": ""}
try:
    _settings.update(json.load(open(SET_FILE, encoding="utf-8")))
except Exception:
    pass


def claude_on():
    return bool(_settings.get("claude"))


def save_settings():
    try:
        json.dump(_settings, open(SET_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


_ui = {}                  # 控制台页面上报的番茄钟 / 键盘状态，给 HUD 显示
_recent = []              # 最近的请求路径（排查用）
_status = {}              # src(local / coder / writer) -> Claude Code statusLine 的原始 JSON
                          # 里面有官方的 rate_limits.five_hour / seven_day / spend_limit


def _now_ms():
    return int(time.time() * 1000)


def _to_ms(v):
    """occurredAt 接受毫秒、秒、数字字符串或 ISO 8601。"""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return 0
        return int(x * 1000) if x < 1e11 else int(x)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0
        try:
            return _to_ms(float(s))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0
    return 0


def _txt(v, n):
    if isinstance(v, str):
        return v.strip()[:n]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)[:n]
    return ""


def _norm_agent(raw):
    s = _txt(raw, 40).lower()
    if not s:
        return ""
    s = AGENT_ALIASES.get(s, s)
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-")
    return s[:32] or "unknown"


def _new_session(key, agent="claude-code", now=None):
    return {
        "key": key,
        "sessionId": key,
        "name": key,
        "agent": agent or "claude-code",
        "state": "idle",
        "tool": "",
        "cwd": "",
        "project": "",
        "host": "",
        "msg": "",
        "idx": "",
        "turnId": "",
        "requestId": "",
        "pending": 0,
        "seq": None,
        "eventAt": 0,
        "startedAt": 0,
        "at": now if now is not None else time.time(),
        "prev": "idle",
        "_pending": OrderedDict(),
        "completedTurns": OrderedDict(),
    }


def _public_row(v):
    return {
        "key": v.get("key", ""),
        "sessionId": v.get("sessionId", v.get("key", "")),
        "name": v.get("name", v.get("key", "")),
        "agent": v.get("agent", "claude-code"),
        "state": v.get("state", "idle"),
        "tool": v.get("tool", ""),
        "cwd": v.get("cwd", ""),
        "project": v.get("project", ""),
        "host": v.get("host", ""),
        "msg": v.get("msg", ""),
        "idx": v.get("idx", ""),
        "turnId": v.get("turnId", ""),
        "requestId": v.get("requestId", ""),
        "pending": int(v.get("pending", 0) or 0),
        "seq": v.get("seq"),
        "eventAt": int(v.get("eventAt", 0) or 0),
        "at": float(v.get("at", 0) or 0),
        "prev": v.get("prev", "idle"),
    }


def _pending_of(cur):
    pend = cur.get("_pending")
    if pend is None:
        pend = cur["_pending"] = OrderedDict()
    return pend


def _drop_attention_locked(key, request_id=None):
    """清掉一个会话的待处理授权；request_id 为 None 时全清。"""
    cur = _sessions.get(key)
    if not cur:
        return 0
    pend = _pending_of(cur)
    if request_id is None:
        ids = list(pend)
    else:
        ids = [request_id] if request_id in pend else []
    for rid in ids:
        pend.pop(rid, None)
        if rid:
            _requests.pop(rid, None)
    cur["pending"] = len(pend)
    if not pend:
        cur["requestId"] = ""
    return len(ids)


def _resolve_attention_locked(key, request_id):
    """processing_resumed 能不能解除等待态。"""
    cur = _sessions.get(key)
    if not cur:
        return True
    pend = _pending_of(cur)
    if not pend:
        return True
    if request_id and request_id in pend:
        _drop_attention_locked(key, request_id)
        return True
    # 等待记录没带 requestId（空 key），这次 resumed 就当解除
    if not any(pend):
        _drop_attention_locked(key)
        return True
    if not request_id:
        _drop_attention_locked(key)
        return True
    return False


def _add_attention_locked(cur, event, request_id, turn_id, at_ms):
    pend = _pending_of(cur)
    rid = request_id or ""
    pend.pop(rid, None)
    pend[rid] = {"event": event, "turnId": turn_id, "at": at_ms}
    if rid:
        _requests[rid] = cur["key"]
        _requests.move_to_end(rid)
        while len(_requests) > 2048:
            _requests.popitem(last=False)
    while len(pend) > COMPANION_PENDING_MAX:
        old, _ = pend.popitem(last=False)
        if old:
            _requests.pop(old, None)
    cur["pending"] = len(pend)
    cur["requestId"] = rid


def _mark_completed_locked(cur, turn_id, now):
    if not turn_id:
        return
    done = cur.get("completedTurns")
    if done is None:
        done = cur["completedTurns"] = OrderedDict()
    done.pop(turn_id, None)
    done[turn_id] = now
    while len(done) > COMPANION_TURNS_MAX:
        done.popitem(last=False)


def _remove_session_locked(key):
    cur = _sessions.pop(key, None)
    if not cur:
        return 0
    for rid in list(_pending_of(cur)):
        if rid:
            _requests.pop(rid, None)
    return 1


def _mark_ended_locked(key, at_ms):
    _ended[key] = max(int(_ended.get(key, 0) or 0), int(at_ms or 0) or _now_ms())
    _ended.move_to_end(key)
    while len(_ended) > 512:
        _ended.popitem(last=False)


def _snapshot_locked(now):
    rows = []
    dead = []
    for k, v in sorted(_sessions.items()):
        at = float(v.get("at", 0) or 0)
        if now - at > DROP_AFTER:
            dead.append(k)
            continue
        if v.get("state") != "idle" and now - at > IDLE_AFTER:
            v["prev"] = v.get("state", "idle")
            v["state"] = "idle"
            _drop_attention_locked(k)
        rows.append(_public_row(v))
    for k in dead:
        _remove_session_locked(k)
    top = max(rows, key=lambda r: (PRIORITY.get(r.get("state"), 0),
                                   r.get("at", 0), r.get("eventAt", 0)), default=None)
    return {"sessions": rows, "top": top}


def snapshot():
    with _lock:
        return _snapshot_locked(time.time())


def broadcast(obj):
    data = json.dumps(obj, ensure_ascii=False)
    with _lock:
        dead = []
        for q in _subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subs.remove(q)


def record(key, state, **extra):
    """旧接口（/hook、/state、服务器事件流）写会话。新工具走 apply_event。"""
    key = _txt(key, 120) or "claude"
    now = time.time()
    agent = _norm_agent(extra.get("agent"))
    with _lock:
        cur = _sessions.get(key)
        if cur is None:
            cur = _sessions[key] = _new_session(key, agent or "claude-code", now)
        if agent:
            cur["agent"] = agent
        cur["prev"] = cur.get("state", "idle")
        cur["state"] = state
        cur["at"] = now
        cur["eventAt"] = int(now * 1000)
        for k, v in extra.items():
            if k in ("agent", "key", "sessionId") or v in (None, ""):
                continue
            cv = _txt(v, 200)
            if cv:
                cur[k] = cv
        if state == "idle":
            _drop_attention_locked(key)
        snap = _snapshot_locked(now)
    broadcast(snap)
    print("[%-10s] %-5s  %s" % (key[:10], state, extra.get("tool", "")), flush=True)
    return snap


# POST /events 请求体（除 event 外都可省略，sessionId 必填）：
# {
#   "schema": "yogo.companion.v1",
#   "event": "approval_requested",
#   "sessionId": "...", "turnId": "...", "eventId": "...",
#   "agent": "claude-code|codex|opencode|trae",
#   "occurredAt": 1760000000000, "sequence": 12,
#   "interaction": {"requestId": "...", "tool": "Bash"},
#   "outcome": "completed|failed",
#   "data": {"message": "...", "cwd": "...", "project": "..."}
# }
def _parse_companion_event(d):
    if not isinstance(d, dict):
        raise ValueError("body must be a JSON object")
    if set(d) == {"_raw"}:
        raise ValueError("invalid JSON body")
    schema = _txt(d.get("schema"), 40)
    if schema and not re.fullmatch(r"(?:(?:yogo\.)?companion\.)?v1(?:\.\d+)?", schema):
        raise ValueError("unsupported schema: %s" % schema)

    raw_ev = d.get("event")
    if raw_ev is None:
        raw_ev = d.get("eventType") or d.get("event_type") or d.get("type") or ""
    if not isinstance(raw_ev, str):
        raise ValueError("event must be a string")
    ev = raw_ev.strip().lower().replace("-", "_")
    ev = COMPANION_EVENT_ALIASES.get(ev, ev)
    if ev not in COMPANION_EVENTS:
        raise ValueError("unknown event: %s" % (raw_ev[:60] or "(empty)"))

    data = d.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    inter = d.get("interaction") or {}
    if not isinstance(inter, dict):
        raise ValueError("interaction must be an object")

    sid = d.get("sessionId") or d.get("session_id") or d.get("session") \
        or data.get("sessionId") or data.get("session_id")
    sid = _txt(sid, 120)
    if not sid:
        raise ValueError("sessionId is required")

    tid = d.get("turnId") or d.get("turn_id") or d.get("turn") \
        or data.get("turnId") or data.get("turn_id")
    tid = _txt(tid, 80)

    eid = d.get("eventId") or d.get("event_id") or d.get("id") or data.get("eventId")
    eid = _txt(eid, 120)

    ms = _to_ms(d.get("occurredAt") or d.get("occurred_at") or d.get("timestamp")
                or d.get("ts") or data.get("occurredAt") or data.get("timestamp"))
    if ms <= 0:
        ms = _now_ms()

    seq_raw = d.get("sequence", data.get("sequence"))
    seq = None
    if isinstance(seq_raw, bool):
        seq = None
    elif isinstance(seq_raw, (int, float)):
        seq = int(seq_raw)
    elif isinstance(seq_raw, str) and seq_raw.strip().lstrip("-").isdigit():
        seq = int(seq_raw.strip())

    rid = d.get("requestId") or d.get("request_id") or inter.get("requestId") \
        or inter.get("request_id") or data.get("requestId")
    rid = _txt(rid, 120)

    outcome = d.get("outcome") or data.get("outcome") or inter.get("outcome") or ""
    outcome = _txt(outcome, 40).lower()

    msg = data.get("message")
    if msg is None:
        msg = d.get("message")
    msg = _txt(msg, 200)

    tool = data.get("tool") or data.get("toolName") or inter.get("tool") \
        or inter.get("toolName") or d.get("tool") or ""
    tool = _txt(tool, 80)

    cwd = data.get("cwd") or d.get("cwd") or ""
    cwd = _txt(cwd, 300)

    project = data.get("project") or data.get("projectName") \
        or d.get("project") or d.get("projectName") or ""
    project = _txt(project, 80)
    if not project and cwd:
        project = os.path.basename(cwd.rstrip("/\\"))[:80]

    name = data.get("sessionName") or data.get("name") \
        or d.get("sessionName") or d.get("name") or ""
    name = _txt(name, 80)

    host = data.get("host") or d.get("host") or ""
    host = _txt(host, 80)

    idx = data.get("index") or data.get("idx") or d.get("idx") or ""
    idx = _txt(idx, 40)

    agent = _norm_agent(d.get("agent") or data.get("agent") or inter.get("agent") or "")

    return {
        "event": ev,
        "sessionId": sid,
        "turnId": tid,
        "eventId": eid,
        "occurredAt": ms,
        "sequence": seq,
        "requestId": rid,
        "outcome": outcome,
        "message": msg,
        "tool": tool,
        "cwd": cwd,
        "project": project,
        "name": name,
        "host": host,
        "idx": idx,
        "agent": agent,
    }


def _auto_event_id(ev, key):
    sig = "|".join([key, ev["turnId"], ev["event"], str(ev["sequence"]),
                    str(ev["occurredAt"]), ev["requestId"], ev["message"]])
    return "auto:" + hashlib.sha1(sig.encode("utf-8")).hexdigest()


def apply_event(d):
    """标准事件入口。返回 (响应体, HTTP 状态码)。"""
    try:
        ev = _parse_companion_event(d)
    except ValueError as e:
        return {"ok": False, "why": str(e)[:160]}, 400

    key = ev["sessionId"]
    now = time.time()
    now_ms = int(now * 1000)
    eid = ev["eventId"] or _auto_event_id(ev, key)
    need_broadcast = False

    with _lock:
        if eid in _seen_events:
            _seen_events.move_to_end(eid)
            cur = _sessions.get(key)
            return {"ok": True, "event": ev["event"], "sessionId": key,
                    "state": (cur or {}).get("state", "idle"),
                    "duplicate": True}, 200
        _seen_events[eid] = now
        _seen_events.move_to_end(eid)
        while len(_seen_events) > COMPANION_SEEN_MAX:
            _seen_events.popitem(last=False)

        if ev["event"] == "session_ended":
            cur = _sessions.get(key)
            if cur and (int(cur.get("startedAt") or 0) > ev["occurredAt"]
                        or ev["occurredAt"] < int(cur.get("eventAt") or 0)):
                result = {"ok": True, "event": ev["event"], "sessionId": key,
                          "state": cur.get("state", "idle"), "stale": True}
            else:
                removed = _remove_session_locked(key)
                _mark_ended_locked(key, ev["occurredAt"])
                result = {"ok": True, "event": ev["event"], "sessionId": key,
                          "removed": removed}
                need_broadcast = True
            snap = _snapshot_locked(now)
        else:
            # 会话已经结束：结束前的迟到事件丢掉；结束后的新事件当新会话
            if key in _ended:
                ended_at = int(_ended.get(key) or 0)
                late = ev["occurredAt"] <= ended_at
                if not late and ev["event"] not in ("session_started",
                                                    "user_message_submitted"):
                    # 其他业务事件留 15 秒乱序窗口，挡住结束前发出的迟到 hook
                    late = ev["occurredAt"] <= ended_at + COMPANION_GRACE_MS
                if late:
                    _snapshot_locked(now)
                    return {"ok": True, "event": ev["event"], "sessionId": key,
                            "state": "idle", "stale": True, "ended": True}, 200
                _ended.pop(key, None)

            cur = _sessions.get(key)
            existed = cur is not None
            new_session = cur is None
            if ev["event"] == "session_started" and cur:
                # 只有发生在现有事件之后（或空闲/完成态同一毫秒）的 start 才算重开；
                # 迟到的 SessionStart 不能把正在跑的任务打回空闲。
                cur_at = int(cur.get("eventAt") or 0)
                if ev["occurredAt"] > cur_at:
                    new_session = True
                elif ev["occurredAt"] == cur_at and cur.get("state") in ("idle", "done", "error"):
                    new_session = True
                else:
                    new_session = False

            if cur is None:
                cur = _sessions[key] = _new_session(key, ev["agent"] or "claude-code", now)

            if ev["event"] == "session_started":
                cur["startedAt"] = max(int(cur.get("startedAt") or 0), ev["occurredAt"])

            stale = False
            if ev["event"] == "session_started":
                stale = existed and not new_session
            else:
                if ev["turnId"] and ev["turnId"] in (cur.get("completedTurns") or {}):
                    stale = True
                if not stale and ev["event"] != "user_message_submitted" \
                        and ev["sequence"] is not None and cur.get("seq") is not None \
                        and (not ev["turnId"] or ev["turnId"] == cur.get("turnId")) \
                        and ev["sequence"] <= int(cur.get("seq")):
                    stale = True
                if not stale and ev["occurredAt"] and cur.get("eventAt") \
                        and ev["occurredAt"] + COMPANION_GRACE_MS < int(cur.get("eventAt")):
                    stale = True

            if stale:
                result = {"ok": True, "event": ev["event"], "sessionId": key,
                          "state": cur.get("state", "idle"), "stale": True}
            else:
                if ev["agent"]:
                    cur["agent"] = ev["agent"]
                if ev["name"]:
                    cur["name"] = ev["name"]
                if ev["project"]:
                    cur["project"] = ev["project"]
                if ev["cwd"]:
                    cur["cwd"] = ev["cwd"]
                if ev["host"]:
                    cur["host"] = ev["host"]
                if ev["tool"]:
                    cur["tool"] = ev["tool"]
                if ev["idx"]:
                    cur["idx"] = ev["idx"]
                if ev["turnId"]:
                    cur["turnId"] = ev["turnId"]

                prev = cur.get("state", "idle")
                state = prev

                if ev["event"] == "session_started":
                    if new_session:
                        state = "idle"
                        cur["turnId"] = ""
                        cur["seq"] = None
                        _drop_attention_locked(key)
                        cur["completedTurns"].clear()
                        cur["msg"] = ev["message"] or "已连接"
                elif ev["event"] == "user_message_submitted":
                    _drop_attention_locked(key)
                    cur["seq"] = None
                    state = "busy"
                    cur["msg"] = ev["message"] or "开始处理"
                elif ev["event"] == "processing_resumed":
                    resolved = _resolve_attention_locked(key, ev["requestId"])
                    if prev in ("wait", "ask") and _pending_of(cur):
                        # 还有别的授权/补充没处理完，别提前退出等待态
                        state = prev
                    elif resolved or prev not in ("wait", "ask"):
                        state = "busy"
                        cur["msg"] = ev["message"] or "执行中"
                elif ev["event"] == "approval_requested":
                    _add_attention_locked(cur, ev["event"], ev["requestId"],
                                          ev["turnId"], ev["occurredAt"])
                    state = "wait"
                    cur["msg"] = ev["message"] or (
                        "等待授权：" + ev["tool"] if ev["tool"] else "等待授权")
                elif ev["event"] == "supplement_requested":
                    _add_attention_locked(cur, ev["event"], ev["requestId"],
                                          ev["turnId"], ev["occurredAt"])
                    state = "ask"
                    cur["msg"] = ev["message"] or "等待补充"
                elif ev["event"] == "turn_completed":
                    _drop_attention_locked(key)
                    failed = ev["outcome"] in ("failed", "failure", "error", "errored")
                    state = "error" if failed else "done"
                    cur["msg"] = ev["message"] or ("失败" if failed else "完成")
                    _mark_completed_locked(cur, ev["turnId"], now)

                cur["state"] = state
                cur["at"] = now
                cur["eventAt"] = max(int(cur.get("eventAt") or 0),
                                     ev["occurredAt"] or now_ms)
                if ev["sequence"] is not None:
                    cur["seq"] = ev["sequence"]
                result = {"ok": True, "event": ev["event"], "sessionId": key,
                          "state": state, "pending": int(cur.get("pending") or 0)}
                need_broadcast = True

            snap = _snapshot_locked(now)

    if need_broadcast:
        broadcast(snap)
        print("[companion] %-22s %-5s %s %s" % (
            ev["event"], result.get("state", "-"), key, ev["message"][:60]), flush=True)
    return result, 200


def clear_sessions(d):
    if not isinstance(d, dict):
        return {"ok": False, "why": "body must be a JSON object"}, 400
    sid = _txt(d.get("sessionId") or d.get("session_id") or d.get("key"), 120)
    clear_all = d.get("all") in (True, 1, "1", "true", "True")
    if not sid and not clear_all:
        return {"ok": False, "why": "need sessionId or all"}, 400
    now = time.time()
    with _lock:
        if clear_all:
            keys = list(_sessions)
        else:
            keys = [k for k in _sessions
                    if k == sid or _sessions[k].get("sessionId") == sid]
        removed = 0
        for k in keys:
            removed += _remove_session_locked(k)
        snap = _snapshot_locked(now)
    if removed:
        broadcast(snap)
    return {"ok": True, "removed": removed}, 200


class Base(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code="-", size="-"):
        # 最近 40 条请求路径留个环形日志，排查"页面到底有没有在跑"这类问题用
        try:
            _recent.append("%s %s %s %s" % (time.strftime("%H:%M:%S"), self.command,
                                          self.path.split("?")[0], code))
            del _recent[:-40]
        except Exception:
            pass

    def end_headers(self):
        # 页面绝不能缓存。控制台是长期挂在托盘里的窗口，一旦缓存住旧版本，
        # 改了代码它也不更新 —— 排查起来极其费劲（真踩过）。
        if not getattr(self, "_nocache_sent", False):
            self._nocache_sent = True
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            pass
        # 兜底：编码异常时也别丢状态，从原始字节里把关键字段抠出来
        txt = raw.decode("utf-8", "replace")
        try:
            return json.loads(txt)
        except Exception:
            out = {}
            for field in ("hook_event_name", "tool_name", "cwd", "session_id"):
                m = re.search(r'"%s"\s*:\s*"([^"]*)"' % field, txt)
                if m:
                    out[field] = m.group(1)
            return out or {"_raw": txt.strip()[:200]}

    # ---- 公共路由 ----
    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/hook":
            return self._json(self.on_hook())
        if p == "/events":                      # Coding 伴侣标准事件
            out, code = apply_event(self._body())
            return self._json(out, code)
        if p == "/sessions/clear":              # 主动清理会话
            out, code = clear_sessions(self._body())
            return self._json(out, code)
        if p == "/state":                       # 手动/测试上报
            d = self._body()
            record(d.get("key") or d.get("tmux"), d.get("state", "idle"),
                   agent=d.get("agent", ""), tool=d.get("tool", ""),
                   cwd=d.get("cwd", ""), msg=d.get("msg", ""),
                   name=d.get("name", ""), project=d.get("project", ""),
                   host=d.get("host", ""), turnId=d.get("turnId", ""),
                   idx=d.get("idx", ""))
            return self._json({"ok": True})
        if p == "/status":                      # statusLine 桥接：官方 rate_limits
            d = self._body()
            src = self.headers.get("X-Src") or d.get("src") or "local"
            st = d.get("status") if isinstance(d.get("status"), dict) else d
            with _lock:
                _status[src] = {"at": time.time(), "data": st}
            rl = (st.get("rate_limits") or {})
            f = (rl.get("five_hour") or {}).get("used_percentage")
            print("[status %-8s] 5h=%s%%" % (src, round(f,1) if isinstance(f,(int,float)) else "-"), flush=True)
            return self._json({})
        if p.startswith("/kb/"):
            return self._json(kb_api(p, self._body() or {}))
        if p == "/open/console":                # 胶囊里点一下，把控制台窗口叫出来
            broadcast({"open": "console"})
            _want["console"] = time.time()
            return self._json({"ok": True})
        if p == "/hud/shown":                   # 宿主：胶囊放下来了，页面重放进场动画
            broadcast({"shown": True, "at": time.time()})
            return self._json({"ok": True})
        if p == "/hud/pop":                     # 页面请求"把胶囊弹下来"
            why = (self._body() or {}).get("why", "")
            broadcast({"pop": str(why)[:120], "at": time.time()})
            with _lock:
                _want["pop"] = str(why)[:120]   # 托盘进程轮询 /want 时真去弹
            print("[pop] %s" % why, flush=True)
            return self._json({"ok": True})
        if p == "/settings":                    # 总开关：Claude 模式 / 远端主机
            d = self._body() or {}
            with _lock:
                if "claude" in d:
                    _settings["claude"] = bool(d["claude"])
                if "ssh_host" in d:
                    _settings["ssh_host"] = str(d["ssh_host"])[:64].strip()
                save_settings()
                out = dict(_settings)
            if out["claude"]:
                start_claude_services()
            else:
                stop_claude_services()
            print("[模式] Claude 模式：%s" % ("开" if out["claude"] else "关"), flush=True)
            return self._json(out)
        if p == "/capsule":                     # 控制台里调胶囊：皮肤 / 动画 / 宠物
            d = self._body() or {}
            with _lock:
                for k in ("skin", "anim", "pet", "hold"):
                    if k in d:
                        _cap[k] = str(d[k])[:32]
                try:
                    json.dump(_cap, open(CAP_FILE, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=1)
                except Exception:
                    pass
                out = dict(_cap)
            print("[胶囊] %s" % out, flush=True)
            return self._json(out)
        if p == "/token":                       # 粘贴 claude setup-token 给的那串
            tok = (self._body() or {}).get("token", "").strip()
            if len(tok) < 20:
                return self._json({"ok": False, "why": "太短了，不像 token"}, 400)
            if not tok.startswith("sk-ant-"):
                # 不拦，但要说清楚 —— setup-token 的输出是 sk-ant-oat01- 开头的长串
                print("[额度] 警告：这串不是 sk-ant- 开头，可能不是 setup-token 的输出",
                      flush=True)
            try:
                with open(paths.data(".token"), "w", encoding="utf-8") as f:
                    f.write(tok)
            except Exception as e:
                return self._json({"ok": False, "why": str(e)}, 500)
            try:
                import quota
                good, why, wins = quota.verify(tok)
            except Exception as e:
                good, why, wins = False, str(e), []
            ok = start_quota() if good else False
            print("[额度] 收到 token：%s %s" % (why, wins), flush=True)
            return self._json({"ok": True, "valid": good, "why": why,
                               "windows": wins, "polling": ok})
        if p == "/hud/geom":                    # 旧版胶囊用的，留着不碍事
            with _lock:
                _geom.update(self._body() or {})
            return self._json({"ok": True})
        if p == "/log":                         # 页面报错
            d = self._body() or {}
            print("[页面错误] %s: %s  @%s:%s" % (d.get("where"), d.get("msg"),
                  d.get("line"), d.get("col")), flush=True)
            return self._json({"ok": True})
        if p == "/hud/keep":                    # 鼠标悬停在胶囊上
            _hud_keep["at"] = time.time()
            return self._json({"ok": True})
        if p == "/ui":                          # 控制台上报自身状态
            with _lock:
                _ui.update(self._body() or {})
            return self._json({"ok": True})
        if p == "/action":                      # 键盘扩展键
            d = self._body()
            broadcast({"action": d.get("action", "")})
            return self._json({"ok": True})
        return self._json({"error": "unknown"}, 404)

    def on_hook(self):
        """Claude Code hook 回调。返回 {} —— 不携带任何决策，绝不介入权限流程。"""
        d = self._body()
        ev = d.get("hook_event_name", "")
        state = EVENT_STATE.get(ev)
        if state:
            # 窗口名由 hook 脚本放在 X-Tmux 头里（tmux display-message -p '#S:#W'）
            key = self.headers.get("X-Tmux") or d.get("cwd", "").split("/")[-1] or d.get("session_id", "")[:8]
            record(key, state,
                   agent="claude-code",
                   idx=self.headers.get("X-Tmux-Index", ""),
                   tool=d.get("tool_name", ""),
                   cwd=d.get("cwd", ""),
                   msg=(d.get("last_assistant_message") or d.get("message") or "")[:200])
        return {}


def kb_api(path, d):
    """键盘由托盘进程直接持有（kbd.py，Windows HID API），页面只是遥控器。
    这里把页面原来在 WebHID 上做的几件事一一对应过来。"""
    try:
        import kbd
    except Exception as e:
        return {"ok": False, "why": "kbd 模块没起来：%s" % e}
    kb = kbd.KB
    try:
        if path == "/kb/status":
            if not kb.connected():
                kb.open()                 # 懒打开：没人发命令时也得有人去接设备
            return dict(kb.status(), ok=True)
        if path == "/kb/reconnect":
            kb.close()
            kb.last_open_try = 0
            ok = kb.open()
            return dict(kb.status(), ok=ok, why=None if ok else kb.err)
        if path == "/kb/readcfg":
            cfg = kb.read_config()
            return {"ok": True, "awake": True, "cfg": list(cfg)}
        if path == "/kb/config":
            patch = {int(k): int(v) for k, v in (d.get("bytes") or {}).items()}
            cfg = kb.patch_config(patch)
            if 2 in patch and patch[2] != 254:   # 背光交回固件：推流那帧灯作废
                kb.forget_keys()
            return {"ok": True, "awake": True, "cfg": list(cfg)}
        if path == "/kb/dotmode":
            cfg = kb.set_dot_mode(
                int(d.get("mode", 6)),
                brightness=d.get("brightness"),
                speed=d.get("speed"),
                colorful=d.get("colorful"),
                color_index=d.get("colorIndex", d.get("color_index")),
                color=d.get("color"),
                sync=bool(d.get("sync", True)))
            return {"ok": True, "awake": True, "cfg": list(cfg), "dot": kb.dot_config()}
        if path == "/kb/lightbuf":
            if "colors" in d:
                ok = kb.write_custom_led(d.get("colors") or [])
                return {"ok": ok, "awake": kb.awake, "via": kb.via.ok,
                        "why": None if ok else (kb.via.err or "VIA 写不进去")}
            colors = kb.read_custom_led()
            return {"ok": colors is not None, "colors": colors, "via": kb.via.ok,
                    "why": None if colors is not None else (kb.via.err or "VIA 读不到")}
        if path == "/kb/anim":
            ok = kb.play_frames(d.get("frames") or [], d.get("fps", 6),
                                d.get("loop", True))
            return {"ok": ok, "anim": bool(kb._anim)}
        if path == "/kb/anim/stop":
            kb.stop_frames()
            return {"ok": True}
        if path == "/kb/matrix":
            kb.push_matrix(bytes(int(v) & 255 for v in (d.get("rgb") or [])),
                           force=bool(d.get("force")))
            return {"ok": True, "awake": kb.awake}
        if path == "/kb/keys":
            kb.push_keys(bytes(int(v) & 255 for v in (d.get("rgb565") or [])),
                         force=bool(d.get("force")))
            return {"ok": True, "awake": kb.awake}
        if path == "/kb/stopkeys":
            kb.stop_keys()
            return {"ok": True}
        if path == "/kb/power":
            pw = kb.power(force=bool(d.get("force")))
            if not pw:
                return {"ok": False, "awake": kb.awake, "why": "读不到电量"}
            return dict(pw, ok=True, awake=True)
        return {"ok": False, "why": "unknown"}
    except TimeoutError as e:
        return {"ok": False, "awake": False, "why": str(e)}
    except Exception as e:
        return {"ok": False, "awake": kb.awake, "why": str(e)[:160],
                "connected": kb.connected()}


class UI(Base):
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/kb/status", "/kb/power", "/kb/lightbuf"):
            return self._json(kb_api(p, {}))
        if p == "/debug":
            with _lock:
                return self._json({"sse_subs": len(_subs), "recent": list(_recent)})
        if p == "/events":
            return self._sse()
        if p == "/snapshot":
            return self._json(snapshot())
        if p == "/jump":                        # 热键问：现在该去哪个窗口
            return self._json(snapshot().get("top") or {})
        if p == "/hud/keep":
            return self._json({"at": _hud_keep["at"]})
        if p == "/hud/geom":
            with _lock:
                return self._json(dict(_geom))
        if p == "/capsule":
            with _lock:
                return self._json(dict(_cap))
        if p == "/settings":
            with _lock:
                return self._json(dict(_settings))
        if p == "/want":                        # 托盘程序轮询：有没有要我做的事
            with _lock:
                out = dict(_want)
                _want.clear()
            return self._json(out)
        if p == "/ui":
            with _lock:
                return self._json(dict(_ui))
        if p == "/usage":
            # 只报官方 rate_limits。本地 transcript 估算已经去掉了 —— 那是猜的，
            # 跟设置里看到的数对不上，不如空着诚实。
            out = {"official": {}}
            with _lock:
                for k, v in _status.items():
                    st = v["data"]
                    out["official"][k] = {
                        "at": v["at"],
                        "rate_limits": st.get("rate_limits") or {},
                        "model": (st.get("model") if isinstance(st.get("model"), str)
                                  else (st.get("model") or {}).get("display_name")),
                        "session": st.get("session_name") or st.get("session_id", "")[:8],
                        "cwd": st.get("cwd", ""),
                    }
            return self._json(out)
        return super().do_GET()

    def _sse(self):
        q = queue.Queue(maxsize=64)
        with _lock:
            _subs.append(q)
        first = json.dumps(snapshot(), ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(("data: %s\n\n" % first).encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    chunk = "data: %s\n\n" % q.get(timeout=15)
                except queue.Empty:
                    chunk = ": ka\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _lock:
                if q in _subs:
                    _subs.remove(q)


class Hook(Base):
    """本机 statusLine 用的精简端口（顺带给以后可能的反向隧道留着）。"""
    def do_GET(self):
        return self._json({"ok": True})


# ── 服务器状态：SSH 拉流 ──────────────────────────────────────────────
# 不用反向隧道。服务器上的 hook 只往 ~/.claude/yogo/events.jsonl 追加一行，
# 这边一条常驻 `ssh … tail -F` 把它读回来。好处：
#   · Termius 开不开都无所谓，也不用配端口转发
#   · SSH 掉线自己重连，不会留下半死的隧道
#   · 服务器那边不监听任何端口

# 从无控制台的父进程（pythonw）起控制台程序，Windows 会给它开一个黑框。
# 所有子进程一律带上这组参数，开机就不会弹出一堆 cmd 窗口。
def quiet():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                           # SW_HIDE
    return dict(creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=si,
                stdin=subprocess.DEVNULL)


SSH_HOST = os.environ.get("YOGO_SSH_HOST") or str(_settings.get("ssh_host") or "")
REMOTE_LOG = "~/.claude/yogo/events.jsonl"


_ssh_proc = [None]      # 当前那条 ssh；关 Claude 模式时直接掐断


def stop_claude_services():
    """关 Claude 模式：掐断 ssh。额度轮询自己看 GATE 会停。"""
    p = _ssh_proc[0]
    if p and p.poll() is None:
        try:
            p.terminate()
            print("[ssh] Claude 模式关了，断开", flush=True)
        except Exception:
            pass


def ssh_tail():
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
           "-o", "StrictHostKeyChecking=accept-new", SSH_HOST,
           "mkdir -p ~/.claude/yogo && touch %s && exec tail -n 200 -F %s"
           % (REMOTE_LOG, REMOTE_LOG)]
    backoff = 3
    while True:
        if not claude_on():                  # Claude 模式关着：不连服务器
            time.sleep(3)
            continue
        try:
            print("[ssh] 连 %s 拉事件流…" % SSH_HOST, flush=True)
            proc = _ssh_proc[0] = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=1,
                                    text=True, encoding="utf-8", errors="replace",
                                    **quiet())
            backoff = 3
            for line in proc.stdout:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    on_remote_event(json.loads(line))
                except Exception:
                    pass
            proc.wait()
        except Exception as e:
            print("[ssh] 断了：%s" % e, flush=True)
        time.sleep(backoff)
        backoff = min(60, backoff * 2)


_seen = set()          # (key, t) 去重：重连后 tail 会把最近 200 行再吐一遍
STALE_EVENT = 900      # 回看到的旧事件只用来补额度，不拿来当"刚刚完成"


def on_remote_event(row):
    key = row.get("key") or "server"
    sig = (key, row.get("t"), row.get("ev"))
    if sig in _seen:
        return
    _seen.add(sig)
    if len(_seen) > 4000:
        _seen.clear()
    if row.get("ev") == "Status":
        st = dict(row.get("status") or {})
        # 远端脚本里 model 已经取成了字符串，本机 statusLine 传的是整个对象。统一。
        if isinstance(st.get("model"), str):
            st["model"] = {"display_name": st["model"]}
        with _lock:
            _status[key] = {"at": time.time(), "data": st}
        return
    if time.time() - (row.get("t") or 0) > STALE_EVENT:
        return
    state = EVENT_STATE.get(row.get("ev", ""))
    if state:
        record(key, state, agent="claude-code", idx=row.get("idx", ""),
               tool=row.get("tool", ""), cwd=row.get("cwd", ""), msg=row.get("msg", ""))


def serve(port, handler, name):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    print("[%s] http://127.0.0.1:%d" % (name, port), flush=True)
    srv.serve_forever()


_quota_on = [False]


def start_quota():
    """有 token 就起额度轮询。粘完 token 不用重启软件。"""
    if _quota_on[0]:
        return True
    try:
        import quota
        if not quota.token():
            return False
        quota.GATE = claude_on                # 模式关了就暂停，不再问 Anthropic
        threading.Thread(target=quota.main, daemon=True).start()
        _quota_on[0] = True
        return True
    except Exception as e:
        print("[额度] 轮询没起来：%s" % e, flush=True)
        return False


_claude_started = False


def start_claude_services():
    """跟 Claude 有关的后台：拉服务器事件流、问额度。只在 Claude 模式下起，起过就不重复。"""
    global _claude_started, SSH_HOST
    if _claude_started or not claude_on():
        return
    _claude_started = True
    SSH_HOST = os.environ.get("YOGO_SSH_HOST") or str(_settings.get("ssh_host") or "")
    if SSH_HOST:
        threading.Thread(target=ssh_tail, daemon=True).start()
    else:
        print("[ssh] 没配远端主机，只看本机的 Claude Code", flush=True)
    if start_quota():
        print("[额度] 直连账号轮询已启动", flush=True)
    else:
        print("[额度] 没有 token，只靠 statusLine 上报"
              "（跑 claude setup-token，把那串粘进控制台就行）", flush=True)


def start():
    """把所有后台线程拉起来，不阻塞 —— 给 yogo.pyw（托盘程序）调用。"""
    threading.Thread(target=serve, args=(HOOK_PORT, Hook, "hook"), daemon=True).start()
    threading.Thread(target=serve, args=(PORT, UI, "ui"), daemon=True).start()
    if claude_on():
        start_claude_services()
    else:
        print("[模式] Claude 模式关着：纯键盘灯效工具", flush=True)


if __name__ == "__main__":
    start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.exit(0)
