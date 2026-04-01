#!/usr/bin/env python3
"""
OverMultiASRSuite for Windows
Hold the hotkey -> records audio -> sends to transcription server -> inserts result.
"""

import collections
import ctypes
import datetime
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import keyboard
import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

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


# When running as a frozen exe there is no console, so redirect prints to a
# rolling log file next to the exe instead.
if getattr(sys, "frozen", False):
    _log = open(_app_dir() / "overmultiasrsuite.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log


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

CONFIG_FILE = _app_dir() / "config.json"

DEFAULT_CONFIG = {
    "server_url": "http://192.168.1.227:8001/transcribe",
    "hotkey": "ctrl+shift+space",
    "fast_hotkey": "",
    "undo_hotkey": "",          # empty = disabled
    "microphone_index": None,   # None = system default
    "language": None,           # None = auto-detect
    "sample_rate": 16000,
    "pre_type_delay": 0.05,
    "char_delay": 0.0,
    "erase_delay":  0.08,  # pause after erasing status before typing result (helps SSH/terminals)
    "main_live_mode": False,
    "main_simple_mode": True,
    "fast_live_mode": False,
    "fast_simple_mode": True,
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
        cfg.setdefault("main_live_mode", cfg.get("live_mode", DEFAULT_CONFIG["main_live_mode"]))
        cfg.setdefault("main_simple_mode", cfg.get("simple_mode", DEFAULT_CONFIG["main_simple_mode"]))
        cfg.setdefault("fast_live_mode", cfg.get("live_mode", DEFAULT_CONFIG["fast_live_mode"]))
        cfg.setdefault("fast_simple_mode", cfg.get("simple_mode", DEFAULT_CONFIG["fast_simple_mode"]))
        return cfg
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ── Transcription history ─────────────────────────────────────────────────────

_HISTORY_FILE = _app_dir() / "history.json"
_history: collections.deque = collections.deque(maxlen=20)


def _load_history():
    if _HISTORY_FILE.exists():
        try:
            items = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            _history.extend(items)
        except Exception:
            pass


def _save_history():
    _HISTORY_FILE.write_text(
        json.dumps(list(_history), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _add_to_history(entry: dict):
    """Append a history entry dict and persist. Callers build the dict."""
    _history.append(entry)
    _save_history()


def _helper_project_dir() -> Path:
    return _app_dir() / "native_hotkey_helper"


def _helper_exe_path() -> Path | None:
    candidates = [
        _app_dir() / "HotkeyHelper.exe",
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


def _helper_command(*args: str) -> list[str]:
    if not getattr(sys, "frozen", False):
        helper_dll = _helper_dll_path()
        if helper_dll is not None:
            return ["dotnet", str(helper_dll), *args]

        project = _helper_project_dir() / "HotkeyHelper.csproj"
        if project.exists():
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
            raise RuntimeError("Hotkey helper did not become ready.")

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
            print(f"[HOTKEY] Helper exited unexpectedly with code {code}.", flush=True)

    def _handle_payload(self, payload: dict):
        kind = payload.get("type")
        if kind == "status" and payload.get("event") == "ready":
            self._ready.set()
            print("[HOTKEY] Native helper ready.", flush=True)
            return

        if kind == "error":
            print(f"[HOTKEY] ERROR: {payload.get('message', 'unknown error')}", flush=True)
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


def paste_text(text: str, method: str = "paste_ctrl_v"):
    """Paste plain text quickly via the clipboard, then restore text clipboard content."""
    previous = _clipboard_get_text()
    restore_delay = 0.05
    try:
        print(f"[PASTE] Preparing {method} with {len(text)} chars.", flush=True)
        _clipboard_set_text(text)
        print("[PASTE] Clipboard populated with outgoing text.", flush=True)
        time.sleep(0.05)
        if method == "paste_right_click":
            print("[PASTE] Sending right-click paste trigger.", flush=True)
            _send_right_click()
            # Console-style right-click paste can consume the clipboard slightly
            # after the click event, so restore later than Ctrl+V paths.
            restore_delay = 0.35
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


def hotkey_mode_settings(config: dict, profile: str) -> tuple[bool, bool]:
    """Return (live_mode, simple_mode) for the given hotkey profile."""
    if profile == "fast":
        return (
            bool(config.get("fast_live_mode", config.get("live_mode", False))),
            bool(config.get("fast_simple_mode", config.get("simple_mode", True))),
        )
    return (
        bool(config.get("main_live_mode", config.get("live_mode", False))),
        bool(config.get("main_simple_mode", config.get("simple_mode", True))),
    )


def session_mode_settings(config: dict, insert_mode: str, profile: str) -> tuple[bool, bool]:
    """Return (live_mode, simple_mode) using hotkey defaults plus focused-class overrides."""
    live_mode, simple_mode = hotkey_mode_settings(config, profile)
    cls = focused_class()

    if cls in _SIMPLE_MODE_INPUT_CLASSES:
        simple_mode = True

    if insert_mode == "type" and cls in _LIVE_MODE_INPUT_CLASSES:
        live_mode = True
        simple_mode = False

    return live_mode, simple_mode


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
    if from_sr == to_sr:
        return audio
    flat  = audio.flatten().astype(np.float64)
    n_out = max(1, int(round(len(flat) * to_sr / from_sr)))
    out   = np.interp(
        np.linspace(0, len(flat) - 1, n_out),
        np.arange(len(flat)),
        flat,
    )
    return np.clip(out, -32768, 32767).astype(np.int16).reshape(-1, 1)


def _record_with_vad(config: dict, on_chunk_ready=None, hotkey_name: str = "ptt"):
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
        print(f"[REC] Device native {native_sr} Hz → resampling to {target_sr} Hz", flush=True)

    BLOCK        = 512
    SIL_BLKS     = max(1, int(sil_secs   * native_sr / BLOCK))
    SPEECH_BLKS  = max(1, int(min_speech * native_sr / BLOCK))
    MIN_BLKS     = max(1, int(0.3        * native_sr / BLOCK))
    # Hangover: how long to stay in "speech" state after the last loud block.
    # Bridges natural inter-word gaps so they don't restart the silence counter.
    HANGOVER_BLKS = max(1, int(config.get("vad_hangover_s", 0.3) * native_sr / BLOCK))

    pending:    list[tuple[list, threading.Event]] = []
    all_data:   list[np.ndarray] = []
    current:    list[np.ndarray] = []
    spk         = 0          # total blocks since last chunk reset
    chunk_peak  = 0.0        # peak RMS in current (unsent) chunk
    rms_min     = float("inf")
    rms_max     = 0.0
    # Hysteresis state machine
    in_speech   = False      # True while voice (or hangover) is active
    hangover    = 0          # remaining hangover blocks
    sil_count   = 0          # consecutive silence blocks after hangover expires

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

    try:
        with sd.InputStream(device=device, samplerate=native_sr, channels=1,
                            dtype="int16", blocksize=BLOCK) as stream:
            print("[REC] Recording (VAD)...", flush=True)
            while (_native_hotkeys.is_pressed(hotkey_name) if _native_hotkeys else keyboard.is_pressed(config["hotkey"])):
                data, _ = stream.read(BLOCK)
                blk = data.copy()
                all_data.append(blk)
                current.append(blk)
                rms = float(np.sqrt(np.mean(blk.astype(np.float64) ** 2)))
                if rms < rms_min: rms_min = rms
                if rms > rms_max: rms_max = rms
                spk += 1

                if rms >= sil_rms:
                    # Loud block — enter / stay in speech; reset hangover & silence
                    chunk_peak = max(chunk_peak, rms)
                    in_speech  = True
                    hangover   = HANGOVER_BLKS
                    sil_count  = 0
                else:
                    if hangover > 0:
                        # Within hangover window — treat as speech, don't count silence
                        hangover  -= 1
                        sil_count  = 0
                    else:
                        # Genuinely silent
                        in_speech = False
                        sil_count += 1

                if config.get("debug"):
                    print(f"[VAD] rms={rms:.0f} speech={in_speech} hang={hangover} "
                          f"sil={sil_count}/{SIL_BLKS} peak={chunk_peak:.0f}", flush=True)

                if sil_count >= SIL_BLKS and spk >= SPEECH_BLKS and current:
                    if chunk_peak >= sil_rms:
                        # Real speech detected in this chunk → send it
                        _submit(np.concatenate(current))
                        current = []
                    else:
                        # No speech reached the threshold — carry tail so a soft
                        # speech onset in the next chunk isn't lost.
                        tail_blks = SIL_BLKS
                        if len(current) > tail_blks:
                            current = current[-tail_blks:]
                        print(f"[VAD] Silence window, no speech "
                              f"(peak={chunk_peak:.0f} < {sil_rms}) "
                              f"— carrying tail forward", flush=True)
                    _reset_chunk()

                # ── Max chunk duration guard ───────────────────────────────
                chunk_dur = len(current) * BLOCK / native_sr
                if chunk_dur >= max_chunk_s and chunk_peak >= sil_rms:
                    print(f"[VAD] Max chunk duration {max_chunk_s}s reached — sending", flush=True)
                    _submit(np.concatenate(current))
                    current = []
                    _reset_chunk()
    except Exception as exc:
        print(f"[REC] Stream error: {exc}", flush=True)
        return None, [], None

    print(f"[REC] Hotkey released. RMS min={rms_min:.0f}  max={rms_max:.0f}  "
          f"silence-threshold={sil_rms}  chunks-sent={len(pending)}", flush=True)

    if len(all_data) < MIN_BLKS and not pending:
        print("[REC] Too short, ignoring.", flush=True)
        return None, [], None

    raw_full  = np.concatenate(all_data) if all_data else None
    raw_tail  = np.concatenate(current)  if current  else None

    full_audio = _resample_audio(raw_full, native_sr, target_sr) if raw_full is not None else None
    remaining  = _resample_audio(raw_tail, native_sr, target_sr) if raw_tail  is not None else None

    # Drop a very short tail when we already have pending sends
    if remaining is not None and len(remaining) < int(0.3 * target_sr) and pending:
        remaining = None

    return full_audio, pending, remaining


# ── Transcription ─────────────────────────────────────────────────────────────

FAILED_AUDIO_DIR = _app_dir() / "failed_audio"


def _post_wav_bytes(wav_bytes: bytes, config: dict) -> tuple[str | None, str | None]:
    """POST raw WAV bytes to the transcription server. Returns (text, error)."""
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


def _save_failed_wav(audio: np.ndarray, sr: int) -> Path:
    """Write audio to the failed_audio/ directory and return the path."""
    FAILED_AUDIO_DIR.mkdir(exist_ok=True)
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = FAILED_AUDIO_DIR / f"failed_{ts}.wav"
    sf.write(str(path), audio, sr, subtype="PCM_16")
    print(f"[FAILED] WAV saved: {path}", flush=True)
    return path


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


def reinsert_last_transcription():
    """Insert the last transcription again using the current focused-field strategy."""
    text = _last_typed[0]
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
        paste_text(text, method=insert_mode)


# ── Live-mode overlay indicator ───────────────────────────────────────────────
# A small always-on-top dot in the bottom-right corner.
# Only used when live_mode = True; invisible otherwise.

_OVL_IDLE         = 0
_OVL_RECORDING    = 1
_OVL_TRANSCRIBING = 2
_OVL_FAST_RECORDING    = 3
_OVL_FAST_TRANSCRIBING = 4

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

    # Initial off-screen placement; real position set on first show.
    win.geometry(f"{SIZE}x{SIZE}+-100+-100")

    # Any pixel painted with this exact colour becomes click-through.
    TRANSPARENT = "#010203"
    win.wm_attributes("-transparentcolor", TRANSPARENT)
    win.configure(bg=TRANSPARENT)

    canvas = tk.Canvas(win, width=SIZE, height=SIZE, bg=TRANSPARENT,
                       highlightthickness=0)
    canvas.pack(fill="both", expand=True)
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
        if state != _prev[0]:
            _prev[0] = state
            if state == _OVL_IDLE:
                win.withdraw()
            else:
                # Move to cursor position + small offset so the dot sits just
                # below and to the right of the cursor tip without obscuring it.
                cx, cy = _overlay_pos[0], _overlay_pos[1]
                win.geometry(f"{SIZE}x{SIZE}+{cx + OFFSET}+{cy + OFFSET}")
                canvas.itemconfig(dot, fill=_COLORS[state])
                win.deiconify()
                win.lift()
        if state in _FAST_STATES:
            pulse = 8 + (_tick[0] % 6) // 2
            _set_halo(radius=pulse, color=_COLORS[state], width=2)
        else:
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


def _join_chunks(texts: list[str]) -> str:
    """Join VAD chunk transcriptions into a single string.

    Parakeet adds terminal punctuation to every chunk it sees as a complete
    sentence.  When the VAD cuts mid-sentence the result looks like:
        "I was trying to explain. something important."
    Heuristic: if chunk[N] ends with '.' and chunk[N+1] starts with a
    lowercase letter, the period was added by the model at an artificial
    boundary — strip it so the joined text reads naturally.
    Other punctuation (! ? , ; :) is never stripped automatically.
    """
    cleaned: list[str] = []
    for i, text in enumerate(texts):
        text = text.strip()
        if not text:
            continue
        # Look ahead: if the next non-empty chunk starts with lowercase,
        # this chunk's trailing period is likely spurious.
        if text.endswith("."):
            next_text = next((t.strip() for t in texts[i + 1:] if t.strip()), "")
            if next_text and next_text[0].islower():
                text = text[:-1]  # drop the period
        cleaned.append(text)
    return " ".join(cleaned)


def run_session(config: dict, insert_mode: str = "type", profile: str = "main", hotkey_name: str = "ptt"):
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

    live_cfg, simple_cfg = session_mode_settings(config, insert_mode, profile)
    fancy = not simple_cfg
    live  = live_cfg and insert_mode == "type"
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

            full_audio, pending, remaining = _record_with_vad(config, hotkey_name=hotkey_name)

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

            if inline_status and fancy:
                stop_spin.set()
                spin_thread.join(timeout=1.0)
            if inline_status:
                _erase_status()
                time.sleep(config.get("erase_delay", 0.08))
            if overlay_status:
                _set_overlay(_OVL_IDLE)

            if result:
                _add_to_history({"time": _now_str(), "text": result})
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
                        paste_text(result, method=insert_mode)
                    else:
                        type_text(result, char_delay=config.get("char_delay", 0.0))
                    _last_typed[0] = result
                    print("[TYPE] Done.", flush=True)
            elif error:
                wav_path = _save_failed_wav(full_audio, config.get("sample_rate", 16000))
                _add_to_history({"time": _now_str(), "text": None,
                                 "error": error, "wav": str(wav_path)})
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

            full_audio, pending, remaining = _record_with_vad(
                config, on_chunk_ready=_on_chunk_during_hold, hotkey_name=hotkey_name)

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
                _add_to_history({"time": _now_str(), "text": full_result})
                if not focused:
                    print(f"[TYPE] Skipped — not a text input "
                          f"(class: {focused_cls!r}). Saved to history.", flush=True)
                else:
                    _last_typed[0] = field_content
                    print(f"[TYPE] Done — {len(field_content)} chars in "
                          f"{focused_cls!r}.", flush=True)
            elif error:
                wav_path = _save_failed_wav(full_audio, config.get("sample_rate", 16000))
                _add_to_history({"time": _now_str(), "text": None,
                                 "error": error, "wav": str(wav_path)})
                print(f"[TYPE] Error — typing message for 5 s: {error!r}", flush=True)
                type_text(error)
                time.sleep(5)
                delete_chars(len(error))
            else:
                print("[TYPE] Empty result — nothing typed.", flush=True)

    finally:
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
    MT = tk.Frame(nb, padx=10, pady=8)   # Microphone Test tab
    IC = tk.Frame(nb, padx=10, pady=8)   # Input Classes tab
    nb.add(MIC, text="  Microphone  ")
    nb.add(S,  text="  Advanced  ")
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

    tk.Label(S, text="Server URL:").grid(row=0, column=0, sticky="e", **pad)
    url_var = tk.StringVar(master=win, value=config["server_url"])
    tk.Entry(S, textvariable=url_var, width=46).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

    # Hotkey
    tk.Label(S, text="Hotkey:").grid(row=1, column=0, sticky="e", **pad)
    hotkey_frame = tk.Frame(S)
    hotkey_frame.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
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
    tk.Label(S, text="Fast hotkey:").grid(row=2, column=0, sticky="e", **pad)
    fast_frame = tk.Frame(S)
    fast_frame.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
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
    tk.Label(S, text="Last result hotkey:").grid(row=3, column=0, sticky="e", **pad)
    undo_frame = tk.Frame(S)
    undo_frame.grid(row=3, column=1, columnspan=2, sticky="w", **pad)
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
    tk.Label(S, text="Language (BCP-47):").grid(row=5, column=0, sticky="e", **pad)
    lang_var = tk.StringVar(master=win, value=config.get("language") or "")
    tk.Entry(S, textvariable=lang_var, width=16).grid(row=5, column=1, sticky="w", **pad)
    tk.Label(S, text="blank = auto", fg="grey").grid(row=5, column=2, sticky="w", **pad)

    # ── VAD ───────────────────────────────────────────────────────────────────
    vad_frame = tk.LabelFrame(S, text=" Voice Activity Detection ", padx=8, pady=4)
    vad_frame.grid(row=6, column=0, columnspan=3, sticky="ew", padx=2, pady=(6, 2))
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

    # ── Animations ────────────────────────────────────────────────────────────
    main_live_var   = tk.BooleanVar(master=win, value=bool(config.get("main_live_mode", config.get("live_mode", False))))
    main_simple_var = tk.BooleanVar(master=win, value=bool(config.get("main_simple_mode", config.get("simple_mode", True))))
    fast_live_var   = tk.BooleanVar(master=win, value=bool(config.get("fast_live_mode", config.get("live_mode", False))))
    fast_simple_var = tk.BooleanVar(master=win, value=bool(config.get("fast_simple_mode", config.get("simple_mode", True))))
    live_var = main_live_var
    simple_var = main_simple_var

    live_cb = tk.Checkbutton(
        S, text="Live transcription mode — stream each sentence as it arrives"
                " (uses a screen overlay dot for status)",
        variable=live_var, anchor="w",
    )
    live_cb.grid(row=7, column=0, columnspan=3, sticky="w", padx=2, pady=(6, 0))

    simple_cb = tk.Checkbutton(
        S, text="    Simple mode — use plain ® / ¿ indicators instead of animations"
                "  (SSH / terminal safe)   [classic mode only]",
        variable=simple_var, anchor="w",
    )
    simple_cb.grid(row=8, column=0, columnspan=3, sticky="w", padx=2, pady=(0, 2))

    def _update_simple_state(*_):
        state = "disabled" if live_var.get() else "normal"
        simple_cb.config(state=state)

    live_var.trace_add("write", _update_simple_state)
    _update_simple_state()   # set initial state

    fast_live_cb = tk.Checkbutton(
        S, text="Fast hotkey: live transcription mode",
        variable=fast_live_var, anchor="w",
    )
    fast_live_cb.grid(row=9, column=0, columnspan=3, sticky="w", padx=2, pady=(8, 0))

    fast_simple_cb = tk.Checkbutton(
        S, text="    Fast hotkey: simple mode (classic mode only; live only applies on typed-input targets)",
        variable=fast_simple_var, anchor="w",
    )
    fast_simple_cb.grid(row=10, column=0, columnspan=3, sticky="w", padx=2, pady=(0, 2))

    def _update_fast_simple_state(*_):
        fast_simple_cb.config(state="disabled" if fast_live_var.get() else "normal")

    fast_live_var.trace_add("write", _update_fast_simple_state)
    _update_fast_simple_state()

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

    def _selected_device():
        sel = mic_var.get()
        return None if sel == "(system default)" else int(sel.split(":")[0])

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
            with sd.InputStream(device=_selected_device(), samplerate=16000,
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
                for remaining in range(3, 0, -1):
                    win.after(0, lambda n=remaining: rec_btn.config(
                        text=f"Recording... {n}s", state="disabled"))
                    chunk = sd.rec(16000, samplerate=16000, channels=1,
                                   dtype="int16", device=_selected_device())
                    sd.wait()
                    rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
                    db  = 20 * np.log10(max(rms, 1.0) / 32768.0)
                    pct = max(0.0, min(100.0, (db + 60) / 60 * 100))
                    win.after(0, lambda p=pct, d=db: _update_meter(p, d))
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
            sd.play(_test_audio[0], samplerate=16000)

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

            else:
                # ── Success entry ──────────────────────────────────────────
                text    = entry.get("text", "")
                display = text if len(text) <= 72 else text[:69] + "..."
                tk.Label(row_f, text=display, anchor="w", justify="left",
                         font=("Segoe UI", 9), wraplength=400).grid(row=0, column=0, sticky="ew")

                def _make_copy(t):
                    def _copy():
                        win.clipboard_clear()
                        win.clipboard_append(t)
                    return _copy

                tk.Button(row_f, text="Copy", command=_make_copy(text),
                          width=6).grid(row=0, column=1, padx=(6, 0))
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
        new_cfg["server_url"] = url_var.get().strip()
        new_cfg["language"]   = lang_var.get().strip() or None
        sel = mic_var.get()
        new_cfg["microphone_index"] = (
            None if sel == "(system default)" else int(sel.split(":")[0])
        )
        if not new_cfg["server_url"]:
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
        new_cfg["main_live_mode"] = bool(main_live_var.get())
        new_cfg["main_simple_mode"] = bool(main_simple_var.get())
        new_cfg["fast_live_mode"] = bool(fast_live_var.get())
        new_cfg["fast_simple_mode"] = bool(fast_simple_var.get())
        new_cfg["live_mode"]   = new_cfg["main_live_mode"]
        new_cfg["simple_mode"] = new_cfg["main_simple_mode"]
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

def _make_icon(color: tuple[int, int, int] = (34, 197, 94)):
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(*color, 255))
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
        text = next(
            (e["text"] for e in reversed(list(_history)) if e.get("text")),
            None,
        )
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
        return any(e.get("text") for e in _history)

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
        pystray.MenuItem("Quit",              on_quit),
    )

    icon_img = _make_icon(_ICON_IDLE)
    tray_icon = pystray.Icon("overmultiasrsuite", icon_img, "OverMultiASRSuite (idle)", menu)

    # _on_hotkey runs on the keyboard library's internal hook thread.
    # Never touch pystray icon handles here — Win32 DestroyIcon crashes across threads.
    # Icon updates happen inside _session (a plain daemon thread) instead.
    def _on_hotkey(hotkey_name: str, insert_mode: str, profile: str):
        if _session_lock.locked():
            return  # Already recording — silently drop the repeat trigger
        print(f"[HOTKEY] Triggered ({profile}/{insert_mode}) — starting session.", flush=True)
        threading.Thread(target=_session, args=(hotkey_name, insert_mode, profile), daemon=True).start()

    def _session(hotkey_name: str, insert_mode: str, profile: str):
        try:
            active_icon = _ICON_FAST if profile == "fast" else _ICON_RECORDING
            active_label = "fast mode" if profile == "fast" else "recording"
            tray_icon.icon  = _make_icon(active_icon)
            tray_icon.title = f"OverMultiASRSuite ({active_label}…)"
            run_session(get_cfg(), insert_mode=insert_mode, profile=profile, hotkey_name=hotkey_name)
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

        try:
            bridge = NativeHotkeyBridge(hotkey_str, fast_hotkey_str, undo_str)
            bridge.start()
            _native_hotkeys = bridge
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

            threading.Thread(target=_watch_hold, daemon=True).start()
        except Exception as exc:
            _native_hotkeys = None
            print(f"[TRAY] Native helper unavailable ({exc}); falling back to keyboard library.", flush=True)
            try:
                keyboard.add_hotkey(hotkey_str, lambda: _on_hotkey("ptt", "auto", "main"), suppress=True)
                print(f"[TRAY] Hotkey registered: {hotkey_str}", flush=True)
            except Exception as hotkey_exc:
                print(f"[TRAY] ERROR registering hotkey {hotkey_str!r}: {hotkey_exc}", flush=True)
                print("[TRAY] Try running as Administrator or choose a different hotkey.", flush=True)

            if fast_hotkey_str:
                try:
                    keyboard.add_hotkey(fast_hotkey_str, lambda: _on_hotkey("fast_ptt", "paste_ctrl_v", "fast"), suppress=True)
                    print(f"[TRAY] Fast hotkey registered: {fast_hotkey_str}", flush=True)
                except Exception as fast_exc:
                    print(f"[TRAY] ERROR registering fast hotkey {fast_hotkey_str!r}: {fast_exc}", flush=True)

            if undo_str:
                try:
                    keyboard.add_hotkey(undo_str, reinsert_last_transcription, suppress=True)
                    print(f"[TRAY] Last-result hotkey registered: {undo_str}", flush=True)
                except Exception as undo_exc:
                    print(f"[TRAY] ERROR registering last-result hotkey {undo_str!r}: {undo_exc}", flush=True)

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

    print("OverMultiASRSuite", flush=True)
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
