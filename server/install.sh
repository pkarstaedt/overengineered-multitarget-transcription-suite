#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu121}"
TORCH_VERSION="${TORCH_VERSION:-2.4.1}"
TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION:-2.4.1}"

if ! command -v uv >/dev/null 2>&1; then
  echo "[install] uv not found. Install uv first: https://github.com/astral-sh/uv" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[install] $PYTHON_BIN not found. Set PYTHON_BIN=/path/to/python if needed." >&2
  exit 1
fi

echo "[install] creating server venv with $PYTHON_BIN"
uv venv --python "$PYTHON_BIN" .venv

echo "[install] installing PyTorch/Torchaudio from $TORCH_INDEX_URL"
echo "[install] torch==$TORCH_VERSION torchaudio==$TORCHAUDIO_VERSION"
uv pip install --python .venv/bin/python "torch==$TORCH_VERSION" "torchaudio==$TORCHAUDIO_VERSION" --index-url "$TORCH_INDEX_URL"

echo "[install] installing Parakeet server dependencies"
uv pip install --python .venv/bin/python -r requirements.txt

echo "[install] restoring Pascal-compatible PyTorch/Torchaudio pin after NeMo dependency resolution"
uv pip install --python .venv/bin/python "torch==$TORCH_VERSION" "torchaudio==$TORCHAUDIO_VERSION" --index-url "$TORCH_INDEX_URL" --force-reinstall

echo "[install] verifying imports"
.venv/bin/python - <<'PY'
import torch
import fastapi
import uvicorn
import soundfile
import nemo.collections.asr as nemo_asr

print("torch", torch.__version__, "cuda_available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("cuda_device", torch.cuda.get_device_name(0))
    arch_list = torch.cuda.get_arch_list()
    print("cuda_arch_list", arch_list)
    if "sm_61" not in arch_list:
        raise SystemExit("installed torch wheel does not support this Pascal GPU (required arch sm_61)")
print("nemo_asr", nemo_asr.__name__)
PY

echo "[install] complete"
echo "[install] run with: ./run.sh"
