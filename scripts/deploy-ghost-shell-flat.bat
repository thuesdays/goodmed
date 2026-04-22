@echo off
REM ============================================================
REM  Ghost Shell - flat deploy (no version subfolder)
REM
REM  Inlines all copy operations to avoid the cmd.exe subroutine
REM  label-caching quirk that strikes after ~5 'call :label' uses.
REM ============================================================

setlocal EnableDelayedExpansion

REM --- Config ---
set "CHROMIUM_SRC=F:\projects\chromium\src"
set "BUILD_DIR=out\GhostShell"
set "DEPLOY_DIR=F:\projects\goodmedika\chrome_win64"

set "SRC=%CHROMIUM_SRC%\%BUILD_DIR%"

echo.
echo ============================================================
echo  Ghost Shell flat deploy
echo ============================================================
echo  Source : %SRC%
echo  Target : %DEPLOY_DIR%
echo ============================================================
echo.

REM --- Validate source ---
if not exist "%SRC%\chrome.exe" (
    echo [ERROR] chrome.exe not found at %SRC%\chrome.exe
    echo         Build it first: autoninja -C %BUILD_DIR% chrome
    exit /b 1
)

REM --- Read version ---
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
echo Chromium version: !VERSION!
echo.

REM --- Wipe target & recreate ---
echo [1/4] Preparing target ...
if exist "%DEPLOY_DIR%" rmdir /S /Q "%DEPLOY_DIR%"
mkdir "%DEPLOY_DIR%"
mkdir "%DEPLOY_DIR%\locales"
echo.

REM --- Required files (inline loop) ---
echo [2/4] Copying required files ...
set "MISSING="
for %%F in (
    chrome.exe
    chrome.dll
    chrome_elf.dll
    d3dcompiler_47.dll
    libEGL.dll
    libGLESv2.dll
    vk_swiftshader.dll
    resources.pak
    chrome_100_percent.pak
    chrome_200_percent.pak
    v8_context_snapshot.bin
    icudtl.dat
    vk_swiftshader_icd.json
) do (
    if exist "%SRC%\%%F" (
        copy /Y "%SRC%\%%F" "%DEPLOY_DIR%\%%F" >nul
        echo   %%F
    ) else (
        echo   [ERROR] %%F missing in build output
        set "MISSING=1"
    )
)
echo.

REM --- SxS manifest (critical) ---
echo [3/4] Copying SxS manifest ...
if exist "%SRC%\!VERSION!.manifest" (
    copy /Y "%SRC%\!VERSION!.manifest" "%DEPLOY_DIR%\!VERSION!.manifest" >nul
    echo   !VERSION!.manifest   [critical - copied from build]
) else (
    echo   [WARN] !VERSION!.manifest not found in build dir
    echo          Generating a minimal fallback ...
    (
        echo ^<?xml version='1.0' encoding='UTF-8' standalone='yes'?^>
        echo ^<assembly xmlns='urn:schemas-microsoft-com:asm.v1' manifestVersion='1.0'^>
        echo   ^<assemblyIdentity type='win32' name='!VERSION!' version='!VERSION!' processorArchitecture='amd64'/^>
        echo ^</assembly^>
    ) > "%DEPLOY_DIR%\!VERSION!.manifest"
    echo   !VERSION!.manifest   [generated fallback]
)
echo.

REM --- Optional files (inline loop) ---
echo [4/4] Copying optional files ...
for %%F in (
    crashpad_handler.exe
    chromedriver.exe
    snapshot_blob.bin
    vulkan-1.dll
    msvcp140.dll
    vcruntime140.dll
    vcruntime140_1.dll
) do (
    if exist "%SRC%\%%F" (
        copy /Y "%SRC%\%%F" "%DEPLOY_DIR%\%%F" >nul
        echo   %%F
    ) else (
        echo   [skip] %%F ^(not in build^)
    )
)
echo.

REM --- locales\ ---
if exist "%SRC%\locales" (
    xcopy /E /I /Y /Q "%SRC%\locales" "%DEPLOY_DIR%\locales" >nul
    echo   locales\          copied
) else (
    echo   [warn] locales\ missing in build dir
)

REM --- Warn if crashpad_handler is missing ---
if not exist "%DEPLOY_DIR%\crashpad_handler.exe" (
    echo.
    echo ============================================================
    echo  NOTICE: crashpad_handler.exe was not in the build output.
    echo          chrome.exe will FAIL TO START silently without it.
    echo          To build it:
    echo              cd %CHROMIUM_SRC%
    echo              autoninja -C %BUILD_DIR% crashpad_handler
    echo          Then re-run this deploy script.
    echo ============================================================
)

echo.
if defined MISSING (
    echo ============================================================
    echo  DEPLOY FAILED - some required files missing
    echo ============================================================
    exit /b 1
)
echo ============================================================
echo  DEPLOY COMPLETE
echo ============================================================
echo  chrome.exe : %DEPLOY_DIR%\chrome.exe
echo.
echo  Set this path in dashboard (Profile detail page):
echo      %DEPLOY_DIR%\chrome.exe
echo ============================================================
echo.

endlocal
exit /b 0
