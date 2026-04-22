@echo off
REM ============================================================
REM  Ghost Shell — deploy-only (no build)
REM
REM  Copies an already-built chrome.exe + its runtime files from
REM  F:\projects\chromium\src\out\GhostShell\
REM  to
REM  F:\projects\goodmedika\chrome_win64\<version>\
REM
REM  Reads Chromium version from chrome\VERSION automatically.
REM  Updates the "latest" junction to point at the new version.
REM
REM  Usage (from anywhere):
REM     deploy-ghost-shell.bat
REM ============================================================

setlocal EnableDelayedExpansion

REM ─── Config ─────────────────────────────────────────────────
set "CHROMIUM_SRC=F:\projects\chromium\src"
set "BUILD_DIR=out\GhostShell"
set "DEPLOY_ROOT=F:\projects\goodmedika\chrome_win64"
set "LATEST_JUNCTION=F:\projects\goodmedika\chrome_win64\latest"
REM ────────────────────────────────────────────────────────────

echo.
echo ============================================================
echo  Ghost Shell deploy
echo ============================================================
echo  Source    : %CHROMIUM_SRC%\%BUILD_DIR%
echo  Target    : %DEPLOY_ROOT%
echo ============================================================
echo.

REM ─── Validate source ────────────────────────────────────────
set "SRC=%CHROMIUM_SRC%\%BUILD_DIR%"
set "CHROME_EXE=%SRC%\chrome.exe"

if not exist "%CHROME_EXE%" (
    echo [ERROR] chrome.exe not found at %CHROME_EXE%
    echo         Build it first with:
    echo             autoninja -C %BUILD_DIR% chrome
    exit /b 1
)

REM ─── Read version from chrome\VERSION ───────────────────────
set "VERSION_FILE=%CHROMIUM_SRC%\chrome\VERSION"
if not exist "!VERSION_FILE!" (
    echo [ERROR] VERSION file not found at !VERSION_FILE!
    exit /b 1
)

set "MAJOR="
set "MINOR="
set "BUILD="
set "PATCH="
for /f "usebackq tokens=1,2 delims==" %%A in ("!VERSION_FILE!") do (
    if /i "%%A"=="MAJOR" set "MAJOR=%%B"
    if /i "%%A"=="MINOR" set "MINOR=%%B"
    if /i "%%A"=="BUILD" set "BUILD=%%B"
    if /i "%%A"=="PATCH" set "PATCH=%%B"
)
set "VERSION=!MAJOR!.!MINOR!.!BUILD!.!PATCH!"
echo [1/4] Detected Chromium version: !VERSION!

REM ─── Prepare deploy directory ───────────────────────────────
set "DEPLOY_DIR=%DEPLOY_ROOT%\!VERSION!"
echo [2/4] Deploying to !DEPLOY_DIR! ...
if not exist "%DEPLOY_ROOT%" mkdir "%DEPLOY_ROOT%"
if exist "!DEPLOY_DIR!" (
    echo   Existing directory found - overwriting.
    rmdir /S /Q "!DEPLOY_DIR!"
)
mkdir "!DEPLOY_DIR!"
mkdir "!DEPLOY_DIR!\locales"

REM ─── Copy runtime files ─────────────────────────────────────
echo [3/4] Copying files ...

REM --- Required executables ---
call :copy_file "chrome.exe"
call :copy_file "crashpad_handler.exe"

REM --- Core DLLs ---
call :copy_file "chrome.dll"
call :copy_file "chrome_elf.dll"
call :copy_file "d3dcompiler_47.dll"
call :copy_file "libEGL.dll"
call :copy_file "libGLESv2.dll"
call :copy_file "vk_swiftshader.dll"
call :copy_file_optional "vulkan-1.dll"

REM --- Resources ---
call :copy_file "resources.pak"
call :copy_file "chrome_100_percent.pak"
call :copy_file "chrome_200_percent.pak"

REM --- Snapshot blobs ---
call :copy_file "v8_context_snapshot.bin"
call :copy_file_optional "snapshot_blob.bin"

REM --- ICU data ---
call :copy_file "icudtl.dat"

REM --- Vulkan config ---
call :copy_file "vk_swiftshader_icd.json"

REM --- MSVC runtimes (may or may not be in build dir) ---
call :copy_file_optional "msvcp140.dll"
call :copy_file_optional "vcruntime140.dll"
call :copy_file_optional "vcruntime140_1.dll"

REM --- Version manifest (empty file named like version, used by shell) ---
type nul > "!DEPLOY_DIR!\!VERSION!.manifest"

REM --- locales directory ---
if exist "%SRC%\locales" (
    xcopy /E /I /Y /Q "%SRC%\locales" "!DEPLOY_DIR!\locales" >nul
    echo   locales\          copied
) else (
    echo   [warn] locales\ missing in build dir
)

REM ─── Update "latest" junction ───────────────────────────────
echo [4/4] Updating 'latest' junction ...
if exist "%LATEST_JUNCTION%" (
    rmdir "%LATEST_JUNCTION%" 2>nul
    if exist "%LATEST_JUNCTION%" (
        rmdir /S /Q "%LATEST_JUNCTION%"
    )
)
mklink /J "%LATEST_JUNCTION%" "!DEPLOY_DIR!" >nul
if errorlevel 1 (
    echo   [warn] could not create junction
) else (
    echo   %LATEST_JUNCTION% -^> !DEPLOY_DIR!
)

echo.
echo ============================================================
echo  DEPLOY COMPLETE
echo ============================================================
echo  Version         : !VERSION!
echo  Deployed to     : !DEPLOY_DIR!
echo  Launcher path   : !DEPLOY_DIR!\chrome.exe
echo  Latest junction : %LATEST_JUNCTION%\chrome.exe
echo ============================================================
echo.

endlocal
exit /b 0


REM ─── Subroutines ────────────────────────────────────────────

:copy_file
REM %1 = filename (relative to build dir) — required
if exist "%SRC%\%~1" (
    copy /Y "%SRC%\%~1" "!DEPLOY_DIR!\%~1" >nul
    echo   %~1
) else (
    echo   [ERROR] %~1 missing in build output
    exit /b 1
)
goto :eof

:copy_file_optional
REM %1 = filename — warn if missing but don't fail
if exist "%SRC%\%~1" (
    copy /Y "%SRC%\%~1" "!DEPLOY_DIR!\%~1" >nul
    echo   %~1
) else (
    echo   [skip] %~1
)
goto :eof
