@echo off
rem 每日采集：挂到 Windows 计划任务，每天运行 1~2 次
rem 采集结果追加到 data\collect.log，数据库在 data\oracle.db
cd /d "%~dp0"
echo [%date% %time%] start >> data\collect.log
python collect.py >> data\collect.log 2>&1
echo [%date% %time%] done >> data\collect.log
