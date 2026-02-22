@echo off
setlocal

echo === THRIVE Scheduler: first-time setup ===

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
call "%~dp0run_healthcheck_windows.bat"
if not %ERRORLEVEL%==0 (
  echo.
  echo [ERROR] Health check failed. Resolve the error above, then try again.
  pause
  goto :end
)

echo.
echo [3/3] Launching GUI...
call "%~dp0THRIVE Scheduler.bat"

:end
endlocal
