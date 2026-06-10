# Post-Editor Proxy

The post-editor proxy is a separate middleware component:

```text
client -> post-editor-proxy:/transcribe -> upstream ASR:/transcribe -> Ollama cleanup -> client
```

It accepts the same multipart `POST /transcribe` request as the ASR servers and
returns the same primary `text` field. The proxy replaces `text` with the
cleaned transcript and adds `raw_text`, `editor_notes`, and `post_editor`
diagnostics for inspection.

## Install

```bash
cd post-editor-proxy
./install.sh
```

The installer creates `.venv-post-editor/` and creates the local editable prompt
file if it does not already exist. It also creates `.env` from `.env.example`
for local runtime tuning.

## Run

Run an ASR backend first, for example from the `server/` component:

```bash
cd ../server
VENV_DIR=.venv-sherpa-cuda12 PROVIDER=cuda ./run_sherpa_onnx.sh
```

Then run this proxy on `:8010`:

```bash
cd ../post-editor-proxy
POST_EDITOR_MODEL=qwen3:1.7b \
POST_EDITOR_UPSTREAM_URL=http://127.0.0.1:8001/transcribe \
./run.sh
```

Point the client at:

```json
{ "server_url": "http://127.0.0.1:8010/transcribe" }
```

## Prompt

Editable local prompt:

```text
post-editor-proxy/post_editor_prompt.md
```

Committed template:

```text
post-editor-proxy/post_editor_prompt.md.example
```

The local prompt file is ignored by git so each machine can tune it
independently.

## Configuration

The proxy and chat launcher automatically load:

```text
post-editor-proxy/.env
```

Committed template:

```text
post-editor-proxy/.env.example
```

Common local settings:

```dotenv
POST_EDITOR_PROMPT_FILE=post_editor_prompt.md
POST_EDITOR_THINK=false
POST_EDITOR_MIN_EDIT_CHARS=120
POST_EDITOR_LOG_TEXT=true
OLLAMA_CHAT_THINK=true
```

| Variable | Default | Purpose |
|---|---|---|
| `POST_EDITOR_UPSTREAM_URL` | `http://127.0.0.1:8001/transcribe` | Actual ASR backend |
| `POST_EDITOR_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama base URL |
| `POST_EDITOR_MODEL` | `qwen3:1.7b` | Cleanup model |
| `POST_EDITOR_PROMPT_FILE` | `post_editor_prompt.md` | Editable prompt file |
| `POST_EDITOR_THINK` | `false` | Enable Qwen3 thinking mode; keep `false` for low-latency cleanup |
| `POST_EDITOR_MIN_EDIT_CHARS` | `0` | Skip Ollama cleanup for transcripts shorter than this many characters |
| `POST_EDITOR_LOG_TEXT` | `true` | Print raw and edited transcripts to stdout for debugging |
| `POST_EDITOR_DISABLE_EDIT` | unset | Set to `true` to forward raw ASR text without cleanup |
| `POST_EDITOR_NUM_CTX` | `4096` | Ollama context size |
| `POST_EDITOR_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded |

For Qwen3 models, `POST_EDITOR_THINK=false` keeps transcript cleanup closer to a
fast editing pass instead of a slow reasoning pass.

To bypass cleanup for short utterances:

```bash
POST_EDITOR_MIN_EDIT_CHARS=120 ./run.sh
```

When bypassed, the proxy returns the raw ASR text unchanged and sets
`post_editor.status` to `skipped_below_min_chars`.

By default, each request prints timing plus the raw and edited transcript to
stdout. Set `POST_EDITOR_LOG_TEXT=false` to keep only the compact timing line.

## Interactive Chat

Launch a terminal chat against the same local Ollama model:

```bash
cd post-editor-proxy
./chat.sh
```

The chat interface enables reasoning by default with `OLLAMA_CHAT_THINK=true`.
That setting is separate from the proxy's `POST_EDITOR_THINK=false`, so chat can
reason while transcript cleanup stays fast.

Useful chat variables:

| Variable | Default | Purpose |
|---|---|---|
| `POST_EDITOR_MODEL` | `qwen3:1.7b` | Ollama model to chat with |
| `POST_EDITOR_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama base URL |
| `OLLAMA_CHAT_THINK` | `true` | Enable Qwen3 thinking mode for chat |
| `OLLAMA_CHAT_NUM_CTX` | `4096` | Chat context size |

Disable chat reasoning for one session:

```bash
OLLAMA_CHAT_THINK=false ./chat.sh
```

## Debug

```bash
curl http://127.0.0.1:8010/health
curl http://127.0.0.1:8010/debug/last
```

Each response includes timing diagnostics:

| Field | Meaning |
|---|---|
| `upstream_ms` | Upstream ASR request time |
| `elapsed_ms` | Ollama cleanup request time |
| `post_edit_total_ms` | Proxy cleanup wrapper time |
| `total_ms` | Full proxy request time |
