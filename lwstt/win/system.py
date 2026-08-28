"""Small Windows facilities: power state, foreground window, beeps, dialogs,
single-instance guard.

All ctypes against system DLLs -- nothing third-party enters the supervisor.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183

# Explicit prototypes: on 64-bit, ctypes defaults to c_int and truncates handles.
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.MessageBoxW.argtypes = (
    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT,
)
kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

MB_OK = 0x0
MB_ICONERROR = 0x10
MB_SYSTEMMODAL = 0x1000


# -- power ---------------------------------------------------------------


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def on_battery() -> bool:
    """True when running on battery.

    Read by the idle timer itself rather than cached from the last dictation, so
    unplugging mid-session takes effect immediately. Unknown power state (255)
    is treated as mains -- a desktop should not get battery timings.
    """
    status = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return status.ACLineStatus == 0


def battery_percent() -> int | None:
    status = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    value = status.BatteryLifePercent & 0xFF
    return None if value == 255 else value


# -- foreground window ---------------------------------------------------


def foreground_window() -> int:
    """Handle of the window with focus, as an integer.

    Captured when the combo arms and rechecked before every batch of typing. If
    it changes mid-dictation we abort rather than backspace into a document the
    earlier characters never went into.
    """
    return int(user32.GetForegroundWindow() or 0)


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


# -- audible feedback ----------------------------------------------------

ARM_TONE = (880, 60)
ERROR_TONE = (300, 120)
COMMIT_TONE = (1320, 40)


def beep(frequency: int, duration_ms: int) -> None:
    """Beep without blocking the caller.

    winsound.Beep is synchronous, and the hook callback must never block --
    a stalled hook stalls keyboard input system-wide.
    """

    def run() -> None:
        try:
            import winsound

            winsound.Beep(frequency, duration_ms)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


# -- fatal errors --------------------------------------------------------


def message_box(text: str, title: str = "local-whisper-stt") -> None:
    """Last-resort visible error.

    With no tray icon and no console, a startup failure is otherwise completely
    silent -- the tool looks installed and simply does nothing.
    """
    user32.MessageBoxW(None, text, title, MB_OK | MB_ICONERROR | MB_SYSTEMMODAL)


# -- single instance -----------------------------------------------------


class SingleInstance:
    """Named-mutex guard.

    A second launch does not install a rival hook; it becomes a client and sends
    a command to the running instance over the control pipe.
    """

    def __init__(self, name: str = "Local\\lwstt-supervisor") -> None:
        self.name = name
        self._handle = None
        self.already_running = False

    def acquire(self) -> bool:
        self._handle = kernel32.CreateMutexW(None, True, self.name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        return not self.already_running

    def release(self) -> None:
        if self._handle:
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
            self._handle = None
