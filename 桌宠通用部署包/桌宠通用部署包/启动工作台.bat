@echo off
rem 双击本文件 = 启动工作台（桌宠 + 工作台面板）
rem 想调试请看控制台报错，请用 调试工作台.bat
chcp 936 >nul 2>nul
cd /d "%~dp0"
set "LILYZ_APP=panel"
call "启动桌宠.bat" %*