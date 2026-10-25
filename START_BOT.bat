@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".env" (
  echo Файл .env не найден. Сначала запустите УСТАНОВИТЬ_ЗАВИСИМОСТИ.bat
  pause
  exit /b 1
)
where py >nul 2>nul
if %errorlevel%==0 (
  py main.py
) else (
  python main.py
)
if errorlevel 1 (
  echo.
  echo Бот завершился с ошибкой. Проверьте настройки в .env.
  pause
)
