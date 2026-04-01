@echo off
setlocal
echo === Parakeet Server — first-time setup ===
echo.

echo [1/3] Creating venv...
uv venv
if errorlevel 1 (echo venv creation failed. & pause & exit /b 1)

echo.
echo [2/3] Installing PyTorch with CUDA 12.1...
echo (If you have a different CUDA version, edit this line.)
uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
if errorlevel 1 (echo PyTorch install failed. & pause & exit /b 1)

echo.
echo [3/3] Installing server dependencies...
uv pip install fastapi "uvicorn[standard]" python-multipart soundfile numpy scipy "nemo_toolkit[asr]"
if errorlevel 1 (echo Dependency install failed. & pause & exit /b 1)

echo.
echo Setup complete. Run the server with:
echo   run.bat
echo.
echo The Parakeet model (~4.4 GB) will be downloaded on first start.
pause
