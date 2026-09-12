@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || (
    echo [ERROR] Failed to change directory to %SCRIPT_DIR%
    exit /b 1
)

set "PROJECT_ROOT=%SCRIPT_DIR%"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

set "PYTHONPATH=%PROJECT_ROOT%;%PROJECT_ROOT%\APIs;%PYTHONPATH%"
if "%OFFLOAD_CATEGORY_MODEL%"=="" set "OFFLOAD_CATEGORY_MODEL=%PROJECT_ROOT%\models\issue_category_model.joblib"
if "%OFFLOAD_CATEGORY_WEIGHT_MAP%"=="" set "OFFLOAD_CATEGORY_WEIGHT_MAP=%PROJECT_ROOT%\issue_category_weights.json"
if "%ISSUE_CATEGORY_PREDICT_BACKEND%"=="" set "ISSUE_CATEGORY_PREDICT_BACKEND=ml"

set "PY_EXE=python"
set "PY_ARGS="
if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set "PY_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
) else if exist "%SCRIPT_DIR%..\jira_customer_tat\customer issue analysis\.venv\Scripts\python.exe" (
    set "PY_EXE=%SCRIPT_DIR%..\jira_customer_tat\customer issue analysis\.venv\Scripts\python.exe"
) else (
    py -3.14 --version >nul 2>nul
    if not errorlevel 1 (
        set "PY_EXE=py"
        set "PY_ARGS=-3.14"
    )
)

if "%OFFLOAD_ALWAYS_EXCLUDED_ENGINEERS%"=="" (
    if not "%OFFLOAD_ALWAYS_EXCLUDED_REPORTERS%"=="" (
        set "OFFLOAD_ALWAYS_EXCLUDED_ENGINEERS=%OFFLOAD_ALWAYS_EXCLUDED_REPORTERS%"
    ) else (
        set "OFFLOAD_ALWAYS_EXCLUDED_ENGINEERS=Jonathan Tsao"
    )
)

echo [INFO] Running offload_reporter_issues.py %*
echo [INFO] OFFLOAD_ALWAYS_EXCLUDED_ENGINEERS=%OFFLOAD_ALWAYS_EXCLUDED_ENGINEERS%
echo [INFO] PYTHONPATH=%PYTHONPATH%
echo [INFO] PY_EXE=%PY_EXE%
echo [INFO] PY_ARGS=%PY_ARGS%
echo [INFO] OFFLOAD_CATEGORY_MODEL=%OFFLOAD_CATEGORY_MODEL%
echo [INFO] OFFLOAD_CATEGORY_WEIGHT_MAP=%OFFLOAD_CATEGORY_WEIGHT_MAP%
echo [INFO] ISSUE_CATEGORY_PREDICT_BACKEND=%ISSUE_CATEGORY_PREDICT_BACKEND%
"%PY_EXE%" %PY_ARGS% offload_reporter_issues.py %*
set "EXIT_CODE=%ERRORLEVEL%"

echo [INFO] Finished with exit code %EXIT_CODE%
exit /b %EXIT_CODE%
