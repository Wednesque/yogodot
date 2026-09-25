#!/usr/bin/env python3
"""
YOGO 胶囊 —— 中键唤出的小组件。

按鼠标中键，一条圆角胶囊从屏幕上方长出来，显示 Claude 额度、会话状态、
番茄钟。再按一次收起。点胶囊任意位置会把控制台（调图案那个界面）叫出来。

渲染方式：**自己出图**，不借 Chrome。
  Chrome 的 --app 窗口会自己画一圈浅色框架和标题栏，而那圈东西走它自己的
  合成层，SetWindowRgn 裁不到（实测：区域只裁网页内容，框架照画），所以
  胶囊边上总挂一道白边、底角是方的。现在改成 PIL 出一张带 alpha 的图，
  再用 UpdateLayeredWindow 逐像素透明地贴上去 —— 圆角是抗锯齿的，还能带
  柔和投影，压根没有"框架"这回事。
控制台页面还跑在一个 Chrome 窗口里，但只是当 JS 运行时用 —— 键盘读写已经
搬进 kbd.py（Windows HID API），不再经过 WebHID，也就不再有授权这回事。

用法：  py -3 hud.py        （或者跑 yogo.pyw，那是完整的托盘程序）
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import subprocess
import threading
import time
import urllib.request
import winreg

import capsule

W, H = 470, 120              # 胶囊本体尺寸（DIP）—— 不是通栏横条，是一块悬浮方块
GAP = 14                     # 离屏幕上沿的距离，脱开边缘才像悬浮的东西
RADIUS = 34                  # 四角全圆（DIP）
SHADOW = 2                   # 窗口比胶囊稍大一点，给抗锯齿边缘留余量
SERVER = os.environ.get("YOGO_UI", "http://127.0.0.1:8787")
TITLE = "YOGO HUD"
MORPH_MS = 460               # 展开时长
COLLAPSE_MS = 200            # 收起要干脆，回弹只放在展开上
AUTO_HIDE_S = 2              # 完全展开后停留多久（默认；控制台里可调 1.5/2/3）
ENABLED = False              # Claude 模式总开关。关着时：中键不管、胶囊不弹、不拉数据。
                             # 窗口和钩子照常建（必须在主线程建），只是不干活，这样能随时开关。
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hudprofile")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 高分屏上必须自己声明 DPI 感知，否则给的坐标会被系统按缩放比例二次放大。
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW = 0x00080000, 0x00000080
WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x08000000, 0x00000008
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x02, 0x00, 0x01
BI_RGB, DIB_RGB_COLORS = 0, 0
WM_DESTROY, WM_LBUTTONUP, WM_MOUSEMOVE = 0x0002, 0x0202, 0x0200
WH_MOUSE_LL = 14
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
# 锁屏 / 解锁的系统通知。注册到胶囊窗口上，收到就告诉 kbd 去熄屏 / 恢复。
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
NOTIFY_FOR_THIS_SESSION = 0
wtsapi = ctypes.WinDLL("wtsapi32", use_last_error=True)
wtsapi.WTSRegisterSessionNotification.argtypes = [wt.HWND, wt.DWORD]
wtsapi.WTSRegisterSessionNotification.restype = wt.BOOL
# 中键事件吞不吞：吞掉就不会触发自动滚动（那个上下箭头的圆圈），
# 代价是中键原本的功能（浏览器关标签页、终端粘贴）也一起没了。
SWALLOW_MIDDLE = True

# 64 位下句柄参数必须显式声明，否则 ctypes 按 C int 传，高 32 位是垃圾。
LRESULT = ctypes.c_ssize_t
HHOOK = wt.HANDLE
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_void_p, ctypes.c_void_p,
                                       wt.HDC, ctypes.c_void_p, wt.DWORD,
                                       ctypes.c_void_p, wt.DWORD]
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = HHOOK
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HANDLE
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                   ctypes.c_void_p, wt.HANDLE, wt.DWORD]
gdi32.DeleteObject.argtypes = [wt.HANDLE]
user32.GetDC.argtypes = [wt.HWND]
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteDC.argtypes = [wt.HDC]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


# ── 找窗口 / 起 Chrome（控制台还要用）────────────────────────────────
def enum_windows():
    """[(hwnd, pid, title)]，只列有标题的顶层窗口。"""
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append((hwnd, pid.value, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_hwnd(pid, exclude, want=TITLE):
    """认窗口靠 PID —— 标题会撞：开着同一个页面的窗口标题都一样。"""
    wins = enum_windows()
    for hwnd, wpid, title in wins:
        if pid and wpid == pid and want in title:
            return hwnd
    for hwnd, _, title in wins:
        if want in title and hwnd not in exclude:
            return hwnd
    return None


def chrome_path():
    """找一个 Chromium 系浏览器开控制台：先 Chrome，没有就 Edge（Windows 自带，
    国内很多机器只有它）。两者参数通用。"""
    for exe in ("chrome.exe", "msedge.exe"):
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                k = winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + exe)
                p = winreg.QueryValue(k, None)
                if p and os.path.exists(p):
                    return p
            except OSError:
                pass
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    return None


CHROME_FLAGS = [
    # 窗口藏起来时 Chrome 会判定被遮挡而停止绘制，控制台一藏进托盘灯效就卡住。
    "--disable-features=Translate,MediaRouter,CalculateNativeWinOcclusion",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--no-first-run", "--no-default-browser-check",
]


def chrome_app(url, title, w, h, x, y, wait=25.0):
    """起一个无标签页的 Chrome 窗口，返回 (hwnd, pid)。控制台走这条路。"""
    exe = chrome_path()
    if not exe:
        print("!! 找不到 chrome.exe")
        return None, 0
    pre = {hw for hw, _, t in enum_windows() if title in t}
    args = [exe, "--app=%s" % url, "--user-data-dir=%s" % PROFILE,
            "--window-size=%d,%d" % (w, h), "--window-position=%d,%d" % (x, y)] + CHROME_FLAGS
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **quiet())
    except Exception as e:
        print("!! 起 Chrome 失败：", e)
        return None, 0
    for _ in range(int(wait / 0.25)):
        time.sleep(0.25)
        hw = find_hwnd(proc.pid, pre, title)
        if hw:
            return hw, proc.pid
    return None, proc.pid



# 从无控制台的父进程（pythonw）起控制台程序，Windows 会给它开一个黑框。
# 所有子进程一律带上这组参数，开机就不会弹出一堆 cmd 窗口。
def quiet():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                           # SW_HIDE
    return dict(creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=si,
                stdin=subprocess.DEVNULL)


PS_KILL = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
           "Where-Object { $_.CommandLine -like '*hudprofile*' } | "
           "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")


def kill_stale():
    """清掉上次跑剩的控制台窗口。只杀用我们这个 profile 的 Chrome。"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", PS_KILL],
                       timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       **quiet())
    except Exception:
        pass


# ── 状态：从本地服务拉 ──────────────────────────────────────────────
def _ago(t):
    d = max(0.0, time.time() - (t or 0))
    if d < 60:
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % int(d / 60)
    return "%d 小时前" % int(d / 3600)


def _get(path, timeout=1.5):
    try:
        with urllib.request.urlopen(SERVER + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def collect():
    """把三个接口拼成渲染要用的那份状态。"""
    st = {"quota": [], "sessions": [], "pom": {}, "qwhen": "", "theme": "", "pet": "idle"}

    u = _get("/usage") or {}
    best, at = {}, 0
    for v in (u.get("official") or {}).values():
        for k, w in (v.get("rate_limits") or {}).items():
            if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
                continue
            if k not in best or v.get("at", 0) > best[k].get("at", 0):
                best[k] = dict(w, at=v.get("at", 0))
        at = max(at, v.get("at", 0))
    rows = []
    for k in sorted(best, key=lambda x: (capsule.win_order(x), x)):
        o = best[k]
        row = {"key": k, "label": capsule.win_label(k),
               "pct": float(o["used_percentage"])}
        rst = o.get("resets_at")
        if rst:
            row["left"] = capsule.hms(rst - time.time())
            # 只对周额度算趋势。5 小时窗口太短，本来就是一阵一阵地用，
            # 算"是否用超"没有意义。
            if k != "five_hour":
                total = 7 * 86400
                elapsed = min(1.0, max(0.0, (total - (rst - time.time())) / total))
                if elapsed > 0.03:
                    row["tick"] = elapsed
                    row["end"] = row["pct"] / elapsed
        rows.append(row)
    st["quota"] = rows[:3]
    st["qwhen"] = _ago(at) if at else ""

    snap = _get("/snapshot") or {}
    ss = sorted(snap.get("sessions") or [],
                key=lambda s: (-capsule.PRI.get(s.get("state"), 0), -(s.get("at") or 0)))
    for s in ss:
        s["ago"] = _ago(s.get("at"))
    st["sessions"] = ss

    cfg = _get("/capsule") or {}
    st["skin"] = cfg.get("skin") or ""          # 空 = 跟随键盘主题
    st["anim"] = cfg.get("anim") or "island"
    st["petKind"] = cfg.get("pet") or "octopus"
    try:
        st["hold"] = max(1.0, min(60.0, float(cfg.get("hold") or AUTO_HIDE_S)))
    except (TypeError, ValueError):
        st["hold"] = float(AUTO_HIDE_S)

    ui = _get("/ui") or {}
    st["pom"] = ui.get("pom") or {}
    kb = ui.get("kb") or {}
    st["kb_lost"] = (not kb.get("connected")) if kb else False
    st["theme"] = ((ui.get("kb") or {}).get("theme")) or ""

    pet = "idle"
    for s in ss:
        if capsule.PRI.get(s.get("state"), 0) > capsule.PRI.get(pet, 0):
            pet = s.get("state")
    st["pet"] = pet
    return st


# ── 胶囊窗口 ────────────────────────────────────────────────────────
class Hud:
    def __init__(self):
        self.hwnd = None
        self.shown = False
        self.t = 0.0                 # 形变进度：0 药丸，1 完整
        self.state = {"quota": [], "sessions": [], "pom": {}, "pet": "idle", "phase": 0}
        self.skin = capsule.SKINS["obsidian"]
        self.body = None             # 内容层，数据变了才重画
        self.style = "island"
        self.animating = False
        self.sig = None              # 数据指纹，用来判断要不要重画
        self.dirty = True
        self.last_keep = 0.0
        self.phase = 0
        self.scale = 1.0
        self.lock = threading.Lock()
        self._dib = None

    # ── 建窗口（必须在有消息循环的那个线程上建）──
    def create(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)(
            self.wndproc)
        wc = WNDCLASS()
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = "YogoCapsule"
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))   # IDC_ARROW
        self._wc = wc
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
            "YogoCapsule", TITLE, WS_POPUP, 0, 0, 100, 100, None, None, hinst, None)
        if not self.hwnd:
            print("!! 建胶囊窗口失败", ctypes.get_last_error())
            return
        self.measure()
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self.x, 0, self.wpx, self.hpx,
                            SWP_NOACTIVATE)
        if not wtsapi.WTSRegisterSessionNotification(self.hwnd, NOTIFY_FOR_THIS_SESSION):
            print("!! 锁屏通知注册失败", ctypes.get_last_error(), "—— 退回轮询判定")
        self.paint(0.0)
        print("   胶囊就位  %dx%d @%.2fx" % (self.wpx, self.hpx, self.scale), flush=True)

    def measure(self):
        try:
            dpi = user32.GetDpiForWindow(self.hwnd) or 96
        except Exception:
            dpi = 96
        self.scale = dpi / 96.0
        self.cw = int(round(W * self.scale))
        self.ch = int(round(H * self.scale))
        self.gap = int(round(GAP * self.scale))
        self.rend = capsule.Renderer(self.cw, self.ch, self.scale, SHADOW, self.gap)
        self.wpx, self.hpx = self.rend.W, self.rend.H
        self.sw = user32.GetSystemMetrics(0)
        self.x = (self.sw - self.wpx) // 2
        self.dirty = True

    def alive(self):
        return bool(self.hwnd) and bool(user32.IsWindow(self.hwnd))

    # ── 出图并贴上去 ──
    def rebuild(self):
        """重画内容层。只在数据真变了、或宠物该动一帧时才调 ——
        动画每帧都重画的话，光背景渐变就够把帧率拖垮。"""
        self.skin = capsule.skin_for(self.state.get("theme"), self.state.get("skin"))
        self.body = self.rend.content(self.state, self.skin)
        self.dirty = False

    def paint(self, t):
        if not self.alive():
            return
        try:
            with self.lock:
                if self.dirty or self.body is None:
                    self.rebuild()
                data = self.rend.frame(self.body, self.style, t)
            self._blit(data)
        except Exception as e:
            # 带上出错的那一行 —— 光看 "argument 1: OverflowError" 根本不知道是哪个调用
            import traceback
            tb = traceback.extract_tb(e.__traceback__)
            where = "%s:%d" % (tb[-1].name, tb[-1].lineno) if tb else "?"
            print("!! 画胶囊出错（%s）：%s" % (where, e), flush=True)

    def _blit(self, data):
        hdc = user32.GetDC(None)
        memdc = gdi32.CreateCompatibleDC(hdc)
        if self._dib is None:
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = self.wpx
            bmi.biHeight = -self.hpx          # 负数 = 自上而下，跟 PIL 的行序一致
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                         ctypes.byref(bits), None, 0)
            self._dib = (hbm, bits)
        hbm, bits = self._dib
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(memdc, hbm)
        pt_dst = wt.POINT(self.x, 0)
        size = SIZE(self.wpx, self.hpx)
        pt_src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, hdc, ctypes.byref(pt_dst), ctypes.byref(size),
                                   memdc, ctypes.byref(pt_src), 0,
                                   ctypes.byref(blend), ULW_ALPHA)
        gdi32.SelectObject(memdc, old)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(None, hdc)

    # ── 展开 / 收起 ──
    def ease(self, style, k, opening):
        """各样式各自的手感。灵动岛和展开走真正的阻尼弹簧（ω0/ζ 由灵动岛常用的
        stiffness=400 / damping=30 换算而来），尾巴上有一点回弹；下坠用 ease-out；
        淡入接近线性。收起一律干脆，不回弹 —— 收的时候还弹会显得黏。"""
        if not opening:
            return k * k * (3 - 2 * k)
        if style in ("island", "unfold"):
            w0, zeta = 16.0, 0.74
            wd = w0 * math.sqrt(1 - zeta * zeta)
            tt = (MORPH_MS / 1000.0) * k
            return 1 - math.exp(-zeta * w0 * tt) * (
                math.cos(wd * tt) + (zeta * w0 / wd) * math.sin(wd * tt))
        if style == "drop":
            return 1 - pow(1 - k, 3)
        return k * k * (3 - 2 * k)

    def morph(self, a, b, opening=True):
        dur = (MORPH_MS if opening else COLLAPSE_MS) / 1000.0
        n = max(6, int(dur * 70))
        self.animating = True
        t0 = time.time()
        try:
            for i in range(n + 1):
                k = i / n
                e = max(0.0, min(1.0, self.ease(self.style, k, opening)))
                self.t = a + (b - a) * e
                self.paint(self.t)
                nxt = t0 + dur * (i + 1) / (n + 1)
                time.sleep(max(0.0, nxt - time.time()))
            self.t = b
            self.paint(b)
            el = time.time() - t0
            print('   动画 %s %s: %d 帧 / %.0f ms (目标 %.0f)' %
                  (self.style, '展开' if opening else '收起', n + 1, el * 1000, dur * 1000),
                  flush=True)
        finally:
            self.animating = False

    def show(self):
        if not ENABLED or not self.alive() or self.shown:
            return
        self.shown = True
        self.last_keep = time.time()
        # 每次弹出都重新读一遍动画样式 —— 控制台里改完不用重启
        self.style = self.state.get("anim") or "island"
        self.t = 0.0
        self.paint(0.0)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.morph(0.0, 1.0)
        self.last_keep = time.time()      # 停留时间从完全展开后起算，不含动画

    def hide(self):
        if not self.alive() or not self.shown:
            return
        self.shown = False
        self.morph(1.0, 0.0, opening=False)
        user32.ShowWindow(self.hwnd, SW_HIDE)

    def toggle(self):
        (self.hide if self.shown else self.show)()

    # ── 鼠标在不在胶囊上 ──
    def hovered(self):
        p = wt.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return (self.x + SHADOW <= p.x <= self.x + SHADOW + self.cw) and \
               (self.gap <= p.y <= self.gap + self.ch)

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_LBUTTONUP:
            # 点胶囊任意位置 = 把控制台（调图案那个界面）叫出来
            threading.Thread(target=open_console, daemon=True).start()
            return 0
        if msg == WM_MOUSEMOVE:
            self.last_keep = time.time()
            return 0
        if msg == WM_WTSSESSION_CHANGE and wparam in (WTS_SESSION_LOCK, WTS_SESSION_UNLOCK):
            locked = wparam == WTS_SESSION_LOCK
            print("[hud] 系统通知：%s" % ("锁屏" if locked else "解锁"), flush=True)
            try:
                import kbd
                kbd.KB.session_locked = locked
            except Exception as e:
                print("[hud] 传给 kbd 失败：%s" % e, flush=True)
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ── 后台：拉数据 + 动画心跳 + 自动收起 ──
    @staticmethod
    def _sig(st):
        """数据指纹。变了才重画内容层 —— 否则每 0.12 秒白画一张大图。"""
        return (st.get("skin"), st.get("anim"), st.get("petKind"), st.get("theme"),
                st.get("qwhen"), st.get("pet"),
                tuple((r.get("key"), round(r.get("pct", 0), 1), r.get("left"),
                       round(r.get("end") or 0)) for r in st.get("quota", [])),
                tuple((x.get("key"), x.get("state"), x.get("msg"), x.get("ago"))
                      for x in st.get("sessions", [])[:3]),
                str(st.get("pom")))

    def pump(self):
        last = 0.0
        while True:
            time.sleep(0.12)
            self.phase += 1
            now = time.time()
            if now - last > 1.5 and ENABLED:
                last = now
                try:
                    st = collect()
                    sig = self._sig(st)
                    with self.lock:
                        st["phase"] = self.phase
                        self.state = st
                        if sig != self.sig:
                            self.sig = sig
                            self.dirty = True
                except Exception:
                    pass
            if not self.shown or self.animating:
                continue
            # 露着的时候按宠物的节奏轻刷一下就够；动画期间绝不重画内容
            with self.lock:
                self.state["phase"] = self.phase
                self.dirty = True
            self.paint(self.t)
            if self.hovered():
                self.last_keep = now
            elif now - self.last_keep > (self.state.get("hold") or AUTO_HIDE_S):
                self.hide()


HUD = Hud()


def open_console():
    try:
        urllib.request.urlopen(
            urllib.request.Request(SERVER + "/open/console", data=b"{}",
                                   headers={"Content-Type": "application/json"}),
            timeout=3).read()
    except Exception:
        pass


# ── 低层鼠标钩子：接中键 ────────────────────────────────────────────
HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
_last_mb = [0.0]


def _proc(nCode, wParam, lParam):
    try:
        if not ENABLED:                           # Claude 模式关着：中键还给系统
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        if nCode == 0 and wParam == WM_MBUTTONDOWN:
            now = time.time()
            if now - _last_mb[0] > 0.35:          # 去抖，免得一次点击触发两下
                _last_mb[0] = now
                threading.Thread(target=HUD.toggle, daemon=True).start()
        if nCode == 0 and SWALLOW_MIDDLE and wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
            return 1                              # 吃掉，系统就不会开自动滚动
    except Exception:
        pass
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


_hook_ref = HOOKPROC(_proc)          # 必须留引用，否则被 GC 掉钩子就废了


def create_window():
    HUD.create()


def start_background():
    threading.Thread(target=HUD.pump, daemon=True).start()


def install_hook():
    hh = user32.SetWindowsHookExW(WH_MOUSE_LL, _hook_ref, None, 0)
    if not hh:
        print("!! 鼠标钩子注册失败，错误码", ctypes.get_last_error())
        return None
    return hh


def main():
    print("YOGO 胶囊启动中…")
    create_window()
    start_background()
    hh = install_hook()
    if not hh:
        return
    print("   按【鼠标中键】唤出 / 收起；点胶囊打开控制台")
    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnhookWindowsHookEx(hh)


if __name__ == "__main__":
    main()
