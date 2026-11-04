@echo off
REM Daily collect - bound to Windows Task Scheduler.
REM   collect_daily.bat        full run  (8-day window + heat index + douyin + push)
REM   collect_daily.bat fast   morning   (2-day window + baidu heat, no douyin, ~2 min)
REM   collect_daily.bat heat   midday    (1-day window + heat + douyin + push, ~2 min)
REM Log: data\collect.log   DB: data\oracle.db
REM NOTE: keep this file ASCII-only - cmd.exe parses it as GBK.
setlocal
cd /d "%~dp0"
if not exist data mkdir data

REM Task Scheduler has a different PATH: bare "python" may hit the Store stub.
set "PYEXE=C:\Users\Nan\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

set "FAST=0"
set "HEAT=0"
if /I "%~1"=="fast" set "FAST=1"
if /I "%~1"=="heat" set "HEAT=1"
if "%FAST%"=="1" (
    REM Morning: today column needs a 2-day ticket window. Heat backfill is cheap
    REM (baidu ~20s); douyin is skipped (it starts a whole Edge browser).
    set "COLLECT_DAYS=2"
    set "HEAT_DAYS=2"
    set "SKIP_DOUYIN=1"
)
if "%HEAT%"=="1" (
    REM Midday: yesterday's baidu/douyin index publishes during the day.
    REM 1-day ticket window keeps it short; heat + douyin do the real work.
    set "COLLECT_DAYS=1"
    set "HEAT_DAYS=2"
    set "SKIP_DOUYIN=0"
)

echo [%date% %time%] start (fast=%FAST% heat=%HEAT%) >> data\collect.log
"%PYEXE%" collect.py >> data\collect.log 2>&1
echo [%date% %time%] collect done >> data\collect.log

REM All modes collect heat now, so always push the last 2 days (idempotent).
"%PYEXE%" push_heat.py --days 2 >> data\collect.log 2>&1
echo [%date% %time%] push done >> data\collect.log
