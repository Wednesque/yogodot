#!/usr/bin/env python3
"""
YOGO 扩展键监听器

键盘上那两个可编程键改成组合键后，由这里接住并执行动作。
纯 ctypes + tkinter，无第三方依赖。

默认绑定（在 ATK HUB 里把 key1 / key2 设成这两个组合）：
    Ctrl+Alt+Shift+J   ->  跳到最需要你的那个 tmux 窗口
    Ctrl+Alt+Shift+K   ->  记一笔（弹极简输入框，写进 backlog）

用法：  py -3 hotkeys.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import threading
import time
import urllib.request

SERVER = os.environ.get("YOGO_UI", "http://127.0.0.1:8787")
BACKLOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backlog.md")
TERM_TITLES = ("Termius", "Windows Terminal", "MobaXterm", "PuTTY")
TMUX_PREFIX_VK = 0x42          # B   (tmux 默认前缀 Ctrl+B)

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
WM_HOTKEY = 0x0312
HOT = MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT

VK_J, VK_K = 0x4A, 0x4B
VK_CONTROL = 0x11
KEYEVENTF_KEYUP = 0x0002


# ── 发按键 ──────────────────────────────────────────────────────────────
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _key(vk, up=False):
    i = INPUT(type=1)
    i.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                      time=0, dwExtraInfo=None)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def tap(vk, ctrl=False):
    if ctrl:
        _key(VK_CONTROL)
    _key(vk)
    _key(vk, True)
    if ctrl:
        _key(VK_CONTROL, True)


# ── 找终端窗口 ──────────────────────────────────────────────────────────
def focus_terminal():
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if any(t.lower() in buf.value.lower() for t in TERM_TITLES):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)             # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.12)
    return True


# ── 动作 ────────────────────────────────────────────────────────────────
def get_json(path):
    try:
        with urllib.request.urlopen(SERVER + path, timeout=1) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def post_action(name):
    try:
        data = json.dumps({"action": name}).encode("utf-8")
        req = urllib.request.Request(SERVER + "/action", data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1).read()
    except Exception:
        pass


def action_jump():
    """跳到当前最需要你的那个 tmux 窗口。"""
    top = get_json("/jump")
    if not focus_terminal():
        print("[jump] 没找到终端窗口，标题需含：" + " / ".join(TERM_TITLES))
        return
    idx = str(top.get("idx") or "").strip()
    if idx.isdigit():
        tap(TMUX_PREFIX_VK, ctrl=True)                 # tmux 前缀 Ctrl+B
        time.sleep(0.05)
        tap(0x30 + int(idx) % 10)                      # 数字键 = select-window
        print("[jump] -> %s (window %s, %s)" % (top.get("key"), idx, top.get("state")))
    else:
        print("[jump] 已聚焦终端；这个会话没上报 tmux 窗口号")
    post_action("ack")                                  # 告诉控制台「我看到了」


def action_capture():
    """记一笔：不打断当前任务，把念头丢进 backlog。"""
    def ui():
        import tkinter as tk
        root = tk.Tk()
        root.title("记一笔")
        root.attributes("-topmost", True)
        root.configure(bg="#14121C")
        root.overrideredirect(True)
        w, h = 520, 62
        root.geometry("%dx%d+%d+%d" % (w, h,
                      (root.winfo_screenwidth() - w) // 2,
                      int(root.winfo_screenheight() * 0.32)))
        e = tk.Entry(root, font=("Microsoft YaHei", 13), bg="#1B1826", fg="#EFECF7",
                     insertbackground="#FF6A1F", relief="flat", highlightthickness=2,
                     highlightbackground="#FF6A1F", highlightcolor="#FF6A1F")
        e.pack(fill="both", expand=True, padx=10, pady=10, ipady=6)
        e.focus_force()

        def save(_=None):
            txt = e.get().strip()
            if txt:
                with open(BACKLOG, "a", encoding="utf-8") as f:
                    f.write("- [ ] %s  <!-- %s -->\n" % (txt, time.strftime("%Y-%m-%d %H:%M")))
                print("[capture] " + txt)
            root.destroy()

        e.bind("<Return>", save)
        e.bind("<Escape>", lambda _: root.destroy())
        root.after(30000, root.destroy)                 # 30 秒没输入自动消失
        root.mainloop()

    threading.Thread(target=ui, daemon=True).start()


BINDINGS = {1: ("Ctrl+Alt+Shift+J  跳到需要我的窗口", VK_J, action_jump),
            2: ("Ctrl+Alt+Shift+K  记一笔",           VK_K, action_capture)}


def main():
    for hid, (desc, vk, _fn) in BINDINGS.items():
        if not user32.RegisterHotKey(None, hid, HOT, vk):
            print("!! 注册失败（可能被别的程序占用）: " + desc)
        else:
            print("   " + desc)
    print("\n监听中，Ctrl+C 退出。backlog -> " + BACKLOG)
    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                b = BINDINGS.get(msg.wParam)
                if b:
                    try:
                        b[2]()
                    except Exception as e:
                        print("动作出错:", e)
    except KeyboardInterrupt:
        pass
    finally:
        for hid in BINDINGS:
            user32.UnregisterHotKey(None, hid)


if __name__ == "__main__":
    main()
