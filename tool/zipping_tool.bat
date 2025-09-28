@echo off
setlocal EnableExtensions

rem ============================================
rem  zipping_tool.bat (wrapper)
rem  Call PowerShell script with JSON config.
rem ============================================

set "BASEDIR=%~dp0"
set "PS1=%BASEDIR%zipping_tool.ps1"

if not exist "%PS1%" (
  echo ERROR: PowerShell script not found: "%PS1%"
  exit /b 2
)

rem ---- config path (default: zipping_tool.json in same folder) ----
if "%~1"=="" (
  set "CFG=%BASEDIR%zipping_tool.json"
) else (
  set "CFG=%~1"
  shift
)

set "EXTRA=%*"

echo Running: powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -ConfigPath "%CFG%" %EXTRA%
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -ConfigPath "%CFG%" %EXTRA%
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo Failed. ERRORLEVEL=%RC%
  exit /b %RC%
)

echo Done.
exit /b 0
