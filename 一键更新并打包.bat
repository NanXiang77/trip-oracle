@echo off
REM One-click: collect fresh data, rebuild the offline snapshot, re-zip it.
REM Double-click this file before you upload / send the snapshot to teammates.
REM NOTE: keep this file ASCII-only - cmd.exe parses it as GBK.
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] collecting 12306 / weather / heat index ...
python collect.py
if errorlevel 1 (
    echo [warn] collect failed - snapshot will use whatever is already in the db.
)
echo [2/2] building snapshot ...
python build_snapshot.py
if errorlevel 1 (
    echo [error] snapshot build failed.
    pause
    exit /b 1
)
echo.
echo Done. Files in the dist folder:
dir /b dist\*.html dist\*.zip
echo.
echo Upload the NEW html / zip - the old one will NOT update by itself.
pause
