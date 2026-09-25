"""服务器端：给一条 Claude Code 事件起名字。

名字的优先级 —— 越靠前越接近你在客户端里看到的那个名字：
  1. session 的 customTitle（~/.claude/projects/*/<session_id>/custom-title.json）
     这就是你自己起的 coder / writer，客户端 others 里显示的也是它。
  2. tmux 窗口名（先看 $TMUX_PANE，再顺着父进程往上找哪个祖先是某个 pane）
     —— 直接在 tmux 里跑的会话走这条。
  3. cwd 的目录名。
被 claude-yogo-hook.sh / claude-statusline.sh 用 `from yogo_name import resolve` 引入（文件名必须是下划线，横杠导入不了）。
"""
import glob
import json
import os
import subprocess


def from_title(session_id):
    if not session_id:
        return ""
    pat = os.path.expanduser("~/.claude/projects/*/%s/custom-title.json" % session_id)
    for f in glob.glob(pat):
        try:
            t = json.load(open(f, encoding="utf-8")).get("customTitle")
            if t:
                return str(t)
        except Exception:
            pass
    return ""


def from_tmux():
    """返回 (窗口名, 窗口序号)。不在 tmux 里就是 ("", "")。"""
    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            out = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#W\t#I"],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
            if out and "\t" in out:
                w, i = out.split("\t", 1)
                if w:
                    return w, i
        except Exception:
            pass
    # 环境变量没有（比如 daemon 起的会话），就顺着父进程往上找
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#{window_index}\t#{window_name}"],
                             capture_output=True, text=True, timeout=2).stdout
        panes = {}
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) == 3:
                panes[p[0]] = (p[2], p[1])
        pid = str(os.getpid())
        for _ in range(12):
            if pid in panes:
                return panes[pid]
            try:
                pid = open("/proc/%s/stat" % pid).read().split(") ", 1)[1].split()[1]
            except Exception:
                break
            if pid in ("0", "1"):
                break
    except Exception:
        pass
    return "", ""


def resolve(d):
    """d 是 hook / statusLine 的原始 JSON。返回 (name, idx)。"""
    t = from_title(d.get("session_id"))
    w, i = from_tmux()
    if t:
        return t, i
    if w:
        return w, i
    cwd = d.get("cwd") or os.getcwd()
    return os.path.basename(cwd.rstrip("/")) or "claude", i
