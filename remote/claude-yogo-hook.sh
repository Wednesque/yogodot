#!/bin/sh
# Claude Code -> YOGO 胶囊 状态上报（服务器端）
#
# 装法：~/.claude/hooks/claude-yogo-hook.sh   (chmod +x)
#       同目录还要有 yogo_name.py
#
# 不走网络。只往 ~/.claude/yogo/events.jsonl 追加一行，
# 桌面那边用 `ssh <你的服务器> tail -F` 把这条流拉过去 —— 不需要反向隧道，
# Termius 断开也不影响，SSH 掉了会自己重连。
#
# 无论如何 exit 0：上报出任何问题都不能拖累 Claude 本身。

mkdir -p "$HOME/.claude/yogo" 2>/dev/null

cat | OUT="$HOME/.claude/yogo/events.jsonl" python3 -c '
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
try:
    d = json.loads(sys.stdin.read() or "{}")
except Exception:
    d = {}
try:
    from yogo_name import resolve
    key, idx = resolve(d)
except Exception:
    key, idx = "claude", ""
row = {"t": time.time(), "key": key, "idx": idx,
       "ev":   d.get("hook_event_name", ""),
       "tool": d.get("tool_name", ""),
       "cwd":  d.get("cwd", ""),
       "msg":  (d.get("last_assistant_message") or d.get("message") or "")[:200]}
p = os.environ["OUT"]
with open(p, "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
try:                                    # 别让文件无限长
    if os.path.getsize(p) > 240000:
        lines = open(p, encoding="utf-8").readlines()[-300:]
        open(p, "w", encoding="utf-8").writelines(lines)
except Exception:
    pass
' >/dev/null 2>&1

exit 0
