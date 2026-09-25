@echo off
rem  打包成免安装的 YogoDot.exe。只有开发者需要跑这个，用户下 release 就行。
rem  用独立 venv 打包，不碰你全局的 Python 环境。
setlocal
cd /d "%~dp0"

if not exist .buildenv (
  echo [1/3] 建打包用的 venv ...
  py -3 -m venv .buildenv || goto :err
)

echo [2/3] 装 pyinstaller + pillow ...
.buildenv\Scripts\python.exe -m pip install -q --upgrade pip pyinstaller pillow || goto :err

echo [3/3] 打包 ...
.buildenv\Scripts\python.exe -m PyInstaller --noconfirm --log-level WARN YogoDot.spec || goto :err

echo.
echo 好了： dist\YogoDot\YogoDot.exe
echo 发布时把整个 dist\YogoDot 文件夹打成 zip。
goto :eof

:err
echo.
echo 打包失败，看上面的报错。
exit /b 1
