@echo off
REM ============================================================
REM  download_chromium.bat — wrapper for download_chromium.ps1
REM
REM  Pulls the patched Chromium binary from the latest GitHub
REM  release asset and unpacks it into chrome_win64\.
REM
REM  Usage:
REM    .\scripts\download_chromium.bat
REM    .\scripts\download_chromium.bat -Tag v0.2.0.3
REM    .\scripts\download_chromium.bat -Force
REM ============================================================
setlocal
cd /d "%~dp0\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\download_chromium.ps1" %*
exit /b %errorlevel%
