@echo off
setlocal

rem Ensure module launches relative to this script's directory when double-clicked.
pushd "%~dp0" >nul 2>nul
if not %ERRORLEVEL%==0 (
  echo [ERROR] Could not switch to script directory: %~dp0
  pause
  goto :end
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -m app.windows_program gui
  if not %ERRORLEVEL%==0 (
    echo.
    echo [ERROR] Failed to launch GUI. Exit code: %ERRORLEVEL%
    echo If a traceback flashed by, run this from Command Prompt to inspect details:
    echo     py -3 -m app.windows_program gui
    pause
  )
  goto :end
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python -m app.windows_program gui
  if not %ERRORLEVEL%==0 (
    echo.
    echo [ERROR] Failed to launch GUI. Exit code: %ERRORLEVEL%
    echo If a traceback flashed by, run this from Command Prompt to inspect details:
    echo     python -m app.windows_program gui
    pause
  )
  goto :end
)

echo [ERROR] Python launcher was not found.
echo.
echo Install Python 3.11+ from https://www.python.org/downloads/windows/
echo During install, check "Add python.exe to PATH".
echo Then run this file again.
pause

:end
popd >nul 2>nul
endlocal
