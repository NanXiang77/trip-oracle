@echo off
REM Start the tool HTTP service that the agent platform will call.
REM Keep this window open while the platform is being tested.
REM NOTE: keep this file ASCII-only - cmd.exe parses it as GBK.
chcp 65001 >nul
cd /d "%~dp0"
echo Tools API: http://127.0.0.1:8910
echo   GET  /health   /tools   /openapi.json
echo   POST /tools/get_crowd_matrix   {"days":8}
echo Set TOOL_API_TOKEN first if you want simple bearer auth.
python tool_api.py
pause
