param(
    [switch]$OneDir
)

Write-Host "Building Therapy Scheduler executable..."

$pyInstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyInstaller) {
    Write-Error "pyinstaller not found. Install with: python -m pip install pyinstaller"
    exit 1
}

if ($OneDir) {
    pyinstaller --noconfirm --name TherapySchedulerPOC --windowed --collect-all tkinter app\windows_gui.py
} else {
    pyinstaller --noconfirm --onefile --name TherapySchedulerPOC --windowed --collect-all tkinter app\windows_gui.py
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Build failed"
    exit $LASTEXITCODE
}

Write-Host "Build complete. Check dist\TherapySchedulerPOC.exe"
