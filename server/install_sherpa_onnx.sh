#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
VENV_DIR="${VENV_DIR:-.venv-sherpa}"
SHERPA_MODE="${SHERPA_MODE:-cpu}"
SHERPA_VERSION="${SHERPA_VERSION:-1.13.2}"
MODEL_URL="${MODEL_URL:-https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2}"
MODEL_ROOT="${MODEL_ROOT:-models}"
MODEL_DIR="${MODEL_DIR:-${MODEL_ROOT}/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8}"

if ! command -v uv >/dev/null 2>&1; then
  echo "[install-sherpa] uv not found. Install uv first: https://github.com/astral-sh/uv" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[install-sherpa] $PYTHON_BIN not found. Set PYTHON_BIN=/path/to/python if needed." >&2
  exit 1
fi

echo "[install-sherpa] creating $VENV_DIR with $PYTHON_BIN"
uv venv --python "$PYTHON_BIN" "$VENV_DIR"

echo "[install-sherpa] installing server dependencies"
uv pip install --python "$VENV_DIR/bin/python" fastapi "uvicorn[standard]" python-multipart soundfile numpy scipy

if [ "$SHERPA_MODE" = "cuda11" ]; then
  echo "[install-sherpa] installing sherpa-onnx ${SHERPA_VERSION}+cuda"
  uv pip install --python "$VENV_DIR/bin/python" "sherpa-onnx==${SHERPA_VERSION}+cuda" --no-index -f https://k2-fsa.github.io/sherpa/onnx/cuda.html
elif [ "$SHERPA_MODE" = "cuda12" ]; then
  echo "[install-sherpa] installing sherpa-onnx ${SHERPA_VERSION}+cuda12.cudnn9"
  uv pip install --python "$VENV_DIR/bin/python" "sherpa-onnx==${SHERPA_VERSION}+cuda12.cudnn9" -f https://k2-fsa.github.io/sherpa/onnx/cuda.html
elif [ "$SHERPA_MODE" = "cpu" ]; then
  echo "[install-sherpa] installing CPU sherpa-onnx"
  uv pip install --python "$VENV_DIR/bin/python" sherpa-onnx sherpa-onnx-bin
else
  echo "[install-sherpa] invalid SHERPA_MODE=$SHERPA_MODE; use cpu, cuda11, or cuda12" >&2
  exit 1
fi

mkdir -p "$MODEL_ROOT"
if [ ! -f "${MODEL_DIR}/encoder.int8.onnx" ]; then
  archive="${MODEL_ROOT}/$(basename "$MODEL_URL")"
  echo "[install-sherpa] downloading model: $MODEL_URL"
  if command -v wget >/dev/null 2>&1; then
    wget -O "$archive" "$MODEL_URL"
  else
    curl -L -o "$archive" "$MODEL_URL"
  fi
  echo "[install-sherpa] extracting model"
  tar -xjf "$archive" -C "$MODEL_ROOT"
  rm -f "$archive"
else
  echo "[install-sherpa] model already present at $MODEL_DIR"
fi

echo "[install-sherpa] verifying install"
"$VENV_DIR/bin/python" - <<'PY'
import sherpa_onnx
print("sherpa_onnx", getattr(sherpa_onnx, "__version__", "unknown"), sherpa_onnx.__file__)
PY

echo "[install-sherpa] complete"
echo "[install-sherpa] run with: ./run_sherpa_onnx.sh"
