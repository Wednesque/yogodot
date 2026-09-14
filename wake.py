#!/usr/bin/env python3
"""
键盘救援 —— 守在那儿等键盘回来。

用法：跑起来，然后插 USB-C 线 / 拨 2.4G 档 / 开机。
谁应答了就把它是哪条路（有线还是接收器）和配置打出来。
"""
import sys, time
import hidprobe as H
import kbd

def devs():
    try:
        return [r for r in H.interfaces() if r[1] == H.VID]
    except Exception as e:
        print("枚举失败：", e); return []

def label(path):
    p = path.lower()
    return "有线（键盘直连）" if "col" in p and "mi_" in p else path[:60]

def main(secs=180):
    print("在等键盘应答，最多 %d 秒。现在可以插线 / 开机 / 拨到 2.4G 档。" % secs)
    print("（Ctrl+C 可以随时停）\n")
    t0 = time.time()
    seen = None
    while time.time() - t0 < secs:
        rows = devs()
        sig = tuple(sorted((r[0] for r in rows)))
        if sig != seen:
            seen = sig
            ff60 = [r for r in rows if r[3] == 0xFF60]
            print("[%3ds] 设备变化：VID 373B 共 %d 个接口，其中厂商口 %d 个"
                  % (time.time() - t0, len(rows), len(ff60)))
        K = kbd.Keyboard()
        K.last_open_try = 0
        try:
            if K.open() and K.awake:
                cfg = K.read_config()
                print("\n===== 键盘回来了 =====")
                print("走的口 :", K.path)
                print("产品名 :", K.product)
                print("配置   : %d 字节  blMode=%d dsMode=%d dsBright=%d"
                      % (len(cfg), cfg[2], cfg[30], cfg[31]))
                print("备份   :", "已存 config.bak")
                K.close()
                return 0
        except Exception:
            pass
        finally:
            try: K.close()
            except Exception: pass
        time.sleep(2)
    print("\n超时：这段时间里键盘一直没应答。")
    return 1

if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 180))
