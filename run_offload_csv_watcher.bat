@echo off
setlocal

cd /d "%~dp0"

set "PY=py -3.14"
if exist ".\.venv\Scripts\python.exe" (
	.\.venv\Scripts\python.exe --version >nul 2>nul
	if not errorlevel 1 (
		set "PY=.\.venv\Scripts\python.exe"
	)
)

echo Starting offload CSV watcher...
%PY% watch_offload_csv_notify.py --interval-sec 60 %*

endlocal
