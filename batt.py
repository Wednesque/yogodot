#!/usr/bin/env python3
"""
电量探测 —— 只用已知的安全命令（握手 16 / 读 20）。

不扫未知命令号：那有可能撞上恢复出厂或进 bootloader。
思路是把握手回包和各段可读区域的原始字节打出来，靠「插线/拔线」
和「电量变化」两次快照做差分，找出哪个字节是电量、哪个位是充电中。
"""
import sys, time
import kbd

def hexs(b):
    return " ".join("%02X" % x for x in b)

def dump(tag):
    K = kbd.Keyboard(); K.last_open_try = 0
    if not K.open():
        print("打不开：", K.err); return None
    out = {}
    try:
        r = K.send(kbd.C_HAND, 0, b"", 0)
        print("\n=== %s ===" % tag)
        print("口     :", "有线" if "mi_02" in K.path.lower() else K.path.split("#")[1])
        print("产品名 :", K.product)
        if r:
            print("握手回包 (%d 字节):" % len(r))
            print("   ", hexs(r))
            out["hand"] = bytes(r)
        else:
            print("握手没回包")
        # 配置区 0..63（已知安全），再往后试探读，读不到就算了
        for base in (0, 64, 128):
            seg = bytearray()
            ok = True
            for off in range(base, base + 64, 24):
                ln = min(24, base + 64 - off)
                rr = K.send(kbd.C_READ, off, b"", ln)
                if not rr or len(rr) < 9 + ln:
                    ok = False; break
                seg += rr[9:9 + ln]
            if ok:
                print("读 offset %3d..%3d:" % (base, base + 63))
                for i in range(0, 64, 16):
                    print("   %3d: %s" % (base + i, hexs(seg[i:i + 16])))
                out[base] = bytes(seg)
            else:
                print("读 offset %3d.. 没应答（这段不可读，正常）" % base)
    finally:
        K.close()
    return out

if __name__ == "__main__":
    a = dump(sys.argv[1] if len(sys.argv) > 1 else "快照")
