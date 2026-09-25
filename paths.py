#!/usr/bin/env python3
"""
路径统一出口 —— 打包成 exe 之后，"程序自带的文件"和"要写的文件"不在一个地方。

  RES   只读资源：yogo.html、designs.js、art/。打包后在 PyInstaller 的解压目录
        （sys._MEIPASS），那是临时目录，重启就没了，绝不能往里写东西。
  DATA  可写数据：settings.json、capsule.json、config.bak、.token、日志、
        Chrome profile。放在 exe 旁边；exe 装在 Program Files 那种没写权限的
        地方时，退回 %LOCALAPPDATA%\\YogoDot。

直接跑源码时两者都是项目目录，跟以前一模一样。
"""
import os
import sys

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
else:
    RES = os.path.dirname(os.path.abspath(__file__))
    _exe_dir = RES


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".wtest")
        with open(p, "w") as f:
            f.write("x")
        os.remove(p)
        return True
    except OSError:
        return False


DATA = _exe_dir if _writable(_exe_dir) else os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "YogoDot")
os.makedirs(DATA, exist_ok=True)


def res(*p):
    """程序自带的只读文件。"""
    return os.path.join(RES, *p)


def data(*p):
    """要写的文件。"""
    return os.path.join(DATA, *p)
