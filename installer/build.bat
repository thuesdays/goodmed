@echo off
REM ============================================================
REM  Build the Ghost Shell installer .exe
REM
REM  Prerequisites:
REM    - Inno Setup 6+ from https://jrsoftware.org/isinfo.php
REM    - A Python 3.12 installer at deps\python-3.12.x-amd64.exe
REM      (download from https://www.python.org/downloads/windows/)
REM
REM  Output: installer\output\GhostShellAntySetup.exe
REM ============================================================

setlocal
cd /d "%~dp0"

REM Find ISCC.exe — try the standard install paths for Inno 5/6/7,
REM then fall back to anything on the user's PATH.
set "ISCC="
for %%P in (
    "C:\Program Files\Inno Setup 7\ISCC.exe"
    "C:\Program Files (x86)\Inno Setup 7\ISCC.exe"
    "C:\Program Files\Inno Setup 6\ISCC.exe"
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    "C:\Program Files\Inno Setup 5\ISCC.exe"
    "C:\Program Files (x86)\Inno Setup 5\ISCC.exe"
) do (
    if exist %%P set "ISCC=%%~P"
)

REM Fallback: try the PATH. `where` exits 0 if found and prints the path.
if not defined ISCC (
    for /f "delims=" %%P in ('where ISCC.exe 2^>nul') do (
        if not defined ISCC set "ISCC=%%P"
    )
)

if not defined ISCC (
    echo [x] Inno Setup not found.
    echo     Install from https://jrsoftware.org/isinfo.php and re-run.
    echo     Tried: Program Files\Inno Setup 5..7 and PATH.
    pause
    exit /b 1
)

echo [*] Using Inno: %ISCC%

REM Look for a Python installer in deps\ — prefer the newest
REM stable line we know works (3.13 > 3.12 > 3.11). Falls through if
REM the user dropped in any python-3.*-amd64.exe matching the glob.
set "PYBOX="
for %%V in (3.13 3.12 3.11) do (
    if not defined PYBOX (
        for /f "delims=" %%F in ('dir /b /o-n "deps\python-%%V*-amd64.exe" 2^>nul') do (
            if not defined PYBOX set "PYBOX=%%~nxF"
        )
    )
)
REM Last-ditch: any other python-3.*-amd64.exe (e.g. 3.14 future)
if not defined PYBOX (
    for /f "delims=" %%F in ('dir /b /o-n "deps\python-3.*-amd64.exe" 2^>nul') do (
        if not defined PYBOX set "PYBOX=%%~nxF"
    )
)

if not defined PYBOX (
    echo [x] No Python installer found in deps\
    echo     Drop python-3.13.x-amd64.exe ^(or 3.12 / 3.11^) into installer\deps\
    echo     Download from https://www.python.org/downloads/windows/
    pause
    exit /b 1
)
echo [*] Bundling: %PYBOX%

REM Compile (Inno picks up the #define from the .iss file directly;
REM if the version pattern doesn't match what's in the .iss, edit the
REM PyInstaller line at the top of ghost_shell_installer.iss)
"%ISCC%" /Q "ghost_shell_installer.iss" /D"PyInstaller=deps\%PYBOX%"
if errorlevel 1 (
    echo [x] Compile failed.
    pause
    exit /b 1
)

echo.
echo === Installer built ===
echo  output\GhostShellAntySetup.exe
echo.
explorer output
pause
exit /b 0
