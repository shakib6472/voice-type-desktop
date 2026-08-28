@echo off
rem Removes the Start Menu, Desktop and startup shortcuts. Nothing else is
rem touched, so the files in this folder keep working.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Remove
echo.
pause
