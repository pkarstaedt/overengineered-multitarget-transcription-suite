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

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
POST_EDITOR_MODEL="${POST_EDITOR_MODEL:-qwen3:1.7b}"
POST_EDITOR_OLLAMA_URL="${POST_EDITOR_OLLAMA_URL:-http://127.0.0.1:11434}"
OLLAMA_CHAT_THINK="${OLLAMA_CHAT_THINK:-true}"

export POST_EDITOR_MODEL POST_EDITOR_OLLAMA_URL OLLAMA_CHAT_THINK

exec "$PYTHON_BIN" ollama_chat.py "$@"
