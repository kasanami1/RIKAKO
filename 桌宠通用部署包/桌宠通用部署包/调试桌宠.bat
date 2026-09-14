@echo off
rem 双击本文件 = 调试模式启动桌宠：保留控制台窗口，报错不会一闪而过。
chcp 936 >nul 2>nul
cd /d "%~dp0"
call "启动桌宠.bat" debug