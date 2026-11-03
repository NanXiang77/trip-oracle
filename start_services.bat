@echo off
rem Start trip-oracle dashboard (port 8901) and tool API (port 8910).
rem Runs minimized console windows, output appended to log files. ASCII only.
set "PYEXE=C:\Users\Nan\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PYEXE%" set "PYEXE=python.exe"
cd /d "%~dp0"
start "TripOracle-Dashboard" /min cmd /c ""%PYEXE%" app.py >> data\dashboard.log 2>&1"
start "TripOracle-ToolAPI" /min cmd /c ""%PYEXE%" tool_api.py >> data\toolapi.log 2>&1"
