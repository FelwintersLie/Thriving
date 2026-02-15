@echo off
setlocal

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -m app.windows_program api --host 127.0.0.1 --port 8080
  goto :end
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python -m app.windows_program api --host 127.0.0.1 --port 8080
  goto :end
)

echo [ERROR] Python launcher was not found.
echo Install Python 3.11+ and try again.
pause

:end
endlocal
