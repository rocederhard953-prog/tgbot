@echo off
chcp 65001 >nul
echo Установка зависимостей...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости. Проверь установку Python.
  pause
  exit /b 1
)
echo.
echo ВАЖНО: сначала задай BOT_TOKEN, CHANNEL_URL и ADMIN_ID в переменных окружения.
echo Затем запусти: python main.py
pause
