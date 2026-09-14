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

C_HAND, C_READ, C_WRITE, C_SYNCLED, C_ENDSYNC, C_MATRIX = 16, 20, 21, 46, 47, 59
C_POWER = 48                 # 电量，从官方 HUB 的命令表里对出来的
# 同一张表里还有 C_RESET=49（恢复出厂）—— 记在这儿是为了永远别手滑发出去，
# 也是为什么探测协议时绝对不能盲扫命令号。
POWER_EVERY = 30.0           # 电量变化慢，没必要频繁占用 HID 通道
PKT = 33                     # report id + 32
NUL = bytes(1)               # 补零用；写 b"..." 转义在某些管道里会被吃掉
HAND_EVERY = 4.0             # 握手节流：4 秒内不重复
REPLY_TIMEOUT = 0.9          # 等回包；2.4G 转发有延迟，键盘刚醒更慢
REOPEN_EVERY = 2.0

# ── 推流防线 ──────────────────────────────────────────────────────
# 上层（网页里的动画循环）出 bug 时会疯狂重推同一帧：轻则键盘灯乱闪，
# 重则把 HID 通道塞爆，连按键都上报不上来（真发生过）。所以这一层必须兜底：
#   · 内容去重：这一帧和上一帧一模一样就不写设备（静态画面反复推 = 闪）
#   · 硬限速：无论上层多疯，写设备的频率有上限
# 上限比正常动画帧率高一点，正常动画不受影响，只挡异常。
MIN_MATRIX_GAP = 0.16        # 点阵最快 ~6fps（36 个点，便宜）
MIN_KEYS_GAP = 0.5           # 键盘灯最快 2fps（84 颗灯，耗电大头，砍最狠）
SAME_MATRIX_HOLD = 600.0     # 画面没变：十分钟补推一次保活就够（图在设备里不会自己丢）
SAME_KEYS_HOLD = 900.0       # 键盘灯静态画面：十五分钟一次
BATT_KEYS_OFF = 20           # 低于这个电量：不再推键盘灯
BATT_ALL_OFF = 8             # 低于这个：连点阵也停，把电留给打字
# 真正的耗电大头是「灯一直亮着」，不是推帧的频率 —— 停止推流并不会让灯灭，
# 最后一帧会一直留在屏上。所以省电必须主动熄屏：人离开键盘一段时间就全灭，
# 回来再把画面推回去。键盘自己的休眠是 900 秒，我们比它早一步。
IDLE_BLANK_S = 180.0         # 三分钟没碰键鼠：熄屏（锁屏则立刻）


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
        return 0.0
    now = _k32.GetTickCount64() & 0xFFFFFFFF
    d = (now - li.dwTime) & 0xFFFFFFFF
    return d / 1000.0


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
        self._saved = (None, None)                    # 熄屏前的画面，醒来推回去
        self._idler = None

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
        if wired != self.wired:             # 换了路：上一条路的电量读数作废
            self._pw, self._pw_t = None, 0.0
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
    def handshake(self, force=False):
        now = time.time()
        if not force and now - self.last_hand < HAND_EVERY:
            return
        self.send(C_HAND, 0, b"", 0)
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
                self._backup(bytes(cfg))
                return bytes(cfg)
        self.awake = False
        self.err = last or "读配置超时"
        raise TimeoutError(self.err)

    # ── 配置备份 ───────────────────────────────────────────────────
    # write_config 是读-改-写，写坏了没处找原始值。第一次成功读到配置时
    # 落一份到 config.bak，之后任何时候都能 restore_config() 推回去。
    BAK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.bak")

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
        self.handshake()
        self.send(C_ENDSYNC, 0, b"", 0)
        for off, ln in ((0, 24), (24, 24), (48, 16)):
            self.send(C_WRITE, off, bytes(cfg[off:off + ln]))

    def patch_config(self, patch):
        """读-改-写：patch 是 {字节偏移: 值}，只动指定字节。"""
        cfg = bytearray(self.read_config())
        for k, v in patch.items():
            cfg[int(k)] = max(0, min(255, int(v)))
        self.write_config(cfg)
        return bytes(cfg)

    # ── 省电闸门 ───────────────────────────────────────────────────
    def budget(self):
        """这一刻允许推多快、推不推。返回
        (点阵最小间隔, 键盘灯最小间隔, 点阵保活, 键盘灯保活, 可推键盘灯?, 可推点阵?)

        只按电量分档，不分有线无线 —— 平时基本都是无线在用，与其分两套不如
        一直按省电的那套来，省得插一次线就把功耗习惯带回去。
        电量读不到时按满电处理：宁可多推，也不要因为读不到电量把屏黑了。
        """
        if self.blanked:                    # 熄屏中：上层推什么都挡掉
            return (MIN_MATRIX_GAP, MIN_KEYS_GAP, SAME_MATRIX_HOLD, SAME_KEYS_HOLD,
                    False, False)
        pct = 100 if not self._pw else self._pw["pct"]
        if self._pw and self._pw.get("charging"):
            pct = max(pct, 100)             # 在充电就别降级了
        return (MIN_MATRIX_GAP, MIN_KEYS_GAP, SAME_MATRIX_HOLD, SAME_KEYS_HOLD,
                pct > BATT_KEYS_OFF, pct > BATT_ALL_OFF)

    def _idle_loop(self):
        """人走了就熄屏（锁屏立刻、空闲三分钟），人回来就把画面推回去。

        熄屏 = 键盘灯推全黑 + 点阵推全黑，**不退出推流态**。之前用 close_sync_led
        退出推流，结果固件把点阵屏从存储里的图重画了一遍，锁了屏点阵还亮着。
        专注模式一直是推全黑，那条路是验证过的。

        锁屏判定两路：系统 WTS 通知（hud 窗口收到 LOCK/UNLOCK 就写 session_locked，
        权威）；没收到过通知时退回 OpenInputDesktop 轮询（要去抖，UAC 也会让它失败）。
        """
        lock_hits = 0
        while self.running:
            time.sleep(2.0)
            try:
                lock_hits = lock_hits + 1 if locked() else 0
                if self.session_locked is not None:
                    self.locked = self.session_locked
                else:
                    self.locked = lock_hits >= 2
                idle = idle_secs()
                away = self.locked or idle >= IDLE_BLANK_S
                if away and not self.blanked:
                    self._saved = (self._matrix_last, self._keys_last)
                    self.blanked = True
                    now = time.time()
                    with self.lock:
                        if self._keys_last is not None:
                            self._send_keys(bytes(168), now)
                        self._send_matrix(bytes(108), now)
                    print("[kbd] 熄屏（%s）" % ("锁屏" if self.locked else "空闲 %d 秒" % idle),
                          flush=True)
                elif not away and self.blanked:
                    self.blanked = False
                    mx, ky = self._saved
                    now = time.time()
                    with self.lock:
                        if ky:
                            self._send_keys(ky, now)
                        if mx:
                            self._send_matrix(mx, now)
                    if not mx:
                        self._matrix_last = None
                    print("[kbd] 恢复画面", flush=True)
            except Exception as e:
                print("[kbd] 熄屏循环异常：%s" % e, flush=True)

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
                now = time.time()
                mgap, kgap = self.budget()[0], self.budget()[1]
                with self.lock:
                    p = self._matrix_pending
                    if p is not None and now - self._matrix_t >= mgap:
                        self._send_matrix(p, now)
                    p = self._keys_pending
                    if p is not None and now - self._keys_t >= kgap:
                        self._send_keys(p, now)
            except Exception:
                self._matrix_pending = self._keys_pending = None
                return

    def _send_matrix(self, flat, now):
        self._matrix_pending = None
        self.handshake()
        for i in range(5):
            self.send(C_MATRIX, i * 24, flat[i * 24:i * 24 + 24].ljust(24, NUL), wait=False)
        self._matrix_last, self._matrix_t = flat, now

    def _send_keys(self, data, now):
        self._keys_pending = None
        self.handshake()
        for off in range(0, 168, 24):
            self.send(C_SYNCLED, off, data[off:off + 24], wait=False)
        self._keys_last, self._keys_t = data, now

    def push_matrix(self, flat):
        """108 字节 RGB（6×6×3），5 包，每包 24 字节。

        限速时**不丢帧，而是留着补发**。动画丢一帧无所谓，但模式切换（比如切到
        专注要全黑）也是一次推送，丢了就永远留在上一张图上 —— 表现成「点了没反应」。
        """
        flat = bytes(flat)[:108].ljust(108, NUL)
        gap, _, hold, _, _, allow = self.budget()
        if not allow:                       # 电量见底：点阵也停，电留给打字
            self._matrix_pending = None
            self.dropped += 1
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

    def push_keys(self, rgb565):
        """168 字节 RGB565 小端（84 颗灯），7 包。前提是背光模式已经是 254。

        键盘灯是耗电大头（84 颗 vs 点阵的 36 点），所以低电量时先撤的是它。
        同样是限速留着补发，不丢。"""
        data = bytes(rgb565)[:168].ljust(168, NUL)
        _, gap, _, hold, allow, _ = self.budget()
        if not allow:                       # 低电量：键盘灯先让位
            self._keys_pending = None
            self.dropped += 1
            if self._keys_last is not None:
                self._keys_last = None
                self.stop_keys()            # 退出推流，让键盘自己回默认灯效
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
        self._keys_last = self._keys_pending = None
        self._saved = (self._saved[0], None)

    def stop_keys(self):
        self.send(C_ENDSYNC, 0, b"", 0, wait=False)
        self.forget_keys()

    def status(self):
        return {"connected": self.connected(), "awake": self.awake,
                "product": self.product, "err": self.err, "dropped": self.dropped,
                "wired": self.wired, "power": self._pw,
                "blanked": self.blanked, "idle": round(idle_secs()), "locked": self.locked}


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
