#!/usr/bin/env python3
"""
在服务器上直接问账号额度，把结果写进 ~/.claude/yogo/events.jsonl。

为什么在服务器上跑：
  · /api/oauth/usage 需要带 user:profile scope 的 token。`claude setup-token`
    生成的长期 token 没有这个 scope（403：scope requirement user:profile），
    只有交互式登录那条有 —— 它存在 ~/.claude/.credentials.json 里。
  · 那条 token 每小时过期，得有正在跑的 Claude 会话帮它续。服务器上 coder /
    writer 一直开着，token 一直是新的；桌面端没有这个条件。
  · token 只在本机读、本机用，出去的只有百分比。

装法：~/.claude/hooks/yogo-quota.py，用 systemd --user 或 nohup 常驻。
    python3 yogo-quota.py --probe    看一次返回结构（只打字段名和数值）
    python3 yogo-quota.py            常驻，每 180 秒一次
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = "https://api.anthropic.com/api/oauth/usage"
CRED = os.path.expanduser("~/.claude/.credentials.json")
OUT = os.path.expanduser("~/.claude/yogo/events.jsonl")
POLL = 180


def claude_version():
    try:
        import subprocess
        v = subprocess.run([os.path.expanduser("~/.local/bin/claude"), "--version"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
        for tok in v.split():
            if tok[0].isdigit():
                return tok
    except Exception:
        pass
    return "2.1.263"


UA = "claude-code/%s" % claude_version()


def token():
    try:
        d = json.load(open(CRED, encoding="utf-8"))
    except Exception:
        return None
    o = d.get("claudeAiOauth") or {}
    t = o.get("accessToken")
    if not t:
        return None
    exp = o.get("expiresAt") or 0
    if exp > 1e12:
        exp /= 1000.0
    if exp and exp < time.time():
        return None                    # 过期了，等会话把它续上再说
    return t


def fetch(tok):
    req = urllib.request.Request(URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": UA,              # 少了这个会立刻 429
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _iso(v):
    if isinstance(v, (int, float)):
        return int(v / 1000) if v > 1e12 else int(v)
    if isinstance(v, str):
        try:
            return int(datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def norm(d):
    """按模型拆分的周额度**不在** seven_day_opus / seven_day_sonnet 那些字段里
    （实测全是 null），而在 `limits` 列表里：
        kind=session        -> 5 小时
        kind=weekly_all     -> 本周（全模型）
        kind=weekly_scoped  -> 某个模型的本周，scope.model.display_name 写着是谁
    所以优先读 limits；顶层的 five_hour / seven_day 只当兜底。"""
    out = {}
    for it in (d or {}).get("limits") or []:
        if not isinstance(it, dict) or not isinstance(it.get("percent"), (int, float)):
            continue
        kind = it.get("kind")
        if kind == "session":
            key = "five_hour"
        elif kind == "weekly_all":
            key = "seven_day"
        elif kind == "weekly_scoped":
            sc = it.get("scope") or {}
            name = ((sc.get("model") or {}).get("display_name")
                    or (sc.get("model") or {}).get("id") or sc.get("surface") or "scoped")
            key = "seven_day_" + str(name).lower().replace(" ", "_")
        else:
            continue
        row = {"used_percentage": round(float(it["percent"]), 1)}
        rst = _iso(it.get("resets_at"))
        if rst:
            row["resets_at"] = rst
        out[key] = row
    # 兜底：limits 里没有的，从顶层字段补
    for k in ("five_hour", "seven_day"):
        v = (d or {}).get(k)
        if k not in out and isinstance(v, dict) and isinstance(v.get("utilization"), (int, float)):
            row = {"used_percentage": round(float(v["utilization"]), 1)}
            rst = _iso(v.get("resets_at"))
            if rst:
                row["resets_at"] = rst
            out[k] = row
    return out


def shape(o, depth=0):
    """只打字段名和数值，不打字符串内容。"""
    if depth > 4:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                print("  " * depth + k + ":")
                shape(v, depth + 1)
            else:
                print("  " * depth + k + " =",
                      v if isinstance(v, (int, float, bool)) or v is None else "<str>")
    elif isinstance(o, list):
        print("  " * depth + "[%d 项]" % len(o))
        if o:
            shape(o[0], depth + 1)


def emit(rl):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    row = {"t": time.time(), "key": "账号", "ev": "Status",
           "status": {"rate_limits": rl, "model": "账号", "cwd": ""}}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    probe = "--probe" in sys.argv
    wait = POLL
    while True:
        tok = token()
        if not tok:
            print("[额度] 没有可用的 token（没登录或已过期，等会话续）", flush=True)
            if probe:
                return 1
            time.sleep(120)
            continue
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
                emit(rl)
                print("[额度] " + "  ".join("%s=%s%%" % (k, v["used_percentage"])
                                          for k, v in rl.items()), flush=True)
            wait = POLL
        except urllib.error.HTTPError as e:
            print("[额度] HTTP %s %s" % (e.code, e.reason), flush=True)
            if probe:
                try:
                    print(e.read().decode("utf-8", "replace")[:300])
                except Exception:
                    pass
                return 1
            wait = min(900, wait * 2) if e.code == 429 else max(wait, 300)
        except Exception as e:
            print("[额度] 取数失败：%s" % e, flush=True)
            if probe:
                return 1
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main() or 0)
