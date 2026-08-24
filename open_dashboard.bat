@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "dashboard_url=http://127.0.0.1:8765/index.html"
set "launch_lock=data\dashboard\.http-server-launch.lock"
if not exist "data\dashboard\index.html" (
  echo Dashboard is not built yet. Run: py -3 -m analyzer.main dashboard --config analyzer\config.json
  exit /b 1
)

rem Reuse a healthy listener; this keeps repeated launches single-instance.
call :server_state
if !errorlevel! EQU 0 goto :open_browser
if !errorlevel! EQU 2 goto :wait_for_existing
goto :start_server

:wait_for_existing
for /l %%N in (1,1,10) do (
  call :server_state
  if !errorlevel! EQU 0 goto :open_browser
  if !errorlevel! EQU 1 goto :start_server
  >NUL timeout /t 1 /nobreak
)
call :stop_matching_servers
rmdir /s /q "!launch_lock!" >NUL 2>&1
goto :start_server

:start_server
2>NUL mkdir "!launch_lock!"
if !errorlevel! EQU 1 goto :wait_for_existing
call :server_state
if !errorlevel! EQU 0 goto :server_ready
if !errorlevel! EQU 2 call :stop_matching_servers
start "SFD Dashboard Server" /b py -3 -m http.server 8765 --directory "%~dp0data\dashboard" >NUL 2>&1
for /l %%N in (1,1,10) do (
  call :server_state
  if !errorlevel! EQU 0 goto :server_ready
  >NUL timeout /t 1 /nobreak
)
rmdir /s /q "!launch_lock!" >NUL 2>&1
echo Dashboard server did not become healthy on port 8765.
exit /b 1

:server_ready
rmdir /s /q "!launch_lock!" >NUL 2>&1
goto :open_browser

:open_browser
start "SFD Telemetry Dashboard" "!dashboard_url!"
exit /b 0

:server_state
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$healthy=$false; try {$response=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:8765/index.html'; $healthy=$response.StatusCode -eq 200} catch {}; if($healthy){exit 0}; $running=Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -and $_.CommandLine -match 'http\.server\s+8765' -and $_.CommandLine -match 'data\\dashboard'}; if($running){exit 2}; exit 1"
exit /b !errorlevel!

:stop_matching_servers
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$running=Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -and $_.CommandLine -match 'http\.server\s+8765' -and $_.CommandLine -match 'data\\dashboard'}; $running | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}"
exit /b 0
