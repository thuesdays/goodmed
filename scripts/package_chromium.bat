@echo off
REM ============================================================
REM  package_chromium.bat — wrapper for package_chromium.ps1
REM
REM  Why this wrapper exists:
REM    Windows PowerShell's default execution policy refuses to
REM    run .ps1 files directly. Invoking the .ps1 via this .bat
REM    with -ExecutionPolicy Bypass works without altering any
REM    machine-wide settings.
REM
REM  Usage:
REM    .\scripts\package_chromium.bat
REM    .\scripts\package_chromium.bat -Version 0.2.0.3
REM ============================================================
setlocal
cd /d "%~dp0\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\package_chromium.ps1" %*
exit /b %errorlevel%
