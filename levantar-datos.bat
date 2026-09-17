@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%dist\datos.exe" (
    "%SCRIPT_DIR%dist\datos.exe" %*
) else if exist "%SCRIPT_DIR%datos.exe" (
    "%SCRIPT_DIR%datos.exe" %*
) else (
    python "%SCRIPT_DIR%datos.py" %*
)
