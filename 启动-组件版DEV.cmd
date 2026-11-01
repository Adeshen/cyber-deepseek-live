@echo off
rem ============================================================
rem  DEV launcher: run the S2-COMPONENT digital human from
rem  SOURCE. Any change to src\*.py or stage\*.html takes
rem  effect on the next restart of this script.
rem
rem  ASCII-only on purpose (cmd.exe code-page safety).
rem  Docs live in README.md (UTF-8).
rem ============================================================
title CloudDH-S2-Component (SOURCE / dev)
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [X] venv python not found: %PY%
  echo     Create it first:
  echo       python -m venv .venv
  echo       .venv\Scripts\pip install -r requirements.txt
  echo     Or re-run tools\setup-venv.ps1
  pause
  exit /b 1
)

rem bridge.py imports sibling modules via relative imports, so run
rem as a module from the project root: python -m src.main
"%PY%" -m src.main %*

set "RC=%errorlevel%"
echo.
echo   [exit code %RC%]
pause
exit /b %RC%
