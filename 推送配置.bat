@echo off
REM Configure cloud push (run once after you deployed to Render / Railway).
REM Writes data/cloud.json. NOTE: keep ASCII-only - cmd.exe parses it as GBK.
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  Cloud push setup (heat index only)
echo ============================================
echo  URL example : https://trip-oracle.onrender.com
echo  Token       : the TOOL_API_TOKEN value on the cloud
echo.
set /p CURL=https://
set /p CTOK=Token:
if "%CURL%"=="" goto empty
if "%CTOK%"=="" goto empty
python set_cloud.py --url "https://%CURL%" --token "%CTOK%"
goto done
:empty
echo.
echo [skip] nothing entered.
:done
echo.
pause
