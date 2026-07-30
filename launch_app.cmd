@echo off
rem Double-click this to open the desktop app. The console stays open only if
rem something goes wrong, so you can read the error.
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

python -m iw29_export gui
if errorlevel 1 (
    echo.
    echo The app exited with code %ERRORLEVEL%.
    echo Full details are in "%CD%\logs\iw29_export.log".
    echo.
    pause
)
