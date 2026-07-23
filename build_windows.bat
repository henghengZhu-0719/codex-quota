@echo off
setlocal

cd /d "%~dp0"
set "VENV_DIR=%CD%\.build-venv-windows"

where py >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Error: Python 3 was not found. Install Python 3.9 or newer first.
        exit /b 1
    )
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% -c "import sys; raise SystemExit(sys.version_info < (3, 9))"
if errorlevel 1 (
    echo Error: Python 3.9 or newer is required.
    exit /b 1
)

echo [1/4] Creating build environment...
%PYTHON_CMD% -m venv "%VENV_DIR%"
if errorlevel 1 goto :error

echo [2/4] Installing build dependencies...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
"%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [3/4] Building codex-quota.exe...
"%VENV_DIR%\Scripts\python.exe" -m PyInstaller --noconfirm --clean CodexQuotaWindows.spec
if errorlevel 1 goto :error

echo [4/4] Build complete.
echo Output: %CD%\dist\codex-quota.exe
exit /b 0

:error
echo.
echo Build failed. See the error output above.
exit /b 1
