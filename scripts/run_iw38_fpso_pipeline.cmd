@echo off
rem End-to-end GIR+DAL+PAZ+CLV IW38 pipeline for Task Scheduler:
rem   1) Harvest IW39 variants GIR/DAL/PAZ/CLV-PG2026
rem   2) Upsert each site into dataset\FPSO_wo_fact.csv (+ summary/matrix)
rem   3) Optionally open the FPSO Power BI report
rem
rem Usage:
rem   run_iw38_fpso_pipeline.cmd
rem   run_iw38_fpso_pipeline.cmd --no-pbi
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

echo [%DATE% %TIME%] IW38 FPSO harvest starting (GIR DAL PAZ CLV)...
"%PY%" -m iw29_export iw38 --variants GIR-PG2026 DAL-PG2026 PAZ-PG2026 CLV-PG2026
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="8" set "CODE=0"
if not "%CODE%"=="0" (
    echo [%DATE% %TIME%] IW38 FPSO harvest failed with code %CODE%
    exit /b %CODE%
)

echo [%DATE% %TIME%] IW38 FPSO dataset refreshed.
if "%REFRESH_PBI%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh_fpso_powerbi.ps1"
    set "PBI_CODE=%ERRORLEVEL%"
    if not "%PBI_CODE%"=="0" (
        echo [%DATE% %TIME%] Power BI refresh returned %PBI_CODE% ^(CSV data is still updated^)
    )
)

echo [%DATE% %TIME%] FPSO pipeline done.
exit /b 0
