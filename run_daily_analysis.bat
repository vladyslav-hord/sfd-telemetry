@echo off
setlocal
cd /d "%~dp0"
if not exist data\analysis mkdir data\analysis
py -3 -m analyzer.main daily --config analyzer\config.json >> data\analysis\analyzer.log 2>&1
exit /b %ERRORLEVEL%
