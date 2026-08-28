"""Synthetic keyboard input via SendInput.

Text is injected with KEYEVENTF_UNICODE, which delivers a UTF-16 code unit
directly rather than simulating a key. No virtual key, no scan code, no keyboard
layout, no Shift and no Caps Lock -- capitals, punctuation, em-dashes and
accented characters all go through identically.

Every event we inject carries a signature in dwExtraInfo so our own low-level
hook can recognise and ignore it. Without that, injected keystrokes re-enter the
hook and are processed as if the user had typed them.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .keys import VK_BACK, is_extended

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# "LWST" -- marks events this process injected.
INJECT_SIGNATURE = 0x4C575354

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def _key_event(vk: int, scan: int, flags: int) -> INPUT:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(
        wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=INJECT_SIGNATURE
    )
    return event


def unicode_events(text: str) -> list[INPUT]:
    """One down/up pair per UTF-16 code unit.

    Characters outside the BMP arrive as surrogate pairs; each half is sent as
    its own event, which is what Windows expects.
    """
    events: list[INPUT] = []
    encoded = text.encode("utf-16-le")
    for i in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[i : i + 2], "little")
        events.append(_key_event(0, unit, KEYEVENTF_UNICODE))
        events.append(_key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def vk_events(vk: int, count: int = 1) -> list[INPUT]:
    """Down/up pairs for a real virtual key (backspace, Escape, ...)."""
    flags = KEYEVENTF_EXTENDEDKEY if is_extended(vk) else 0
    events: list[INPUT] = []
    for _ in range(count):
        events.append(_key_event(vk, 0, flags))
        events.append(_key_event(vk, 0, flags | KEYEVENTF_KEYUP))
    return events


def vk_down(vk: int) -> INPUT:
    flags = KEYEVENTF_EXTENDEDKEY if is_extended(vk) else 0
    return _key_event(vk, 0, flags)


def vk_up(vk: int) -> INPUT:
    flags = KEYEVENTF_EXTENDEDKEY if is_extended(vk) else 0
    return _key_event(vk, 0, flags | KEYEVENTF_KEYUP)


def send(events: list[INPUT]) -> int:
    """Dispatch a batch of events in a single SendInput call.

    Batching matters: a whole sentence lands essentially instantly, and Windows
    will not interleave another thread's input into the middle of the batch.
    """
    if not events:
        return 0
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise ctypes.WinError(ctypes.get_last_error())
    return sent


def type_text(text: str) -> int:
    """Type a string. Returns the number of events dispatched."""
    return send(unicode_events(text))


def backspace(count: int) -> int:
    if count <= 0:
        return 0
    return send(vk_events(VK_BACK, count))


def apply_edit(backspaces: int, text: str) -> None:
    """Apply one edit as a single atomic batch.

    Backspaces and the replacement text go in one SendInput call so nothing can
    be observed, or interrupted, half-applied.
    """
    events = vk_events(VK_BACK, backspaces) if backspaces > 0 else []
    events.extend(unicode_events(text))
    send(events)


def clear_modifiers() -> None:
    """Tell the target application that every modifier is up.

    Belt and braces alongside the hook's deferred forwarding: if a modifier
    down-event ever did leak, this neutralises it before we type.
    """
    from .keys import (
        VK_LCONTROL,
        VK_LMENU,
        VK_LSHIFT,
        VK_LWIN,
        VK_RCONTROL,
        VK_RMENU,
        VK_RSHIFT,
        VK_RWIN,
    )

    send([vk_up(vk) for vk in (VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU,
                               VK_LSHIFT, VK_RSHIFT, VK_LWIN, VK_RWIN)])
