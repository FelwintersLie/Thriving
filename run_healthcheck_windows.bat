@echo off
setlocal

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -m app.health_check
  goto :end
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python -m app.health_check
  goto :end
)

echo [ERROR] Python launcher was not found.
echo Install Python 3.11+ and try again.
pause

:end
endlocal
