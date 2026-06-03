@echo off
setlocal
cd /d "%~dp0"

echo === OverMultiASRSuite build ===
echo.

echo [1/6] Checking uv...
uv --version >nul 2>nul
if errorlevel 1 (
    echo uv not found. Install uv first, then run build.bat again.
    echo Download: https://github.com/astral-sh/uv
    pause
    exit /b 1
)

echo.
echo [2/6] Checking .NET SDK...
dotnet --list-sdks | findstr /r "^8\." >nul
if errorlevel 1 (
    echo .NET 8 SDK not found. Install the .NET 8 SDK, then run build.bat again.
    echo Download: https://dotnet.microsoft.com/download/dotnet/8.0
    pause
    exit /b 1
)

echo.
echo [3/6] Preparing .venv...
if not exist ".venv\Scripts\python.exe" (
    uv venv .venv
    if errorlevel 1 (echo venv creation failed. & pause & exit /b 1)
) else (
    echo Reusing existing .venv
)
uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt pyinstaller
if errorlevel 1 (echo Dependency install failed. & pause & exit /b 1)

echo.
echo [4/6] Publishing native hotkey helper...
if exist "native_hotkey_helper\publish" rmdir /s /q "native_hotkey_helper\publish"
dotnet publish native_hotkey_helper\HotkeyHelper.csproj -c Release -r win-x64 --self-contained true /p:PublishSingleFile=true -o "native_hotkey_helper\publish"
if errorlevel 1 (echo Hotkey helper publish failed. & pause & exit /b 1)

:: Run PyInstaller
echo.
echo [5/6] Building exe...
".venv\Scripts\python.exe" -m PyInstaller overmultiasrsuite.spec --noconfirm
if errorlevel 1 (echo Build failed. & pause & exit /b 1)

:: Copy runtime files into dist and the client root.
:: The root copies are the normal local launch target; they share config and
:: prompt Markdown files with source runs. dist remains disposable staging.
echo.
echo [6/6] Copying runtime files...
if not exist "dist\config.json" (
    if exist "config.json" (
        copy config.json "dist\config.json" >nul
        echo Copied local config.json to dist\
    ) else if exist "config.json.example" (
        copy config.json.example "dist\config.json" >nul
        echo Copied config.json.example to dist\config.json
    ) else (
        echo No config template found; the app will create config.json on first run.
    )
) else (
    echo config.json already exists in dist\ - not overwriting.
)
copy "native_hotkey_helper\publish\HotkeyHelper.exe" "dist\HotkeyHelper.exe" >nul
if errorlevel 1 (echo Copying HotkeyHelper.exe failed. & pause & exit /b 1)
echo Copied HotkeyHelper.exe to dist\

copy "dist\OverMultiASRSuite.exe" "OverMultiASRSuite.exe" >nul
if errorlevel 1 (echo Copying OverMultiASRSuite.exe to client root failed. & pause & exit /b 1)
copy "native_hotkey_helper\publish\HotkeyHelper.exe" "HotkeyHelper.exe" >nul
if errorlevel 1 (echo Copying HotkeyHelper.exe to client root failed. & pause & exit /b 1)
echo Copied OverMultiASRSuite.exe and HotkeyHelper.exe to client root.

echo.
echo Build complete!
echo Local launch target:
echo   OverMultiASRSuite.exe
echo.
echo Distribute these files together:
echo   OverMultiASRSuite.exe
echo   HotkeyHelper.exe
echo   config.json ^(optional starter/local settings^)
echo   *_post_edit_prompt.md / transcription_prompt.md ^(optional local prompt files^)
echo.
echo NOTE: The exe requests UAC elevation on launch (needed for global hotkeys).
echo       history.json and overmultiasrsuite.log are created next to the exe on first run.
pause
