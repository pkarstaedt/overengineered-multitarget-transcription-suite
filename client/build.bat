@echo off
setlocal

echo === OverMultiASRSuite build ===
echo.

:: Install / upgrade PyInstaller into the venv
echo [1/4] Installing PyInstaller...
uv pip install pyinstaller
if errorlevel 1 (echo PyInstaller install failed. & pause & exit /b 1)

echo.
echo [2/4] Publishing native hotkey helper...
dotnet publish native_hotkey_helper\HotkeyHelper.csproj -c Release -r win-x64 --self-contained false /p:PublishSingleFile=true
if errorlevel 1 (echo Hotkey helper publish failed. & pause & exit /b 1)

:: Run PyInstaller
echo.
echo [3/4] Building exe...
.venv\Scripts\pyinstaller overmultiasrsuite.spec --noconfirm
if errorlevel 1 (echo Build failed. & pause & exit /b 1)

:: Copy config and helper next to the exe so first-run picks it up
echo.
echo [4/4] Copying runtime files...
if not exist "dist\config.json" (
    copy config.json "dist\config.json" >nul
    echo Copied config.json to dist\
) else (
    echo config.json already exists in dist\ - not overwriting.
)
copy "native_hotkey_helper\bin\Release\net8.0-windows\win-x64\publish\HotkeyHelper.exe" "dist\HotkeyHelper.exe" >nul
if errorlevel 1 (echo Copying HotkeyHelper.exe failed. & pause & exit /b 1)
echo Copied HotkeyHelper.exe to dist\

echo.
echo Build complete!
echo Distribute these two files:
echo   dist\OverMultiASRSuite.exe
echo   dist\HotkeyHelper.exe
echo   dist\config.json
echo.
echo NOTE: The exe requests UAC elevation on launch (needed for global hotkeys).
echo       history.json and overmultiasrsuite.log are created next to the exe on first run.
pause
