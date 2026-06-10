#!/usr/bin/env python3
"""
Local Sherpa-ONNX Parakeet transcription server.

Implements the same /transcribe API as the NeMo and faster-whisper servers so
clients can switch ASR engines without changing their HTTP integration.
"""

import argparse
import io
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Optional

import numpy as np
import sherpa_onnx
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", default=8001, type=int)
parser.add_argument("--provider", default="cpu", choices=["cpu", "cuda"])
parser.add_argument("--model-dir", default="models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")
parser.add_argument("--num-threads", default=2, type=int)
args, _ = parser.parse_known_args()


app = FastAPI(title="Sherpa-ONNX Parakeet TDT Server")
recognizer: Optional[sherpa_onnx.OfflineRecognizer] = None


def _model_file(name: str) -> str:
    path = Path(args.model_dir) / name
    if not path.exists():
        raise FileNotFoundError(f"missing model file: {path}")
    return str(path)


@app.on_event("startup")
async def load_model():
    global recognizer
    print(
        f"[STARTUP] Loading Sherpa-ONNX Parakeet model from {args.model_dir} "
        f"provider={args.provider} threads={args.num_threads}",
        flush=True,
    )
    t0 = time.time()
    try:
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=_model_file("encoder.int8.onnx"),
            decoder=_model_file("decoder.int8.onnx"),
            joiner=_model_file("joiner.int8.onnx"),
            tokens=_model_file("tokens.txt"),
            num_threads=args.num_threads,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            max_active_paths=4,
            provider=args.provider,
            model_type="nemo_transducer",
        )
    except Exception as exc:
        print(f"[STARTUP] Failed to load model: {exc}", flush=True)
        raise
    print(f"[STARTUP] Model ready in {time.time() - t0:.1f}s", flush=True)


def _load_audio_16k_mono(data: bytes) -> tuple[np.ndarray, float]:
    buf = io.BytesIO(data)
    try:
        audio, sr = sf.read(buf, dtype="float32", always_2d=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode audio: {exc}")

    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    if sr != 16000:
        from math import gcd
        from scipy.signal import resample_poly

        g = gcd(sr, 16000)
        audio = resample_poly(audio, 16000 // g, sr // g).astype(np.float32)

    audio = np.ascontiguousarray(audio, dtype=np.float32)
    duration = len(audio) / 16000
    return audio, duration


def _contains_non_latin_letters(text: str) -> bool:
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            if "LATIN" not in unicodedata.name(ch):
                return True
        except ValueError:
            return True
    return False


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(None),
):
    if recognizer is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet.")

    raw = await file.read()
    audio, duration = _load_audio_16k_mono(raw)

    print(f"[REQ] {duration:.2f}s audio received", flush=True)
    t0 = time.time()

    stream = recognizer.create_stream()
    stream.accept_waveform(16000, audio)
    recognizer.decode_stream(stream)
    result = stream.result
    text = getattr(result, "text", str(result)).strip()

    elapsed = time.time() - t0
    if _contains_non_latin_letters(text):
        print(f"[REQ] Non-Latin transcript suppressed: {text!r}", flush=True)
        text = ""
    print(f"[REQ] Transcribed in {elapsed:.2f}s: {text!r}", flush=True)

    return JSONResponse(
        {
            "text": text,
            "language": language or "auto",
            "language_probability": 1.0,
            "duration": round(duration, 3),
            "segments": [],
        }
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": recognizer is not None,
        "backend": "sherpa-onnx",
        "provider": args.provider,
        "model_dir": args.model_dir,
    }


if __name__ == "__main__":
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
