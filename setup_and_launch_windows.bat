@echo off
setlocal

echo === Therapy Scheduler POC: first-time setup ===

echo [1/3] Checking for Python...
where py >nul 2>nul
if %ERRORLEVEL%==0 goto :run

where python >nul 2>nul
if %ERRORLEVEL%==0 goto :run

echo.
echo [ERROR] Python was not found.
echo Install Python 3.11+ from https://www.python.org/downloads/windows/
echo During install, check "Add python.exe to PATH".
echo Then run this file again.
pause
goto :end

:run
echo [2/3] Running health check...
call run_healthcheck_windows.bat

echo.
echo [3/3] Launching GUI...
call launch_gui_windows.bat

:end
endlocal
