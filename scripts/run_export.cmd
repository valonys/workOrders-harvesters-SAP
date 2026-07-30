@echo off
rem Task Scheduler entry point. Any arguments are passed straight through,
rem e.g. run_export.cmd run --days 1
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"

if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m iw29_export %*
set "CODE=%ERRORLEVEL%"

rem 0 = exported, 8 = ran fine but nothing matched. Both are non-events for
rem Task Scheduler; anything else deserves an alert.
if "%CODE%"=="8" set "CODE=0"
exit /b %CODE%
