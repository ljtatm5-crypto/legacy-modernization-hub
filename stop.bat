@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "hub\artifacts\.server.pid" (
  echo   [i] 没有找到 pid 文件，服务可能没在运行。
  exit /b 0
)

set /p PID=<"hub\artifacts\.server.pid"
tasklist /fi "PID eq !PID!" 2>nul | findstr /i "node.exe" >nul
if errorlevel 1 (
  echo   [i] 进程 !PID! 已不在，清理 pid 文件。
  del /q "hub\artifacts\.server.pid" 2>nul
  exit /b 0
)

taskkill /PID !PID! /F >nul 2>nul
del /q "hub\artifacts\.server.pid" 2>nul
echo   已停止服务（PID !PID!）。
