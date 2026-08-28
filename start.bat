@echo off
title Voice Bridge
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was not found on PATH.
  echo Install Python 3 from python.org and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

python voicebridge.py
pause
