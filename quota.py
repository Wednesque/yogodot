#!/usr/bin/env python3
"""
直接读账号额度 —— 不用等 Claude 的 statusLine 上报。

Claude Code 自己的 /usage 面板走的就是这个端点：
    GET https://api.anthropic.com/api/oauth/usage
        Authorization: Bearer <本机已登录的 OAuth token>
        anthropic-beta: oauth-2025-04-20
返回 five_hour / seven_day 的使用率，就是设置里看到的那两个数。

几点说明：
  · token 只在本进程里用，从本机凭据里读出来后既不打印也不落盘
  · 这个端点限流很凶（claude-code#31021）。180 秒一次是社区验证过安全的节奏，
    并且 User-Agent 必须像 claude-cli，否则立刻 429
  · 拿到就 POST 给本地 server.py，胶囊那边照常显示

用法：
    py -3 quota.py --probe     看一次返回结构（只打印字段名和百分比）
    py -3 quota.py             常驻，每 180 秒刷一次
"""
import json
import os

import paths
import sys
import time
import urllib.error
import urllib.request

URL = "https://api.anthropic.com/api/oauth/usage"
UA = "claude-cli/2.1.260 (external, cli)"
SINK = "http://127.0.0.1:%s/status" % os.environ.get("YOGO_HOOK_PORT", "9099")
POLL = 180


# ── 找 token ────────────────────────────────────────────────────────
def _from_file():
    p = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    return _dig(d)


def _from_credman():
    """Windows 凭据管理器。Claude Code 在 Windows 上把 token 存这儿。"""
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
                    ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                    ("CredentialBlobSize", wt.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
                    ("UserName", wt.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wt.BOOL
    for target in ("Claude Code-credentials", "Claude Code", "claude-code-credentials"):
        ptr = ctypes.POINTER(CREDENTIAL)()
        if not adv.CredReadW(target, 1, 0, ctypes.byref(ptr)):
            continue
        try:
            raw = ctypes.string_at(ptr.contents.CredentialBlob,
                                   ptr.contents.CredentialBlobSize)
        finally:
            adv.CredFree(ptr)
        for enc in ("utf-8", "utf-16-le"):
            try:
                got = _dig(json.loads(raw.decode(enc).strip("\x00")))
                if got:
                    return got
            except Exception:
                pass
    return None


def _dig(o):
    """在任意嵌套里找 accessToken —— 各版本存的层级不一样。"""
    if isinstance(o, dict):
        for k in ("accessToken", "access_token"):
            v = o.get(k)
            if isinstance(v, str) and len(v) > 20:
                return v
        for v in o.values():
            got = _dig(v)
            if got:
                return got
    elif isinstance(o, list):
        for v in o:
            got = _dig(v)
            if got:
                return got
    return None


def _from_local():
    """`claude setup-token` 生成的长期 token，放在这两个地方任一个都行。
    这是官方给自动化用的入口 —— 不去撬桌面端那个加密存储。"""
    for p in (paths.data(".token"),
              os.path.expanduser("~/.yogo-token")):
        try:
            t = open(p, encoding="utf-8").read().strip()
            if len(t) > 20:
                return t
        except Exception:
            pass
    return None


def token():
    return (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or _from_local() or _from_file() or _from_credman())


# ── 取数 ────────────────────────────────────────────────────────────
def fetch(tok):
    req = urllib.request.Request(URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": UA,                 # 少了这个立刻 429
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def norm(d):
    """把返回体整成 statusLine 那个形状，胶囊那边就不用改了。

    端点的真实结构（Claude-Code-Usage-Monitor#202 里记录的）：
        five_hour / seven_day / seven_day_opus / seven_day_sonnet / extra_usage
        每个窗口 = {"utilization": 0~100, "resets_at": ISO8601}
    按模型拆分的周额度就在 seven_day_<模型> 里 —— 所以这里不写死键名，
    凡是长得像"用量窗口"的顶层字段一律收进来，将来多出 seven_day_fable 也照收。
    """
    out = {}
    for k, v in (d or {}).items():
        if not isinstance(v, dict):
            continue
        pct = _first(v, ("utilization", "used_percentage", "percent_used", "percentage"))
        if not isinstance(pct, (int, float)):
            continue
        if pct <= 1.0 and k != "five_hour":          # 有的字段给 0~1 的比例
            pct *= 100
        row = {"used_percentage": round(float(pct), 1)}
        rst = _iso(_first(v, ("resets_at", "resetsAt", "reset_at")))
        if rst:
            row["resets_at"] = rst
        out[k] = row
    return out


def _iso(v):
    """resets_at 可能是 ISO 字符串，也可能已经是秒。统一成秒。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            import datetime
            return int(datetime.datetime.fromisoformat(
                v.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def _find(o, key):
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            got = _find(v, key)
            if got is not None:
                return got
    elif isinstance(o, list):
        for v in o:
            got = _find(v, key)
            if got is not None:
                return got
    return None


def _first(d, keys):
    for k in keys:
        if k in d:
            return d[k]
    return None


def verify(tok=None):
    """拿 token 立刻问一次端点，返回 (通不通, 说明, 有哪几格窗口)。
    存 token 的时候当场验，比等三分钟后翻日志强得多。"""
    tok = tok or token()
    if not tok:
        return False, "没找到 token", []
    try:
        d = fetch(tok)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "账号不认这串（401）——多半不是 setup-token 输出的那条", []
        if e.code == 429:
            return True, "限流了（429），token 本身应该是好的，等几分钟再看", []
        return False, "HTTP %s %s" % (e.code, e.reason), []
    except Exception as e:
        return False, "连不上：%s" % e, []
    rl = norm(d)
    return True, "通了", sorted(rl.keys())


def push(rl):
    body = json.dumps({"src": "账号", "status": {"rate_limits": rl,
                                                "model": {"display_name": "账号总额度"}}})
    req = urllib.request.Request(SINK, data=body.encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def shape(o, pre="", depth=0):
    """只打字段名和数值，不打字符串内容 —— 免得把 token 之类的东西吐出来。"""
    if depth > 4:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                print("  " * depth + k + ":")
                shape(v, pre + k + ".", depth + 1)
            else:
                print("  " * depth + k + " =",
                      v if isinstance(v, (int, float, bool)) or v is None else "<str>")
    elif isinstance(o, list):
        print("  " * depth + "[%d 项]" % len(o))
        if o:
            shape(o[0], pre, depth + 1)


def main():
    tok = token()
    if not tok:
        print("!! 还没有可用的 token。在终端跑一次：")
        print("     claude setup-token")
        print("   把它给出的那串粘到  %s  里就行（只存本机，不会外发）"
              % paths.data(".token"))
        return 1
    probe = "--probe" in sys.argv
    wait = POLL
    while True:
        try:
            d = fetch(tok)
            if probe:
                print("=== /api/oauth/usage 返回结构 ===")
                shape(d)
                print("\n=== 归一化后 ===")
                print(json.dumps(norm(d), ensure_ascii=False, indent=1))
                return 0
            rl = norm(d)
            if rl:
                push(rl)
                print("[额度] " + "  ".join(
                    "%s=%s%%" % (k, v["used_percentage"]) for k, v in rl.items()), flush=True)
                wait = POLL              # 成功了就回到正常节奏
            else:
                print("[额度] 返回里没认出用量窗口，用 --probe 看看结构", flush=True)
        except urllib.error.HTTPError as e:
            print("[额度] HTTP %s —— %s" % (e.code, e.reason), flush=True)
            if probe:
                return 1
            if e.code == 403:
                # 403 = token 认了但没这个端点的权限。setup-token 生成的长期 token
                # 就是这样（冷却一晚之后验证过，不是限流）。再轮询也是 403，退出。
                print("[额度] 这条 token 没有 /api/oauth/usage 的权限（403），停止轮询", flush=True)
                return 0
            if e.code == 429:
                # 这个端点限流极凶，触发一次要卡很久。硬退避，别继续撞墙 ——
                # 一直撞的话它会一直不放行。
                wait = min(900, wait * 2)
                print("[额度] 退避到 %d 秒后再试" % wait, flush=True)
                time.sleep(wait)
                continue
        except Exception as e:
            print("[额度] 取数失败：%s" % e, flush=True)
            if probe:
                return 1
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main() or 0)
