@echo off
echo Installing OverMultiASRSuite dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Done! Run with:
echo   python overmultiasrsuite.py
echo.
echo Other options:
echo   python overmultiasrsuite.py --list-mics    List microphone indices
echo   python overmultiasrsuite.py --settings     Open settings dialog
pause
