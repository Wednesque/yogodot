#!/usr/bin/env python3
"""YogoDot Coding 伴侣本地适配器。

把 Claude Code / Codex / TRAE 的原生 hook JSON 翻译成 YogoDot 标准事件，
POST 到本机服务（默认 http://127.0.0.1:8787/events）。

用法：
    python companion-hook.py --source claude
    python companion-hook.py --source codex --hook-event-name UserPromptSubmit
    python companion-hook.py --source trae
    python companion-hook.py --source claude --dry-run   # 只打印标准事件

隐私：只提取白名单元数据。prompt / tool_input / tool_response /
last_assistant_message / message / additionalContext 等正文一律不提取、不发送。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager

MAX_INPUT = 8 * 1024 * 1024
MAX_MESSAGE = 200
MAX_TOOL = 80
MAX_CWD = 300
MAX_PROJECT = 80
SOURCE_AGENT = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claudecode": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
    "trae": "trae",
}


def txt(v, n):
    if isinstance(v, str):
        return v.strip()[:n]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)[:n]
    return ""


def first(*values):
    for v in values:
        s = txt(v, 4096)
        if s:
            return s
    return ""


def norm_name(v):
    return re.sub(r"[^a-z0-9]", "", txt(v, 120).lower())


def canonical_source(v):
    s = norm_name(v)
    if s in ("claudecode", "claude"):
        return "claude"
    if s in ("codex", "openaicodex"):
        return "codex"
    if s in ("opencode",):
        return "opencode"
    if s in ("trae", "traecn"):
        return "trae"
    return "claude"


def read_payload():
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(MAX_INPUT + 1)
    except Exception:
        return {}
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not raw or len(raw) > MAX_INPUT:
        return {}
    text = raw.decode("utf-8-sig", "replace").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def state_dir(arg):
    d = txt(arg, 400) or txt(os.environ.get("YOGO_STATE_DIR"), 400)
    if not d:
        # 固定放在用户目录，避免 Codex 受限 Hook 环境偶尔没有 LOCALAPPDATA
        # 导致同一会话在两个目录之间分裂。
        home = os.path.expanduser("~")
        if home == "~":
            home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.getcwd()
        d = os.path.join(home, ".yogo", "companion")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def state_path(folder, source, sid):
    h = hashlib.sha1((source + "\0" + sid).encode("utf-8")).hexdigest()[:24]
    return os.path.join(folder, h + ".json")


@contextmanager
def state_lock(path):
    """跨进程小锁；拿不到就继续跑，绝不卡住工具。"""
    lock = path + ".lock"
    fd = None
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 5:
                    os.unlink(lock)
                    continue
            except OSError:
                pass
            time.sleep(0.01)
        except OSError:
            break
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(lock)
            except OSError:
                pass


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(path, state):
    state["version"] = 1
    state["updated"] = int(time.time())
    tmp = path + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def cwd_of(payload):
    cwd = first(payload.get("cwd"), payload.get("workspace"),
                payload.get("working_directory"), payload.get("workingDirectory"))
    if not cwd:
        roots = payload.get("workspace_roots")
        if isinstance(roots, list) and roots:
            cwd = first(roots[0])
    return cwd[:MAX_CWD]


def project_of(payload, cwd):
    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        for root in roots:
            s = txt(root, MAX_CWD)
            if s:
                return os.path.basename(s.rstrip("/\\"))[:MAX_PROJECT]
    if cwd:
        return os.path.basename(cwd.rstrip("/\\"))[:MAX_PROJECT]
    return ""


def session_id_of(payload, source):
    raw = first(payload.get("session_id"), payload.get("sessionId"),
                payload.get("thread_id"), payload.get("threadId"),
                payload.get("conversation_id"), payload.get("conversationId"),
                payload.get("session"))
    if not raw:
        cwd = cwd_of(payload)
        raw = os.path.basename(cwd.rstrip("/\\")) or "default"
    sid = "%s:%s" % (source, raw)
    if len(sid) > 120:
        sid = "%s:%s" % (source, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20])
    return sid


def native_turn_id(payload):
    return first(payload.get("turn_id"), payload.get("turnId"),
                 payload.get("turnID"), payload.get("thread_turn_id"))[:80]


def tool_of(payload):
    return first(payload.get("tool_name"), payload.get("toolName"),
                 payload.get("llm_tool_name"), payload.get("llmToolName"),
                 payload.get("tool"), payload.get("operation"))[:MAX_TOOL]


def request_id_of(payload):
    for key in ("tool_use_id", "toolUseId", "toolUseID", "tool_useId",
                "permission_id", "permissionId", "approval_id", "approvalId",
                "question_id", "questionId", "request_id", "requestId",
                "call_id", "callId", "callID"):
        v = txt(payload.get(key), 120)
        if v:
            return v
    for key in ("permission", "approval", "question", "request", "input", "info"):
        obj = payload.get(key)
        if not isinstance(obj, dict):
            continue
        for sub in ("requestId", "request_id", "permissionId", "permission_id",
                    "approvalId", "approval_id", "questionId", "question_id",
                    "toolUseId", "tool_use_id", "callId", "call_id", "id"):
            v = txt(obj.get(sub), 120)
            if v:
                return v
    return ""


def notification_type(payload):
    return first(payload.get("notification_type"), payload.get("notificationType"),
                 payload.get("notification"), payload.get("type"))


def native_event_name(payload, arg):
    # payload 里的 hook_event_name 最可信；其次才是模板显式传的事件名，
    # 最后才退回泛化的 event/type（Codex 的 type 不一定是 hook 名）。
    return first(payload.get("hook_event_name"), payload.get("hookEventName"),
                 arg, payload.get("event"), payload.get("type"))


def map_notification(payload):
    n = norm_name(notification_type(payload))
    if n in ("permissionprompt", "permissionrequest", "permissionrequested",
             "approvalrequest", "approvalrequested"):
        return "approval_requested"
    if n in ("elicitationdialog", "question", "askuserquestion", "documentreview",
             "browserinteraction", "supplement", "supplementrequested"):
        return "supplement_requested"
    if n in ("idleprompt", "completed", "complete", "done"):
        return "turn_completed"
    return ""


def map_event(native):
    n = norm_name(native)
    if n in ("sessionstart",):
        return "session_started"
    if n in ("sessionend",):
        return "session_ended"
    if n in ("userpromptsubmit", "userpromptsubmitted",
             "usermessagesubmit", "usermessagesubmitted"):
        return "user_message_submitted"
    if n in ("permissionrequest", "permissionrequested", "approvalrequest",
             "approvalrequested", "directoryapprovalrequest",
             "workspaceapprovalrequest"):
        return "approval_requested"
    if n in ("permissiondenied", "approvaldenied"):
        return "processing_resumed"
    if n in ("permissionresolved", "permissionapproved", "approvalresolved",
             "approvalapproved", "processingresumed"):
        return "processing_resumed"
    if n in ("pretooluse", "toolusestarted"):
        return "processing_resumed"
    if n in ("posttooluse", "posttoolusefailure", "toolusecompleted"):
        return "processing_resumed"
    if n in ("stop", "turncomplete", "turncompleted"):
        return "turn_completed"
    if n in ("stopfailure", "turnfailed"):
        return "turn_completed_failed"
    if n in ("subagentstart", "subagentstop", "precompact", "postcompact"):
        return ""
    return ""


def map_payload_event(payload, arg):
    native = native_event_name(payload, arg)
    n = norm_name(native)
    if n == "notification":
        return native, map_notification(payload)
    return native, map_event(native)


def message_for(mapped, native, tool):
    n = norm_name(native)
    if mapped == "session_started":
        return "已连接"
    if mapped == "session_ended":
        return ""
    if mapped == "user_message_submitted":
        return "开始处理"
    if mapped == "processing_resumed":
        if n in ("permissiondenied", "approvaldenied"):
            return "授权被拒绝，继续处理"
        if n in ("permissionresolved", "permissionapproved",
                 "approvalresolved", "approvalapproved", "processingresumed"):
            return "授权已处理，继续执行"
        if n in ("posttoolusefailure",):
            return ("工具失败：" + tool) if tool else "工具失败"
        if n in ("posttooluse", "toolusecompleted"):
            return ("工具完成：" + tool) if tool else "继续执行"
        return ("执行：" + tool) if tool else "执行中"
    if mapped == "approval_requested":
        return ("等待授权：" + tool) if tool else "等待授权"
    if mapped == "supplement_requested":
        return "等待补充"
    if mapped in ("turn_completed", "turn_completed_failed"):
        return "失败" if mapped == "turn_completed_failed" else "完成"
    return ""


def new_turn_id(sid, counter):
    return ("%s:%d" % (sid[:40], counter))[:80]


def translate(source, native, mapped, payload, state, sid, args):
    """返回 (标准事件 body, 是否删除状态)。"""
    agent = SOURCE_AGENT.get(source, "claude-code")
    tool = tool_of(payload)
    cwd = cwd_of(payload)
    project = project_of(payload, cwd)
    native_turn = native_turn_id(payload)
    n = norm_name(native)

    if not state.get("sessionId"):
        state["sessionId"] = sid
    state.setdefault("turnId", "")
    state.setdefault("counter", 0)
    state.setdefault("seq", 0)
    state.setdefault("pendingApproval", "")
    state.setdefault("pendingSupplement", "")
    seq = int(state.get("seq") or 0) + 1
    state["seq"] = seq

    delete = False
    if mapped == "session_started":
        turn_id = native_turn or state.get("turnId") or ""
        state["pendingApproval"] = ""
        state["pendingSupplement"] = ""
        message = "已连接"
    elif mapped == "session_ended":
        turn_id = native_turn or state.get("turnId") or ""
        message = ""
        delete = True
    elif mapped == "user_message_submitted":
        if native_turn:
            turn_id = native_turn
        else:
            state["counter"] = int(state.get("counter") or 0) + 1
            turn_id = new_turn_id(sid, state["counter"])
        state["turnId"] = turn_id
        state["pendingApproval"] = ""
        state["pendingSupplement"] = ""
        message = "开始处理"
    else:
        turn_id = native_turn or state.get("turnId") or ""
        if not turn_id:
            state["counter"] = int(state.get("counter") or 0) + 1
            turn_id = new_turn_id(sid, state["counter"])
            state["turnId"] = turn_id
        message = message_for(mapped, native, tool)

    event = "turn_completed" if mapped == "turn_completed_failed" else mapped
    outcome = "failed" if mapped == "turn_completed_failed" else ""

    body = {
        "schema": "yogo.companion.v1",
        "event": event,
        "sessionId": sid,
        "turnId": turn_id,
        "eventId": uuid.uuid4().hex,
        "agent": agent,
        "occurredAt": int(time.time() * 1000),
        "sequence": seq,
    }
    data = {}
    if message:
        data["message"] = message[:MAX_MESSAGE]
    if tool:
        data["tool"] = tool
    if cwd:
        data["cwd"] = cwd
    if project:
        data["project"] = project
        data["name"] = project
    host = txt(os.environ.get("YOGO_COMPANION_HOST"), 80)
    if host:
        data["host"] = host
    if data:
        body["data"] = data
    if outcome:
        body["outcome"] = outcome

    rid = request_id_of(payload)
    if mapped == "approval_requested":
        state["pendingApproval"] = rid
        body["interaction"] = {"kind": "authorization", "requestId": rid, "tool": tool}
    elif mapped == "supplement_requested":
        state["pendingSupplement"] = rid
        body["interaction"] = {"kind": "supplement", "requestId": rid, "tool": tool}
    elif mapped == "processing_resumed":
        pending_approval = state.get("pendingApproval") or ""
        pending_supplement = state.get("pendingSupplement") or ""
        if rid:
            use = rid
            if rid == pending_approval:
                state["pendingApproval"] = ""
            elif rid == pending_supplement:
                state["pendingSupplement"] = ""
        elif pending_approval:
            use = pending_approval
            state["pendingApproval"] = ""
        elif pending_supplement:
            use = pending_supplement
            state["pendingSupplement"] = ""
        else:
            use = ""
        if use:
            body["interaction"] = {"requestId": use, "tool": tool}
    elif mapped in ("turn_completed", "turn_completed_failed", "session_ended"):
        state["pendingApproval"] = ""
        state["pendingSupplement"] = ""

    return body, delete


def loopback_url(arg):
    url = txt(arg, 400) or txt(os.environ.get("YOGO_COMPANION_URL"), 400)
    if not url:
        port = txt(os.environ.get("YOGO_PORT"), 12) or "8787"
        url = "http://127.0.0.1:%s/events" % port
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost", "::1"):
            return ""
    except Exception:
        return ""
    return url


def post_event(url, body):
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST",
                                 headers={"Content-Type": "application/json"})
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=0.35) as r:
                r.read()
            return True
        except urllib.error.HTTPError:
            return False
        except Exception:
            if attempt == 0:
                time.sleep(0.1)
    return False


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--source", default="claude")
    p.add_argument("--hook-event-name", default="")
    p.add_argument("--url", default="")
    p.add_argument("--state-dir", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--help", action="help")
    try:
        args = p.parse_args()
    except SystemExit:
        return 0
    payload = read_payload()
    source = canonical_source(args.source)
    native, mapped = map_payload_event(payload, args.hook_event_name)
    if not mapped:
        return 0
    sid = session_id_of(payload, source)
    folder = state_dir(args.state_dir)
    path = state_path(folder, source, sid)
    with state_lock(path):
        state = load_state(path)
        body, delete = translate(source, native, mapped, payload, state, sid, args)
        if args.dry_run:
            print(json.dumps(body, ensure_ascii=False, indent=1))
            return 0
        url = loopback_url(args.url)
        if url:
            post_event(url, body)
        if delete:
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            save_state(path, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # hook 绝不能阻断工具
        sys.exit(0)
