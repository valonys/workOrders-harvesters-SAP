@echo off
rem End-to-end CLV IW38 pipeline for Task Scheduler:
rem   0) Close leftover Power BI Desktop so CLV CSVs can be overwritten
rem   1) Harvest IW39 variant CLV-PG2026
rem   2) Rebuild dataset CSVs
rem   3) Refresh CLV_Inspection.pbix and publish/replace on the workspace
rem
rem Usage:
rem   run_iw38_clv_pipeline.cmd
rem   run_iw38_clv_pipeline.cmd --no-pbi
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
set "REFRESH_PBI=1"
if /I "%~1"=="--no-pbi" set "REFRESH_PBI=0"

if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

if "%REFRESH_PBI%"=="1" (
    echo [%DATE% %TIME%] Closing Power BI Desktop so CLV CSVs can be overwritten...
    powershell -STA -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh_clv_powerbi.ps1" -UnlockDataset
)

echo [%DATE% %TIME%] IW38 CLV harvest starting...
"%PY%" -m iw29_export iw38 --variants CLV-PG2026
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="8" set "CODE=0"
if not "%CODE%"=="0" (
    echo [%DATE% %TIME%] IW38 CLV harvest failed with code %CODE%
    exit /b %CODE%
)

echo [%DATE% %TIME%] IW38 CLV dataset refreshed.
if "%REFRESH_PBI%"=="1" (
    powershell -STA -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh_clv_powerbi.ps1"
    set "PBI_CODE=%ERRORLEVEL%"
    if not "%PBI_CODE%"=="0" (
        echo [%DATE% %TIME%] Power BI refresh/publish returned %PBI_CODE% ^(CSV data is still updated^)
    )
)

echo [%DATE% %TIME%] CLV pipeline done.
exit /b 0
