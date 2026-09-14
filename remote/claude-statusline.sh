#!/bin/sh
# 服务器端 statusLine 桥接：把官方 rate_limits 写进同一条事件流。
#
# 装法：~/.claude/hooks/claude-statusline.sh   (chmod +x)
# 服务器 ~/.claude/settings.json 里：
#   "statusLine": { "type": "command",
#                   "command": "$HOME/.claude/hooks/claude-statusline.sh" }
#
# 跟 hook 一样只写本地文件，桌面那边 tail -F 拉走。
# 无论如何 exit 0，绝不影响 Claude 的状态栏。

BODY=$(cat)
mkdir -p "$HOME/.claude/yogo" 2>/dev/null

printf '%s' "$BODY" | OUT="$HOME/.claude/yogo/events.jsonl" python3 -c '
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
try:
    d = json.loads(sys.stdin.read() or "{}")
except Exception:
    d = {}
try:
    from yogo_name import resolve
    key, _ = resolve(d)
except Exception:
    key = "server"
row = {"t": time.time(), "key": key, "ev": "Status",
       "status": {"rate_limits": d.get("rate_limits") or {},
                  "model": (d.get("model") or {}).get("display_name"),
                  "cwd": d.get("cwd", "")}}
with open(os.environ["OUT"], "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
sys.stderr.write(key)
' 2>/tmp/yogo-win.$$ >/dev/null

WIN=$(cat /tmp/yogo-win.$$ 2>/dev/null); rm -f /tmp/yogo-win.$$
[ -z "$WIN" ] && WIN="claude"

# 照常打印一行状态栏（没有 jq 也能抠出百分比）
FIVE=$(printf '%s' "$BODY" | grep -o '"five_hour":{[^}]*}' | grep -o '"used_percentage":[0-9.]*' | cut -d: -f2)
SEVEN=$(printf '%s' "$BODY" | grep -o '"seven_day":{[^}]*}' | grep -o '"used_percentage":[0-9.]*' | cut -d: -f2)
LINE="$WIN"
[ -n "$FIVE" ]  && LINE="$LINE  5h ${FIVE%.*}%"
[ -n "$SEVEN" ] && LINE="$LINE  7d ${SEVEN%.*}%"
echo "$LINE"

exit 0
