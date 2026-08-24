@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"
if exist "OPENAI_API" (
  for /f "usebackq delims=" %%K in ("%~dp0OPENAI_API") do if not defined OPENAI_API_KEY set "OPENAI_API_KEY=%%K"
)

:supervise
echo [%date% %time%] analyzer starting >> "data\logs\analyzer-live.log"
py -3 -m analyzer.main live --config analyzer\config.json >> "data\logs\analyzer-live.log" 2>> "data\logs\analyzer-live.err.log"
echo [%date% %time%] analyzer exited with code %ERRORLEVEL% >> "data\logs\analyzer-live.log"
goto supervise
