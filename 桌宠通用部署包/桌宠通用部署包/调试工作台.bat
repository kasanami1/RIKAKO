@echo off
rem 双击本文件 = 调试模式启动工作台：保留控制台窗口，报错不会一闪而过。
chcp 936 >nul 2>nul
cd /d "%~dp0"
set "LILYZ_APP=panel"
call "启动桌宠.bat" debug