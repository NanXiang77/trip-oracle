@echo off
REM One-click: collect fresh data, push heat to cloud, rebuild the offline snapshot, re-zip it.
REM Double-click this file before you upload / send the snapshot to teammates.
REM NOTE: keep this file ASCII-only - cmd.exe parses it as GBK.
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] collecting 12306 / weather / heat index ...
python collect.py
if errorlevel 1 (
    echo [warn] collect failed - snapshot will use whatever is already in the db.
)
if "%TOOL_API_URL%"=="" (
    echo [2/3] pushing heat index to cloud if configured ...
) else (
    echo [2/3] pushing heat index to cloud ...
)
python push_heat.py --days 2
if errorlevel 1 echo [skip] heat not pushed - run 推送配置.bat once to set cloud url / token
echo [3/3] building snapshot ...
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
