@echo off
rem ===================================================================
rem  李花子桌宠 · 启动器（共用一个启动内核）
rem
rem  一般不用直接双击本文件，请用这两个入口：
rem      启动桌宠.bat      只启动桌宠
rem      启动工作台.bat    桌宠 + 工作台面板
rem
rem  本文件支持的参数：
rem      debug   保留控制台窗口，能看到报错
rem      check   只做环境自检，不启动
rem  应用模式由调用方通过环境变量 LILYZ_APP 指定（pet / panel），默认 pet。
rem ===================================================================
setlocal EnableExtensions
chcp 936 >nul 2>nul
cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "NEED=PySide6, qasync, httpx, openai"
set "PIPOPT=--no-input --disable-pip-version-check --timeout 30 --retries 2"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "VERCHK=import sys; sys.exit(0 if sys.version_info>=(3,9) else 2)"
set "MODE=%~1"

set "APP=pet"
set "APPNAME=桌宠"
if /i "%LILYZ_APP%"=="panel" (set "APP=panel" & set "APPNAME=工作台")
title 李花子 · %APPNAME%

echo.
echo   ================================================
echo     李花子桌宠  启动器   [%APPNAME%]
echo   ================================================
echo.

if not exist "main.py" (
  echo   [错误] 当前目录下找不到 main.py
  echo          请把本脚本放在桌宠项目根目录再运行。
  goto :fail
)

rem ---------- 1. 准备可用的 Python 环境 ----------
call :ensure_python
if errorlevel 1 goto :fail

rem ---------- 2. 检查依赖 ----------
call :deps_ok
if errorlevel 1 (
  echo   [信息] 缺少依赖，正在安装 requirements.txt ...
  echo          首次运行需要几分钟，请耐心等待。
  echo.
  "%PY%" -m pip install %PIPOPT% -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   [信息] 直连安装失败，正在升级 pip 并改用清华镜像重试 ...
    "%PY%" -m pip install %PIPOPT% --upgrade pip
    "%PY%" -m pip install %PIPOPT% -r requirements.txt -i %MIRROR%
    if errorlevel 1 (
      echo.
      echo   [错误] 依赖安装失败，请把上面的 pip 报错发出来排查。
      goto :fail
    )
  )
  rem 装完必须复查一次，否则会误报「环境就绪」，启动时才崩。
  call :deps_ok
  if errorlevel 1 (
    echo.
    echo   [错误] 依赖装完仍然无法导入，请查看上面的 pip 输出。
    goto :fail
  )
)
echo   [OK] 运行环境就绪

if /i "%MODE%"=="check" (
  echo.
  echo   [OK] 自检通过：虚拟环境与依赖都正常。
  echo        启动模式 = %APPNAME%（main.py %APP%）
  echo.
  pause
  exit /b 0
)

rem ---------- 3. 启动 ----------
if /i "%MODE%"=="debug" (
  echo   [OK] 调试模式启动（%APPNAME%），保留控制台输出
  echo.
  "%PY%" main.py %APP%
  echo.
  echo   已退出，退出码 = %errorlevel%
  echo.
  pause
  exit /b %errorlevel%
)

echo   [OK] 正在启动%APPNAME% ...
start "" "%PYW%" main.py %APP%
exit /b 0

rem ===================================================================
rem  子过程：确保 .venv 可用，必要时创建或重建
rem ===================================================================
:ensure_python
if exist "%PY%" (
  "%PY%" -c "import sys" >nul 2>nul
  if not errorlevel 1 exit /b 0
  echo   [信息] 虚拟环境已损坏（可能项目被移动过），正在重建 ...
  rmdir /s /q "%VENV%" >nul 2>nul
)

echo   [信息] 正在查找可用的 Python ...
set "BOOT="

rem py 启动器 → PATH 里的 python / python3 → 常见安装位置（含 Anaconda）
where py >nul 2>nul && call :probe "py -3"
if not defined BOOT call :probe "python"
if not defined BOOT call :probe "python3"
if not defined BOOT call :probepath "%ProgramData%\Anaconda3\python.exe"
if not defined BOOT call :probepath "%ProgramData%\miniconda3\python.exe"
if not defined BOOT call :probepath "%USERPROFILE%\anaconda3\python.exe"
if not defined BOOT call :probepath "%USERPROFILE%\miniconda3\python.exe"
if not defined BOOT for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined BOOT call :probepath "%%~fD\python.exe"

if not defined BOOT (
  echo.
  echo   [错误] 没有找到可用的 Python（需要 3.9 或更高版本）。
  echo.
  echo          注意：Microsoft Store 里的 python 占位程序不算，它会直接失败。
  echo          请安装 Python 3.9+ 并勾选 "Add python.exe to PATH"，
  echo          或安装 Anaconda 后重新运行本脚本。
  echo          下载地址： https://www.python.org/downloads/
  exit /b 1
)

echo   [OK] 使用 %BOOT%
echo   [信息] 正在创建虚拟环境 %VENV% ...
%BOOT% -m venv "%VENV%"
if errorlevel 1 (
  echo.
  echo   [错误] 创建虚拟环境失败，请查看上面的报错信息。
  exit /b 1
)
echo   [OK] 虚拟环境已创建
exit /b 0

rem 探测「命令+参数」形式的解释器是否可用且版本达标
:probe
%~1 -c "%VERCHK%" >nul 2>nul
if errorlevel 1 exit /b 1
set "BOOT=%~1"
exit /b 0

rem 探测「纯路径」形式的解释器（路径可能含空格，必须整体加引号）
:probepath
"%~1" -c "%VERCHK%" >nul 2>nul
if errorlevel 1 exit /b 1
set "BOOT="%~1""
exit /b 0

rem 依赖是否可导入：0 = 正常，1 = 缺失
:deps_ok
"%PY%" -c "import %NEED%" >nul 2>nul
exit /b %errorlevel%

:fail
echo.
pause
exit /b 1
