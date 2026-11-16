@echo off
setlocal EnableExtensions

set "PROJECT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%start_bot.ps1"
exit /b %ERRORLEVEL%
