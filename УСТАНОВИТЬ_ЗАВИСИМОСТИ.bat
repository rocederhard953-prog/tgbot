@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   Установка зависимостей Telegram-бота
echo ============================================
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python не найден. Установите Python 3.10+ с сайта python.org
    echo При установке отметьте Add Python to PATH.
    pause
    exit /b 1
  )
  set "PY=python"
)
%PY% -m pip install --upgrade pip
if errorlevel 1 goto failed
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Создан файл .env. Сейчас он откроется в Блокноте.
  echo Заполните BOT_TOKEN и ADMIN_ID, сохраните файл и закройте Блокнот.
  notepad ".env"
)
echo.
echo Установка завершена.
echo Запуск: запустите START_BOT.bat
pause
exit /b 0
:failed
echo.
echo Ошибка установки зависимостей. Проверьте интернет и установку Python.
pause
exit /b 1
