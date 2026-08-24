@echo off
setlocal
cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"
set "LOG=data\logs\collector.log"

:restart
echo [%date% %time%] collector starting >> "%LOG%"
if exist "collector\config.json" (
  py -3 -m collector.main --config "collector\config.json" run >> "%LOG%" 2>&1
) else (
  py -3 -m collector.main --config "collector\config.example.json" run >> "%LOG%" 2>&1
)
echo [%date% %time%] collector exited with code %ERRORLEVEL%; restarting in 5 seconds >> "%LOG%"
timeout /t 5 /nobreak >nul
goto restart
