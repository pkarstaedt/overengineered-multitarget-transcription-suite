#!/usr/bin/env python3
"""
Small Ollama chat client for the post-editor model.

This is intentionally separate from post_editor_proxy.py. The proxy can keep
thinking disabled for latency while this chat client enables thinking for
interactive use.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _post_stream(url: str, body: dict[str, Any]):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=float(os.getenv("OLLAMA_CHAT_TIMEOUT_SECONDS", "300")))


def chat(args: argparse.Namespace) -> int:
    messages: list[dict[str, str]] = []
    endpoint = f"{args.ollama_url.rstrip('/')}/api/chat"

    print(f"[ollama-chat] model={args.model} think={str(args.think).lower()} url={args.ollama_url}")
    print("[ollama-chat] commands: /exit, /clear")

    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/clear":
            messages.clear()
            print("[ollama-chat] conversation cleared")
            continue

        messages.append({"role": "user", "content": user_input})
        body = {
            "model": args.model,
            "stream": True,
            "think": args.think,
            "keep_alive": args.keep_alive,
            "options": {
                "temperature": args.temperature,
                "num_ctx": args.num_ctx,
            },
            "messages": messages,
        }

        assistant_text: list[str] = []
        saw_thinking = False
        saw_answer = False

        try:
            with _post_stream(endpoint, body) as response:
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    chunk = json.loads(raw_line)
                    message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}

                    thinking = message.get("thinking")
                    if thinking:
                        if not saw_thinking:
                            print("\nthinking>")
                            saw_thinking = True
                        print(thinking, end="", flush=True)

                    content = message.get("content")
                    if content:
                        if not saw_answer:
                            if saw_thinking:
                                print("\n\nassistant>")
                            else:
                                print("assistant>")
                            saw_answer = True
                        print(content, end="", flush=True)
                        assistant_text.append(content)

                    if chunk.get("done"):
                        break
        except urllib.error.URLError as exc:
            messages.pop()
            print(f"[ollama-chat] request failed: {exc}", file=sys.stderr)
            continue
        except json.JSONDecodeError as exc:
            messages.pop()
            print(f"[ollama-chat] invalid Ollama stream JSON: {exc}", file=sys.stderr)
            continue

        print()
        if assistant_text:
            messages.append({"role": "assistant", "content": "".join(assistant_text)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ollama-url", default=os.getenv("POST_EDITOR_OLLAMA_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--model", default=os.getenv("POST_EDITOR_MODEL", "qwen3:1.7b"))
    parser.add_argument("--think", action="store_true", default=_env_bool("OLLAMA_CHAT_THINK", True))
    parser.add_argument("--no-think", action="store_false", dest="think")
    parser.add_argument("--temperature", default=float(os.getenv("OLLAMA_CHAT_TEMPERATURE", "0.6")), type=float)
    parser.add_argument("--num-ctx", default=int(os.getenv("OLLAMA_CHAT_NUM_CTX", "4096")), type=int)
    parser.add_argument("--keep-alive", default=os.getenv("OLLAMA_CHAT_KEEP_ALIVE", "30m"))
    return chat(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
