@echo off
setlocal
cd /d "%~dp0"

echo === OverMultiASRSuite client setup ===
echo.

echo [1/3] Checking uv...
uv --version >nul 2>nul
if errorlevel 1 (
    echo uv not found. Install uv first, then run install.bat again.
    echo Download: https://github.com/astral-sh/uv
    pause
    exit /b 1
)

echo.
echo [2/3] Creating/updating .venv...
if not exist ".venv\Scripts\python.exe" (
    uv venv .venv
    if errorlevel 1 (echo venv creation failed. & pause & exit /b 1)
) else (
    echo Reusing existing .venv
)

echo.
echo [3/3] Installing runtime and build dependencies into .venv...
uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt pyinstaller
if errorlevel 1 (echo Dependency install failed. & pause & exit /b 1)

echo.
echo Done! Run with:
echo   run.bat
echo.
echo Other options:
echo   .venv\Scripts\python overmultiasrsuite.py --list-mics    List microphone indices
echo   .venv\Scripts\python overmultiasrsuite.py --settings     Open settings dialog
echo.
echo Build with:
echo   build.bat
pause
