#!/usr/bin/env python3
"""
绕开浏览器，直接用 Windows 的 HID API 跟键盘说话。

用途有两个：
  1. 排查用 —— 页面推不动时，这里能分清是"键盘不回包"还是"网页的锅"
  2. 打底用 —— 如果以后要把 HID 从 WebHID 搬进 Python（甩掉 Chrome），
     枚举、打开、收发这套就是从这儿长出去的

只用 ctypes，不装任何东西。

    py -3 hidprobe.py          列出接口并对厂商口做一次握手 + 读配置
"""
import ctypes
import ctypes.wintypes as wt

VID = 0x373B
C_HAND, C_READ = 16, 20

setup = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]
INVALID_HANDLE = wt.HANDLE(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.POINTER(wt.ULONG))]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("DevicePath", wt.WCHAR * 512)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT),
                ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT),
                ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT),
                ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT),
                ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


# 句柄参数一律要声明，否则 ctypes 按 C int 传，64 位下直接 OverflowError
setup.SetupDiGetClassDevsW.restype = wt.HANDLE
setup.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
setup.SetupDiEnumDeviceInterfaces.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setup.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
hid.HidD_GetAttributes.argtypes = [wt.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidD_SetNumInputBuffers.argtypes = [wt.HANDLE, wt.ULONG]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                         ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.CreateEventW.restype = wt.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.ResetEvent.argtypes = [wt.HANDLE]
k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                    ctypes.POINTER(wt.DWORD), wt.BOOL]
setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wt.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, wt.HANDLE]


def interfaces():
    """列出所有 HID 接口：(设备路径, vid, pid, usagePage, usage, 收/发报告长度)。"""
    g = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(g))
    h = setup.SetupDiGetClassDevsW(ctypes.byref(g), None, None,
                                   DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    out = []
    i = 0
    while True:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = ctypes.sizeof(did)
        if not setup.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(g), i,
                                                 ctypes.byref(did)):
            break
        i += 1
        need = wt.DWORD()
        setup.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(did), None, 0,
                                               ctypes.byref(need), None)
        det = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
        det.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if not setup.SetupDiGetDeviceInterfaceDetailW(
                h, ctypes.byref(did), ctypes.byref(det), ctypes.sizeof(det), None, None):
            continue
        path = det.DevicePath
        # 枚举阶段用 0 访问权限打开（只查属性）。用 GENERIC_READ|WRITE 去开
        # 系统里每一个 HID 设备，碰上某些驱动会直接卡死。
        fh = k32.CreateFileW(path, 0,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                             OPEN_EXISTING, 0, None)
        if fh == INVALID_HANDLE:
            continue
        try:
            at = HIDD_ATTRIBUTES()
            at.Size = ctypes.sizeof(at)
            if not hid.HidD_GetAttributes(fh, ctypes.byref(at)):
                continue
            pre = ctypes.c_void_p()
            if not hid.HidD_GetPreparsedData(fh, ctypes.byref(pre)):
                continue
            caps = HIDP_CAPS()
            hid.HidP_GetCaps(pre, ctypes.byref(caps))
            hid.HidD_FreePreparsedData(pre)
            out.append((path, at.VendorID, at.ProductID, caps.UsagePage, caps.Usage,
                        caps.InputReportByteLength, caps.OutputReportByteLength))
        finally:
            k32.CloseHandle(fh)
    setup.SetupDiDestroyDeviceInfoList(h)
    return out


def talk(path, out_len, in_len, timeout_ms=1500):
    """握手 + 读一段配置。返回 (成功?, 说明)。

    读必须走重叠 I/O：HID 的 ReadFile 在没数据时会一直阻塞，用线程 + join
    看着像超时了，实际那个线程还卡在里面，进程退不掉。
    """
    fh = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                         FILE_FLAG_OVERLAPPED, None)
    if fh == INVALID_HANDLE:
        return False, "打不开：%d" % ctypes.get_last_error()
    ev = k32.CreateEventW(None, True, False, None)

    def cancel(ov):
        """取消之后必须等它真的结束再让 ov 出作用域 ——
        否则内核还在往这块已经被回收的内存里写，进程直接段错误。"""
        k32.CancelIoEx(fh, ctypes.byref(ov))
        n = wt.DWORD()
        k32.GetOverlappedResult(fh, ctypes.byref(ov), ctypes.byref(n), True)

    try:
        hid.HidD_SetNumInputBuffers(fh, 64)

        def write(cmd, off, ln, seq):
            buf = (ctypes.c_ubyte * out_len)()
            buf[0] = 0                      # report id
            buf[1] = 0xAA
            buf[2] = cmd
            buf[3] = off & 255
            buf[4] = (off >> 8) & 255
            buf[5] = ln
            buf[6] = seq
            ov = OVERLAPPED()
            ov.hEvent = ev
            k32.ResetEvent(ev)
            ok = k32.WriteFile(fh, buf, out_len, None, ctypes.byref(ov))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                return False
            if k32.WaitForSingleObject(ev, 800) != WAIT_OBJECT_0:
                cancel(ov)
                return False
            return True

        def read(ms):
            buf = (ctypes.c_ubyte * in_len)()
            ov = OVERLAPPED()
            ov.hEvent = ev
            k32.ResetEvent(ev)
            ok = k32.ReadFile(fh, buf, in_len, None, ctypes.byref(ov))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                return None
            if k32.WaitForSingleObject(ev, ms) != WAIT_OBJECT_0:
                cancel(ov)
                return None
            n = wt.DWORD()
            k32.GetOverlappedResult(fh, ctypes.byref(ov), ctypes.byref(n), False)
            return bytes(buf[:n.value])

        if not write(C_HAND, 0, 0, 1):
            return False, "握手写不出去：%d" % ctypes.get_last_error()
        if not write(C_READ, 0, 24, 2):
            return False, "读命令写不出去：%d" % ctypes.get_last_error()
        r = read(timeout_ms)
        if r is None:
            return False, "没回包（键盘没醒 / 不在 2.4G）"
        return True, "回包 %d 字节：%s" % (len(r), r[:12].hex(" "))
    finally:
        k32.CloseHandle(ev)
        k32.CloseHandle(fh)


if __name__ == "__main__":
    rows = [r for r in interfaces() if r[1] == VID]
    if not rows:
        print("没找到 VID %04X 的设备" % VID)
        raise SystemExit(1)
    print("接口一览（usagePage/usage  收/发报告长度）：")
    for path, vid, pid, up, us, il, ol in rows:
        print("  %04X:%04X  %04X/%02X  in=%d out=%d" % (vid, pid, up, us, il, ol))
    print()
    for path, vid, pid, up, us, il, ol in rows:
        if up == 0xFF60:
            print("对 %04X/%02X 试一次握手 + 读配置…" % (up, us))
            ok, why = talk(path, ol, il)
            print("   ->", "成功" if ok else "失败", why)
