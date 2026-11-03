@echo off
REM Forward-observation scheduler entry (called by Windows Task Scheduler, or run manually).
REM   run_watch.bat --once
REM   run_watch.bat --status        (prints to console, not the log)
REM   run_watch.bat --loop --interval 60
REM NOTE: keep this file ASCII-only - cmd.exe parses it as GBK before chcp applies.
chcp 65001 >nul
cd /d "%~dp0"
if not exist data mkdir data
if /I "%1"=="--status" (
    python -m trip_oracle.watch %*
) else (
    python -m trip_oracle.watch %* >> data\watch.log 2>&1
)
