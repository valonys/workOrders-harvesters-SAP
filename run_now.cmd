@echo off
rem One-click export: no form, no options, just does the job and tells you how it
rem went. This is also what the scheduled task runs.
title IW29 export
cd /d "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

echo Exporting SAP IW29 to the synced SharePoint folder...
echo Leave SAP alone while this runs.
echo.

python -m iw29_export run
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
    echo Done. The workbook is in the OneDrive folder and will sync to SharePoint.
) else if "%CODE%"=="8" (
    echo The report ran, but nothing matched the selection. Nothing was saved.
) else (
    echo FAILED with exit code %CODE%.
    echo Details: "%CD%\logs\iw29_export.log"
)

echo.
echo This window stays open so you can read the result. Close it when done.
pause >nul
