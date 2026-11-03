@echo off
REM Daily collect - bound to Windows Task Scheduler.
REM Log: data\collect.log   DB: data\oracle.db
REM NOTE: keep this file ASCII-only - cmd.exe parses it as GBK.
setlocal
cd /d "%~dp0"
if not exist data mkdir data

REM Task Scheduler has a different PATH: bare "python" may hit the Store stub.
set "PYEXE=C:\Users\Nan\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo [%date% %time%] start >> data\collect.log
"%PYEXE%" collect.py >> data\collect.log 2>&1
echo [%date% %time%] collect done >> data\collect.log

REM Push heat to cloud when data\cloud.json exists (no-op otherwise)
"%PYEXE%" push_heat.py --days 2 >> data\collect.log 2>&1
echo [%date% %time%] push done >> data\collect.log
