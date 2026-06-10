#!/usr/bin/env python3
"""
Print the Win32 class name of the currently focused control.

This is a tiny standalone version of the main client's --focus utility. It does
not import or start the transcription client.
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import sys
import time
from pathlib import Path


APP_DIR = Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"

DEFAULT_INPUT_CLASSES = [
    "Edit",
    "RichEdit",
    "RichEdit20W",
    "RichEdit20A",
    "RichEdit50W",
    "RICHEDIT60W",
    "Scintilla",
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "Chrome_RenderWidgetHostHWND",
    "MozillaWindowClass",
    "MozillaContentWindowClass",
    "WebViewWnd",
    "TListBox",
]

DEFAULT_TYPE_INPUT_CLASSES = [
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("hwndActive", ctypes.wintypes.HWND),
        ("hwndFocus", ctypes.wintypes.HWND),
        ("hwndCapture", ctypes.wintypes.HWND),
        ("hwndMenuOwner", ctypes.wintypes.HWND),
        ("hwndMoveSize", ctypes.wintypes.HWND),
        ("hwndCaret", ctypes.wintypes.HWND),
        ("rcCaret", ctypes.wintypes.RECT),
    ]


user32 = ctypes.windll.user32


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"Warning: could not read {CONFIG_FILE}: {exc}", file=sys.stderr)
        return {}


def get_class_name(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def get_window_text(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def focused_handles() -> tuple[int, int]:
    foreground = user32.GetForegroundWindow()
    thread_id = user32.GetWindowThreadProcessId(foreground, None)
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return foreground, foreground
    return foreground, info.hwndFocus or foreground


def choose_insert_mode(
    focused_class: str,
    type_classes: set[str],
    right_click_classes: set[str],
) -> str:
    if focused_class in type_classes:
        return "type"
    if focused_class in right_click_classes:
        return "paste_right_click"
    return "paste_ctrl_v"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show the Win32 class for the currently focused input control."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds to wait before checking focus, so you can click the target field.",
    )
    args = parser.parse_args()

    if args.delay > 0:
        print(f"Focus the target field now. Checking in {args.delay:g}s...")
        time.sleep(args.delay)

    cfg = load_config()
    input_classes = set(cfg.get("input_classes") or DEFAULT_INPUT_CLASSES)
    type_classes = set(cfg.get("type_input_classes") or DEFAULT_TYPE_INPUT_CLASSES)
    right_click_classes = set(cfg.get("right_click_paste_input_classes") or [])

    foreground, focused = focused_handles()
    foreground_class = get_class_name(foreground)
    focused_class = get_class_name(focused)
    allowed = focused_class in input_classes

    print(f"Foreground title : {get_window_text(foreground)!r}")
    print(f"Foreground class : {foreground_class!r}")
    print(f"Focused class    : {focused_class!r}")
    print(f"Typing allowed   : {allowed}")
    if allowed:
        print(f"Insert mode      : {choose_insert_mode(focused_class, type_classes, right_click_classes)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
