#!/usr/bin/env python3
"""
键盘传输层 —— 用 Windows HID API 直接跟 ATK YOGO 75 PRO 说话，不经过浏览器。

为什么要有这一层：之前键盘读写走的是网页里的 WebHID，带来一整类问题 ——
接收器一插拔授权就丢（Chrome 只给带序列号的设备持久化授权，这个接收器没有）、
页面被缓存、被遮挡就停止绘制、进程重启灯效中断。全都是"网页当驱动"的副作用。
现在设备由托盘进程直接持有，页面只当遥控器。

协议（从页面里原样搬过来的）：
    包 32 字节：[0xAA, cmd, off_lo, off_hi, len, seq, 0, 0, payload@8…]
    走 HID 时前面再加 1 字节 report id (0)，所以收发都是 33 字节
    回包 seq 在第 6 字节（含 report id），读回来的数据从第 9 字节起
    握手 16 / 读配置 20 / 写配置 21 / 逐键推流 46 / 结束推流 47 / 点阵 59

只用 ctypes，不装任何东西。
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

import hidprobe as H
import paths

C_HAND, C_READ, C_WRITE, C_SYNCLED, C_ENDSYNC, C_MATRIX = 16, 20, 21, 46, 47, 59
C_POWER = 48                 # 电量，从官方 HUB 的命令表里对出来的
C_END_SYNC_DOT = 61          # 结束点阵同步会话（官方 begin/end 都先发它）
# 同一张表里还有 C_RESET=49（恢复出厂）—— 记在这儿是为了永远别手滑发出去，
# 也是为什么探测协议时绝对不能盲扫命令号。
POWER_EVERY = 30.0           # 电量变化慢，没必要频繁占用 HID 通道
PKT = 33                     # report id + 32
NUL = bytes(1)               # 补零用；写 b"..." 转义在某些管道里会被吃掉
HAND_EVERY = 4.0             # 握手节流：4 秒内不重复
REPLY_TIMEOUT = 0.9          # 等回包；2.4G 转发有延迟，键盘刚醒更慢
REOPEN_EVERY = 2.0

# ── VIA 自定义灯效缓冲区（官方 EVISION_V2 的 id_set_light_buffer 路径）──
# 命令从官方 HUB 的 keymap 表里对出来：cmd 8/69 读、7/66 写、3/150/18 保存。
# 走 FF60/61 厂商口，report id 75（EVISION 数据用 report id 0，两者靠 report id 分流）。
VIA_UP, VIA_US = 0xFF60, 0x61
VIA_SIZE = 32
VIA_RID = 75                  # 官方 EVISION_V2 的 VIA 传输用 report id 0x4B
VIA_RID_FALLBACK = 0
VIA_GET_CUSTOM, VIA_GET_LAMPLIGHT = 8, 69
VIA_SET_CUSTOM, VIA_SET_LAMPLIGHT = 7, 66
VIA_SET_KBD, VIA_CUSTOM_ID, VIA_SAVE_LAMPLIGHT = 3, 150, 18
VIA_COLS = 16                 # YOGO 75 PRO：6 行 × 16 列（含空位）
VIA_ROWS = 6
VIA_TIMEOUT_MS = 600
DEBUG_RX = os.environ.get("YOGO_DEBUG_RX") == "1"

# ── 推流防线 ──────────────────────────────────────────────────────
# 上层（网页里的动画循环）出 bug 时会疯狂重推同一帧：轻则键盘灯乱闪，
# 重则把 HID 通道塞爆，连按键都上报不上来（真发生过）。所以这一层必须兜底：
#   · 内容去重：这一帧和上一帧一模一样就不写设备（静态画面反复推 = 闪）
#   · 硬限速：无论上层多疯，写设备的频率有上限
# 上限比正常动画帧率高一点，正常动画不受影响，只挡异常。
MIN_MATRIX_GAP = 0.16        # 点阵最快 ~6fps（36 个点，便宜）
MIN_KEYS_GAP = 0.5           # 键盘灯最快 2fps（84 颗灯，耗电大头，砍最狠）
SAME_MATRIX_HOLD = 1800.0    # 画面没变：半小时补推一次保活就够（图在设备里不会自己丢）
SAME_KEYS_HOLD = 3600.0      # 键盘灯静态画面：一小时一次
PACKET_GAP = 0.003           # 同一帧内包与包之间留一点空，别把 2.4G 链路的按键上报挤掉
TYPING_GUARD = True          # 最近有键鼠输入时，非必要的画面先让路
TYPING_GUARD_MS = 250
BATT_KEYS_OFF = 20           # 低于这个电量：不再推键盘灯
BATT_ALL_OFF = 8             # 低于这个：连点阵也停，把电留给打字
# 真正的耗电大头是「灯一直亮着」，不是推帧的频率 —— 停止推流并不会让灯灭，
# 最后一帧会一直留在屏上。所以省电必须主动熄屏：人离开键盘一段时间就全灭，
# 回来再把画面推回去。键盘自己的休眠是 900 秒，我们比它早一步。
IDLE_BLANK_S = 180.0         # 三分钟没碰键鼠：熄屏（锁屏则立刻）
OFF_REBLANK_S = 15.0         # 熄着的时候定期补一帧全黑：固件可能自己把存储画面重画回来


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
_u32.GetLastInputInfo.restype = wt.BOOL
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetTickCount64.restype = ctypes.c_ulonglong


_u32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_u32.OpenInputDesktop.restype = wt.HANDLE
_u32.CloseDesktop.argtypes = [wt.HANDLE]
DESKTOP_SWITCHDESKTOP = 0x0100


def locked():
    """Windows 锁屏了吗。锁屏时输入桌面切到 Winlogon，普通进程打不开它。
    UAC 弹窗的安全桌面也会让这个调用失败，所以调用方要去抖，别一次就当真。"""
    h = _u32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return True
    _u32.CloseDesktop(h)
    return False


def idle_secs():
    """距上次键盘/鼠标输入多少秒。GetLastInputInfo 给的是 32 位 tick，要自己处理回绕。"""
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not _u32.GetLastInputInfo(ctypes.byref(li)):
        # 查不到就当成「已经离开」：宁可多熄一次，也别因为一次查询失败整夜亮着。
        return float(IDLE_BLANK_S)
    now = _k32.GetTickCount64() & 0xFFFFFFFF
    d = (now - li.dwTime) & 0xFFFFFFFF
    return d / 1000.0


def recent_input(ms=None):
    """最近 ms 毫秒内有键鼠输入（GetLastInputInfo，鼠标移动也算）。

    这是键盘钩子没装时的兜底；正常走 recent_key_input()，只认键盘。
    """
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not _u32.GetLastInputInfo(ctypes.byref(li)):
        return False                   # 查不到就别妨碍推流
    now = _k32.GetTickCount64() & 0xFFFFFFFF
    d = (now - li.dwTime) & 0xFFFFFFFF
    return d < (TYPING_GUARD_MS if ms is None else ms)


_last_key = [0.0]


def note_key():
    """低级键盘钩子回调里调用：只记时间戳，不看键值、不拦按键。"""
    _last_key[0] = time.time()


def recent_key_input(ms=None):
    """最近 ms 毫秒内按过键盘。钩子没装时退回 GetLastInputInfo。"""
    t = _last_key[0]
    if t:
        return (time.time() - t) * 1000.0 < (TYPING_GUARD_MS if ms is None else ms)
    return recent_input(ms)


class ViaLink:
    """FF00/01 boot 接口上的 VIA 小通道，只用来读写自定义灯效缓冲区。

    官方 EVISION_V2 控制器用 VIA 的 get/set_light_buffer 把整张逐键 RGB 矩阵
    写进键盘；写一次之后切到 blMode=253，背光就不需要 cmd 46 逐帧推流了。
    这里只发读、写、保存三个命令，绝不碰 reset/OTP。
    """

    def __init__(self, transport=None):
        self.fh = None
        self.in_len = self.out_len = 33
        self.lock = threading.RLock()
        self.err = ""
        self.ok = None                 # None=没试过，True/False=探测结果
        self.rid_used = None           # 探测成功后记住能用的 report id
        self.pid = None                # 备用路径：只开和键盘同一个 PID 的 VIA 口
        self.transport = transport     # 主路径：复用键盘句柄，回包走 _read_loop

    def close(self):
        with self.lock:
            fh, self.fh = self.fh, None
            if fh is not None:
                try:
                    H.k32.CloseHandle(fh)
                except Exception:
                    pass

    def _open(self):
        if self.fh is not None:
            return True
        try:
            rows = [r for r in H.interfaces()
                    if r[1] == H.VID and r[3] == VIA_UP and r[4] == VIA_US
                    and (self.pid is None or r[2] == self.pid)]
        except Exception as e:
            self.err = "VIA 枚举失败：%s" % e
            return False
        if not rows:
            self.err = "没找到 VIA 接口"
            return False
        path, vid, pid, up, us, il, ol = rows[0]
        fh = H.k32.CreateFileW(path, H.GENERIC_READ | H.GENERIC_WRITE,
                               H.FILE_SHARE_READ | H.FILE_SHARE_WRITE, None,
                               H.OPEN_EXISTING, H.FILE_FLAG_OVERLAPPED, None)
        if fh == H.INVALID_HANDLE:
            self.err = "VIA 打不开：%d" % ctypes.get_last_error()
            return False
        H.hid.HidD_SetNumInputBuffers(fh, 64)
        self.fh = fh
        self.in_len, self.out_len = il or 64, ol or 64
        self.err = ""
        return True

    def _transact(self, pkt):
        """发一个 32 字节 VIA 包并等回包。失败返回 None。"""
        if self.transport is not None:
            return self.transport(pkt)     # 复用键盘句柄，回包由 _read_loop 投递
        with self.lock:
            if not self._open():
                return None
            body = bytes(pkt[:VIA_SIZE]).ljust(VIA_SIZE, NUL)
            first = self.rid_used if self.rid_used is not None else VIA_RID
            rids = [first] + [x for x in (VIA_RID, VIA_RID_FALLBACK) if x != first]
            for rid in rids:
                data = (bytes([rid]) + body)[:self.out_len].ljust(self.out_len, NUL)
                r = self._transact_once(data)
                if r is not None:
                    self.rid_used = rid
                    self.err = ""
                    return r
            return None

    def _transact_once(self, data):
        ev = H.k32.CreateEventW(None, True, False, None)
        if not ev:
            self.err = "VIA 建事件失败"
            return None
        try:
            ov = H.OVERLAPPED()
            ov.hEvent = ev
            H.k32.ResetEvent(ev)
            wbuf = (ctypes.c_ubyte * len(data))(*data)
            ok = H.k32.WriteFile(self.fh, wbuf, len(data), None, ctypes.byref(ov))
            if not ok and ctypes.get_last_error() != H.ERROR_IO_PENDING:
                self.err = "VIA 写失败：%d" % ctypes.get_last_error()
                return None
            if H.k32.WaitForSingleObject(ev, 800) != H.WAIT_OBJECT_0:
                H.k32.CancelIoEx(self.fh, ctypes.byref(ov))
                self.err = "VIA 写超时"
                return None
            buf = (ctypes.c_ubyte * self.in_len)()
            ov2 = H.OVERLAPPED()
            ov2.hEvent = ev
            H.k32.ResetEvent(ev)
            ok = H.k32.ReadFile(self.fh, buf, self.in_len, None, ctypes.byref(ov2))
            if not ok and ctypes.get_last_error() != H.ERROR_IO_PENDING:
                self.err = "VIA 读失败：%d" % ctypes.get_last_error()
                return None
            if H.k32.WaitForSingleObject(ev, VIA_TIMEOUT_MS) != H.WAIT_OBJECT_0:
                H.k32.CancelIoEx(self.fh, ctypes.byref(ov2))
                self.err = "VIA 读超时"
                return None
            n = wt.DWORD()
            H.k32.GetOverlappedResult(self.fh, ctypes.byref(ov2), ctypes.byref(n), False)
            return bytes(buf[:n.value])
        finally:
            H.k32.CloseHandle(ev)

    @staticmethod
    def _strip(resp, cmd):
        if resp and len(resp) > 1 and resp[0] in (VIA_RID_FALLBACK, VIA_RID) \
                and resp[1] == cmd:
            return resp[1:]            # 某些驱动会把 report id 留在缓冲里
        return resp

    def read_chunk(self, row, col, num):
        r = self._transact(bytes([VIA_GET_CUSTOM, VIA_GET_LAMPLIGHT,
                                  row & 255, col & 255, num & 255]))
        r = self._strip(r, VIA_GET_CUSTOM)
        if not r or len(r) < 5 + num * 3 or r[0] != VIA_GET_CUSTOM \
                or r[1] != VIA_GET_LAMPLIGHT:
            if r:
                self.err = "VIA 回包不匹配：%s" % r[:8].hex(" ")
            return None
        return r[5:5 + num * 3]

    def write_chunk(self, row, col, num, colors):
        pkt = bytes([VIA_SET_CUSTOM, VIA_SET_LAMPLIGHT,
                     row & 255, col & 255, num & 255]) + bytes(colors)
        r = self._transact(pkt)
        r = self._strip(r, VIA_SET_CUSTOM)
        if not r:
            return False
        return r[0] in (VIA_SET_CUSTOM, VIA_SET_LAMPLIGHT)

    def save(self):
        r = self._transact(bytes([VIA_SET_KBD, VIA_CUSTOM_ID, VIA_SAVE_LAMPLIGHT]))
        r = self._strip(r, VIA_SET_KBD)
        return bool(r) and r[0] == VIA_SET_KBD

    def read_led(self, rows=VIA_ROWS, cols=VIA_COLS):
        with self.lock:
            out = []
            for r in range(rows):
                line = []
                for c in range(0, cols, 8):
                    n = min(8, cols - c)
                    chunk = self.read_chunk(r, c, n)
                    if chunk is None:
                        self.ok = False
                        return None
                    line.extend([list(chunk[i:i + 3]) for i in range(0, n * 3, 3)])
                out.append(line)
            self.ok = True
            return out

    def write_led(self, colors, rows=VIA_ROWS, cols=VIA_COLS):
        with self.lock:
            for r in range(rows):
                line = colors[r] if r < len(colors) else []
                for c in range(0, cols, 8):
                    n = min(8, cols - c)
                    chunk = []
                    for i in range(n):
                        rgb = line[c + i] if c + i < len(line) else [0, 0, 0]
                        rgb = list(rgb) + [0, 0, 0]
                        chunk.extend([int(x) & 255 for x in rgb[:3]])
                    if not self.write_chunk(r, c, n, chunk):
                        self.ok = False
                        return False
            if not self.save():
                self.ok = False
                return False
            self.ok = True
            return True


class Keyboard:
    def __init__(self):
        self.fh = None
        self.path = None
        self.product = ""
        self.in_len = self.out_len = PKT
        self.lock = threading.RLock()         # 一次只走一条命令（open 里会重入）
        self.seq = 0
        self.pending = {}                     # seq -> [event, data]
        self.last_hand = 0.0
        self.last_open_try = 0.0
        self.awake = False                    # 最近一次读配置有没有应答
        self.err = ""
        self.reader = None
        self.running = False
        self.ev_write = None
        self._keys_last = self._matrix_last = None    # 上一帧内容，用来去重
        self._keys_t = self._matrix_t = 0.0
        self.dropped = 0                              # 被去重/限速挡掉的帧数
        self.wired = False                            # 走的是键盘直连口还是接收器
        self._pw = None                               # 上一次读到的电量
        self._pw_t = 0.0
        self._matrix_pending = self._keys_pending = None   # 被限速挡下、待补发的那一帧
        self._flusher = None
        self.blanked = False                          # 空闲/锁屏熄屏中
        self.locked = False                           # 综合判定：锁屏了
        self.session_locked = None                    # 系统 WTS 通知给的权威值（None=还没收到过）
        self._mx_off = self._ky_off = False           # 点阵 / 键盘灯此刻是否被我们熄了
        self._saved_mx = self._saved_ky = None        # 熄着时上层最新想显示的画面
        self._off_reblank = 0                         # 熄屏期间补黑帧的次数（排查用）
        self._off_log_t = 0.0
        self._idler = None
        self.via_event = threading.Event()
        self.via_reply = None
        self.via_rid_used = None
        self.via = ViaLink(self.via_transact)         # FF60 上的自定义灯效缓冲区
        self._last_cfg = None                         # 最近一次读到的 64 字节配置
        self._anim = None                             # 原生帧序列播放器

    # ── 打开 / 关闭 ────────────────────────────────────────────────
    def connected(self):
        return self.fh is not None

    def _try_path(self, path, il, ol):
        """打开一个口并起读线程。成功只代表口开了，不代表键盘醒着。"""
        fh = H.k32.CreateFileW(path, H.GENERIC_READ | H.GENERIC_WRITE,
                               H.FILE_SHARE_READ | H.FILE_SHARE_WRITE, None,
                               H.OPEN_EXISTING, H.FILE_FLAG_OVERLAPPED, None)
        if fh == H.INVALID_HANDLE:
            self.err = "打不开：%d" % ctypes.get_last_error()
            return False
        H.hid.HidD_SetNumInputBuffers(fh, 64)
        self.fh, self.path = fh, path
        self.in_len, self.out_len = il or PKT, ol or PKT
        self.ev_write = H.k32.CreateEventW(None, True, False, None)
        try:
            buf = ctypes.create_unicode_buffer(128)
            H.hid.HidD_GetProductString.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
            if H.hid.HidD_GetProductString(fh, buf, 256):
                self.product = buf.value
        except Exception:
            pass
        # 接收器报的产品名带 Dongle，键盘直连（插线）报的不带 —— 用这个分辨
        # 走的是哪条路。有线基本就等于在充电，是判断充电状态的主要依据。
        wired = "dongle" not in (self.product or "").lower()
        if wired != self.wired:             # 换了路：上一条路的电量和 VIA 能力都作废
            self._pw, self._pw_t = None, 0.0
            self.via.ok = None
            self.via.rid_used = None
            self.via.err = ""
        self.wired = wired
        self.err = ""
        self.running = True
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        if self._idler is None or not self._idler.is_alive():
            self._idler = threading.Thread(target=self._idle_loop, daemon=True)
            self._idler.start()
        return True

    def ping(self):
        """握手 + 读一小段配置，只试一次。用来分辨哪个口后面真挂着键盘。"""
        try:
            self.handshake(force=True)
            return self.send(C_READ, 0, b"", 24) is not None
        except Exception:
            return False

    def open(self):
        """挑出后面真的挂着键盘的那个 FF60/61 口。

        有线和 2.4G 会各出一个 FF60/61，长得一模一样，但只有一个后面有键盘。
        以前这里直接拿 rows[0]，插着线也可能选中那个没人应答的接收器。
        所以改成挨个试握手，谁回包就用谁。
        """
        now = time.time()
        if now - self.last_open_try < REOPEN_EVERY:
            return False
        self.last_open_try = now
        try:
            rows = [r for r in H.interfaces() if r[1] == H.VID and r[3] == 0xFF60]
        except Exception as e:
            self.err = "枚举失败：%s" % e
            return False
        if not rows:
            self.err = "没找到键盘或接收器"
            return False
        rows.sort(key=lambda r: r[0] != self.path)     # 上次能用的口先试
        spare = None
        for path, vid, pid, up, us, il, ol in rows:
            self.via.pid = pid
            if not self._try_path(path, il, ol):
                continue
            if self.ping():
                self.awake = True
                self.err = ""
                return True
            if spare is None:
                spare = (path, il, ol)                 # 都不应答的话留个兜底
            self.close()
        if spare:                                      # 占住一个口，等键盘醒过来
            self._try_path(*spare)
            self.awake = False
            self.err = "键盘没应答（关机 / 不在 2.4G 档 / 没电）"
            return True
        return False

    def close(self):
        self.running = False
        self.stop_frames()
        self.via.close()
        fh, self.fh = self.fh, None
        if fh is not None:
            try:
                H.k32.CancelIoEx(fh, None)
            except Exception:
                pass
            H.k32.CloseHandle(fh)
        if self.ev_write:
            H.k32.CloseHandle(self.ev_write)
            self.ev_write = None
        self.awake = False

    # ── 收：一个线程一直读，按 seq 分发 ──────────────────────────────
    def _read_loop(self):
        fh = self.fh
        ev = H.k32.CreateEventW(None, True, False, None)
        try:
            while self.running and self.fh == fh:
                buf = (ctypes.c_ubyte * self.in_len)()
                ov = H.OVERLAPPED()
                ov.hEvent = ev
                H.k32.ResetEvent(ev)
                ok = H.k32.ReadFile(fh, buf, self.in_len, None, ctypes.byref(ov))
                if not ok and ctypes.get_last_error() != H.ERROR_IO_PENDING:
                    self.err = "读失败：%d（接收器拔了？）" % ctypes.get_last_error()
                    break
                w = H.k32.WaitForSingleObject(ev, 1000)
                n = wt.DWORD()
                if w != H.WAIT_OBJECT_0:
                    H.k32.CancelIoEx(fh, ctypes.byref(ov))
                    H.k32.GetOverlappedResult(fh, ctypes.byref(ov), ctypes.byref(n), True)
                    continue
                H.k32.GetOverlappedResult(fh, ctypes.byref(ov), ctypes.byref(n), False)
                data = bytes(buf[:n.value])
                if DEBUG_RX:
                    print("[rx] %s" % data.hex(" "), flush=True)
                if len(data) >= 2:
                    if data[0] == VIA_RID or (
                            data[0] == VIA_RID_FALLBACK
                            and data[1] in (VIA_GET_CUSTOM, VIA_SET_CUSTOM, VIA_SET_KBD)):
                        self.via_reply = data
                        self.via_event.set()
                        continue
                    if data[0] in (VIA_GET_CUSTOM, VIA_SET_CUSTOM, VIA_SET_KBD) \
                            and data[1] in (VIA_GET_LAMPLIGHT, VIA_SET_LAMPLIGHT, VIA_CUSTOM_ID):
                        self.via_reply = data          # 驱动把 report id 去掉了
                        self.via_event.set()
                        continue
                if len(data) >= 7:
                    slot = self.pending.get(data[6])
                    if slot:
                        slot[1] = data
                        slot[0].set()
        finally:
            H.k32.CloseHandle(ev)
            if self.fh == fh:                 # 是我们这条连接坏了，不是主动 close
                self.close()

    # ── 发 ──────────────────────────────────────────────────────────
    def _write(self, pkt):
        buf = (ctypes.c_ubyte * self.out_len)()
        for i, b in enumerate(pkt[:self.out_len]):
            buf[i] = b
        ov = H.OVERLAPPED()
        ov.hEvent = self.ev_write
        H.k32.ResetEvent(self.ev_write)
        ok = H.k32.WriteFile(self.fh, buf, self.out_len, None, ctypes.byref(ov))
        if not ok and ctypes.get_last_error() != H.ERROR_IO_PENDING:
            return False
        if H.k32.WaitForSingleObject(self.ev_write, 800) != H.WAIT_OBJECT_0:
            H.k32.CancelIoEx(self.fh, ctypes.byref(ov))
            n = wt.DWORD()
            H.k32.GetOverlappedResult(self.fh, ctypes.byref(ov), ctypes.byref(n), True)
            return False
        return True

    def send(self, cmd, off=0, payload=b"", ln=None, wait=True):
        """发一条命令。wait=True 时等回包，超时返回 None；写不出去抛异常。"""
        with self.lock:
            if self.fh is None and not self.open():
                raise IOError(self.err or "未连接")
            self.seq = self.seq % 250 + 1
            my = self.seq
            pkt = bytearray(PKT)
            pkt[0] = 0
            pkt[1] = 0xAA
            pkt[2] = cmd
            pkt[3] = off & 255
            pkt[4] = (off >> 8) & 255
            pkt[5] = ln if ln is not None else len(payload)
            pkt[6] = my
            pl = bytes(payload)[:24]
            pkt[9:9 + len(pl)] = pl
            slot = [threading.Event(), None]
            if wait:
                self.pending[my] = slot
            try:
                if not self._write(pkt):
                    self.err = "写不出去（err %d）" % ctypes.get_last_error()
                    self.close()
                    raise IOError(self.err)
                if not wait:
                    return None
                if slot[0].wait(REPLY_TIMEOUT):
                    return slot[1]
                return None
            finally:
                self.pending.pop(my, None)

    # ── 协议层 ─────────────────────────────────────────────────────
    def handshake(self, force=False, wait=True):
        now = time.time()
        if not force and now - self.last_hand < HAND_EVERY:
            return
        self.send(C_HAND, 0, b"", 0, wait=wait)
        self.last_hand = now

    def power(self, force=False):
        """电量 + 充电状态。返回 {"pct":int, "charging":bool, "wired":bool, "flag":int}。

        协议是从官方 HUB 的 bundle 里对出来的：握手之后发 48，回包数据区
        第 0 字节是百分比（大于 100 截到 100），第 1 字节是供电状态。
        官方代码判「== 2 才是充电」，但这把键盘实测：
            无线放电 = 0，插着线充电 = 1（电量一路往上爬）。
        所以按「非 0 且没充满」算充电中，再拿「走的是有线口」兜一下底。
        不要再加「电量涨过就算充电」这类猜测 —— 它拔线后要滞后三分钟才反应过来。
        """
        now = time.time()
        with self.lock:
            if not force and self._pw and now - self._pw_t < POWER_EVERY:
                return self._pw
            try:
                self.handshake()
                r = self.send(C_POWER, 0, b"", 0)
            except Exception:
                return self._pw
            if not r or len(r) < 11:
                return self._pw                 # 读不到就沿用上一次，别把界面清空
            pct, flag = min(100, r[9]), r[10]
            if not self._pw or self._pw["pct"] != pct:
                print("[batt] %d%%  %s  %s" % (pct, "有线" if self.wired else "无线",
                                             "熄屏中" if self.blanked else "亮着"), flush=True)
            self._pw = {"pct": pct,
                        "charging": bool((flag != 0 or self.wired) and pct < 100),
                        "wired": self.wired, "flag": flag}
            self._pw_t = now
            return self._pw

    def read_config(self):
        """64 字节配置。三次重试并强制重新握手 —— 键盘刚醒时第一次多半没人应答。"""
        last = None
        for attempt in range(3):
            if attempt:
                time.sleep(0.22)
                self.last_hand = 0
            with self.lock:
                self.handshake()
                cfg = bytearray(64)
                ok = True
                for off, ln in ((0, 24), (24, 24), (48, 16)):
                    r = self.send(C_READ, off, b"", ln)
                    if not r or len(r) < 9 + ln:
                        ok = False
                        last = "读配置超时"
                        break
                    cfg[off:off + ln] = r[9:9 + ln]
                if ok:
                    self.awake = True
                    self.err = ""
                    self._last_cfg = bytes(cfg)
                    self._backup(bytes(cfg))
                    return bytes(cfg)
        self.awake = False
        self.err = last or "读配置超时"
        raise TimeoutError(self.err)

    # ── 配置备份 ───────────────────────────────────────────────────
    # write_config 是读-改-写，写坏了没处找原始值。第一次成功读到配置时
    # 落一份到 config.bak，之后任何时候都能 restore_config() 推回去。
    BAK = paths.data("config.bak")

    def _backup(self, cfg):
        try:
            if len(cfg) == 64 and not os.path.exists(self.BAK):
                with open(self.BAK, "wb") as f:
                    f.write(cfg)
        except Exception:
            pass

    def restore_config(self):
        """把出厂那次读到的配置原样写回去。"""
        with open(self.BAK, "rb") as f:
            cfg = f.read()
        if len(cfg) != 64:
            raise ValueError("备份不是 64 字节")
        self.write_config(cfg)
        return cfg

    def write_config(self, cfg):
        with self.lock:
            self.handshake()
            self.send(C_ENDSYNC, 0, b"", 0)
            for off, ln in ((0, 24), (24, 24), (48, 16)):
                self.send(C_WRITE, off, bytes(cfg[off:off + ln]))
            self._last_cfg = bytes(cfg)
            self._reblank_mx()          # 写配置可能让固件把点阵重新点亮

    def patch_config(self, patch):
        """读-改-写：patch 是 {字节偏移: 值}，只动指定字节。"""
        with self.lock:
            cfg = bytearray(self.read_config())
            for k, v in patch.items():
                cfg[int(k)] = max(0, min(255, int(v)))
            self.write_config(cfg)
            self._last_cfg = bytes(cfg)
            return bytes(cfg)

    # ── 省电闸门 ───────────────────────────────────────────────────
    def budget(self):
        """这一刻允许推多快、推不推。返回
        (点阵最小间隔, 键盘灯最小间隔, 点阵保活, 键盘灯保活, 可推键盘灯?, 可推点阵?)

        推不推由熄灯状态决定（_ky_off / _mx_off，见 _idle_loop），
        速度只有一套省电档，不分有线无线。
        """
        return (MIN_MATRIX_GAP, MIN_KEYS_GAP, SAME_MATRIX_HOLD, SAME_KEYS_HOLD,
                not self._ky_off, not self._mx_off)

    def _want_off(self):
        """现在点阵 / 键盘灯各自该不该灭。三个理由：锁屏、离开太久、电量低。"""
        away = self.locked or idle_secs() >= IDLE_BLANK_S
        pw = self._pw
        low_keys = low_all = False
        if pw and not pw.get("charging"):   # 读不到电量 / 在充电：不因电量降级
            low_keys = pw["pct"] <= BATT_KEYS_OFF
            low_all = pw["pct"] <= BATT_ALL_OFF
        return (away or low_all), (away or low_keys), away

    def _idle_loop(self):
        """该灭的时候真灭，该亮的时候把最新的画面推回去。

        「灭」一定是推一帧全黑，**不是停止推送** —— 停推的话最后一帧会一直亮着，
        等于没省电（低电量降级第一版就栽在这）。也不退出推流态：之前用
        close_sync_led 退出，固件会把点阵从存储里的图重画一遍，锁了屏点阵还亮。

        灭着的时候还要**定期补一帧全黑**：固件在一段时间收不到点阵帧后，会自己
        退出同步态、把存储的画面重画回来；熄屏期间上层改点阵配置（进自定义模式、
        切主题、页面重连）也会把屏重新点亮。只在刚熄灭那一刻发一帧，管不住整夜。

        灭着的时候上层推来的画面不丢，记成「最新意图」（push_* 里写 _saved_*），
        恢复时推的是它 —— 期间换了主题，回来看到的就是新主题。

        锁屏判定两路：系统 WTS 通知（hud 窗口收到 LOCK/UNLOCK 写 session_locked，
        权威）；没收到过通知时退回 OpenInputDesktop 轮询（要去抖，UAC 也会让它失败）。

        这个循环跟进程走，不跟连接走：连接断开时跳过，重连后接着用。以前它绑在
        `running` 上，close() 会让它退出，极端情况下重连没把它拉起来，整晚都没人熄屏。
        """
        lock_hits = 0
        last_off = last_ky_off = 0.0
        while True:
            time.sleep(2.0)
            try:
                if not self.connected():
                    self.blanked = False
                    last_off = last_ky_off = 0.0
                    continue
                lock_hits = lock_hits + 1 if locked() else 0
                if self.session_locked is not None:
                    self.locked = self.session_locked
                else:
                    self.locked = lock_hits >= 2
                try:
                    self.power()                # 自带 30 秒缓存，不会真的每 2 秒问一次
                except Exception:
                    pass
                mx_off, ky_off, away = self._want_off()
                now = time.time()
                why = "锁屏" if self.locked else ("离开" if away else "电量低")
                with self.lock:
                    if mx_off and not self._mx_off:
                        if self._matrix_last and any(self._matrix_last):
                            self._saved_mx = self._matrix_last
                        self._mx_off = True
                        self._send_matrix(bytes(108), now)
                        last_off = now
                        print("[kbd] 点阵熄灭（%s）" % why, flush=True)
                    elif not mx_off and self._mx_off:
                        self._mx_off = False
                        last_off = 0.0
                        if self._saved_mx:
                            self._send_matrix(self._saved_mx, now)
                        else:
                            self._matrix_last = None
                        self._saved_mx = None
                        print("[kbd] 点阵恢复", flush=True)
                    elif mx_off and self._mx_off and now - last_off >= OFF_REBLANK_S:
                        self._send_matrix(bytes(108), now)
                        last_off = now
                        self._off_reblank += 1
                    if ky_off and not self._ky_off:
                        if self._keys_last and any(self._keys_last):
                            self._saved_ky = self._keys_last
                        self._ky_off = True
                        if self._keys_last is not None:     # 背光在我们手里才去灭
                            self._send_keys(bytes(168), now)
                            last_ky_off = now
                        print("[kbd] 键盘灯熄灭（%s）" % why, flush=True)
                    elif not ky_off and self._ky_off:
                        self._ky_off = False
                        last_ky_off = 0.0
                        if self._saved_ky:
                            self._send_keys(self._saved_ky, now)
                        self._saved_ky = None
                        print("[kbd] 键盘灯恢复", flush=True)
                    elif (ky_off and self._ky_off and self._keys_last is not None
                          and now - last_ky_off >= OFF_REBLANK_S):
                        self._send_keys(bytes(168), now)
                        last_ky_off = now
                        self._off_reblank += 1
                self.blanked = self._mx_off and self._ky_off
            except Exception as e:
                print("[kbd] 熄灯循环异常：%s" % e, flush=True)

    def _ensure_flusher(self):
        """有待补发的帧时才起补发线程，发完自己退出。"""
        if self._flusher is not None and self._flusher.is_alive():
            return
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()

    def _flush_loop(self):
        while True:
            time.sleep(0.03)
            if self._matrix_pending is None and self._keys_pending is None:
                return
            try:
                mgap, kgap = self.budget()[0], self.budget()[1]
                with self.lock:
                    if TYPING_GUARD and recent_key_input():
                        continue            # 打字优先，补发帧先等着
                    now = time.time()
                    p = self._matrix_pending
                    if p is not None and now - self._matrix_t >= mgap:
                        self._send_matrix(p, now)
                    now = time.time()
                    p = self._keys_pending
                    if p is not None and now - self._keys_t >= kgap:
                        self._send_keys(p, now)
            except Exception:
                self._matrix_pending = self._keys_pending = None
                return

    def _send_matrix(self, flat, now):
        with self.lock:
            self._matrix_pending = None
            # 先占住时间槽：并发进来的一律走补发，不能各自再发一整帧
            self._matrix_t = now
            self.handshake(wait=False)
            for i in range(5):
                self.send(C_MATRIX, i * 24, flat[i * 24:i * 24 + 24].ljust(24, NUL), wait=False)
                if PACKET_GAP:
                    time.sleep(PACKET_GAP)
            self._matrix_last = flat

    def _reblank_mx(self):
        """熄屏期间又被写入了点阵配置：立刻把全黑帧盖回去。

        写配置会碰 LedTFTOnOff / dsMode，固件收到后可能把存储的画面重画一遍；
        只靠 _idle_loop 在「刚熄灭」那一刻发的一帧，屏会被重新点亮并一直亮到下次熄屏。
        """
        if not self._mx_off:
            return
        try:
            self._send_matrix(bytes(108), time.time())
            self._off_reblank += 1
            now = time.time()
            if now - self._off_log_t > 60:
                self._off_log_t = now
                print("[kbd] 熄屏中点阵配置被改写，已重新盖黑", flush=True)
        except Exception:
            pass

    def _send_keys(self, data, now):
        with self.lock:
            self._keys_pending = None
            self._keys_t = now
            self.handshake(wait=False)
            for off in range(0, 168, 24):
                self.send(C_SYNCLED, off, data[off:off + 24], wait=False)
                if PACKET_GAP:
                    time.sleep(PACKET_GAP)
            self._keys_last = data

    def push_matrix(self, flat, force=False):
        """108 字节 RGB（6×6×3），5 包，每包 24 字节。

        限速时**不丢帧，而是留着补发**。动画丢一帧无所谓，但模式切换（比如切到
        专注要全黑）也是一次推送，丢了就永远留在上一张图上 —— 表现成「点了没反应」。
        force=True 时绕过打字保护，给模式切换这类必须立刻生效的帧用。
        """
        flat = bytes(flat)[:108].ljust(108, NUL)
        with self.lock:
            gap, _, hold, _, _, allow = self.budget()
            if not allow:                       # 灭着：不推，但记住这是最新想显示的
                self._matrix_pending = None
                self._saved_mx = flat if any(flat) else None
                self.dropped += 1
                return False
            if TYPING_GUARD and not force and recent_input():
                self._matrix_pending = flat     # 正在打字：先攒着，键停 250ms 再补
                self.dropped += 1
                self._ensure_flusher()
                return False
            now = time.time()
            if flat == self._matrix_last and now - self._matrix_t < hold:
                self._matrix_pending = None     # 内容一样，没什么可补的
                self.dropped += 1
                return False
            if now - self._matrix_t < gap:
                self._matrix_pending = flat     # 太快了：留着，间隔够了补发
                self.dropped += 1
                self._ensure_flusher()
                return False
            self._send_matrix(flat, now)
            return True

    def push_keys(self, rgb565, force=False):
        """168 字节 RGB565 小端（84 颗灯），7 包。前提是背光模式已经是 254。

        键盘灯是耗电大头（84 颗 vs 点阵的 36 点），所以低电量时先撤的是它。
        同样是限速留着补发，不丢。"""
        data = bytes(rgb565)[:168].ljust(168, NUL)
        with self.lock:
            _, gap, _, hold, allow, _ = self.budget()
            if not allow:                       # 灭着：不推，但记住这是最新想显示的
                self._keys_pending = None
                self._saved_ky = data if any(data) else None
                self.dropped += 1
                return False
            if TYPING_GUARD and not force and recent_input():
                self._keys_pending = data
                self.dropped += 1
                self._ensure_flusher()
                return False
            now = time.time()
            if data == self._keys_last and now - self._keys_t < hold:
                self._keys_pending = None
                self.dropped += 1
                return False
            if now - self._keys_t < gap:
                self._keys_pending = data
                self.dropped += 1
                self._ensure_flusher()
                return False
            self._send_keys(data, now)
            return True

    def forget_keys(self):
        """背光交回固件（模式不是 254）时调用：忘掉手里那帧灯。

        否则熄屏 / 恢复会把「关背光之前」的最后一帧主题配色推回去 ——
        逐键推流不管背光模式是不是 0，照亮。表现就是「关了背光，过一会儿又亮了」。"""
        self._keys_last = self._keys_pending = self._saved_ky = None

    def stop_keys(self):
        self.send(C_ENDSYNC, 0, b"", 0, wait=False)
        self.forget_keys()

    # ── 点阵屏模式 / 会话语义 ─────────────────────────────────────
    def end_sync_dot(self):
        """结束点阵同步会话。官方在进入/退出自定义点阵前都会发一次。"""
        self.send(C_END_SYNC_DOT, 0, b"", 0, wait=False)

    def set_dot_mode(self, mode, brightness=None, speed=None, colorful=None,
                     color_index=None, color=None, sync=True):
        """mode: 0-12 内置效果，6=自定义推帧；speed 是 0-6 的 UI 值。"""
        if sync:
            try:
                self.end_sync_dot()
                time.sleep(0.05)
            except Exception:
                pass
        patch = {29: 0, 30: int(mode) & 255}   # 29=LedTFTOnOff，官方设 0 表示启用
        if brightness is not None:
            patch[31] = max(0, min(255, int(brightness)))
        if speed is not None:
            patch[32] = max(0, min(6, 6 - int(speed)))
        if colorful is not None:
            patch[33] = 1 if colorful else 0
        if color_index is not None:
            patch[34] = max(0, min(255, int(color_index)))
        if color is not None:
            rgb = list(color) + [0, 0, 0]
            patch[35], patch[36], patch[37] = [max(0, min(255, int(x))) for x in rgb[:3]]
        cfg = self.patch_config(patch)
        self._last_cfg = bytes(cfg)
        return cfg

    def dot_config(self):
        if self._last_cfg is None:
            self.read_config()
        c = self._last_cfg
        if not c:
            return {}
        return {"mode": c[30], "brightness": c[31],
                "speed": max(0, min(6, 6 - c[32])), "colorful": c[33],
                "colorIndex": c[34], "color": [c[35], c[36], c[37]]}

    # ── VIA 事务（复用 FF60 句柄，report id 75 / 回退 0）─────────────
    def via_transact(self, pkt):
        with self.lock:
            if self.fh is None and not self.open():
                return None
            rid = self.via_rid_used if self.via_rid_used is not None else VIA_RID
            rids = [rid] + [x for x in (VIA_RID, VIA_RID_FALLBACK) if x != rid]
            for r in rids:
                self.via_reply = None
                self.via_event.clear()
                data = (bytes([r]) + bytes(pkt[:VIA_SIZE]).ljust(VIA_SIZE, NUL))
                data = data[:self.out_len].ljust(self.out_len, NUL)
                if DEBUG_RX:
                    print("[via tx] rid=%d %s" % (r, data[:8].hex(" ")), flush=True)
                if not self._write(data):
                    continue
                if self.via_event.wait(VIA_TIMEOUT_MS / 1000.0):
                    self.via_rid_used = r
                    if DEBUG_RX:
                        print("[via rx] %s" % self.via_reply.hex(" "), flush=True)
                    return self.via_reply
            self.via.err = "VIA 读超时"
            return None

    # ── 自定义逐键灯效缓冲区（写一次，设备自己显示）────────────────
    def read_custom_led(self):
        return self.via.read_led()

    def write_custom_led(self, colors):
        """colors: 6×16 RGB。成功返回 True；失败时调用方回退到 cmd 46。"""
        # 已知这条连接不支持 VIA（例如 2.4G 接收器）：直接回退，别再等超时
        if self.via.ok is False:
            return False
        # 先读一次确认 VIA 通道真的通，再写；避免在错误的报告格式上盲写
        if self.via.ok is None and self.via.read_led() is None:
            return False
        if not self.via.write_led(colors):
            return False
        try:
            self.patch_config({2: 253})       # 253 = 自定义灯效缓冲区
        except Exception:
            return False
        self.forget_keys()
        self._keys_last = None
        return True

    # ── 原生帧序列播放器（单帧/多帧都走这里，统一串行）────────────
    def play_frames(self, frames, fps=6, loop=True):
        frames = [bytes(f)[:108].ljust(108, NUL) for f in (frames or []) if f]
        self.stop_frames()
        if not frames:
            return False
        anim = {"frames": frames, "fps": max(1.0, min(6.0, float(fps))),
                "loop": bool(loop), "stop": False}
        self._anim = anim
        threading.Thread(target=self._anim_loop, args=(anim,), daemon=True).start()
        return True

    def stop_frames(self):
        a, self._anim = self._anim, None
        if a:
            a["stop"] = True

    def _anim_loop(self, a):
        i, nxt = 0, time.time()
        while not a["stop"] and self._anim is a:
            try:
                self.push_matrix(a["frames"][i])
            except Exception:
                break
            i += 1
            if i >= len(a["frames"]):
                if not a["loop"]:
                    break
                i = 0
            nxt += 1.0 / a["fps"]
            time.sleep(max(0.0, nxt - time.time()))

    def status(self):
        return {"connected": self.connected(), "awake": self.awake,
                "product": self.product, "err": self.err, "dropped": self.dropped,
                "wired": self.wired, "power": self._pw,
                "blanked": self.blanked, "idle": round(idle_secs()), "locked": self.locked,
                "off_reblank": self._off_reblank,
                "via": self.via.ok, "via_err": self.via.err, "anim": bool(self._anim),
                "dot": self.dot_config() if self._last_cfg is not None else None}


KB = Keyboard()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    kb = KB
    print("打开：", kb.open(), kb.status())
    try:
        cfg = kb.read_config()
        print("配置读到 %d 字节：blMode=%d dsMode=%d dsBright=%d" % (len(cfg), cfg[2], cfg[30], cfg[31]))
        # 点阵屏画一个绿色的勾（确认通了）
        g = bytearray(108)
        for r, c in ((1, 4), (2, 5), (3, 4), (4, 3), (5, 2), (4, 1)):
            i = (r * 6 + c) * 3
            g[i:i + 3] = bytes((30, 220, 80))
        kb.patch_config({30: 6, 31: 90})
        kb.push_matrix(g)
        print("推了一帧绿勾")
    except Exception as e:
        print("失败：", e)
    time.sleep(0.5)
    kb.close()
