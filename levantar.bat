@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%dist\datos.exe" (
    "%SCRIPT_DIR%dist\datos.exe" redis %*
) else if exist "%SCRIPT_DIR%datos.exe" (
    "%SCRIPT_DIR%datos.exe" redis %*
) else (
    python "%SCRIPT_DIR%datos.py" redis %*
)
