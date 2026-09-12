@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || (
    echo [ERROR] Failed to change directory to %SCRIPT_DIR%
    exit /b 1
)

if "%OFFLOAD_ALWAYS_EXCLUDED_REPORTERS%"=="" set "OFFLOAD_ALWAYS_EXCLUDED_REPORTERS=Jonathan Tsao"

call "%SCRIPT_DIR%run_offload_reporter_issues.bat" %*
exit /b %ERRORLEVEL%
