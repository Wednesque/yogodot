# PyInstaller 打包脚本。用法（在项目目录）：
#     .buildenv\Scripts\python.exe -m PyInstaller --noconfirm YogoDot.spec
# 产物：dist\YogoDot\YogoDot.exe —— 一个文件夹，双击 exe 就跑，不用装 Python。
#
# 为什么是 onedir 不是 onefile：onefile 每次启动都要把几十 MB 解压到临时目录，
# 开机自启时明显慢一拍；而且杀软对自解压 exe 更敏感。onedir 启动几乎即时。

block_cipher = None

a = Analysis(
    ['yogo.pyw'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('yogo.html', '.'),
        ('designs.js', '.'),
        ('yogo.ico', '.'),
        ('fonts', 'fonts'),
        ('art', 'art'),
        ('remote', 'remote'),
        ('README.md', '.'),
        ('LICENSE', '.'),
    ],
    # 这些模块是运行时按名字找的，Analysis 静态扫不出来
    hiddenimports=['paths', 'server', 'hud', 'capsule', 'kbd', 'hidprobe', 'quota'],
    hookspath=[],
    runtime_hooks=[],
    # 打包时用不到的大件，去掉能省一半体积
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'test',
              'numpy', 'matplotlib', 'PIL.ImageQt', 'PyQt5', 'PySide2'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='YogoDot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 托盘程序，不要黑框
    icon='yogo.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='YogoDot',
)
