#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
VENV_DIR="${VENV_DIR:-.venv-post-editor}"

if ! command -v uv >/dev/null 2>&1; then
  echo "[install-post-editor] uv not found. Install uv first: https://github.com/astral-sh/uv" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[install-post-editor] $PYTHON_BIN not found. Set PYTHON_BIN=/path/to/python if needed." >&2
  exit 1
fi

echo "[install-post-editor] creating $VENV_DIR with $PYTHON_BIN"
uv venv --python "$PYTHON_BIN" "$VENV_DIR"

echo "[install-post-editor] installing dependencies"
uv pip install --python "$VENV_DIR/bin/python" fastapi "uvicorn[standard]" python-multipart httpx

if [ ! -f post_editor_prompt.md ]; then
  cp post_editor_prompt.md.example post_editor_prompt.md
  echo "[install-post-editor] created editable prompt file: post_editor_prompt.md"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[install-post-editor] created local env file: .env"
fi

echo "[install-post-editor] complete"
echo "[install-post-editor] run with: ./run.sh"
