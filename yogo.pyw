#!/usr/bin/env python3
"""
YOGO —— 托盘程序。一个进程带起整套东西：

  · 本地服务（server.py）：托管控制台页面、拉服务器状态、推 SSE
  · 键盘（kbd.py）：托盘进程直接用 Windows HID API 持有接收器，不经过浏览器
  · 控制台窗口（yogo.html）：主题 / 动画 / 番茄钟的引擎目前还是这页的 JS，
    通过本地 HTTP 把帧交给 kbd.py 写设备。平时藏在托盘里，要调图案时叫出来
  · 胶囊（hud.py，纯 Python 分层窗口）：中键唤出

托盘右键有菜单。开机自启用 install.py 装。

为什么是 .pyw：双击不弹黑框。命令行调试的话用
    py -3 yogo.pyw --console
会把日志打到当前终端。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time
import urllib.request

import paths

HERE = paths.RES                   # 程序自带的文件
DATA = paths.DATA                  # 要写的文件
sys.path.insert(0, HERE)
os.chdir(DATA)

import hud                      # noqa: E402  —— 里面会先声明 DPI 感知，必须早导入
import server                   # noqa: E402

SERVER = "http://127.0.0.1:%d" % server.PORT
ICON = paths.data("yogo.ico")
LOG = paths.data("yogo.log")
APP = "YOGO 键盘"

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY, WM_COMMAND, WM_CLOSE = 0x0002, 0x0111, 0x0010
WM_APP_TRAY = 0x0400 + 17
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
TPM_RIGHTALIGN, TPM_BOTTOMALIGN, TPM_RETURNCMD = 0x0008, 0x0020, 0x0100
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0000, 0x0800, 0x0008
SW_HIDE, SW_SHOW, SW_RESTORE, SW_SHOWNOACTIVATE = 0, 5, 9, 4
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
# 控制台窗口先生在屏幕外：Chrome 的 --app 窗口从创建到我们能藏起来之间，
# 会先白屏一段（开机时尤其久），而且它没有关闭按钮，用户只能干看着。
# 生在屏外就一次都不会露脸，真要看时再搬回来。
OFFSCREEN = (-32000, -32000)
CONSOLE_SIZE = (1320, 900)

ID_CONSOLE, ID_CAPSULE, ID_AUTOSTART, ID_RESTART, ID_QUIT = 1001, 1002, 1003, 1004, 1005


# ── 日志：.pyw 没有 stdout，全部落到 yogo.log ──────────────────────
class Tee:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s):
        if s.strip():
            self.f.write("%s %s\n" % (time.strftime("%H:%M:%S"), s.rstrip()))
        return len(s)

    def flush(self):
        self.f.flush()


if "--console" not in sys.argv:
    sys.stdout = sys.stderr = Tee(LOG)


# ── 图标：没有就现画一个（跟胶囊里那只小章鱼同一个造型）──────────
def ensure_icon():
    if os.path.exists(ICON):
        return
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return
    art = [
        "......#####......",
        "....#########....",
        "...###########...",
        "..#############..",
        "..#############..",
        "..###ww###ww###..",
        "..###wp###wp###..",
        "..#############..",
        "...###########...",
        "...###########...",
        "...##.##.##.##...",
        "....#..#..#..#...",
        "....#.....#......",
    ]
    C = {"#": (217, 119, 87, 255), "w": (255, 255, 255, 255), "p": (10, 10, 12, 255)}
    base = Image.new("RGBA", (17, 17), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    for y, line in enumerate(art):
        for x, ch in enumerate(line):
            if ch in C:
                d.point((x, y + 2), fill=C[ch])
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base.resize((256, 256), Image.NEAREST).save(
        ICON, sizes=[(s, s) for s in sizes])


# ── 控制台窗口 ────────────────────────────────────────────────────
class Console:
    """yogo.html 那个页面。键盘的灯效、点阵屏、番茄钟全靠它，所以开机就起，
    但默认藏起来 —— 要调图案时再叫出来。"""
    TITLE = "YOGO 控制台"

    def __init__(self):
        self.hwnd = None
        self.visible = False

    def launch(self, show=False):
        # URL 带上文件 mtime：改过页面就必然重新加载，绕开 Chrome profile 里
        # 已经存下的旧缓存条目（no-store 只对将来的请求生效）。
        ver = self.page_ver()
        self.ver = ver
        w, hgt = CONSOLE_SIZE
        h, pid = hud.chrome_app("%s/yogo.html?v=%d" % (SERVER, ver), self.TITLE,
                                w, hgt, OFFSCREEN[0], OFFSCREEN[1], wait=25.0)
        if not h:
            print("!! 控制台窗口没起来")
            return
        self.hwnd = h
        self.visible = False
        user32.ShowWindow(h, SW_HIDE)
        if show:
            self.show()
        print("   控制台就位  hwnd=0x%X  pid=%d" % (h, pid))

    @staticmethod
    def page_ver():
        try:
            return int(os.path.getmtime(os.path.join(paths.RES, "yogo.html")))
        except OSError:
            return int(time.time())

    def alive(self):
        return bool(self.hwnd) and bool(user32.IsWindow(self.hwnd))

    def show(self):
        if not self.alive():
            threading.Thread(target=self.launch, args=(True,), daemon=True).start()
            return
        self.center()
        user32.ShowWindow(self.hwnd, SW_RESTORE)
        user32.SetForegroundWindow(self.hwnd)
        self.visible = True

    def center(self):
        """从屏幕外搬回来，摆在屏幕中间偏上。"""
        w, h = CONSOLE_SIZE
        try:
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        except Exception:
            x, y = 140, 60
        user32.SetWindowPos(self.hwnd, None, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def hide(self):
        if self.alive():
            user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False

    def toggle(self):
        (self.hide if self.visible else self.show)()

    def watchdog(self):
        """控制台是服务，不是普通窗口 —— 被叉掉就悄悄重新拉起来藏好，
        否则键盘的灯和点阵屏会一起停摆。"""
        while True:
            time.sleep(4)
            if self.hwnd and not self.alive():
                print("   控制台被关了，重新拉起")
                self.hwnd = None
                self.visible = False
                self.launch(show=False)
                continue
            # 页面文件改了而窗口还跑着旧版 —— 在它藏着的时候悄悄换成新版。
            # 否则会出现「改了没生效」「在旧版里做的设置新版不认」这类怪事。
            if self.alive() and not self.visible and self.page_ver() != getattr(self, "ver", None):
                print("   控制台页面更新了，换成新版")
                try:
                    user32.PostMessageW(self.hwnd, 0x0010, 0, 0)     # WM_CLOSE
                except Exception:
                    pass
                time.sleep(2)
                self.hwnd = None
                self.launch(show=False)


CONSOLE = Console()


# ── 开机自启 ──────────────────────────────────────────────────────
def _lnk_dir(*parts):
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", *parts)


def startup_lnk():
    return _lnk_dir("Start Menu", "Programs", "Startup", "YOGO.lnk")


def make_lnk(path):
    """建一个指向本程序的快捷方式。开始菜单和桌面各放一个，
    这样"调图案那个软件"在开始菜单里搜得到、桌面上点得到。"""
    import subprocess
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
          "$s.TargetPath='%s';$s.Arguments='\"%s\"';$s.WorkingDirectory='%s';"
          "$s.IconLocation='%s';$s.Description='YOGO 键盘控制台';$s.Save()"
          % (path, pyw, os.path.join(HERE, "yogo.pyw"), HERE, ICON))
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                       **hud.quiet())
    except Exception:
        pass


def ensure_shortcuts():
    for p in (_lnk_dir("Start Menu", "Programs", "YOGO 键盘.lnk"),
              os.path.join(os.path.expanduser("~"), "Desktop", "YOGO 键盘.lnk")):
        if not os.path.exists(p):
            d = os.path.dirname(p)
            if os.path.isdir(d):
                make_lnk(p)
                print("   建了快捷方式：%s" % p)


def autostart_on():
    return os.path.exists(startup_lnk())


def set_autostart(on):
    if not on:
        try:
            os.remove(startup_lnk())
        except OSError:
            pass
        return
    make_lnk(startup_lnk())


# ── 托盘 ──────────────────────────────────────────────────────────
class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HANDLE),
                ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HANDLE)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# ctypes.WinDLL("user32") 每次都是新实例，hud.py 里声明过的签名在这儿不算数，
# 得重来一遍。漏声明的话句柄按 32 位传，返回的 HWND 高位被截断 —— 静默失效。
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.CreateMutexW.restype = wt.HANDLE
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HANDLE), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class Tray:
    def __init__(self):
        self.hwnd = None
        self.nid = None
        self._proc = WNDPROC(self.wndproc)

    def create(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASS()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "YogoTrayWnd"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            print("!! 注册托盘窗口类失败", ctypes.get_last_error())
        self._wc = wc                      # 留引用，别让 GC 回收 WNDPROC
        self.hwnd = user32.CreateWindowExW(0, "YogoTrayWnd", APP, 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        icon = user32.LoadImageW(None, ICON, IMAGE_ICON, 0, 0,
                                 LR_LOADFROMFILE | LR_DEFAULTSIZE)
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = icon
        nid.szTip = APP
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self.nid = nid

    def tip(self, text):
        if not self.nid:
            return
        self.nid.szTip = text[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def remove(self):
        if self.nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None

    def menu(self):
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING, ID_CONSOLE,
                           "隐藏控制台" if CONSOLE.visible else "打开控制台")
        user32.AppendMenuW(m, MF_STRING, ID_CAPSULE, "显示胶囊")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if autostart_on() else 0),
                           ID_AUTOSTART, "开机自启")
        user32.AppendMenuW(m, MF_STRING, ID_RESTART, "重启")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, ID_QUIT, "退出")
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # TrackPopupMenu 的老规矩：不先 SetForegroundWindow，菜单点外面不会消失
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(m)
        if cmd:
            self.command(cmd)

    def command(self, cmd):
        if cmd == ID_CONSOLE:
            CONSOLE.toggle()
        elif cmd == ID_CAPSULE:
            threading.Thread(target=hud.HUD.show, daemon=True).start()
        elif cmd == ID_AUTOSTART:
            set_autostart(not autostart_on())
        elif cmd == ID_RESTART:
            restart()
        elif cmd == ID_QUIT:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            low = lparam & 0xFFFF
            if low == WM_RBUTTONUP:
                self.menu()
            elif low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                CONSOLE.toggle()
            return 0
        if msg == WM_COMMAND:
            self.command(wparam & 0xFFFF)
            return 0
        if msg in (WM_CLOSE, WM_DESTROY):
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


TRAY = Tray()


def restart():
    import subprocess
    cleanup()
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    subprocess.Popen([pyw, os.path.join(HERE, "yogo.pyw")],
                     cwd=HERE, close_fds=True, **hud.quiet())
    os._exit(0)


PS_KILL_SSH = ("Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
               "Where-Object { $_.CommandLine -like '*yogo/events.jsonl*' } | "
               "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")


def kill_ssh():
    """清掉我们自己起的 ssh。上一个实例退出时它不会跟着死（Windows 不级联杀子进程），
    留下来的那条重连时还会开一个终端窗口 —— 开机时那堆黑框有一半是它。"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", PS_KILL_SSH],
                       timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       **hud.quiet())
    except Exception:
        pass


def cleanup():
    TRAY.remove()
    hud.kill_stale()
    kill_ssh()


# ── 页面请求宿主做事（胶囊里点"控制台"）────────────────────────────
def apply_claude(on):
    """Claude 模式开关落到托盘程序这边：胶囊能不能弹、中键归不归我们。"""
    if hud.ENABLED == on:
        return
    hud.ENABLED = on
    if not on:
        try:
            hud.HUD.hide()
        except Exception:
            pass
    TRAY.tip("%s —— %s" % (APP, "中键唤出胶囊" if on else "键盘灯效工具"))
    print("   Claude 模式：%s" % ("开（胶囊 / vibe / 额度）" if on else "关（纯灯效）"))


def watch_wants():
    tick = 0
    while True:
        time.sleep(1.0)
        tick += 1
        if tick % 3 == 0:                       # 每 3 秒对一次总开关，控制台里切了就生效
            try:
                apply_claude(server.claude_on())
            except Exception:
                pass
        try:
            with urllib.request.urlopen(SERVER + "/want", timeout=1.5) as r:
                import json
                d = json.loads(r.read().decode("utf-8"))
            if d.get("console"):
                CONSOLE.show()
            if d.get("pop"):                    # 会话有事，把胶囊放下来
                try:
                    hud.HUD.show()
                except Exception:
                    pass
        except Exception:
            pass


def main():
    # 只允许跑一个：托盘里挂两个图标、两套鼠标钩子会互相打架
    kernel32.CreateMutexW(None, True, "Global\\YOGO_KEYBOARD_APP")
    if kernel32.GetLastError() == 183:            # ERROR_ALREADY_EXISTS
        print("已经在运行了")
        return

    ensure_icon()
    ensure_shortcuts()
    print("=== YOGO 启动 ===")
    hud.kill_stale()                              # 清掉上次跑剩的 Chrome 窗口
    kill_ssh()                                    # 和上次跑剩的 ssh（会开黑框）
    time.sleep(0.6)

    server.start()
    time.sleep(0.8)
    hud.create_window()          # 胶囊窗口必须建在有消息循环的这个线程上
    hud.start_background()
    threading.Thread(target=CONSOLE.launch, daemon=True).start()
    threading.Thread(target=CONSOLE.watchdog, daemon=True).start()
    threading.Thread(target=watch_wants, daemon=True).start()

    TRAY.create()
    hh = hud.install_hook()                       # 钩子必须和消息循环同一个线程
    hud.ENABLED = server.claude_on()
    TRAY.tip("%s —— %s" % (APP, "中键唤出胶囊" if hud.ENABLED else "键盘灯效工具"))
    print("   托盘就位；%s" % ("中键唤出胶囊，托盘左键开控制台" if hud.ENABLED
                            else "Claude 模式关着，托盘左键开控制台"))

    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        pass
    finally:
        if hh:
            hud.user32.UnhookWindowsHookEx(hh)
        cleanup()
        print("=== YOGO 退出 ===")


if __name__ == "__main__":
    main()
