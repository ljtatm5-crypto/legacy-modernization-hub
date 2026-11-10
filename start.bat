@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Legacy Modernization Hub

echo.
echo   Legacy Modernization Hub
echo   ----------------------------------------
echo.

rem ---- 环境自检 ----
where node >nul 2>nul
if errorlevel 1 (
  echo   [X] 没找到 Node.js，请先安装 18 及以上版本：https://nodejs.org
  goto :fail
)
where java >nul 2>nul
if errorlevel 1 (
  echo   [X] 没找到 Java，请先安装 JDK 17 或 21，并把 bin 加入 PATH
  goto :fail
)
where python >nul 2>nul
if errorlevel 1 (
  echo   [X] 没找到 Python，请先安装 3.9 及以上版本，并把 python 加入 PATH
  goto :fail
)

for /f "delims=" %%v in ('node -v') do set NODE_V=%%v
for /f "tokens=3" %%v in ('java -version 2^>^&1 ^| findstr /i "version"') do set JAVA_V=%%v
for /f "delims=" %%v in ('python -V 2^>^&1') do set PY_V=%%v
echo   Node   !NODE_V!
echo   Java   !JAVA_V!
echo   !PY_V!
echo.

rem ---- 端口占用检查 ----
if exist "hub\artifacts\.server.pid" (
  set /p OLD_PID=<"hub\artifacts\.server.pid"
  tasklist /fi "PID eq !OLD_PID!" 2>nul | findstr /i "node.exe" >nul
  if not errorlevel 1 (
    echo   [i] 检测到已在运行（PID !OLD_PID!），直接打开页面。
    start "" http://127.0.0.1:8787
    goto :eof
  )
  del /q "hub\artifacts\.server.pid" 2>nul
)

rem ---- 启动 ----
echo   正在启动服务...
start "" /min cmd /c "node hub\backend\server.js"
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8787
echo.
echo   已打开 http://127.0.0.1:8787
echo   停止服务请运行 stop.bat
echo.
timeout /t 3 /nobreak >nul
goto :eof

:fail
echo.
pause
exit /b 1
