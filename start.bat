@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
py -m pip install -q -r requirements.txt
py -m app.main
pause
