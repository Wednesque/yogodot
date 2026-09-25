#!/usr/bin/env python3
"""
YOGO 控制台 —— 本地服务

  1. 托管 yogo.html（固定来源，WebHID 授权只需一次）
  2. 拉服务器上 Claude Code 的状态，**按 tmux 窗口分别跟踪**
  3. SSE 推给页面（GET /events）
  4. 接收本地热键动作（POST /action），并回答"该跳去哪个窗口"（GET /jump）

服务器状态不走反向隧道：那边的 hook 只往 ~/.claude/yogo/events.jsonl 追加一行，
这边常驻一条 `ssh <你的服务器> tail -F` 把流拉回来（见 ssh_tail）。
换服务器就改环境变量 YOGO_SSH_HOST。

用法：  py -3 server.py
"""
import json
import os

import paths
import queue
import re
import subprocess
import sys
import threading
import time
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
PRIORITY = {"wait": 4, "error": 3, "busy": 2, "done": 1, "idle": 0}
IDLE_AFTER = 15 * 60      # 15 分钟没动静就当空闲
DROP_AFTER = 2 * 3600     # 2 小时没动静就从列表里去掉，免得攒一堆死会话

_lock = threading.Lock()
_subs = []
_sessions = {}            # key(tmux 窗口名或 session_id) -> dict
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


def snapshot():
    with _lock:
        rows = []
        now = time.time()
        for k, v in sorted(_sessions.items()):
            if now - v["at"] > DROP_AFTER:
                continue
            s = dict(v)
            if s["state"] != "idle" and now - s["at"] > IDLE_AFTER:
                s["state"] = "idle"
            rows.append(s)
        top = max(rows, key=lambda r: (PRIORITY.get(r["state"], 0), r["at"]), default=None)
        return {"sessions": rows, "top": top}


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
    key = (key or "claude")[:60]
    with _lock:
        cur = _sessions.setdefault(key, {"key": key, "state": "idle", "tool": "", "cwd": "",
                                         "msg": "", "idx": "", "at": 0.0, "prev": "idle"})
        cur["prev"] = cur["state"]
        cur["state"] = state
        cur["at"] = time.time()
        for k, v in extra.items():
            if v:
                cur[k] = str(v)[:200]
    snap = snapshot()
    broadcast(snap)
    print("[%-10s] %-5s  %s" % (key, state, extra.get("tool", "")), flush=True)
    return snap


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
        if p == "/state":                       # 手动/测试上报
            d = self._body()
            record(d.get("key") or d.get("tmux"), d.get("state", "idle"),
                   tool=d.get("tool", ""), cwd=d.get("cwd", ""), msg=d.get("msg", ""))
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
        if path == "/kb/matrix":
            kb.push_matrix(bytes(int(v) & 255 for v in (d.get("rgb") or [])))
            return {"ok": True, "awake": kb.awake}
        if path == "/kb/keys":
            kb.push_keys(bytes(int(v) & 255 for v in (d.get("rgb565") or [])))
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
        if p in ("/kb/status", "/kb/power"):
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


def ssh_tail():
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
           "-o", "StrictHostKeyChecking=accept-new", SSH_HOST,
           "mkdir -p ~/.claude/yogo && touch %s && exec tail -n 200 -F %s"
           % (REMOTE_LOG, REMOTE_LOG)]
    backoff = 3
    while True:
        try:
            print("[ssh] 连 %s 拉事件流…" % SSH_HOST, flush=True)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
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
        record(key, state, idx=row.get("idx", ""), tool=row.get("tool", ""),
               cwd=row.get("cwd", ""), msg=row.get("msg", ""))


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
