"""Microphone capture via the winmm waveIn API.

PortAudio (sounddevice) would drag numpy into the always-resident supervisor and
take it from ~25 MB to ~75 MB. The MME API costs a couple of hundred lines of
ctypes instead and keeps the resident process free of third-party code.

Its higher buffer latency (~30-50 ms) is irrelevant for dictation.

Callbacks are avoided deliberately: waveIn is opened with CALLBACK_EVENT and a
worker thread waits on that event, so audio never calls into Python from a
driver thread.
"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes
from dataclasses import dataclass

winmm = ctypes.WinDLL("winmm", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WAVE_FORMAT_PCM = 1
WAVE_MAPPER = -1
CALLBACK_EVENT = 0x00050000
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102

DWORD_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    # POINTER(c_char), never c_char_p. ctypes auto-converts a c_char_p struct
    # field to a Python bytes object on attribute access and truncates it at
    # the first NUL -- and PCM audio is full of NULs. Reading the buffer back
    # through such a field yields arbitrary heap memory rather than audio.
    ("lpData", ctypes.POINTER(ctypes.c_char)),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", DWORD_PTR),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(WAVEHDR)),
    ("reserved", DWORD_PTR),
]


class WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


class AudioError(RuntimeError):
    pass


def _check(code: int, what: str) -> None:
    if code != MMSYSERR_NOERROR:
        raise AudioError(f"{what} failed with MMSYSERR {code}")


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    channels: int


def list_devices() -> list[Device]:
    """Enumerate capture devices, for --list-devices."""
    count = winmm.waveInGetNumDevs()
    devices: list[Device] = []
    for i in range(count):
        caps = WAVEINCAPSW()
        if winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            devices.append(Device(i, caps.szPname, caps.wChannels))
    return devices


class Recorder:
    """Records 16-bit mono PCM into an internal queue.

    Opened on key-down and closed on release -- never held open. A standing open
    microphone is a liability that a key-triggered one is not.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        device: int | None = None,
        buffer_ms: int = 100,
        buffer_count: int = 8,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = WAVE_MAPPER if device is None else device
        self.bytes_per_buffer = int(sample_rate * 2 * buffer_ms / 1000)
        self.buffer_count = buffer_count
        self._handle = wintypes.HANDLE()
        self._event = None
        self._headers: list[WAVEHDR] = []
        self._blocks: list[ctypes.Array] = []
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._bytes_recorded = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        fmt = WAVEFORMATEX(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=1,
            nSamplesPerSec=self.sample_rate,
            nAvgBytesPerSec=self.sample_rate * 2,
            nBlockAlign=2,
            wBitsPerSample=16,
            cbSize=0,
        )
        self._event = kernel32.CreateEventW(None, False, False, None)
        if not self._event:
            raise ctypes.WinError(ctypes.get_last_error())

        _check(
            winmm.waveInOpen(
                ctypes.byref(self._handle),
                self.device,
                ctypes.byref(fmt),
                DWORD_PTR(self._event),
                DWORD_PTR(0),
                CALLBACK_EVENT,
            ),
            "waveInOpen",
        )

        for _ in range(self.buffer_count):
            block = ctypes.create_string_buffer(self.bytes_per_buffer)
            header = WAVEHDR(
                lpData=ctypes.cast(block, ctypes.POINTER(ctypes.c_char)),
                dwBufferLength=self.bytes_per_buffer,
                dwFlags=0,
            )
            _check(
                winmm.waveInPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header)),
                "waveInPrepareHeader",
            )
            _check(
                winmm.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(header)),
                "waveInAddBuffer",
            )
            self._blocks.append(block)
            self._headers.append(header)

        self._running.set()
        self._thread = threading.Thread(target=self._harvest, name="lwstt-audio", daemon=True)
        self._thread.start()
        _check(winmm.waveInStart(self._handle), "waveInStart")

    def drain_ready(self, requeue: bool = True) -> int:
        """Move every completed buffer into the queue. Returns bytes taken.

        Split out from the harvest thread so the buffer-read path is testable
        without a microphone -- this is where a NUL-truncating pointer read
        silently substituted heap garbage for audio.
        """
        taken = 0
        for header, block in zip(self._headers, self._blocks):
            if not header.dwFlags & WHDR_DONE:
                continue
            n = header.dwBytesRecorded
            if n:
                # Read from the Python buffer we own rather than back through
                # header.lpData: same memory, no pointer games, and .raw keeps
                # every NUL byte intact.
                self._queue.put(block.raw[:n])
                self._bytes_recorded += n
                taken += n
            header.dwFlags &= ~WHDR_DONE
            header.dwBytesRecorded = 0
            if requeue:
                winmm.waveInAddBuffer(
                    self._handle, ctypes.byref(header), ctypes.sizeof(header)
                )
        return taken

    def _harvest(self) -> None:
        while self._running.is_set():
            kernel32.WaitForSingleObject(self._event, 50)
            if not self._running.is_set():
                return
            self.drain_ready()

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        winmm.waveInStop(self._handle)
        winmm.waveInReset(self._handle)
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        for header in self._headers:
            winmm.waveInUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
        self._headers.clear()
        self._blocks.clear()
        winmm.waveInClose(self._handle)
        self._handle = wintypes.HANDLE()
        if self._event:
            kernel32.CloseHandle(self._event)
            self._event = None

    # -- consumption -----------------------------------------------------

    def read_available(self) -> bytes:
        """Drain everything captured so far. Never blocks."""
        parts: list[bytes] = []
        while True:
            try:
                parts.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return b"".join(parts)

    @property
    def seconds_recorded(self) -> float:
        return self._bytes_recorded / (self.sample_rate * 2)

    def __enter__(self) -> "Recorder":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
