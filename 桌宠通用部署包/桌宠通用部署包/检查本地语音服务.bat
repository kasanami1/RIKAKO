@echo off
rem ===================================================================
rem  检查「本地语音合成（GPT-SoVITS）」环境
rem
rem  双击本文件即可：不联网、不花钱，只查四件事——
rem    1) 配置里填了什么   2) 整合包目录对不对
rem    3) 服务端口通不通   4) 参考音频合不合格（要 3~10 秒）
rem  想顺便听一句合成效果：双击本文件之后再看提示，或执行
rem    .venv\Scripts\python.exe 本地语音服务.py test
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
"%PY%" "本地语音服务.py" check %*
set "CODE=%errorlevel%"
echo.
echo   ------------------------------------------------------------
echo   下一步建议：
echo     服务没在跑 -^> 双击 启动本地语音服务.bat
echo     服务在跑   -^> 执行下面的命令，真合成一句听效果
echo        .venv\Scripts\python.exe 本地语音服务.py test
echo.
pause
exit /b %CODE%
