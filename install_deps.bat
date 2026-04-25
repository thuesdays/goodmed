@echo off
REM ============================================================
REM  Ghost Shell — dependency installer for Windows.
REM
REM  Installs cryptography (required by the Accounts & Vault
REM  page) plus refreshes every other package from requirements.txt
REM  into the venv at .\.venv.
REM
REM  Double-click to run, or launch from PowerShell / cmd.
REM ============================================================

setlocal

REM Work from the script's own directory regardless of CWD
cd /d "%~dp0"

set VENV_PY=.venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [x] No venv found at %VENV_PY%
    echo     Create one first:  python -m venv .venv
    pause
    exit /b 1
)

echo [*] Using venv: %VENV_PY%
echo [*] Upgrading pip...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :err

echo.
echo [*] Installing requirements...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :err

echo.
echo [*] Verifying cryptography is importable...
"%VENV_PY%" -c "from cryptography.fernet import Fernet; print('cryptography OK — Fernet ready')"
if errorlevel 1 goto :err

REM Clean up the stray file left by the previous broken shell quoting
REM (pip install 'cryptography>=42' in cmd redirects > into a file `42'`)
if exist ".venv\Scripts\42'" (
    del ".venv\Scripts\42'" >nul 2>&1
    echo [*] Removed stray file .venv\Scripts\42'
)

echo.
echo === All dependencies installed ===
echo Next:  python -m ghost_shell dashboard
pause
exit /b 0

:err
echo.
echo !!! Something failed. Check the output above.
pause
exit /b 1
