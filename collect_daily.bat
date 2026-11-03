@echo off
REM Daily collect - bound to Windows Task Scheduler.
REM   collect_daily.bat        full run  (8-day window + heat index + douyin)
REM   collect_daily.bat fast   morning   (2-day window only, ~40s)
REM Log: data\collect.log   DB: data\oracle.db
REM NOTE: keep this file ASCII-only - cmd.exe parses it as GBK.
setlocal
cd /d "%~dp0"
if not exist data mkdir data

REM Task Scheduler has a different PATH: bare "python" may hit the Store stub.
set "PYEXE=C:\Users\Nan\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

set "FAST=0"
if /I "%~1"=="fast" set "FAST=1"
if "%FAST%"=="1" (
    set "COLLECT_DAYS=2"
    set "SKIP_HEAT=1"
    set "SKIP_DOUYIN=1"
)

echo [%date% %time%] start (fast=%FAST%) >> data\collect.log
"%PYEXE%" collect.py >> data\collect.log 2>&1
echo [%date% %time%] collect done >> data\collect.log

REM Push heat to cloud only on full runs (fast runs collect no heat)
if "%FAST%"=="0" (
    "%PYEXE%" push_heat.py --days 2 >> data\collect.log 2>&1
    echo [%date% %time%] push done >> data\collect.log
)
