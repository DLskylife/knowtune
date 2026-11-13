@echo off
cd /d "%~dp0"
.\.venv\Scripts\knowtune.exe serve --port 8013
pause