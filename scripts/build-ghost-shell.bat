@echo off
REM ============================================================
REM  Ghost Shell - Chromium build + flat deploy
REM
REM  1. Runs autoninja for BOTH chrome and crashpad_handler
REM  2. Reads Chromium version from chrome\VERSION
REM  3. Copies runtime files + SxS manifest to
REM     F:\projects\goodmedika\chrome_win64\
REM
REM  No versioned subfolder, no junctions - each run overwrites
REM  the previous chrome_win64\ directory.
REM
REM  Usage (from anywhere):
REM     build-ghost-shell.bat              build + deploy
REM     build-ghost-shell.bat /clean       wipe out\GhostShell first
REM     build-ghost-shell.bat /skip-build  only deploy existing build
REM ============================================================

setlocal EnableDelayedExpansion

REM --- Config ---
set "CHROMIUM_SRC=F:\projects\chromium\src"
set "BUILD_DIR=out\GhostShell"
set "DEPLOY_DIR=F:\projects\goodmedika\chrome_win64"

set "SRC=%CHROMIUM_SRC%\%BUILD_DIR%"

REM --- Parse flags ---
set "FLAG_CLEAN=0"
set "FLAG_SKIP_BUILD=0"
for %%A in (%*) do (
    if /i "%%A"=="/clean"      set "FLAG_CLEAN=1"
    if /i "%%A"=="/skip-build" set "FLAG_SKIP_BUILD=1"
)

echo.
echo ============================================================
echo  Ghost Shell build ^& deploy
echo ============================================================
echo  Chromium src : %CHROMIUM_SRC%
echo  Build dir    : %BUILD_DIR%
echo  Deploy to    : %DEPLOY_DIR%
echo ============================================================
echo.

REM --- Validate source tree ---
if not exist "%CHROMIUM_SRC%\BUILD.gn" (
    echo [ERROR] %CHROMIUM_SRC% does not look like a Chromium source tree.
    exit /b 1
)

pushd "%CHROMIUM_SRC%"

REM --- Optional clean ---
if "%FLAG_CLEAN%"=="1" (
    echo [1/6] Cleaning %BUILD_DIR% ...
    if exist "%BUILD_DIR%" (
        if exist "%BUILD_DIR%\args.gn" (
            copy /Y "%BUILD_DIR%\args.gn" "%TEMP%\ghost_shell_args_backup.gn" >nul
            echo   args.gn backed up to %TEMP%\ghost_shell_args_backup.gn
        )
        rmdir /S /Q "%BUILD_DIR%"
    )
    gn gen "%BUILD_DIR%"
    if errorlevel 1 (
        echo [ERROR] gn gen failed
        popd
        exit /b 1
    )
    if exist "%TEMP%\ghost_shell_args_backup.gn" (
        copy /Y "%TEMP%\ghost_shell_args_backup.gn" "%BUILD_DIR%\args.gn" >nul
        echo   args.gn restored from backup
        gn gen "%BUILD_DIR%"
    )
    echo.
) else (
    echo [1/6] Skipping clean
    echo.
)

REM --- Build chrome + chromedriver + crashpad_handler ---
if "%FLAG_SKIP_BUILD%"=="1" (
    echo [2/6] Skipping build ^(/skip-build^)
    echo.
) else (
    echo [2/6] Running autoninja for chrome + chromedriver + crashpad_handler ...
    call autoninja -C "%BUILD_DIR%" chrome chromedriver crashpad_handler
    if errorlevel 1 (
        echo.
        echo [ERROR] Build failed. Fix errors and re-run.
        popd
        exit /b 1
    )
    echo.
)

REM --- Verify chrome.exe exists ---
if not exist "%SRC%\chrome.exe" (
    echo [ERROR] chrome.exe not found at %SRC%\chrome.exe
    popd
    exit /b 1
)

REM --- Read version from chrome\VERSION ---
set "VERSION_FILE=%CHROMIUM_SRC%\chrome\VERSION"
if not exist "!VERSION_FILE!" (
    echo [ERROR] VERSION file not found at !VERSION_FILE!
    popd
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
echo [3/6] Detected Chromium version: !VERSION!
echo.

REM --- Wipe target & recreate ---
echo [4/6] Preparing %DEPLOY_DIR% ...
if exist "%DEPLOY_DIR%" rmdir /S /Q "%DEPLOY_DIR%"
mkdir "%DEPLOY_DIR%"
mkdir "%DEPLOY_DIR%\locales"
echo.

REM --- Copy required files (inline loop, no subroutines) ---
echo [5/6] Copying files ...
set "MISSING="

echo   Required:
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
        echo     %%F
    ) else (
        echo     [ERROR] %%F missing in build output
        set "MISSING=1"
    )
)

REM --- SxS manifest (critical) ---
echo   Manifest:
if exist "%SRC%\!VERSION!.manifest" (
    copy /Y "%SRC%\!VERSION!.manifest" "%DEPLOY_DIR%\!VERSION!.manifest" >nul
    echo     !VERSION!.manifest   [critical - copied from build]
) else (
    echo     [WARN] !VERSION!.manifest not found in build dir
    echo            Generating a minimal fallback ...
    (
        echo ^<?xml version='1.0' encoding='UTF-8' standalone='yes'?^>
        echo ^<assembly xmlns='urn:schemas-microsoft-com:asm.v1' manifestVersion='1.0'^>
        echo   ^<assemblyIdentity type='win32' name='!VERSION!' version='!VERSION!' processorArchitecture='amd64'/^>
        echo ^</assembly^>
    ) > "%DEPLOY_DIR%\!VERSION!.manifest"
    echo     !VERSION!.manifest   [generated fallback]
)

REM --- Optional files ---
echo   Optional:
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
        echo     %%F
    ) else (
        echo     [skip] %%F
    )
)

REM --- locales\ ---
echo   Locales:
if exist "%SRC%\locales" (
    xcopy /E /I /Y /Q "%SRC%\locales" "%DEPLOY_DIR%\locales" >nul
    echo     locales\          copied
) else (
    echo     [warn] locales\ missing in build dir
)
echo.

REM --- Sanity check: crashpad_handler.exe is required ---
echo [6/6] Final checks ...
if not exist "%DEPLOY_DIR%\crashpad_handler.exe" (
    echo   [WARN] crashpad_handler.exe missing - chrome.exe may fail silently.
    echo          The build step above should have built it. If it did not,
    echo          check your args.gn and try:
    echo              autoninja -C %BUILD_DIR% crashpad_handler
) else (
    echo   crashpad_handler.exe present - chrome should start OK.
)
echo.

popd

if defined MISSING (
    echo ============================================================
    echo  DEPLOY FAILED - some required files missing
    echo ============================================================
    exit /b 1
)

echo ============================================================
echo  BUILD ^& DEPLOY COMPLETE
echo ============================================================
echo  Version     : !VERSION!
echo  Deployed to : %DEPLOY_DIR%
echo  Launcher    : %DEPLOY_DIR%\chrome.exe
echo.
echo  Dashboard path (Profile detail):
echo      %DEPLOY_DIR%\chrome.exe
echo ============================================================
echo.

endlocal
exit /b 0
