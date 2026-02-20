@echo off
setlocal

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -m app.windows_program gui
  goto :end
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python -m app.windows_program gui
  goto :end
)

echo [ERROR] Python launcher was not found.
echo.
echo Install Python 3.11+ from https://www.python.org/downloads/windows/
echo During install, check "Add python.exe to PATH".
echo Then run this file again.
pause

:end
endlocal
