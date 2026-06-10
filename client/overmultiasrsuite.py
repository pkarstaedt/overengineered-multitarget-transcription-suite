#!/usr/bin/env python3
"""
OverMultiASRSuite for Windows
Hold the hotkey -> records audio -> sends to transcription server -> inserts result.
"""

import collections
import ctypes
import ctypes.wintypes
import datetime
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import audioop
from pathlib import Path

# ── Paths (frozen-exe aware) ──────────────────────────────────────────────────

def _app_dir() -> Path:
    """Directory that contains the exe (frozen) or this script (development)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _stdio_is_usable(stream) -> bool:
    if stream is None:
        return False
    try:
        stream.write("")
        stream.flush()
        return True
    except Exception:
        return False


# When running as a frozen exe or via pythonw there is no usable console, so
# redirect prints to a rolling log file next to the app instead.
if getattr(sys, "frozen", False) or not (_stdio_is_usable(sys.stdout) and _stdio_is_usable(sys.stderr)):
    _log = open(_app_dir() / "overmultiasrsuite.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log
else:
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(errors="backslashreplace")
            except Exception:
                pass


def _safe_console_text(value) -> str:
    """Return a console-safe representation even on narrow Windows code pages."""
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding, errors="strict")
        return text
    except Exception:
        return text.encode(encoding, errors="backslashreplace").decode(encoding, errors="ignore")

# ── Config ───────────────────────────────────────────────────────────────────

import keyboard
import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

CONFIG_FILE = _app_dir() / "config.json"
DEFAULT_PROMPT_FILE = "post_edit_prompt.md"
DEFAULT_TRANSCRIPTION_PROMPT_FILE = "transcription_prompt.md"
POST_EDIT_PROFILES = ("dev", "pro", "personal")

DEFAULT_CONFIG = {
    "transcription_backend": "http",
    "server_url": "http://127.0.0.1:8001/transcribe",
    "openai_transcription_model": "gpt-4o-mini-transcribe",
    "openai_transcription_prompt_markdown_file": DEFAULT_TRANSCRIPTION_PROMPT_FILE,
    "openai_transcription_prompt": (
        "Transcribe the spoken audio into clean text. Return only transcript text. Do not echo this prompt. "
        "Do not use ellipses as continuation markers and do not end the transcript with `...`. "
        "Remove obvious filler words when doing so does not change meaning. Preserve technical vocabulary, "
        "product names, file names, identifiers, and code-like terms exactly when possible."
    ),
    "hotkey": "ctrl+shift+space",
    "fast_hotkey": "",
    "undo_hotkey": "",          # empty = disabled
    "microphone_index": None,   # None = system default
    "language": None,           # None = auto-detect
    "sample_rate": 16000,
    "pre_type_delay": 0.05,
    "char_delay": 0.0,
    "duck_audio_during_dictation": False,
    "duck_audio_level_percent": 25,
    "erase_delay":  0.08,  # pause after erasing status before typing result (helps SSH/terminals)
    "main_live_mode": False,
    "main_simple_mode": True,
    "main_mode": "preview",
    "main_post_edit_mode": False,
    "fast_live_mode": False,
    "fast_simple_mode": True,
    "fast_mode": "preview",
    "fast_post_edit_mode": False,
    "post_edit_provider": "openai",
    "external_post_edit_url": "http://127.0.0.1:8010/external_postedit",
    "openai_model": "gpt-5-mini",
    "openai_reasoning_effort": "low",
    "openai_prompt_markdown_file": DEFAULT_PROMPT_FILE,
    "editor_notes_overlay_seconds": 4.0,
    "editor_notes_chars_per_extra_second": 45.0,
    "post_edit_toggle_key": "y",
    "main_review_non_post_edit_sessions": False,
    "fast_review_non_post_edit_sessions": False,
    "preview_max_width": 860,
    "preview_max_height": 1000,
    "openai_system_prompt": (
        "You are a careful transcript post-editor. Return only valid JSON matching the requested schema. "
        "Do not invent facts."
    ),
    "openai_developer_prompt": (
        "Clean up the transcript for use as a coding-agent or technical prompt. Remove filler words such as "
        "um, uh, like, and you know when they are not meaningful. Preserve intent, preserve technical details, "
        "fix obvious grammar, and do not add new facts or requirements."
    ),
    "openai_user_prompt_template": "Transcript:\n{transcript}",
    "live_mode":   False,  # True = stream chunks live as they arrive (uses screen overlay); False = classic (collect all, type at end)
    "simple_mode": True,   # True = simple ® / ¿ indicators (SSH/terminal safe); False = fancy block-shade/corner-spin animations  (classic mode only)
    "vad_silence_rms":  400,   # RMS below this = silence. Hangover handles noise, so keep this low.
    "vad_silence_secs": 1.5,   # seconds of silence that triggers a background send
    "vad_min_speech_s": 0.5,   # minimum speech seconds before a cut is allowed
    "vad_hangover_s":   0.3,   # stay in "speech" state this long after last loud block
    "vad_max_chunk_s":  30.0,  # force-send a chunk after this many seconds regardless
    "input_classes": [
        "Edit",
        "RichEdit", "RichEdit20W", "RichEdit20A", "RichEdit50W", "RICHEDIT60W",
        "Scintilla", "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
        "Chrome_RenderWidgetHostHWND", "MozillaWindowClass",
        "MozillaContentWindowClass", "WebViewWnd",
    ],
    "type_input_classes": [
        "ConsoleWindowClass",
        "CASCADIA_HOSTING_WINDOW_CLASS",
    ],
    "right_click_paste_input_classes": [],
    "simple_mode_input_classes": [
        "ConsoleWindowClass",
        "CASCADIA_HOSTING_WINDOW_CLASS",
    ],
    "live_mode_input_classes": [],
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        legacy_prompt = cfg.get("openai_post_edit_prompt")
        if legacy_prompt:
            cfg.setdefault("openai_user_prompt_template", legacy_prompt)
        cfg.pop("openai_api_key", None)
        cfg.pop("openai_context_markdown_file", None)
        cfg.setdefault("main_live_mode", cfg.get("live_mode", DEFAULT_CONFIG["main_live_mode"]))
        cfg.setdefault("main_simple_mode", cfg.get("simple_mode", DEFAULT_CONFIG["main_simple_mode"]))
        cfg.setdefault("fast_live_mode", cfg.get("live_mode", DEFAULT_CONFIG["fast_live_mode"]))
        cfg.setdefault("fast_simple_mode", cfg.get("simple_mode", DEFAULT_CONFIG["fast_simple_mode"]))
        if "main_mode" not in cfg:
            cfg["main_mode"] = "live" if cfg.get("main_live_mode", False) else "classic" if cfg.get("main_simple_mode", True) else "preview"
        if "fast_mode" not in cfg:
            cfg["fast_mode"] = "live" if cfg.get("fast_live_mode", False) else "classic" if cfg.get("fast_simple_mode", True) else "preview"
        legacy_review = bool(cfg.get("review_non_post_edit_sessions", False))
        cfg.setdefault("main_review_non_post_edit_sessions", legacy_review)
        cfg.setdefault("fast_review_non_post_edit_sessions", legacy_review)
        cfg.pop("review_non_post_edit_sessions", None)
        prompt_sections = _load_prompt_markdown(cfg)
        cfg["openai_transcription_prompt"] = _load_transcription_prompt_markdown(cfg)
        cfg["openai_system_prompt"] = prompt_sections["system"]
        cfg["openai_developer_prompt"] = prompt_sections["developer"]
        cfg["openai_user_prompt_template"] = prompt_sections["user"]
        return cfg
    cfg = DEFAULT_CONFIG.copy()
    prompt_sections = _load_prompt_markdown(cfg)
    cfg["openai_transcription_prompt"] = _load_transcription_prompt_markdown(cfg)
    cfg["openai_system_prompt"] = prompt_sections["system"]
    cfg["openai_developer_prompt"] = prompt_sections["developer"]
    cfg["openai_user_prompt_template"] = prompt_sections["user"]
    return cfg


def save_config(cfg: dict):
    cfg = dict(cfg)
    _save_prompt_markdown(cfg)
    _save_transcription_prompt_markdown(cfg)
    cfg.pop("openai_api_key", None)
    cfg.pop("openai_context_markdown_file", None)
    cfg.pop("review_non_post_edit_sessions", None)
    cfg.pop("openai_transcription_prompt", None)
    cfg.pop("openai_system_prompt", None)
    cfg.pop("openai_developer_prompt", None)
    cfg.pop("openai_user_prompt_template", None)
    cfg.pop("openai_post_edit_prompt", None)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ── Transcription history ─────────────────────────────────────────────────────

_HISTORY_FILE = _app_dir() / "history.json"
_history: collections.deque = collections.deque(maxlen=20)
_last_result_text = [""]


def _set_last_result_text(text: str | None):
    _last_result_text[0] = (text or "").strip()


def _latest_history_text() -> str:
    return next(
        ((e.get("text") or "").strip() for e in reversed(list(_history)) if (e.get("text") or "").strip()),
        "",
    )


def _history_text_is_meaningful(text: str | None) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    words = re.findall(r"\b[\w']+\b", stripped, flags=re.UNICODE)
    return len(words) >= 2


def _load_history():
    if _HISTORY_FILE.exists():
        try:
            items = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            _history.extend(items)
            _set_last_result_text(_latest_history_text())
        except Exception:
            pass


def _save_history():
    _HISTORY_FILE.write_text(
        json.dumps(list(_history), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _add_to_history(entry: dict):
    """Append a history entry dict and persist. Callers build the dict."""
    text = entry.get("text")
    if text and not _history_text_is_meaningful(text):
        print(f"[HISTORY] Skipping short transcription: {text!r}", flush=True)
        return
    _history.append(entry)
    _save_history()


def _openai_api_key() -> str:
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def _post_edit_profile_name(value) -> str:
    profile = str(value or "").strip().lower()
    return profile if profile in POST_EDIT_PROFILES else ""


def _next_post_edit_profile(current) -> str:
    profile = _post_edit_profile_name(current)
    if not profile:
        return POST_EDIT_PROFILES[0]
    idx = POST_EDIT_PROFILES.index(profile)
    return POST_EDIT_PROFILES[idx + 1] if idx + 1 < len(POST_EDIT_PROFILES) else ""


def _post_edit_prompt_file(profile: str) -> str:
    profile = _post_edit_profile_name(profile) or POST_EDIT_PROFILES[0]
    return f"{profile}_post_edit_prompt.md"


def _prompt_markdown_path(cfg: dict | None = None, profile: str = "dev") -> Path:
    key = f"openai_{_post_edit_profile_name(profile) or 'dev'}_prompt_markdown_file"
    name = (cfg or {}).get(key) or _post_edit_prompt_file(profile)
    path = Path(name)
    if not path.is_absolute():
        path = _app_dir() / path
    return path


def _transcription_prompt_markdown_path(cfg: dict | None = None) -> Path:
    name = (cfg or {}).get("openai_transcription_prompt_markdown_file", DEFAULT_TRANSCRIPTION_PROMPT_FILE)
    path = Path(name)
    if not path.is_absolute():
        path = _app_dir() / path
    return path


def _prompt_sections_from_config(cfg: dict) -> dict[str, str]:
    return {
        "system": cfg.get("openai_system_prompt", DEFAULT_CONFIG["openai_system_prompt"]),
        "developer": cfg.get("openai_developer_prompt", DEFAULT_CONFIG["openai_developer_prompt"]),
        "user": cfg.get("openai_user_prompt_template", DEFAULT_CONFIG["openai_user_prompt_template"]),
    }


def _render_prompt_markdown(sections: dict[str, str]) -> str:
    ordered = [
        ("System", sections.get("system", "").strip()),
        ("Developer", sections.get("developer", "").strip()),
        ("User", sections.get("user", "").strip()),
    ]
    parts = ["# Post-Edit Prompt"]
    for title, body in ordered:
        parts.append(f"## {title}")
        parts.append(body)
    return "\n\n".join(parts).strip() + "\n"


def _parse_prompt_markdown(text: str) -> dict[str, str]:
    sections = {"system": "", "developer": "", "user": ""}
    current_key = None
    current_lines: list[str] = []

    def _flush():
        nonlocal current_key, current_lines
        if current_key is not None:
            sections[current_key] = "\n".join(current_lines).strip()
        current_lines = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            _flush()
            header = stripped[3:].strip().lower()
            if header in sections:
                current_key = header
            else:
                current_key = None
        elif current_key is not None:
            current_lines.append(line)
    _flush()
    return sections


def _load_prompt_markdown(cfg: dict, profile: str = "dev") -> dict[str, str]:
    path = _prompt_markdown_path(cfg, profile)
    if not path.exists() and _post_edit_profile_name(profile) == "dev":
        legacy_path = _app_dir() / DEFAULT_PROMPT_FILE
        if legacy_path.exists():
            path = legacy_path
    if path.exists():
        try:
            parsed = _parse_prompt_markdown(path.read_text(encoding="utf-8"))
            return {
                "system": parsed.get("system") or cfg.get("openai_system_prompt", DEFAULT_CONFIG["openai_system_prompt"]),
                "developer": parsed.get("developer") or cfg.get("openai_developer_prompt", DEFAULT_CONFIG["openai_developer_prompt"]),
                "user": parsed.get("user") or cfg.get("openai_user_prompt_template", DEFAULT_CONFIG["openai_user_prompt_template"]),
            }
        except Exception:
            pass
    sections = _prompt_sections_from_config(cfg)
    try:
        path.write_text(_render_prompt_markdown(sections), encoding="utf-8")
    except Exception:
        pass
    return sections


def _save_prompt_markdown(cfg: dict, profile: str = "dev", sections: dict[str, str] | None = None):
    path = _prompt_markdown_path(cfg, profile)
    path.write_text(_render_prompt_markdown(sections or _prompt_sections_from_config(cfg)), encoding="utf-8")


def _load_transcription_prompt_markdown(cfg: dict) -> str:
    path = _transcription_prompt_markdown_path(cfg)
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    default_prompt = (cfg.get("openai_transcription_prompt") or DEFAULT_CONFIG["openai_transcription_prompt"]).strip()
    try:
        path.write_text(default_prompt + "\n", encoding="utf-8")
    except Exception:
        pass
    return default_prompt


def _save_transcription_prompt_markdown(cfg: dict):
    path = _transcription_prompt_markdown_path(cfg)
    prompt_text = (cfg.get("openai_transcription_prompt") or DEFAULT_CONFIG["openai_transcription_prompt"]).strip()
    try:
        path.write_text(prompt_text + "\n", encoding="utf-8")
    except Exception:
        pass


def _helper_project_dir() -> Path:
    return _app_dir() / "native_hotkey_helper"


def _helper_exe_path() -> Path | None:
    candidates = [
        _app_dir() / "HotkeyHelper.exe",
        _helper_project_dir() / "publish" / "HotkeyHelper.exe",
        _helper_project_dir() / "bin" / "Release" / "net8.0-windows" / "win-x64" / "publish" / "HotkeyHelper.exe",
        _helper_project_dir() / "bin" / "Release" / "net8.0-windows" / "HotkeyHelper.exe",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _helper_dll_path() -> Path | None:
    candidates = [
        _helper_project_dir() / "bin" / "Release" / "net8.0-windows" / "HotkeyHelper.dll",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _dotnet_sdk_available() -> bool:
    try:
        result = subprocess.run(
            ["dotnet", "--list-sdks"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _helper_command(*args: str) -> list[str]:
    if not getattr(sys, "frozen", False):
        helper_dll = _helper_dll_path()
        if helper_dll is not None:
            return ["dotnet", str(helper_dll), *args]

        project = _helper_project_dir() / "HotkeyHelper.csproj"
        if project.exists() and _dotnet_sdk_available():
            return ["dotnet", "run", "--project", str(project), "--configuration", "Release", "--", *args]

    helper_exe = _helper_exe_path()
    if helper_exe is not None:
        return [str(helper_exe), *args]

    raise FileNotFoundError("Hotkey helper executable not found.")


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class NativeHotkeyBridge:
    """Runs the native helper and exposes hotkey state to Python threads."""

    def __init__(self, hotkey: str, fast_hotkey: str = "", undo_hotkey: str = "", debug_raw: bool = False):
        self.hotkey = hotkey
        self.fast_hotkey = fast_hotkey.strip()
        self.undo_hotkey = undo_hotkey.strip()
        self.debug_raw = debug_raw
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.last_error = ""
        self._pressed = {
            "ptt": threading.Event(),
            "fast_ptt": threading.Event(),
            "undo": threading.Event(),
        }

    def start(self):
        cmd = _helper_command("--ptt", self.hotkey)
        cmd.extend(["--parent-pid", str(os.getpid())])
        if self.fast_hotkey:
            cmd.extend(["--fast-ptt", self.fast_hotkey])
        if self.undo_hotkey:
            cmd.extend(["--undo", self.undo_hotkey])
        if self.debug_raw:
            cmd.append("--debug-raw")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        if not self._ready.wait(timeout=5):
            code = self._proc.poll() if self._proc is not None else None
            detail = self.last_error or (f"exited with code {code}" if code is not None else "no ready signal")
            raise RuntimeError(f"Hotkey helper did not become ready ({detail}).")

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            self._proc = None

    def is_pressed(self, name: str) -> bool:
        ev = self._pressed.get(name)
        return bool(ev and ev.is_set())

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._ready.is_set()

    def _reader_loop(self):
        assert self._proc is not None and self._proc.stdout is not None
        for raw_line in self._proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                print(f"[HOTKEY] {line}", flush=True)
                continue
            self._handle_payload(payload)

        if not self._stop.is_set():
            code = self._proc.poll()
            self.last_error = f"exited unexpectedly with code {code}"
            print(f"[HOTKEY] Helper {self.last_error}.", flush=True)

    def _handle_payload(self, payload: dict):
        kind = payload.get("type")
        if kind == "status" and payload.get("event") == "ready":
            self._ready.set()
            print("[HOTKEY] Native helper ready.", flush=True)
            return

        if kind == "error":
            self.last_error = str(payload.get("message") or "unknown error")
            print(f"[HOTKEY] ERROR: {self.last_error}", flush=True)
            return

        if kind == "raw":
            print(f"  [{payload.get('event', '?'):8}] vk={payload.get('vk', '?')}", flush=True)
            return

        if kind != "hotkey":
            print(f"[HOTKEY] {payload}", flush=True)
            return

        name = payload.get("name")
        event = payload.get("event")
        if name not in self._pressed:
            return

        if event == "down":
            self._pressed[name].set()
            if name == "undo":
                reinsert_last_transcription()
        elif event == "up":
            self._pressed[name].clear()


_native_hotkeys: NativeHotkeyBridge | None = None
_shutdown_requested = threading.Event()


def _work_area_height() -> int | None:
    """Return usable screen height excluding the taskbar when available."""
    try:
        rect = _RECT()
        SPI_GETWORKAREA = 0x0030
        ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if ok:
            return int(rect.bottom - rect.top)
    except Exception:
        pass
    return None


# ── Win32 SendInput typing ────────────────────────────────────────────────────
# Characters are sent as Unicode key events — works in any focused input field
# without touching the clipboard.

KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP   = 0x0002
INPUT_KEYBOARD    = 1

_ptr_size = ctypes.sizeof(ctypes.c_void_p)
_ulong_ptr = ctypes.c_ulonglong if _ptr_size == 8 else ctypes.c_ulong


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", _ulong_ptr),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", _ulong_ptr),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg",    ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type",  ctypes.c_ulong),
        ("_data", _INPUT_UNION),
    ]


_user32 = ctypes.windll.user32
_input_size = ctypes.sizeof(_INPUT)
_user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.SendInput.restype = ctypes.c_uint
_user32.OpenClipboard.argtypes = (ctypes.wintypes.HWND,)
_user32.OpenClipboard.restype = ctypes.c_bool
_user32.CloseClipboard.argtypes = ()
_user32.CloseClipboard.restype = ctypes.c_bool
_user32.EmptyClipboard.argtypes = ()
_user32.EmptyClipboard.restype = ctypes.c_bool
_user32.GetClipboardData.argtypes = (ctypes.c_uint,)
_user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
_user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.wintypes.HANDLE)
_user32.SetClipboardData.restype = ctypes.wintypes.HANDLE

_ole32 = ctypes.windll.ole32
_ole32.CoCreateInstance.argtypes = (
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
)
_ole32.CoCreateInstance.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.wintypes.DWORD)
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = ()
_ole32.CoUninitialize.restype = None

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
S_OK = 0
S_FALSE = 1


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, value: str):
        super().__init__()
        import uuid

        u = uuid.UUID(value)
        self.Data1, self.Data2, self.Data3, rest = u.fields[0], u.fields[1], u.fields[2], u.bytes[8:]
        for i, b in enumerate(rest):
            self.Data4[i] = b


class _PROPVARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("llVal", ctypes.c_longlong),
        ("lVal", ctypes.c_long),
        ("ulVal", ctypes.c_ulong),
        ("punkVal", ctypes.c_void_p),
        ("pwszVal", ctypes.c_wchar_p),
    ]


class _PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ubyte),
        ("wReserved2", ctypes.c_ubyte),
        ("wReserved3", ctypes.c_ulong),
        ("u", _PROPVARIANT_UNION),
    ]


class _IUnknownVTable(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.c_void_p),
        ("AddRef", ctypes.c_void_p),
        ("Release", ctypes.c_void_p),
    ]


class _IUnknown(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IUnknownVTable))]


class _IMMDeviceEnumeratorVTable(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.c_void_p),
        ("AddRef", ctypes.c_void_p),
        ("Release", ctypes.c_void_p),
        ("EnumAudioEndpoints", ctypes.c_void_p),
        ("GetDefaultAudioEndpoint", ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))),
    ]


class _IMMDeviceEnumerator(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IMMDeviceEnumeratorVTable))]


class _IMMDeviceVTable(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.c_void_p),
        ("AddRef", ctypes.c_void_p),
        ("Release", ctypes.c_void_p),
        ("Activate", ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.wintypes.DWORD, ctypes.POINTER(_PROPVARIANT), ctypes.POINTER(ctypes.c_void_p))),
    ]


class _IMMDevice(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IMMDeviceVTable))]


class _IAudioEndpointVolumeVTable(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.c_void_p),
        ("AddRef", ctypes.c_void_p),
        ("Release", ctypes.c_void_p),
        ("RegisterControlChangeNotify", ctypes.c_void_p),
        ("UnregisterControlChangeNotify", ctypes.c_void_p),
        ("GetChannelCount", ctypes.c_void_p),
        ("SetMasterVolumeLevel", ctypes.c_void_p),
        ("SetMasterVolumeLevelScalar", ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p)),
        ("GetMasterVolumeLevel", ctypes.c_void_p),
        ("GetMasterVolumeLevelScalar", ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float))),
    ]


class _IAudioEndpointVolume(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IAudioEndpointVolumeVTable))]


_CLSID_MMDeviceEnumerator = _GUID("bcde0395-e52f-467c-8e3d-c4579291692e")
_IID_IMMDeviceEnumerator = _GUID("a95664d2-9614-4f35-a746-de8db63617e6")
_IID_IAudioEndpointVolume = _GUID("5cdf2c82-841e-4546-9722-0cf74078229a")
_EDataFlow_eRender = 0
_ERole_eMultimedia = 1

_duck_audio_lock = threading.Lock()
_duck_audio_state = {
    "active": False,
    "original_scalar": None,
}


def type_text(text: str, char_delay: float = 0.0):
    """Type text into the currently focused window using SendInput (no clipboard)."""
    events: list[_INPUT] = []

    for ch in text:
        if ch == "\n":
            # VK_RETURN (0x0D) for newlines
            for flags in (0, KEYEVENTF_KEYUP):
                ki = _KEYBDINPUT(0x0D, 0, flags, 0, 0)
                events.append(_INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki)))
        else:
            code = ord(ch)
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                ki = _KEYBDINPUT(0, code, flags, 0, 0)
                events.append(_INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki)))

        if char_delay > 0 and events:
            # Send accumulated events then sleep
            n = len(events)
            arr = (_INPUT * n)(*events)
            _user32.SendInput(n, arr, _input_size)
            events.clear()
            time.sleep(char_delay)

    if events:
        n = len(events)
        arr = (_INPUT * n)(*events)
        _user32.SendInput(n, arr, _input_size)


def _release_com(obj_ptr) -> None:
    if obj_ptr:
        try:
            unk = ctypes.cast(obj_ptr, ctypes.POINTER(_IUnknown))
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(unk.contents.lpVtbl.contents.Release)
            release(obj_ptr)
        except Exception:
            pass


def _default_endpoint_volume() -> ctypes.c_void_p | None:
    enum_ptr = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(
        ctypes.byref(_CLSID_MMDeviceEnumerator),
        None,
        CLSCTX_INPROC_SERVER,
        ctypes.byref(_IID_IMMDeviceEnumerator),
        ctypes.byref(enum_ptr),
    )
    if hr not in (S_OK, S_FALSE) or not enum_ptr.value:
        return None
    device_ptr = ctypes.c_void_p()
    endpoint_ptr = ctypes.c_void_p()
    try:
        enum_obj = ctypes.cast(enum_ptr, ctypes.POINTER(_IMMDeviceEnumerator))
        hr = enum_obj.contents.lpVtbl.contents.GetDefaultAudioEndpoint(enum_ptr, _EDataFlow_eRender, _ERole_eMultimedia, ctypes.byref(device_ptr))
        if hr not in (S_OK, S_FALSE) or not device_ptr.value:
            return None
        device_obj = ctypes.cast(device_ptr, ctypes.POINTER(_IMMDevice))
        hr = device_obj.contents.lpVtbl.contents.Activate(
            device_ptr,
            ctypes.byref(_IID_IAudioEndpointVolume),
            CLSCTX_INPROC_SERVER,
            None,
            ctypes.byref(endpoint_ptr),
        )
        if hr not in (S_OK, S_FALSE) or not endpoint_ptr.value:
            return None
        return endpoint_ptr
    finally:
        _release_com(device_ptr)
        _release_com(enum_ptr)


def _get_master_volume_scalar() -> float | None:
    init_hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    endpoint_ptr = None
    try:
        endpoint_ptr = _default_endpoint_volume()
        if not endpoint_ptr:
            return None
        endpoint_obj = ctypes.cast(endpoint_ptr, ctypes.POINTER(_IAudioEndpointVolume))
        value = ctypes.c_float()
        hr = endpoint_obj.contents.lpVtbl.contents.GetMasterVolumeLevelScalar(endpoint_ptr, ctypes.byref(value))
        if hr not in (S_OK, S_FALSE):
            return None
        return float(value.value)
    finally:
        _release_com(endpoint_ptr)
        if init_hr in (S_OK, S_FALSE):
            _ole32.CoUninitialize()


def _set_master_volume_scalar(value: float) -> bool:
    init_hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    endpoint_ptr = None
    try:
        endpoint_ptr = _default_endpoint_volume()
        if not endpoint_ptr:
            return False
        endpoint_obj = ctypes.cast(endpoint_ptr, ctypes.POINTER(_IAudioEndpointVolume))
        hr = endpoint_obj.contents.lpVtbl.contents.SetMasterVolumeLevelScalar(endpoint_ptr, ctypes.c_float(max(0.0, min(1.0, value))), None)
        return hr in (S_OK, S_FALSE)
    finally:
        _release_com(endpoint_ptr)
        if init_hr in (S_OK, S_FALSE):
            _ole32.CoUninitialize()


def _begin_audio_ducking(config: dict):
    if not config.get("duck_audio_during_dictation", False):
        return
    target_percent = max(0, min(100, int(config.get("duck_audio_level_percent", DEFAULT_CONFIG["duck_audio_level_percent"]))))
    target_scalar = target_percent / 100.0
    with _duck_audio_lock:
        if _duck_audio_state["active"]:
            return
        current = _get_master_volume_scalar()
        if current is None:
            print("[AUDIO] Could not read master volume for ducking.", flush=True)
            return
        _duck_audio_state["original_scalar"] = current
        _duck_audio_state["active"] = True
        if current <= target_scalar:
            print(f"[AUDIO] Ducking skipped - current volume already <= {target_percent}%.", flush=True)
            return
        if _set_master_volume_scalar(target_scalar):
            print(f"[AUDIO] Ducked Windows output volume to {target_percent}%.", flush=True)
        else:
            print("[AUDIO] Failed to lower Windows output volume.", flush=True)


def _end_audio_ducking():
    with _duck_audio_lock:
        if not _duck_audio_state["active"]:
            return
        original = _duck_audio_state["original_scalar"]
        _duck_audio_state["active"] = False
        _duck_audio_state["original_scalar"] = None
    if original is None:
        return
    if _set_master_volume_scalar(float(original)):
        print("[AUDIO] Restored Windows output volume.", flush=True)
    else:
        print("[AUDIO] Failed to restore Windows output volume.", flush=True)


def delete_chars(n: int):
    """Send n Backspace key events via SendInput."""
    if n <= 0:
        return
    VK_BACK = 0x08
    events = []
    for _ in range(n):
        for flags in (0, KEYEVENTF_KEYUP):
            ki = _KEYBDINPUT(VK_BACK, 0, flags, 0, 0)
            events.append(_INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki)))
    arr = (_INPUT * len(events))(*events)
    _user32.SendInput(len(events), arr, _input_size)


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
_kernel32 = ctypes.windll.kernel32
_kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
_kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = (ctypes.wintypes.HGLOBAL,)
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = (ctypes.wintypes.HGLOBAL,)
_kernel32.GlobalUnlock.restype = ctypes.c_bool
_kernel32.GlobalFree.argtypes = (ctypes.wintypes.HGLOBAL,)
_kernel32.GlobalFree.restype = ctypes.wintypes.HGLOBAL


def _send_vk(vk: int):
    events = []
    for flags in (0, KEYEVENTF_KEYUP):
        ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
        events.append(_INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki)))
    arr = (_INPUT * len(events))(*events)
    _user32.SendInput(len(events), arr, _input_size)


def _send_ctrl_v():
    events = []
    sequence = [
        (VK_CONTROL, 0),
        (VK_V, 0),
        (VK_V, KEYEVENTF_KEYUP),
        (VK_CONTROL, KEYEVENTF_KEYUP),
    ]
    for vk, flags in sequence:
        ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
        events.append(_INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki)))
    arr = (_INPUT * len(events))(*events)
    _user32.SendInput(len(events), arr, _input_size)


def _send_right_click():
    events = []
    for flags in (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP):
        mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
        events.append(_INPUT(0, _INPUT_UNION(mi=mi)))
    arr = (_INPUT * len(events))(*events)
    _user32.SendInput(len(events), arr, _input_size)


def _release_possible_modifiers():
    """Defensively release modifiers in case a suppressed hotkey leaves state behind."""
    for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
        ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)
        arr = (_INPUT * 1)(_INPUT(INPUT_KEYBOARD, _INPUT_UNION(ki=ki)))
        _user32.SendInput(1, arr, _input_size)


def _clipboard_get_text() -> str | None:
    if not _user32.OpenClipboard(None):
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def _clipboard_set_text(text: str):
    if not _user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        _user32.EmptyClipboard()
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise MemoryError("GlobalAlloc failed")
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            _kernel32.GlobalFree(handle)
            raise MemoryError("GlobalLock failed")
        try:
            ctypes.memmove(ptr, ctypes.addressof(data), size)
        finally:
            _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
    finally:
        _user32.CloseClipboard()


def paste_text(text: str, method: str = "paste_ctrl_v", source: str = "session"):
    """Paste plain text quickly via the clipboard, then restore text clipboard content."""
    previous = _clipboard_get_text()
    restore_delay = 0.05
    try:
        preview = _safe_console_text(repr(text[:80]))
        print(f"[PASTE] Preparing {method} from {source} with {len(text)} chars.", flush=True)
        print(f"[PASTE] Outgoing preview: {preview}", flush=True)
        _clipboard_set_text(text)
        confirm = _clipboard_get_text()
        print(f"[PASTE] Clipboard populated with outgoing text (readback match={confirm == text}).", flush=True)
        time.sleep(0.09 if method == "paste_right_click" else 0.05)
        if method == "paste_right_click":
            print("[PASTE] Sending right-click paste trigger.", flush=True)
            _send_right_click()
            # Console-style right-click paste can consume the clipboard slightly
            # after the click event, so restore later than Ctrl+V paths.
            restore_delay = 0.45
        else:
            print("[PASTE] Sending Ctrl+V paste trigger.", flush=True)
            _send_ctrl_v()
            restore_delay = 0.05
    finally:
        try:
            if previous is not None:
                time.sleep(restore_delay)
                _clipboard_set_text(previous)
                print("[PASTE] Clipboard restored to previous text.", flush=True)
            else:
                print("[PASTE] No prior text clipboard content to restore.", flush=True)
        except Exception:
            print("[PASTE] Clipboard restore failed.", flush=True)


# ── Focus detection ───────────────────────────────────────────────────────────

class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",       ctypes.wintypes.DWORD),
        ("flags",        ctypes.wintypes.DWORD),
        ("hwndActive",   ctypes.wintypes.HWND),
        ("hwndFocus",    ctypes.wintypes.HWND),
        ("hwndCapture",  ctypes.wintypes.HWND),
        ("hwndMenuOwner",ctypes.wintypes.HWND),
        ("hwndMoveSize", ctypes.wintypes.HWND),
        ("hwndCaret",    ctypes.wintypes.HWND),
        ("rcCaret",      ctypes.wintypes.RECT),
    ]


# Win32 / classic app class names that accept keyboard text input.
# This is a mutable set — synced from config at startup and when settings are saved.
_DEFAULT_TEXT_INPUT_CLASSES = [
    "Edit",
    "RichEdit", "RichEdit20W", "RichEdit20A", "RichEdit50W", "RICHEDIT60W",
    "Scintilla",
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "Chrome_RenderWidgetHostHWND",
    "MozillaWindowClass",
    "MozillaContentWindowClass",
    "WebViewWnd",
]
_TEXT_INPUT_CLASSES: set[str] = set(_DEFAULT_TEXT_INPUT_CLASSES)
_DEFAULT_TYPE_INPUT_CLASSES = {
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
}
_TYPE_INPUT_CLASSES: set[str] = set(_DEFAULT_TYPE_INPUT_CLASSES)
_RIGHT_CLICK_PASTE_INPUT_CLASSES: set[str] = set()
_SIMPLE_MODE_INPUT_CLASSES: set[str] = set(_DEFAULT_TYPE_INPUT_CLASSES)
_LIVE_MODE_INPUT_CLASSES: set[str] = set()


def sync_input_classes(cfg: dict):
    """Update _TEXT_INPUT_CLASSES from config. Takes effect immediately."""
    _TEXT_INPUT_CLASSES.clear()
    _TEXT_INPUT_CLASSES.update(cfg.get("input_classes", _DEFAULT_TEXT_INPUT_CLASSES))
    _TYPE_INPUT_CLASSES.clear()
    _TYPE_INPUT_CLASSES.update(cfg.get("type_input_classes", sorted(_DEFAULT_TYPE_INPUT_CLASSES)))
    _RIGHT_CLICK_PASTE_INPUT_CLASSES.clear()
    _RIGHT_CLICK_PASTE_INPUT_CLASSES.update(cfg.get("right_click_paste_input_classes", []))
    _SIMPLE_MODE_INPUT_CLASSES.clear()
    _SIMPLE_MODE_INPUT_CLASSES.update(cfg.get("simple_mode_input_classes", sorted(_DEFAULT_TYPE_INPUT_CLASSES)))
    _LIVE_MODE_INPUT_CLASSES.clear()
    _LIVE_MODE_INPUT_CLASSES.update(cfg.get("live_mode_input_classes", []))


def focused_class() -> str:
    """Return the Win32 class name of the currently focused control."""
    try:
        hwnd = _user32.GetForegroundWindow()
        tid  = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(info))
        target = info.hwndFocus or hwnd
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(target, buf, 256)
        return buf.value
    except Exception:
        return ""


def is_text_input_focused() -> bool:
    cls = focused_class()
    return cls in _TEXT_INPUT_CLASSES


def choose_insert_mode() -> str:
    """Pick the safest insertion path for the currently focused control."""
    cls = focused_class()
    if cls in _TYPE_INPUT_CLASSES:
        return "type"
    if cls in _RIGHT_CLICK_PASTE_INPUT_CLASSES:
        return "paste_right_click"
    return "paste_ctrl_v"


def hotkey_mode_settings(config: dict, profile: str) -> tuple[str, bool]:
    """Return (mode_name, simple_mode) for the given hotkey profile."""
    if profile == "fast":
        mode_name = (config.get("fast_mode") or "").strip().lower()
        if not mode_name:
            mode_name = "live" if config.get("fast_live_mode", config.get("live_mode", False)) else "classic" if config.get("fast_simple_mode", config.get("simple_mode", True)) else "preview"
        simple_mode = bool(config.get("fast_simple_mode", config.get("simple_mode", True)))
    else:
        mode_name = (config.get("main_mode") or "").strip().lower()
        if not mode_name:
            mode_name = "live" if config.get("main_live_mode", config.get("live_mode", False)) else "classic" if config.get("main_simple_mode", config.get("simple_mode", True)) else "preview"
        simple_mode = bool(config.get("main_simple_mode", config.get("simple_mode", True)))
    if mode_name not in {"classic", "preview", "live"}:
        mode_name = "preview"
    return mode_name, simple_mode


def session_mode_settings(config: dict, insert_mode: str, profile: str) -> tuple[str, bool]:
    """Return (mode_name, simple_mode) using hotkey defaults plus focused-class overrides."""
    mode_name, simple_mode = hotkey_mode_settings(config, profile)
    cls = focused_class()

    if cls in _SIMPLE_MODE_INPUT_CLASSES:
        simple_mode = True

    if insert_mode == "type" and cls in _LIVE_MODE_INPUT_CLASSES:
        mode_name = "live"
        simple_mode = False

    return mode_name, simple_mode


# ── Audio ─────────────────────────────────────────────────────────────────────

def list_input_devices() -> list[tuple[int, str, str]]:
    """Return list of (index, name, host_api) for all input devices."""
    hostapis = sd.query_hostapis()
    result   = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = hostapis[d["hostapi"]]["name"]
            result.append((i, d["name"], api))
    return result


# Host APIs that reliably work with PortAudio on Windows.
# WDM-KS (Kernel Streaming) often fails with -9999 on consumer devices.
_PREFERRED_APIS = {"MME", "Windows DirectSound", "Windows WASAPI"}


# ── Voice-activity-detection (VAD) constants ─────────────────────────────────
# Simple energy-based VAD. Audio whose RMS is below _VAD_SILENCE_RMS is treated
# as silence. Once _VAD_SILENCE_SECS of silence are detected after at least
# _VAD_MIN_SPEECH_S of speech, the accumulated utterance is sent to the server
# in a background thread while recording continues.

_VAD_SILENCE_RMS  = 300    # RMS amplitude threshold for silence (0–32767)
_VAD_SILENCE_SECS = 0.8    # seconds of silence that triggers a chunk send
_VAD_MIN_SPEECH_S = 0.4    # minimum speech seconds required before a cut counts


def _device_sample_rate(device) -> int | None:
    """Return the default sample rate for a sounddevice device index, or None."""
    try:
        return int(sd.query_devices(device)["default_samplerate"])
    except Exception:
        return None


def _resample_audio(audio: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """Resample int16 audio array (shape N×1 or N,) via linear interpolation."""
    mono = np.asarray(audio, dtype=np.int16).reshape(-1, 1)
    if from_sr == to_sr:
        return mono
    if from_sr % to_sr == 0:
        factor = from_sr // to_sr
        taps = max(31, factor * 32 + 1)
        if taps % 2 == 0:
            taps += 1
        n = np.arange(taps) - (taps - 1) / 2
        cutoff = 0.45 / factor
        kernel = 2 * cutoff * np.sinc(2 * cutoff * n)
        kernel *= np.hamming(taps)
        kernel /= np.sum(kernel)
        filtered = np.convolve(mono.reshape(-1).astype(np.float64), kernel, mode="same")
        decimated = filtered[::factor]
        return np.clip(decimated, -32768, 32767).astype(np.int16).reshape(-1, 1)
    converted, _state = audioop.ratecv(
        np.ascontiguousarray(mono.reshape(-1)).tobytes(),
        2,
        1,
        int(from_sr),
        int(to_sr),
        None,
    )
    return np.frombuffer(converted, dtype=np.int16).copy().reshape(-1, 1)


def _record_with_vad(
    config: dict,
    on_chunk_ready=None,
    hotkey_name: str = "ptt",
    post_edit_active: bool = False,
    on_post_edit_toggle=None,
    insert_mode: str = "type",
):
    """
    Record while the hotkey is held.  Whenever a pause (_VAD_SILENCE_SECS of
    silence after _VAD_MIN_SPEECH_S of speech) is detected the accumulated
    utterance is handed to a background transcription thread immediately,
    overlapping with continued recording.

    The stream is opened at the device's native sample rate and audio is
    resampled to config["sample_rate"] before being sent to the server —
    this avoids -9997 (Invalid sample rate) on WASAPI devices that only
    support their Windows-configured rate (e.g. 48000 Hz).

    Returns:
        full_audio  – complete int16 array at target SR, or None if too short
        pending     – [(result_holder, done_event), ...] for background sends
        remaining   – audio since last VAD cut (target SR), not yet submitted
    """
    target_sr   = config.get("sample_rate",     16000)
    device      = config.get("microphone_index")
    sil_rms     = config.get("vad_silence_rms",  _VAD_SILENCE_RMS)
    sil_secs    = config.get("vad_silence_secs", _VAD_SILENCE_SECS)
    min_speech  = config.get("vad_min_speech_s", _VAD_MIN_SPEECH_S)
    max_chunk_s = config.get("vad_max_chunk_s",  30.0)

    # Use the device's native sample rate for recording, then resample.
    # This avoids PortAudio -9997 on WASAPI devices locked to e.g. 48000 Hz.
    native_sr = _device_sample_rate(device) or target_sr
    if native_sr != target_sr:
        print(f"[REC] Device native {native_sr} Hz -> resampling to {target_sr} Hz", flush=True)

    SIL_SAMPLES      = max(1, int(sil_secs * native_sr))
    SPEECH_SAMPLES   = max(1, int(min_speech * native_sr))
    MIN_SAMPLES      = max(1, int(0.3 * native_sr))
    HANGOVER_SAMPLES = max(1, int(config.get("vad_hangover_s", 0.3) * native_sr))

    pending:    list[tuple[list, threading.Event]] = []
    all_data:   list[np.ndarray] = []
    current:    list[np.ndarray] = []
    spk         = 0          # total samples since last chunk reset
    chunk_peak  = 0.0        # peak RMS in current (unsent) chunk
    rms_min     = float("inf")
    rms_max     = 0.0
    # Hysteresis state machine
    in_speech   = False      # True while voice (or hangover) is active
    hangover    = 0          # remaining hangover samples
    sil_count   = 0          # consecutive silence samples after hangover expires

    def _submit(audio: np.ndarray):
        resampled = _resample_audio(audio, native_sr, target_sr)
        h: list = []
        ev = threading.Event()
        chunk_index = [len(pending)]   # capture index at submit time
        def _go():
            result = transcribe(resampled, config)
            h.append(result)
            ev.set()
            if on_chunk_ready:
                on_chunk_ready(chunk_index[0], result)
        threading.Thread(target=_go, daemon=True).start()
        pending.append((h, ev))
        print(f"[VAD] Chunk {len(pending)} sent "
              f"({len(resampled)/target_sr:.2f}s)", flush=True)

    def _reset_chunk():
        nonlocal spk, chunk_peak, in_speech, hangover, sil_count
        spk        = 0
        in_speech  = False
        hangover   = 0
        sil_count  = 0
        chunk_peak = max(
            (float(np.sqrt(np.mean(b.astype(np.float64) ** 2))) for b in current),
            default=0.0,
        )

    post_edit_active = _post_edit_profile_name(post_edit_active)
    toggle_y_down = False
    toggle_enabled = on_post_edit_toggle is not None
    toggle_key = (config.get("post_edit_toggle_key") or DEFAULT_CONFIG["post_edit_toggle_key"]).strip().lower()
    if not toggle_key:
        toggle_key = DEFAULT_CONFIG["post_edit_toggle_key"]

    toggle_press_hook = None
    toggle_release_hook = None

    if on_post_edit_toggle:
        try:
            on_post_edit_toggle(post_edit_active)
        except Exception:
            pass

    try:
        if toggle_enabled and insert_mode == "paste_right_click":
            try:
                toggle_press_hook = keyboard.on_press_key(toggle_key, lambda _e: None, suppress=True)
                toggle_release_hook = keyboard.on_release_key(toggle_key, lambda _e: None, suppress=True)
                print(f"[POST] Suppressing toggle key {toggle_key.upper()} for paste_right_click target.", flush=True)
            except Exception as exc:
                print(f"[POST] Failed to suppress toggle key {toggle_key.upper()}: {_safe_console_text(exc)}", flush=True)

        max_record_s = max(1.0, float(max_chunk_s))
        max_record_samples = max(1, int(max_record_s * native_sr))
        print(
            f"[REC] Recording (single sd.rec buffer at {native_sr} Hz, max {max_record_s:.1f}s)...",
            flush=True,
        )
        started_at = time.time()
        recording = sd.rec(
            max_record_samples,
            samplerate=native_sr,
            channels=1,
            dtype="int16",
            device=device,
        )
        reached_limit = False
        while (_native_hotkeys.is_pressed(hotkey_name) if _native_hotkeys else keyboard.is_pressed(config["hotkey"])):
            y_down = False
            if toggle_enabled:
                try:
                    y_down = keyboard.is_pressed(toggle_key)
                except Exception:
                    y_down = False
                if y_down and not toggle_y_down:
                    post_edit_active = _next_post_edit_profile(post_edit_active)
                    state_label = post_edit_active.upper() if post_edit_active else "OFF"
                    print(
                        f"[POST] Session post-edit profile set to {state_label} via {toggle_key.upper()}.",
                        flush=True,
                    )
                    try:
                        on_post_edit_toggle(post_edit_active)
                    except Exception:
                        pass
            toggle_y_down = y_down

            if time.time() - started_at >= max_record_s:
                reached_limit = True
                break
            time.sleep(0.02)

        elapsed_s = min(max(0.0, time.time() - started_at), max_record_s)
        if reached_limit:
            sd.wait()
        else:
            sd.stop()
        used_samples = min(max_record_samples, max(0, int(elapsed_s * native_sr)))
        if used_samples:
            raw_capture = recording[:used_samples].copy()
            all_data.append(raw_capture)
            current.append(raw_capture)
            analysis_block_samples = max(1, int(0.10 * native_sr))
            analysis_blocks = max(1, int(np.ceil(len(raw_capture) / analysis_block_samples)))
            for blk in np.array_split(raw_capture, analysis_blocks):
                if len(blk) == 0:
                    continue
                rms = float(np.sqrt(np.mean(blk.astype(np.float64) ** 2)))
                if rms < rms_min:
                    rms_min = rms
                if rms > rms_max:
                    rms_max = rms
                chunk_peak = max(chunk_peak, rms)
                spk += len(blk)
        else:
            print("[REC] No samples captured before hotkey release.", flush=True)

        skip_legacy_chunk_recorder = True
        rec_chunk_samples = max(512, int(0.10 * native_sr))
        if not skip_legacy_chunk_recorder:
            print(f"[REC] Recording (VAD, sd.rec chunks at {native_sr} Hz)...", flush=True)
        while (not skip_legacy_chunk_recorder) and (_native_hotkeys.is_pressed(hotkey_name) if _native_hotkeys else keyboard.is_pressed(config["hotkey"])):
                y_down = False
                if toggle_enabled:
                    try:
                        y_down = keyboard.is_pressed(toggle_key)
                    except Exception:
                        y_down = False
                    if y_down and not toggle_y_down:
                        post_edit_active = _next_post_edit_profile(post_edit_active)
                        state_label = post_edit_active.upper() if post_edit_active else "OFF"
                        print(
                            f"[POST] Session post-edit profile set to {state_label} via {toggle_key.upper()}.",
                            flush=True,
                        )
                        try:
                            on_post_edit_toggle(post_edit_active)
                        except Exception:
                            pass
                toggle_y_down = y_down

                data = sd.rec(
                    rec_chunk_samples,
                    samplerate=native_sr,
                    channels=1,
                    dtype="int16",
                    device=device,
                )
                sd.wait()
                blk = data.copy()
                frames = len(blk)
                all_data.append(blk)
                current.append(blk)
                rms = float(np.sqrt(np.mean(blk.astype(np.float64) ** 2)))
                if rms < rms_min: rms_min = rms
                if rms > rms_max: rms_max = rms
                spk += frames

                if rms >= sil_rms:
                    # Loud block — enter / stay in speech; reset hangover & silence
                    chunk_peak = max(chunk_peak, rms)
                    in_speech  = True
                    hangover   = HANGOVER_SAMPLES
                    sil_count  = 0
                else:
                    if hangover > 0:
                        # Within hangover window — treat as speech, don't count silence
                        hangover  = max(0, hangover - frames)
                        sil_count  = 0
                    else:
                        # Genuinely silent
                        in_speech = False
                        sil_count += frames

                if config.get("debug"):
                    print(f"[VAD] rms={rms:.0f} speech={in_speech} hang={hangover} "
                          f"sil={sil_count}/{SIL_SAMPLES} peak={chunk_peak:.0f}", flush=True)

                if sil_count >= SIL_SAMPLES and spk >= SPEECH_SAMPLES and current:
                    if chunk_peak >= sil_rms:
                        # Real speech detected in this chunk → send it
                        _submit(np.concatenate(current))
                        current = []
                    else:
                        # No speech reached the threshold — carry tail so a soft
                        # speech onset in the next chunk isn't lost.
                        kept = []
                        kept_samples = 0
                        for block in reversed(current):
                            kept.insert(0, block)
                            kept_samples += len(block)
                            if kept_samples >= SIL_SAMPLES:
                                break
                        current = kept
                        print(f"[VAD] Silence window, no speech "
                              f"(peak={chunk_peak:.0f} < {sil_rms}) "
                              f"- carrying tail forward", flush=True)
                    _reset_chunk()

                # ── Max chunk duration guard ───────────────────────────────
                chunk_dur = sum(len(block) for block in current) / native_sr
                if chunk_dur >= max_chunk_s and chunk_peak >= sil_rms:
                    print(f"[VAD] Max chunk duration {max_chunk_s}s reached - sending", flush=True)
                    _submit(np.concatenate(current))
                    current = []
                    _reset_chunk()
    except Exception as exc:
        print(f"[REC] Stream error: {exc}", flush=True)
        return None, [], None, post_edit_active, None, native_sr
    finally:
        for hook in (toggle_press_hook, toggle_release_hook):
            if hook is not None:
                try:
                    keyboard.unhook(hook)
                except Exception:
                    pass

    print(f"[REC] Hotkey released. RMS min={rms_min:.0f}  max={rms_max:.0f}  "
          f"silence-threshold={sil_rms}  chunks-sent={len(pending)}", flush=True)

    if sum(len(block) for block in all_data) < MIN_SAMPLES and not pending:
        print("[REC] Too short, ignoring.", flush=True)
        return None, [], None, post_edit_active, None, native_sr
    soft_floor = max(120.0, sil_rms * 0.35)
    if not pending and rms_max < soft_floor:
        print(f"[REC] No meaningful speech detected (max={rms_max:.0f} < soft floor {soft_floor:.0f}), ignoring.", flush=True)
        return None, [], None, post_edit_active, None, native_sr
    if not pending and rms_max < sil_rms:
        print(f"[REC] Soft speech stayed below VAD threshold (max={rms_max:.0f} < {sil_rms}) - transcribing anyway.", flush=True)

    raw_full  = np.concatenate(all_data) if all_data else None
    raw_tail  = np.concatenate(current)  if current  else None

    full_audio = _resample_audio(raw_full, native_sr, target_sr) if raw_full is not None else None
    remaining  = _resample_audio(raw_tail, native_sr, target_sr) if raw_tail  is not None else None

    # Drop a very short tail when we already have pending sends
    if remaining is not None and len(remaining) < int(0.3 * target_sr) and pending:
        remaining = None

    return full_audio, pending, remaining, post_edit_active, raw_full, native_sr


# ── Transcription ─────────────────────────────────────────────────────────────

FAILED_AUDIO_DIR = _app_dir() / "failed_audio"


def _post_edit_enabled(config: dict, profile: str, live_mode_selected: bool) -> str:
    return ""


def _history_entry_prompt_text(entry: dict) -> str:
    text = (entry.get("text") or "").strip()
    notes = (entry.get("editor_notes") or "").strip()
    if text and notes:
        return f"{text}\n\nEditor notes:\n{notes}"
    return text


def _recent_transcript_placeholders() -> dict[str, str]:
    recent = [
        _history_entry_prompt_text(e)
        for e in reversed(list(_history))
        if e.get("text") and e.get("post_edit_active")
    ]
    placeholders: dict[str, str] = {}
    recent_block_lines: list[str] = []
    for i in range(5):
        value = recent[i] if i < len(recent) else ""
        placeholders[f"last_{i + 1}"] = value
        if value:
            recent_block_lines.append(f"{i + 1}. {value}")
    placeholders["recent_transcripts"] = "\n".join(recent_block_lines)
    return placeholders


def _normalize_promptish_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _looks_like_prompt_echo(edited_text: str, *, transcript: str, system_prompt: str, developer_prompt: str, rendered_user_prompt: str) -> bool:
    edited_norm = _normalize_promptish_text(edited_text)
    transcript_norm = _normalize_promptish_text(transcript)
    if not edited_norm or len(edited_norm) < 80:
        return False
    if transcript_norm and edited_norm == transcript_norm:
        return False

    prompt_sources = [
        system_prompt,
        developer_prompt,
        rendered_user_prompt,
    ]
    for source in prompt_sources:
        source_norm = _normalize_promptish_text(source)
        if len(source_norm) < 80:
            continue
        if edited_norm == source_norm:
            return True
        if edited_norm in source_norm:
            return True
        if source_norm in edited_norm and len(source_norm) >= max(120, int(len(edited_norm) * 0.6)):
            return True

    suspicious_markers = (
        "you are a careful transcript post-editor",
        "the source input is spoken dictation",
        "preserve:",
        "improve:",
        "remove obvious fillers",
    )
    return any(marker in edited_norm for marker in suspicious_markers)


POST_EDIT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "edited_text": {"type": "string"},
        "editor_notes": {"type": "string"},
    },
    "required": ["edited_text", "editor_notes"],
    "additionalProperties": False,
}


def _render_post_edit_messages(text: str, config: dict, edit_profile: str) -> tuple[list[dict], dict[str, str]]:
    prompt_sections = _load_prompt_markdown(config, edit_profile)
    system_prompt = prompt_sections.get("system") or ""
    developer_prompt = prompt_sections.get("developer") or ""
    user_prompt_template = prompt_sections.get("user") or ""
    prompt_values = {
        "transcript": text,
        **_recent_transcript_placeholders(),
    }
    rendered_developer_prompt = developer_prompt
    for key, value in prompt_values.items():
        rendered_developer_prompt = rendered_developer_prompt.replace(f"{{{key}}}", value)

    rendered_user_prompt = user_prompt_template
    for key, value in prompt_values.items():
        rendered_user_prompt = rendered_user_prompt.replace(f"{{{key}}}", value)

    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    if rendered_developer_prompt.strip():
        messages.append({"role": "developer", "content": rendered_developer_prompt})
    if rendered_user_prompt.strip():
        messages.append({"role": "user", "content": rendered_user_prompt})
    return messages, {
        "system_prompt": system_prompt,
        "developer_prompt": rendered_developer_prompt,
        "rendered_user_prompt": rendered_user_prompt,
    }


def _openai_input_from_messages(messages: list[dict]) -> list[dict]:
    return [
        {
            "role": message.get("role", "user"),
            "content": [{"type": "input_text", "text": str(message.get("content") or "")}],
        }
        for message in messages
        if str(message.get("content") or "").strip()
    ]


def _validate_post_edit_result(
    edited: str,
    editor_notes: str,
    original_text: str,
    prompts: dict[str, str],
) -> tuple[str, str, bool]:
    edited = (edited or "").strip()
    editor_notes = (editor_notes or "").strip()
    if not edited:
        print("[POST] Empty edited text returned - keeping original transcript.", flush=True)
        return original_text, editor_notes, False
    if _looks_like_prompt_echo(
        edited,
        transcript=original_text,
        system_prompt=prompts.get("system_prompt", ""),
        developer_prompt=prompts.get("developer_prompt", ""),
        rendered_user_prompt=prompts.get("rendered_user_prompt", ""),
    ):
        print("[POST] Edited text looks like prompt echo - keeping original transcript.", flush=True)
        return original_text, editor_notes, False

    print(f"[POST] Edited text: {_safe_console_text(repr(edited))}", flush=True)
    if editor_notes:
        print(f"[POST] Editor notes: {_safe_console_text(repr(editor_notes))}", flush=True)
    print("[POST] Post-edit complete.", flush=True)
    return edited, editor_notes, True


def _post_edit_text_openai(
    text: str,
    config: dict,
    profile: str,
    edit_profile: str,
    messages: list[dict],
    prompts: dict[str, str],
) -> tuple[str, str, bool]:
    api_key = _openai_api_key()
    model = (config.get("openai_model") or "").strip()
    reasoning_effort = (config.get("openai_reasoning_effort") or "low").strip().lower()

    if not api_key or not model or not messages:
        print("[POST] Post-edit skipped - OpenAI settings incomplete (set OPENAI_API_KEY and LLM settings).", flush=True)
        return text, "", False

    payload = {
        "model": model,
        "input": _openai_input_from_messages(messages),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "post_edit_result",
                "strict": True,
                "schema": POST_EDIT_RESULT_SCHEMA,
            }
        },
    }
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}

    print(f"[POST] Sending transcript to OpenAI post-editor via {model} ({profile}/{edit_profile}).", flush=True)
    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        print(f"[POST] HTTP {resp.status_code}", flush=True)
        print(f"[POST] Response: {_safe_console_text(resp.text)}", flush=True)
        resp.raise_for_status()
        body = resp.json()

        raw_json = None
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    raw_json = content["text"]
                    break
            if raw_json:
                break

        if not raw_json:
            print("[POST] No structured output text returned - keeping original transcript.", flush=True)
            return text, "", False

        print(f"[POST] Structured output: {_safe_console_text(raw_json)}", flush=True)
        parsed = json.loads(raw_json)
        return _validate_post_edit_result(
            parsed.get("edited_text") or "",
            parsed.get("editor_notes") or "",
            text,
            prompts,
        )
    except Exception as exc:
        print(f"[POST] Post-edit failed: {_safe_console_text(exc)}", flush=True)
        return text, "", False


def _post_edit_text_external(
    text: str,
    config: dict,
    profile: str,
    edit_profile: str,
    messages: list[dict],
    prompts: dict[str, str],
) -> tuple[str, str, bool]:
    url = (config.get("external_post_edit_url") or DEFAULT_CONFIG["external_post_edit_url"]).strip()
    model = (config.get("openai_model") or "").strip()
    if not url or not model or not messages:
        print("[POST] External post-edit skipped - proxy URL, model, or prompt messages missing.", flush=True)
        return text, "", False

    payload = {
        "model": model,
        "profile": profile,
        "edit_profile": edit_profile,
        "transcript": text,
        "messages": messages,
        "schema": POST_EDIT_RESULT_SCHEMA,
    }
    print(f"[POST] Sending transcript to external post-editor via {url} ({model}, {profile}/{edit_profile}).", flush=True)
    try:
        resp = requests.post(url, json=payload, timeout=90)
        print(f"[POST] External HTTP {resp.status_code}", flush=True)
        print(f"[POST] External response: {_safe_console_text(resp.text)}", flush=True)
        resp.raise_for_status()
        body = resp.json()
        return _validate_post_edit_result(
            body.get("edited_text") or body.get("text") or "",
            body.get("editor_notes") or "",
            text,
            prompts,
        )
    except Exception as exc:
        print(f"[POST] External post-edit failed: {_safe_console_text(exc)}", flush=True)
        return text, "", False


def _post_edit_text(text: str, config: dict, profile: str, edit_profile: str = "dev") -> tuple[str, str, bool]:
    edit_profile = _post_edit_profile_name(edit_profile) or "dev"
    messages, prompts = _render_post_edit_messages(text, config, edit_profile)
    provider = (config.get("post_edit_provider") or DEFAULT_CONFIG["post_edit_provider"]).strip().lower()
    if provider in {"external", "proxy", "local"}:
        return _post_edit_text_external(text, config, profile, edit_profile, messages, prompts)
    return _post_edit_text_openai(text, config, profile, edit_profile, messages, prompts)


def _post_wav_bytes(wav_bytes: bytes, config: dict) -> tuple[str | None, str | None]:
    """Send raw WAV bytes to the configured transcription backend. Returns (text, error)."""
    backend = (config.get("transcription_backend") or DEFAULT_CONFIG["transcription_backend"]).strip().lower()
    if backend == "openai":
        return _post_wav_bytes_openai(wav_bytes, config)
    url  = config["server_url"]
    lang = config.get("language")
    print(f"[API] {len(wav_bytes)//1024} KB -> {url}...", flush=True)
    try:
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data  = {"language": lang} if lang else {}
        resp  = requests.post(url, files=files, data=data, timeout=60)
        print(f"[API] HTTP {resp.status_code}", flush=True)
        print(f"[API] Response: {_safe_console_text(resp.text)}", flush=True)
        resp.raise_for_status()
        body = resp.json()
        text = body.get("text", "").strip()
        print(f"[API] Got: {_safe_console_text(repr(text))}", flush=True)
        if not text:
            msg = "Empty transcription (server returned no text)"
            print(f"[API] Error: {msg}", flush=True)
            return None, msg
        return text or None, None
    except requests.exceptions.ConnectionError:
        msg = "Service unavailable"
    except requests.exceptions.Timeout:
        msg = "Request timed out"
    except requests.exceptions.HTTPError as exc:
        msg = f"Server error (HTTP {exc.response.status_code})"
    except requests.RequestException:
        msg = "Transcription failed"
    print(f"[API] Error: {msg}", flush=True)
    return None, msg


def _post_wav_bytes_openai(wav_bytes: bytes, config: dict) -> tuple[str | None, str | None]:
    api_key = _openai_api_key()
    model = (config.get("openai_transcription_model") or DEFAULT_CONFIG["openai_transcription_model"]).strip()
    lang = (config.get("language") or "").strip()
    prompt = (config.get("openai_transcription_prompt") or "").strip()

    if not api_key:
        msg = "OpenAI API key missing"
        print(f"[API] Error: {msg}", flush=True)
        return None, msg
    if not model:
        msg = "OpenAI transcription model missing"
        print(f"[API] Error: {msg}", flush=True)
        return None, msg

    print(f"[API] {len(wav_bytes)//1024} KB -> OpenAI audio/transcriptions ({model})...", flush=True)
    try:
        data = {
            "model": model,
            "response_format": "json",
        }
        if lang:
            data["language"] = lang
        if prompt:
            data["prompt"] = prompt
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            timeout=60,
        )
        print(f"[API] HTTP {resp.status_code}", flush=True)
        print(f"[API] Response: {_safe_console_text(resp.text)}", flush=True)
        resp.raise_for_status()
        body = resp.json()
        text = (body.get("text") or "").strip()
        print(f"[API] Got: {_safe_console_text(repr(text))}", flush=True)
        if not text:
            msg = "Empty OpenAI transcription (server returned no text)"
            print(f"[API] Error: {msg}", flush=True)
            return None, msg
        return text or None, None
    except requests.exceptions.ConnectionError:
        msg = "OpenAI unavailable"
    except requests.exceptions.Timeout:
        msg = "OpenAI request timed out"
    except requests.exceptions.HTTPError as exc:
        code = getattr(exc.response, "status_code", "?")
        msg = f"OpenAI error (HTTP {code})"
    except requests.RequestException:
        msg = "OpenAI transcription failed"
    print(f"[API] Error: {msg}", flush=True)
    return None, msg


def _post_wav_to_server(wav_path: Path, config: dict) -> tuple[str | None, str | None]:
    """Read a WAV file from disk and POST it (used for retrying failed recordings)."""
    print(f"[API] Reading {wav_path.name} ({wav_path.stat().st_size//1024} KB)", flush=True)
    with open(wav_path, "rb") as f:
        return _post_wav_bytes(f.read(), config)


def transcribe(audio: np.ndarray, config: dict) -> tuple[str | None, str | None]:
    """Encode numpy audio to WAV in memory and POST. No temp file on disk."""
    sr       = config.get("sample_rate", 16000)
    duration = len(audio) / sr
    peak     = int(np.abs(audio).max())
    print(f"[REC] chunk {duration:.2f}s  peak {peak}/32767", flush=True)
    if peak < 200:
        print("[REC] WARNING: very low peak — mic may be muted or wrong device.", flush=True)
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return _post_wav_bytes(buf.getvalue(), config)


def _save_failed_wav(audio: np.ndarray, sr: int, label: str = "failed") -> Path:
    """Write audio to the failed_audio/ directory and return the path."""
    FAILED_AUDIO_DIR.mkdir(exist_ok=True)
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or "failed"
    path = FAILED_AUDIO_DIR / f"{safe_label}_{ts}.wav"
    sf.write(str(path), audio, sr, subtype="PCM_16")
    print(f"[FAILED] WAV saved: {path}", flush=True)
    return path


def _should_save_failed_wav(error: str | None) -> bool:
    if not error:
        return False
    skip_errors = {
        "Service unavailable",
    }
    return error not in skip_errors


def retry_transcription(wav_path: str, time_key: str, config: dict) -> tuple[str | None, str | None]:
    """Re-send a saved failed WAV. On success update the history entry and delete the file."""
    path = Path(wav_path)
    if not path.exists():
        return None, f"WAV file not found: {path.name}"
    text, error = _post_wav_to_server(path, config)
    if text:
        # Update the existing history entry in-place (deque items are dicts — mutable)
        for entry in _history:
            if entry.get("time") == time_key:
                entry.pop("error", None)
                entry.pop("wav",   None)
                entry["text"] = text
                break
        _save_history()
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    return text, error


# ── Session ───────────────────────────────────────────────────────────────────

_session_lock = threading.Lock()
_last_typed   = [""]   # text from the most recent successful transcription
_editor_notes_lock = threading.Lock()
_pending_editor_notes = {
    "text": "",
    "expires_at": 0.0,
    "duration": 0.0,
}


def _format_editor_notes_for_insert(text: str) -> str:
    notes = (text or "").strip()
    if not notes:
        return ""
    return f"\n\nEditor notes:\n{notes}"


def _show_pending_editor_notes(text: str, duration: float = 4.0):
    notes = (text or "").strip()
    if not notes:
        return
    with _editor_notes_lock:
        _pending_editor_notes["text"] = notes
        _pending_editor_notes["duration"] = duration
        _pending_editor_notes["expires_at"] = time.time() + duration
    _ensure_overlay()
    _set_overlay(_OVL_EDITOR_NOTES)


def _editor_notes_overlay_duration(text: str, config: dict) -> float:
    notes = (text or "").strip()
    base = max(0.5, float(config.get("editor_notes_overlay_seconds", DEFAULT_CONFIG["editor_notes_overlay_seconds"])))
    chars_per_extra_second = max(
        1.0,
        float(
            config.get(
                "editor_notes_chars_per_extra_second",
                DEFAULT_CONFIG["editor_notes_chars_per_extra_second"],
            )
        ),
    )
    if not notes:
        return base
    return base + (len(notes) / chars_per_extra_second)


def _clear_pending_editor_notes():
    with _editor_notes_lock:
        _pending_editor_notes["text"] = ""
        _pending_editor_notes["duration"] = 0.0
        _pending_editor_notes["expires_at"] = 0.0
    if _overlay_state[0] == _OVL_EDITOR_NOTES:
        _set_overlay(_OVL_IDLE)


def _peek_pending_editor_notes() -> str | None:
    with _editor_notes_lock:
        notes = _pending_editor_notes["text"]
        expires_at = _pending_editor_notes["expires_at"]
    if not notes or time.time() >= expires_at:
        _clear_pending_editor_notes()
        return None
    return notes


def _consume_pending_editor_notes() -> str | None:
    notes = _peek_pending_editor_notes()
    if not notes:
        return None
    _clear_pending_editor_notes()
    return notes


_draft_lock = threading.Lock()
_pending_draft = {
    "active": False,
    "text": "",
    "editor_notes": "",
    "profile": "main",
    "post_edit": False,
    "post_edit_profile": "",
    "post_edit_failed": False,
    "edit_in_progress": False,
    "review_open": False,
    "created_at": 0.0,
}
_preview_overlay_lock = threading.Lock()
_preview_overlay_state = {
    "visible": False,
    "text": "",
    "editor_notes": "",
    "status": "",
    "profile": "main",
    "post_edit": False,
    "loading": False,
}
_preview_overlay_started = [False]
_draft_tap_lock = threading.Lock()
_draft_tap_state = {
    "stamp": 0.0,
    "include_notes": False,
}
_current_config = [None]

_DRAFT_TAP_RELEASE_MAX = 0.28
_DRAFT_SINGLE_TAP_DELAY = 0.38
_DRAFT_DOUBLE_TAP_GAP = 0.45
_DRAFT_NOTES_GRACE = 0.24


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


_user32.MonitorFromPoint.argtypes = (_POINT, ctypes.wintypes.DWORD)
_user32.MonitorFromPoint.restype = ctypes.wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = (ctypes.wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO))
_user32.GetMonitorInfoW.restype = ctypes.c_bool
_user32.GetWindowLongW.argtypes = (ctypes.wintypes.HWND, ctypes.c_int)
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = (ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_long)
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetWindowPos.argtypes = (
    ctypes.wintypes.HWND,
    ctypes.wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.wintypes.UINT,
)
_user32.SetWindowPos.restype = ctypes.c_bool

MONITOR_DEFAULTTONEAREST = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020


def _monitor_work_area(x: int, y: int) -> _RECT:
    pt = _POINT(x, y)
    monitor = _user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    if monitor:
        info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
        if _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return info.rcWork
    return _RECT(0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))


def _apply_noactivate(hwnd: int):
    try:
        exstyle = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        exstyle |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)
        _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
    except Exception:
        pass


def _set_preview_overlay(*, visible: bool, text: str = "", editor_notes: str = "", status: str = "", profile: str = "main", post_edit: bool = False, loading: bool = False):
    with _preview_overlay_lock:
        _preview_overlay_state.update({
            "visible": bool(visible),
            "text": text,
            "editor_notes": editor_notes,
            "status": status,
            "profile": profile,
            "post_edit": post_edit,
            "loading": bool(loading),
        })


def _show_preview_overlay(text: str, status: str, profile: str, post_edit: bool = False, loading: bool = False, editor_notes: str = ""):
    _ensure_preview_overlay()
    _set_preview_overlay(
        visible=True,
        text=text,
        editor_notes=editor_notes,
        status=status,
        profile=profile,
        post_edit=post_edit,
        loading=loading,
    )


def _hide_preview_overlay():
    _set_preview_overlay(visible=False)


def _set_pending_draft(text: str, profile: str, post_edit: bool | str, editor_notes: str = "", post_edit_failed: bool = False):
    notes = (editor_notes or "").strip()
    post_edit_profile = _post_edit_profile_name(post_edit)
    with _draft_lock:
        _pending_draft.update({
            "active": True,
            "text": text,
            "editor_notes": notes,
            "profile": profile,
            "post_edit": bool(post_edit_profile or post_edit is True),
            "post_edit_profile": post_edit_profile,
            "post_edit_failed": bool(post_edit_failed),
            "edit_in_progress": False,
            "review_open": False,
            "created_at": time.time(),
        })
    if post_edit_failed:
        status = "Post-edit failed - single tap inserts raw text, tap + modifier retries edit, double tap opens review"
    else:
        status = "Edited preview ready - tap hotkey to insert, double-tap to review" if post_edit else "Preview ready - tap hotkey to insert, double-tap to edit"
    _show_preview_overlay(text=text, editor_notes=notes, status=status, profile=profile, post_edit=post_edit_profile, loading=False)


def _peek_pending_draft() -> dict | None:
    with _draft_lock:
        if not _pending_draft["active"]:
            return None
        return dict(_pending_draft)


def _clear_pending_draft():
    with _draft_lock:
        _pending_draft.update({
            "active": False,
            "text": "",
            "editor_notes": "",
            "profile": "main",
            "post_edit": False,
            "post_edit_profile": "",
            "post_edit_failed": False,
            "edit_in_progress": False,
            "review_open": False,
            "created_at": 0.0,
        })
    with _draft_tap_lock:
        _draft_tap_state["stamp"] = 0.0
        _draft_tap_state["include_notes"] = False
    _hide_preview_overlay()


def _set_pending_draft_review(open_: bool):
    draft = None
    with _draft_lock:
        if _pending_draft["active"]:
            _pending_draft["review_open"] = bool(open_)
            draft = dict(_pending_draft)
    if not draft:
        _hide_preview_overlay()
        return
    if open_:
        _hide_preview_overlay()
    else:
        if draft.get("edit_in_progress"):
            status = "Post-editing"
        else:
            status = "Edited preview ready - tap hotkey to insert, double-tap to review" if draft["post_edit"] else "Preview ready - tap hotkey to insert, double-tap to edit"
        _show_preview_overlay(
            text=draft["text"],
            editor_notes=draft.get("editor_notes", ""),
            status=status,
            profile=draft["profile"],
            post_edit=draft.get("post_edit_profile", "") if draft.get("post_edit") else "",
            loading=False,
        )


def _draft_review_copy_text(text: str):
    try:
        _clipboard_set_text(text)
        print("[DRAFT] Copied review text to clipboard.", flush=True)
    except Exception as exc:
        print(f"[DRAFT] Clipboard copy failed: {_safe_console_text(exc)}", flush=True)


def _post_edit_toggle_key(config: dict) -> str:
    return (config.get("post_edit_toggle_key") or DEFAULT_CONFIG["post_edit_toggle_key"]).strip().lower()


def _wait_for_notes_insert_request(config: dict, timeout_s: float = _DRAFT_NOTES_GRACE) -> bool:
    toggle_key = _post_edit_toggle_key(config)
    if not toggle_key:
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if keyboard.is_pressed(toggle_key):
                return True
        except Exception:
            return False
        time.sleep(0.01)
    return False


def _current_draft_status(draft: dict) -> str:
    if draft.get("edit_in_progress"):
        return "Post-editing"
    if draft.get("post_edit_failed"):
        return "Post-edit failed - single tap inserts raw text, tap + modifier retries edit, double tap opens review"
    if draft.get("post_edit"):
        return "Edited preview ready - tap hotkey to insert, double-tap to review"
    return "Preview ready - tap hotkey to insert, double-tap to edit"


def _update_preview_from_draft(draft: dict, *, loading: bool | None = None):
    _show_preview_overlay(
        text=draft.get("text", ""),
        editor_notes=draft.get("editor_notes", ""),
        status=_current_draft_status(draft),
        profile=draft.get("profile", "main"),
        post_edit=draft.get("post_edit_profile", "") if draft.get("post_edit") else "",
        loading=bool(draft.get("edit_in_progress")) if loading is None else loading,
    )


def _insert_pending_draft(config: dict, include_notes: bool = False) -> bool:
    draft = _peek_pending_draft()
    if not draft:
        return False
    if draft.get("edit_in_progress"):
        print("[DRAFT] Post-edit still running; insert is not available yet.", flush=True)
        return False
    text = (draft.get("text") or "").strip()
    if not text:
        _clear_pending_draft()
        return False
    notes = (draft.get("editor_notes") or "").strip()
    if include_notes and notes:
        text = f"{text}\n\nEditor notes:\n{notes}".strip()
    if not is_text_input_focused():
        print("[DRAFT] Focused control is not a text input; keeping preview open.", flush=True)
        return False
    insert_mode = choose_insert_mode()
    print(f"[DRAFT] Inserting preview via {insert_mode}{' with editor notes' if include_notes and notes else ''}.", flush=True)
    _release_possible_modifiers()
    if insert_mode == "type":
        type_text(text, char_delay=config.get("char_delay", 0.0))
    else:
        paste_text(text, method=insert_mode, source="draft_insert")
    _last_typed[0] = text
    _clear_pending_draft()
    return True


def _start_pending_draft_post_edit(config_getter) -> bool:
    draft = _peek_pending_draft()
    if not draft:
        return False
    if draft.get("post_edit"):
        return False
    if draft.get("edit_in_progress"):
        print("[DRAFT] Post-edit already running for the preview.", flush=True)
        return True
    source_text = (draft.get("text") or "").strip()
    if not source_text:
        return False
    edit_profile = _post_edit_profile_name(draft.get("post_edit_profile")) or POST_EDIT_PROFILES[0]

    with _draft_lock:
        if not _pending_draft["active"]:
            return False
        _pending_draft["edit_in_progress"] = True
        draft = dict(_pending_draft)
    _update_preview_from_draft(draft, loading=True)
    print("[DRAFT] Starting on-demand edit pass for the preview.", flush=True)

    def _go():
        cfg = config_getter()
        edited_text, editor_notes, success = _post_edit_text(source_text, cfg, draft.get("profile", "main"), edit_profile)
        with _draft_lock:
            if not _pending_draft["active"]:
                return
            _pending_draft["text"] = (edited_text or source_text).strip() or source_text
            _pending_draft["editor_notes"] = (editor_notes or "").strip()
            _pending_draft["post_edit"] = bool(success)
            _pending_draft["post_edit_profile"] = edit_profile if success else ""
            _pending_draft["post_edit_failed"] = not bool(success)
            _pending_draft["edit_in_progress"] = False
            updated = dict(_pending_draft)
        _update_preview_from_draft(updated, loading=False)
        if success:
            print("[DRAFT] On-demand edit pass complete.", flush=True)
        else:
            print("[DRAFT] On-demand edit pass failed; preview kept for retry or raw insert.", flush=True)

    threading.Thread(target=_go, daemon=True).start()
    return True


def _schedule_pending_draft_insert(config_getter):
    stamp = time.time()
    with _draft_tap_lock:
        _draft_tap_state["stamp"] = stamp
        _draft_tap_state["include_notes"] = False

    def _go(local_stamp=stamp):
        time.sleep(_DRAFT_SINGLE_TAP_DELAY)
        with _draft_tap_lock:
            if _draft_tap_state["stamp"] != local_stamp:
                return
            include_notes = bool(_draft_tap_state["include_notes"])
            _draft_tap_state["stamp"] = 0.0
            _draft_tap_state["include_notes"] = False
        cfg = config_getter()
        include_notes = include_notes or _wait_for_notes_insert_request(cfg)
        latest_draft = _peek_pending_draft()
        if include_notes and latest_draft and not latest_draft.get("post_edit"):
            print("[DRAFT] Modifier tap requested edit pass before insertion.", flush=True)
            _start_pending_draft_post_edit(config_getter)
            return
        _insert_pending_draft(cfg, include_notes=include_notes)

    threading.Thread(target=_go, daemon=True).start()


def _register_pending_draft_tap(config_getter) -> str:
    now = time.time()
    cfg = config_getter()
    toggle_key = _post_edit_toggle_key(cfg)
    saw_toggle = False
    if toggle_key:
        try:
            saw_toggle = keyboard.is_pressed(toggle_key)
        except Exception:
            saw_toggle = False
    with _draft_tap_lock:
        last = _draft_tap_state["stamp"]
        if saw_toggle:
            _draft_tap_state["include_notes"] = True
        if last and (now - last) <= _DRAFT_DOUBLE_TAP_GAP:
            _draft_tap_state["stamp"] = 0.0
            include_notes = bool(_draft_tap_state["include_notes"])
            _draft_tap_state["include_notes"] = False
            return "insert_notes" if include_notes else "double"
    _schedule_pending_draft_insert(config_getter)
    return "wait"


def _ensure_preview_overlay():
    if _preview_overlay_started[0]:
        return
    _preview_overlay_started[0] = True
    threading.Thread(target=_preview_overlay_main, daemon=True).start()


def _preview_overlay_main():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()

    preview = tk.Toplevel(root)
    preview.overrideredirect(True)
    preview.wm_attributes("-topmost", True)
    preview.wm_attributes("-alpha", 0.96)
    preview.configure(bg="#0f172a")

    frame = tk.Frame(preview, bg="#0f172a", bd=2, relief="solid", highlightthickness=0)
    frame.pack(fill="both", expand=True)

    header = tk.Frame(frame, bg="#0f172a")
    header.pack(fill="x", padx=12, pady=(10, 6))
    title_var = tk.StringVar(value="Dictation")
    status_var = tk.StringVar(value="")
    body_var = tk.StringVar(value="")
    notes_var = tk.StringVar(value="")
    badge_var = tk.StringVar(value="")
    spinner_frames = ["|", "/", "-", "\\"]
    tick = [0]

    tk.Label(header, textvariable=title_var, bg="#0f172a", fg="#e2e8f0", font=("Segoe UI", 12, "bold")).pack(side="left")
    tk.Label(header, textvariable=badge_var, bg="#0f172a", fg="#93c5fd", font=("Segoe UI", 10, "bold")).pack(side="right")
    tk.Label(frame, textvariable=status_var, bg="#0f172a", fg="#cbd5e1", justify="left", anchor="w", wraplength=520, font=("Segoe UI", 10)).pack(fill="x", padx=12)
    body_label = tk.Label(
        frame,
        textvariable=body_var,
        bg="#0f172a",
        fg="#f8fafc",
        justify="left",
        anchor="nw",
        wraplength=520,
        font=("Segoe UI", 13),
    )
    body_label.pack(fill="both", expand=True, padx=12, pady=(8, 12))
    notes_title = tk.Label(frame, text="Editor notes", bg="#0f172a", fg="#bfdbfe", justify="left", anchor="w", font=("Segoe UI", 10, "bold"))
    notes_body = tk.Label(
        frame,
        textvariable=notes_var,
        bg="#0f172a",
        fg="#cbd5e1",
        justify="left",
        anchor="nw",
        wraplength=560,
        font=("Segoe UI", 10),
    )

    review = tk.Toplevel(root)
    review.title("Dictation Review")
    review.geometry("760x420")
    review.withdraw()
    review.columnconfigure(0, weight=1)
    review.rowconfigure(0, weight=1)
    review_text = tk.Text(review, wrap="word", font=("Segoe UI", 11))
    review_text.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 6))
    review_scroll = tk.Scrollbar(review, orient="vertical", command=review_text.yview)
    review_scroll.grid(row=0, column=1, sticky="ns", pady=(10, 6))
    review_text.configure(yscrollcommand=review_scroll.set)
    review_status = tk.Label(review, text="Review and copy manually if you want to paste later.", anchor="w")
    review_status.grid(row=1, column=0, sticky="ew", padx=10)
    review_btns = tk.Frame(review)
    review_btns.grid(row=2, column=0, columnspan=2, sticky="e", padx=10, pady=(8, 10))

    def _copy_review():
        _draft_review_copy_text(review_text.get("1.0", "end-1c"))
        _clear_pending_draft()

    def _close_review():
        _set_pending_draft_review(False)

    tk.Button(review_btns, text="Copy", command=_copy_review, width=10).pack(side="left", padx=(0, 6))
    tk.Button(review_btns, text="Close", command=_close_review, width=10).pack(side="left")
    review.protocol("WM_DELETE_WINDOW", _close_review)

    def _position_preview(width: int, height: int):
        # Use Tk's pointer coordinates for Tk geometry. Mixing Win32 physical
        # cursor coordinates with Tk-scaled window coordinates drifts badly on
        # mixed-DPI monitor setups.
        try:
            x, y = preview.winfo_pointerxy()
        except Exception:
            x, y = _cursor_pos()

        try:
            work_left = preview.winfo_vrootx()
            work_top = preview.winfo_vrooty()
            work_right = work_left + preview.winfo_vrootwidth()
            work_bottom = work_top + preview.winfo_vrootheight()
        except Exception:
            work_left = 0
            work_top = 0
            work_right = preview.winfo_screenwidth()
            work_bottom = preview.winfo_screenheight()

        offset_x = 24
        offset_y = 24
        margin = 8

        px = x + offset_x
        py = y + offset_y
        if px + width > work_right - margin:
            px = x - width - offset_x
        if py + height > work_bottom - margin:
            py = y - height - offset_y

        px = min(max(px, work_left + margin), max(work_left + margin, work_right - width - margin))
        py = min(max(py, work_top + margin), max(work_top + margin, work_bottom - height - margin))
        preview.geometry(f"{int(width)}x{int(height)}+{int(px)}+{int(py)}")

    def _poll():
        tick[0] += 1
        with _preview_overlay_lock:
            st = dict(_preview_overlay_state)
        cfg = _current_config[0] if _current_config and _current_config[0] else DEFAULT_CONFIG
        max_preview_width = max(500, int(cfg.get("preview_max_width", DEFAULT_CONFIG["preview_max_width"])))
        max_preview_height = max(220, int(cfg.get("preview_max_height", DEFAULT_CONFIG["preview_max_height"])))
        draft = _peek_pending_draft()
        review_open = bool(draft and draft.get("review_open"))
        if review_open:
            wanted = draft.get("text") or ""
            notes = (draft.get("editor_notes") or "").strip()
            if notes:
                wanted = f"{wanted}\n\nEditor notes:\n{notes}".strip()
            current = review_text.get("1.0", "end-1c")
            if current != wanted:
                review_text.delete("1.0", "end")
                review_text.insert("1.0", wanted)
            if not review.winfo_viewable():
                review.deiconify()
                review.lift()
                review.focus_force()
        elif review.winfo_viewable():
            review.withdraw()

        if st.get("visible") and not review_open:
            profile = st.get("profile") or "main"
            post_edit_profile = _post_edit_profile_name(st.get("post_edit"))
            title_var.set("Fast Dictation" if profile == "fast" else "Dictation")
            badge_var.set(f"EDIT {post_edit_profile.upper()}" if post_edit_profile else "EDIT OFF")
            status = st.get("status") or ""
            if st.get("loading"):
                status = f"{status} {spinner_frames[tick[0] % len(spinner_frames)]}".strip()
            status_var.set(status)
            body_var.set(st.get("text") or "Listening...")
            notes_value = (st.get("editor_notes") or "").strip()
            notes_var.set(notes_value)
            if notes_value:
                if not notes_title.winfo_ismapped():
                    notes_title.pack(fill="x", padx=12, pady=(0, 2))
                    notes_body.pack(fill="x", padx=12, pady=(0, 12))
            else:
                if notes_title.winfo_ismapped():
                    notes_title.pack_forget()
                    notes_body.pack_forget()
            body_label.configure(wraplength=560)
            preview.update_idletasks()
            width = max(500, min(max_preview_width, frame.winfo_reqwidth() + 4))
            height = max(220, min(max_preview_height, frame.winfo_reqheight() + 4))
            _position_preview(width, height)
            if not preview.winfo_viewable():
                preview.deiconify()
                preview.lift()
        elif preview.winfo_viewable():
            preview.withdraw()
        root.after(50, _poll)

    root.after(0, lambda: _apply_noactivate(preview.winfo_id()))
    root.after(50, _poll)
    root.mainloop()


def _hotkey_is_down_now(hotkey_name: str, config: dict) -> bool:
    if _native_hotkeys is not None:
        return _native_hotkeys.is_pressed(hotkey_name)
    hotkey_text = config.get("fast_hotkey") if hotkey_name == "fast_ptt" else config.get("hotkey")
    try:
        return keyboard.is_pressed(hotkey_text)
    except Exception:
        return False


def reinsert_last_transcription():
    """Insert the last transcription again using the current focused-field strategy."""
    text = _last_result_text[0] or _last_typed[0] or _latest_history_text()
    if not text:
        print("[REINSERT] No last transcription available.", flush=True)
        return
    if not is_text_input_focused():
        print("[REINSERT] Focused control is not an allowed text input.", flush=True)
        return

    insert_mode = choose_insert_mode()
    print(f"[REINSERT] Re-inserting last transcription via {insert_mode}.", flush=True)
    _release_possible_modifiers()
    if insert_mode == "type":
        type_text(text)
    else:
        paste_text(text, method=insert_mode, source="last_result_reinsert")


# ── Live-mode overlay indicator ───────────────────────────────────────────────
# A small always-on-top dot in the bottom-right corner.
# Only used when live_mode = True; invisible otherwise.

_OVL_IDLE         = 0
_OVL_RECORDING    = 1
_OVL_TRANSCRIBING = 2
_OVL_FAST_RECORDING    = 3
_OVL_FAST_TRANSCRIBING = 4
_OVL_EDITOR_NOTES = 5

_overlay_state   = [_OVL_IDLE]
_overlay_started = [False]
_overlay_pos     = [0, 0]   # cursor position captured when recording starts


def _cursor_pos() -> tuple[int, int]:
    """Return current mouse cursor position via Win32."""
    import ctypes
    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _set_overlay(state: int):
    if state != _OVL_IDLE:
        # Snapshot cursor position so the dot appears next to where the
        # user's mouse is when recording / transcribing begins.
        x, y = _cursor_pos()
        _overlay_pos[0] = x
        _overlay_pos[1] = y
    _overlay_state[0] = state


def _ensure_overlay():
    """Start the overlay thread the first time it is needed."""
    if _overlay_started[0]:
        return
    _overlay_started[0] = True
    threading.Thread(target=_overlay_main, daemon=True).start()


def _overlay_main():
    import tkinter as tk

    win = tk.Tk()
    win.overrideredirect(True)               # no title-bar / borders
    win.wm_attributes("-topmost", True)
    win.wm_attributes("-alpha", 0.92)

    SIZE   = 22   # overlay canvas size in pixels
    OFFSET = 18   # distance from cursor tip to dot centre
    NOTE_W = 360
    NOTE_H = 180

    # Initial off-screen placement; real position set on first show.
    win.geometry(f"{SIZE}x{SIZE}+-100+-100")

    # Any pixel painted with this exact colour becomes click-through.
    TRANSPARENT = "#010203"
    win.wm_attributes("-transparentcolor", TRANSPARENT)
    win.configure(bg=TRANSPARENT)

    canvas = tk.Canvas(win, width=SIZE, height=SIZE, bg=TRANSPARENT,
                       highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    note_bg = canvas.create_rectangle(0, 0, 0, 0, fill="#0f172a", outline="#334155", width=2)
    note_title = canvas.create_text(0, 0, text="", anchor="nw", fill="#bfdbfe", font=("Segoe UI", 12, "bold"))
    note_text = canvas.create_text(0, 0, text="", anchor="nw", fill="#e2e8f0", width=NOTE_W - 28, font=("Segoe UI", 11))
    note_bar_bg = canvas.create_rectangle(0, 0, 0, 0, fill="#1e293b", outline="", width=0)
    note_bar_fg = canvas.create_rectangle(0, 0, 0, 0, fill="#38bdf8", outline="", width=0)
    halo = canvas.create_polygon(0, 0, 0, 0, 0, 0, 0, 0,
                                 fill="", outline="", width=0, smooth=False)
    dot = canvas.create_oval(4, 4, SIZE - 4, SIZE - 4,
                             fill="#ef4444", outline="", width=0)
    win.withdraw()   # start hidden

    _prev = [_OVL_IDLE]
    _tick = [0]
    _COLORS = {
        _OVL_RECORDING: "#ef4444",
        _OVL_TRANSCRIBING: "#f59e0b",
        _OVL_FAST_RECORDING: "#0f766e",
        _OVL_FAST_TRANSCRIBING: "#0891b2",
    }
    _FAST_STATES = {_OVL_FAST_RECORDING, _OVL_FAST_TRANSCRIBING}

    def _hide_notes():
        canvas.coords(note_bg, 0, 0, 0, 0)
        canvas.coords(note_title, 0, 0)
        canvas.itemconfig(note_title, text="")
        canvas.coords(note_text, 0, 0)
        canvas.itemconfig(note_text, text="")
        canvas.coords(note_bar_bg, 0, 0, 0, 0)
        canvas.coords(note_bar_fg, 0, 0, 0, 0)

    def _set_halo(radius: int, color: str, width: int):
        cx = SIZE // 2
        cy = SIZE // 2
        points = [
            cx, cy - radius,
            cx + radius, cy,
            cx, cy + radius,
            cx - radius, cy,
        ]
        canvas.coords(halo, *points)
        canvas.itemconfig(halo, outline=color, width=width)

    def _poll():
        state = _overlay_state[0]
        _tick[0] += 1
        note_text_value = None
        note_ratio = 0.0
        if state == _OVL_EDITOR_NOTES:
            with _editor_notes_lock:
                note_text_value = (_pending_editor_notes.get("text") or "").strip()
                expires_at = float(_pending_editor_notes.get("expires_at") or 0.0)
                duration = max(0.1, float(_pending_editor_notes.get("duration") or 0.0))
            remaining = expires_at - time.time()
            if not note_text_value or remaining <= 0:
                _clear_pending_editor_notes()
                state = _overlay_state[0]
            else:
                note_ratio = max(0.0, min(1.0, remaining / duration))
        if state != _prev[0]:
            _prev[0] = state
            if state == _OVL_IDLE:
                win.withdraw()
            else:
                cx, cy = _overlay_pos[0], _overlay_pos[1]
                if state == _OVL_EDITOR_NOTES:
                    win.geometry(f"{NOTE_W}x{NOTE_H}+{cx + OFFSET}+{cy + OFFSET}")
                    canvas.config(width=NOTE_W, height=NOTE_H)
                else:
                    win.geometry(f"{SIZE}x{SIZE}+{cx + OFFSET}+{cy + OFFSET}")
                    canvas.config(width=SIZE, height=SIZE)
                    canvas.itemconfig(dot, fill=_COLORS[state])
                win.deiconify()
                win.lift()
        if state == _OVL_EDITOR_NOTES:
            canvas.coords(note_bg, 4, 4, NOTE_W - 4, NOTE_H - 4)
            canvas.coords(note_title, 14, 12)
            canvas.itemconfig(note_title, text="Editor notes")
            canvas.coords(note_text, 14, 40)
            canvas.itemconfig(note_text, text=note_text_value)
            canvas.coords(note_bar_bg, 14, NOTE_H - 18, NOTE_W - 14, NOTE_H - 10)
            canvas.coords(note_bar_fg, 14, NOTE_H - 18, 14 + int((NOTE_W - 28) * note_ratio), NOTE_H - 10)
            canvas.itemconfig(halo, outline="", width=0)
            canvas.coords(dot, 0, 0, 0, 0)
        else:
            _hide_notes()
            canvas.coords(dot, 4, 4, SIZE - 4, SIZE - 4)
        if state in _FAST_STATES:
            pulse = 8 + (_tick[0] % 6) // 2
            _set_halo(radius=pulse, color=_COLORS[state], width=2)
        elif state != _OVL_EDITOR_NOTES:
            canvas.itemconfig(halo, outline="", width=0)
        win.after(50, _poll)

    _poll()
    win.mainloop()


# Single-character status indicators so deletion is always exactly 1 backspace.
# Listening   : block-shade pulse ░ → ▒ → ▓ → ▒ → ░ …  (CP437: 176-177-178)
# Transcribing: box-corner spin   ┐ → ┘ → └ → ┌ → …    (CP437: 191-217-192-218)
# The / character is intentionally avoided — it opens command palettes in many apps.
_LISTEN_FRAMES   = ["\u2591", "\u2592", "\u2593", "\u2588"]  # ░ ▒ ▓ █
_SPIN_FRAMES     = ["\u2510", "\u2518", "\u2514", "\u250c"]  # ┐ ┘ └ ┌
_LISTEN_INTERVAL = 0.35
_SPIN_INTERVAL   = 0.18

_TRAILING_PUNCT = set(".!?,;:")

# Phrases Whisper commonly hallucinates on near-silent audio.
_HALLUCINATION_PHRASES: frozenset[str] = frozenset({
    "thank you.", "thank you", "thanks.", "thanks",
    "thanks for watching.", "thanks for watching",
    "thank you for watching.", "thank you for watching",
    "you", "you.", ".", "..", "...", "....", ".....",
    "bye.", "bye", "goodbye.", "goodbye",
    "subscribe.", "subscribe",
    "like and subscribe.", "like and subscribe",
    "um", "uh", "um.", "uh.",
})


def _is_hallucination(text: str) -> bool:
    """Return True if the transcription result looks like a Whisper hallucination.

    Whisper is prone to producing repetitive gibberish (e.g. hundreds of 'I'
    or '.' characters) or well-known filler phrases when it receives near-silent
    audio.  Such results should be silently discarded rather than typed.

    Heuristics (any one is sufficient):
      1. After stripping spaces the text is empty.
      2. The lowercased stripped text matches a known hallucination phrase.
      3. Any single non-space character makes up ≥ 40 % of the non-space content
         (catches "IIIIIII…", "........", etc.).
      4. The text is suspiciously short (≤ 3 non-space characters) and consists
         entirely of punctuation or the single word "you" / "um" / "uh".
    """
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.lower() in _HALLUCINATION_PHRASES:
        return True
    chars = stripped.replace(" ", "")
    if chars:
        from collections import Counter
        top_count = Counter(chars).most_common(1)[0][1]
        if top_count / len(chars) >= 0.40:
            return True
    return False


def _strip_boundary_ellipsis(text: str) -> str:
    """Remove model-added continuation ellipses from chunk and final boundaries."""
    cleaned = (text or "").strip()
    while True:
        stripped = cleaned.rstrip()
        if stripped.endswith("..."):
            cleaned = stripped[:-3].rstrip()
            continue
        if stripped.endswith("…"):
            cleaned = stripped[:-1].rstrip()
            continue
        return stripped


def _join_chunks(texts: list[str]) -> str:
    """Join VAD chunk transcriptions into a single string.

    Parakeet adds terminal punctuation to every chunk it sees as a complete
    sentence.  When the VAD cuts mid-sentence the result looks like:
        "I was trying to explain. something important."
    Heuristic: if chunk[N] ends with '.' / '...' / '…' and chunk[N+1] starts
    with a lowercase letter, digit, or opening bracket/quote, that ending was
    likely added by the model at an artificial boundary — strip it so the
    joined text reads naturally.
    Other punctuation (! ? , ; :) is never stripped automatically.
    """
    cleaned: list[str] = []
    for i, text in enumerate(texts):
        text = _strip_boundary_ellipsis(text)
        if not text:
            continue
        # Look ahead: if the next non-empty chunk starts with lowercase,
        # digit, or an opener, this chunk's trailing punctuation is likely
        # spurious chunk-boundary punctuation.
        next_text = next((t.strip() for t in texts[i + 1:] if t.strip()), "")
        if next_text and (
            next_text[0].islower()
            or next_text[0].isdigit()
            or next_text[0] in "([{\"'`"
        ):
            if text.endswith("..."):
                text = text[:-3].rstrip()
            elif text.endswith("…"):
                text = text[:-1].rstrip()
            elif text.endswith("."):
                text = text[:-1].rstrip()
        cleaned.append(text)
    return _strip_boundary_ellipsis(" ".join(cleaned))


def run_session(
    config: dict,
    insert_mode: str = "type",
    profile: str = "main",
    hotkey_name: str = "ptt",
    on_post_edit_change=None,
):
    """Full push-to-talk cycle: record → transcribe → type.  Runs in a thread."""
    if not _session_lock.acquire(blocking=False):
        print("[SESSION] Already recording, ignoring trigger.", flush=True)
        return

    if insert_mode == "auto":
        insert_mode = choose_insert_mode()
        print(f"[TYPE] Auto insert mode selected: {insert_mode}", flush=True)

    # typed_status tracks how many chars are currently in the field as status.
    # It is always 0 or 1 — deletion is a single backspace, safe on SSH/terminals.
    typed_status = [0]
    _anim_lock   = threading.Lock()

    def _erase_status():
        with _anim_lock:
            if typed_status[0]:
                delete_chars(typed_status[0])
                typed_status[0] = 0

    def _start_listen_anim(frames, interval):
        """Listening animation — replace in-place with a mirrored ping-pong cycle."""
        stop = threading.Event()
        if len(frames) <= 1:
            cycle = [0]
        else:
            cycle = list(range(len(frames))) + list(range(len(frames) - 2, 0, -1))
        cycle_pos = [0]

        def _run():
            while not stop.wait(interval):
                with _anim_lock:
                    cycle_pos[0] = (cycle_pos[0] + 1) % len(cycle)
                    idx = cycle[cycle_pos[0]]
                    if typed_status[0]:
                        delete_chars(1)
                    type_text(frames[idx])
                    typed_status[0] = 1

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return stop, t

    def _start_spin_anim(frames, interval):
        """Transcribing spinner — replace in-place (backspace + new char).
        Safe to use here because the hotkey is already released."""
        stop = threading.Event()
        idx  = [len(frames) - 1]

        def _run():
            while not stop.wait(interval):
                with _anim_lock:
                    idx[0] = (idx[0] + 1) % len(frames)
                    if typed_status[0]:
                        delete_chars(1)
                    type_text(frames[idx[0]])
                    typed_status[0] = 1

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return stop, t

    mode_name, simple_cfg = session_mode_settings(config, insert_mode, profile)
    live_cfg = mode_name == "live"
    post_edit_active = _post_edit_enabled(config, profile, live_cfg)
    live = live_cfg and insert_mode == "type"
    _begin_audio_ducking(config)

    if mode_name == "preview" or (not live and mode_name != "classic"):
        preview_text = [""]
        preview_status = ["Listening"]
        preview_post_edit = [post_edit_active]

        def _apply_post_edit_state(enabled):
            preview_post_edit[0] = _post_edit_profile_name(enabled)
            if on_post_edit_change:
                try:
                    on_post_edit_change(preview_post_edit[0])
                except Exception:
                    pass
            _show_preview_overlay(
                text=preview_text[0],
                editor_notes=((_peek_pending_draft() or {}).get("editor_notes", "")),
                status=preview_status[0],
                profile=profile,
                post_edit=preview_post_edit[0],
                loading=False,
            )

        def _set_preview(status: str | None = None, text: str | None = None, *, loading: bool = False):
            if status is not None:
                preview_status[0] = status
            if text is not None:
                preview_text[0] = text
            _show_preview_overlay(
                text=preview_text[0],
                editor_notes=((_peek_pending_draft() or {}).get("editor_notes", "")),
                status=preview_status[0],
                profile=profile,
                post_edit=preview_post_edit[0],
                loading=loading,
            )

        _apply_post_edit_state(post_edit_active)

        try:
            time.sleep(config.get("pre_type_delay", 0.05))
            chunk_results: dict[int, str] = {}
            chunk_lock = threading.Lock()
            _set_preview("Listening", "")
            print("[STATUS] Listening (preview)", flush=True)

            def _on_classic_chunk(chunk_index: int, result: tuple):
                text_value, error_value = result
                if error_value or not text_value or _is_hallucination(text_value):
                    return
                with chunk_lock:
                    chunk_results[chunk_index] = text_value
                    ordered = [chunk_results[i] for i in sorted(chunk_results)]
                _set_preview(text=_join_chunks(ordered))

            full_audio, pending, remaining, post_edit_active, raw_full_audio, native_sr = _record_with_vad(
                config,
                on_chunk_ready=_on_classic_chunk,
                hotkey_name=hotkey_name,
                post_edit_active=post_edit_active,
                on_post_edit_toggle=_apply_post_edit_state,
                insert_mode=insert_mode,
            )

            if full_audio is None:
                _hide_preview_overlay()
                return

            _set_preview("Transcribing", preview_text[0], loading=True)
            print("[STATUS] Transcribing (preview)", flush=True)

            if remaining is not None:
                h: list = []
                ev = threading.Event()

                def _go(a=remaining):
                    h.append(transcribe(a, config))
                    ev.set()

                threading.Thread(target=_go, daemon=True).start()
                pending.append((h, ev))
                print(
                    f"[VAD] Final chunk sent ({len(remaining)/config.get('sample_rate',16000):.2f}s)",
                    flush=True,
                )

            texts: list[str] = []
            errors: list[str] = []
            for i, (holder, ev) in enumerate(pending):
                ev.wait(timeout=65)
                if holder:
                    t, e = holder[0]
                    if t:
                        if _is_hallucination(t):
                            print(f"[VAD] Chunk {i+1} discarded (hallucination): {t!r}", flush=True)
                        else:
                            texts.append(t)
                            print(f"[VAD] Chunk {i+1} -> {t!r}", flush=True)
                    elif e:
                        errors.append(e)
                        print(f"[VAD] Chunk {i+1} error: {e}", flush=True)

            result = _join_chunks(texts) if texts else None
            error = errors[0] if errors and not texts else None
            editor_notes = ""
            post_edit_success = False

            if result:
                _set_preview(text=result)
            if result and post_edit_active and not live_cfg:
                _set_preview("Post-editing", result, loading=True)
                result, editor_notes, post_edit_success = _post_edit_text(result, config, profile, post_edit_active)

            review_non_post_edit = bool(
                config.get(
                    "fast_review_non_post_edit_sessions" if profile == "fast" else "main_review_non_post_edit_sessions",
                    False,
                )
            )
            auto_insert_without_post_edit = not review_non_post_edit

            if result:
                _set_last_result_text(result)
                _add_to_history({
                    "time": _now_str(),
                    "text": result,
                    "editor_notes": editor_notes,
                    "post_edit_active": bool(post_edit_success and post_edit_active and not live_cfg),
                    "post_edit_profile": post_edit_active if post_edit_success and post_edit_active and not live_cfg else "",
                })
                should_hold_preview = bool(post_edit_active) or not auto_insert_without_post_edit
                if should_hold_preview:
                    _set_pending_draft(
                        result,
                        profile=profile,
                        post_edit=(post_edit_active if post_edit_success else ""),
                        editor_notes=editor_notes,
                        post_edit_failed=bool(post_edit_active and not post_edit_success),
                    )
                    print("[DRAFT] Preview ready and waiting for hotkey tap.", flush=True)
                else:
                    cls = focused_class()
                    if not is_text_input_focused():
                        print(
                            f"[TYPE] Skipped — focused element is not a text input (class: {cls!r}). Text saved to history.",
                            flush=True,
                        )
                        _hide_preview_overlay()
                    else:
                        action = {
                            "type": "Typing",
                            "paste_ctrl_v": "Pasting (Ctrl+V)",
                            "paste_right_click": "Pasting (right click)",
                        }.get(insert_mode, "Typing")
                        print(f"[TYPE] {action} {len(result)} chars into {cls!r}", flush=True)
                        _last_typed[0] = ""
                        if insert_mode in {"paste_ctrl_v", "paste_right_click"}:
                            paste_text(result, method=insert_mode, source=f"{profile}_result")
                        else:
                            type_text(result, char_delay=config.get("char_delay", 0.0))
                        _last_typed[0] = result
                        if editor_notes:
                            _show_pending_editor_notes(
                                editor_notes,
                                duration=_editor_notes_overlay_duration(editor_notes, config),
                            )
                        _hide_preview_overlay()
                        print("[TYPE] Done.", flush=True)
            elif error:
                history_entry = {"time": _now_str(), "text": None, "error": error}
                if _should_save_failed_wav(error):
                    wav_path = _save_failed_wav(full_audio, config.get("sample_rate", 16000), "failed_resampled")
                    history_entry["wav"] = str(wav_path)
                    if raw_full_audio is not None:
                        history_entry["raw_wav"] = str(_save_failed_wav(raw_full_audio, native_sr, "failed_native"))
                _add_to_history(history_entry)
                _hide_preview_overlay()
                print(f"[TYPE] Error — typing message for 5 s: {error!r}", flush=True)
                type_text(error)
                time.sleep(5)
                delete_chars(len(error))
            else:
                _hide_preview_overlay()
                print("[TYPE] Empty result — nothing typed.", flush=True)
            return
        finally:
            _end_audio_ducking()
            _session_lock.release()

    if on_post_edit_change:
        try:
            on_post_edit_change(post_edit_active)
        except Exception:
            pass
    fancy = not simple_cfg
    inline_status = (
        insert_mode == "type"
        or profile == "fast"
        or (profile == "main" and simple_cfg)
    )
    overlay_status = False
    overlay_recording = _OVL_FAST_RECORDING if profile == "fast" else _OVL_RECORDING
    overlay_transcribing = _OVL_FAST_TRANSCRIBING if profile == "fast" else _OVL_TRANSCRIBING

    try:
        # ════════════════════════════════════════════════════════════════════
        # CLASSIC MODE — collect all chunks, type everything at the end.
        # Text-field indicators (® / ¿ or fancy animations) show status.
        # ════════════════════════════════════════════════════════════════════
        if not live:
            time.sleep(config.get("pre_type_delay", 0.05))
            if overlay_status:
                _ensure_overlay()
                _set_overlay(overlay_recording)

            # ── Listening indicator ───────────────────────────────────────
            if inline_status and fancy:
                with _anim_lock:
                    type_text(_LISTEN_FRAMES[0])
                    typed_status[0] = 1
                print("[STATUS] Listening (fancy)", flush=True)
                stop_listen, listen_thread = _start_listen_anim(
                    _LISTEN_FRAMES, _LISTEN_INTERVAL)
            elif inline_status:
                with _anim_lock:
                    type_text("\u00ae")   # ®
                    typed_status[0] = 1
                print("[STATUS] Listening", flush=True)
            else:
                print("[STATUS] Listening", flush=True)

            full_audio, pending, remaining, post_edit_active, raw_full_audio, native_sr = _record_with_vad(
                config,
                hotkey_name=hotkey_name,
                post_edit_active=post_edit_active,
                on_post_edit_toggle=(None if live else on_post_edit_change),
                insert_mode=insert_mode,
            )

            if inline_status and fancy:
                stop_listen.set()
                listen_thread.join(timeout=1.0)
                _erase_status()

            if full_audio is None:
                if overlay_status:
                    _set_overlay(_OVL_IDLE)
                return

            # ── Transcribing indicator ────────────────────────────────────
            if overlay_status:
                _set_overlay(overlay_transcribing)
            if inline_status and fancy:
                with _anim_lock:
                    type_text(_SPIN_FRAMES[0])
                    typed_status[0] = 1
                print("[STATUS] Transcribing (fancy)", flush=True)
                stop_spin, spin_thread = _start_spin_anim(
                    _SPIN_FRAMES, _SPIN_INTERVAL)
            elif inline_status:
                with _anim_lock:
                    if typed_status[0]:
                        delete_chars(typed_status[0])
                    type_text("\u00bf")   # ¿
                    typed_status[0] = 1
                print("[STATUS] Transcribing", flush=True)
            else:
                print("[STATUS] Transcribing", flush=True)

            # Submit final audio chunk
            if remaining is not None:
                h: list = []
                ev = threading.Event()
                def _go(a=remaining):
                    h.append(transcribe(a, config))
                    ev.set()
                threading.Thread(target=_go, daemon=True).start()
                pending.append((h, ev))
                print(f"[VAD] Final chunk sent "
                      f"({len(remaining)/config.get('sample_rate',16000):.2f}s)",
                      flush=True)

            # Collect ALL results in order
            texts:  list[str] = []
            errors: list[str] = []
            for i, (holder, ev) in enumerate(pending):
                ev.wait(timeout=65)
                if holder:
                    t, e = holder[0]
                    if t:
                        if _is_hallucination(t):
                            print(f"[VAD] Chunk {i+1} discarded (hallucination): {t!r}", flush=True)
                        else:
                            texts.append(t)
                            print(f"[VAD] Chunk {i+1} -> {t!r}", flush=True)
                    elif e:
                        errors.append(e)
                        print(f"[VAD] Chunk {i+1} error: {e}", flush=True)

            result = _join_chunks(texts) if texts else None
            error  = errors[0] if errors and not texts else None

            editor_notes = ""
            post_edit_success = False
            if result and post_edit_active and not live_cfg:
                result, editor_notes, post_edit_success = _post_edit_text(result, config, profile, post_edit_active)

            if inline_status and fancy:
                stop_spin.set()
                spin_thread.join(timeout=1.0)
            if inline_status:
                _erase_status()
                erase_delay = float(config.get("erase_delay", 0.08))
                if insert_mode == "paste_right_click":
                    erase_delay = max(erase_delay, 0.14)
                time.sleep(erase_delay)
            if overlay_status:
                _set_overlay(_OVL_IDLE)

            if result:
                _set_last_result_text(result)
                _add_to_history({
                    "time": _now_str(),
                    "text": result,
                    "editor_notes": editor_notes,
                    "post_edit_active": bool(post_edit_success and post_edit_active and not live_cfg),
                    "post_edit_profile": post_edit_active if post_edit_success and post_edit_active and not live_cfg else "",
                })
                cls = focused_class()
                if not is_text_input_focused():
                    print(f"[TYPE] Skipped — focused element is not a text input "
                          f"(class: {cls!r}). Text saved to history.", flush=True)
                else:
                    action = {
                        "type": "Typing",
                        "paste_ctrl_v": "Pasting (Ctrl+V)",
                        "paste_right_click": "Pasting (right click)",
                    }.get(insert_mode, "Typing")
                    print(f"[TYPE] {action} {len(result)} chars into {cls!r}", flush=True)
                    _last_typed[0] = ""
                    if insert_mode in {"paste_ctrl_v", "paste_right_click"}:
                        paste_text(result, method=insert_mode, source=f"{profile}_result")
                    else:
                        type_text(result, char_delay=config.get("char_delay", 0.0))
                    _last_typed[0] = result
                    if editor_notes:
                        _show_pending_editor_notes(
                            editor_notes,
                            duration=_editor_notes_overlay_duration(editor_notes, config),
                        )
                    print("[TYPE] Done.", flush=True)
            elif error:
                history_entry = {"time": _now_str(), "text": None, "error": error}
                if _should_save_failed_wav(error):
                    wav_path = _save_failed_wav(full_audio, config.get("sample_rate", 16000), "failed_resampled")
                    history_entry["wav"] = str(wav_path)
                    if raw_full_audio is not None:
                        history_entry["raw_wav"] = str(_save_failed_wav(raw_full_audio, native_sr, "failed_native"))
                _add_to_history(history_entry)
                print(f"[TYPE] Error — typing message for 5 s: {error!r}", flush=True)
                type_text(error)
                time.sleep(5)
                delete_chars(len(error))
            else:
                print("[TYPE] Empty result — nothing typed.", flush=True)

        # ════════════════════════════════════════════════════════════════════
        # LIVE MODE — stream each chunk to the field as it arrives.
        # No text-field status characters; screen overlay dot shows state.
        # ════════════════════════════════════════════════════════════════════
        else:
            _ensure_overlay()

            focused     = is_text_input_focused()
            focused_cls = focused_class()
            char_delay  = config.get("char_delay", 0.0)

            field_content    = ""
            all_chunk_texts: list[str] = []
            _stream_lock     = threading.Lock()
            recording_active = [True]
            _ooo_buffer: dict[int, tuple] = {}
            _next_stream_idx = [0]

            def _flush_ooo():
                nonlocal field_content
                while _next_stream_idx[0] in _ooo_buffer:
                    t, e = _ooo_buffer.pop(_next_stream_idx[0])
                    _next_stream_idx[0] += 1
                    if t:
                        if _is_hallucination(t):
                            print(f"[STREAM] Discarded (hallucination): {t!r}", flush=True)
                        else:
                            stripped = t.strip()
                            if stripped and focused:
                                prefix = " " if field_content else ""
                                type_text(prefix + stripped, char_delay=char_delay)
                                field_content += prefix + stripped
                                print(f"[STREAM] Typed chunk: {stripped!r}", flush=True)
                            all_chunk_texts.append(t)

            def _on_chunk_during_hold(chunk_index: int, result: tuple):
                with _stream_lock:
                    if not recording_active[0]:
                        return
                    _ooo_buffer[chunk_index] = result
                    _flush_ooo()

            # ── Record; show red dot ──────────────────────────────────────
            time.sleep(config.get("pre_type_delay", 0.05))
            _set_overlay(_OVL_RECORDING)
            print("[STATUS] Listening (live)", flush=True)

            full_audio, pending, remaining, _, raw_full_audio, native_sr = _record_with_vad(
                config,
                on_chunk_ready=_on_chunk_during_hold,
                hotkey_name=hotkey_name,
                post_edit_active=post_edit_active,
                on_post_edit_toggle=None,
                insert_mode=insert_mode,
            )

            # Stop streaming callback
            with _stream_lock:
                recording_active[0] = False
                _flush_ooo()

            if full_audio is None:
                _set_overlay(_OVL_IDLE)
                return

            # Collect pending chunks that finished just after hotkey release
            still_pending: list = []
            for idx, (holder, ev) in enumerate(pending):
                if idx < _next_stream_idx[0]:
                    continue   # already typed by callback
                if ev.is_set() and holder:
                    t, e = holder[0]
                    if t:
                        _ooo_buffer[idx] = (t, e)
                else:
                    still_pending.append((idx, holder, ev))
            _flush_ooo()

            # Submit final audio chunk
            if remaining is not None:
                h_f: list = []
                ev_f = threading.Event()
                def _go_f(a=remaining):
                    h_f.append(transcribe(a, config))
                    ev_f.set()
                threading.Thread(target=_go_f, daemon=True).start()
                still_pending.append((len(pending), h_f, ev_f))
                print(f"[VAD] Final chunk sent "
                      f"({len(remaining)/config.get('sample_rate',16000):.2f}s)",
                      flush=True)

            # ── Wait for remaining chunks; show amber dot ─────────────────
            more_texts:  list[str] = []
            more_errors: list[str] = []

            if still_pending:
                _set_overlay(_OVL_TRANSCRIBING)
                for _, holder, ev in still_pending:
                    ev.wait(timeout=65)
                    if holder:
                        t, e = holder[0]
                        if t:
                            if _is_hallucination(t):
                                print(f"[VAD] Late chunk discarded (hallucination): {t!r}", flush=True)
                            else:
                                more_texts.append(t)
                                print(f"[VAD] Late chunk -> {t!r}", flush=True)
                        elif e:
                            more_errors.append(e)

            _set_overlay(_OVL_IDLE)

            # ── Type remaining chunks (post-release) ─────────────────────
            all_chunk_texts.extend(more_texts)
            full_result = _join_chunks(all_chunk_texts) if all_chunk_texts else None
            error       = more_errors[0] if more_errors and not all_chunk_texts else None

            if more_texts and focused:
                if field_content and not full_result.startswith(field_content):
                    delete_chars(1)
                    field_content = field_content[:-1]
                to_type = full_result[len(field_content):]
                if to_type:
                    type_text(to_type, char_delay=char_delay)
                    field_content += to_type

            if full_result:
                _set_last_result_text(full_result)
                _add_to_history({
                    "time": _now_str(),
                    "text": full_result,
                    "editor_notes": "",
                    "post_edit_active": False,
                })
                if not focused:
                    print(f"[TYPE] Skipped — not a text input "
                          f"(class: {focused_cls!r}). Saved to history.", flush=True)
                else:
                    _last_typed[0] = field_content
                    print(f"[TYPE] Done — {len(field_content)} chars in "
                          f"{focused_cls!r}.", flush=True)
            elif error:
                history_entry = {"time": _now_str(), "text": None, "error": error}
                if _should_save_failed_wav(error):
                    wav_path = _save_failed_wav(full_audio, config.get("sample_rate", 16000), "failed_resampled")
                    history_entry["wav"] = str(wav_path)
                    if raw_full_audio is not None:
                        history_entry["raw_wav"] = str(_save_failed_wav(raw_full_audio, native_sr, "failed_native"))
                _add_to_history(history_entry)
                print(f"[TYPE] Error — typing message for 5 s: {error!r}", flush=True)
                type_text(error)
                time.sleep(5)
                delete_chars(len(error))
            else:
                print("[TYPE] Empty result — nothing typed.", flush=True)

    finally:
        _end_audio_ducking()
        _session_lock.release()


# ── Settings UI ───────────────────────────────────────────────────────────────

_MODIFIERS = {
    # English names
    "ctrl", "shift", "alt", "windows",
    "left ctrl", "right ctrl", "left shift", "right shift",
    "left alt", "right alt", "left windows", "right windows",
    # German names (keyboard library uses locale key names on German Windows)
    "strg", "umschalt", "feststell", "altgr",
    "linke strg", "rechte strg",
    "linke umschalt", "rechte umschalt",
    "linkes alt", "rechtes alt",
    "linke windows", "rechte windows",
}


def validate_hotkey(hotkey: str) -> tuple[bool, str]:
    """
    Check that a hotkey string:
      - is non-empty
      - contains at least one non-modifier key, or at least two modifiers
      - can be registered / unregistered by the keyboard library without error
    Returns (ok, message).
    """
    hotkey = hotkey.strip()
    if not hotkey:
        return False, "Hotkey cannot be empty."

    parts = [p.strip().lower() for p in hotkey.split("+")]
    non_mods = [p for p in parts if p not in _MODIFIERS]
    if not non_mods and len(parts) < 2:
        return False, (
            "Modifier-only hotkeys must include at least two modifiers.\n"
            "Examples: ctrl+windows or ctrl+shift."
        )

    # Try a live register/unregister to catch bad key names.
    # IMPORTANT: pass the handler *object* (not the string) to remove_hotkey.
    # Passing a string removes ALL handlers for that combination, which would
    # silently kill the main hotkey if the user typed it into the field.
    try:
        cmd = _helper_command("--validate", hotkey)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            creationflags=creationflags,
        )
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        payload = json.loads(lines[-1]) if lines else {}
        if not payload.get("ok"):
            return False, payload.get("error") or "Invalid hotkey."
    except Exception as exc:
        try:
            handler = keyboard.add_hotkey(hotkey, lambda: None, suppress=False)
            keyboard.remove_hotkey(handler)
        except Exception:
            return False, f"Invalid hotkey: {exc}"

    return True, f"OK — {hotkey}"


def open_settings(config: dict, on_save_callback):
    """Open a Tkinter settings dialog. Runs in its own thread."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    win = tk.Tk()
    win.title("OverMultiASRSuite - Settings")
    win.resizable(False, False)

    outer = tk.Frame(win, padx=10, pady=8)
    outer.pack(fill="both", expand=True)

    pad = {"padx": 8, "pady": 4}


    # ── Notebook (Settings / Microphone Test / Text Input Classes) ────────────
    nb = ttk.Notebook(outer)
    nb.pack(fill="x", expand=False)

    MIC = tk.Frame(nb, padx=10, pady=8)  # Microphone tab
    S  = tk.Frame(nb, padx=10, pady=8)   # Advanced tab
    LLM = tk.Frame(nb, padx=10, pady=8)  # LLM tab
    MT = tk.Frame(nb, padx=10, pady=8)   # Microphone Test tab
    IC = tk.Frame(nb, padx=10, pady=8)   # Input Classes tab
    nb.add(MIC, text="  Microphone  ")
    nb.add(S,  text="  Advanced  ")
    nb.add(LLM, text="  LLM  ")
    nb.add(MT, text="  Microphone Test  ")
    nb.add(IC, text="  Input Classes  ")

    def _fit_to_current_tab(*_):
        win.update_idletasks()
        current = nb.nametowidget(nb.select())
        nb.configure(
            width=max(480, current.winfo_reqwidth() + 24),
            height=max(140, current.winfo_reqheight() + 18),
        )
        win.update_idletasks()
        work_h = _work_area_height() or win.winfo_screenheight()
        req_w = max(520, outer.winfo_reqwidth() + 24)
        req_h = min(max(360, outer.winfo_reqheight() + 24), work_h - 24)
        win.geometry(f"{req_w}x{req_h}")

    nb.bind("<<NotebookTabChanged>>", _fit_to_current_tab)

    # ════════════════════════════════════════════════════════════════════════
    # Settings tab
    # ════════════════════════════════════════════════════════════════════════
    S.columnconfigure(1, weight=1)

    tk.Label(S, text="Backend:").grid(row=0, column=0, sticky="e", **pad)
    transcription_backend_var = tk.StringVar(
        master=win,
        value=config.get("transcription_backend", DEFAULT_CONFIG["transcription_backend"]),
    )
    ttk.Combobox(
        S,
        textvariable=transcription_backend_var,
        values=("http", "openai"),
        width=18,
        state="readonly",
    ).grid(row=0, column=1, sticky="w", **pad)

    tk.Label(S, text="Server URL:").grid(row=1, column=0, sticky="e", **pad)
    url_var = tk.StringVar(master=win, value=config["server_url"])
    url_entry = tk.Entry(S, textvariable=url_var, width=46)
    url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)

    # Hotkey
    tk.Label(S, text="Hotkey:").grid(row=2, column=0, sticky="e", **pad)
    hotkey_frame = tk.Frame(S)
    hotkey_frame.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
    hotkey_var = tk.StringVar(master=win, value=config["hotkey"])
    tk.Entry(hotkey_frame, textvariable=hotkey_var, width=28).pack(side="left")
    hotkey_status = tk.Label(hotkey_frame, text="", width=30, anchor="w")
    hotkey_status.pack(side="left", padx=(8, 0))

    def _set_status(ok, msg):
        hotkey_status.config(text=msg, fg="#16a34a" if ok else "#dc2626")

    def _validate_current(*_):
        ok, msg = validate_hotkey(hotkey_var.get())
        _set_status(ok, msg)

    hotkey_var.trace_add("write", _validate_current)
    _capture_active = [False]

    def _start_capture():
        if _capture_active[0]:
            return
        _capture_active[0] = True
        capture_btn.config(state="disabled")
        _set_status(False, "Press your hotkey now...")
        def _do():
            try:
                combo = keyboard.read_hotkey(suppress=False)
                win.after(0, lambda: hotkey_var.set(combo) or _validate_current())
            except Exception as exc:
                win.after(0, lambda: _set_status(False, f"Capture failed: {exc}"))
            finally:
                win.after(0, lambda: capture_btn.config(state="normal"))
                _capture_active[0] = False
        threading.Thread(target=_do, daemon=True).start()

    capture_btn = tk.Button(hotkey_frame, text="Capture...", command=_start_capture)
    capture_btn.pack(side="left", padx=(6, 0))
    _validate_current()

    # Fast paste hotkey
    tk.Label(S, text="Fast hotkey:").grid(row=3, column=0, sticky="e", **pad)
    fast_frame = tk.Frame(S)
    fast_frame.grid(row=3, column=1, columnspan=2, sticky="w", **pad)
    fast_var = tk.StringVar(master=win, value=config.get("fast_hotkey", ""))
    tk.Entry(fast_frame, textvariable=fast_var, width=28).pack(side="left")
    fast_status = tk.Label(fast_frame, text="", width=30, anchor="w")
    fast_status.pack(side="left", padx=(8, 0))

    def _validate_fast(*_):
        val = fast_var.get().strip()
        if not val:
            fast_status.config(text="disabled", fg="grey")
            return
        ok, msg = validate_hotkey(val)
        fast_status.config(text=msg, fg="#16a34a" if ok else "#dc2626")

    fast_var.trace_add("write", _validate_fast)
    _fast_capture_active = [False]

    def _start_fast_capture():
        if _fast_capture_active[0]:
            return
        _fast_capture_active[0] = True
        fast_capture_btn.config(state="disabled")
        fast_status.config(text="Press your fast hotkey now...", fg="grey")
        def _do():
            try:
                combo = keyboard.read_hotkey(suppress=False)
                win.after(0, lambda: fast_var.set(combo) or _validate_fast())
            except Exception as exc:
                win.after(0, lambda: fast_status.config(text=f"Capture failed: {exc}", fg="#dc2626"))
            finally:
                win.after(0, lambda: fast_capture_btn.config(state="normal"))
                _fast_capture_active[0] = False
        threading.Thread(target=_do, daemon=True).start()

    fast_capture_btn = tk.Button(fast_frame, text="Capture...", command=_start_fast_capture)
    fast_capture_btn.pack(side="left", padx=(6, 0))
    _validate_fast()

    # Reinsert-last hotkey
    tk.Label(S, text="Last result hotkey:").grid(row=4, column=0, sticky="e", **pad)
    undo_frame = tk.Frame(S)
    undo_frame.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
    undo_var = tk.StringVar(master=win, value=config.get("undo_hotkey", ""))
    tk.Entry(undo_frame, textvariable=undo_var, width=28).pack(side="left")
    undo_status = tk.Label(undo_frame, text="", width=30, anchor="w")
    undo_status.pack(side="left", padx=(8, 0))

    def _validate_undo(*_):
        val = undo_var.get().strip()
        if not val:
            undo_status.config(text="disabled", fg="grey")
            return
        ok, msg = validate_hotkey(val)
        undo_status.config(text=msg, fg="#16a34a" if ok else "#dc2626")

    undo_var.trace_add("write", _validate_undo)
    _undo_capture_active = [False]

    def _start_undo_capture():
        if _undo_capture_active[0]:
            return
        _undo_capture_active[0] = True
        undo_capture_btn.config(state="disabled")
        undo_status.config(text="Press your last-result hotkey now...", fg="grey")
        def _do():
            try:
                combo = keyboard.read_hotkey(suppress=False)
                win.after(0, lambda: undo_var.set(combo) or _validate_undo())
            except Exception as exc:
                win.after(0, lambda: undo_status.config(text=f"Capture failed: {exc}", fg="#dc2626"))
            finally:
                win.after(0, lambda: undo_capture_btn.config(state="normal"))
                _undo_capture_active[0] = False
        threading.Thread(target=_do, daemon=True).start()

    undo_capture_btn = tk.Button(undo_frame, text="Capture...", command=_start_undo_capture)
    undo_capture_btn.pack(side="left", padx=(6, 0))
    _validate_undo()

    # Microphone tab
    MIC.columnconfigure(0, weight=1)
    tk.Label(
        MIC,
        text="Choose the recording device used for push-to-talk. You can also switch it from the tray icon.",
        fg="grey",
        justify="left",
        wraplength=460,
    ).grid(row=0, column=0, sticky="w", padx=4, pady=(4, 10))

    devices = list_input_devices()
    wasapi_devices = [(i, n, api) for i, n, api in devices if api == "Windows WASAPI"]
    wasapi_devices = [(i, n, api) for i, n, api in devices if api == "Windows WASAPI"]
    wasapi_devices = [(i, n, api) for i, n, api in devices if api == "Windows WASAPI"]

    def _dev_label(i, n, api):
        return f"{i}: {n}  [{api}{' (!)' if api not in _PREFERRED_APIS else ''}]"

    mic_labels  = ["(system default)"] + [_dev_label(i, n, api) for i, n, api in devices]
    mic_var     = tk.StringVar(master=win)
    current_idx = config.get("microphone_index")
    mic_var.set(
        mic_labels[0] if current_idx is None else
        next((_dev_label(i, n, api) for i, n, api in devices if i == current_idx), mic_labels[0])
    )
    ttk.Combobox(MIC, textvariable=mic_var, values=mic_labels, width=58, state="readonly").grid(
        row=1, column=0, sticky="ew", padx=4, pady=(0, 6)
    )

    # Language
    tk.Label(S, text="Language (BCP-47):").grid(row=6, column=0, sticky="e", **pad)
    lang_var = tk.StringVar(master=win, value=config.get("language") or "")
    tk.Entry(S, textvariable=lang_var, width=16).grid(row=6, column=1, sticky="w", **pad)
    tk.Label(S, text="blank = auto", fg="grey").grid(row=6, column=2, sticky="w", **pad)

    # ── VAD ───────────────────────────────────────────────────────────────────
    vad_frame = tk.LabelFrame(S, text=" Voice Activity Detection ", padx=8, pady=4)
    vad_frame.grid(row=7, column=0, columnspan=3, sticky="ew", padx=2, pady=(6, 2))
    vad_frame.columnconfigure(1, weight=1)

    vpad = {"padx": 6, "pady": 3}

    tk.Label(vad_frame, text="Silence RMS:").grid(row=0, column=0, sticky="e", **vpad)
    vad_rms_var = tk.StringVar(master=win, value=str(config.get("vad_silence_rms", 500)))
    tk.Entry(vad_frame, textvariable=vad_rms_var, width=7).grid(row=0, column=1, sticky="w", **vpad)
    tk.Label(vad_frame, text="0–32767  raise if pauses are never detected",
             fg="grey").grid(row=0, column=2, sticky="w", **vpad)

    tk.Label(vad_frame, text="Silence secs:").grid(row=1, column=0, sticky="e", **vpad)
    vad_secs_var = tk.StringVar(master=win, value=str(config.get("vad_silence_secs", 1.5)))
    tk.Entry(vad_frame, textvariable=vad_secs_var, width=7).grid(row=1, column=1, sticky="w", **vpad)
    tk.Label(vad_frame, text="seconds of silence that fires a background send",
             fg="grey").grid(row=1, column=2, sticky="w", **vpad)

    tk.Label(vad_frame, text="Min speech secs:").grid(row=2, column=0, sticky="e", **vpad)
    vad_speech_var = tk.StringVar(master=win, value=str(config.get("vad_min_speech_s", 0.5)))
    tk.Entry(vad_frame, textvariable=vad_speech_var, width=7).grid(row=2, column=1, sticky="w", **vpad)
    tk.Label(vad_frame, text="minimum speech before a cut is allowed",
             fg="grey").grid(row=2, column=2, sticky="w", **vpad)

    tk.Label(vad_frame, text="Hangover secs:").grid(row=3, column=0, sticky="e", **vpad)
    vad_hang_var = tk.StringVar(master=win, value=str(config.get("vad_hangover_s", 0.3)))
    tk.Entry(vad_frame, textvariable=vad_hang_var, width=7).grid(row=3, column=1, sticky="w", **vpad)
    tk.Label(vad_frame, text="stay in speech state this long after last loud block",
             fg="grey").grid(row=3, column=2, sticky="w", **vpad)

    tk.Label(vad_frame, text="Max chunk secs:").grid(row=4, column=0, sticky="e", **vpad)
    vad_max_var = tk.StringVar(master=win, value=str(config.get("vad_max_chunk_s", 30.0)))
    tk.Entry(vad_frame, textvariable=vad_max_var, width=7).grid(row=4, column=1, sticky="w", **vpad)
    tk.Label(vad_frame, text="force-send after this many seconds regardless of silence",
             fg="grey").grid(row=4, column=2, sticky="w", **vpad)

    # ── Mode defaults ─────────────────────────────────────────────────────────
    main_mode_var = tk.StringVar(master=win, value=config.get("main_mode", DEFAULT_CONFIG["main_mode"]))
    main_simple_var = tk.BooleanVar(master=win, value=bool(config.get("main_simple_mode", config.get("simple_mode", True))))
    main_post_edit_var = tk.BooleanVar(master=win, value=bool(config.get("main_post_edit_mode", False)))
    fast_mode_var = tk.StringVar(master=win, value=config.get("fast_mode", DEFAULT_CONFIG["fast_mode"]))
    fast_simple_var = tk.BooleanVar(master=win, value=bool(config.get("fast_simple_mode", config.get("simple_mode", True))))
    fast_post_edit_var = tk.BooleanVar(master=win, value=bool(config.get("fast_post_edit_mode", False)))
    main_review_non_post_edit_var = tk.BooleanVar(
        master=win,
        value=bool(config.get("main_review_non_post_edit_sessions", DEFAULT_CONFIG["main_review_non_post_edit_sessions"])),
    )
    fast_review_non_post_edit_var = tk.BooleanVar(
        master=win,
        value=bool(config.get("fast_review_non_post_edit_sessions", DEFAULT_CONFIG["fast_review_non_post_edit_sessions"])),
    )
    normal_mode_frame = tk.LabelFrame(S, text=" Normal Hotkey ", padx=8, pady=4)
    normal_mode_frame.grid(row=8, column=0, columnspan=3, sticky="ew", padx=2, pady=(6, 2))
    normal_mode_frame.columnconfigure(0, weight=1)

    tk.Label(normal_mode_frame, text="Mode:").grid(row=0, column=0, sticky="w", padx=4, pady=(2, 0))
    ttk.Combobox(
        normal_mode_frame,
        textvariable=main_mode_var,
        values=("classic", "preview", "live"),
        width=14,
        state="readonly",
    ).grid(row=0, column=1, sticky="w", padx=(0, 4), pady=(2, 0))

    simple_cb = tk.Checkbutton(
        normal_mode_frame,
        text="Simple mode — use plain ® / ¿ indicators instead of animations (SSH / terminal safe) [classic mode only]",
        variable=main_simple_var,
        anchor="w",
        justify="left",
        wraplength=500,
    )
    simple_cb.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 2))

    main_post_edit_cb = tk.Checkbutton(
        normal_mode_frame,
        text="Start with post-edit enabled — send the final transcript through the LLM before insertion; you can toggle it during dictation with the key set in the LLM tab",
        variable=main_post_edit_var,
        anchor="w",
        justify="left",
        wraplength=500,
    )
    main_post_edit_cb.grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 2))

    main_review_non_post_edit_cb = tk.Checkbutton(
        normal_mode_frame,
        text="When post-edit is off, keep the floating preview open. Single tap inserts, single tap + modifier starts post-transcription edit, and double tap opens review. If off, non-post-edit results insert immediately on release.",
        variable=main_review_non_post_edit_var,
        anchor="w",
        justify="left",
        wraplength=500,
    )
    main_review_non_post_edit_cb.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 2))

    def _update_simple_state(*_):
        mode = main_mode_var.get().strip().lower()
        simple_cb.grid_remove()
        main_review_non_post_edit_cb.grid_remove()
        main_post_edit_cb.grid_remove()
        if mode == "classic":
            simple_cb.grid()
        elif mode == "preview":
            main_review_non_post_edit_cb.grid()

    main_mode_var.trace_add("write", _update_simple_state)
    _update_simple_state()

    fast_mode_frame = tk.LabelFrame(S, text=" Fast Hotkey ", padx=8, pady=4)
    fast_mode_frame.grid(row=9, column=0, columnspan=3, sticky="ew", padx=2, pady=(6, 2))
    fast_mode_frame.columnconfigure(0, weight=1)

    tk.Label(fast_mode_frame, text="Mode:").grid(row=0, column=0, sticky="w", padx=4, pady=(2, 0))
    ttk.Combobox(
        fast_mode_frame,
        textvariable=fast_mode_var,
        values=("classic", "preview", "live"),
        width=14,
        state="readonly",
    ).grid(row=0, column=1, sticky="w", padx=(0, 4), pady=(2, 0))

    fast_simple_cb = tk.Checkbutton(
        fast_mode_frame,
        text="Simple mode (classic mode only; live only applies on typed-input targets)",
        variable=fast_simple_var,
        anchor="w",
        justify="left",
        wraplength=500,
    )
    fast_simple_cb.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 2))

    fast_post_edit_cb = tk.Checkbutton(
        fast_mode_frame,
        text="Start with post-edit enabled — send the final transcript through the LLM before insertion; you can toggle it during dictation with the key set in the LLM tab",
        variable=fast_post_edit_var,
        anchor="w",
        justify="left",
        wraplength=500,
    )
    fast_post_edit_cb.grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 2))

    fast_review_non_post_edit_cb = tk.Checkbutton(
        fast_mode_frame,
        text="When post-edit is off, keep the floating preview open. Single tap inserts, single tap + modifier starts post-transcription edit, and double tap opens review. If off, non-post-edit results insert immediately on release.",
        variable=fast_review_non_post_edit_var,
        anchor="w",
        justify="left",
        wraplength=500,
    )
    fast_review_non_post_edit_cb.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 2))

    def _update_fast_simple_state(*_):
        mode = fast_mode_var.get().strip().lower()
        fast_simple_cb.grid_remove()
        fast_review_non_post_edit_cb.grid_remove()
        fast_post_edit_cb.grid_remove()
        if mode == "classic":
            fast_simple_cb.grid()
        elif mode == "preview":
            fast_review_non_post_edit_cb.grid()

    fast_mode_var.trace_add("write", _update_fast_simple_state)
    _update_fast_simple_state()

    preview_size_frame = tk.LabelFrame(S, text=" Preview Window ", padx=8, pady=4)
    preview_size_frame.grid(row=10, column=0, columnspan=3, sticky="ew", padx=2, pady=(6, 2))
    preview_size_frame.columnconfigure(1, weight=1)

    tk.Label(preview_size_frame, text="Max width:").grid(row=0, column=0, sticky="e", **vpad)
    preview_max_width_var = tk.StringVar(
        master=win,
        value=str(config.get("preview_max_width", DEFAULT_CONFIG["preview_max_width"])),
    )
    tk.Entry(preview_size_frame, textvariable=preview_max_width_var, width=8).grid(row=0, column=1, sticky="w", **vpad)
    tk.Label(preview_size_frame, text="pixels", fg="grey").grid(row=0, column=2, sticky="w", **vpad)

    tk.Label(preview_size_frame, text="Max height:").grid(row=1, column=0, sticky="e", **vpad)
    preview_max_height_var = tk.StringVar(
        master=win,
        value=str(config.get("preview_max_height", DEFAULT_CONFIG["preview_max_height"])),
    )
    tk.Entry(preview_size_frame, textvariable=preview_max_height_var, width=8).grid(row=1, column=1, sticky="w", **vpad)
    tk.Label(preview_size_frame, text="pixels", fg="grey").grid(row=1, column=2, sticky="w", **vpad)

    duck_audio_var = tk.BooleanVar(
        master=win,
        value=bool(config.get("duck_audio_during_dictation", DEFAULT_CONFIG["duck_audio_during_dictation"])),
    )
    duck_audio_level_var = tk.StringVar(
        master=win,
        value=str(config.get("duck_audio_level_percent", DEFAULT_CONFIG["duck_audio_level_percent"])),
    )

    duck_audio_frame = tk.LabelFrame(S, text=" Audio Ducking ", padx=8, pady=4)
    duck_audio_frame.grid(row=11, column=0, columnspan=3, sticky="ew", padx=2, pady=(6, 2))
    duck_audio_frame.columnconfigure(1, weight=1)

    duck_audio_cb = tk.Checkbutton(
        duck_audio_frame,
        text="Lower Windows output volume while dictating/transcribing, then restore it when the session ends",
        variable=duck_audio_var,
        anchor="w",
        justify="left",
        wraplength=500,
    )
    duck_audio_cb.grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 4))

    tk.Label(duck_audio_frame, text="Target volume:").grid(row=1, column=0, sticky="e", **vpad)
    duck_audio_level_entry = tk.Entry(duck_audio_frame, textvariable=duck_audio_level_var, width=8)
    duck_audio_level_entry.grid(row=1, column=1, sticky="w", **vpad)
    tk.Label(duck_audio_frame, text="% of current Windows output volume", fg="grey").grid(row=1, column=2, sticky="w", **vpad)

    def _update_duck_audio_state(*_):
        duck_audio_level_entry.config(state=("normal" if duck_audio_var.get() else "disabled"))

    duck_audio_var.trace_add("write", _update_duck_audio_state)
    _update_duck_audio_state()

    # ════════════════════════════════════════════════════════════════════════
    # LLM tab
    # ════════════════════════════════════════════════════════════════════════
    LLM.columnconfigure(1, weight=1)

    tk.Label(
        LLM,
        text="Configure optional OpenAI post-editing for classic-mode transcriptions. "
            "The API key is read from the OPENAI_API_KEY environment variable, not from config.json. "
             "The OpenAI transcription backend can also use its own cleanup-oriented prompt file. "
             "Use {transcript} in the user prompt template for the current transcript. "
             "The post-edit toggle key cycles per-session profiles: off, dev, pro, personal, then off again. "
             "Each profile uses its own markdown prompt file named <profile>_post_edit_prompt.md. "
             "Keep project-specific vocabulary in local prompt or context files.",
        fg="grey",
        justify="left",
        wraplength=520,
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

    tk.Label(LLM, text="API key env:").grid(row=1, column=0, sticky="e", **pad)
    api_key_status = "set" if _openai_api_key() else "not set"
    tk.Label(LLM, text=f"OPENAI_API_KEY ({api_key_status})", anchor="w").grid(row=1, column=1, sticky="w", **pad)

    def _make_scrolled_text(parent, row: int, height: int):
        frame = tk.Frame(parent)
        frame.grid(row=row, column=1, sticky="nsew", **pad)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text_widget = tk.Text(frame, width=62, height=height, wrap="word")
        yscroll = tk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        text_widget.configure(yscrollcommand=yscroll.set)
        text_widget.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        return text_widget

    tk.Label(LLM, text="Model:").grid(row=2, column=0, sticky="e", **pad)
    openai_model_var = tk.StringVar(master=win, value=config.get("openai_model", DEFAULT_CONFIG["openai_model"]))
    tk.Entry(LLM, textvariable=openai_model_var, width=28).grid(row=2, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Reasoning effort:").grid(row=3, column=0, sticky="e", **pad)
    reasoning_var = tk.StringVar(master=win, value=config.get("openai_reasoning_effort", DEFAULT_CONFIG["openai_reasoning_effort"]))
    ttk.Combobox(
        LLM,
        textvariable=reasoning_var,
        values=("minimal", "low", "medium", "high"),
        width=12,
        state="readonly",
    ).grid(row=3, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Transcribe model:").grid(row=4, column=0, sticky="e", **pad)
    openai_transcription_model_var = tk.StringVar(
        master=win,
        value=config.get("openai_transcription_model", DEFAULT_CONFIG["openai_transcription_model"]),
    )
    tk.Entry(LLM, textvariable=openai_transcription_model_var, width=28).grid(row=4, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Transcribe prompt:").grid(row=5, column=0, sticky="ne", **pad)
    openai_transcription_prompt_text = _make_scrolled_text(LLM, row=5, height=4)
    openai_transcription_prompt_text.insert(
        "1.0",
        config.get("openai_transcription_prompt", DEFAULT_CONFIG["openai_transcription_prompt"]),
    )
    tk.Label(LLM, text="Transcribe prompt file:").grid(row=6, column=0, sticky="ne", **pad)
    tk.Label(
        LLM,
        text=str(_transcription_prompt_markdown_path(config)),
        anchor="w",
        justify="left",
        wraplength=520,
        fg="grey",
    ).grid(row=6, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Notes base secs:").grid(row=7, column=0, sticky="e", **pad)
    editor_notes_overlay_var = tk.StringVar(
        master=win,
        value=str(config.get("editor_notes_overlay_seconds", DEFAULT_CONFIG["editor_notes_overlay_seconds"])),
    )
    tk.Entry(LLM, textvariable=editor_notes_overlay_var, width=8).grid(row=7, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Chars / extra sec:").grid(row=8, column=0, sticky="e", **pad)
    editor_notes_scale_var = tk.StringVar(
        master=win,
        value=str(config.get("editor_notes_chars_per_extra_second", DEFAULT_CONFIG["editor_notes_chars_per_extra_second"])),
    )
    tk.Entry(LLM, textvariable=editor_notes_scale_var, width=8).grid(row=8, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Toggle key:").grid(row=9, column=0, sticky="e", **pad)
    post_edit_toggle_key_var = tk.StringVar(
        master=win,
        value=config.get("post_edit_toggle_key", DEFAULT_CONFIG["post_edit_toggle_key"]),
    )
    tk.Entry(LLM, textvariable=post_edit_toggle_key_var, width=8).grid(row=9, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Post-edit profile:").grid(row=10, column=0, sticky="e", **pad)
    prompt_profile_var = tk.StringVar(master=win, value=POST_EDIT_PROFILES[0])
    prompt_profile_combo = ttk.Combobox(
        LLM,
        textvariable=prompt_profile_var,
        values=POST_EDIT_PROFILES,
        width=12,
        state="readonly",
    )
    prompt_profile_combo.grid(row=10, column=1, sticky="w", **pad)

    tk.Label(LLM, text="Profile prompt file:").grid(row=11, column=0, sticky="ne", **pad)
    prompt_profile_path_var = tk.StringVar(master=win, value=str(_prompt_markdown_path(config, POST_EDIT_PROFILES[0])))
    tk.Label(
        LLM,
        textvariable=prompt_profile_path_var,
        anchor="w",
        justify="left",
        wraplength=520,
        fg="grey",
    ).grid(row=11, column=1, sticky="w", **pad)

    tk.Label(LLM, text="System prompt:").grid(row=12, column=0, sticky="ne", **pad)
    system_prompt_text = _make_scrolled_text(LLM, row=12, height=4)
    system_prompt_text.insert("1.0", config.get("openai_system_prompt", DEFAULT_CONFIG["openai_system_prompt"]))

    tk.Label(LLM, text="Developer prompt:").grid(row=13, column=0, sticky="ne", **pad)
    developer_prompt_text = _make_scrolled_text(LLM, row=13, height=6)
    developer_prompt_text.insert("1.0", config.get("openai_developer_prompt", DEFAULT_CONFIG["openai_developer_prompt"]))

    tk.Label(LLM, text="User prompt template:").grid(row=14, column=0, sticky="ne", **pad)
    user_prompt_text = _make_scrolled_text(LLM, row=14, height=8)
    user_prompt_text.insert("1.0", config.get("openai_user_prompt_template", DEFAULT_CONFIG["openai_user_prompt_template"]))

    tk.Label(LLM, text="Post-edit provider:").grid(row=15, column=0, sticky="e", **pad)
    post_edit_provider_var = tk.StringVar(
        master=win,
        value=config.get("post_edit_provider", DEFAULT_CONFIG["post_edit_provider"]),
    )
    ttk.Combobox(
        LLM,
        textvariable=post_edit_provider_var,
        values=("openai", "external"),
        width=12,
        state="readonly",
    ).grid(row=15, column=1, sticky="w", **pad)

    tk.Label(LLM, text="External URL:").grid(row=16, column=0, sticky="e", **pad)
    external_post_edit_url_var = tk.StringVar(
        master=win,
        value=config.get("external_post_edit_url", DEFAULT_CONFIG["external_post_edit_url"]),
    )
    tk.Entry(LLM, textvariable=external_post_edit_url_var, width=62).grid(row=16, column=1, sticky="ew", **pad)

    prompt_sections_by_profile = {
        profile: _load_prompt_markdown(config, profile)
        for profile in POST_EDIT_PROFILES
    }
    active_prompt_profile = [POST_EDIT_PROFILES[0]]

    def _prompt_fields_to_sections() -> dict[str, str]:
        return {
            "system": system_prompt_text.get("1.0", "end").strip(),
            "developer": developer_prompt_text.get("1.0", "end").strip(),
            "user": user_prompt_text.get("1.0", "end").strip(),
        }

    def _store_active_prompt_profile():
        prompt_sections_by_profile[active_prompt_profile[0]] = _prompt_fields_to_sections()

    def _load_prompt_profile(profile: str):
        profile = _post_edit_profile_name(profile) or POST_EDIT_PROFILES[0]
        sections = prompt_sections_by_profile.get(profile) or _load_prompt_markdown(config, profile)
        for widget, key in (
            (system_prompt_text, "system"),
            (developer_prompt_text, "developer"),
            (user_prompt_text, "user"),
        ):
            widget.delete("1.0", "end")
            widget.insert("1.0", sections.get(key, ""))
        prompt_profile_path_var.set(str(_prompt_markdown_path(config, profile)))
        active_prompt_profile[0] = profile

    def _on_prompt_profile_changed(_event=None):
        _store_active_prompt_profile()
        _load_prompt_profile(prompt_profile_var.get())

    prompt_profile_combo.bind("<<ComboboxSelected>>", _on_prompt_profile_changed)
    _load_prompt_profile(POST_EDIT_PROFILES[0])

    # ════════════════════════════════════════════════════════════════════════
    # Microphone Test tab
    # ════════════════════════════════════════════════════════════════════════
    MT.columnconfigure(0, weight=1)

    meter     = tk.Canvas(MT, height=18, bg="#e5e7eb",
                          highlightthickness=1, highlightbackground="#9ca3af")
    meter.grid(row=0, column=0, sticky="ew", pady=(0, 2))
    meter_bar = meter.create_rectangle(0, 0, 0, 18, fill="#16a34a", outline="")
    db_label  = tk.Label(MT, text="-- dB", width=7, anchor="e", font=("Courier", 9))
    db_label.grid(row=0, column=1, padx=(6, 0))

    # Min / max RMS display (for VAD threshold calibration)
    stats_row = tk.Frame(MT)
    stats_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
    tk.Label(stats_row, text="Floor (min RMS):", font=("Segoe UI", 8), fg="grey").pack(side="left")
    floor_label = tk.Label(stats_row, text="---", width=6, font=("Courier", 9), fg="#2563eb")
    floor_label.pack(side="left", padx=(2, 10))
    tk.Label(stats_row, text="Peak (max RMS):", font=("Segoe UI", 8), fg="grey").pack(side="left")
    peak_label  = tk.Label(stats_row, text="---", width=6, font=("Courier", 9), fg="#dc2626")
    peak_label.pack(side="left", padx=(2, 10))
    _mon_rms_min = [float("inf")]
    _mon_rms_max = [0.0]
    def _reset_stats():
        _mon_rms_min[0] = float("inf")
        _mon_rms_max[0] = 0.0
        floor_label.config(text="---")
        peak_label.config(text="---")
    tk.Button(stats_row, text="Reset", command=_reset_stats,
              font=("Segoe UI", 8), pady=0).pack(side="left")

    btn_row   = tk.Frame(MT)
    btn_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 0))

    _mon_active = [False]
    _test_audio = [None]
    _test_audio_sr = [16000]

    def _selected_device():
        sel = mic_var.get()
        return None if sel == "(system default)" else int(sel.split(":")[0])

    def _selected_device_sample_rate() -> int:
        return _device_sample_rate(_selected_device()) or int(config.get("sample_rate", 16000))

    def _update_meter(pct, db, rms):
        w     = meter.winfo_width() or 300
        color = "#16a34a" if pct < 60 else "#f59e0b" if pct < 85 else "#dc2626"
        meter.coords(meter_bar, 0, 0, int(w * pct / 100), 18)
        meter.itemconfig(meter_bar, fill=color)
        db_label.config(text=f"{db:+.0f} dB")
        if rms < _mon_rms_min[0]:
            _mon_rms_min[0] = rms
            floor_label.config(text=f"{rms:.0f}")
        if rms > _mon_rms_max[0]:
            _mon_rms_max[0] = rms
            peak_label.config(text=f"{rms:.0f}")

    def _monitor_loop():
        try:
            native_sr = _selected_device_sample_rate()
            print(f"[MIC TEST] Monitor using {native_sr} Hz.", flush=True)
            with sd.InputStream(device=_selected_device(), samplerate=native_sr,
                                channels=1, dtype="int16", blocksize=512) as stream:
                while _mon_active[0]:
                    data, _ = stream.read(512)
                    rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
                    db  = 20 * np.log10(max(rms, 1.0) / 32768.0)
                    pct = max(0.0, min(100.0, (db + 60) / 60 * 100))
                    win.after(0, lambda p=pct, d=db, r=rms: _update_meter(p, d, r))
        except Exception as exc:
            msg = "WDM-KS error - pick MME/WASAPI" if "-9999" in str(exc) else "open failed"
            win.after(0, lambda m=msg: db_label.config(text=m))
            win.after(0, lambda: mon_btn.config(text="Monitor"))
            _mon_active[0] = False
            print(f"[MIC TEST] {exc}", flush=True)

    def _stop_monitor():
        _mon_active[0] = False
        mon_btn.config(text="Monitor")
        win.after(250, lambda: meter.coords(meter_bar, 0, 0, 0, 18))
        win.after(250, lambda: db_label.config(text="-- dB"))

    def _toggle_monitor():
        if _mon_active[0]:
            _stop_monitor()
        else:
            _reset_stats()
            _mon_active[0] = True
            mon_btn.config(text="Stop")
            threading.Thread(target=_monitor_loop, daemon=True).start()

    def _record_test():
        _stop_monitor()
        rec_btn.config(state="disabled")
        play_btn.config(state="disabled")
        def _do():
            _test_audio[0] = None
            try:
                native_sr = _selected_device_sample_rate()
                _test_audio_sr[0] = native_sr
                print(f"[MIC TEST] Recording test using {native_sr} Hz.", flush=True)
                for remaining in range(3, 0, -1):
                    win.after(0, lambda n=remaining: rec_btn.config(
                        text=f"Recording... {n}s", state="disabled"))
                    chunk = sd.rec(native_sr, samplerate=native_sr, channels=1,
                                   dtype="int16", device=_selected_device())
                    sd.wait()
                    rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
                    db  = 20 * np.log10(max(rms, 1.0) / 32768.0)
                    pct = max(0.0, min(100.0, (db + 60) / 60 * 100))
                    win.after(0, lambda p=pct, d=db, r=rms: _update_meter(p, d, r))
                    _test_audio[0] = chunk if _test_audio[0] is None else np.concatenate([_test_audio[0], chunk])
            except Exception as exc:
                hint = " (WDM-KS)" if "-9999" in str(exc) else ""
                print(f"[MIC TEST] record error: {exc}", flush=True)
                win.after(0, lambda h=hint: rec_btn.config(text=f"Error{h}", state="normal"))
                return
            win.after(0, lambda: (rec_btn.config(text="Record 3s", state="normal"),
                                  play_btn.config(state="normal")))
        threading.Thread(target=_do, daemon=True).start()

    def _play_test():
        if _test_audio[0] is not None:
            sd.play(_test_audio[0], samplerate=_test_audio_sr[0])

    mon_btn  = tk.Button(btn_row, text="Monitor",   command=_toggle_monitor, width=10)
    rec_btn  = tk.Button(btn_row, text="Record 3s", command=_record_test,    width=10)
    play_btn = tk.Button(btn_row, text="Play back", command=_play_test,      width=10, state="disabled")
    mon_btn.pack(side="left", padx=(0, 4))
    rec_btn.pack(side="left", padx=4)
    play_btn.pack(side="left", padx=4)

    # ════════════════════════════════════════════════════════════════════════
    # Input Classes tab
    # ════════════════════════════════════════════════════════════════════════
    IC.columnconfigure(0, weight=1)

    tk.Label(IC,
             text="Win32 class names where insertion is allowed. Main hotkey auto-selects among Ctrl+V paste, right-click paste, and slow typing.\nRun --focus to find the class name for any window.",
             fg="grey", justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

    tk.Label(IC, text="Allowed input classes:", anchor="w").grid(row=1, column=0, columnspan=2, sticky="w")

    classes_lb = tk.Listbox(IC, height=8, selectmode="single",
                            font=("Consolas", 9), activestyle="none", exportselection=False)
    classes_sb = tk.Scrollbar(IC, orient="vertical", command=classes_lb.yview)
    classes_lb.configure(yscrollcommand=classes_sb.set)
    classes_lb.grid(row=2, column=0, sticky="nsew")
    classes_sb.grid(row=2, column=1, sticky="ns")
    IC.rowconfigure(2, weight=1)

    for cls in sorted(_TEXT_INPUT_CLASSES):
        classes_lb.insert("end", cls)

    add_row = tk.Frame(IC)
    add_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 8))

    new_class_var   = tk.StringVar(master=win)
    new_class_entry = tk.Entry(add_row, textvariable=new_class_var, width=28)
    new_class_entry.pack(side="left")

    def _add_class():
        val = new_class_var.get().strip()
        if not val:
            return
        existing = list(classes_lb.get(0, "end"))
        if val in existing:
            new_class_var.set("")
            return
        pos = next((i for i, x in enumerate(existing) if x > val), "end")
        classes_lb.insert(pos, val)
        new_class_var.set("")

    def _remove_class():
        sel = classes_lb.curselection()
        if sel:
            classes_lb.delete(sel[0])

    new_class_entry.bind("<Return>", lambda e: _add_class())
    tk.Button(add_row, text="Add",            command=_add_class,    width=8).pack(side="left", padx=(6, 4))
    tk.Button(add_row, text="Remove Selected", command=_remove_class, width=14).pack(side="left")

    tk.Label(IC, text="Type-only classes:", anchor="w").grid(row=4, column=0, columnspan=2, sticky="w")

    type_classes_lb = tk.Listbox(IC, height=5, selectmode="single",
                                 font=("Consolas", 9), activestyle="none", exportselection=False)
    type_classes_sb = tk.Scrollbar(IC, orient="vertical", command=type_classes_lb.yview)
    type_classes_lb.configure(yscrollcommand=type_classes_sb.set)
    type_classes_lb.grid(row=5, column=0, sticky="nsew")
    type_classes_sb.grid(row=5, column=1, sticky="ns")
    IC.rowconfigure(5, weight=1)

    for cls in sorted(_TYPE_INPUT_CLASSES):
        type_classes_lb.insert("end", cls)

    type_row = tk.Frame(IC)
    type_row.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

    type_class_var   = tk.StringVar(master=win)
    type_class_entry = tk.Entry(type_row, textvariable=type_class_var, width=28)
    type_class_entry.pack(side="left")

    def _add_type_class():
        val = type_class_var.get().strip()
        if not val:
            return
        existing = list(type_classes_lb.get(0, "end"))
        if val in existing:
            type_class_var.set("")
            return
        pos = next((i for i, x in enumerate(existing) if x > val), "end")
        type_classes_lb.insert(pos, val)
        type_class_var.set("")

    def _remove_type_class():
        sel = type_classes_lb.curselection()
        if sel:
            type_classes_lb.delete(sel[0])

    type_class_entry.bind("<Return>", lambda e: _add_type_class())
    tk.Button(type_row, text="Add",            command=_add_type_class,    width=8).pack(side="left", padx=(6, 4))
    tk.Button(type_row, text="Remove Selected", command=_remove_type_class, width=14).pack(side="left")

    tk.Label(IC, text="Right-click paste classes:", anchor="w").grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))

    right_click_lb = tk.Listbox(IC, height=5, selectmode="single",
                                font=("Consolas", 9), activestyle="none", exportselection=False)
    right_click_sb = tk.Scrollbar(IC, orient="vertical", command=right_click_lb.yview)
    right_click_lb.configure(yscrollcommand=right_click_sb.set)
    right_click_lb.grid(row=8, column=0, sticky="nsew")
    right_click_sb.grid(row=8, column=1, sticky="ns")
    IC.rowconfigure(8, weight=1)

    for cls in sorted(_RIGHT_CLICK_PASTE_INPUT_CLASSES):
        right_click_lb.insert("end", cls)

    right_click_row = tk.Frame(IC)
    right_click_row.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 0))

    right_click_var = tk.StringVar(master=win)
    right_click_entry = tk.Entry(right_click_row, textvariable=right_click_var, width=28)
    right_click_entry.pack(side="left")

    def _add_right_click_class():
        val = right_click_var.get().strip()
        if not val:
            return
        existing = list(right_click_lb.get(0, "end"))
        if val in existing:
            right_click_var.set("")
            return
        pos = next((i for i, x in enumerate(existing) if x > val), "end")
        right_click_lb.insert(pos, val)
        right_click_var.set("")

    def _remove_right_click_class():
        sel = right_click_lb.curselection()
        if sel:
            right_click_lb.delete(sel[0])

    right_click_entry.bind("<Return>", lambda e: _add_right_click_class())
    tk.Button(right_click_row, text="Add",            command=_add_right_click_class,    width=8).pack(side="left", padx=(6, 4))
    tk.Button(right_click_row, text="Remove Selected", command=_remove_right_click_class, width=14).pack(side="left")

    tk.Label(IC, text="Simple-mode classes:", anchor="w").grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))

    simple_mode_lb = tk.Listbox(IC, height=5, selectmode="single",
                                font=("Consolas", 9), activestyle="none", exportselection=False)
    simple_mode_sb = tk.Scrollbar(IC, orient="vertical", command=simple_mode_lb.yview)
    simple_mode_lb.configure(yscrollcommand=simple_mode_sb.set)
    simple_mode_lb.grid(row=11, column=0, sticky="nsew")
    simple_mode_sb.grid(row=11, column=1, sticky="ns")
    IC.rowconfigure(11, weight=1)

    for cls in sorted(_SIMPLE_MODE_INPUT_CLASSES):
        simple_mode_lb.insert("end", cls)

    simple_mode_row = tk.Frame(IC)
    simple_mode_row.grid(row=12, column=0, columnspan=2, sticky="w", pady=(6, 0))

    simple_mode_var = tk.StringVar(master=win)
    simple_mode_entry = tk.Entry(simple_mode_row, textvariable=simple_mode_var, width=28)
    simple_mode_entry.pack(side="left")

    def _add_simple_mode_class():
        val = simple_mode_var.get().strip()
        if not val:
            return
        existing = list(simple_mode_lb.get(0, "end"))
        if val in existing:
            simple_mode_var.set("")
            return
        pos = next((i for i, x in enumerate(existing) if x > val), "end")
        simple_mode_lb.insert(pos, val)
        simple_mode_var.set("")

    def _remove_simple_mode_class():
        sel = simple_mode_lb.curselection()
        if sel:
            simple_mode_lb.delete(sel[0])

    simple_mode_entry.bind("<Return>", lambda e: _add_simple_mode_class())
    tk.Button(simple_mode_row, text="Add",            command=_add_simple_mode_class,    width=8).pack(side="left", padx=(6, 4))
    tk.Button(simple_mode_row, text="Remove Selected", command=_remove_simple_mode_class, width=14).pack(side="left")

    tk.Label(IC, text="Live-transcription classes:", anchor="w").grid(row=13, column=0, columnspan=2, sticky="w", pady=(10, 0))

    live_mode_lb = tk.Listbox(IC, height=5, selectmode="single",
                              font=("Consolas", 9), activestyle="none", exportselection=False)
    live_mode_sb = tk.Scrollbar(IC, orient="vertical", command=live_mode_lb.yview)
    live_mode_lb.configure(yscrollcommand=live_mode_sb.set)
    live_mode_lb.grid(row=14, column=0, sticky="nsew")
    live_mode_sb.grid(row=14, column=1, sticky="ns")
    IC.rowconfigure(14, weight=1)

    for cls in sorted(_LIVE_MODE_INPUT_CLASSES):
        live_mode_lb.insert("end", cls)

    live_mode_row = tk.Frame(IC)
    live_mode_row.grid(row=15, column=0, columnspan=2, sticky="w", pady=(6, 0))

    live_mode_var = tk.StringVar(master=win)
    live_mode_entry = tk.Entry(live_mode_row, textvariable=live_mode_var, width=28)
    live_mode_entry.pack(side="left")

    def _add_live_mode_class():
        val = live_mode_var.get().strip()
        if not val:
            return
        existing = list(live_mode_lb.get(0, "end"))
        if val in existing:
            live_mode_var.set("")
            return
        pos = next((i for i, x in enumerate(existing) if x > val), "end")
        live_mode_lb.insert(pos, val)
        live_mode_var.set("")

    def _remove_live_mode_class():
        sel = live_mode_lb.curselection()
        if sel:
            live_mode_lb.delete(sel[0])

    live_mode_entry.bind("<Return>", lambda e: _add_live_mode_class())
    tk.Button(live_mode_row, text="Add",            command=_add_live_mode_class,    width=8).pack(side="left", padx=(6, 4))
    tk.Button(live_mode_row, text="Remove Selected", command=_remove_live_mode_class, width=14).pack(side="left")

    # ════════════════════════════════════════════════════════════════════════
    # Recent Transcriptions (always visible, below the notebook)
    # ════════════════════════════════════════════════════════════════════════
    hist_frame = tk.LabelFrame(MIC, text=" Recent Transcriptions ", padx=8, pady=6)
    hist_frame.grid(row=2, column=0, sticky="ew", padx=4, pady=(8, 0))
    hist_frame.columnconfigure(0, weight=1)

    def _build_history():
        for child in hist_frame.winfo_children():
            child.destroy()

        recent = list(reversed(list(_history)))[:5]
        if not recent:
            tk.Label(hist_frame, text="No transcriptions yet.", fg="grey").pack(anchor="w")
            return

        for entry in recent:
            row_f = tk.Frame(hist_frame)
            row_f.pack(fill="x", pady=2)
            row_f.columnconfigure(0, weight=1)

            ts_str = entry.get("time", "")
            text_value = (entry.get("text") or "").strip()
            error_value = (entry.get("error") or "").strip()

            if entry.get("wav"):
                # ── Failed entry ───────────────────────────────────────────
                error_text = entry.get("error", "Transcription failed")
                wav_path   = entry["wav"]
                wav_name   = Path(wav_path).name

                tk.Label(row_f, text=error_text, fg="#dc2626", anchor="w",
                         font=("Segoe UI", 9)).grid(row=0, column=0, sticky="ew")

                retry_btn = tk.Button(row_f, text="Retry", width=6)
                retry_btn.grid(row=0, column=1, padx=(6, 0))

                tk.Label(row_f, text=f"{ts_str}  |  {wav_name}", fg="grey",
                         font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", padx=2)

                def _make_retry(e, btn):
                    def _do_retry():
                        text, err = retry_transcription(e["wav"], e["time"], config)
                        if text:
                            win.after(0, _build_history)
                        else:
                            win.after(0, lambda: btn.config(
                                text="Retry", state="normal",
                                fg="#dc2626" if err else "black"))
                            if err:
                                win.after(0, lambda: btn.config(text="Retry"))
                    def _start():
                        btn.config(state="disabled", text="...")
                        threading.Thread(target=_do_retry, daemon=True).start()
                    return _start

                retry_btn.config(command=_make_retry(entry, retry_btn))

            elif text_value:
                # ── Success entry ──────────────────────────────────────────
                display = text_value if len(text_value) <= 72 else text_value[:69] + "..."
                tk.Label(row_f, text=display, anchor="w", justify="left",
                         font=("Segoe UI", 9), wraplength=400).grid(row=0, column=0, sticky="ew")

                def _make_copy(t):
                    def _copy():
                        win.clipboard_clear()
                        win.clipboard_append(t)
                    return _copy

                tk.Button(row_f, text="Copy", command=_make_copy(text_value),
                          width=6).grid(row=0, column=1, padx=(6, 0))
                tk.Label(row_f, text=ts_str, fg="grey",
                         font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", padx=2)
            else:
                tk.Label(row_f, text=error_value or "(empty history entry)", anchor="w", justify="left",
                         fg="#6b7280", font=("Segoe UI", 9), wraplength=400).grid(row=0, column=0, sticky="ew")
                tk.Label(row_f, text=ts_str, fg="grey",
                         font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", padx=2)

    _build_history()
    win.after(0, _fit_to_current_tab)

    # ── Save button ───────────────────────────────────────────────────────────
    save_notice_var = tk.StringVar(master=win, value="")

    def on_save():
        _add_class()
        _mon_active[0] = False
        new_cfg = dict(config)
        new_cfg["transcription_backend"] = transcription_backend_var.get().strip() or DEFAULT_CONFIG["transcription_backend"]
        new_cfg["server_url"] = url_var.get().strip()
        new_cfg["language"]   = lang_var.get().strip() or None
        sel = mic_var.get()
        new_cfg["microphone_index"] = (
            None if sel == "(system default)" else int(sel.split(":")[0])
        )
        if new_cfg["transcription_backend"] == "http" and not new_cfg["server_url"]:
            messagebox.showerror("Error", "Server URL cannot be empty.")
            return
        hotkey = hotkey_var.get().strip()
        ok, msg = validate_hotkey(hotkey)
        if not ok:
            messagebox.showerror("Invalid Hotkey", msg)
            return
        new_cfg["hotkey"] = hotkey
        fast_hotkey = fast_var.get().strip()
        if fast_hotkey:
            ok, msg = validate_hotkey(fast_hotkey)
            if not ok:
                messagebox.showerror("Invalid Fast Hotkey", msg)
                return
        new_cfg["fast_hotkey"] = fast_hotkey
        undo = undo_var.get().strip()
        if undo:
            ok, msg = validate_hotkey(undo)
            if not ok:
                messagebox.showerror("Invalid Undo Hotkey", msg)
                return
        new_cfg["undo_hotkey"] = undo
        combos = [c for c in (hotkey, fast_hotkey, undo) if c]
        if len({c.lower() for c in combos}) != len(combos):
            messagebox.showerror("Duplicate Hotkeys", "Hotkey, fast hotkey, and last-result hotkey must be different.")
            return
        try:
            new_cfg["vad_silence_rms"]  = float(vad_rms_var.get())
            new_cfg["vad_silence_secs"] = float(vad_secs_var.get())
            new_cfg["vad_min_speech_s"] = float(vad_speech_var.get())
            new_cfg["vad_hangover_s"]   = float(vad_hang_var.get())
            new_cfg["vad_max_chunk_s"]  = float(vad_max_var.get())
        except ValueError:
            messagebox.showerror("Invalid VAD value", "VAD fields must be numbers.")
            return
        new_cfg["input_classes"] = list(classes_lb.get(0, "end"))
        new_cfg["type_input_classes"] = list(type_classes_lb.get(0, "end"))
        new_cfg["right_click_paste_input_classes"] = list(right_click_lb.get(0, "end"))
        new_cfg["simple_mode_input_classes"] = list(simple_mode_lb.get(0, "end"))
        new_cfg["live_mode_input_classes"] = list(live_mode_lb.get(0, "end"))
        new_cfg["main_mode"] = main_mode_var.get().strip().lower() or DEFAULT_CONFIG["main_mode"]
        new_cfg["main_live_mode"] = new_cfg["main_mode"] == "live"
        new_cfg["main_simple_mode"] = bool(main_simple_var.get())
        new_cfg["main_post_edit_mode"] = False
        new_cfg["fast_mode"] = fast_mode_var.get().strip().lower() or DEFAULT_CONFIG["fast_mode"]
        new_cfg["fast_live_mode"] = new_cfg["fast_mode"] == "live"
        new_cfg["fast_simple_mode"] = bool(fast_simple_var.get())
        new_cfg["fast_post_edit_mode"] = False
        new_cfg["main_review_non_post_edit_sessions"] = bool(main_review_non_post_edit_var.get())
        new_cfg["fast_review_non_post_edit_sessions"] = bool(fast_review_non_post_edit_var.get())
        try:
            new_cfg["preview_max_width"] = max(500, int(preview_max_width_var.get().strip() or DEFAULT_CONFIG["preview_max_width"]))
        except Exception:
            new_cfg["preview_max_width"] = DEFAULT_CONFIG["preview_max_width"]
        try:
            new_cfg["preview_max_height"] = max(220, int(preview_max_height_var.get().strip() or DEFAULT_CONFIG["preview_max_height"]))
        except Exception:
            new_cfg["preview_max_height"] = DEFAULT_CONFIG["preview_max_height"]
        new_cfg["duck_audio_during_dictation"] = bool(duck_audio_var.get())
        try:
            new_cfg["duck_audio_level_percent"] = max(0, min(100, int(duck_audio_level_var.get().strip() or DEFAULT_CONFIG["duck_audio_level_percent"])))
        except Exception:
            new_cfg["duck_audio_level_percent"] = DEFAULT_CONFIG["duck_audio_level_percent"]
        new_cfg["post_edit_provider"] = post_edit_provider_var.get().strip().lower() or DEFAULT_CONFIG["post_edit_provider"]
        new_cfg["external_post_edit_url"] = (
            external_post_edit_url_var.get().strip() or DEFAULT_CONFIG["external_post_edit_url"]
        )
        new_cfg["openai_model"] = openai_model_var.get().strip() or DEFAULT_CONFIG["openai_model"]
        new_cfg["openai_transcription_model"] = (
            openai_transcription_model_var.get().strip() or DEFAULT_CONFIG["openai_transcription_model"]
        )
        new_cfg["openai_transcription_prompt"] = (
            openai_transcription_prompt_text.get("1.0", "end").strip() or DEFAULT_CONFIG["openai_transcription_prompt"]
        )
        new_cfg["openai_reasoning_effort"] = reasoning_var.get().strip() or DEFAULT_CONFIG["openai_reasoning_effort"]
        try:
            new_cfg["editor_notes_overlay_seconds"] = max(0.5, float(editor_notes_overlay_var.get().strip() or DEFAULT_CONFIG["editor_notes_overlay_seconds"]))
        except Exception:
            new_cfg["editor_notes_overlay_seconds"] = DEFAULT_CONFIG["editor_notes_overlay_seconds"]
        try:
            new_cfg["editor_notes_chars_per_extra_second"] = max(1.0, float(editor_notes_scale_var.get().strip() or DEFAULT_CONFIG["editor_notes_chars_per_extra_second"]))
        except Exception:
            new_cfg["editor_notes_chars_per_extra_second"] = DEFAULT_CONFIG["editor_notes_chars_per_extra_second"]
        new_cfg["post_edit_toggle_key"] = post_edit_toggle_key_var.get().strip().lower() or DEFAULT_CONFIG["post_edit_toggle_key"]
        _store_active_prompt_profile()
        for prompt_profile, prompt_sections in prompt_sections_by_profile.items():
            _save_prompt_markdown(new_cfg, prompt_profile, prompt_sections)
        dev_prompt_sections = prompt_sections_by_profile.get("dev") or _load_prompt_markdown(new_cfg, "dev")
        new_cfg["openai_system_prompt"] = dev_prompt_sections.get("system") or DEFAULT_CONFIG["openai_system_prompt"]
        new_cfg["openai_developer_prompt"] = dev_prompt_sections.get("developer") or DEFAULT_CONFIG["openai_developer_prompt"]
        new_cfg["openai_user_prompt_template"] = dev_prompt_sections.get("user") or DEFAULT_CONFIG["openai_user_prompt_template"]
        new_cfg["live_mode"]   = new_cfg["main_live_mode"]
        new_cfg["simple_mode"] = new_cfg["main_simple_mode"]
        _current_config[0] = new_cfg
        sync_input_classes(new_cfg)
        save_config(new_cfg)
        on_save_callback(new_cfg)
        win.destroy()

    tk.Button(outer, text="Save", command=on_save, width=14).pack(pady=(8, 0))
    tk.Label(outer, textvariable=save_notice_var, fg="#b45309").pack(pady=(6, 0))

    # ── Close handling ────────────────────────────────────────────────────────
    def _on_close():
        _mon_active[0] = False
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", _on_close)

    win.mainloop()


# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_icon(color: tuple[int, int, int] = (34, 197, 94), badge_text: str | None = None):
    from PIL import Image, ImageDraw, ImageFont
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(*color, 255))
    if badge_text:
        try:
            font = ImageFont.truetype("arial.ttf", 34)
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), badge_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        draw.text(
            ((64 - text_w) / 2, (64 - text_h) / 2 - 2),
            badge_text,
            fill=(255, 255, 255, 255),
            font=font,
            stroke_width=2,
            stroke_fill=(0, 0, 0, 160),
        )
    return img


_ICON_IDLE        = (34,  197,  94)   # green
_ICON_RECORDING   = (239,  68,  68)   # red
_ICON_TRANSCODING = (234, 179,   8)   # yellow
_ICON_FAST        = (8, 145, 178)     # cyan


def run_tray(config_holder: list[dict]):
    """
    Run the system-tray icon.  config_holder is a one-element list so that
    the settings dialog can update the shared config in-place.
    """
    import pystray
    global _native_hotkeys

    tray_icon: pystray.Icon | None = None
    helper_status = {
        "state": "starting",
        "detail": "",
    }

    def _force_exit_after_delay(delay_s: float = 1.5):
        def _go():
            time.sleep(delay_s)
            if _shutdown_requested.is_set():
                print("[TRAY] Forcing process exit after quit timeout.", flush=True)
                os._exit(0)
        threading.Thread(target=_go, daemon=True).start()

    def get_cfg() -> dict:
        return config_holder[0]

    def on_settings(icon, item):
        def _open():
            try:
                def on_saved(new_cfg):
                    old_cfg = config_holder[0]
                    config_holder[0] = new_cfg
                    hotkey_keys = {
                        "hotkey", "fast_hotkey", "undo_hotkey",
                        "main_live_mode", "main_simple_mode",
                        "fast_live_mode", "fast_simple_mode",
                    }
                    if any(old_cfg.get(k) != new_cfg.get(k) for k in hotkey_keys):
                        _bind_hotkeys()
                open_settings(get_cfg(), on_saved)
            except Exception as exc:
                print(f"[TRAY] Failed to open settings: {_safe_console_text(exc)}", flush=True)
        threading.Thread(target=_open, daemon=True).start()

    devices = list_input_devices()
    wasapi_devices = [(i, n, api) for i, n, api in devices if api == "Windows WASAPI"]

    def _dev_label(i, n, api):
        return f"{i}: {n}  [{api}{'  (!) WDM-KS' if api not in _PREFERRED_APIS else ''}]"

    def _select_microphone(index: int | None):
        cfg = dict(get_cfg())
        cfg["microphone_index"] = index
        config_holder[0] = cfg
        save_config(cfg)

    def _make_mic_handler(index: int | None):
        def _handler(icon, item):
            _select_microphone(index)
        return _handler

    def _mic_checked(index: int | None):
        def _checked(item):
            return get_cfg().get("microphone_index") == index
        return _checked

    def on_copy_last(icon, item):
        text = _last_result_text[0] or _latest_history_text() or None
        if not text:
            return
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()   # keep clipboard alive after destroy on some systems
        root.after(500, root.destroy)
        root.mainloop()

    def _has_last(item) -> bool:
        return bool(_last_result_text[0] or _latest_history_text())

    def _helper_status_text(_item=None) -> str:
        bridge = _native_hotkeys
        if bridge is not None and bridge.is_running():
            return "KB helper OK"
        if bridge is not None and bridge.last_error:
            return f"KB helper N/A: {bridge.last_error}"
        state = helper_status.get("state")
        detail = helper_status.get("detail", "")
        if state == "ok":
            return "KB helper OK"
        if state == "starting":
            return "KB helper starting"
        return f"KB helper N/A: {detail}" if detail else "KB helper N/A"

    def _set_helper_status(state: str, detail: str = ""):
        helper_status["state"] = state
        helper_status["detail"] = detail
        if tray_icon is None:
            return
        try:
            if state == "ok":
                tray_icon.title = "OverMultiASRSuite (idle)"
                tray_icon.icon = _make_icon(_ICON_IDLE)
            elif state == "starting":
                tray_icon.title = "OverMultiASRSuite (keyboard helper starting)"
                tray_icon.icon = _make_icon(_ICON_TRANSCODING, badge_text="K")
            else:
                tray_icon.title = f"OverMultiASRSuite ({_helper_status_text()})"
                tray_icon.icon = _make_icon(_ICON_TRANSCODING, badge_text="!")
            tray_icon.update_menu()
        except Exception:
            pass

    def on_quit(icon, item):
        global _native_hotkeys
        _shutdown_requested.set()
        if _native_hotkeys is not None:
            _native_hotkeys.stop()
            _native_hotkeys = None
        else:
            keyboard.unhook_all()
        try:
            icon.visible = False
        except Exception:
            pass
        _force_exit_after_delay()
        icon.stop()

    microphone_menu = pystray.Menu(
        pystray.MenuItem(
            "(system default)",
            _make_mic_handler(None),
            checked=_mic_checked(None),
            radio=True,
        ),
        *[
            pystray.MenuItem(
                _dev_label(i, n, api),
                _make_mic_handler(i),
                checked=_mic_checked(i),
                radio=True,
            )
            for i, n, api in wasapi_devices
        ],
    )

    menu = pystray.Menu(
        pystray.MenuItem("Copy last result",  on_copy_last, enabled=_has_last),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Microphone",        microphone_menu),
        pystray.MenuItem("Settings…",         on_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_helper_status_text, lambda icon, item: None, enabled=False),
        pystray.MenuItem("Quit",              on_quit),
    )

    icon_img = _make_icon(_ICON_IDLE)
    tray_icon = pystray.Icon("overmultiasrsuite", icon_img, "OverMultiASRSuite (idle)", menu)

    # _on_hotkey runs on the keyboard library's internal hook thread.
    # Never touch pystray icon handles here — Win32 DestroyIcon crashes across threads.
    # Icon updates happen inside _session (a plain daemon thread) instead.
    def _on_hotkey(hotkey_name: str, insert_mode: str, profile: str):
        pending_draft = _peek_pending_draft()
        if pending_draft:
            tap_deadline = time.time() + _DRAFT_TAP_RELEASE_MAX
            while time.time() < tap_deadline and _hotkey_is_down_now(hotkey_name, get_cfg()):
                time.sleep(0.01)
            if _hotkey_is_down_now(hotkey_name, get_cfg()):
                print("[DRAFT] Starting a fresh session and clearing the pending preview.", flush=True)
                _clear_pending_draft()
            else:
                if pending_draft.get("review_open"):
                    print("[DRAFT] Review window is open. Copy from there if you want a manual paste.", flush=True)
                    return
                action = _register_pending_draft_tap(get_cfg)
                if action == "insert_notes":
                    latest_draft = _peek_pending_draft()
                    if latest_draft and not latest_draft.get("post_edit"):
                        print("[DRAFT] Quick modifier gesture requested an edit pass.", flush=True)
                        _start_pending_draft_post_edit(get_cfg)
                    else:
                        print("[DRAFT] Quick insert with editor notes requested.", flush=True)
                        _insert_pending_draft(get_cfg(), include_notes=True)
                elif action == "double":
                    print("[DRAFT] Opening review window for the pending preview.", flush=True)
                    _set_pending_draft_review(True)
                else:
                    print("[DRAFT] Pending preview tap registered.", flush=True)
                return

        pending_notes = _peek_pending_editor_notes()
        if pending_notes:
            tap_deadline = time.time() + 0.28
            while time.time() < tap_deadline and _hotkey_is_down_now(hotkey_name, get_cfg()):
                time.sleep(0.01)
            if not _hotkey_is_down_now(hotkey_name, get_cfg()):
                if not is_text_input_focused():
                    print("[POST] Editor notes available, but focused control is not a text input.", flush=True)
                    return
                notes_to_insert = _consume_pending_editor_notes()
                if notes_to_insert:
                    note_block = _format_editor_notes_for_insert(notes_to_insert)
                    note_insert_mode = choose_insert_mode()
                    print(f"[POST] Appending editor notes via {note_insert_mode}.", flush=True)
                    _release_possible_modifiers()
                    if note_insert_mode == "type":
                        type_text(note_block, char_delay=get_cfg().get("char_delay", 0.0))
                    else:
                        paste_text(note_block, method=note_insert_mode, source="editor_notes_append")
                    _last_typed[0] = (_last_typed[0] or "") + note_block
                return
        if _session_lock.locked():
            return  # Already recording — silently drop the repeat trigger
        print(f"[HOTKEY] Triggered ({profile}/{insert_mode}) — starting session.", flush=True)
        threading.Thread(target=_session, args=(hotkey_name, insert_mode, profile), daemon=True).start()

    def _session(hotkey_name: str, insert_mode: str, profile: str):
        active_icon = _ICON_FAST if profile == "fast" else _ICON_RECORDING
        active_label = "fast mode" if profile == "fast" else "recording"

        def _update_active_icon(post_edit_enabled):
            edit_profile = _post_edit_profile_name(post_edit_enabled)
            label = f"{active_label} + {edit_profile}" if edit_profile else active_label
            tray_icon.icon = _make_icon(active_icon, badge_text=(edit_profile[:1].upper() if edit_profile else None))
            tray_icon.title = f"OverMultiASRSuite ({label}...)"

        try:
            _update_active_icon(False)
            tray_icon.title = f"OverMultiASRSuite ({active_label}…)"
            run_session(
                get_cfg(),
                insert_mode=insert_mode,
                profile=profile,
                hotkey_name=hotkey_name,
                on_post_edit_change=_update_active_icon,
            )
        finally:
            _release_possible_modifiers()
            tray_icon.icon  = _make_icon(_ICON_IDLE)
            tray_icon.title = "OverMultiASRSuite (idle)"

    def _bind_hotkeys():
        global _native_hotkeys
        hotkey_str = get_cfg()["hotkey"]
        fast_hotkey_str = get_cfg().get("fast_hotkey", "").strip()
        undo_str = get_cfg().get("undo_hotkey", "").strip()

        keyboard.unhook_all()
        if _native_hotkeys is not None:
            _native_hotkeys.stop()
            _native_hotkeys = None

        _set_helper_status("starting")
        try:
            bridge = NativeHotkeyBridge(hotkey_str, fast_hotkey_str, undo_str)
            bridge.start()
            _native_hotkeys = bridge
            _set_helper_status("ok")
            print(f"[TRAY] Native hotkey registered: {hotkey_str}", flush=True)
            if fast_hotkey_str:
                print(f"[TRAY] Native fast hotkey registered: {fast_hotkey_str}", flush=True)
            if undo_str:
                print(f"[TRAY] Native last-result hotkey registered: {undo_str}", flush=True)

            def _watch_hold(local_bridge=bridge):
                held = {"ptt": False, "fast_ptt": False}
                while (
                    _native_hotkeys is local_bridge
                    and not local_bridge._stop.is_set()
                    and not _shutdown_requested.is_set()
                ):
                    for name, mode, profile in (("ptt", "auto", "main"), ("fast_ptt", "paste_ctrl_v", "fast")):
                        is_down = local_bridge.is_pressed(name)
                        if is_down and not held[name]:
                            _on_hotkey(name, mode, profile)
                        held[name] = is_down
                    time.sleep(0.01)
                if (
                    _native_hotkeys is local_bridge
                    and not local_bridge._stop.is_set()
                    and not _shutdown_requested.is_set()
                    and not local_bridge.is_running()
                ):
                    detail = local_bridge.last_error or "helper process is not running"
                    _set_helper_status("na", detail)

            threading.Thread(target=_watch_hold, daemon=True).start()
        except Exception as exc:
            _native_hotkeys = None
            detail = (
                f"{_safe_console_text(exc)}. "
                "Run build.bat with the .NET 8 SDK installed, or place HotkeyHelper.exe next to the app."
            )
            print(f"[TRAY] Native hotkey helper unavailable: {detail}", flush=True)
            _set_helper_status("na", detail)

    _bind_hotkeys()

    print("[TRAY] Running. Right-click tray icon to open settings or quit.", flush=True)

    tray_icon.run()   # blocks main thread
    print("[TRAY] Tray loop exited.", flush=True)
    if _native_hotkeys is not None:
        _native_hotkeys.stop()
        _native_hotkeys = None
    if _shutdown_requested.is_set():
        raise SystemExit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    save_config(cfg)   # persist defaults on first run
    _load_history()
    sync_input_classes(cfg)
    _current_config[0] = cfg

    print("OverMultiASRSuite", flush=True)
    print(f"  Backend: {cfg.get('transcription_backend', DEFAULT_CONFIG['transcription_backend'])}", flush=True)
    if (cfg.get("transcription_backend") or DEFAULT_CONFIG["transcription_backend"]) == "openai":
        print(f"  Model  : {cfg.get('openai_transcription_model', DEFAULT_CONFIG['openai_transcription_model'])}", flush=True)
    else:
        print(f"  Server : {cfg['server_url']}", flush=True)
    print(f"  Hotkey : {cfg['hotkey']}", flush=True)
    if cfg.get("fast_hotkey"):
        print(f"  Fast   : {cfg['fast_hotkey']}", flush=True)
    print(f"  Mic    : {cfg['microphone_index'] if cfg['microphone_index'] is not None else 'system default'}", flush=True)
    print(f"  Lang   : {cfg['language'] or 'auto'}", flush=True)

    config_holder = [cfg]

    # If --settings flag passed, open settings dialog and exit
    if "--settings" in sys.argv:
        def _noop(c): pass
        open_settings(cfg, _noop)
        return

    # --focus: print the focused control's class name and exit (helps add new apps)
    if "--focus" in sys.argv:
        cls = focused_class()
        ok  = is_text_input_focused()
        print(f"Focused class : {cls!r}")
        print(f"Text input    : {'yes' if ok else 'NO — typing would be skipped'}")
        return

    # --debug-keys: log every key event so you can confirm the hook is alive
    if "--debug-keys" in sys.argv:
        print("Debug mode: logging all key events. Press Ctrl+C to stop.", flush=True)
        bridge = NativeHotkeyBridge(
            cfg["hotkey"],
            cfg.get("fast_hotkey", ""),
            cfg.get("undo_hotkey", ""),
            debug_raw=True,
        )
        bridge.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            bridge.stop()
        return

    # If --list-mics flag passed, print devices and exit
    if "--list-mics" in sys.argv:
        for idx, name, api in list_input_devices():
            warn = "  (!) WDM-KS - may fail" if api not in _PREFERRED_APIS else ""
            print(f"  [{idx:2}] {name}  [{api}]{warn}")
        return

    run_tray(config_holder)


if __name__ == "__main__":
    main()
