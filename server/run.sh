#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8001}"
DEVICE="${DEVICE:-cuda}"

if [ ! -x ".venv/bin/python" ]; then
  echo "[run] missing .venv. Run ./install.sh first." >&2
  exit 1
fi

echo "[run] starting Parakeet server on http://${HOST}:${PORT} using device=${DEVICE}"
exec .venv/bin/python parakeet_server.py --host "$HOST" --port "$PORT" --device "$DEVICE"
