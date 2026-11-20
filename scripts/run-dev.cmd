@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*
if errorlevel 1 (
  echo.
  echo Dev script failed. Press any key to close...
  pause >nul
)
endlocal
