@echo off
chcp 65001 >nul
title ZeroFall - DeepSeek前置守护系统
echo ========================================
echo   ZeroFall - DeepSeek前置守护系统
echo ========================================
echo.

cd /d "%~dp0"

REM 检查Python是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到Python，请先安装Python 3.8+
    pause
    exit /b 1
)

REM 检查依赖是否安装
python -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖...
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
)

echo [启动] ZeroFall正在启动...
python main.py

if errorlevel 1 (
    echo.
    echo [错误] ZeroFall启动失败，请查看日志
    pause
)
