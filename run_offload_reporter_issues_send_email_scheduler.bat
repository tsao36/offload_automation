@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || (
    echo [ERROR] Failed to change directory to %SCRIPT_DIR%
    exit /b 1
)

set "LOG_DIR=%SCRIPT_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"
if "%TS%"=="" set "TS=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "LOG_FILE=%LOG_DIR%\offload_scheduler_%TS%.log"

(
echo [INFO] ===== Offload job start =====
echo [INFO] Job: %~nx0
echo [INFO] Timestamp: %DATE% %TIME%
echo [INFO] Script dir: %SCRIPT_DIR%
echo [INFO] Current dir: %CD%
echo [INFO] Args: %*
)>>"%LOG_FILE%"

rem Check DB host reachability before proceeding (avoids 0x1 when VPN is down)
set "DB_HOST=10-108-27-113.dbaas.intel.com"
ping -n 1 -w 3000 "%DB_HOST%" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Cannot reach %DB_HOST% - VPN may be down. Skipping run.>>"%LOG_FILE%"
    echo [INFO] Exit code: 0 ^(skipped - no network^)>>"%LOG_FILE%"
    echo [INFO] ===== Offload job end =====>>"%LOG_FILE%"
    exit /b 0
)
echo [INFO] DB host reachable: %DB_HOST%>>"%LOG_FILE%"

rem Use original delegated Graph auth by default (interactive web login)
if "%GRAPH_AUTH_MODE%"=="" set "GRAPH_AUTH_MODE=delegated"

rem Cap receiving engineer workload for offload recommendation
if "%OFFLOAD_RECEIVER_MAX_ISSUES%"=="" set "OFFLOAD_RECEIVER_MAX_ISSUES=10"
if "%OFFLOAD_ALWAYS_EXCLUDED_REPORTERS%"=="" set "OFFLOAD_ALWAYS_EXCLUDED_REPORTERS=Jonathan Tsao"

if /i "%GRAPH_AUTH_MODE%"=="app" (
    if "%GRAPH_SENDER_UPN%"=="" (
        if exist "%SCRIPT_DIR%.env" (
            for /f "usebackq tokens=1,* delims==" %%A in ("%SCRIPT_DIR%.env") do (
                if /i "%%~A"=="GRAPH_SENDER_UPN" set "GRAPH_SENDER_UPN=%%~B"
                if /i "%%~A"=="DEFAULT_TO" if "%GRAPH_SENDER_UPN%"=="" set "GRAPH_SENDER_UPN=%%~B"
            )
        )
    )
    for /f "tokens=1 delims=,; " %%i in ("%GRAPH_SENDER_UPN%") do set "GRAPH_SENDER_UPN=%%~i"

    if "%GRAPH_SENDER_UPN%"=="" (
        echo [ERROR] GRAPH_SENDER_UPN is not set. Configure it in .env or Task Scheduler environment.>>"%LOG_FILE%"
        exit /b 1
    )
)

where python >>"%LOG_FILE%" 2>&1
echo [INFO] GRAPH_AUTH_MODE=%GRAPH_AUTH_MODE%>>"%LOG_FILE%"
echo [INFO] GRAPH_SENDER_UPN=%GRAPH_SENDER_UPN%>>"%LOG_FILE%"
echo [INFO] OFFLOAD_RECEIVER_MAX_ISSUES=%OFFLOAD_RECEIVER_MAX_ISSUES%>>"%LOG_FILE%"
echo [INFO] OFFLOAD_ALWAYS_EXCLUDED_REPORTERS=%OFFLOAD_ALWAYS_EXCLUDED_REPORTERS%>>"%LOG_FILE%"

call "%SCRIPT_DIR%run_offload_reporter_issues.bat" --send-email %* >>"%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

(
echo [INFO] Exit code: %EXIT_CODE%
echo [INFO] ===== Offload job end =====
)>>"%LOG_FILE%"

exit /b %EXIT_CODE%
