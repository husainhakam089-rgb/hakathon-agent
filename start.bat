@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title دفتر الديون

where py >nul 2>nul
if errorlevel 1 (
  echo ما لگيت Python. نصّبه من python.org وأشّر على Add python.exe to PATH.
  pause
  exit /b 1
)
if not exist ".env" (
  echo تنبيه: ما كو ملف .env بهذا المجلد، فالوكيل ما يگدر يحچي ويا Gemini.
  echo انسخ ملف .env من المجلد القديم لهنا.
  echo.
)

echo [1/2] دا أتأكد من المكتبات...
py -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo فشل تنصيب المكتبات. صوّر هاي النافذة ودزها.
  pause
  exit /b 1
)

echo [2/2] دا أشغّل البرنامج... خلّي هاي النافذة مفتوحة، وإذا سديتها البرنامج يوگف.
py -m app.main
echo.
echo البرنامج وگف. إذا أكو خطأ فوگ، صوّر هاي النافذة ودزها.
pause
