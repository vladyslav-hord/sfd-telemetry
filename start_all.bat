@echo off
setlocal
set "STEAM_PATH="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Valve\Steam" /v SteamPath 2^>nul') do set "STEAM_PATH=%%B"
if not defined STEAM_PATH (
  echo SteamPath was not found in HKCU registry.
  exit /b 2
)
set "SFD_GAME=%STEAM_PATH%\steamapps\common\Superfighters Deluxe\Superfighters Deluxe.exe"
if not exist "%SFD_GAME%" (
  echo SFD executable was not found: "%SFD_GAME%"
  exit /b 2
)
schtasks /Run /TN "SFD Telemetry Collector" >nul 2>&1
if errorlevel 1 start "SFD Telemetry Collector" /min cmd.exe /c call "%~dp0start_collector.bat"
schtasks /Run /TN "SFD Telemetry Analyzer Live" >nul 2>&1
if errorlevel 1 start "SFD Telemetry Analyzer Live" /min cmd.exe /c call "%~dp0start_analyzer.bat"
tasklist /FI "IMAGENAME eq Superfighters Deluxe Server.exe" 2>nul | find /I "Superfighters Deluxe Server.exe" >nul
if errorlevel 1 start "SFD Server" /min "%SFD_GAME%" -server -start -totray
endlocal
