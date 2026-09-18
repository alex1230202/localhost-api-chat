@echo off
chcp 65001 >nul
title Local Chat - keep this window open
echo.
echo   Starting chat server, your browser will open automatically.
echo   Keep this window open; closing it stops the server.
echo.
python "%~dp0chat.py" --port 8788 --open
if errorlevel 1 (
  echo.
  echo   "python" not found. Try:  py "%~dp0chat.py" --port 8788 --open
)
pause
