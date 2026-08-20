@echo off
rem One-click force IW38 GIR+DAL+PAZ+CLV harvest + FPSO dataset rebuild.
rem Double-click this or run from a shortcut. Requires SAP Logon / FR3 available.
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
call "%~dp0run_iw38_fpso_pipeline.cmd" --no-pbi
pause
