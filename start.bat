@echo off
chcp 65001 >nul 2>&1
title 供应链 AI Agent 启动器

cd /d "C:\Users\86176\Desktop\重要信息\后端备份"

echo ============================================
echo   供应链 AI Agent 启动中...
echo ============================================
echo.

:: 检查数据库
if not exist "data\supply_chain.db" (
    echo [1/3] 导入数据到数据库...
    C:\Users\86176\AppData\Local\Programs\Python\Python313\python.exe data\import_data.py
    echo.
) else (
    echo [1/3] 数据库已存在，跳过导入
    echo.
)

echo [2/3] 检查守护进程状态...

:: 检查 daemon.lock 是否存在且进程存活
set DAEMON_RUNNING=0
if exist "daemon.lock" (
    for /f "tokens=*" %%i in ('C:\Users\86176\AppData\Local\Programs\Python\Python313\python.exe -c "import json;d=json.load(open('daemon.lock'));print(d.get('pid',''))" 2^>nul') do set LOCK_PID=%%i
    if defined LOCK_PID (
        tasklist /FI "PID eq %LOCK_PID%" 2>nul | findstr "%LOCK_PID%" >nul
        if %ERRORLEVEL%==0 (
            set DAEMON_RUNNING=1
            echo   守护进程已在运行 (PID=%LOCK_PID%)
        )
    )
)

if %DAEMON_RUNNING%==1 (
    echo   跳过启动，直接检查服务器状态...
    goto :check_server
)

echo [3/3] 启动守护进程...

:: 先杀掉残留的服务器进程（不杀守护进程）
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8081 ^| findstr LISTENING') do (
    tasklist /FI "PID eq %%a" 2>nul | findstr "pythonw.exe python.exe" >nul
    if %ERRORLEVEL%==0 taskkill /F /PID %%a >nul 2>&1
)

:: 启动守护进程
start "" /B C:\Users\86176\AppData\Local\Programs\Python\Python313\pythonw.exe daemon.py

:check_server
:: 等待服务器启动
echo.
echo   本机:   http://localhost:8081
echo   局域网: http://192.168.18.66:8081
echo   等待服务器就绪...

:: 循环检测，最多等 20 秒
set TRIED=0
:wait_loop
timeout /t 3 /nobreak >nul
set /a TRIED+=1
C:\Users\86176\AppData\Local\Programs\Python\Python313\python.exe -c "import urllib.request; r=urllib.request.urlopen('http://localhost:8081/api/health',timeout=3); print(r.read().decode()[:30])" 2>nul
if %ERRORLEVEL%==0 goto :success
if %TRIED% LSS 7 goto :wait_loop

echo.
echo ⚠ 服务器启动超时，可能还在初始化
echo   请等待片刻再访问，或查看 server.log
goto :end

:success
echo.
echo ✅ 服务器已就绪！

:: 启动自动刷新调度器
echo.
echo [额外] 启动自动刷新调度器...
set SCHEDULER_RUNNING=0
if exist "auto_sync.lock" (
    for /f "tokens=*" %%i in ('C:\Users\86176\AppData\Local\Programs\Python\Python313\python.exe -c "import json;d=json.load(open('auto_sync.lock'));print(d.get('pid',''))" 2^>nul') do set SYNC_PID=%%i
    if defined SYNC_PID (
        tasklist /FI "PID eq %SYNC_PID%" 2>nul | findstr "%SYNC_PID%" >nul
        if %ERRORLEVEL%==0 (
            set SCHEDULER_RUNNING=1
            echo   调度器已在运行 (PID=%SYNC_PID%)
        )
    )
)
if %SCHEDULER_RUNNING%==0 (
    start "" /B C:\Users\86176\AppData\Local\Programs\Python\Python313\pythonw.exe auto_sync.py
    echo   调度器已启动（销量09:00 在途+工厂09:30 库存每小时 完成后重启）
)

:end
echo.
echo   本机:   http://localhost:8081
echo   局域网: http://192.168.18.66:8081
echo   日志:   daemon.log / server.log / auto_sync.log
echo   调度:   销量09:00 | 在途+工厂09:30 | 库存每小时 | 完成后重启
echo ============================================
echo.
echo 按任意键关闭此窗口（后台服务继续运行）
pause >nul
