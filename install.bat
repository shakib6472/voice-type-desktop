@echo off
rem Puts Voice Bridge in the Start Menu and on the Desktop so you never have to
rem open this folder again. Run uninstall.bat to remove the shortcuts.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
