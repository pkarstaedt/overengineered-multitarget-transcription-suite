#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-.venv-sherpa}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8001}"
PROVIDER="${PROVIDER:-cpu}"
MODEL_DIR="${MODEL_DIR:-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8}"
NUM_THREADS="${NUM_THREADS:-2}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "[run-sherpa] missing $VENV_DIR. Run ./install_sherpa_onnx.sh first." >&2
  exit 1
fi

echo "[run-sherpa] starting on http://${HOST}:${PORT} provider=${PROVIDER} model=${MODEL_DIR}"
exec "$VENV_DIR/bin/python" sherpa_onnx_server.py \
  --host "$HOST" \
  --port "$PORT" \
  --provider "$PROVIDER" \
  --model-dir "$MODEL_DIR" \
  --num-threads "$NUM_THREADS"
