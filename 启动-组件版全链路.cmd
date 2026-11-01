@echo off
REM ============================================================
REM  One-click: Vidu S2 component bridge + danmaku chain + stage page
REM  (OmniVoice 9877 / bridge 8795 / reader.py -> tray icon)
REM  ASCII-only on purpose: Chinese output is printed by the .ps1
REM  (start-component-stack.ps1 must be saved as UTF-8 with BOM)
REM  2026-10-17: every run appends a full log to logs\launcher.log
REM              (the window used to close after 8s, so failures left no trace)
REM ============================================================
title Vidu S2 component - danmaku full chain
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-component-stack.ps1" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo [ERROR] launcher failed, exit code %RC%
  echo Log file: %~dp0logs\launcher.log
  pause
  exit /b %RC%
)
REM services are hosted by scheduled tasks (no window). keep window 8s for the summary
timeout /t 8 >nul 2>&1
exit /b 0
