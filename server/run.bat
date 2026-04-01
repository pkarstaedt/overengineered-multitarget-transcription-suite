@echo off
echo Starting Parakeet TDT server on http://localhost:8001
echo Model loads on first start (smaller 0.6B model download if not cached).
echo Press Ctrl+C to stop.
echo.
.venv\Scripts\python parakeet_server.py --host 0.0.0.0 --port 8001 --device cuda
pause
