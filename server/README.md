# Transcription Servers

This directory contains local HTTP transcription servers for the client.
All variants expose the same contract:

```text
POST /transcribe
multipart/form-data:
  file      audio upload
  language  optional BCP-47 language code
```

Minimum successful response:

```json
{ "text": "hello world" }
```

## Options

| Server | Runtime | Model | Recommended use |
|---|---|---|---|
| `sherpa_onnx_server.py` | Sherpa-ONNX | `sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8` | Default Linux path, especially on constrained machines |
| `parakeet_server.py` | NVIDIA NeMo + PyTorch | `nvidia/parakeet-tdt-0.6b-v3` | Original Parakeet path for CUDA machines with a known-good Torch stack |

The Sherpa-ONNX model is still Parakeet TDT 0.6B v3, but converted and int8
quantized for ONNX/Sherpa instead of loaded through NeMo.

## Sherpa-ONNX Quick Start

```bash
./install_sherpa_onnx.sh
./run_sherpa_onnx.sh
```

Defaults:

- venv: `.venv-sherpa`
- provider: `cpu`
- host: `0.0.0.0`
- port: `8001`
- model dir: `models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`

Try CUDA explicitly:

```bash
VENV_DIR=.venv-sherpa-cuda12 SHERPA_MODE=cuda12 ./install_sherpa_onnx.sh
VENV_DIR=.venv-sherpa-cuda12 PROVIDER=cuda ./run_sherpa_onnx.sh
```

Use CUDA 12 on systems with a matching CUDA 12 / cuDNN 9 runtime. On a GTX 1060
Max-Q CUDA-12 host this path loaded successfully, used about 270 MiB GPU memory
after warmup, and decoded the bundled short English/German test WAVs faster than
real time. CUDA 11 wheels require CUDA 11 runtime libraries such as
`libcublasLt.so.11`.

## NeMo/PyTorch Quick Start

Windows:

```bat
install.bat
run.bat
```

Linux:

```bash
./install.sh
./run.sh
```

The Linux installer pins Torch/Torchaudio by default for older Pascal GPUs, but
NeMo can still be fragile because its dependency resolver may try to replace the
Torch stack. Prefer Sherpa-ONNX when you want a small, predictable local server.

## Health Check

```bash
curl http://127.0.0.1:8001/health
```

Expected shape:

```json
{ "status": "ok", "model_loaded": true }
```

## Test Request

```bash
curl -s -X POST http://127.0.0.1:8001/transcribe \
  -F file=@models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/test_wavs/en.wav \
  -F language=en
```
