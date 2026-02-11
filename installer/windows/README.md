# Windows Single-EXE Packaging (PyInstaller)

This project can be packaged into one executable for non-technical users.

## Prerequisites
- Windows machine
- Python 3.10+
- pip

## Steps
1. Open PowerShell in the project root.
2. Install PyInstaller:
   ```powershell
   python -m pip install pyinstaller
   ```
3. Build one-file executable:
   ```powershell
   .\installer\windows\build_exe.ps1
   ```
4. Output executable:
   - `dist\TherapySchedulerPOC.exe`

## Notes
- `--onefile` creates a single EXE (slower startup, easiest sharing).
- To build folder mode (faster startup):
  ```powershell
  .\installer\windows\build_exe.ps1 -OneDir
  ```
