@echo off
title ClaudeBridge
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start-all.ps1"
echo.
echo Окно можно закрыть — туннель остановится.
pause
