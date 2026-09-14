#!/usr/bin/env python3
"""
Claude Code statusLine 桥接。

Claude Code 每个回合都会把一份 JSON 从 stdin 传给 statusLine 命令，
Pro/Max 账号的 JSON 里带官方的 rate_limits：
    rate_limits.five_hour.used_percentage / resets_at
    rate_limits.seven_day.used_percentage / resets_at
    rate_limits.spend_limit.used_percentage / resets_at   (v2.1.251+)
—— 就是你在设置里看到的那个百分比本身，不是估算。

这个脚本做两件事：
  1. 把整份 JSON 转发给本地控制台（POST /status），HUD 就能显示官方额度
  2. 照常打印一行 statusline，所以你原本的状态栏不受影响

装法：把下面这段合并进 ~/.claude/settings.json
  "statusLine": { "type": "command",
                  "command": "py -3 <本项目目录>/statusline.py" }
"""
import json
import sys
import urllib.request

PORT = 9099          # 跟 server.py 的 hook 端口一致
SRC = "local"        # 本机；服务器那边用 remote/claude-statusline.sh 并带上 tmux 窗口名


def main():
    try:
        raw = sys.stdin.read()
        d = json.loads(raw or "{}")
    except Exception:
        print("")
        return

    # ── 转发（失败一律吞掉，绝不能影响 Claude 的状态栏）──
    try:
        body = json.dumps({"src": SRC, "status": d}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/status" % PORT, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=0.4).read()
    except Exception:
        pass

    # ── 照常打印状态栏 ──
    parts = []
    model = (d.get("model") or {}).get("display_name") or (d.get("model") or {}).get("id")
    if model:
        parts.append(model)
    rl = d.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        v = rl.get(key) or {}
        p = v.get("used_percentage")
        if isinstance(p, (int, float)):
            parts.append("%s %.0f%%" % (label, p))
    cwd = d.get("cwd") or ""
    if cwd:
        parts.append(cwd.replace("\\", "/").rsplit("/", 1)[-1])
    print("  ".join(parts))


if __name__ == "__main__":
    main()
