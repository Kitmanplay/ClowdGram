@echo off
chcp 65001 >nul
cd /d "%~dp0"
pip install pyinstaller
pyinstaller --onefile --noconsole --name ClawdGram client.py
pyinstaller --onefile --name ClawdGramServer server.py
echo.
echo Готово! Файлы лежат в папке dist
pause
