@echo off
:: Run OverMultiASRSuite in background (no console window after startup)
:: For development/debugging, run overmultiasrsuite.py directly instead.
cd /d "%~dp0"
if exist "OverMultiASRSuite.exe" (
    start "" "%~dp0OverMultiASRSuite.exe"
) else if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0overmultiasrsuite.py"
) else (
    start "" pythonw "%~dp0overmultiasrsuite.py"
)
