@echo off
setlocal
rem 便携版打包入口：优先 PowerShell 7（pwsh），没有时回落 Windows PowerShell 5.1。
rem 参数原样透传，例如：build-release.cmd -Verify / -BumpVersion 1.3.0 / -SkipFrontend
set "PS=powershell"
where pwsh >nul 2>nul && set "PS=pwsh"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-release.ps1" %*
exit /b %errorlevel%
