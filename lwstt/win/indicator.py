"""The recording indicator: a small red dot beside the notification area.

No persistent tray icon -- nothing at all while idle. Shell_NotifyIcon was
rejected because Windows 11 hides new tray icons in the overflow flyout by
default, so an icon that only exists during recording would frequently not be
visible, defeating the point.

Instead: a layered, click-through, topmost window positioned from the
Shell_TrayWnd rect, with a bottom-right fallback for unusual taskbar setups.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002

DOT_SIZE = 10
DOT_COLOR = 0x0000E0  # COLORREF is 0x00BBGGRR -- this is red.

LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

# Explicit prototypes are not optional on 64-bit: without them ctypes assumes
# c_int, which truncates window handles and overflows on LPARAM.
user32.DefWindowProcW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)
user32.CreateWindowExW.restype = wintypes.HWND
user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = (
    wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
)
user32.FindWindowExW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.SetWindowPos.argtypes = (
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.DestroyWindow.argtypes = (wintypes.HWND,)
user32.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HRGN, wintypes.BOOL)
user32.SetLayeredWindowAttributes.argtypes = (
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD,
)
gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateEllipticRgn.argtypes = (ctypes.c_int,) * 4
gdi32.CreateEllipticRgn.restype = wintypes.HRGN
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def tray_rect() -> tuple[int, int, int, int] | None:
    """Screen rect of the notification area, if it can be found."""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
    target = notify or tray
    rect = wintypes.RECT()
    if not user32.GetWindowRect(target, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def dot_position() -> tuple[int, int]:
    """Where to put the dot: just left of the notification area.

    Falls back to the bottom-right corner above the taskbar when the tray rect
    cannot be resolved (unusual taskbar placement, multi-monitor oddities).
    """
    rect = tray_rect()
    if rect:
        left, top, _right, bottom = rect
        x = left - DOT_SIZE - 6
        y = top + (bottom - top - DOT_SIZE) // 2
        if x > 0 and y > 0:
            return x, y
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    return screen_w - 120, screen_h - 60


class RecordingDot:
    """Shown only while recording. Never interactive."""

    _CLASS_NAME = "LwsttRecordingDot"
    _class_registered = False

    def __init__(self, size: int = DOT_SIZE) -> None:
        self.size = size
        self._hwnd = None
        self._brush = None
        self._proc = WNDPROC(self._wndproc)
        self._topmost_timer: threading.Timer | None = None
        self._visible = False

    def _wndproc(self, hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _ensure_class(self) -> None:
        if RecordingDot._class_registered:
            return
        self._brush = gdi32.CreateSolidBrush(DOT_COLOR)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hbrBackground = self._brush
        wc.lpszClassName = self._CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        RecordingDot._class_registered = True

    def _create(self) -> None:
        self._ensure_class()
        x, y = dot_position()
        style_ex = (
            WS_EX_LAYERED
            | WS_EX_TRANSPARENT
            | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE
        )
        self._hwnd = user32.CreateWindowExW(
            style_ex,
            self._CLASS_NAME,
            None,
            WS_POPUP,
            x,
            y,
            self.size,
            self.size,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        # Round it off so it reads as a dot rather than a square.
        region = gdi32.CreateEllipticRgn(0, 0, self.size, self.size)
        user32.SetWindowRgn(self._hwnd, region, True)
        user32.SetLayeredWindowAttributes(self._hwnd, 0, 230, LWA_ALPHA)

    def _reassert_topmost(self) -> None:
        """The taskbar is itself topmost, so the dot can fall behind it.

        Cheap to re-assert, and only runs while a dictation is in progress.
        """
        if not self._visible or not self._hwnd:
            return
        user32.SetWindowPos(
            self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        )
        self._topmost_timer = threading.Timer(1.0, self._reassert_topmost)
        self._topmost_timer.daemon = True
        self._topmost_timer.start()

    def show(self) -> None:
        try:
            if self._hwnd is None:
                self._create()
            x, y = dot_position()
            user32.SetWindowPos(
                self._hwnd, HWND_TOPMOST, x, y, self.size, self.size,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True
            self._reassert_topmost()
        except OSError:
            # A missing indicator must never stop a dictation.
            self._visible = False

    def hide(self) -> None:
        self._visible = False
        if self._topmost_timer:
            self._topmost_timer.cancel()
            self._topmost_timer = None
        if self._hwnd:
            user32.ShowWindow(self._hwnd, SW_HIDE)

    def destroy(self) -> None:
        self.hide()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
