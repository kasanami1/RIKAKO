@echo off
rem ===================================================================
rem  启动「本地语音合成（GPT-SoVITS）」服务
rem
rem  双击本文件即可。作用：把整合包里的 api_v2.py 起起来。
rem  会弹出（其实是它自己新建的）一个控制台窗口显示加载进度——
rem  那个窗口是唯一能看到「加载到哪、报什么错」的地方，别关它。
rem
rem  服务起来后，在桌宠的「语音合成设置 → 本地」页点「检测 / 用当前音色试听」。
rem  更细的自检：双击 检查本地语音服务.bat
rem ===================================================================
setlocal EnableExtensions
chcp 936 >nul 2>nul
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo   [错误] 没找到 %PY%
  echo          请先双击一次 启动桌宠.bat，让它把运行环境装好。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在启动本地语音合成服务（整合包加载模型要几十秒到几分钟）...
echo.
"%PY%" "本地语音服务.py" start %*
set "CODE=%errorlevel%"
echo.
if not "%CODE%"=="0" (
  echo   [错误] 启动没成功，退出码 %CODE%。上面的提示里通常写了原因。
) else (
  echo   [OK] 服务已在跑。接下来去桌宠：「语音合成设置 -^> 合成方式 -^> 本地 · GPT-SoVITS」。
)
echo.
pause
exit /b %CODE%
