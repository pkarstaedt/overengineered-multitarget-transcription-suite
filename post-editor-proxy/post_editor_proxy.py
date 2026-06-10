#!/usr/bin/env python3
"""
ASR post-editor proxy.

Accepts the same /transcribe multipart API as the ASR servers, forwards audio
to an upstream ASR backend, and optionally cleans the returned transcript through
an Ollama-hosted local LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse


DEFAULT_PROMPT = """You are a careful transcript post-editor for spoken instructions that are often meant for coding agents.

Return only valid JSON matching the requested schema.

Output fields:
- `edited_text`: the cleaned transcript to send onward.
- `editor_notes`: empty string unless there are useful extra terms, constraints, or ambiguity notes for the downstream coding agent.

Preserve:
- user intent
- technical details
- real constraints
- requested tradeoffs

Improve:
- clarity
- precision
- wording
- structure
- actionability

Do not invent facts, requirements, files, APIs, or architecture that were not at least strongly implied.

If the transcript is clearly about implementation, debugging, investigation, review, design, or product behavior, preserve that mode instead of forcing it into some other kind of request.

If editor notes are useful, place them in the `editor_notes` field. Do not use `editor_notes` to explain what edits you made. Use them only for extra terminology, constraints, or prompt-improving additions that would genuinely help the downstream coding agent.
"""

EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edited_text", "editor_notes"],
    "properties": {
        "edited_text": {
            "type": "string",
            "description": "The cleaned transcript text to send to the downstream target.",
        },
        "editor_notes": {
            "type": "string",
            "description": "Empty unless there are extra terminology, constraints, ambiguities, or prompt-improving additions for the downstream coding agent.",
        },
    },
}


parser = argparse.ArgumentParser()
parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
parser.add_argument("--port", default=int(os.getenv("PORT", "8010")), type=int)
parser.add_argument(
    "--upstream-url",
    default=os.getenv("POST_EDITOR_UPSTREAM_URL", "http://127.0.0.1:8001/transcribe"),
)
parser.add_argument("--ollama-url", default=os.getenv("POST_EDITOR_OLLAMA_URL", "http://127.0.0.1:11434"))
parser.add_argument("--model", default=os.getenv("POST_EDITOR_MODEL", "qwen3:1.7b"))
parser.add_argument("--prompt-file", default=os.getenv("POST_EDITOR_PROMPT_FILE", "post_editor_prompt.md"))
parser.add_argument("--timeout", default=float(os.getenv("POST_EDITOR_TIMEOUT_SECONDS", "120")), type=float)
parser.add_argument("--ollama-timeout", default=float(os.getenv("POST_EDITOR_OLLAMA_TIMEOUT_SECONDS", "45")), type=float)
parser.add_argument("--temperature", default=float(os.getenv("POST_EDITOR_TEMPERATURE", "0")), type=float)
parser.add_argument("--num-ctx", default=int(os.getenv("POST_EDITOR_NUM_CTX", "4096")), type=int)
parser.add_argument("--keep-alive", default=os.getenv("POST_EDITOR_KEEP_ALIVE", "30m"))
parser.add_argument("--think", action="store_true", default=os.getenv("POST_EDITOR_THINK", "").lower() in {"1", "true", "yes"})
parser.add_argument("--min-edit-chars", default=int(os.getenv("POST_EDITOR_MIN_EDIT_CHARS", "0")), type=int)
parser.add_argument("--disable-edit", action="store_true", default=os.getenv("POST_EDITOR_DISABLE_EDIT", "").lower() in {"1", "true", "yes"})
args, _ = parser.parse_known_args()


app = FastAPI(title="ASR Post-Editor Proxy")
last_result: dict[str, Any] | None = None


def _read_prompt() -> str:
    path = Path(args.prompt_file)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return DEFAULT_PROMPT


async def _call_upstream(raw: bytes, filename: str, content_type: str, language: str | None) -> dict[str, Any]:
    files = {"file": (filename, raw, content_type or "application/octet-stream")}
    data: dict[str, str] = {}
    if language:
        data["language"] = language
    try:
        async with httpx.AsyncClient(timeout=args.timeout) as client:
            response = await client.post(args.upstream_url, files=files, data=data)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream ASR unavailable: {exc}") from exc

    try:
        payload = response.json() if response.content else {}
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="upstream ASR returned non-JSON response") from exc

    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else response.text
        raise HTTPException(status_code=502, detail=f"upstream ASR failed ({response.status_code}): {detail or 'unknown error'}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="upstream ASR returned non-object JSON")
    return payload


def _json_from_ollama_response(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    content = message.get("content") or payload.get("response") or ""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("Ollama response did not contain string content")
    return json.loads(content)


async def _post_edit(raw_text: str, language: str | None) -> tuple[str, str, dict[str, Any]]:
    if args.disable_edit:
        return raw_text, "", {"status": "disabled"}

    transcript = raw_text.strip()
    if not transcript:
        return raw_text, "", {"status": "skipped_empty_transcript"}
    if args.min_edit_chars > 0 and len(transcript) < args.min_edit_chars:
        return raw_text, "", {
            "status": "skipped_below_min_chars",
            "transcript_chars": len(transcript),
            "min_edit_chars": args.min_edit_chars,
        }

    prompt = _read_prompt()
    user_message = (
        "Clean up this ASR transcript for downstream insertion.\n\n"
        f"Language hint: {language or 'auto'}\n\n"
        "Set `editor_notes` to an empty string unless the transcript contains extra context "
        "that would genuinely help a downstream coding agent.\n\n"
        "Transcript:\n"
        f"{transcript}"
    )
    body = {
        "model": args.model,
        "stream": False,
        "think": args.think,
        "format": EDIT_SCHEMA,
        "keep_alive": args.keep_alive,
        "options": {
            "temperature": args.temperature,
            "num_ctx": args.num_ctx,
        },
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_message},
        ],
    }

    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=args.ollama_timeout) as client:
            response = await client.post(f"{args.ollama_url.rstrip('/')}/api/chat", json=body)
    except httpx.HTTPError as exc:
        return raw_text, "", {"status": "failed", "error": f"ollama_unavailable: {exc}"}

    elapsed_ms = round((time.time() - started) * 1000, 3)
    if response.status_code >= 400:
        return raw_text, "", {
            "status": "failed",
            "error": f"ollama_failed_http_{response.status_code}",
            "detail": response.text[:1000],
            "elapsed_ms": elapsed_ms,
        }

    try:
        payload = response.json()
        parsed = _json_from_ollama_response(payload)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return raw_text, "", {
            "status": "failed",
            "error": f"invalid_ollama_json: {exc}",
            "elapsed_ms": elapsed_ms,
        }

    edited_text = str(parsed.get("edited_text") or "").strip()
    editor_notes = str(parsed.get("editor_notes") or "").strip()
    if not edited_text:
        return raw_text, editor_notes, {
            "status": "failed",
            "error": "empty_edited_text",
            "elapsed_ms": elapsed_ms,
        }

    return edited_text, editor_notes, {
        "status": "completed",
        "model": args.model,
        "elapsed_ms": elapsed_ms,
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(None),
):
    global last_result

    total_started = time.time()
    raw = await file.read()
    upstream_started = time.time()
    upstream = await _call_upstream(
        raw=raw,
        filename=file.filename or "audio.wav",
        content_type=file.content_type or "application/octet-stream",
        language=language,
    )
    upstream_ms = round((time.time() - upstream_started) * 1000, 3)

    raw_text = str(upstream.get("text") or "").strip()
    edit_started = time.time()
    edited_text, editor_notes, edit_meta = await _post_edit(raw_text, language or upstream.get("language"))
    edit_ms = round((time.time() - edit_started) * 1000, 3)

    response = dict(upstream)
    response["text"] = edited_text
    response["raw_text"] = raw_text
    response["editor_notes"] = editor_notes
    response["post_editor"] = {
        **edit_meta,
        "upstream_url": args.upstream_url,
        "upstream_ms": upstream_ms,
        "post_edit_total_ms": edit_ms,
        "total_ms": round((time.time() - total_started) * 1000, 3),
    }

    last_result = {
        "at": time.time(),
        "filename": file.filename,
        "language": language,
        "response": response,
    }
    status = response["post_editor"].get("status")
    print(
        f"[POST-EDITOR] upstream={upstream_ms}ms edit={edit_ms}ms status={status} "
        f"raw_len={len(raw_text)} edited_len={len(edited_text)}",
        flush=True,
    )
    return JSONResponse(response)


@app.get("/health")
async def health():
    upstream_ok = False
    upstream_detail: Any = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(args.upstream_url.rsplit("/", 1)[0] + "/health")
            upstream_ok = response.status_code < 400
            upstream_detail = response.json() if response.content else None
    except Exception as exc:  # noqa: BLE001 - health endpoint reports diagnostics
        upstream_detail = str(exc)

    return {
        "status": "ok",
        "component": "post-editor-proxy",
        "upstream_url": args.upstream_url,
        "upstream_health_ok": upstream_ok,
        "upstream_health": upstream_detail,
        "editing_enabled": not args.disable_edit,
        "ollama_url": args.ollama_url,
        "model": args.model,
        "thinking_enabled": args.think,
        "min_edit_chars": args.min_edit_chars,
        "prompt_file": args.prompt_file,
    }


@app.get("/debug/last")
async def debug_last():
    return last_result or {}


if __name__ == "__main__":
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
