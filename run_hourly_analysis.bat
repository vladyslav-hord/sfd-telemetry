@echo off
setlocal
cd /d "%~dp0"
if not exist data\analysis mkdir data\analysis
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run_batch_analysis.ps1" -Mode hourly >> data\analysis\analyzer.log 2>&1
exit /b %ERRORLEVEL%
