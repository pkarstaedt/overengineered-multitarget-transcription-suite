# overengineered-multitarget-transcription-suite

A Windows push-to-talk voice transcription client. Hold an overengineered array of possible hotkeys to record, release to transcribe via a simple HTTP transcription endpoint, and the recognised text is inserted into the currently focused application using the safest output path for that target.

The project is intentionally asymmetric:

- The transcription boundary is kept simple. The client sends audio to a small `/transcribe` HTTP API and expects a small JSON response back. This makes it easy to swap in different backends such as faster-whisper, a local Parakeet server, or a future in-process ONNX backend.
- The output side is intentionally more complex. Different Windows targets accept text in very different ways: some need slow typed input, some are happy with `Ctrl+V`, some console-like targets prefer right-click paste, and some remote-agent / coding-console workflows break unless the app is very conservative.

That complexity is deliberate. One of the main goals of this client is to work not only in normal local GUI applications, but also in terminals, console-based tools, remote coding agents, SSH/RDP sessions, and other places where many commercial dictation products still fail. The result is a richer insertion-mode matrix on the client side, paired with a very small and replaceable transcription-server contract.

There are also intentionally two hotkey families:

- The main hotkey is the more configurable path. It inspects the focused target and chooses the insertion strategy that best matches that class of application: slow typed input, `Ctrl+V` paste, or right-click paste.
- The fast hotkey is the "simple and predictable" path. It is for the common case where you already know you want a quick dump into a friendly text field, without relying on the richer target-routing logic of the main hotkey.

This split exists because there is no single Windows text-insertion method that works well everywhere. The main hotkey optimizes for compatibility; the fast hotkey optimizes for speed.

---

## How it works

Two transcription modes are available; both use the same VAD pipeline underneath.

### Classic mode (default)

```
Hold hotkey → ®  (typed into the focused field as a listening indicator)
  [silence detected by VAD] → utterance sent to server in background
Release     → ¿  (typed while waiting for remaining chunks)
Result      → transcribed text  (indicator erased, full text typed in place)
```

All chunks are collected and the complete transcription is typed at once when everything is ready. The status indicator is typed directly into the focused field and erased with a single backspace before the result appears. This is safe over SSH and RDP connections where rapid keystrokes can be dropped.

Optionally enable **fancy animations** in Settings: the `®` indicator becomes a block-shade pulse (`░▒▓▒░`) that fills in while recording, and `¿` becomes a box-corner spinner (`┐┘└┌`) during transcription.

### Live mode

```
Hold hotkey → red dot appears next to the cursor
  [silence detected by VAD] → utterance sent to server; text typed immediately when ready
  [more speech …]           → each sentence appended as it arrives
Release     → amber dot while the final chunk transcribes
Result      → dot disappears; all text already in the field
```

Completed background chunks are typed into the field **as they arrive** — you see text appearing while you continue speaking. Only the final audio chunk (after you release the hotkey) needs the amber waiting phase. Status is shown via a small always-on-top dot near the cursor rather than characters in the field.

### Classic mode vs. live mode for paste-style targets

For paste-oriented workflows it helps to think of the modes this way:

- **Classic mode** records the whole dictation, waits for the full result, and then inserts one final block of text. This is usually the safer choice for consoles, remote agents, and any target where partial insertion would be distracting or risky.
- **Live mode** continuously inserts completed chunks while you are still speaking. That feels faster and more conversational in normal editors, but it is also more intrusive because text starts appearing before you are done.

So even when the actual insertion method is paste-based rather than typed character-by-character, the "classic vs. live" decision still matters:

- classic = one final paste/dump after release
- live = continuous incremental insertion while dictating

---

## VAD pipeline (common to both modes)

Audio is captured from the microphone while the hotkey is held. A hysteresis-based VAD (voice activity detector) watches each audio block in real time:

- A block is considered **speech** when its RMS energy exceeds `vad_silence_rms`.
- Once speech is detected the recorder stays in *speech* state for `vad_hangover_s` seconds after the last loud block, bridging natural inter-word gaps.
- After the hangover expires, a silence counter starts. When `vad_silence_secs` of continuous silence accumulates **and** at least `vad_min_speech_s` of audio has been recorded, the current buffer is submitted to the server in a **background thread** and recording continues.
- This means longer dictations are partially transcribed before you release the button, keeping the final wait short.

When the hotkey is released the remaining (final) audio chunk is sent and all partial results are assembled in order. If multiple chunks returned text, adjacent chunks are joined intelligently — a trailing period is stripped from a chunk when the next chunk starts with a lowercase letter, avoiding spurious mid-sentence full stops added by the model.

Audio is sent directly from memory as WAV bytes — no temporary files are written to disk for normal transcriptions. The response text is injected using Win32 `SendInput` with Unicode key events — this works in applications where clipboard paste fails or is not supported.

If the transcription server is unreachable the error reason is typed into the field for 5 seconds, then automatically erased.

If focus moves away from a text input (e.g. a dialog pops up) while recording, the result is saved to history but not typed, preventing accidental keystrokes in the wrong place.

---

## Requirements

- Windows 10 or later
- A running transcription HTTP server with the expected `/transcribe` API (see [Transcription server](#transcription-server))
- Administrator privileges (required for global keyboard hooks)

---

## Transcription server

The client posts audio to any HTTP endpoint that accepts `multipart/form-data`:

```
POST /transcribe
Content-Type: multipart/form-data

file      binary   WAV audio (16 kHz mono, 16-bit PCM)
language  string   BCP-47 code, e.g. "en" (optional, omit for auto-detect)
```

Expected JSON response:

```json
{
  "text": "The recognised transcript.",
  "language": "en",
  "language_probability": 0.9987,
  "duration": 3.2,
  "segments": []
}
```

### Response contract

The client currently relies on these fields:

- `text` — required string; the transcript to insert
- `language` — optional string; informational only
- `language_probability` — optional number; informational only
- `duration` — optional number; informational only
- `segments` — optional array; currently accepted and passed through for compatibility, but not required by the client logic

In practice, the minimum viable successful response is:

```json
{ "text": "hello world" }
```

If the server returns a non-2xx status, invalid JSON, or a payload without usable text, the client treats the request as failed and shows/logs an error instead of inserting text.

The client is tested against [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) and the local [Parakeet server](#local-parakeet-server) included in this repo. Any server with the same API shape works.

---

## Installation

### Run from source

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bat
cd client
uv venv
uv pip install -r requirements.txt
```

Run (must be Administrator):

```bat
.venv\Scripts\python overmultiasrsuite.py
```

### Distribute as a standalone exe

```bat
cd client
build.bat
```

Produces `client/dist/OverMultiASRSuite.exe` plus a native hotkey sidecar. Distribute alongside `config.json`:

```
OverMultiASRSuite.exe
HotkeyHelper.exe
config.json
```

`history.json` and `overmultiasrsuite.log` are created automatically next to the exe on first run.

The exe embeds a UAC manifest so Windows will prompt for elevation on launch — this is required for global hotkey interception.

---

## Configuration

Edit `config.json` next to the exe, or use the Settings dialog (right-click tray icon → **Settings…**).

### Core settings

| Key | Default | Description |
|---|---|---|
| `server_url` | `http://…/transcribe` | Transcription server endpoint |
| `hotkey` | `ctrl+shift+space` | Main push-to-talk hotkey. Hold to record, release to transcribe. Insertion is chosen automatically: console-like fields use typed input, normal editors use fast paste. |
| `fast_hotkey` | `""` | Optional push-to-talk override that always uses fast paste / dump. Empty = disabled. |
| `undo_hotkey` | `""` | Optional hotkey to re-insert the last successful transcription into the current target using the current insertion logic. Empty = disabled. |
| `microphone_index` | `null` | PortAudio device index. `null` = system default. Use `--list-mics` to find the right index. |
| `language` | `null` | BCP-47 language code (e.g. `"en"`, `"de"`). `null` = auto-detect. |
| `sample_rate` | `16000` | Recording sample rate in Hz. The device's native rate is used automatically if the device does not support this value; audio is resampled before sending. |
| `pre_type_delay` | `0.05` | Seconds to wait before typing after the hotkey fires. Lets focus settle. |
| `char_delay` | `0.0` | Extra delay between typed characters. `0` = as fast as possible. |
| `erase_delay` | `0.08` | Pause after erasing the status indicator before typing the result. Increase (e.g. `0.15`) if the indicator character is not fully deleted before the transcription appears — common over SSH or RDP. Only applies in classic mode. |
| `input_classes` | *(built-in list)* | Win32 class names treated as text inputs. Editable in the **Input Classes** tab of Settings. |
| `type_input_classes` | console defaults | Subset of input classes that should always use slow typed insertion instead of paste. Editable in the **Input Classes** tab of Settings. |
| `right_click_paste_input_classes` | `[]` | Subset of input classes that should paste via right-click instead of `Ctrl+V` when using the main hotkey. Editable in the **Input Classes** tab of Settings. |
| `debug` | `false` | Enable verbose per-block VAD logging to stdout / log file. |

### Transcription mode settings

These settings exist because "where should text go?" and "how should text get there?" are different problems. The app may need to type slowly into a terminal, paste quickly into a code editor, or use right-click paste for console-style windows, all while still letting you choose between classic and live transcription behavior.

| Key | Default | Description |
|---|---|---|
| `live_mode` | `false` | `true` = live mode: each sentence is typed as it arrives; status shown via cursor overlay dot. `false` = classic mode: collect all chunks, type everything at once with in-field indicators. |
| `simple_mode` | `true` | Classic mode only. `true` = use plain `®` / `¿` single-character status indicators (reliable over SSH and RDP). `false` = use animated block-shade (`░▒▓▒`) / corner-spin (`┐┘└┌`) indicators. Ignored when `live_mode` is `true`. |

### VAD (Voice Activity Detection) settings

These control when background chunk sends fire. All are editable live in **Settings → Settings tab → Voice Activity Detection**.

| Key | Default | Description |
|---|---|---|
| `vad_silence_rms` | `400` | RMS energy below this value counts as silence. Your noise floor is typically 10–50; raise this if VAD never fires, lower it if soft speech is missed. |
| `vad_silence_secs` | `1.5` | Seconds of continuous silence required to send a background chunk. Increase if VAD cuts mid-sentence; decrease for faster background sends. |
| `vad_min_speech_s` | `0.5` | Minimum seconds of audio that must be recorded before a background send is allowed. Prevents tiny accidental clips from being sent. |
| `vad_hangover_s` | `0.3` | Seconds to stay in "speech" state after the last loud block. Bridges natural inter-word gaps so brief pauses don't split words. |
| `vad_max_chunk_s` | `30.0` | Force-send the current chunk after this many seconds even if silence is never detected, as a safety valve for very long continuous speech. |

### Hotkey format

Key names are locale-aware and accept the same style already used in the app config, including German modifier names such as `umschalt` and `strg`. Use the **Capture** button in Settings to record the exact combination rather than typing it manually.

Hotkeys must include at least one non-modifier key. Modifier-only combinations (e.g. `ctrl+shift+alt`) will not fire.

---

## Tray icon

The system tray icon changes colour to indicate the current state:

| Colour | State |
|---|---|
| Green | Idle, ready |
| Red | Recording |
| Yellow/amber | Transcribing |

Right-click the tray icon for the context menu:

- **Copy last result** — copy the most recent transcription to the clipboard (greyed out if nothing has been transcribed yet)
- **Settings…** — open the settings dialog
- **Quit** — stop the app

---

## Settings dialog

Right-click the tray icon and choose **Settings…**. The window has three tabs at the top and a persistent history panel below them. Click **Save** to write all changes to `config.json`; closing with × discards unsaved changes.

### Settings tab

- **Server URL** — transcription endpoint
- **Hotkey** — main push-to-talk combination, with live validation and a **Capture** button
- **Fast hotkey** — optional push-to-talk override that always uses clipboard paste / fast dump
- **Last result hotkey** — re-insert the last successful transcription using the currently selected insertion logic for the focused target; leave blank to disable
- **Microphone** — dropdown of all input devices labelled with their audio API. Devices marked `(!)` use Windows WDM-KS which can fail on some hardware; prefer MME, DirectSound, or WASAPI entries.
- **Language** — BCP-47 code (e.g. `en`, `de`) or blank for auto-detect
- **Voice Activity Detection** — inline group showing all five VAD parameters with descriptions; changes take effect on the next recording after saving.
- **Live transcription mode** — when checked, each sentence is streamed to the field as it arrives and status is shown via a cursor-side overlay dot instead of characters in the field. When unchecked (default), all chunks are collected and typed at once.
- **Simple mode** — classic mode only; greyed out when live mode is on. When checked (default), the status indicators are plain `®` (listening) and `¿` (transcribing) — a single character each, reliable over SSH and RDP. When unchecked, animated block-shade and corner-spin characters are used instead.

### Microphone Test tab

- **Monitor** — starts a live level meter showing input amplitude in dB; click **Stop** to end. Use this to discover your noise floor and set `vad_silence_rms` appropriately.
- **Record 3s** — records a three-second test clip with a live countdown and level meter
- **Play back** — plays the recorded clip through your speakers

### Input Classes tab

Lists the Win32 class names of controls where typing is allowed. Add or remove entries here to control which windows receive typed output — no restart required.

The tab now maintains two categories:

- **Allowed input classes** — windows where insertion is permitted at all
- **Type-only classes** — windows that should force slow typed insertion instead of paste when using the main hotkey
- **Right-click paste classes** — windows that should paste via right-click instead of `Ctrl+V` when using the main hotkey

- Type a class name in the entry field and press **Add** (or Enter)
- Select an entry and click **Remove Selected** to delete it

To find the class name of an unfamiliar window, focus it and run:

```bat
.venv\Scripts\python overmultiasrsuite.py --focus
```

### Recent Transcriptions panel

Always visible below the tabs. Shows the last 10 entries (successful and failed), newest first.

- **Successful** entries show the transcribed text, a timestamp, and a **Copy** button.
- **Failed** entries show the error reason in red (e.g. `Server error (HTTP 500)`), the filename of the saved WAV, and a **Retry** button. Clicking Retry re-sends that audio to the server; on success the entry is updated with the transcribed text — the result is not typed anywhere but can be copied from history.

When a transcription fails the raw audio is saved to `failed_audio/` next to the exe. These files are deleted automatically when a retry succeeds.

History is persisted to `history.json` and survives restarts.

---

## Status indicators

### Classic mode

Status is typed directly into the focused field and erased automatically — no popups or overlays. Indicators are always a **single character** (or a short sequence that grows to at most four characters with fancy animations), so cleanup is reliable even over SSH or RDP where rapid keystrokes can be dropped.

**Simple mode** (default, `simple_mode: true`):

| Character | Meaning |
|---|---|
| `®` | Hotkey held, microphone recording |
| `¿` | Transcription in progress, waiting for server |

**Fancy animations** (`simple_mode: false`):

| Indicator | Meaning |
|---|---|
| `░` → `░▒` → `░▒▓` → `░▒▓▒` filling in | Hotkey held, microphone recording |
| `┐` `┘` `└` `┌` spinning in place | Transcription in progress |

The `/` character is intentionally not used as a status indicator — it opens command palettes in many apps.

**Error messages** (both modes):

| Message | Meaning |
|---|---|
| `Service unavailable` | Server not reachable — auto-erased after 5 s |
| `Request timed out` | Server took longer than 60 s — auto-erased after 5 s |
| `Server error (HTTP 5xx)` | Server returned an error response — auto-erased after 5 s |

### Live mode

No characters are typed into the field as status. Instead, a small always-on-top dot appears next to the mouse cursor:

| Dot colour | Meaning |
|---|---|
| 🔴 Red | Hotkey held, microphone recording; sentences typed as they arrive |
| 🟡 Amber | Final chunk still transcribing after hotkey release |
| *(hidden)* | Idle |

The dot is 14 × 14 px, semi-transparent, and positioned 18 px below and to the right of the cursor tip. It is click-through — mouse events pass straight through to whatever is underneath.

---

## Re-insert last transcription

If a last-result hotkey is configured (stored in config as `undo_hotkey` for backward compatibility), pressing it inserts the most recent successful transcription again into the currently focused target.

The app uses the same insertion routing as the normal hotkey:

- typed input for type-only targets
- `Ctrl+V` paste for normal fast-paste targets
- right-click paste for configured console-style targets

This is useful when you want to place the same transcription into a second field, recover from a bad target choice, or quickly resend text into a remote console without dictating it again.

---

## Focus detection

Before typing, the client checks the Win32 class of the currently focused control. Typing is only performed if the class appears in the configured input class list. The defaults cover the most common apps:

| Class | Application |
|---|---|
| `Edit` | Standard Windows text fields |
| `RichEdit`, `RichEdit20A/W`, `RichEdit50W`, `RICHEDIT60W` | Rich text editors (Word, Outlook, etc.) |
| `Scintilla` | Code editors (Notepad++, etc.) |
| `Chrome_RenderWidgetHostHWND` | Chrome, Edge, Electron-based apps (VS Code, Discord, etc.) |
| `MozillaWindowClass`, `MozillaContentWindowClass` | Firefox |
| `ConsoleWindowClass` | Windows terminal |
| `WebViewWnd` | WebView2-based apps |

If focus has moved to a non-text element (dialog, button, list box) when recording ends, the transcription is silently saved to history without typing. The log will show: `Skipped — focused element is not a text input (class: 'X')`.

To add a missing class, focus the target window, run `--focus` to discover its name, then add it in **Settings → Input Classes tab**.

---

## CLI flags

| Flag | Description |
|---|---|
| `--list-mics` | Print all input devices with their index and audio API, then exit |
| `--settings` | Open the settings dialog without starting the tray app |
| `--focus` | Print the focused control's Win32 class name and whether typing would proceed |
| `--debug-keys` | Log raw key events from the native hotkey helper — useful to confirm the hook is working |

---

## Logging

When running as a compiled exe, all output is written to `overmultiasrsuite.log` next to the exe (the console window is suppressed). Check this file if something appears broken.

When running from source, output goes to the terminal. Enable `"debug": true` in `config.json` for verbose per-block VAD output.

---

## Local Parakeet server

`server/` contains an alternative transcription server using NVIDIA's [Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) model. It exposes the exact same `/transcribe` API so the client can be pointed at it with a one-line config change.

**Parakeet TDT 0.6B v3 vs. Whisper large-v3**

| | Parakeet TDT 0.6B v3 | Whisper large-v3 |
|---|---|---|
| Languages | Multilingual | 100+ |
| VRAM | Lower than 1.1B; hardware-dependent | ~3 GB (float16) |
| Speed | Very fast | Fast |
| Accuracy | Strong low-latency general ASR | Excellent |
| Segments / timestamps | No | Yes |

Worth trying if you want a smaller local model with low latency while keeping the same simple HTTP contract.

### VRAM note

On this project, the practical GPU memory picture has been:

- cold start / first real inference: roughly **6 GB VRAM**
- warm steady-state operation: roughly **3 GB VRAM**

That distinction matters. A machine may have enough VRAM to run Parakeet comfortably once it is warm, but still fail during startup or the first inference if the initial allocation peak does not fit.

Treat these numbers as approximate, hardware- and driver-dependent observations rather than strict guarantees, but they are a useful rule of thumb when deciding whether Parakeet is a good fit for a given GPU.

### Setup

Requires Python 3.11+, [uv](https://github.com/astral-sh/uv), and a CUDA-capable GPU.

```bat
cd server
install.bat
```

This installs PyTorch and NeMo. The Parakeet model is downloaded from HuggingFace on first start and cached in `~/.cache/huggingface`.

> **Windows note:** NeMo is officially supported on Linux. It generally works on Windows but installation can be fragile. If you hit issues, running the server in WSL2 is the most reliable option.

### Run

```bat
cd server
run.bat
```

Server starts on `http://localhost:8001`. Once you see `Model ready`, point the client at it:

```json
{ "server_url": "http://localhost:8001/transcribe" }
```

### Options

```
python parakeet_server.py --host 0.0.0.0 --port 8001 --device cuda
python parakeet_server.py --device cpu   # slower, no GPU required
```

`GET /health` returns `{"status": "ok", "model_loaded": true}` once the model is ready.

---

## Tuning guide

### VAD is cutting mid-sentence

Increase `vad_silence_secs` (try `2.0`) and/or `vad_hangover_s` (try `0.5`). This requires a longer silence before a background send fires, giving you more time between clauses.

### VAD never fires / no background chunks

Your mic noise floor may be above `vad_silence_rms`. Open **Settings → Microphone Test → Monitor** and watch the dB meter while silent — note the typical floor value. Set `vad_silence_rms` to something between the floor and your soft-speech level.

### Soft trailing words are missed

Lower `vad_silence_rms`. If your voice trails off to 500 RMS at the end of sentences but the threshold is 800, those blocks are classified as silence and the VAD may fire before you finish. The noise floor on most WASAPI microphones is below 100 RMS so values as low as 200–400 are safe.

### Mid-sentence periods appear in the output

This happens when a background chunk fires mid-sentence and the model adds terminal punctuation. The client already strips a trailing period from an intermediate chunk when the next chunk starts with a lowercase letter. If it still occurs, increase `vad_silence_secs` to keep more of the sentence in one chunk.

### Status indicator not fully erased before result appears (SSH / RDP)

Increase `erase_delay` (try `0.15` or `0.25`). This adds a short pause between the backspace that removes the indicator and the first character of the transcription. Only relevant in classic mode. Switching to live mode avoids this entirely since no characters are typed into the field as status.

### WASAPI device fails with "Invalid sample rate"

The device's native sample rate (often 44100 or 48000 Hz) differs from `sample_rate` in config. The client detects this automatically and records at the native rate, resampling to 16000 Hz before sending. If the error persists, try a different device entry in the microphone dropdown.

---

## Project structure

```
client/
  overmultiasrsuite.py     Main application
  overmultiasrsuite.spec   PyInstaller build spec
  build.bat             One-click build script
  install.bat           First-time venv setup
  requirements.txt      Python dependencies
  config.json           Runtime configuration (edit directly or via Settings dialog)
  history.json          Transcription history (auto-created)
  overmultiasrsuite.log    Log file when running as exe (auto-created)
  failed_audio/         WAV recordings of failed transcriptions, for retry (auto-created)
  .venv/                Python virtual environment
  dist/                 PyInstaller build output

server/
  parakeet_server.py    Local Parakeet TDT 0.6B v3 transcription server
  requirements.txt      Server dependencies
  install.bat           First-time setup
  run.bat               Start the server
```
