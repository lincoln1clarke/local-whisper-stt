"""Low-level keyboard hook (WH_KEYBOARD_LL).

This is the only mechanism that can *suppress* a key, and suppression is what
keeps the held modifiers from leaking into the target application.

Security note from PLAN.md, restated because it governs this file: this hook
receives *every* keystroke system-wide. There is no way to subscribe to only two
keys. The callback below must therefore stay short and auditable -- it makes a
decision and returns. It never buffers, never logs key data, and never writes a
keystroke anywhere.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

from .sendinput import INJECT_SIGNATURE

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

LLKHF_INJECTED = 0x00000010

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT)
)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (
    wintypes.HHOOK,
    ctypes.c_int,
    wintypes.WPARAM,
    ctypes.POINTER(KBDLLHOOKSTRUCT),
)
user32.CallNextHookEx.restype = ctypes.c_long
user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)


class KeyboardHook:
    """Installs a low-level keyboard hook and pumps its message loop.

    ``on_key(vk, is_down) -> bool`` returns True to swallow the event.
    """

    def __init__(self, on_key: Callable[[int, bool], bool]) -> None:
        self._on_key = on_key
        self._handle: int | None = None
        self._thread_id: int | None = None
        # Keep a reference: if the trampoline is garbage collected while the
        # hook is installed, Windows calls into freed memory.
        self._proc = HOOKPROC(self._callback)
        self._stop = threading.Event()

    def _callback(self, code, wparam, lparam):
        if code < 0:
            return user32.CallNextHookEx(None, code, wparam, lparam)

        info = lparam[0]

        # Ignore what we injected ourselves, or we process our own typing.
        if info.dwExtraInfo == INJECT_SIGNATURE:
            return user32.CallNextHookEx(None, code, wparam, lparam)

        is_down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = wparam in (WM_KEYUP, WM_SYSKEYUP)
        if not (is_down or is_up):
            return user32.CallNextHookEx(None, code, wparam, lparam)

        try:
            swallow = self._on_key(int(info.vkCode), is_down)
        except Exception:
            # A crash here would wedge keyboard input system-wide. Never let an
            # exception escape into Windows; fail open so keys keep working.
            swallow = False

        if swallow:
            return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def install(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def uninstall(self) -> None:
        if self._handle:
            user32.UnhookWindowsHookEx(self._handle)
            self._handle = None

    def pump(self) -> None:
        """Run the message loop. Blocks until :meth:`stop` is called.

        A low-level hook only fires on a thread with a running message loop.
        """
        msg = wintypes.MSG()
        while not self._stop.is_set():
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def __enter__(self) -> "KeyboardHook":
        self.install()
        return self

    def __exit__(self, *exc) -> None:
        self.uninstall()
