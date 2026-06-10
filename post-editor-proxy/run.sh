#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f .env ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    if [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < <(grep -v '^[[:space:]]*#' .env | grep -v '^[[:space:]]*$')
fi

VENV_DIR="${VENV_DIR:-.venv-post-editor}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8010}"
POST_EDITOR_UPSTREAM_URL="${POST_EDITOR_UPSTREAM_URL:-http://127.0.0.1:8001/transcribe}"
POST_EDITOR_OLLAMA_URL="${POST_EDITOR_OLLAMA_URL:-http://127.0.0.1:11434}"
POST_EDITOR_MODEL="${POST_EDITOR_MODEL:-qwen3:1.7b}"
POST_EDITOR_PROMPT_FILE="${POST_EDITOR_PROMPT_FILE:-post_editor_prompt.md}"
POST_EDITOR_THINK="${POST_EDITOR_THINK:-false}"
POST_EDITOR_MIN_EDIT_CHARS="${POST_EDITOR_MIN_EDIT_CHARS:-0}"
POST_EDITOR_LOG_TEXT="${POST_EDITOR_LOG_TEXT:-true}"

export HOST PORT POST_EDITOR_UPSTREAM_URL POST_EDITOR_OLLAMA_URL POST_EDITOR_MODEL POST_EDITOR_PROMPT_FILE POST_EDITOR_THINK POST_EDITOR_MIN_EDIT_CHARS POST_EDITOR_LOG_TEXT

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "[run-post-editor] missing $VENV_DIR. Run ./install.sh first." >&2
  exit 1
fi

echo "[run-post-editor] starting on http://${HOST}:${PORT}"
echo "[run-post-editor] upstream=${POST_EDITOR_UPSTREAM_URL}"
echo "[run-post-editor] model=${POST_EDITOR_MODEL} ollama=${POST_EDITOR_OLLAMA_URL}"
echo "[run-post-editor] think=${POST_EDITOR_THINK}"
echo "[run-post-editor] min_edit_chars=${POST_EDITOR_MIN_EDIT_CHARS}"
echo "[run-post-editor] prompt_file=${POST_EDITOR_PROMPT_FILE}"
echo "[run-post-editor] log_text=${POST_EDITOR_LOG_TEXT}"
exec "$VENV_DIR/bin/python" post_editor_proxy.py --host "$HOST" --port "$PORT"
