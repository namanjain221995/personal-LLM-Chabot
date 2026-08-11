@echo off
setlocal
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0techsara.ps1" %*
exit /b %ERRORLEVEL%
