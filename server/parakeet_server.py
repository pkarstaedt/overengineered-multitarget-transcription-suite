#!/usr/bin/env python3
"""
Local Parakeet TDT 0.6B transcription server.
Implements the same /transcribe API as faster-whisper-server so the client
can be pointed at either with no changes.

Parakeet TDT 0.6B v3 is a smaller multilingual model.
Model: nvidia/parakeet-tdt-0.6b-v3

Usage:
    python parakeet_server.py [--host 0.0.0.0] [--port 8001] [--device cuda]
"""

import argparse
import io
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--host",   default="0.0.0.0")
parser.add_argument("--port",   default=8001, type=int)
parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
args, _ = parser.parse_known_args()

# ── App & model ───────────────────────────────────────────────────────────────

app   = FastAPI(title="Parakeet TDT Server")
model = None   # loaded at startup
MODEL_NAME = "nvidia/parakeet-tdt-0.6b-v3"


@app.on_event("startup")
async def load_model():
    global model
    print(f"[STARTUP] Loading {MODEL_NAME} on {args.device}…", flush=True)
    t0 = time.time()

    import nemo.collections.asr as nemo_asr
    model = nemo_asr.models.ASRModel.from_pretrained(MODEL_NAME)

    if args.device == "cuda":
        import torch
        if not torch.cuda.is_available():
            print("[STARTUP] CUDA not available — falling back to CPU.", flush=True)
        else:
            model = model.cuda()

    model.eval()
    print(f"[STARTUP] Model ready in {time.time() - t0:.1f}s", flush=True)


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _load_audio_16k_mono(data: bytes) -> tuple[np.ndarray, float]:
    """
    Read audio from raw bytes (any soundfile-supported format),
    resample to 16 kHz mono float32.
    Returns (audio_array, duration_seconds).
    """
    buf = io.BytesIO(data)

    try:
        audio, sr = sf.read(buf, dtype="float32", always_2d=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode audio: {exc}")

    # Mix down to mono
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    # Resample to 16 kHz if needed
    if sr != 16000:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(sr, 16000)
        audio = resample_poly(audio, 16000 // g, sr // g).astype(np.float32)

    duration = len(audio) / 16000
    return audio, duration


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/transcribe")
async def transcribe(
    file:     UploadFile = File(...),
    language: str        = Form(None),
):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet.")

    raw   = await file.read()
    audio, duration = _load_audio_16k_mono(raw)

    print(f"[REQ] {duration:.2f}s audio received", flush=True)
    t0 = time.time()

    # NeMo needs a file path — write to a temp WAV
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        sf.write(str(tmp_path), audio, 16000, subtype="PCM_16")

        output = model.transcribe([str(tmp_path)])
    finally:
        tmp_path.unlink(missing_ok=True)

    # NeMo may return strings or Hypothesis objects depending on version
    raw_out = output[0]
    if isinstance(raw_out, str):
        text = raw_out
    else:
        # Hypothesis object
        text = getattr(raw_out, "text", str(raw_out))

    elapsed = time.time() - t0
    text = text.strip()
    print(f"[REQ] Transcribed in {elapsed:.2f}s: {text!r}", flush=True)

    return JSONResponse({
        "text":                 text,
        "language":             language or "auto",
        "language_probability": 1.0,
        "duration":             round(duration, 3),
        "segments":             [],      # TDT doesn't expose segments via this API
    })


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": model is not None}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
